"""Project-scoped governance for Agent initiated n8n administration.

The model never receives the n8n API key or credential material.  It creates a
bounded operation proposal; this service applies policy, persists an immutable
digest, obtains approval, and asks a server-side broker to perform the call.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

import requests

import database
from n8n_gmail_crypto import AesGcmContentCipher
from n8n_lifecycle import N8N_BASE_URL


POLICY_MODES = {"off", "restricted", "full_audit"}
ELEVATION_POLICIES = {"one_hour", "session", "persistent", "smart"}
OPERATIONS = {
    "create_draft", "update_draft", "publish", "activate", "deactivate",
    "execute", "delete", "credential_create", "credential_update",
    "credential_delete", "credential_bind",
}
PROTECTED_WORKFLOW_NAMES = {
    "workbench-gmail-inbound-v1", "workbench-gmail-send-v1",
    "workbench-agent-bridge-v1", "workbench-approval-gate-v1",
    "workbench-approval-bridge-v1",
}
_NEW_WORKFLOW_RESOURCE = "__new__"
MUTATING_OPERATIONS = OPERATIONS
SAFE_DRAFT_OPERATIONS = {"create_draft", "update_draft"}
HIGH_RISK_MARKERS = (
    "executecommand", "readwritefile", "readbinaryfile",
    "writebinaryfile", "code", "function", "community", "filesystem",
)
SECRET_KEY_RE = re.compile(
    r"(?i)(password|secret|token|api[_-]?key|authorization|cookie|private[_-]?key)"
)
WORKFLOW_DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
_UNSET = object()


class N8nGovernanceError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime] = None) -> str:
    return (value or _now()).isoformat()


def _parse_time(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


def _contains_secret(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            (str(key) != "secret_handle" and SECRET_KEY_RE.search(str(key)))
            or _contains_secret(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_secret(item) for item in value)
    return False


def _safe_json(value: Any, *, limit: int = 250_000) -> Any:
    encoded = _canonical(value)
    if len(encoded.encode("utf-8")) > limit:
        raise N8nGovernanceError("N8N_OPERATION_TOO_LARGE", "The n8n operation is too large.", status_code=413)
    if _contains_secret(value):
        raise N8nGovernanceError(
            "N8N_SECRET_IN_PROPOSAL",
            "Secrets must be supplied through the dedicated secure form.",
            status_code=422,
        )
    return json.loads(encoded)


def _sanitize_proposed_workflow(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise N8nGovernanceError(
            "N8N_WORKFLOW_INVALID",
            "A workflow object is required.",
            status_code=422,
        )
    safe = _safe_json(value)
    # Lifecycle, ownership and revision fields are server-controlled.  The
    # Public API receives only workflow definition fields reviewed by policy.
    allowed = {
        key: safe[key]
        for key in ("name", "nodes", "connections", "settings", "staticData", "pinData")
        if key in safe
    }
    if not str(allowed.get("name") or "").strip():
        raise N8nGovernanceError(
            "N8N_WORKFLOW_NAME_REQUIRED",
            "The workflow name is required.",
            status_code=422,
        )
    if not isinstance(allowed.get("nodes"), list):
        raise N8nGovernanceError(
            "N8N_WORKFLOW_NODES_REQUIRED",
            "The workflow nodes must be an array.",
            status_code=422,
        )
    if "connections" in allowed and not isinstance(allowed["connections"], Mapping):
        raise N8nGovernanceError(
            "N8N_WORKFLOW_CONNECTIONS_INVALID",
            "The workflow connections must be an object.",
            status_code=422,
        )
    return json.loads(_canonical(allowed))


def _node_types(payload: Mapping[str, Any]) -> list[str]:
    graph = payload.get("workflow") if isinstance(payload.get("workflow"), Mapping) else payload
    nodes = graph.get("nodes") if isinstance(graph, Mapping) else None
    if not isinstance(nodes, list):
        return []
    return [str(node.get("type") or "") for node in nodes if isinstance(node, Mapping)]


def _node_types_from_snapshot(snapshot: Optional[Mapping[str, Any]]) -> list[str]:
    if not snapshot:
        return []
    facts = _facts_from_snapshot(snapshot)
    return [
        str(node.get("type") or "")
        for node in facts.get("nodes", [])
        if isinstance(node, Mapping)
    ]


def _is_high_risk(payload: Mapping[str, Any]) -> bool:
    if payload.get("high_risk") is True:
        return True
    for node_type in _node_types(payload):
        normalized = node_type.casefold()
        if _node_type_is_high_risk(normalized):
            return True
        if normalized and not normalized.startswith((
            "n8n-nodes-base.", "n8n-nodes-langchain.",
            "@n8n/n8n-nodes-langchain.",
        )):
            return True
    return False


def _node_types_are_high_risk(node_types: list[str]) -> bool:
    for node_type in node_types:
        normalized = node_type.casefold()
        if _node_type_is_high_risk(normalized):
            return True
        if normalized and not normalized.startswith((
            "n8n-nodes-base.", "n8n-nodes-langchain.",
            "@n8n/n8n-nodes-langchain.",
        )):
            return True
    return False


def _node_type_is_high_risk(normalized: str) -> bool:
    """Classify nodes that can hide autonomous tools behind one canvas node.

    The reviewed Workbench Agent is represented by an ordinary Execute
    Sub-workflow node and is governed by an opaque server-side binding.  Native
    n8n Agent/Tool nodes do not have that boundary: they can hold their own
    credentials and select tools at runtime.  Keep them searchable for Full
    Audit planning, but never treat them as a low-risk restricted-mode node.
    """

    if any(marker in normalized for marker in HIGH_RISK_MARKERS):
        return True
    leaf = normalized.rsplit(".", 1)[-1]
    if normalized.startswith("@n8n/n8n-nodes-langchain."):
        return "agent" in leaf or leaf.endswith("tool") or "toolexecutor" in leaf
    if normalized.startswith("n8n-nodes-langchain."):
        return "agent" in leaf or leaf.endswith("tool") or "toolexecutor" in leaf
    return leaf in {"messageanagent", "tool", "toolexecutor"} or leaf.endswith("agenttool")


def _normalized_workflow_name(value: Any) -> str:
    """Normalize display names and template ids to the same protected key."""
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")


def _is_protected_workflow_name(value: Any) -> bool:
    return _normalized_workflow_name(value) in PROTECTED_WORKFLOW_NAMES


def _safe_external_target(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("={{") or text.startswith("{{"):
        return "dynamic-expression"
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname.casefold()}{port}"


def _workflow_facts(workflow: Mapping[str, Any]) -> dict[str, Any]:
    """Return bounded, secret-free facts used for review and canonical diff."""
    nodes = workflow.get("nodes") if isinstance(workflow.get("nodes"), list) else []
    node_facts: list[dict[str, Any]] = []
    credential_aliases: set[str] = set()
    external_targets: set[str] = set()
    for index, node in enumerate(nodes[:500]):
        if not isinstance(node, Mapping):
            continue
        node_type = str(node.get("type") or "")[:255]
        parameters = node.get("parameters") if isinstance(node.get("parameters"), Mapping) else {}
        node_facts.append({
            "id": str(node.get("id") or f"node-{index}")[:255],
            "name": str(node.get("name") or "")[:255],
            "type": node_type,
            # Values stay server-side. Reviewers get only changed key names and
            # a digest proving the reviewed parameter snapshot is immutable.
            "parameter_keys": sorted(str(key)[:128] for key in parameters)[:100],
            "parameter_digest": _digest(parameters),
        })
        credentials = node.get("credentials")
        if isinstance(credentials, Mapping):
            for credential in credentials.values():
                if isinstance(credential, Mapping):
                    alias = str(credential.get("name") or "").strip()
                    if alias:
                        credential_aliases.add(alias[:128])
        stack = [parameters] if isinstance(parameters, (Mapping, list)) else []
        while stack:
            current = stack.pop()
            if isinstance(current, Mapping):
                for key, item in current.items():
                    if isinstance(item, (Mapping, list)):
                        stack.append(item)
                    elif str(key).casefold() in {"url", "endpoint", "baseurl", "webhookurl"}:
                        target = _safe_external_target(item)
                        if target:
                            external_targets.add(target)
            elif isinstance(current, list):
                stack.extend(item for item in current if isinstance(item, (Mapping, list)))
        normalized_type = node_type.casefold()
        if normalized_type.startswith("n8n-nodes-base."):
            service = normalized_type.removeprefix("n8n-nodes-base.").split(".", 1)[0]
            if service not in {
                "set", "if", "switch", "merge", "splitinbatches", "noop",
                "manualtrigger", "scheduletrigger", "webhook", "respondtowebhook",
            }:
                external_targets.add(f"service:{service}")
    node_facts.sort(key=lambda item: (item["id"], item["name"], item["type"]))
    edges: list[dict[str, Any]] = []
    connections = workflow.get("connections") if isinstance(workflow.get("connections"), Mapping) else {}
    for source_name, output_groups in list(connections.items())[:500]:
        if not isinstance(output_groups, Mapping):
            continue
        for output_type, branches in output_groups.items():
            if not isinstance(branches, list):
                continue
            for output_index, targets in enumerate(branches[:64]):
                if not isinstance(targets, list):
                    continue
                for target in targets[:64]:
                    if not isinstance(target, Mapping):
                        continue
                    edges.append({
                        "from": str(source_name)[:255],
                        "to": str(target.get("node") or "")[:255],
                        "output": str(output_type)[:64],
                        "output_index": output_index,
                        "input": str(target.get("type") or "main")[:64],
                        "input_index": max(0, int(target.get("index") or 0)),
                    })
    edges.sort(key=lambda item: (
        item["from"], item["output"], item["output_index"], item["to"], item["input_index"]
    ))
    return {
        "name": str(workflow.get("name") or "")[:255],
        "active": bool(workflow.get("active")),
        "nodes": node_facts,
        "edges": edges[:2000],
        "external_targets": sorted(external_targets)[:128],
        "credential_aliases": sorted(credential_aliases)[:128],
    }


def _workflow_snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    provided = str(snapshot.get("snapshot_digest") or "")
    if WORKFLOW_DIGEST_RE.fullmatch(provided):
        return provided
    workflow = snapshot.get("workflow") if isinstance(snapshot.get("workflow"), Mapping) else {}
    return _digest({
        "id": str(snapshot.get("id") or ""),
        "name": str(snapshot.get("name") or workflow.get("name") or ""),
        "active": bool(snapshot.get("active", workflow.get("active"))),
        "updated_at": snapshot.get("updated_at"),
        "facts": snapshot.get("facts") if isinstance(snapshot.get("facts"), Mapping) else _workflow_facts(workflow),
    })


def _facts_from_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(snapshot.get("facts"), Mapping):
        return json.loads(_canonical(snapshot["facts"]))
    workflow = snapshot.get("workflow") if isinstance(snapshot.get("workflow"), Mapping) else {
        "name": snapshot.get("name"), "active": snapshot.get("active"), "nodes": [],
    }
    return _workflow_facts(workflow)


def _canonical_workflow_diff(
    operation: str,
    payload: Mapping[str, Any],
    before_snapshot: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    before = _facts_from_snapshot(before_snapshot or {}) if before_snapshot else None
    if operation == "create_draft":
        proposed = payload.get("workflow") if isinstance(payload.get("workflow"), Mapping) else {}
        after = _workflow_facts(proposed)
    elif operation == "update_draft":
        proposed = payload.get("workflow") if isinstance(payload.get("workflow"), Mapping) else {}
        after = _workflow_facts(proposed)
    elif operation in {"activate", "publish", "deactivate"}:
        after = dict(before or {})
        after["active"] = operation != "deactivate"
    elif operation == "delete":
        after = None
    else:
        after = before

    before_nodes = {item["id"]: item for item in (before or {}).get("nodes", [])}
    after_nodes = {item["id"]: item for item in (after or {}).get("nodes", [])}
    before_edges = {_canonical(item): item for item in (before or {}).get("edges", [])}
    after_edges = {_canonical(item): item for item in (after or {}).get("edges", [])}
    return {
        "source": "server",
        "before": before,
        "after": after,
        "nodes": {
            "added": [after_nodes[key] for key in sorted(after_nodes.keys() - before_nodes.keys())],
            "removed": [before_nodes[key] for key in sorted(before_nodes.keys() - after_nodes.keys())],
            "changed": [
                {"before": before_nodes[key], "after": after_nodes[key]}
                for key in sorted(before_nodes.keys() & after_nodes.keys())
                if before_nodes[key] != after_nodes[key]
            ],
        },
        "connections": {
            "added": [after_edges[key] for key in sorted(after_edges.keys() - before_edges.keys())],
            "removed": [before_edges[key] for key in sorted(before_edges.keys() - after_edges.keys())],
        },
        "external_targets": {
            "before": (before or {}).get("external_targets", []),
            "after": (after or {}).get("external_targets", []),
        },
        "credential_aliases": {
            "before": (before or {}).get("credential_aliases", []),
            "after": (after or {}).get("credential_aliases", []),
        },
    }


def _security_audit_digest(report: Any) -> str:
    if not isinstance(report, Mapping) or not report:
        raise N8nGovernanceError(
            "N8N_SECURITY_AUDIT_UNVERIFIABLE",
            "The n8n security audit result could not be verified.",
            status_code=409,
        )
    # n8n 2.x returns either [] (normalized by the Broker to the explicit
    # clean envelope below) or a mapping of report title -> {risk, sections}.
    # `risk` is a category such as "nodes", not a severity.  Any finding in
    # the executable-surface categories must therefore fail closed; treating
    # the category string as a benign severity would allow risky nodes or
    # filesystem access to pass the gate.
    if (
        report.get("status") == "clean"
        and report.get("verified") is True
        and report.get("findings") == []
    ):
        return _digest(report)

    official_categories = {"credentials", "database", "filesystem", "nodes", "instance"}
    blocking_categories = {"database", "filesystem", "nodes", "instance"}
    official_seen = False
    malformed_official = False
    blocking_findings = False
    for value in report.values():
        if not isinstance(value, Mapping):
            continue
        category = str(value.get("risk") or "").casefold()
        if category not in official_categories:
            continue
        official_seen = True
        sections = value.get("sections")
        if not isinstance(sections, list) or any(not isinstance(section, Mapping) for section in sections):
            malformed_official = True
            continue
        if category in blocking_categories and sections:
            blocking_findings = True
    if malformed_official:
        raise N8nGovernanceError(
            "N8N_SECURITY_AUDIT_UNVERIFIABLE",
            "The n8n security audit result could not be verified.",
            status_code=409,
        )
    if blocking_findings:
        raise N8nGovernanceError(
            "N8N_SECURITY_AUDIT_FINDINGS",
            "The n8n security audit reported findings that block publishing or execution.",
            status_code=409,
        )
    if official_seen:
        # Credential hygiene findings do not describe executable workflow
        # capability.  They remain represented by the digest without exposing
        # credential names or locations to the public audit trail.
        return _digest(report)

    recognized = False
    critical = False
    failed = False
    stack: list[Any] = [report]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            for key, value in current.items():
                normalized_key = re.sub(r"[^a-z0-9]+", "", str(key).casefold())
                if normalized_key in {
                    "risk", "riskreport", "severity", "level", "status", "success",
                    "ok", "verified", "findings", "categories", "sections",
                } or normalized_key.endswith("riskreport"):
                    recognized = True
                if normalized_key in {"success", "ok", "verified"} and value is False:
                    failed = True
                if normalized_key in {"error", "errors", "failed", "failure"} and value not in (None, False, "", 0, []):
                    failed = True
                if normalized_key in {"risk", "severity", "level", "status"}:
                    normalized_value = str(value).casefold()
                    if normalized_value in {"critical", "fatal", "blocked"}:
                        critical = True
                    if normalized_value in {"failed", "failure", "error", "unverifiable"}:
                        failed = True
                if isinstance(value, (Mapping, list)):
                    stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)
    if critical:
        raise N8nGovernanceError(
            "N8N_SECURITY_AUDIT_CRITICAL",
            "The n8n security audit reported a critical risk.",
            status_code=409,
        )
    if failed:
        raise N8nGovernanceError(
            "N8N_SECURITY_AUDIT_FAILED",
            "The n8n security audit failed.",
            status_code=409,
        )
    if not recognized:
        raise N8nGovernanceError(
            "N8N_SECURITY_AUDIT_UNVERIFIABLE",
            "The n8n security audit result could not be verified.",
            status_code=409,
        )
    return _digest(report)


def _risk(operation: str, payload: Mapping[str, Any], high_risk: bool) -> dict[str, Any]:
    external_write = operation in {"publish", "activate", "execute", "delete"} or operation.startswith("credential_")
    irreversible = operation in {"delete", "credential_delete"}
    return {
        "level": "critical" if high_risk or irreversible else "high" if external_write else "medium",
        "external_write": external_write,
        "irreversible": irreversible,
        "high_risk_nodes": high_risk,
        "credential_aliases": [str(item)[:128] for item in payload.get("credential_aliases", []) if isinstance(item, str)][:32],
        "warnings": [
            text for enabled, text in (
                (external_write, "This operation can change n8n or an external service."),
                (irreversible, "This operation may not be recoverable."),
                (high_risk, "Arbitrary code, host access, or unreviewed packages may be involved."),
                (True, "Email, webhooks, and imported content must be treated as untrusted data."),
            ) if enabled
        ],
    }


def _graph_result_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        value = value.to_dict()
    if not isinstance(value, Mapping):
        raise N8nGovernanceError(
            "N8N_GRAPH_AUTHORING_INVALID",
            "The graph authoring service returned an invalid result.",
            status_code=502,
        )
    result = json.loads(_canonical(value))
    if result.get("status") not in {"graph_ready", "needs_input", "blocked"}:
        raise N8nGovernanceError(
            "N8N_GRAPH_AUTHORING_INVALID",
            "The graph authoring service returned an invalid status.",
            status_code=502,
        )
    return result


class N8nApiBroker:
    """Narrow server-side n8n Public API client.  The API key is never returned."""

    def __init__(self, api_key_provider: Callable[[], str], *, base_url: str = N8N_BASE_URL) -> None:
        self._api_key_provider = api_key_provider
        self.base_url = base_url.rstrip("/")

    def _request(self, method: str, path: str, *, body: Any = None) -> Any:
        try:
            key = str(self._api_key_provider() or "").strip()
            if not key:
                raise N8nGovernanceError(
                    "N8N_API_KEY_NOT_CONFIGURED",
                    "The n8n API key is not configured.",
                    status_code=409,
                )
            response = requests.request(
                method, f"{self.base_url}{path}", json=body,
                headers={"X-N8N-API-KEY": key, "Accept": "application/json"}, timeout=15,
            )
        except N8nGovernanceError:
            raise
        except Exception as exc:
            raise N8nGovernanceError("N8N_BROKER_UNAVAILABLE", "The n8n broker is unavailable.", status_code=503) from exc
        if response.status_code >= 400:
            raise N8nGovernanceError("N8N_BROKER_REJECTED", "n8n rejected the governed operation.", status_code=409)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise N8nGovernanceError("N8N_BROKER_INVALID_RESPONSE", "n8n returned an invalid response.", status_code=502) from exc

    def list_workflows(self) -> list[dict[str, Any]]:
        raw = self._request("GET", "/api/v1/workflows?limit=100")
        values = raw.get("data", raw) if isinstance(raw, Mapping) else []
        result = []
        for item in values if isinstance(values, list) else []:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or "")
            result.append({
                "id": str(item.get("id") or ""), "name": name,
                "active": bool(item.get("active")), "updated_at": item.get("updatedAt"),
                "node_count": len(item.get("nodes") or []),
                "protected": _is_protected_workflow_name(name),
            })
        return result

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        """Fetch one workflow so protection checks cannot depend on pagination."""
        raw = self._request("GET", f"/api/v1/workflows/{workflow_id}")
        if not isinstance(raw, Mapping) or str(raw.get("id") or "") != workflow_id:
            raise N8nGovernanceError(
                "N8N_WORKFLOW_LOOKUP_FAILED",
                "The workflow identity could not be verified.",
                status_code=503,
            )
        name = str(raw.get("name") or "")
        facts = _workflow_facts(raw)
        return {
            "id": workflow_id,
            "name": name,
            "active": bool(raw.get("active")),
            "updated_at": raw.get("updatedAt"),
            "version_id": raw.get("versionId"),
            "active_version_id": raw.get("activeVersionId"),
            "node_count": len(raw.get("nodes") or []),
            "protected": _is_protected_workflow_name(name),
            "facts": facts,
            "workflow": _sanitize_proposed_workflow(raw),
            # The full workflow stays inside the Broker boundary; only its
            # digest and bounded review facts leave this method.
            "snapshot_digest": _digest({
                "id": workflow_id,
                "name": name,
                "active": bool(raw.get("active")),
                "updated_at": raw.get("updatedAt"),
                "workflow": raw,
            }),
        }

    def security_audit(self) -> Mapping[str, Any]:
        raw = self._request("POST", "/api/v1/audit", body={"additionalOptions": {"categories": ["credentials", "database", "filesystem", "nodes", "instance"]}})
        if isinstance(raw, list):
            # n8n returns [] when no findings are present.
            return {"status": "clean", "findings": raw, "verified": True}
        return raw if isinstance(raw, Mapping) else {}

    def editor_url(self, workflow_id: str) -> str:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise N8nGovernanceError(
                "N8N_EDITOR_URL_UNSAFE",
                "The n8n editor URL is not a verified loopback address.",
                status_code=503,
            )
        safe_id = str(workflow_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", safe_id):
            raise N8nGovernanceError("N8N_WORKFLOW_ID_INVALID", "The workflow identity is invalid.", status_code=502)
        return f"{self.base_url}/workflow/{safe_id}"

    def execute(self, operation: str, payload: Mapping[str, Any], *, secret: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]:
        workflow_id = str(payload.get("workflow_id") or "").strip()
        if operation == "create_draft":
            return self._request("POST", "/api/v1/workflows", body=payload.get("workflow"))
        if operation == "update_draft":
            return self._request("PUT", f"/api/v1/workflows/{workflow_id}", body=payload.get("workflow"))
        if operation in {"activate", "deactivate"}:
            return self._request("POST", f"/api/v1/workflows/{workflow_id}/{operation}")
        if operation == "delete":
            return self._request("DELETE", f"/api/v1/workflows/{workflow_id}")
        if operation == "publish":
            return self._request("POST", f"/api/v1/workflows/{workflow_id}/activate")
        if operation == "execute":
            raise N8nGovernanceError("N8N_EXECUTION_NOT_BOUND", "Generic execution requires a reviewed trigger binding.", status_code=409)
        if operation.startswith("credential_"):
            credential_id = str(payload.get("credential_id") or "").strip()
            body = {**dict(payload.get("credential") or {}), **dict(secret or {})}
            if operation == "credential_create":
                return self._request("POST", "/api/v1/credentials", body=body)
            if operation == "credential_update":
                return self._request("PATCH", f"/api/v1/credentials/{credential_id}", body=body)
            if operation == "credential_delete":
                return self._request("DELETE", f"/api/v1/credentials/{credential_id}")
            if operation == "credential_bind":
                raise N8nGovernanceError("N8N_CREDENTIAL_BIND_REQUIRES_WORKFLOW", "Credential binding must be part of a workflow update.", status_code=409)
        raise N8nGovernanceError("N8N_OPERATION_UNSUPPORTED", "The governed operation is unsupported.", status_code=422)


class N8nAgentGovernanceService:
    def __init__(
        self, *, broker: Any, cipher: AesGcmContentCipher,
        n8n_running: Callable[[], bool], high_risk_runner_ready: Callable[[], bool] = lambda: False,
        boot_id: Optional[str] = None, graph_authoring: Any = None,
        graph_binding_finalizer: Optional[Callable[[list[dict[str, Any]], Mapping[str, Any]], Any]] = None,
        graph_binding_activator: Optional[Callable[[Mapping[str, Any]], Any]] = None,
        policy_change_callback: Optional[Callable[[str, str], Any]] = None,
        workflow_change_callback: Optional[Callable[[Mapping[str, Any]], Any]] = None,
        integration_permission_check: Optional[Callable[..., Any]] = None,
        _allow_legacy_raw_workflows_for_tests: bool = False,
    ) -> None:
        self.broker = broker
        self.cipher = cipher
        self.n8n_running = n8n_running
        self.high_risk_runner_ready = high_risk_runner_ready
        self.boot_id = boot_id or secrets.token_hex(16)
        self.graph_authoring = graph_authoring
        self.graph_binding_finalizer = graph_binding_finalizer
        self.graph_binding_activator = graph_binding_activator
        self.policy_change_callback = policy_change_callback
        self.workflow_change_callback = workflow_change_callback
        self.integration_permission_check = integration_permission_check
        self._allow_legacy_raw_workflows_for_tests = bool(_allow_legacy_raw_workflows_for_tests)
        self._lock = threading.RLock()
        self._ensure_schema()

    def _integration_permission(
        self,
        project_id: str,
        capability: str,
        *,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        approval_satisfied: bool = False,
        approval_revision: Optional[int] = None,
        permit_review: bool = False,
    ) -> tuple[str, Optional[int]]:
        """Re-evaluate the unified Project grant at the operation boundary.

        The callback returns the Integration Center decision (or ``True`` for a
        narrowly scoped test/legacy adapter).  ``require_approval`` is accepted
        only after this service's immutable approval has been satisfied.  A
        missing callback is deliberately unavailable instead of silently
        falling back to the older n8n-only policy.
        """

        callback = self.integration_permission_check
        if not callable(callback):
            raise N8nGovernanceError(
                "N8N_INTEGRATION_POLICY_UNAVAILABLE",
                "The unified Project integration policy is unavailable.",
                status_code=503,
            )
        try:
            result = callback(
                project_id,
                capability,
                resource_type=resource_type,
                resource_id=resource_id,
            )
        except N8nGovernanceError:
            raise
        except Exception as exc:
            raise N8nGovernanceError(
                "N8N_INTEGRATION_POLICY_UNAVAILABLE",
                "The unified Project integration policy could not be verified.",
                status_code=503,
            ) from exc
        if result is True:
            decision = "allow"
            policy_revision = None
        elif isinstance(result, Mapping):
            decision = str(result.get("decision") or "deny").strip().casefold()
            try:
                policy_revision = int(result["policy_revision"])
            except (KeyError, TypeError, ValueError):
                policy_revision = None
        else:
            decision = "deny"
            policy_revision = None
        if decision == "allow":
            return decision, policy_revision
        if decision == "require_approval" and permit_review:
            if policy_revision is None:
                raise N8nGovernanceError(
                    "N8N_INTEGRATION_POLICY_UNAVAILABLE",
                    "The unified Project policy revision is unavailable.",
                    status_code=503,
                )
            return decision, policy_revision
        if decision == "require_approval" and approval_satisfied:
            if (
                policy_revision is None
                or approval_revision is None
                or policy_revision != int(approval_revision)
            ):
                raise N8nGovernanceError(
                    "N8N_INTEGRATION_APPROVAL_STALE",
                    "The Project integration policy changed; review this operation again.",
                    status_code=409,
                )
            return decision, policy_revision
        if decision == "require_approval":
            raise N8nGovernanceError(
                "N8N_INTEGRATION_APPROVAL_REQUIRED",
                "This n8n operation requires approval under the unified Project policy.",
                status_code=409,
            )
        raise N8nGovernanceError(
            "N8N_INTEGRATION_POLICY_DENIED",
            "The unified Project integration policy does not allow this n8n operation.",
            status_code=403,
        )

    def _ensure_schema(self) -> None:
        with database.get_db_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS n8n_agent_policies (
                    project_id TEXT PRIMARY KEY, mode TEXT NOT NULL DEFAULT 'restricted',
                    elevation_policy TEXT NOT NULL DEFAULT 'smart', elevation_session_id TEXT,
                    expires_at TEXT, last_activity_at TEXT NOT NULL, boot_id TEXT,
                    revision INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS n8n_agent_operations (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, session_id TEXT, run_id TEXT,
                    operation TEXT NOT NULL, workflow_id TEXT, workflow_name TEXT,
                    payload_json TEXT NOT NULL, diff_json TEXT NOT NULL, risk_json TEXT NOT NULL,
                    digest TEXT NOT NULL, base_digest TEXT, high_risk INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL, approval_stage INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                    result_json TEXT, error_code TEXT,
                    origin TEXT NOT NULL DEFAULT 'browser',
                    integration_policy_revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS n8n_agent_audits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL,
                    project_id TEXT NOT NULL, session_id TEXT, run_id TEXT, event_type TEXT NOT NULL,
                    actor TEXT NOT NULL, digest TEXT NOT NULL, public_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS n8n_agent_secret_handles (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, envelope TEXT NOT NULL,
                    created_at TEXT NOT NULL, expires_at TEXT NOT NULL, consumed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS n8n_agent_workflow_bindings (
                    workflow_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    workflow_name TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_n8n_agent_ops_project ON n8n_agent_operations(project_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_n8n_agent_audits_project ON n8n_agent_audits(project_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_n8n_agent_workflow_project ON n8n_agent_workflow_bindings(project_id, workflow_id);
            """)
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(n8n_agent_operations)").fetchall()
            }
            if "origin" not in columns:
                conn.execute(
                    "ALTER TABLE n8n_agent_operations ADD COLUMN origin TEXT NOT NULL DEFAULT 'browser'"
                )
            if "integration_policy_revision" not in columns:
                conn.execute(
                    "ALTER TABLE n8n_agent_operations ADD COLUMN integration_policy_revision INTEGER NOT NULL DEFAULT 0"
                )

    def _project(self, project_id: str) -> Mapping[str, Any]:
        project = database.get_project(str(project_id or "").strip())
        if not project or bool(project.get("archived")):
            raise N8nGovernanceError("PROJECT_NOT_FOUND", "Project was not found.", status_code=404)
        return project

    def _session(self, session_id: Optional[str], project_id: str) -> Optional[Mapping[str, Any]]:
        if not session_id:
            return None
        session = database.get_session(session_id)
        if not session or session.get("project_id") != project_id:
            raise N8nGovernanceError("SESSION_SCOPE_MISMATCH", "Session does not belong to this Project.", status_code=409)
        if bool(session.get("archived")):
            raise N8nGovernanceError(
                "SESSION_ARCHIVED",
                "The Session is archived and cannot authorize n8n operations.",
                status_code=409,
            )
        if str(session.get("mode") or "chat").casefold() == "email":
            raise N8nGovernanceError(
                "SESSION_SCOPE_MISMATCH",
                "Integration-only Sessions cannot authorize Agent n8n operations.",
                status_code=409,
            )
        return session

    def _audit(self, operation_id: str, project_id: str, event_type: str, digest: str, public: Mapping[str, Any], *, session_id: Optional[str] = None, run_id: Optional[str] = None, actor: str = "local_user") -> None:
        with database.get_db_conn() as conn:
            conn.execute(
                "INSERT INTO n8n_agent_audits(operation_id,project_id,session_id,run_id,event_type,actor,digest,public_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (operation_id, project_id, session_id, run_id, event_type, actor, digest, _canonical(public), _iso()),
            )

    def _revoke_pending(self, project_id: str, reason: str) -> None:
        now = _iso()
        with database.get_db_conn() as conn:
            rows = conn.execute("SELECT id,digest,session_id,run_id FROM n8n_agent_operations WHERE project_id=? AND status IN ('pending','pending_second_approval','security_review')", (project_id,)).fetchall()
            conn.execute("UPDATE n8n_agent_operations SET status='revoked',error_code=?,updated_at=? WHERE project_id=? AND status IN ('pending','pending_second_approval','security_review')", (reason, now, project_id))
        for row in rows:
            self._audit(row["id"], project_id, "revoked", row["digest"], {"reason": reason}, session_id=row["session_id"], run_id=row["run_id"], actor="system")

    def _notify_policy_change(self, project_id: str, reason: str) -> None:
        if not callable(self.policy_change_callback):
            return
        try:
            self.policy_change_callback(project_id, reason)
        except Exception as exc:
            raise N8nGovernanceError(
                "N8N_RUNTIME_POLICY_REVOCATION_FAILED",
                "The n8n runtime permissions could not be revoked safely.",
                status_code=503,
            ) from exc

    def set_policy(self, project_id: str, value: Mapping[str, Any]) -> dict[str, Any]:
        self._project(project_id)
        mode = str(value.get("mode") or "restricted")
        duration = str(value.get("elevation_policy") or "smart")
        session_id = str(value.get("session_id") or "").strip() or None
        if mode not in POLICY_MODES or duration not in ELEVATION_POLICIES:
            raise N8nGovernanceError("N8N_POLICY_INVALID", "The n8n Agent policy is invalid.", status_code=422)
        if mode == "full_audit" and value.get("explicit_ack") is not True:
            raise N8nGovernanceError("N8N_ELEVATION_ACK_REQUIRED", "Full audit mode requires explicit acknowledgement.", status_code=409)
        if mode == "full_audit" and duration == "session":
            if not session_id:
                raise N8nGovernanceError("N8N_SESSION_REQUIRED", "Session duration requires an active Session.", status_code=409)
            self._session(session_id, project_id)
        now = _now()
        expires = _iso(now + timedelta(hours=1)) if mode == "full_audit" and duration == "one_hour" else None
        boot = self.boot_id if mode == "full_audit" and duration == "smart" else None
        with database.get_db_conn() as conn:
            previous = conn.execute("SELECT mode FROM n8n_agent_policies WHERE project_id=?", (project_id,)).fetchone()
            conn.execute("""
                INSERT INTO n8n_agent_policies(project_id,mode,elevation_policy,elevation_session_id,expires_at,last_activity_at,boot_id,revision,updated_at)
                VALUES(?,?,?,?,?,?,?,1,?)
                ON CONFLICT(project_id) DO UPDATE SET mode=excluded.mode,elevation_policy=excluded.elevation_policy,
                elevation_session_id=excluded.elevation_session_id,expires_at=excluded.expires_at,last_activity_at=excluded.last_activity_at,
                boot_id=excluded.boot_id,revision=n8n_agent_policies.revision+1,updated_at=excluded.updated_at
            """, (project_id, mode, duration, session_id, expires, _iso(now), boot, _iso(now)))
        mode_rank = {"off": 0, "restricted": 1, "full_audit": 2}
        reason = "policy_changed"
        if previous and mode_rank[mode] < mode_rank.get(previous["mode"], 0):
            reason = "policy_downgraded"
            self._revoke_pending(project_id, reason)
        self._notify_policy_change(project_id, reason)
        return self.get_policy(project_id, session_id=session_id)

    def get_policy(self, project_id: str, *, session_id: Optional[str] = None) -> dict[str, Any]:
        self._project(project_id)
        with database.get_db_conn() as conn:
            row = conn.execute("SELECT * FROM n8n_agent_policies WHERE project_id=?", (project_id,)).fetchone()
            if row is None:
                now = _iso()
                conn.execute("INSERT INTO n8n_agent_policies(project_id,mode,elevation_policy,last_activity_at,revision,updated_at) VALUES(?,'restricted','smart',?,1,?)", (project_id, now, now))
                row = conn.execute("SELECT * FROM n8n_agent_policies WHERE project_id=?", (project_id,)).fetchone()
        value = dict(row)
        reason = None
        if value["mode"] == "full_audit":
            duration = value["elevation_policy"]
            if duration == "one_hour" and (_parse_time(value["expires_at"]) or _now()) <= _now(): reason = "elevation_expired"
            elif duration == "session" and value.get("elevation_session_id") != session_id: reason = "session_ended"
            elif duration == "smart":
                last = _parse_time(value.get("last_activity_at")) or _now()
                if value.get("boot_id") != self.boot_id: reason = "workbench_restarted"
                elif _now() - last >= timedelta(minutes=30): reason = "idle_timeout"
                elif not self.n8n_running(): reason = "n8n_stopped"
        if reason:
            with database.get_db_conn() as conn:
                conn.execute("UPDATE n8n_agent_policies SET mode='restricted',revision=revision+1,updated_at=? WHERE project_id=?", (_iso(), project_id))
            self._revoke_pending(project_id, reason)
            self._notify_policy_change(project_id, reason)
            return self.get_policy(project_id, session_id=session_id)
        try:
            runtime_ready = bool(self.n8n_running())
        except Exception:
            runtime_ready = False
        return {
            "project_id": project_id, "mode": value["mode"], "elevation_policy": value["elevation_policy"],
            "elevation_session_id": value.get("elevation_session_id"), "expires_at": value.get("expires_at"),
            "last_activity_at": value.get("last_activity_at"), "revision": value["revision"],
            "smart_idle_minutes": 30, "api_key_configured": self._api_key_configured(),
            "runtime_ready": runtime_ready,
        }

    def downgrade_smart_policies(self, reason: str = "n8n_stopped") -> int:
        """Immediately revoke smart elevation when the managed service stops."""
        with database.get_db_conn() as conn:
            rows = conn.execute(
                "SELECT project_id FROM n8n_agent_policies WHERE mode='full_audit' AND elevation_policy='smart'"
            ).fetchall()
            conn.execute(
                "UPDATE n8n_agent_policies SET mode='restricted',revision=revision+1,updated_at=? WHERE mode='full_audit' AND elevation_policy='smart'",
                (_iso(),),
            )
        for row in rows:
            self._revoke_pending(row["project_id"], reason)
            self._notify_policy_change(row["project_id"], reason)
        return len(rows)

    def _api_key_configured(self) -> bool:
        try:
            return bool(str(self.broker._api_key_provider() or "").strip())
        except Exception:
            return False

    def _require_broker_ready(self) -> None:
        try:
            running = bool(self.n8n_running())
        except Exception:
            running = False
        if not running:
            raise N8nGovernanceError(
                "N8N_RUNTIME_NOT_READY",
                "The managed n8n runtime is not ready.",
                status_code=503,
            )
        if not self._api_key_configured():
            raise N8nGovernanceError(
                "N8N_API_KEY_NOT_CONFIGURED",
                "The n8n API key is not configured.",
                status_code=409,
            )

    def _assert_workflow_not_protected(self, workflow_id: str) -> Optional[Mapping[str, Any]]:
        """Verify the exact target and fail closed when protection is unknown."""
        if not workflow_id:
            return None
        try:
            exact_lookup = getattr(self.broker, "get_workflow", None)
            if not callable(exact_lookup):
                raise RuntimeError("exact workflow lookup is unavailable")
            workflow = exact_lookup(workflow_id)
            if (
                not isinstance(workflow, Mapping)
                or str(workflow.get("id") or "") != workflow_id
            ):
                raise RuntimeError("workflow identity was not returned")
        except Exception as exc:
            raise N8nGovernanceError(
                "N8N_PROTECTED_WORKFLOW_LOOKUP_FAILED",
                "The workflow protection status could not be verified.",
                status_code=503,
            ) from exc
        if workflow.get("protected") is True or _is_protected_workflow_name(workflow.get("name")):
            raise N8nGovernanceError(
                "N8N_WORKFLOW_PROTECTED",
                "This Workbench workflow is protected.",
                status_code=403,
            )
        return workflow

    def _assert_workflow_project_scope(self, project_id: str, workflow_id: str) -> None:
        """Only workflows created through this Project's broker may be managed."""
        if not workflow_id:
            return
        with database.get_db_conn() as conn:
            row = conn.execute(
                "SELECT project_id FROM n8n_agent_workflow_bindings WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
        if not row:
            raise N8nGovernanceError(
                "N8N_WORKFLOW_NOT_MANAGED",
                "The workflow is not managed by this Project.",
                status_code=403,
            )
        if row["project_id"] != project_id:
            # Do not disclose which other Project owns the workflow.
            raise N8nGovernanceError(
                "N8N_WORKFLOW_SCOPE_MISMATCH",
                "The workflow is not available in this Project.",
                status_code=404,
            )

    def _load_target_snapshot(
        self,
        project_id: str,
        operation: str,
        payload: Mapping[str, Any],
    ) -> tuple[Optional[Mapping[str, Any]], str]:
        workflow_id = str(payload.get("workflow_id") or "").strip()
        if operation == "create_draft":
            if workflow_id:
                raise N8nGovernanceError(
                    "N8N_CREATE_TARGET_FORBIDDEN",
                    "A new workflow proposal cannot select an existing workflow.",
                    status_code=422,
                )
            return None, _digest({"target": "new-workflow"})
        if not workflow_id:
            raise N8nGovernanceError(
                "N8N_WORKFLOW_ID_REQUIRED",
                "An exact workflow id is required.",
                status_code=422,
            )
        self._integration_permission(
            project_id,
            "workflow.read",
            resource_type="workflow",
            resource_id=workflow_id,
        )
        snapshot = self._assert_workflow_not_protected(workflow_id)
        self._assert_workflow_project_scope(project_id, workflow_id)
        if operation == "update_draft" and bool(snapshot and snapshot.get("active")):
            raise N8nGovernanceError(
                "N8N_ACTIVE_WORKFLOW_UPDATE_FORBIDDEN",
                "Deactivate the workflow before changing its draft.",
                status_code=409,
            )
        return snapshot, _workflow_snapshot_digest(snapshot or {})

    def _assert_target_fresh(
        self,
        current: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> Optional[Mapping[str, Any]]:
        snapshot, observed_digest = self._load_target_snapshot(
            str(current["project_id"]), str(current["operation"]), payload,
        )
        expected = str(current.get("base_digest") or "")
        if not WORKFLOW_DIGEST_RE.fullmatch(expected) or not secrets.compare_digest(
            expected, observed_digest
        ):
            raise N8nGovernanceError(
                "N8N_WORKFLOW_STALE",
                "The target workflow changed after the proposal was created.",
                status_code=409,
            )
        graph = current.get("diff", {}).get("graph") if isinstance(current.get("diff"), Mapping) else None
        if isinstance(graph, Mapping):
            if self.graph_authoring is None:
                raise N8nGovernanceError("N8N_GRAPH_AUTHORING_UNAVAILABLE", "Graph validation is unavailable.", status_code=503)
            expected_catalog = str(graph.get("catalog_digest") or "")
            if not WORKFLOW_DIGEST_RE.fullmatch(expected_catalog) or not secrets.compare_digest(
                expected_catalog, self._catalog_digest()
            ):
                raise N8nGovernanceError("N8N_NODE_CATALOG_STALE", "The node catalog changed after approval.", status_code=409)
            if str(current.get("operation")) in {"create_draft", "update_draft"}:
                workflow = payload.get("workflow")
                expected_graph = str(graph.get("after_digest") or "")
                observed_graph = (
                    str(self._authoring().workflow_digest(workflow)) if isinstance(workflow, Mapping) else ""
                )
                if not WORKFLOW_DIGEST_RE.fullmatch(expected_graph) or not secrets.compare_digest(
                    expected_graph, observed_graph
                ):
                    raise N8nGovernanceError("N8N_GRAPH_STALE", "The compiled graph changed after approval.", status_code=409)
        return snapshot

    def _assert_operation_eligible(
        self,
        current: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        project_id = str(current["project_id"])
        self._project(project_id)
        self._session(current.get("session_id"), project_id)
        policy = self.get_policy(project_id, session_id=current.get("session_id"))
        if policy["mode"] == "off":
            raise N8nGovernanceError(
                "N8N_AGENT_DISABLED",
                "n8n Agent access is disabled.",
                status_code=403,
            )
        if policy["mode"] == "restricted" and bool(current.get("high_risk")):
            raise N8nGovernanceError(
                "N8N_HIGH_RISK_FORBIDDEN",
                "High-risk operations are forbidden in restricted mode.",
                status_code=403,
            )
        if str(current.get("operation") or "").startswith("credential_"):
            raise N8nGovernanceError(
                "N8N_CREDENTIAL_GOVERNANCE_UNAVAILABLE",
                "Credential operations remain disabled until Project-scoped credential ownership is available.",
                status_code=403,
            )
        return policy

    def _bind_created_workflow(
        self,
        project_id: str,
        result: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> None:
        workflow_id = str(result.get("id") or "").strip()
        if not workflow_id:
            raise N8nGovernanceError(
                "N8N_BROKER_INVALID_RESPONSE",
                "n8n did not return the created workflow identity.",
                status_code=502,
            )
        workflow = payload.get("workflow") if isinstance(payload.get("workflow"), Mapping) else {}
        workflow_name = str(result.get("name") or workflow.get("name") or "")[:255]
        now = _iso()
        with database.get_db_conn() as conn:
            existing = conn.execute(
                "SELECT project_id FROM n8n_agent_workflow_bindings WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            if existing and existing["project_id"] != project_id:
                raise N8nGovernanceError(
                    "N8N_WORKFLOW_SCOPE_CONFLICT",
                    "The created workflow identity is already bound to another Project.",
                    status_code=409,
                )
            conn.execute(
                """
                INSERT INTO n8n_agent_workflow_bindings(
                    workflow_id,project_id,workflow_name,created_at,updated_at
                ) VALUES(?,?,?,?,?)
                ON CONFLICT(workflow_id) DO UPDATE SET
                    workflow_name=excluded.workflow_name,updated_at=excluded.updated_at
                """,
                (workflow_id, project_id, workflow_name, now, now),
            )

    def stage_secret(self, project_id: str, secret_value: Mapping[str, Any]) -> dict[str, Any]:
        self._project(project_id)
        if not isinstance(secret_value, Mapping) or not secret_value:
            raise N8nGovernanceError("N8N_SECRET_INVALID", "Credential fields are required.", status_code=422)
        encoded = _canonical(dict(secret_value))
        if len(encoded) > 64_000:
            raise N8nGovernanceError("N8N_SECRET_TOO_LARGE", "Credential fields are too large.", status_code=413)
        handle = f"n8ns_{uuid.uuid4().hex}"
        aad = f"n8n-agent-secret:{project_id}:{handle}"
        envelope = self.cipher.encrypt_text(encoded, aad=aad)
        expires = _now() + timedelta(minutes=15)
        with database.get_db_conn() as conn:
            conn.execute("INSERT INTO n8n_agent_secret_handles(id,project_id,envelope,created_at,expires_at) VALUES(?,?,?,?,?)", (handle, project_id, envelope, _iso(), _iso(expires)))
        return {"secret_handle": handle, "expires_at": _iso(expires)}

    def _consume_secret(self, project_id: str, handle: Optional[str]) -> Optional[Mapping[str, Any]]:
        if not handle:
            return None
        with database.get_db_conn() as conn:
            row = conn.execute("SELECT * FROM n8n_agent_secret_handles WHERE id=? AND project_id=?", (handle, project_id)).fetchone()
            if not row or row["consumed_at"] or (_parse_time(row["expires_at"]) or _now()) <= _now():
                raise N8nGovernanceError("N8N_SECRET_HANDLE_INVALID", "The credential secret handle is invalid or expired.", status_code=409)
            conn.execute("UPDATE n8n_agent_secret_handles SET consumed_at=? WHERE id=? AND consumed_at IS NULL", (_iso(), handle))
        aad = f"n8n-agent-secret:{project_id}:{handle}"
        return _loads(self.cipher.decrypt_text(row["envelope"], aad=aad), {})

    def _authoring(self) -> Any:
        if self.graph_authoring is None:
            raise N8nGovernanceError(
                "N8N_GRAPH_AUTHORING_UNAVAILABLE",
                "The n8n graph authoring service is unavailable.",
                status_code=503,
            )
        return self.graph_authoring

    def _catalog_digest(self) -> str:
        authoring = self._authoring()
        digest = str(getattr(getattr(authoring, "catalog", None), "digest", "") or "")
        if not WORKFLOW_DIGEST_RE.fullmatch(digest):
            raise N8nGovernanceError(
                "N8N_NODE_CATALOG_INVALID",
                "The pinned n8n node catalog could not be verified.",
                status_code=503,
            )
        return digest

    def search_node_catalog(
        self, project_id: str, *, session_id: Optional[str] = None,
        query: str = "", limit: int = 50,
    ) -> dict[str, Any]:
        self._project(project_id)
        self._session(session_id, project_id)
        policy = self.get_policy(project_id, session_id=session_id)
        if policy["mode"] == "off":
            raise N8nGovernanceError("N8N_AGENT_DISABLED", "n8n Agent access is disabled.", status_code=403)
        authoring = self._authoring()
        catalog = getattr(authoring, "catalog", None)
        if catalog is None or not callable(getattr(catalog, "search", None)):
            raise N8nGovernanceError("N8N_NODE_CATALOG_INVALID", "The node catalog is unavailable.", status_code=503)
        return {
            "catalog_digest": self._catalog_digest(),
            "nodes": catalog.search(str(query or "")[:255], limit=max(1, min(int(limit), 100))),
        }

    def materialize_planned_choice(
        self, *, project_id: str, session_id: str, operation: str,
        semantic: Mapping[str, Any], source: str = "planner",
    ) -> dict[str, Any]:
        """Turn semantic intent into a server-owned executable graph snapshot."""

        self._project(project_id)
        self._session(session_id, project_id)
        policy = self.get_policy(project_id, session_id=session_id)
        if policy["mode"] == "off":
            raise N8nGovernanceError("N8N_AGENT_DISABLED", "n8n Agent access is disabled.", status_code=403)
        workflow_id = str(semantic.get("workflow_id") or "").strip()
        # Materialization may inspect an existing workflow, but it does not
        # itself perform the mutation.  A restricted write grant may therefore
        # proceed to the immutable review step; execution still rechecks after
        # the user approval.
        self._integration_permission(
            project_id,
            "workflow.execute",
            resource_type="workflow",
            resource_id=workflow_id or _NEW_WORKFLOW_RESOURCE,
            permit_review=True,
        )
        self._require_broker_ready()
        authoring = self._authoring()
        semantic = _safe_json(semantic)
        workflow_id = str(semantic.get("workflow_id") or "").strip()
        try:
            if operation == "create_draft":
                if set(semantic) != {"workflow_spec"} or not isinstance(semantic.get("workflow_spec"), Mapping):
                    raise N8nGovernanceError("N8N_WORKFLOW_SPEC_REQUIRED", "A semantic workflow spec is required.", status_code=422)
                result = _graph_result_dict(authoring.materialize(
                    semantic["workflow_spec"],
                    context={
                        "project_id": project_id, "session_id": session_id,
                        "operation": operation, "source": str(source or "planner")[:32],
                    },
                ))
                base_digest = _digest({"target": "new-workflow"})
                operation_payload: dict[str, Any] = {}
            elif operation == "update_draft":
                if not workflow_id or not isinstance(semantic.get("workflow_patch"), list):
                    raise N8nGovernanceError("N8N_WORKFLOW_PATCH_REQUIRED", "A semantic workflow patch is required.", status_code=422)
                snapshot, base_digest = self._load_target_snapshot(project_id, operation, semantic)
                base_workflow = (snapshot or {}).get("workflow")
                if not isinstance(base_workflow, Mapping):
                    raise N8nGovernanceError("N8N_WORKFLOW_LOOKUP_FAILED", "The workflow graph could not be loaded.", status_code=503)
                result = _graph_result_dict(authoring.apply_patch(
                    base_workflow,
                    semantic["workflow_patch"],
                    context={
                        "project_id": project_id, "session_id": session_id,
                        "operation": operation, "source": str(source or "planner")[:32],
                    },
                ))
                operation_payload = {
                    "workflow_id": workflow_id,
                    "workflow_name": str((snapshot or {}).get("name") or semantic.get("workflow_name") or "")[:255],
                }
            elif operation in {"publish", "activate", "deactivate", "delete"}:
                snapshot, base_digest = self._load_target_snapshot(project_id, operation, semantic)
                base_workflow = (snapshot or {}).get("workflow")
                if not isinstance(base_workflow, Mapping):
                    raise N8nGovernanceError("N8N_WORKFLOW_LOOKUP_FAILED", "The workflow graph could not be loaded.", status_code=503)
                if operation in {"publish", "activate"}:
                    # n8n's editor can change a managed draft after Workbench
                    # created it.  Publishing or activating therefore reviews
                    # the exact current graph through the same fail-closed
                    # catalog, external-action and approval-dominance gates as
                    # a newly compiled graph.  A snapshot digest alone proves
                    # freshness; it does not prove that the graph is safe.
                    result = _graph_result_dict(authoring.adopt(base_workflow))
                    # Adoption inspects already-persisted protected nodes.  It
                    # must not manufacture provisional binding claims for a
                    # state-only operation.
                    result["binding_claims"] = []
                else:
                    # Deactivation and deletion cannot begin workflow
                    # execution, so an invalid existing graph must not prevent
                    # the user from safely stopping or removing it.
                    result = {
                        "status": "graph_ready",
                        "workflow": base_workflow,
                        "graph_preview": authoring.preview(base_workflow),
                        "validation_status": "ready",
                        "catalog_digest": self._catalog_digest(),
                        "graph_digest": authoring.workflow_digest(base_workflow),
                        "issues": [], "questions": [], "diff": {},
                    }
                operation_payload = {
                    "workflow_id": workflow_id,
                    "workflow_name": str((snapshot or {}).get("name") or semantic.get("workflow_name") or "")[:255],
                }
            else:
                raise N8nGovernanceError("N8N_OPERATION_INVALID", "The n8n operation is invalid.", status_code=422)
        except N8nGovernanceError:
            raise
        except Exception as exc:
            code = str(getattr(exc, "code", "") or "N8N_GRAPH_AUTHORING_FAILED")
            message = str(getattr(exc, "message", "") or "The semantic workflow could not be materialized.")
            raise N8nGovernanceError(code, message, status_code=422) from exc

        result["catalog_digest"] = str(result.get("catalog_digest") or self._catalog_digest())
        result["base_digest"] = base_digest
        if operation_payload:
            result["operation_payload"] = operation_payload
        if result.get("status") == "graph_ready":
            workflow = result.get("workflow")
            if not isinstance(workflow, Mapping):
                raise N8nGovernanceError("N8N_GRAPH_AUTHORING_INVALID", "Compiled workflow is missing.", status_code=502)
            if _is_protected_workflow_name(workflow.get("name")):
                raise N8nGovernanceError("N8N_WORKFLOW_PROTECTED", "This Workbench workflow is protected.", status_code=403)
            result["graph_digest"] = str(result.get("graph_digest") or authoring.workflow_digest(workflow))
        # Bounded catalog context supports at most two model repair attempts.
        if result.get("status") == "blocked":
            try:
                terms: list[str] = []
                spec = semantic.get("workflow_spec") if isinstance(semantic.get("workflow_spec"), Mapping) else {}
                nodes = spec.get("nodes") if isinstance(spec.get("nodes"), list) else []
                for node in nodes:
                    if not isinstance(node, Mapping):
                        continue
                    node_type = str(node.get("type") or "").strip().rsplit(".", 1)[-1]
                    if node_type and node_type.casefold() not in {item.casefold() for item in terms}:
                        terms.append(node_type[:64])
                for issue in result.get("issues") or []:
                    if not isinstance(issue, Mapping):
                        continue
                    details = issue.get("details") if isinstance(issue.get("details"), Mapping) else {}
                    for candidate in (details.get("type"), details.get("node_type"), issue.get("node")):
                        term = str(candidate or "").strip().rsplit(".", 1)[-1]
                        if term and term.casefold() not in {item.casefold() for item in terms}:
                            terms.append(term[:64])
                entries: list[dict[str, Any]] = []
                seen: set[str] = set()
                for term in terms[:8]:
                    for entry in getattr(authoring, "catalog").search(term, limit=10):
                        node_type = str(entry.get("type") or "") if isinstance(entry, Mapping) else ""
                        if node_type and node_type not in seen:
                            seen.add(node_type); entries.append(entry)
                            if len(entries) >= 40:
                                break
                    if len(entries) >= 40:
                        break
                result["catalog_entries"] = entries
            except Exception:
                result["catalog_entries"] = []
        return result

    def preview_adoption(
        self, project_id: str, workflow_id: str, *, session_id: Optional[str] = None,
    ) -> dict[str, Any]:
        self._project(project_id); self._session(session_id, project_id)
        if self.get_policy(project_id, session_id=session_id)["mode"] == "off":
            raise N8nGovernanceError("N8N_AGENT_DISABLED", "n8n Agent access is disabled.", status_code=403)
        self._integration_permission(
            project_id,
            "workflow.read",
            resource_type="workflow",
            resource_id=str(workflow_id or "").strip() or None,
        )
        self._require_broker_ready()
        snapshot = self._assert_workflow_not_protected(str(workflow_id or "").strip())
        with database.get_db_conn() as conn:
            owner = conn.execute(
                "SELECT project_id FROM n8n_agent_workflow_bindings WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
        if owner:
            raise N8nGovernanceError("N8N_WORKFLOW_ALREADY_MANAGED", "The workflow is already managed.", status_code=409)
        authoring = self._authoring()
        workflow = (snapshot or {}).get("workflow")
        if not isinstance(workflow, Mapping):
            raise N8nGovernanceError("N8N_WORKFLOW_LOOKUP_FAILED", "The workflow graph could not be loaded.", status_code=503)
        result = _graph_result_dict(authoring.adopt(workflow))
        return {
            "workflow_id": workflow_id, "workflow_name": snapshot.get("name"),
            "active": snapshot.get("active") is True,
            "status": result.get("status"),
            "expected_digest": _workflow_snapshot_digest(snapshot),
            "graph_preview": result.get("graph_preview"),
            "validation_status": result.get("validation_status"),
            "catalog_digest": result.get("catalog_digest"),
            "graph_digest": result.get("graph_digest"),
            "issues": result.get("issues") or [], "questions": result.get("questions") or [],
        }

    def adopt_workflow(
        self, project_id: str, workflow_id: str, *, session_id: Optional[str],
        expected_digest: str, confirmation: str,
    ) -> dict[str, Any]:
        preview = self.preview_adoption(project_id, workflow_id, session_id=session_id)
        if not secrets.compare_digest(str(preview["expected_digest"]), str(expected_digest or "")):
            raise N8nGovernanceError("N8N_WORKFLOW_STALE", "The workflow changed before adoption.", status_code=409)
        if not secrets.compare_digest(str(preview["workflow_name"]), str(confirmation or "")):
            raise N8nGovernanceError("N8N_ADOPTION_CONFIRMATION_REQUIRED", "Type the exact workflow name to adopt it.", status_code=409)
        if preview.get("status") != "graph_ready":
            raise N8nGovernanceError("N8N_WORKFLOW_ADOPTION_BLOCKED", "The workflow did not pass graph validation.", status_code=409)
        now = _iso()
        with database.get_db_conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO n8n_agent_workflow_bindings(workflow_id,project_id,workflow_name,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (workflow_id, project_id, preview["workflow_name"], now, now),
                )
            except Exception as exc:
                raise N8nGovernanceError("N8N_WORKFLOW_ALREADY_MANAGED", "The workflow is already managed.", status_code=409) from exc
        adoption_id = f"n8nadopt_{uuid.uuid4().hex}"
        self._audit(adoption_id, project_id, "workflow_adopted", expected_digest, {
            "workflow_id": workflow_id, "workflow_name": preview["workflow_name"],
            "catalog_digest": preview.get("catalog_digest"), "graph_digest": preview.get("graph_digest"),
        }, session_id=session_id)
        return {**preview, "managed": True}

    def list_workflows(self, project_id: str, *, session_id: Optional[str] = None) -> dict[str, Any]:
        self._project(project_id)
        self._session(session_id, project_id)
        policy = self.get_policy(project_id, session_id=session_id)
        if policy["mode"] == "off":
            raise N8nGovernanceError("N8N_AGENT_DISABLED", "n8n Agent access is disabled.", status_code=403)
        self._integration_permission(project_id, "workflow.read")
        self._require_broker_ready()
        workflows = self.broker.list_workflows()
        with database.get_db_conn() as conn:
            managed_ids = {
                row["workflow_id"] for row in conn.execute(
                    "SELECT workflow_id FROM n8n_agent_workflow_bindings WHERE project_id=?",
                    (project_id,),
                ).fetchall()
            }
        visible: list[Mapping[str, Any]] = []
        for item in workflows:
            if not isinstance(item, Mapping):
                continue
            workflow_id = str(item.get("id") or "").strip()
            if workflow_id not in managed_ids:
                continue
            try:
                self._integration_permission(
                    project_id,
                    "workflow.read",
                    resource_type="workflow",
                    resource_id=workflow_id,
                )
            except N8nGovernanceError as exc:
                if exc.code == "N8N_INTEGRATION_POLICY_DENIED":
                    continue
                raise
            visible.append(item)
        return {
            "project_id": project_id,
            "workflows": visible,
        }

    def create_operation(self, value: Mapping[str, Any]) -> dict[str, Any]:
        """Create a browser-origin request, preserving restricted safe-draft behavior."""
        return self._create_operation(value, force_approval=False, origin="browser")

    def create_planned_operation(self, value: Mapping[str, Any]) -> dict[str, Any]:
        """Server-only planner bridge: every proposal requires human approval.

        This method is deliberately separate from the Browser API method.  An
        untrusted payload cannot opt into or forge planner origin.
        """
        return self._create_operation(value, force_approval=True, origin="planner")

    def _create_operation(
        self,
        value: Mapping[str, Any],
        *,
        force_approval: bool,
        origin: str,
    ) -> dict[str, Any]:
        project_id = str(value.get("project_id") or "").strip()
        session_id = str(value.get("session_id") or "").strip() or None
        run_id = str(value.get("run_id") or "").strip() or None
        self._project(project_id); self._session(session_id, project_id)
        policy = self.get_policy(project_id, session_id=session_id)
        if policy["mode"] == "off":
            raise N8nGovernanceError("N8N_AGENT_DISABLED", "n8n Agent access is disabled.", status_code=403)
        self._require_broker_ready()
        operation = str(value.get("operation") or "")
        if operation not in OPERATIONS:
            raise N8nGovernanceError("N8N_OPERATION_INVALID", "The n8n operation is invalid.", status_code=422)
        if operation.startswith("credential_"):
            raise N8nGovernanceError(
                "N8N_CREDENTIAL_GOVERNANCE_UNAVAILABLE",
                "Credential operations remain disabled until Project-scoped credential ownership is available.",
                status_code=403,
            )
        payload = _safe_json(value.get("payload") or {})
        proposed_workflow_id = str(payload.get("workflow_id") or "").strip()
        integration_decision, integration_policy_revision = self._integration_permission(
            project_id,
            "workflow.execute",
            resource_type="workflow",
            resource_id=proposed_workflow_id or _NEW_WORKFLOW_RESOURCE,
            # This step only records a bounded proposal.  A central
            # require-approval decision is carried forward by forcing the
            # operation out of the historical safe-draft direct path.
            permit_review=True,
        )
        integration_policy_revision = int(integration_policy_revision or 0)
        force_approval = force_approval or integration_decision == "require_approval"
        materialization: Optional[dict[str, Any]] = None
        if self.graph_authoring is not None:
            if origin == "planner":
                materialization = _safe_json(value.get("materialization") or {})
            else:
                # Browser callers use the same semantic compiler. Supplying a
                # raw `workflow` object is not a privileged bypass.
                materialization = self.materialize_planned_choice(
                    project_id=project_id, session_id=session_id or "",
                    operation=operation, semantic=payload, source=origin,
                )
            if materialization.get("status") != "graph_ready":
                code = "N8N_GRAPH_NEEDS_INPUT" if materialization.get("status") == "needs_input" else "N8N_GRAPH_BLOCKED"
                raise N8nGovernanceError(
                    code,
                    "The semantic workflow is not ready for review.",
                    status_code=409,
                )
            operation_payload = materialization.get("operation_payload")
            payload = _safe_json(operation_payload if isinstance(operation_payload, Mapping) else {})
            if operation in {"create_draft", "update_draft"}:
                if not isinstance(materialization.get("workflow"), Mapping):
                    raise N8nGovernanceError("N8N_GRAPH_AUTHORING_INVALID", "Compiled workflow is missing.", status_code=502)
                payload["workflow"] = _sanitize_proposed_workflow(materialization["workflow"])
            supplied_catalog = str(materialization.get("catalog_digest") or "")
            if not WORKFLOW_DIGEST_RE.fullmatch(supplied_catalog) or not secrets.compare_digest(
                supplied_catalog, self._catalog_digest()
            ):
                raise N8nGovernanceError("N8N_NODE_CATALOG_STALE", "The pinned node catalog changed; materialize again.", status_code=409)
            supplied_graph = str(materialization.get("graph_digest") or "")
            materialized_workflow = materialization.get("workflow")
            observed_graph = str(self._authoring().workflow_digest(materialized_workflow)) if isinstance(materialized_workflow, Mapping) else ""
            if (
                not WORKFLOW_DIGEST_RE.fullmatch(supplied_graph)
                or not WORKFLOW_DIGEST_RE.fullmatch(observed_graph)
                or not secrets.compare_digest(supplied_graph, observed_graph)
            ):
                raise N8nGovernanceError("N8N_GRAPH_STALE", "The compiled graph changed; materialize again.", status_code=409)
            binding_claims = materialization.get("binding_claims") or []
            if not isinstance(binding_claims, list) or any(not isinstance(item, Mapping) for item in binding_claims):
                raise N8nGovernanceError("N8N_GRAPH_BINDING_INVALID", "Graph binding claims are invalid.", status_code=502)
            if binding_claims and not callable(self.graph_binding_finalizer):
                raise N8nGovernanceError(
                    "N8N_GRAPH_BINDING_FINALIZER_UNAVAILABLE",
                    "The protected Agent binding service is unavailable.",
                    status_code=503,
                )
            if binding_claims:
                payload["_binding_claims"] = _safe_json(binding_claims, limit=64_000)
        elif operation in {"create_draft", "update_draft"} and not self._allow_legacy_raw_workflows_for_tests:
            # Failing to initialize the pinned catalog must disable graph
            # mutation, not silently re-enable the old raw JSON path.
            raise N8nGovernanceError(
                "N8N_GRAPH_AUTHORING_UNAVAILABLE",
                "The n8n graph authoring service is unavailable.",
                status_code=503,
            )
        asserted_name = str(payload.get("workflow_name") or "")[:255]
        if _is_protected_workflow_name(asserted_name):
            raise N8nGovernanceError(
                "N8N_WORKFLOW_PROTECTED",
                "This Workbench workflow is protected.",
                status_code=403,
            )
        if operation in {"create_draft", "update_draft"}:
            payload["workflow"] = _sanitize_proposed_workflow(payload.get("workflow"))
        workflow = payload.get("workflow") if isinstance(payload.get("workflow"), Mapping) else {}
        workflow_name = str(payload.get("workflow_name") or workflow.get("name") or "")[:255]
        if _is_protected_workflow_name(workflow_name):
            raise N8nGovernanceError("N8N_WORKFLOW_PROTECTED", "This Workbench workflow is protected.", status_code=403)
        workflow_id = str(payload.get("workflow_id") or "").strip()
        before_snapshot, base_digest = self._load_target_snapshot(project_id, operation, payload)
        if materialization is not None:
            supplied_base = str(materialization.get("base_digest") or "")
            if not WORKFLOW_DIGEST_RE.fullmatch(supplied_base) or not secrets.compare_digest(supplied_base, base_digest):
                raise N8nGovernanceError("N8N_WORKFLOW_STALE", "The target workflow changed after materialization.", status_code=409)
        # Ignore the caller/model supplied diff.  Review facts are computed
        # exclusively from the sanitized proposal and exact server snapshot.
        diff = _canonical_workflow_diff(operation, payload, before_snapshot)
        if materialization is not None:
            diff["graph"] = {
                "catalog_digest": materialization["catalog_digest"],
                "base_digest": base_digest,
                "after_digest": materialization["graph_digest"],
                "preview": materialization.get("graph_preview"),
                "validation_status": materialization.get("validation_status"),
            }
        after_facts = diff.get("after") if isinstance(diff.get("after"), Mapping) else {}
        payload["credential_aliases"] = list(after_facts.get("credential_aliases") or [])
        payload["external_targets"] = list(after_facts.get("external_targets") or [])
        high_risk = _is_high_risk(payload) or _node_types_are_high_risk(
            _node_types_from_snapshot(before_snapshot)
        )
        if policy["mode"] == "restricted" and high_risk:
            raise N8nGovernanceError("N8N_HIGH_RISK_FORBIDDEN", "High-risk nodes are forbidden in restricted mode.", status_code=403)
        risk = _risk(operation, payload, high_risk)
        operation_id = f"n8nop_{uuid.uuid4().hex}"
        immutable = {
            "project_id": project_id, "session_id": session_id, "run_id": run_id,
            "operation": operation, "payload": payload, "diff": diff, "risk": risk,
            "origin": origin, "base_digest": base_digest,
            "integration_policy_revision": integration_policy_revision,
            "catalog_digest": (materialization or {}).get("catalog_digest"),
            "after_graph_digest": (materialization or {}).get("graph_digest"),
            "plan_digest": value.get("plan_digest") if origin == "planner" else None,
        }
        digest = _digest(immutable)
        direct = (
            self.graph_authoring is None and not force_approval and policy["mode"] == "restricted"
            and operation in SAFE_DRAFT_OPERATIONS and not high_risk
        )
        status = "approved" if direct else "pending"
        now = _now()
        with database.get_db_conn() as conn:
            conn.execute("""
                INSERT INTO n8n_agent_operations(id,project_id,session_id,run_id,operation,workflow_id,workflow_name,payload_json,diff_json,risk_json,digest,base_digest,high_risk,status,approval_stage,created_at,updated_at,expires_at,origin,integration_policy_revision)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (operation_id, project_id, session_id, run_id, operation, workflow_id or None, workflow_name, _canonical(payload), _canonical(diff), _canonical(risk), digest, base_digest, int(high_risk), status, 0, _iso(now), _iso(now), _iso(now + timedelta(minutes=10)), origin, integration_policy_revision))
            conn.execute("UPDATE n8n_agent_policies SET last_activity_at=?,updated_at=? WHERE project_id=?", (_iso(now), _iso(now), project_id))
        self._audit(operation_id, project_id, "created", digest, {"operation": operation, "risk": risk, "automatic": direct, "origin": origin}, session_id=session_id, run_id=run_id, actor="agent")
        if direct:
            return self._execute(operation_id)
        return self.get_operation(operation_id, project_id=project_id)

    def get_operation(self, operation_id: str, *, project_id: Optional[str] = None) -> dict[str, Any]:
        with database.get_db_conn() as conn:
            row = conn.execute("SELECT * FROM n8n_agent_operations WHERE id=?", (operation_id,)).fetchone()
        if not row or (project_id is not None and row["project_id"] != project_id):
            raise N8nGovernanceError("N8N_OPERATION_NOT_FOUND", "The n8n operation was not found.", status_code=404)
        diff = _loads(row["diff_json"], {})
        graph = diff.get("graph") if isinstance(diff, Mapping) and isinstance(diff.get("graph"), Mapping) else {}
        return {
            "id": row["id"], "project_id": row["project_id"], "session_id": row["session_id"], "run_id": row["run_id"],
            "operation": row["operation"], "workflow_id": row["workflow_id"], "workflow_name": row["workflow_name"],
            "diff": diff, "risk": _loads(row["risk_json"], {}), "digest": row["digest"],
            "graph_preview": graph.get("preview"), "validation_status": graph.get("validation_status"),
            "catalog_digest": graph.get("catalog_digest"), "graph_digest": graph.get("after_digest"),
            "base_digest": row["base_digest"], "high_risk": bool(row["high_risk"]), "status": row["status"],
            "approval_stage": row["approval_stage"], "created_at": row["created_at"], "updated_at": row["updated_at"],
            "expires_at": row["expires_at"], "result": _loads(row["result_json"], None), "error_code": row["error_code"],
            "origin": row["origin"], "integration_policy_revision": int(row["integration_policy_revision"] or 0),
        }

    def list_operations(self, project_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        self._project(project_id)
        with database.get_db_conn() as conn:
            rows = conn.execute("SELECT id FROM n8n_agent_operations WHERE project_id=? ORDER BY created_at DESC LIMIT ?", (project_id, max(1, min(limit, 250)))).fetchall()
        return [self.get_operation(row["id"], project_id=project_id) for row in rows]

    def decide(
        self, operation_id: str, *, project_id: str, session_id: Any = _UNSET,
        expected_digest: str, approved: bool, confirmation: Optional[str] = None,
    ) -> dict[str, Any]:
        current = self.get_operation(operation_id, project_id=project_id)
        if session_id is not _UNSET and not secrets.compare_digest(
            str(current.get("session_id") or ""), str(session_id or "")
        ):
            raise N8nGovernanceError(
                "N8N_APPROVAL_SCOPE_MISMATCH",
                "The approval Session does not match the operation.",
                status_code=409,
            )
        if current["status"] not in {"pending", "pending_second_approval"}:
            raise N8nGovernanceError("N8N_APPROVAL_CONFLICT", "The operation is no longer awaiting approval.", status_code=409)
        if not secrets.compare_digest(current["digest"], str(expected_digest or "")):
            raise N8nGovernanceError("N8N_OPERATION_STALE", "The operation snapshot changed; approval was rejected.", status_code=409)
        if (_parse_time(current["expires_at"]) or _now()) <= _now():
            with database.get_db_conn() as conn:
                conn.execute("UPDATE n8n_agent_operations SET status='expired',updated_at=? WHERE id=?", (_iso(), operation_id))
            raise N8nGovernanceError("N8N_APPROVAL_EXPIRED", "The operation approval expired.", status_code=409)
        self._assert_operation_eligible(current)
        if approved:
            self._integration_permission(
                project_id,
                "workflow.execute",
                resource_type="workflow",
                resource_id=str(current.get("workflow_id") or "") or _NEW_WORKFLOW_RESOURCE,
                approval_satisfied=True,
                approval_revision=int(current.get("integration_policy_revision") or 0),
            )
        if current["risk"].get("irreversible") and approved and confirmation != (current["workflow_name"] or current["workflow_id"]):
            raise N8nGovernanceError("N8N_DESTRUCTIVE_CONFIRMATION_REQUIRED", "Type the exact workflow or credential name to confirm deletion.", status_code=409)
        if not approved:
            with database.get_db_conn() as conn:
                conn.execute("UPDATE n8n_agent_operations SET status='rejected',updated_at=? WHERE id=?", (_iso(), operation_id))
            self._audit(operation_id, project_id, "rejected", current["digest"], {"operation": current["operation"]})
            return self.get_operation(operation_id, project_id=project_id)
        # Target, policy and scope may have changed after the immutable
        # proposal was created.  Re-evaluate immediately before approval.
        with database.get_db_conn() as conn:
            payload_row = conn.execute(
                "SELECT payload_json FROM n8n_agent_operations WHERE id=?", (operation_id,)
            ).fetchone()
        payload = _loads(payload_row["payload_json"], {}) if payload_row else {}
        self._assert_target_fresh(current, payload)
        self._assert_operation_eligible(current)
        current = self.get_operation(operation_id, project_id=project_id)
        if current["status"] not in {"pending", "pending_second_approval"}:
            raise N8nGovernanceError("N8N_APPROVAL_CONFLICT", "The operation is no longer awaiting approval.", status_code=409)
        self._require_broker_ready()
        if current["high_risk"] and current["operation"] in {"publish", "activate", "execute"} and current["approval_stage"] == 0:
            if not self.high_risk_runner_ready():
                raise N8nGovernanceError("N8N_HIGH_RISK_RUNNER_UNAVAILABLE", "The isolated high-risk runner is not ready.", status_code=409)
            try:
                report = self.broker.security_audit()
            except Exception:
                raise N8nGovernanceError("N8N_SECURITY_AUDIT_FAILED", "The n8n security audit could not be verified.", status_code=409)
            report_digest = _security_audit_digest(report)
            with database.get_db_conn() as conn:
                conn.execute("UPDATE n8n_agent_operations SET status='pending_second_approval',approval_stage=1,result_json=?,updated_at=? WHERE id=?", (_canonical({"security_audit": {"verified": True, "digest": report_digest}}), _iso(), operation_id))
            self._audit(operation_id, project_id, "security_review_completed", current["digest"], {"verified": True, "report_digest": report_digest})
            return self.get_operation(operation_id, project_id=project_id)
        if current["operation"] in {"publish", "activate", "execute"} and current["approval_stage"] == 0:
            try:
                report = self.broker.security_audit()
            except Exception:
                raise N8nGovernanceError("N8N_SECURITY_AUDIT_FAILED", "The n8n security audit could not be verified.", status_code=409)
            report_digest = _security_audit_digest(report)
            self._audit(operation_id, project_id, "security_review_completed", current["digest"], {"verified": True, "report_digest": report_digest})
        with database.get_db_conn() as conn:
            approved_claim = conn.execute(
                """UPDATE n8n_agent_operations SET status='approved',updated_at=?
                   WHERE id=? AND project_id=? AND digest=? AND status=?""",
                (_iso(), operation_id, project_id, current["digest"], current["status"]),
            )
            if approved_claim.rowcount != 1:
                raise N8nGovernanceError(
                    "N8N_APPROVAL_CONFLICT",
                    "The operation changed before approval could be recorded.",
                    status_code=409,
                )
        self._audit(
            operation_id, project_id, "approved", current["digest"],
            {"operation": current["operation"], "approval_stage": current["approval_stage"]},
            session_id=current.get("session_id"), run_id=current.get("run_id"), actor="local_user",
        )
        return self._execute(operation_id, approval_satisfied=True)

    def _execute(self, operation_id: str, *, approval_satisfied: bool = False) -> dict[str, Any]:
        current = self.get_operation(operation_id)
        self._assert_operation_eligible(current)
        self._integration_permission(
            str(current["project_id"]),
            "workflow.execute",
            resource_type="workflow",
            resource_id=str(current.get("workflow_id") or "") or _NEW_WORKFLOW_RESOURCE,
            approval_satisfied=approval_satisfied,
            approval_revision=int(current.get("integration_policy_revision") or 0),
        )
        self._require_broker_ready()
        with database.get_db_conn() as conn:
            row = conn.execute("SELECT payload_json FROM n8n_agent_operations WHERE id=?", (operation_id,)).fetchone()
            if not row:
                raise N8nGovernanceError(
                    "N8N_OPERATION_NOT_FOUND",
                    "The n8n operation was not found.",
                    status_code=404,
                )
        payload = _loads(row["payload_json"], {})
        workflow_id = str(payload.get("workflow_id") or "").strip()
        # The target may have been renamed or rebound since proposal time.  The
        # exact identity and Project scope are therefore verified once more as
        # close as possible to the irreversible broker call.
        self._assert_target_fresh(current, payload)
        with database.get_db_conn() as conn:
            claimed = conn.execute(
                """
                UPDATE n8n_agent_operations
                   SET status='executing',updated_at=?
                 WHERE id=?
                   AND status='approved'
                """,
                (_iso(), operation_id),
            )
            if claimed.rowcount != 1:
                raise N8nGovernanceError(
                    "N8N_EXECUTION_ALREADY_CLAIMED",
                    "The operation was already claimed or is no longer executable.",
                    status_code=409,
                )
        secret_value = None
        broker_started = False
        public_result: dict[str, Any] = {}
        try:
            if current["operation"].startswith("credential_"):
                secret_value = self._consume_secret(current["project_id"], payload.get("secret_handle"))
            # The Project policy may change after the durable claim.  Recheck
            # immediately before the broker can mutate n8n or an external
            # service.  Existing approval never carries authorization across a
            # policy revision.
            self._integration_permission(
                str(current["project_id"]),
                "workflow.execute",
                resource_type="workflow",
                resource_id=workflow_id or _NEW_WORKFLOW_RESOURCE,
                approval_satisfied=approval_satisfied,
                approval_revision=int(current.get("integration_policy_revision") or 0),
            )
            broker_started = True
            result = self.broker.execute(current["operation"], payload, secret=secret_value)
            public_result = {
                key: value
                for key, value in dict(result or {}).items()
                if key in {"id", "name", "active", "createdAt", "updatedAt"}
            }
            graph_review = current.get("diff", {}).get("graph") if isinstance(current.get("diff"), Mapping) else None
            if self.graph_authoring is not None and current["operation"] in {"create_draft", "update_draft"}:
                exact_id = str((result or {}).get("id") or workflow_id).strip()
                if not exact_id:
                    raise N8nGovernanceError("N8N_BROKER_INVALID_RESPONSE", "n8n did not return the workflow identity.", status_code=502)
                exact = self.broker.get_workflow(exact_id)
                observed_workflow = exact.get("workflow") if isinstance(exact, Mapping) else None
                expected_graph = str((graph_review or {}).get("after_digest") or "")
                observed_graph = (
                    str(self._authoring().workflow_digest(observed_workflow))
                    if isinstance(observed_workflow, Mapping) else ""
                )
                if (
                    not WORKFLOW_DIGEST_RE.fullmatch(expected_graph)
                    or not WORKFLOW_DIGEST_RE.fullmatch(observed_graph)
                    or not secrets.compare_digest(expected_graph, observed_graph)
                ):
                    raise N8nGovernanceError(
                        "N8N_GRAPH_RECONCILIATION_FAILED",
                        "n8n did not persist the exact reviewed graph.",
                        status_code=409,
                    )
                if current["operation"] == "create_draft" and exact.get("active") is True:
                    raise N8nGovernanceError(
                        "N8N_DRAFT_ACTIVATION_CONFLICT",
                        "The created workflow was unexpectedly active.",
                        status_code=409,
                    )
                public_result["graph_digest"] = observed_graph
                editor_url = getattr(self.broker, "editor_url", None)
                if callable(editor_url):
                    public_result["editor_url"] = editor_url(exact_id)
                if current["operation"] == "create_draft":
                    # The bridge finalizer verifies workflow ownership, so the
                    # exact reconciled draft must be Project-bound first.
                    self._bind_created_workflow(current["project_id"], result or {}, payload)
                binding_claims = payload.get("_binding_claims") or []
                if binding_claims:
                    if not callable(self.graph_binding_finalizer):
                        raise N8nGovernanceError(
                            "N8N_GRAPH_BINDING_FINALIZER_UNAVAILABLE",
                            "The protected Agent binding service is unavailable.",
                            status_code=503,
                        )
                    finalized = self.graph_binding_finalizer(
                        json.loads(_canonical(binding_claims)),
                        {
                            "project_id": current["project_id"], "session_id": current.get("session_id"),
                            "run_id": current.get("run_id"), "workflow_id": exact_id,
                            "workflow_revision": exact.get("version_id") or exact.get("updated_at"),
                            "graph_digest": observed_graph,
                        },
                    )
                    if not isinstance(finalized, list):
                        raise N8nGovernanceError(
                            "N8N_GRAPH_BINDING_FINALIZATION_FAILED",
                            "The protected Agent bindings were not finalized.",
                            status_code=409,
                        )
                    public_result["binding_count"] = len(finalized)
            if current["operation"] == "create_draft" and self.graph_authoring is None:
                self._bind_created_workflow(current["project_id"], result or {}, payload)
            if (
                self.graph_authoring is not None
                and current["operation"] in {"activate", "publish"}
                and callable(self.graph_binding_activator)
            ):
                exact = self.broker.get_workflow(workflow_id)
                workflow_revision = str(
                    exact.get("active_version_id")
                    or exact.get("version_id")
                    or ""
                ).strip()
                if exact.get("active") is not True or not workflow_revision:
                    raise N8nGovernanceError(
                        "N8N_WORKFLOW_ACTIVATION_RECONCILIATION_FAILED",
                        "n8n did not expose the exact active workflow revision.",
                        status_code=409,
                    )
                activated = self.graph_binding_activator(
                    {
                        "project_id": current["project_id"],
                        "session_id": current.get("session_id"),
                        "run_id": current.get("run_id"),
                        "workflow_id": workflow_id,
                        "workflow_revision": workflow_revision,
                        "workflow": exact.get("workflow"),
                    }
                )
                if not isinstance(activated, list):
                    raise N8nGovernanceError(
                        "N8N_AGENT_BINDING_ACTIVATION_FAILED",
                        "The protected Agent bindings could not be activated.",
                        status_code=409,
                    )
                public_result["binding_count"] = len(activated)
            mutated_workflow_id = (
                str((result or {}).get("id") or "").strip()
                if current["operation"] == "create_draft"
                else workflow_id
            )
            if mutated_workflow_id and callable(self.workflow_change_callback):
                self.workflow_change_callback(
                    {
                        "project_id": current["project_id"],
                        "session_id": current.get("session_id"),
                        "run_id": current.get("run_id"),
                        "workflow_id": mutated_workflow_id,
                        "operation": current["operation"],
                    }
                )
            with database.get_db_conn() as conn:
                conn.execute("UPDATE n8n_agent_operations SET status='completed',result_json=?,updated_at=? WHERE id=?", (_canonical(public_result), _iso(), operation_id))
                if current["operation"] == "delete" and workflow_id:
                    conn.execute(
                        "DELETE FROM n8n_agent_workflow_bindings WHERE workflow_id=? AND project_id=?",
                        (workflow_id, current["project_id"]),
                    )
            self._audit(operation_id, current["project_id"], "completed", current["digest"], {"operation": current["operation"], "result": public_result}, session_id=current["session_id"], run_id=current["run_id"], actor="broker")
        except Exception as exc:
            # Once the remote call starts, a timeout, invalid response or local
            # reconciliation failure cannot prove that n8n did not apply the
            # mutation.  Mark it unknown and forbid blind retry/approval.
            # Only bounded result metadata is retained for manual reconciliation.
            if broker_started:
                safe_result = {
                    "reconciliation_required": True,
                    "remote_result": public_result or None,
                }
                with database.get_db_conn() as conn:
                    conn.execute(
                        "UPDATE n8n_agent_operations SET status='execution_unknown',error_code=?,result_json=?,updated_at=? WHERE id=? AND status='executing'",
                        ("N8N_EXECUTION_OUTCOME_UNKNOWN", _canonical(safe_result), _iso(), operation_id),
                    )
                self._audit(
                    operation_id, current["project_id"], "execution_unknown", current["digest"],
                    {"operation": current["operation"], "error_code": "N8N_EXECUTION_OUTCOME_UNKNOWN"},
                    session_id=current["session_id"], run_id=current["run_id"], actor="broker",
                )
                raise N8nGovernanceError(
                    "N8N_EXECUTION_OUTCOME_UNKNOWN",
                    "n8n may have applied the operation. Verify n8n before creating a new proposal.",
                    status_code=409,
                ) from exc
            error_code = exc.code if isinstance(exc, N8nGovernanceError) else "N8N_EXECUTION_FAILED"
            with database.get_db_conn() as conn:
                conn.execute("UPDATE n8n_agent_operations SET status='failed',error_code=?,updated_at=? WHERE id=?", (error_code, _iso(), operation_id))
            self._audit(operation_id, current["project_id"], "failed", current["digest"], {"operation": current["operation"], "error_code": error_code}, actor="broker")
            if isinstance(exc, N8nGovernanceError):
                raise
            raise N8nGovernanceError(error_code, "The governed n8n operation failed before execution.", status_code=500) from exc
        return self.get_operation(operation_id)

    def list_audits(self, project_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        self._project(project_id)
        with database.get_db_conn() as conn:
            rows = conn.execute("SELECT * FROM n8n_agent_audits WHERE project_id=? ORDER BY id DESC LIMIT ?", (project_id, max(1, min(limit, 250)))).fetchall()
        return [{"id": row["id"], "operation_id": row["operation_id"], "project_id": row["project_id"], "session_id": row["session_id"], "run_id": row["run_id"], "event_type": row["event_type"], "actor": row["actor"], "digest": row["digest"], "details": _loads(row["public_json"], {}), "created_at": row["created_at"]} for row in rows]


__all__ = ["N8nAgentGovernanceService", "N8nApiBroker", "N8nGovernanceError"]
