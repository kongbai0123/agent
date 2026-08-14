from __future__ import annotations

"""Deterministic, tool-free n8n workflow graph authoring primitives.

The module intentionally has no dependency on the Workbench database, HTTP app,
or n8n process lifecycle.  Callers inject credential aliases and protected
sub-workflow identifiers.  The large n8n metadata files stay in the pinned local
runtime and are fingerprinted when the catalog is loaded.
"""

import copy
import hashlib
import json
import re
import secrets
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


PINNED_N8N_VERSION = "2.32.5"
PINNED_NODES_BASE_VERSION = "2.32.3"
DEFAULT_N8N_RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "runtime" / "tools" / "n8n"
SPEC_SCHEMA = "workflow_spec.v1"
PATCH_SCHEMA = "workflow_patch.v1"
MAX_NODES = 50
MAX_EDGES = 100
MAX_WORKFLOW_BYTES = 250_000

_UUID_NAMESPACE = uuid.UUID("ed8ff3ad-4d5e-5ab5-b89c-dffb395fe84f")
_BUILTIN_PACKAGES = (
    ("n8n-nodes-base", "n8n-nodes-base"),
    ("@n8n/n8n-nodes-langchain", "@n8n/n8n-nodes-langchain"),
)
_RESERVED_TYPES = {"workbench.agent", "workbench.approval"}
_REVISION_TOKEN_RE = re.compile(r"^wbr_[A-Za-z0-9_-]{16,96}$")
_BINDING_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_BANNED_EXPRESSION_PATTERNS = (
    ("EXPRESSION_ENV_ACCESS", re.compile(r"\$env\b|process\s*\.\s*env", re.I)),
    ("EXPRESSION_CREDENTIAL_ACCESS", re.compile(r"\$credentials?\b", re.I)),
    ("EXPRESSION_CODE_EXECUTION", re.compile(r"\b(?:eval|Function|require|import)\s*\(", re.I)),
    ("EXPRESSION_PROTOTYPE_ACCESS", re.compile(r"(?:__proto__|prototype|constructor)\b", re.I)),
    ("EXPRESSION_FILE_ACCESS", re.compile(r"(?:file:\/\/|[a-z]:[\\/]|\/etc\/|\/proc\/)", re.I)),
)

# External service operations are classified conservatively.  A read is only
# considered safe when its operation is one of the reviewed, side-effect-free
# verbs below.  Everything credential-backed that cannot be classified is
# blocked until a node-specific action-manifest adapter is added.
_EXPLICIT_READ_ONLY_OPERATIONS = {
    "download",
    "get",
    "getall",
    "getmany",
    "getpermalink",
    "getusers",
    "history",
    "list",
    "lookup",
    "lookupbyemail",
    "member",
    "read",
    "replies",
    "retrieve",
    "search",
    "select",
}
_OBVIOUS_WRITE_OPERATIONS = {
    "add",
    "append",
    "appendorupdate",
    "archive",
    "clear",
    "close",
    "create",
    "createdraft",
    "delete",
    "deletescheduled",
    "disable",
    "enable",
    "insert",
    "invite",
    "join",
    "kick",
    "leave",
    "move",
    "post",
    "publish",
    "remove",
    "rename",
    "reply",
    "schedule",
    "send",
    "sendandwait",
    "setpurpose",
    "settopic",
    "update",
    "updateusers",
    "upload",
    "upsert",
    "write",
}
_UNADAPTED_EXTERNAL_WRITE_TYPES = {
    "googlesheets",
    "microsoftoutlook",
    "microsoftsql",
    "mongodb",
    "mysql",
    "postgres",
    "redis",
    "slack",
}


class GraphAuthoringError(ValueError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": copy.deepcopy(self.details)}


@dataclass(frozen=True)
class GraphIssue:
    code: str
    message: str
    severity: str = "blocked"
    node: str | None = None
    path: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }
        if self.node is not None:
            value["node"] = self.node
        if self.path is not None:
            value["path"] = self.path
        if self.details:
            value["details"] = copy.deepcopy(dict(self.details))
        return value


@dataclass
class ValidationResult:
    status: str
    issues: list[GraphIssue] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    @property
    def questions(self) -> list[dict[str, Any]]:
        return [issue.to_dict() for issue in self.issues if issue.severity == "needs_input"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "issues": [issue.to_dict() for issue in self.issues],
            "questions": self.questions,
        }


@dataclass
class GraphResult:
    status: str
    workflow: dict[str, Any] | None
    graph_preview: dict[str, Any]
    validation_status: str
    catalog_digest: str
    graph_digest: str | None
    issues: list[GraphIssue] = field(default_factory=list)
    diff: dict[str, Any] = field(default_factory=dict)
    binding_claims: list[dict[str, Any]] = field(default_factory=list)

    @property
    def questions(self) -> list[dict[str, Any]]:
        return [issue.to_dict() for issue in self.issues if issue.severity == "needs_input"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "workflow": copy.deepcopy(self.workflow),
            "graph_preview": copy.deepcopy(self.graph_preview),
            "validation_status": self.validation_status,
            "catalog_digest": self.catalog_digest,
            "graph_digest": self.graph_digest,
            "issues": [issue.to_dict() for issue in self.issues],
            "questions": self.questions,
            "diff": copy.deepcopy(self.diff),
            "binding_claims": copy.deepcopy(self.binding_claims),
        }


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GraphAuthoringError(
            "N8N_CATALOG_READ_FAILED",
            f"Unable to read pinned n8n metadata: {path}",
            details={"path": str(path), "reason": type(exc).__name__},
        ) from exc


def _version_values(value: Any) -> tuple[float, ...]:
    raw = value if isinstance(value, list) else [value]
    values: list[float] = []
    for item in raw:
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            values.append(float(item))
    return tuple(sorted(set(values)))


def _display_matches(rule: Any, actual: Any, version: float) -> bool:
    if not isinstance(rule, list):
        rule = [rule]
    for expected in rule:
        if isinstance(expected, Mapping) and "_cnd" in expected:
            condition = expected.get("_cnd")
            if not isinstance(condition, Mapping):
                # Unknown conditional metadata is treated as visible by the
                # caller so required inputs fail closed instead of vanishing.
                return True
            for operator, operand in condition.items():
                candidate = version if actual is None and operator in {"gte", "gt", "lte", "lt"} else actual
                if operator == "not":
                    if candidate != operand:
                        return True
                    continue
                if operator == "exists":
                    exists = candidate is not None
                    if exists is bool(operand):
                        return True
                    continue
                if operator in {"includes", "contains"}:
                    try:
                        if operand in candidate:
                            return True
                    except (TypeError, ValueError):
                        pass
                    continue
                if operator in {"notIncludes", "notContains"}:
                    try:
                        if operand not in candidate:
                            return True
                    except (TypeError, ValueError):
                        return True
                    continue
                if operator == "between" and isinstance(operand, (list, tuple)) and len(operand) == 2:
                    try:
                        if float(operand[0]) <= float(candidate) <= float(operand[1]):
                            return True
                    except (TypeError, ValueError):
                        pass
                    continue
                if operator == "regex":
                    try:
                        if re.search(str(operand), str(candidate or "")):
                            return True
                    except re.error:
                        return True
                    continue
                try:
                    number = float(operand)
                except (TypeError, ValueError):
                    # Fail closed for generated conditional operators this
                    # reviewed adapter does not yet understand.
                    return True
                candidate = version if actual is None else actual
                try:
                    candidate_number = float(candidate)
                except (TypeError, ValueError):
                    continue
                if operator == "gte" and candidate_number >= number:
                    return True
                if operator == "gt" and candidate_number > number:
                    return True
                if operator == "lte" and candidate_number <= number:
                    return True
                if operator == "lt" and candidate_number < number:
                    return True
                if operator in {"eq", "equals"} and candidate_number == number:
                    return True
                if operator not in {"gte", "gt", "lte", "lt", "eq", "equals"}:
                    return True
            continue
        candidates = actual if isinstance(actual, list) else [actual]
        if expected in candidates:
            return True
    return False


def _property_is_visible(prop: Mapping[str, Any], parameters: Mapping[str, Any], version: float) -> bool:
    display = prop.get("displayOptions")
    if not isinstance(display, Mapping):
        return True
    show = display.get("show")
    if isinstance(show, Mapping):
        for key, allowed in show.items():
            actual = version if key == "@version" else parameters.get(key)
            if not _display_matches(allowed, actual, version):
                return False
    hide = display.get("hide")
    if isinstance(hide, Mapping):
        for key, denied in hide.items():
            actual = version if key == "@version" else parameters.get(key)
            if _display_matches(denied, actual, version):
                return False
    return True


class NodeCatalog:
    """Sanitized catalog loaded only from explicitly trusted built-in packages."""

    def __init__(self, entries: Iterable[Mapping[str, Any]], *, fingerprint: Mapping[str, Any]):
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for raw in entries:
            if not isinstance(raw, Mapping):
                continue
            package = str(raw.get("_package") or "n8n-nodes-base")
            if package not in {item[0] for item in _BUILTIN_PACKAGES}:
                continue
            short_name = str(raw.get("name") or "").strip()
            if short_name.startswith(f"{package}."):
                short_name = short_name[len(package) + 1 :]
            if not short_name or not re.fullmatch(r"[A-Za-z0-9_.-]+", short_name):
                continue
            full_type = f"{package}.{short_name}"
            item = copy.deepcopy(dict(raw))
            item["_package"] = package
            item["_full_type"] = full_type
            grouped[full_type].append(item)
        self._entries = dict(grouped)
        self.fingerprint = copy.deepcopy(dict(fingerprint))
        digest_basis = {
            "fingerprint": self.fingerprint,
            "nodes": [
                {
                    "type": type_name,
                    "versions": sorted({version for row in rows for version in _version_values(row.get("version"))}),
                    "properties_digest": _sha256_bytes(_canonical_json([row.get("properties", []) for row in rows]).encode("utf-8")),
                }
                for type_name, rows in sorted(self._entries.items())
            ],
        }
        self.digest = _sha256_bytes(_canonical_json(digest_basis).encode("utf-8"))

    @classmethod
    def from_runtime(
        cls,
        runtime_root: str | Path,
        *,
        types_root: str | Path | None = None,
        expected_n8n_version: str = PINNED_N8N_VERSION,
        expected_nodes_base_version: str = PINNED_NODES_BASE_VERSION,
    ) -> "NodeCatalog":
        root = Path(runtime_root).resolve()
        n8n_package_path = root / "node_modules" / "n8n" / "package.json"
        base_package_path = root / "node_modules" / "n8n-nodes-base" / "package.json"
        lock_path = root / "package-lock.json"
        n8n_package = _read_json(n8n_package_path)
        base_package = _read_json(base_package_path)
        actual_n8n = str(n8n_package.get("version") or "") if isinstance(n8n_package, Mapping) else ""
        actual_base = str(base_package.get("version") or "") if isinstance(base_package, Mapping) else ""
        if actual_n8n != expected_n8n_version or actual_base != expected_nodes_base_version:
            raise GraphAuthoringError(
                "N8N_CATALOG_VERSION_MISMATCH",
                "Installed n8n metadata does not match the reviewed pinned versions.",
                details={
                    "expected_n8n": expected_n8n_version,
                    "actual_n8n": actual_n8n,
                    "expected_nodes_base": expected_nodes_base_version,
                    "actual_nodes_base": actual_base,
                },
            )
        try:
            lock_digest = _sha256_bytes(lock_path.read_bytes())
        except OSError as exc:
            raise GraphAuthoringError(
                "N8N_CATALOG_READ_FAILED", "Unable to fingerprint package-lock.json.", details={"path": str(lock_path)}
            ) from exc

        entries: list[dict[str, Any]] = []
        metadata_digests: dict[str, str] = {}
        loaded_packages: dict[str, str] = {"n8n": actual_n8n, "n8n-nodes-base": actual_base}
        for package, relative in _BUILTIN_PACKAGES:
            package_root = root / "node_modules" / Path(relative)
            if package_root.exists():
                package_json = _read_json(package_root / "package.json")
                loaded_packages[package] = str(package_json.get("version") or "") if isinstance(package_json, Mapping) else ""
        inferred_types_root = root.parent.parent / "n8n-data" / ".cache" / "n8n" / "public" / "types"
        type_directory = Path(types_root).resolve() if types_root is not None else inferred_types_root.resolve()
        nodes_path = type_directory / "nodes.json"
        node_versions_path = type_directory / "node-versions.json"
        credentials_path = type_directory / "credentials.json"
        raw_nodes = _read_json(nodes_path)
        raw_versions = _read_json(node_versions_path)
        raw_credentials = _read_json(credentials_path)
        if not isinstance(raw_nodes, list) or not isinstance(raw_versions, list) or not isinstance(raw_credentials, list):
            raise GraphAuthoringError("N8N_CATALOG_INVALID", "Pinned n8n generated type metadata has an unexpected shape.")
        allowed_prefixes = tuple(f"{package}." for package, _ in _BUILTIN_PACKAGES)
        reviewed_versions = {
            str(value)
            for value in raw_versions
            if isinstance(value, str) and value.startswith(allowed_prefixes)
        }
        credential_names = {
            str(item.get("name"))
            for item in raw_credentials
            if isinstance(item, Mapping) and item.get("name")
        }
        for raw in raw_nodes:
            if not isinstance(raw, Mapping):
                continue
            full_name = str(raw.get("name") or "")
            package = next((candidate for candidate, _ in _BUILTIN_PACKAGES if full_name.startswith(f"{candidate}.")), None)
            if package is None:
                continue
            short_name = full_name[len(package) + 1 :]
            versions = _version_values(raw.get("version"))
            if any(f"{package}.{short_name}@{int(version) if version.is_integer() else version}" not in reviewed_versions for version in versions):
                raise GraphAuthoringError(
                    "N8N_CATALOG_VERSION_INDEX_MISMATCH",
                    "Generated node metadata and node-versions index do not match.",
                    details={"type": full_name},
                )
            item = copy.deepcopy(dict(raw))
            item["name"] = short_name
            item["_package"] = package
            item["_known_credentials"] = sorted(credential_names)
            entries.append(item)
        for label, path in (
            ("nodes", nodes_path),
            ("node_versions", node_versions_path),
            ("credentials", credentials_path),
        ):
            metadata_digests[label] = _sha256_bytes(path.read_bytes())
        return cls(
            entries,
            fingerprint={
                "n8n_version": actual_n8n,
                "n8n_nodes_base_version": actual_base,
                "packages": loaded_packages,
                "package_lock_sha256": lock_digest,
                "metadata_sha256": metadata_digests,
                "generated_types_root": str(type_directory),
                "reviewed_node_version_count": len(reviewed_versions),
            },
        )

    @classmethod
    def from_entries(
        cls,
        entries: Iterable[Mapping[str, Any]],
        *,
        fingerprint: Mapping[str, Any] | None = None,
    ) -> "NodeCatalog":
        return cls(entries, fingerprint=fingerprint or {"fixture": True})

    def __len__(self) -> int:
        return len(self._entries)

    def _resolve_type(self, type_name: str) -> str | None:
        candidate = str(type_name or "").strip()
        if candidate in self._entries:
            return candidate
        if "." not in candidate:
            preferred = f"n8n-nodes-base.{candidate}"
            if preferred in self._entries:
                return preferred
            matches = [key for key in self._entries if key.rsplit(".", 1)[-1] == candidate]
            if len(matches) == 1:
                return matches[0]
        return None

    def get(self, type_name: str, version: float | None = None) -> dict[str, Any] | None:
        full_type = self._resolve_type(type_name)
        if full_type is None:
            return None
        rows = self._entries[full_type]
        if version is None:
            row = max(rows, key=lambda item: max(_version_values(item.get("version")) or (0.0,)))
            available = _version_values(row.get("version")) or (0.0,)
            try:
                declared_default = float(row.get("defaultVersion"))
            except (TypeError, ValueError):
                declared_default = None
            selected_version = declared_default if declared_default in available else max(available)
        else:
            row = next((item for item in rows if float(version) in _version_values(item.get("version"))), None)
            if row is None:
                return None
            selected_version = float(version)
        result = copy.deepcopy(row)
        result["type"] = full_type
        result["selected_version"] = selected_version
        result["supported_versions"] = sorted(
            {item for candidate in rows for item in _version_values(candidate.get("version"))}
        )
        return result

    def search(self, query: str = "", *, limit: int = 50) -> list[dict[str, Any]]:
        query_value = str(query or "").strip().casefold()
        bounded_limit = min(max(int(limit), 1), 100)
        matches: list[tuple[int, str, dict[str, Any]]] = []
        for full_type in self._entries:
            item = self.get(full_type)
            if item is None:
                continue
            display_name = str(item.get("displayName") or item.get("name") or full_type)
            haystack = " ".join(
                [display_name, str(item.get("name") or ""), full_type, str(item.get("description") or ""), " ".join(item.get("group") or [])]
            ).casefold()
            if query_value and query_value not in haystack:
                continue
            score = 0 if not query_value else (0 if display_name.casefold().startswith(query_value) else 1)
            matches.append(
                (
                    score,
                    display_name.casefold(),
                    {
                        "type": full_type,
                        "name": str(item.get("name") or ""),
                        "display_name": display_name,
                        "description": str(item.get("description") or "")[:500],
                        "group": [str(value) for value in item.get("group") or []],
                        "versions": item["supported_versions"],
                        "default_version": item["selected_version"],
                        "dynamic_inputs": isinstance(item.get("inputs"), str),
                        "dynamic_outputs": isinstance(item.get("outputs"), str),
                        "credential_types": sorted(
                            {
                                str(value.get("name"))
                                for value in item.get("credentials") or []
                                if isinstance(value, Mapping) and value.get("name")
                            }
                        ),
                    },
                )
            )
        matches.sort(key=lambda value: (value[0], value[1], value[2]["type"]))
        return [value[2] for value in matches[:bounded_limit]]


class LazyNodeCatalog:
    """Lazy, fail-closed catalog holder suitable for application startup.

    Constructing this object never touches the runtime. ``status()`` converts
    catalog load errors into a sanitized readiness response; ``require()`` is
    reserved for graph-authoring requests and raises the original typed error.
    """

    def __init__(
        self,
        runtime_root: str | Path = DEFAULT_N8N_RUNTIME_ROOT,
        *,
        types_root: str | Path | None = None,
        expected_n8n_version: str = PINNED_N8N_VERSION,
        expected_nodes_base_version: str = PINNED_NODES_BASE_VERSION,
    ):
        self.runtime_root = Path(runtime_root)
        self.types_root = Path(types_root) if types_root is not None else None
        self.expected_n8n_version = expected_n8n_version
        self.expected_nodes_base_version = expected_nodes_base_version
        self._catalog: NodeCatalog | None = None
        self._error: GraphAuthoringError | None = None

    def require(self, *, retry: bool = False) -> NodeCatalog:
        if retry:
            self._catalog = None
            self._error = None
        if self._catalog is not None:
            return self._catalog
        if self._error is not None:
            raise self._error
        try:
            self._catalog = NodeCatalog.from_runtime(
                self.runtime_root,
                types_root=self.types_root,
                expected_n8n_version=self.expected_n8n_version,
                expected_nodes_base_version=self.expected_nodes_base_version,
            )
            return self._catalog
        except GraphAuthoringError as exc:
            self._error = exc
            raise

    def status(self, *, probe: bool = False) -> dict[str, Any]:
        if probe and self._catalog is None and self._error is None:
            try:
                self.require()
            except GraphAuthoringError:
                pass
        if self._catalog is not None:
            return {
                "ready": True,
                "catalog_digest": self._catalog.digest,
                "fingerprint": copy.deepcopy(self._catalog.fingerprint),
                "node_count": len(self._catalog),
            }
        if self._error is not None:
            return {
                "ready": False,
                "error": {"code": self._error.code, "message": self._error.message},
            }
        return {"ready": False, "state": "not_loaded"}


CredentialResolver = Callable[..., Mapping[str, Any] | None]
BindingResolver = Callable[..., str | None]


class GraphAuthoringEngine:
    def __init__(
        self,
        catalog: NodeCatalog,
        *,
        credential_resolver: CredentialResolver | None = None,
        protected_workflows: Mapping[str, Any] | None = None,
        binding_resolver: BindingResolver | None = None,
        revision_token_factory: Callable[[], str] | None = None,
    ):
        self.catalog = catalog
        self.credential_resolver = credential_resolver
        self.protected_workflows = copy.deepcopy(dict(protected_workflows or {}))
        self.binding_resolver = binding_resolver
        self._revision_token_factory = revision_token_factory or (
            lambda: f"wbr_{secrets.token_urlsafe(24)}"
        )

    def workflow_digest(self, workflow: Mapping[str, Any]) -> str:
        value = {
            "name": workflow.get("name"),
            "nodes": workflow.get("nodes") or [],
            "connections": workflow.get("connections") or {},
            "settings": workflow.get("settings") or {},
        }
        return _sha256_bytes(_canonical_json(value).encode("utf-8"))

    def _mint_revision_token(self) -> str:
        revision_token = str(self._revision_token_factory() or "").strip()
        if not _REVISION_TOKEN_RE.fullmatch(revision_token):
            raise GraphAuthoringError(
                "N8N_GRAPH_REVISION_TOKEN_INVALID",
                "The server could not mint a safe workflow revision token.",
            )
        return revision_token

    def _workflow_revision_token(self, workflow: Mapping[str, Any]) -> str | None:
        """Read one server-compiled token from an authoritative base graph."""

        protected_ids = {
            str(raw.get("workflow_id") or "")
            for raw in self.protected_workflows.values()
            if isinstance(raw, Mapping)
        } | {
            str(raw)
            for raw in self.protected_workflows.values()
            if isinstance(raw, str)
        }
        tokens: set[str] = set()
        for node in workflow.get("nodes") or []:
            if not isinstance(node, Mapping):
                continue
            parameters = node.get("parameters")
            if not isinstance(parameters, Mapping):
                continue
            selector = parameters.get("workflowId")
            target = (
                str(selector.get("value") or "")
                if isinstance(selector, Mapping)
                else str(selector or "")
            )
            if target not in protected_ids:
                continue
            inputs = parameters.get("workflowInputs")
            values = inputs.get("value") if isinstance(inputs, Mapping) else None
            token = str(values.get("workflow_revision") or "") if isinstance(values, Mapping) else ""
            if token:
                tokens.add(token)
        if not tokens:
            return None
        if len(tokens) != 1 or not _REVISION_TOKEN_RE.fullmatch(next(iter(tokens))):
            raise GraphAuthoringError(
                "N8N_GRAPH_REVISION_TOKEN_INVALID",
                "The managed workflow does not contain one valid Workbench revision token.",
            )
        return next(iter(tokens))

    def materialize(
        self,
        spec: Mapping[str, Any],
        *,
        base_workflow: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> GraphResult:
        if not isinstance(spec, Mapping) or spec.get("schema") != SPEC_SCHEMA:
            raise GraphAuthoringError("N8N_SPEC_SCHEMA_INVALID", f"Expected {SPEC_SCHEMA}.")
        issues: list[GraphIssue] = []
        authoritative_context = copy.deepcopy(dict(context or {}))
        # Never accept this value from the semantic spec or caller context.
        # It is an opaque, server-minted identity for this exact compiled graph
        # and remains static inside n8n because `$workflow.activeVersionId` is
        # not exposed by n8n 2.32.5's expression proxy.
        revision_token = self._mint_revision_token()
        authoritative_context["_workbench_revision_token"] = revision_token
        binding_cache: dict[str, Any] = {}
        approval_manifests = self._approval_manifests_for_spec(spec, issues)
        authoritative_context["_approval_action_manifests"] = approval_manifests
        data_contracts = self._validate_spec_dataflow(spec, issues, authoritative_context, binding_cache)
        workflow = self._compile_spec(spec, issues, authoritative_context, binding_cache)
        validation = self.validate(workflow)
        issues.extend(validation.issues)
        result = self._result(workflow, issues, base_workflow=base_workflow)
        # Binding claims are server-private compiler state.  Only the opaque
        # Agent/approval binding id is written into the n8n workflow; the
        # provisional claim id is carried beside the compiled graph so the
        # governance layer can consume it after the exact n8n draft has been
        # reconciled.  Reconstructing claims from workflow parameters would
        # lose that one-time claim identity.
        private_binding_claims = self._binding_claims_from_cache(
            workflow_name=str(workflow.get("name") or ""),
            binding_cache=binding_cache,
            workflow_revision=revision_token,
        )
        if private_binding_claims:
            result.binding_claims = private_binding_claims
        result.graph_preview["data_contracts"] = data_contracts
        result.graph_preview["semantic_digest"] = _sha256_bytes(_canonical_json(spec).encode("utf-8"))
        return result

    def adopt(self, workflow: Mapping[str, Any]) -> GraphResult:
        copied = self._bounded_workflow_copy(workflow)
        validation = self.validate(copied)
        return self._result(copied, validation.issues, base_workflow=None)

    def _compile_spec(
        self,
        spec: Mapping[str, Any],
        issues: list[GraphIssue],
        context: Mapping[str, Any],
        binding_cache: dict[str, Any],
    ) -> dict[str, Any]:
        name = str(spec.get("name") or "").strip()
        if not name:
            issues.append(GraphIssue("WORKFLOW_NAME_REQUIRED", "Workflow name is required.", "needs_input", path="name"))
            name = "Untitled Workbench Workflow"
        raw_nodes = spec.get("nodes")
        raw_edges = spec.get("edges") or []
        if not isinstance(raw_nodes, list):
            raise GraphAuthoringError("N8N_SPEC_NODES_INVALID", "Workflow spec nodes must be a list.")
        if not isinstance(raw_edges, list):
            raise GraphAuthoringError("N8N_SPEC_EDGES_INVALID", "Workflow spec edges must be a list.")
        if len(raw_nodes) > MAX_NODES or len(raw_edges) > MAX_EDGES:
            issues.append(GraphIssue("GRAPH_LIMIT_EXCEEDED", "Workflow exceeds the reviewed graph size limits."))

        keys: set[str] = set()
        desired_names: list[str] = []
        parsed: list[tuple[str, Mapping[str, Any]]] = []
        for index, raw in enumerate(raw_nodes[: MAX_NODES + 1]):
            if not isinstance(raw, Mapping):
                issues.append(GraphIssue("NODE_SPEC_INVALID", "Node spec must be an object.", node=str(index)))
                continue
            key = str(raw.get("key") or "").strip()
            if not key:
                issues.append(GraphIssue("NODE_KEY_REQUIRED", "Every semantic node requires a stable key.", "needs_input", node=str(index)))
                key = f"missing-{index}"
            if key in keys:
                issues.append(GraphIssue("NODE_KEY_DUPLICATE", f"Duplicate node key: {key}", node=key))
                continue
            keys.add(key)
            desired_names.append(str(raw.get("name") or raw.get("label") or raw.get("type") or key).strip())
            parsed.append((key, raw))
        unique_names = self._unique_names(desired_names)
        levels = self._layout_levels([key for key, _ in parsed], raw_edges)
        level_offsets: dict[int, int] = defaultdict(int)
        compiled_nodes: list[dict[str, Any]] = []
        key_to_name: dict[str, str] = {}

        for index, ((key, raw), node_name) in enumerate(zip(parsed, unique_names)):
            type_name = str(raw.get("type") or "").strip()
            node_id = str(uuid.uuid5(_UUID_NAMESPACE, f"{name}\x00{key}"))
            parameters = copy.deepcopy(raw.get("parameters") or {})
            if not isinstance(parameters, Mapping):
                issues.append(GraphIssue("NODE_PARAMETERS_INVALID", "Node parameters must be an object.", node=key))
                parameters = {}
            node_type = type_name
            node_version: float = 0.0
            if type_name in _RESERVED_TYPES:
                node_type = "n8n-nodes-base.executeWorkflow"
                metadata = self.catalog.get(node_type)
                node_version = float(metadata["selected_version"]) if metadata else 1.3
                special = self._compile_reserved(
                    type_name, key, node_id, raw, issues, context, binding_cache
                )
                parameters = special
            else:
                requested_version = raw.get("type_version")
                try:
                    requested = float(requested_version) if requested_version is not None else None
                except (TypeError, ValueError):
                    requested = None
                    issues.append(GraphIssue("NODE_VERSION_INVALID", "Node version must be numeric.", node=key))
                metadata = self.catalog.get(type_name, requested)
                if metadata is None:
                    issues.append(GraphIssue("NODE_TYPE_UNKNOWN", f"Node type is not installed: {type_name}", node=key))
                    node_type = type_name
                    node_version = requested or 0.0
                else:
                    node_type = str(metadata["type"])
                    node_version = float(metadata["selected_version"])
            level = levels.get(key, 0)
            row = level_offsets[level]
            level_offsets[level] += 1
            position = raw.get("position")
            if not (
                isinstance(position, list)
                and len(position) == 2
                and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in position)
            ):
                position = [240 + level * 260, 180 + row * 160]
            node = {
                "id": node_id,
                "name": node_name,
                "type": node_type,
                "typeVersion": node_version,
                "position": [int(position[0]), int(position[1])],
                "parameters": copy.deepcopy(dict(parameters)),
            }
            disabled = raw.get("disabled")
            if isinstance(disabled, bool) and disabled:
                node["disabled"] = True
            self._resolve_credentials(node, raw, issues, key, context)
            compiled_nodes.append(node)
            key_to_name[key] = node_name
        connections = self._compile_connections(raw_edges[: MAX_EDGES + 1], key_to_name, issues)
        settings = copy.deepcopy(spec.get("settings") or {})
        if not isinstance(settings, Mapping):
            issues.append(GraphIssue("WORKFLOW_SETTINGS_INVALID", "Workflow settings must be an object."))
            settings = {}
        return {"name": name, "nodes": compiled_nodes, "connections": connections, "settings": dict(settings)}

    def _compile_reserved(
        self,
        type_name: str,
        key: str,
        node_id: str,
        raw: Mapping[str, Any],
        issues: list[GraphIssue],
        context: Mapping[str, Any],
        binding_cache: dict[str, Any],
    ) -> dict[str, Any]:
        config = self.protected_workflows.get(type_name)
        if isinstance(config, str):
            config = {"workflow_id": config, "name": type_name}
        if not isinstance(config, Mapping) or not str(config.get("workflow_id") or "").strip():
            issues.append(
                GraphIssue(
                    "PROTECTED_WORKFLOW_UNAVAILABLE",
                    f"Protected bridge is not configured for {type_name}.",
                    "needs_input",
                    node=key,
                )
            )
            workflow_id = ""
            workflow_name = type_name
        else:
            workflow_id = str(config.get("workflow_id"))
            workflow_name = str(config.get("name") or type_name)
        # Both protected bridges use server-side bindings.  Agent bindings own
        # the trusted prompt/model/Skill snapshot.  Approval bindings own an
        # immutable action manifest compiled from the exact downstream write;
        # n8n can present its opaque id/digest but cannot choose its authority.
        manifests = context.get("_approval_action_manifests")
        approval_manifest = (
            manifests.get(key) if isinstance(manifests, Mapping) else None
        )
        resolved_binding = (
            None
            if (
                type_name == "workbench.approval"
                and context.get("_patch_deferred_approval") is True
                and not isinstance(approval_manifest, Mapping)
            )
            else self._resolve_binding(type_name, key, raw, context, binding_cache)
        )
        if type_name == "workbench.approval":
            if isinstance(resolved_binding, Mapping):
                binding_id = str(
                    resolved_binding.get("approval_binding_id")
                    or resolved_binding.get("binding_id")
                    or resolved_binding.get("id")
                    or ""
                ).strip()
            else:
                binding_id = "wba_" + uuid.uuid5(
                    _UUID_NAMESPACE, f"approval\x00{key}\x00{node_id}"
                ).hex
        elif isinstance(resolved_binding, Mapping):
            binding_id = str(
                resolved_binding.get("agent_binding_id")
                or resolved_binding.get("binding_id")
                or resolved_binding.get("id")
                or ""
            ).strip()
        elif resolved_binding is not None:
            binding_id = str(resolved_binding or "").strip()
        else:
            binding_id = ""
        if type_name == "workbench.agent" and not binding_id:
            issues.append(
                GraphIssue("AGENT_BINDING_REQUIRED", f"An opaque binding ID is required for {type_name}.", "needs_input", node=key)
            )
        binding_parameter = str(config.get("binding_parameter") or ("agent_binding_id" if type_name == "workbench.agent" else "approval_binding_id")) if isinstance(config, Mapping) else "binding_id"
        revision_token = str(context.get("_workbench_revision_token") or "").strip()
        if not _REVISION_TOKEN_RE.fullmatch(revision_token):
            raise GraphAuthoringError(
                "N8N_GRAPH_REVISION_TOKEN_INVALID",
                "The server workflow revision token is unavailable.",
            )
        # The parent workflow supplies its id dynamically, while the revision
        # identity is a server-minted static token.  n8n 2.32.5 exposes only
        # active/id/name on the `$workflow` proxy, so activeVersionId would be
        # undefined here.  Workbench separately verifies the live active
        # version through its read-only n8n resolver on every production call.
        workflow_inputs: dict[str, Any] = {
            binding_parameter: binding_id,
            "workflow_id": "={{$workflow.id}}",
            "workflow_revision": revision_token,
            "node_id": node_id,
            "request_id": "={{$execution.id + '-' + $itemIndex}}",
            "input": "={{$json}}",
        }
        if type_name == "workbench.approval":
            manifest = approval_manifest
            resolved_manifest_digest = (
                str(resolved_binding.get("manifest_digest") or "").strip()
                if isinstance(resolved_binding, Mapping) else ""
            )
            manifest_digest = (
                resolved_manifest_digest
                if _SHA256_RE.fullmatch(resolved_manifest_digest)
                else _sha256_bytes(_canonical_json(manifest).encode("utf-8"))
                if isinstance(manifest, Mapping)
                else ""
            )
            if (
                (not isinstance(manifest, Mapping) or not _SHA256_RE.fullmatch(manifest_digest))
                and context.get("_patch_deferred_approval") is not True
            ):
                issues.append(
                    GraphIssue(
                        "APPROVAL_ACTION_BINDING_REQUIRED",
                        "The approval gate must bind one reviewed downstream external action.",
                        "needs_input",
                        node=key,
                    )
                )
            workflow_inputs["manifest_digest"] = manifest_digest
            workflow_inputs["approval_token"] = (
                f"{binding_id}:{manifest_digest}" if binding_id and manifest_digest else ""
            )
            workflow_inputs["input"] = (
                self._approval_input_expression(manifest)
                if isinstance(manifest, Mapping)
                else "={{$json}}"
            )
        return {
            "source": "database",
            "workflowId": {"__rl": True, "value": workflow_id, "mode": "list", "cachedResultName": workflow_name},
            "workflowInputs": {"mappingMode": "defineBelow", "value": workflow_inputs},
            "mode": "each",
            "options": {"waitForSubWorkflow": True},
        }

    @staticmethod
    def _direct_json_expression_field(value: Any) -> str | None:
        """Return the one reviewed `$json.field` selector, otherwise None."""

        if not isinstance(value, str):
            return None
        text = value.strip()
        match = re.fullmatch(
            r"=\{\{\s*\$json(?:\.([A-Za-z_][A-Za-z0-9_]*)|\[['\"]([^'\"]+)['\"]\])\s*\}\}",
            text,
        )
        if not match:
            return None
        return str(match.group(1) or match.group(2) or "").strip() or None

    @staticmethod
    def _expression_strings(value: Any) -> list[str]:
        result: list[str] = []
        if isinstance(value, Mapping):
            for child in value.values():
                result.extend(GraphAuthoringEngine._expression_strings(child))
        elif isinstance(value, list):
            for child in value:
                result.extend(GraphAuthoringEngine._expression_strings(child))
        elif isinstance(value, str) and (value.lstrip().startswith("=") or "{{" in value):
            result.append(value)
        return result

    def _external_action_manifest(
        self,
        raw: Mapping[str, Any],
        *,
        node_id: str,
        approval_node_id: str,
        issues: list[GraphIssue],
        node_label: str,
    ) -> dict[str, Any] | None:
        """Compile the only authority an approval gate may request at runtime."""

        type_name = str(raw.get("type") or "")
        metadata = self.catalog.get(type_name)
        canonical_type = str(metadata.get("type") or type_name) if metadata else type_name
        short_name = type_name.rsplit(".", 1)[-1]
        parameters = raw.get("parameters") or {}
        if not isinstance(parameters, Mapping) or not self._is_external_write(raw):
            return None
        aliases = raw.get("credential_aliases") or {}
        if not isinstance(aliases, Mapping):
            aliases = {}
        alias_items = sorted(
            (
                str(credential_type).strip(),
                str(alias).strip(),
            )
            for credential_type, alias in aliases.items()
            if str(credential_type).strip() and str(alias).strip()
        )
        if len(alias_items) != 1:
            issues.append(
                GraphIssue(
                    "APPROVAL_CREDENTIAL_ALIAS_REQUIRED",
                    "An external write must bind exactly one Project credential alias.",
                    "needs_input",
                    node=node_label,
                )
            )
            return None
        credential_type, credential_alias = alias_items[0]
        if short_name == "gmail":
            action = "send_email"
            target_kind = "email"
            target_value = parameters.get("sendTo")
            operation = str(parameters.get("operation") or "").casefold()
        elif short_name == "httpRequest":
            action = "http_write"
            target_kind = "url"
            target_value = parameters.get("url")
            operation = str(
                parameters.get("method") or parameters.get("requestMethod") or "GET"
            ).upper()
        else:
            return None
        target_rule: dict[str, Any]
        field_name = self._direct_json_expression_field(target_value)
        if field_name:
            target_rule = {"mode": "json_field", "field": field_name}
        elif isinstance(target_value, str) and not (
            target_value.lstrip().startswith("=") or "{{" in target_value
        ) and target_value.strip():
            target_rule = {"mode": "static", "value": target_value.strip()}
        else:
            issues.append(
                GraphIssue(
                    "EXTERNAL_TARGET_REVIEW_REQUIRED",
                    "The external target must be static or one direct reviewed $json field.",
                    "needs_input",
                    node=node_label,
                )
            )
            return None
        return {
            "schema": "approval_action_manifest.v1",
            "approval_node_id": approval_node_id,
            "downstream_node_id": node_id,
            "downstream_node_type": canonical_type,
            "credential_alias": credential_alias,
            "credential_type": credential_type,
            "target_kind": target_kind,
            "target_rule": target_rule,
            "action": action,
            "operation": operation,
        }

    def _approval_manifests_for_spec(
        self, spec: Mapping[str, Any], issues: list[GraphIssue]
    ) -> dict[str, dict[str, Any]]:
        raw_nodes = spec.get("nodes") or []
        raw_edges = spec.get("edges") or []
        if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
            return {}
        nodes = {
            str(raw.get("key") or ""): raw
            for raw in raw_nodes
            if isinstance(raw, Mapping) and str(raw.get("key") or "")
        }
        predecessors: dict[str, set[str]] = defaultdict(set)
        for edge in raw_edges:
            if isinstance(edge, Mapping):
                predecessors[str(edge.get("to") or "")].add(str(edge.get("from") or ""))
        approvals = {
            key for key, raw in nodes.items()
            if str(raw.get("type") or "") == "workbench.approval"
        }
        workflow_name = str(spec.get("name") or "").strip() or "Untitled Workbench Workflow"
        manifests: dict[str, dict[str, Any]] = {}
        for downstream_key, raw in nodes.items():
            if not self._is_external_write(raw):
                continue
            immediate = sorted(predecessors.get(downstream_key, set()) & approvals)
            if len(immediate) != 1 or predecessors.get(downstream_key, set()) != {immediate[0]}:
                issues.append(
                    GraphIssue(
                        "APPROVAL_ACTION_BINDING_REQUIRED",
                        "Every external write must be directly and exclusively preceded by one approval gate.",
                        node=downstream_key,
                    )
                )
                continue
            approval_key = immediate[0]
            if approval_key in manifests:
                issues.append(
                    GraphIssue(
                        "APPROVAL_ACTION_AMBIGUOUS",
                        "One approval gate cannot authorize more than one external write.",
                        node=approval_key,
                    )
                )
                continue
            approval_node_id = str(
                uuid.uuid5(_UUID_NAMESPACE, f"{workflow_name}\x00{approval_key}")
            )
            downstream_node_id = str(
                uuid.uuid5(_UUID_NAMESPACE, f"{workflow_name}\x00{downstream_key}")
            )
            manifest = self._external_action_manifest(
                raw,
                node_id=downstream_node_id,
                approval_node_id=approval_node_id,
                issues=issues,
                node_label=downstream_key,
            )
            if manifest is not None:
                manifests[approval_key] = manifest
        for approval_key in sorted(approvals - set(manifests)):
            if not any(issue.node == approval_key and issue.code.startswith("APPROVAL_") for issue in issues):
                issues.append(
                    GraphIssue(
                        "APPROVAL_ACTION_BINDING_REQUIRED",
                        "The approval gate must directly precede one reviewed external write.",
                        "needs_input",
                        node=approval_key,
                    )
                )
        return manifests

    @staticmethod
    def _approval_input_expression(manifest: Mapping[str, Any]) -> str:
        target_rule = manifest.get("target_rule") or {}
        if isinstance(target_rule, Mapping) and target_rule.get("mode") == "json_field":
            field = str(target_rule.get("field") or "")
            target = f"$json[{json.dumps(field, ensure_ascii=False)}]"
        else:
            target = json.dumps(
                str(target_rule.get("value") or "") if isinstance(target_rule, Mapping) else "",
                ensure_ascii=False,
            )
        static = {
            "credential_alias": str(manifest.get("credential_alias") or ""),
            "target_kind": str(manifest.get("target_kind") or ""),
            "action": str(manifest.get("action") or ""),
        }
        return (
            "={{({payload:$json,credential_alias:"
            + json.dumps(static["credential_alias"], ensure_ascii=False)
            + ",target_kind:"
            + json.dumps(static["target_kind"], ensure_ascii=False)
            + ",target:("
            + target
            + "),action:"
            + json.dumps(static["action"], ensure_ascii=False)
            + ",task_id:null})}}"
        )

    def _validate_spec_dataflow(
        self,
        spec: Mapping[str, Any],
        issues: list[GraphIssue],
        context: Mapping[str, Any],
        binding_cache: dict[str, Any],
    ) -> dict[str, Any]:
        raw_nodes = spec.get("nodes") or []
        raw_edges = spec.get("edges") or []
        if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
            return {"nodes": [], "edges": []}
        nodes: dict[str, Mapping[str, Any]] = {}
        output_schemas: dict[str, Mapping[str, Any] | None] = {}
        input_schemas: dict[str, Mapping[str, Any] | None] = {}
        preview_nodes: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_nodes):
            if not isinstance(raw, Mapping):
                continue
            key = str(raw.get("key") or f"#{index}")
            nodes[key] = raw
            output_schema = raw.get("output_schema")
            if output_schema is None and raw.get("type") == "workbench.agent" and self.binding_resolver is not None:
                resolved_binding = self._resolve_binding(
                    "workbench.agent", key, raw, context, binding_cache
                )
                if isinstance(resolved_binding, Mapping):
                    output_schema = resolved_binding.get("output_schema")
            input_schema = raw.get("input_schema")
            output_schemas[key] = self._validate_object_schema(output_schema, key, "output_schema", issues)
            input_schemas[key] = self._validate_object_schema(input_schema, key, "input_schema", issues)
            preview_nodes.append(
                {
                    "key": key,
                    "output_fields": sorted(self._schema_properties(output_schemas[key])),
                    "input_fields": sorted(self._schema_properties(input_schemas[key])),
                }
            )

        preview_edges: list[dict[str, Any]] = []
        incoming_mapped_fields: dict[str, set[str]] = defaultdict(set)
        for index, edge in enumerate(raw_edges):
            if not isinstance(edge, Mapping):
                continue
            source = str(edge.get("from") or "")
            target = str(edge.get("to") or "")
            mappings = edge.get("field_mappings")
            normalized: list[dict[str, str]] = []
            if mappings is not None and not isinstance(mappings, list):
                issues.append(
                    GraphIssue(
                        "FIELD_MAPPINGS_INVALID",
                        "field_mappings must be a list of {from,to} objects.",
                        node=source,
                        path=f"edges[{index}].field_mappings",
                    )
                )
                mappings = []
            for mapping_index, mapping in enumerate(mappings or []):
                if not isinstance(mapping, Mapping):
                    issues.append(GraphIssue("FIELD_MAPPING_INVALID", "Field mapping must be an object.", path=f"edges[{index}].field_mappings[{mapping_index}]"))
                    continue
                source_field = str(mapping.get("from") or "").strip()
                target_field = str(mapping.get("to") or "").strip()
                if not source_field or not target_field:
                    issues.append(GraphIssue("FIELD_MAPPING_INVALID", "Field mapping requires from and to paths.", path=f"edges[{index}].field_mappings[{mapping_index}]"))
                    continue
                source_schema = output_schemas.get(source)
                target_schema = input_schemas.get(target)
                if source_schema is None:
                    issues.append(
                        GraphIssue(
                            "SOURCE_OUTPUT_SCHEMA_REQUIRED",
                            f"Output schema is required to verify mapping from {source}.",
                            "needs_input",
                            node=source,
                        )
                    )
                elif not self._schema_has_path(source_schema, source_field):
                    issues.append(
                        GraphIssue(
                            "SOURCE_FIELD_UNAVAILABLE",
                            f"Source schema does not declare field: {source_field}",
                            "needs_input",
                            node=source,
                            path=source_field,
                        )
                    )
                if target_schema is None:
                    issues.append(
                        GraphIssue(
                            "TARGET_INPUT_SCHEMA_REQUIRED",
                            f"Input schema is required to verify mapping into {target}.",
                            "needs_input",
                            node=target,
                        )
                    )
                elif not self._schema_has_path(target_schema, target_field):
                    issues.append(
                        GraphIssue(
                            "TARGET_FIELD_UNAVAILABLE",
                            f"Target schema does not declare field: {target_field}",
                            "needs_input",
                            node=target,
                            path=target_field,
                        )
                    )
                if target_field in incoming_mapped_fields[target]:
                    issues.append(GraphIssue("FIELD_MAPPING_TARGET_DUPLICATE", f"Multiple mappings write target field: {target_field}", node=target))
                incoming_mapped_fields[target].add(target_field)
                normalized.append({"from": source_field, "to": target_field})
            preview_edges.append({"from": source, "to": target, "field_mappings": normalized})

        for key, raw in nodes.items():
            parameters = raw.get("parameters") or {}
            expression_fields = self._json_expression_fields(parameters)
            if not expression_fields:
                continue
            incoming = [edge for edge in raw_edges if isinstance(edge, Mapping) and str(edge.get("to") or "") == key]
            mapped = incoming_mapped_fields.get(key, set())
            external_write = self._is_external_write(raw)
            for field_name in sorted(expression_fields):
                if field_name not in mapped:
                    issues.append(
                        GraphIssue(
                            "DATA_FIELD_MAPPING_REQUIRED" if external_write else "DATA_FIELD_SOURCE_UNVERIFIED",
                            f"Expression field cannot be verified from incoming mappings: {field_name}",
                            "needs_input",
                            node=key,
                            path=field_name,
                        )
                    )
        return {"nodes": preview_nodes, "edges": preview_edges}

    def _resolve_binding(
        self,
        type_name: str,
        key: str,
        raw: Mapping[str, Any],
        context: Mapping[str, Any],
        binding_cache: dict[str, Any],
    ) -> Any:
        cache_key = f"{type_name}\x00{key}"
        if cache_key in binding_cache:
            return binding_cache[cache_key]
        if self.binding_resolver is None:
            binding_cache[cache_key] = None
            return None
        safe_raw = copy.deepcopy(dict(raw))
        safe_raw.pop("binding_id", None)
        try:
            resolved = self.binding_resolver(type_name, safe_raw, copy.deepcopy(dict(context)))
        except TypeError:
            try:
                resolved = self.binding_resolver(type_name, safe_raw)
            except TypeError:
                resolved = self.binding_resolver(type_name)
        binding_cache[cache_key] = copy.deepcopy(resolved)
        return binding_cache[cache_key]

    @staticmethod
    def _validate_object_schema(
        schema: Any, node_key: str, field_name: str, issues: list[GraphIssue]
    ) -> Mapping[str, Any] | None:
        if schema is None:
            return None
        if not isinstance(schema, Mapping) or schema.get("type", "object") != "object" or not isinstance(schema.get("properties"), Mapping):
            issues.append(
                GraphIssue(
                    "DATA_SCHEMA_INVALID",
                    f"{field_name} must be an object schema with properties.",
                    node=node_key,
                    path=field_name,
                )
            )
            return None
        properties = schema.get("properties") or {}
        for name, definition in properties.items():
            if not isinstance(name, str) or not name or not isinstance(definition, Mapping):
                issues.append(GraphIssue("DATA_SCHEMA_PROPERTY_INVALID", f"Invalid property in {field_name}.", node=node_key, path=field_name))
                return None
            property_type = definition.get("type")
            if property_type not in {"string", "number", "integer", "boolean", "object", "array", "null", None}:
                issues.append(GraphIssue("DATA_SCHEMA_TYPE_UNSUPPORTED", f"Unsupported schema type: {property_type}", node=node_key, path=f"{field_name}.{name}"))
        required = schema.get("required", [])
        if not isinstance(required, list) or any(value not in properties for value in required):
            issues.append(GraphIssue("DATA_SCHEMA_REQUIRED_INVALID", f"{field_name}.required must reference declared fields.", node=node_key))
        return copy.deepcopy(dict(schema))

    @staticmethod
    def _schema_properties(schema: Mapping[str, Any] | None) -> Mapping[str, Any]:
        if not isinstance(schema, Mapping) or not isinstance(schema.get("properties"), Mapping):
            return {}
        return schema["properties"]

    @classmethod
    def _schema_has_path(cls, schema: Mapping[str, Any], path: str) -> bool:
        current: Mapping[str, Any] | None = schema
        for part in path.split("."):
            properties = cls._schema_properties(current)
            definition = properties.get(part)
            if not isinstance(definition, Mapping):
                return False
            current = definition
        return True

    @staticmethod
    def _json_expression_fields(value: Any) -> set[str]:
        result: set[str] = set()
        if isinstance(value, Mapping):
            for child in value.values():
                result.update(GraphAuthoringEngine._json_expression_fields(child))
        elif isinstance(value, list):
            for child in value:
                result.update(GraphAuthoringEngine._json_expression_fields(child))
        elif isinstance(value, str) and (value.startswith("=") or "{{" in value):
            result.update(re.findall(r"\$json\.([A-Za-z_][A-Za-z0-9_]*)", value))
            result.update(re.findall(r"\$json\[['\"]([^'\"]+)['\"]\]", value))
        return result

    @staticmethod
    def _external_action_classification(raw: Mapping[str, Any]) -> str:
        """Classify an external node without guessing that an unknown action is safe.

        ``supported_write`` actions have a reviewed runtime-approval manifest
        adapter.  ``unadapted_write`` actions are known writes but cannot yet
        be materialized safely.  ``unclassified`` is the fail-closed state for
        credential-backed services whose side effects are not proven.
        """

        type_name = str(raw.get("type") or "").rsplit(".", 1)[-1]
        normalized_type = type_name.casefold()
        parameters = raw.get("parameters") or {}
        if not isinstance(parameters, Mapping):
            return "unclassified" if raw.get("credential_aliases") or raw.get("credentials") else "none"
        operation = str(parameters.get("operation") or "").casefold()
        has_credentials = bool(raw.get("credential_aliases") or raw.get("credentials"))

        if normalized_type == "gmail":
            if operation in {"send", "reply", "sendandwait"}:
                return "supported_write"
            if operation in _EXPLICIT_READ_ONLY_OPERATIONS:
                return "read"
            return "unclassified" if has_credentials else "none"
        if normalized_type == "httprequest":
            method = str(
                parameters.get("method") or parameters.get("requestMethod") or "GET"
            ).upper()
            return "read" if method in {"GET", "HEAD", "OPTIONS"} else "supported_write"
        # A generic Execute Sub-workflow node can hide arbitrary downstream
        # credentials and writes behind one visually innocuous node.  Only the
        # two exact Workbench-protected bridges are reviewed separately by
        # `_validate_protected_node`; every other sub-workflow call is blocked.
        if normalized_type == "executeworkflow":
            return "unclassified"

        if operation in _EXPLICIT_READ_ONLY_OPERATIONS:
            return "read"
        if (
            operation in _OBVIOUS_WRITE_OPERATIONS
            or (
                normalized_type in _UNADAPTED_EXTERNAL_WRITE_TYPES
                and operation in {"executequery", "execute", "run"}
            )
        ):
            return "unadapted_write"
        if has_credentials:
            return "unclassified"
        return "none"

    @classmethod
    def _is_external_write(cls, raw: Mapping[str, Any]) -> bool:
        return cls._external_action_classification(raw) in {
            "supported_write", "unadapted_write"
        }

    def _resolve_credentials(
        self,
        node: dict[str, Any],
        raw: Mapping[str, Any],
        issues: list[GraphIssue],
        key: str,
        context: Mapping[str, Any],
    ) -> None:
        aliases = raw.get("credential_aliases") or {}
        if not isinstance(aliases, Mapping):
            issues.append(GraphIssue("CREDENTIAL_ALIASES_INVALID", "Credential aliases must be an object.", node=key))
            return
        resolved: dict[str, Any] = {}
        for credential_type, alias_value in aliases.items():
            alias = str(alias_value or "").strip()
            credential_name = str(credential_type or "").strip()
            if not alias or not credential_name:
                issues.append(GraphIssue("CREDENTIAL_ALIAS_REQUIRED", "Credential alias is incomplete.", "needs_input", node=key))
                continue
            result: Mapping[str, Any] | None = None
            if self.credential_resolver is not None:
                resolver_context = copy.deepcopy(dict(context))
                resolver_context.update({"node_key": key, "node_type": node["type"]})
                try:
                    result = self.credential_resolver(alias, credential_name, resolver_context)
                except TypeError:
                    result = self.credential_resolver(alias, credential_name)
            if not isinstance(result, Mapping) or not str(result.get("id") or "").strip():
                issues.append(
                    GraphIssue(
                        "CREDENTIAL_ALIAS_UNRESOLVED",
                        f"Credential alias is not available: {alias}",
                        "needs_input",
                        node=key,
                        details={"alias": alias, "credential_type": credential_name},
                    )
                )
                continue
            resolved[credential_name] = {"id": str(result.get("id")), "name": str(result.get("name") or alias)}
        if resolved:
            node["credentials"] = resolved

    @staticmethod
    def _unique_names(desired: Sequence[str]) -> list[str]:
        seen: dict[str, int] = defaultdict(int)
        result: list[str] = []
        for raw in desired:
            base = (raw or "Node").strip()[:128] or "Node"
            seen[base.casefold()] += 1
            suffix = seen[base.casefold()]
            result.append(base if suffix == 1 else f"{base} {suffix}")
        return result

    @staticmethod
    def _layout_levels(keys: list[str], edges: list[Any]) -> dict[str, int]:
        adjacency: dict[str, list[str]] = defaultdict(list)
        indegree = {key: 0 for key in keys}
        for raw in edges:
            if not isinstance(raw, Mapping):
                continue
            source, target = str(raw.get("from") or ""), str(raw.get("to") or "")
            if source in indegree and target in indegree and source != target:
                adjacency[source].append(target)
                indegree[target] += 1
        queue = deque(sorted(key for key, value in indegree.items() if value == 0))
        levels = {key: 0 for key in keys}
        visited: set[str] = set()
        while queue:
            source = queue.popleft()
            visited.add(source)
            for target in sorted(adjacency[source]):
                levels[target] = max(levels[target], levels[source] + 1)
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        for key in keys:
            if key not in visited:
                levels[key] = max(levels.values(), default=0) + 1
        return levels

    @staticmethod
    def _compile_connections(
        edges: Sequence[Any], key_to_name: Mapping[str, str], issues: list[GraphIssue]
    ) -> dict[str, Any]:
        connections: dict[str, dict[str, list[list[dict[str, Any]]]]] = {}
        seen: set[tuple[str, str, int, str, int]] = set()
        for index, raw in enumerate(edges):
            if not isinstance(raw, Mapping):
                issues.append(GraphIssue("EDGE_SPEC_INVALID", "Edge spec must be an object.", path=f"edges[{index}]"))
                continue
            source_key, target_key = str(raw.get("from") or ""), str(raw.get("to") or "")
            if source_key not in key_to_name or target_key not in key_to_name:
                issues.append(GraphIssue("EDGE_NODE_UNKNOWN", "Edge refers to an unknown semantic node.", path=f"edges[{index}]"))
                continue
            output_type = str(raw.get("output") or "main")
            input_type = str(raw.get("input") or "main")
            try:
                output_index = int(raw.get("output_index", 0))
                input_index = int(raw.get("input_index", 0))
            except (TypeError, ValueError):
                issues.append(GraphIssue("EDGE_PORT_INVALID", "Edge port indexes must be integers.", path=f"edges[{index}]"))
                continue
            item = (key_to_name[source_key], output_type, output_index, key_to_name[target_key], input_index)
            if item in seen:
                issues.append(GraphIssue("EDGE_DUPLICATE", "Duplicate edge is not allowed.", path=f"edges[{index}]"))
                continue
            seen.add(item)
            outputs = connections.setdefault(item[0], {}).setdefault(item[1], [])
            while len(outputs) <= output_index:
                outputs.append([])
            outputs[output_index].append({"node": item[3], "type": input_type, "index": input_index})
        return connections

    def validate(self, workflow: Mapping[str, Any]) -> ValidationResult:
        issues: list[GraphIssue] = []
        try:
            value = self._bounded_workflow_copy(workflow)
        except GraphAuthoringError as exc:
            return ValidationResult("blocked", [GraphIssue(exc.code, exc.message, details=exc.details)])
        nodes = value.get("nodes")
        connections = value.get("connections")
        if not isinstance(nodes, list) or not isinstance(connections, Mapping):
            return ValidationResult("blocked", [GraphIssue("GRAPH_SHAPE_INVALID", "Workflow nodes/connections shape is invalid.")])
        if not nodes:
            issues.append(GraphIssue("GRAPH_EMPTY", "Workflow must contain at least one node.", "needs_input"))
        if len(nodes) > MAX_NODES:
            issues.append(GraphIssue("GRAPH_NODE_LIMIT", f"Workflow may contain at most {MAX_NODES} nodes."))
        names: set[str] = set()
        ids: set[str] = set()
        by_name: dict[str, Mapping[str, Any]] = {}
        port_cache: dict[str, tuple[int | None, int | None]] = {}
        trigger_names: set[str] = set()
        for index, node in enumerate(nodes):
            if not isinstance(node, Mapping):
                issues.append(GraphIssue("NODE_INVALID", "Workflow node must be an object.", node=str(index)))
                continue
            name = str(node.get("name") or "").strip()
            node_id = str(node.get("id") or "").strip()
            if not name:
                issues.append(GraphIssue("NODE_NAME_REQUIRED", "Node name is required.", node=str(index)))
                name = f"#{index}"
            if name.casefold() in names:
                issues.append(GraphIssue("NODE_NAME_DUPLICATE", f"Duplicate node name: {name}", node=name))
            names.add(name.casefold())
            if not node_id:
                issues.append(GraphIssue("NODE_ID_REQUIRED", "Node ID is required.", node=name))
            elif node_id in ids:
                issues.append(GraphIssue("NODE_ID_DUPLICATE", f"Duplicate node ID: {node_id}", node=name))
            ids.add(node_id)
            by_name[name] = node
            try:
                version = float(node.get("typeVersion"))
            except (TypeError, ValueError):
                version = -1
            metadata = self.catalog.get(str(node.get("type") or ""), version)
            if metadata is None:
                issues.append(
                    GraphIssue(
                        "NODE_TYPE_VERSION_UNAVAILABLE",
                        f"Installed node type/version is unavailable: {node.get('type')}@{node.get('typeVersion')}",
                        node=name,
                    )
                )
                continue
            params = node.get("parameters") or {}
            if not isinstance(params, Mapping):
                issues.append(GraphIssue("NODE_PARAMETERS_INVALID", "Node parameters must be an object.", node=name))
                params = {}
            self._validate_parameters(metadata, params, name, issues)
            self._validate_credentials(metadata, params, node.get("credentials") or {}, name, issues)
            self._scan_expressions(params, name, issues)
            is_trigger = self._is_trigger_node(node, metadata)
            if is_trigger:
                trigger_names.add(name)
            protected_kind = self._protected_node_kind(node)
            external_classification = self._external_action_classification(
                {
                    "type": node.get("type"),
                    "parameters": params,
                    "credentials": node.get("credentials") or {},
                }
            )
            if not is_trigger and not protected_kind and external_classification in {
                "unadapted_write", "unclassified"
            }:
                issues.append(
                    GraphIssue(
                        "EXTERNAL_ACTION_CLASSIFICATION_REQUIRED",
                        "This external service action is not yet proven read-only or bound to a reviewed runtime approval adapter.",
                        "needs_input",
                        node=name,
                    )
                )
            if protected_kind:
                self._validate_protected_node(node, protected_kind, issues)
            node_type = str(node.get("type") or "")
            short_type = node_type.rsplit(".", 1)[-1].casefold()
            if node_type.startswith("@n8n/n8n-nodes-langchain.") and (
                "agent" in short_type or "tool" in short_type
            ):
                issues.append(
                    GraphIssue(
                        "GENERIC_AGENT_TOOL_HIGH_RISK",
                        "Generic LangChain Agent/Tool nodes require governed high-risk review.",
                        "warning",
                        node=name,
                    )
                )
            port_cache[name] = self._port_counts(metadata, params)
        edge_records = self._edge_records(connections, issues)
        if len(edge_records) > MAX_EDGES:
            issues.append(GraphIssue("GRAPH_EDGE_LIMIT", f"Workflow may contain at most {MAX_EDGES} edges."))
        seen_edges: set[tuple[str, str, int, str, str, int]] = set()
        degree: dict[str, int] = {name: 0 for name in by_name}
        adjacency: dict[str, list[str]] = defaultdict(list)
        for source, output_type, output_index, target, input_type, input_index in edge_records:
            edge_key = (source, output_type, output_index, target, input_type, input_index)
            if edge_key in seen_edges:
                issues.append(GraphIssue("EDGE_DUPLICATE", f"Duplicate edge: {source} to {target}."))
                continue
            seen_edges.add(edge_key)
            if source not in by_name or target not in by_name:
                issues.append(GraphIssue("EDGE_DANGLING", f"Edge refers to a missing node: {source} to {target}."))
                continue
            if output_type != "main" or input_type != "main":
                issues.append(GraphIssue("EDGE_TYPE_UNREVIEWED", "Only reviewed main-data connections are allowed.", node=source))
            outputs = port_cache.get(source, (None, None))[1]
            inputs = port_cache.get(target, (None, None))[0]
            if output_index < 0 or (outputs is not None and output_index >= outputs):
                issues.append(GraphIssue("EDGE_OUTPUT_PORT_INVALID", f"Invalid output port {output_index} on {source}.", node=source))
            if input_index < 0 or (inputs is not None and input_index >= inputs):
                issues.append(GraphIssue("EDGE_INPUT_PORT_INVALID", f"Invalid input port {input_index} on {target}.", node=target))
            if outputs is None or inputs is None:
                issues.append(
                    GraphIssue(
                        "DYNAMIC_PORTS_UNRESOLVED",
                        f"Dynamic ports require user confirmation: {source} to {target}.",
                        "needs_input",
                        node=source,
                    )
                )
            degree[source] += 1
            degree[target] += 1
            adjacency[source].append(target)
        if len(by_name) > 1:
            for name, count in degree.items():
                if count == 0:
                    issues.append(GraphIssue("NODE_ORPHAN", f"Node is not connected: {name}", node=name))
            self._validate_connected(by_name, adjacency, issues)
        if by_name and not trigger_names:
            issues.append(
                GraphIssue(
                    "TRIGGER_REQUIRED",
                    "Workflow requires at least one enabled trigger node.",
                    "needs_input",
                )
            )
        self._validate_external_write_approvals(
            by_name, adjacency, edge_records, trigger_names, issues
        )
        self._validate_cycles(by_name, adjacency, edge_records, issues)
        status = self._status_for(issues)
        return ValidationResult(status, self._dedupe_issues(issues))

    @staticmethod
    def _is_trigger_node(node: Mapping[str, Any], metadata: Mapping[str, Any]) -> bool:
        if bool(node.get("disabled", False)):
            return False
        node_type = str(node.get("type") or "")
        short_name = node_type.rsplit(".", 1)[-1].casefold()
        groups = {str(value).casefold() for value in metadata.get("group") or []}
        return "trigger" in groups or short_name.endswith("trigger") or short_name in {
            "webhook", "form", "chattrigger"
        }

    def _protected_node_kind(self, node: Mapping[str, Any]) -> str | None:
        if str(node.get("type") or "") != "n8n-nodes-base.executeWorkflow":
            return None
        parameters = node.get("parameters") or {}
        selector = parameters.get("workflowId") if isinstance(parameters, Mapping) else None
        workflow_id = (
            str(selector.get("value") or "")
            if isinstance(selector, Mapping)
            else str(selector or "")
        )
        for kind, raw in self.protected_workflows.items():
            protected_id = (
                str(raw.get("workflow_id") or "")
                if isinstance(raw, Mapping)
                else str(raw or "")
            )
            if protected_id and secrets.compare_digest(workflow_id, protected_id):
                return str(kind)
        return None

    def _validate_protected_node(
        self,
        node: Mapping[str, Any],
        kind: str,
        issues: list[GraphIssue],
    ) -> None:
        """Fail closed when a protected Execute Sub-workflow identity is edited."""

        name = str(node.get("name") or "")
        parameters = node.get("parameters") or {}
        if not isinstance(parameters, Mapping):
            return
        expected_parameter_keys = {
            "source", "workflowId", "workflowInputs", "mode", "options"
        }
        if set(parameters) != expected_parameter_keys:
            issues.append(
                GraphIssue(
                    "PROTECTED_NODE_IDENTITY_INVALID",
                    "Protected bridge parameters were changed.",
                    node=name,
                )
            )
            return
        selector = parameters.get("workflowId")
        inputs = parameters.get("workflowInputs")
        values = inputs.get("value") if isinstance(inputs, Mapping) else None
        if (
            parameters.get("source") != "database"
            or not isinstance(selector, Mapping)
            or selector.get("__rl") is not True
            or selector.get("mode") != "list"
            or not isinstance(inputs, Mapping)
            or inputs.get("mappingMode") != "defineBelow"
            or not isinstance(values, Mapping)
            or parameters.get("mode") != "each"
            or parameters.get("options") != {"waitForSubWorkflow": True}
        ):
            issues.append(
                GraphIssue(
                    "PROTECTED_NODE_IDENTITY_INVALID",
                    "Protected bridge execution settings were changed.",
                    node=name,
                )
            )
            return
        common = {
            "workflow_id", "workflow_revision", "node_id", "request_id", "input"
        }
        binding_parameter = (
            "agent_binding_id" if kind == "workbench.agent" else "approval_binding_id"
        )
        expected_inputs = common | {binding_parameter}
        if kind == "workbench.approval":
            expected_inputs |= {"manifest_digest", "approval_token"}
        if set(values) != expected_inputs:
            issues.append(
                GraphIssue(
                    "PROTECTED_NODE_INPUTS_INVALID",
                    "Protected bridge inputs were changed.",
                    node=name,
                )
            )
            return
        binding_id = str(values.get(binding_parameter) or "")
        revision = str(values.get("workflow_revision") or "")
        if not binding_id or not revision:
            issues.append(
                GraphIssue(
                    "PROTECTED_NODE_IDENTITY_REQUIRED",
                    "Protected bridge identity fields are required.",
                    "needs_input",
                    node=name,
                )
            )
            return
        if (
            not _BINDING_ID_RE.fullmatch(binding_id)
            or not _REVISION_TOKEN_RE.fullmatch(revision)
            or values.get("workflow_id") != "={{$workflow.id}}"
            or values.get("node_id") != str(node.get("id") or "")
            or values.get("request_id") != "={{$execution.id + '-' + $itemIndex}}"
        ):
            issues.append(
                GraphIssue(
                    "PROTECTED_NODE_IDENTITY_INVALID",
                    "Protected bridge identity fields were changed.",
                    node=name,
                )
            )
            return
        if kind == "workbench.agent" and values.get("input") != "={{$json}}":
            issues.append(
                GraphIssue(
                    "PROTECTED_NODE_INPUTS_INVALID",
                    "Protected Agent input mapping was changed.",
                    node=name,
                )
            )
        if kind == "workbench.approval":
            digest = str(values.get("manifest_digest") or "")
            if not digest or not values.get("approval_token"):
                issues.append(
                    GraphIssue(
                        "APPROVAL_ACTION_BINDING_REQUIRED",
                        "Protected approval action identity is required.",
                        "needs_input",
                        node=name,
                    )
                )
                return
            if (
                not _SHA256_RE.fullmatch(digest)
                or values.get("approval_token") != f"{binding_id}:{digest}"
            ):
                issues.append(
                    GraphIssue(
                        "PROTECTED_NODE_IDENTITY_INVALID",
                        "Protected approval manifest identity was changed.",
                        node=name,
                    )
                )

    @staticmethod
    def _reachable(
        starts: Iterable[str], adjacency: Mapping[str, Sequence[str]]
    ) -> set[str]:
        visited: set[str] = set()
        queue = deque(str(value) for value in starts)
        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            queue.extend(str(value) for value in adjacency.get(current, []))
        return visited

    def _validate_external_write_approvals(
        self,
        by_name: Mapping[str, Mapping[str, Any]],
        adjacency: Mapping[str, list[str]],
        edges: Sequence[tuple[str, str, int, str, str, int]],
        trigger_names: set[str],
        issues: list[GraphIssue],
    ) -> None:
        predecessors: dict[str, set[str]] = defaultdict(set)
        for source, _, _, target, _, _ in edges:
            if source in by_name and target in by_name:
                predecessors[target].add(source)
        approval_names = {
            name
            for name, node in by_name.items()
            if self._protected_node_kind(node) == "workbench.approval"
            and not bool(node.get("disabled", False))
        }
        reachable = self._reachable(trigger_names, adjacency)
        used_approvals: set[str] = set()
        for name, node in by_name.items():
            raw = {
                "type": node.get("type"),
                "parameters": node.get("parameters") or {},
                "credential_aliases": {
                    str(credential_type): str(reference.get("name") or "")
                    for credential_type, reference in (node.get("credentials") or {}).items()
                    if isinstance(reference, Mapping)
                },
            }
            if bool(node.get("disabled", False)) or not self._is_external_write(raw):
                continue
            for expression in self._expression_strings(raw["parameters"]):
                if self._direct_json_expression_field(expression) is None:
                    issues.append(
                        GraphIssue(
                            "EXTERNAL_EXPRESSION_UNREVIEWED",
                            "External writes may only use direct reviewed $json.field expressions.",
                            node=name,
                        )
                    )
            if name not in reachable:
                issues.append(
                    GraphIssue(
                        "EXTERNAL_WRITE_NOT_TRIGGERED",
                        "External write is not reachable from an enabled trigger.",
                        node=name,
                    )
                )
            direct_predecessors = predecessors.get(name, set())
            immediate_approvals = direct_predecessors & approval_names
            if len(immediate_approvals) != 1 or direct_predecessors != immediate_approvals:
                issues.append(
                    GraphIssue(
                        "EXTERNAL_WRITE_APPROVAL_BYPASS",
                        "Every path into an external write must pass directly through one approval gate.",
                        node=name,
                    )
                )
                continue
            approval_name = next(iter(immediate_approvals))
            if approval_name in used_approvals:
                issues.append(
                    GraphIssue(
                        "APPROVAL_ACTION_AMBIGUOUS",
                        "One approval gate cannot authorize more than one external write.",
                        node=approval_name,
                    )
                )
                continue
            used_approvals.add(approval_name)
            approval = by_name[approval_name]
            manifest = self._external_action_manifest(
                raw,
                node_id=str(node.get("id") or ""),
                approval_node_id=str(approval.get("id") or ""),
                issues=issues,
                node_label=name,
            )
            values = (
                (approval.get("parameters") or {})
                .get("workflowInputs", {})
                .get("value", {})
                if isinstance(approval.get("parameters") or {}, Mapping)
                else {}
            )
            if not isinstance(manifest, Mapping) or not isinstance(values, Mapping):
                continue
            expected_digest = _sha256_bytes(
                _canonical_json(manifest).encode("utf-8")
            )
            if (
                values.get("manifest_digest") != expected_digest
                or values.get("input") != self._approval_input_expression(manifest)
            ):
                issues.append(
                    GraphIssue(
                        "APPROVAL_ACTION_MANIFEST_MISMATCH",
                        "Approval authority no longer matches the downstream write.",
                        node=approval_name,
                    )
                )
        for approval_name in sorted(approval_names - used_approvals):
            issues.append(
                GraphIssue(
                    "APPROVAL_ACTION_BINDING_REQUIRED",
                    "The approval gate must directly precede one reviewed external write.",
                    "needs_input",
                    node=approval_name,
                )
            )

    def _validate_parameters(
        self, metadata: Mapping[str, Any], parameters: Mapping[str, Any], node_name: str, issues: list[GraphIssue]
    ) -> None:
        version = float(metadata.get("selected_version") or 0)
        known: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for prop in metadata.get("properties") or []:
            if isinstance(prop, Mapping) and prop.get("name"):
                known[str(prop["name"])].append(prop)
        for key, value in parameters.items():
            candidates = [prop for prop in known.get(str(key), []) if _property_is_visible(prop, parameters, version)]
            if not candidates:
                continue
            prop = candidates[0]
            expected = str(prop.get("type") or "")
            valid = True
            if expected == "boolean":
                valid = isinstance(value, bool)
            elif expected == "number":
                valid = isinstance(value, (int, float)) and not isinstance(value, bool)
            elif expected in {"collection", "fixedCollection", "resourceMapper"}:
                valid = isinstance(value, Mapping)
            elif expected in {"multiOptions"}:
                valid = isinstance(value, list)
            elif expected in {"string", "json", "color", "dateTime", "options", "workflowSelector", "credentialsSelect"}:
                valid = isinstance(value, (str, int, float, bool, Mapping)) and value is not None
            if not valid:
                issues.append(
                    GraphIssue(
                        "PARAMETER_TYPE_INVALID",
                        f"Parameter {key} has the wrong type for {expected}.",
                        node=node_name,
                        path=f"parameters.{key}",
                    )
                )
            if expected == "options" and prop.get("options"):
                allowed = {
                    option.get("value") for option in prop.get("options") if isinstance(option, Mapping) and "value" in option
                }
                if value not in allowed:
                    issues.append(
                        GraphIssue(
                            "PARAMETER_ENUM_INVALID",
                            f"Parameter {key} is not an allowed option.",
                            node=node_name,
                            path=f"parameters.{key}",
                        )
                    )
        for prop in metadata.get("properties") or []:
            if not isinstance(prop, Mapping) or not prop.get("required") or not prop.get("name"):
                continue
            if not _property_is_visible(prop, parameters, version):
                continue
            key = str(prop["name"])
            actual = parameters.get(key, prop.get("default"))
            if self._required_value_missing(actual, str(prop.get("type") or "")):
                issues.append(
                    GraphIssue(
                        "PARAMETER_REQUIRED",
                        f"Required parameter is missing: {key}",
                        "needs_input",
                        node=node_name,
                        path=f"parameters.{key}",
                    )
                )

    @staticmethod
    def _required_value_missing(value: Any, parameter_type: str) -> bool:
        if value is None or value == "" or value == [] or value == {}:
            return True
        if parameter_type == "resourceLocator":
            return not isinstance(value, Mapping) or GraphAuthoringEngine._required_value_missing(
                value.get("value"), "string"
            )
        if parameter_type in {"resourceMapper", "collection", "fixedCollection"}:
            if not isinstance(value, Mapping):
                return True
            if "value" in value:
                return GraphAuthoringEngine._required_value_missing(value.get("value"), "collection")
            return not any(
                not GraphAuthoringEngine._required_value_missing(child, "collection")
                for child in value.values()
            )
        return False

    def _validate_credentials(
        self,
        metadata: Mapping[str, Any],
        parameters: Mapping[str, Any],
        credentials: Any,
        node_name: str,
        issues: list[GraphIssue],
    ) -> None:
        if not isinstance(credentials, Mapping):
            issues.append(GraphIssue("NODE_CREDENTIALS_INVALID", "Node credentials must be an object.", node=node_name))
            return
        allowed = {
            str(item.get("name"))
            for item in metadata.get("credentials") or []
            if isinstance(item, Mapping) and item.get("name")
        }
        for key, value in credentials.items():
            if key not in allowed:
                issues.append(GraphIssue("CREDENTIAL_TYPE_INVALID", f"Credential type is not supported: {key}", node=node_name))
            if not isinstance(value, Mapping) or not str(value.get("id") or "").strip():
                issues.append(GraphIssue("CREDENTIAL_REFERENCE_INVALID", "Credential reference requires an opaque ID.", node=node_name))
        version = float(metadata.get("selected_version") or 0)
        for item in metadata.get("credentials") or []:
            if not isinstance(item, Mapping) or not item.get("required") or not item.get("name"):
                continue
            if not _property_is_visible(item, parameters, version):
                continue
            credential_type = str(item["name"])
            if credential_type not in credentials:
                issues.append(
                    GraphIssue(
                        "CREDENTIAL_REQUIRED",
                        f"Credential alias is required for {credential_type}.",
                        "needs_input",
                        node=node_name,
                    )
                )

    @staticmethod
    def _scan_expressions(value: Any, node_name: str, issues: list[GraphIssue], path: str = "parameters") -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                GraphAuthoringEngine._scan_expressions(child, node_name, issues, f"{path}.{key}")
            return
        if isinstance(value, list):
            for index, child in enumerate(value):
                GraphAuthoringEngine._scan_expressions(child, node_name, issues, f"{path}[{index}]")
            return
        if not isinstance(value, str) or not (value.startswith("=") or "{{" in value):
            return
        if len(value) > 10_000:
            issues.append(GraphIssue("EXPRESSION_TOO_LARGE", "Expression exceeds the reviewed size limit.", node=node_name, path=path))
        for code, pattern in _BANNED_EXPRESSION_PATTERNS:
            if pattern.search(value):
                issues.append(GraphIssue(code, "Expression uses a forbidden capability.", node=node_name, path=path))

    @staticmethod
    def _port_counts(metadata: Mapping[str, Any], parameters: Mapping[str, Any]) -> tuple[int | None, int | None]:
        short_name = str(metadata.get("name") or "")
        version = float(metadata.get("selected_version") or 0)
        if short_name == "if":
            return 1, 2
        if short_name == "switch":
            if version >= 3:
                if parameters.get("mode") == "expression":
                    try:
                        return 1, max(1, int(parameters.get("numberOutputs", 0)))
                    except (TypeError, ValueError):
                        return 1, None
                values = ((parameters.get("rules") or {}).get("values") or []) if isinstance(parameters.get("rules"), Mapping) else []
                if isinstance(values, list):
                    fallback = ((parameters.get("options") or {}).get("fallbackOutput") == "extra") if isinstance(parameters.get("options"), Mapping) else False
                    return 1, len(values) + (1 if fallback else 0)
            return 1, None
        if short_name == "merge":
            if version >= 3:
                try:
                    return max(2, int(parameters.get("numberInputs", 2))), 1
                except (TypeError, ValueError):
                    return None, 1
            return 2, 1
        if short_name == "splitInBatches":
            return 1, 2 if version >= 2 else 1
        return GraphAuthoringEngine._static_port_count(metadata.get("inputs")), GraphAuthoringEngine._static_port_count(metadata.get("outputs"))

    @staticmethod
    def _static_port_count(value: Any) -> int | None:
        if not isinstance(value, list):
            return None
        return len(value)

    @staticmethod
    def _edge_records(
        connections: Mapping[str, Any], issues: list[GraphIssue] | None = None
    ) -> list[tuple[str, str, int, str, str, int]]:
        result: list[tuple[str, str, int, str, str, int]] = []
        for source, typed_outputs in connections.items():
            if not isinstance(typed_outputs, Mapping):
                if issues is not None:
                    issues.append(GraphIssue("CONNECTION_SHAPE_INVALID", f"Connections for {source} must be an object."))
                continue
            for output_type, output_groups in typed_outputs.items():
                if not isinstance(output_groups, list):
                    if issues is not None:
                        issues.append(GraphIssue("CONNECTION_SHAPE_INVALID", f"Output groups for {source} must be a list."))
                    continue
                for output_index, targets in enumerate(output_groups):
                    if not isinstance(targets, list):
                        if issues is not None:
                            issues.append(GraphIssue("CONNECTION_SHAPE_INVALID", "Connection target group must be a list."))
                        continue
                    for target in targets:
                        if not isinstance(target, Mapping):
                            if issues is not None:
                                issues.append(GraphIssue("CONNECTION_TARGET_INVALID", "Connection target must be an object."))
                            continue
                        try:
                            input_index = int(target.get("index", 0))
                        except (TypeError, ValueError):
                            input_index = -1
                        result.append(
                            (
                                str(source),
                                str(output_type),
                                output_index,
                                str(target.get("node") or ""),
                                str(target.get("type") or "main"),
                                input_index,
                            )
                        )
        return result

    @staticmethod
    def _validate_connected(
        by_name: Mapping[str, Any], adjacency: Mapping[str, list[str]], issues: list[GraphIssue]
    ) -> None:
        undirected: dict[str, set[str]] = defaultdict(set)
        for source, targets in adjacency.items():
            for target in targets:
                undirected[source].add(target)
                undirected[target].add(source)
        if not by_name:
            return
        start = next(iter(by_name))
        visited = {start}
        queue = deque([start])
        while queue:
            current = queue.popleft()
            for target in undirected[current]:
                if target not in visited:
                    visited.add(target)
                    queue.append(target)
        missing = sorted(set(by_name) - visited)
        if missing:
            issues.append(GraphIssue("GRAPH_DISCONNECTED", "Workflow contains disconnected components.", details={"nodes": missing}))

    @staticmethod
    def _validate_cycles(
        by_name: Mapping[str, Mapping[str, Any]],
        adjacency: Mapping[str, list[str]],
        edges: Sequence[tuple[str, str, int, str, str, int]],
        issues: list[GraphIssue],
    ) -> None:
        index = 0
        indexes: dict[str, int] = {}
        lowlinks: dict[str, int] = {}
        stack: list[str] = []
        on_stack: set[str] = set()
        components: list[list[str]] = []

        def visit(node: str) -> None:
            nonlocal index
            indexes[node] = lowlinks[node] = index
            index += 1
            stack.append(node)
            on_stack.add(node)
            for target in adjacency.get(node, []):
                if target not in indexes:
                    visit(target)
                    lowlinks[node] = min(lowlinks[node], lowlinks[target])
                elif target in on_stack:
                    lowlinks[node] = min(lowlinks[node], indexes[target])
            if lowlinks[node] == indexes[node]:
                component: list[str] = []
                while True:
                    current = stack.pop()
                    on_stack.remove(current)
                    component.append(current)
                    if current == node:
                        break
                components.append(component)

        for node in by_name:
            if node not in indexes:
                visit(node)
        self_loops = {source for source, _, _, target, _, _ in edges if source == target}
        for component in components:
            if len(component) == 1 and component[0] not in self_loops:
                continue
            loop_nodes = [
                name
                for name in component
                if str(by_name[name].get("type") or "").endswith(".splitInBatches")
            ]
            if len(loop_nodes) != 1:
                issues.append(GraphIssue("GRAPH_CYCLE_UNREVIEWED", "Cycles are only allowed through one reviewed Loop Over Items node.", details={"nodes": sorted(component)}))
                continue
            loop_name = loop_nodes[0]
            has_body_edge = any(source == loop_name and output_index == 1 and target in component for source, _, output_index, target, _, _ in edges)
            has_return = any(source in component and target == loop_name and input_index == 0 for source, _, _, target, _, input_index in edges)
            if not (has_body_edge and has_return):
                issues.append(GraphIssue("GRAPH_LOOP_SHAPE_INVALID", "Loop cycle does not match the reviewed Loop Over Items pattern.", node=loop_name))

    def apply_patch(
        self,
        base_workflow: Mapping[str, Any],
        patch: Mapping[str, Any] | Sequence[Mapping[str, Any]],
        *,
        context: Mapping[str, Any] | None = None,
    ) -> GraphResult:
        workflow = self._bounded_workflow_copy(base_workflow)
        if isinstance(patch, Mapping):
            if patch.get("schema") != PATCH_SCHEMA:
                raise GraphAuthoringError("N8N_PATCH_SCHEMA_INVALID", f"Expected {PATCH_SCHEMA}.")
            operations = patch.get("operations")
        else:
            operations = patch
        if not isinstance(operations, Sequence) or isinstance(operations, (str, bytes)):
            raise GraphAuthoringError("N8N_PATCH_INVALID", "Patch operations must be a list.")
        issues: list[GraphIssue] = []
        authoritative_context = copy.deepcopy(dict(context or {}))
        revision_token = self._workflow_revision_token(workflow) or self._mint_revision_token()
        authoritative_context["_workbench_revision_token"] = revision_token
        authoritative_context["_patch_deferred_approval"] = True
        binding_cache: dict[str, Any] = {}
        for index, operation in enumerate(operations):
            if not isinstance(operation, Mapping):
                issues.append(GraphIssue("PATCH_OPERATION_INVALID", "Patch operation must be an object.", path=f"operations[{index}]"))
                continue
            self._apply_one_patch(
                workflow, operation, index, issues,
                authoritative_context, binding_cache,
            )
        self._bind_pending_patch_approvals(
            workflow, issues, authoritative_context, binding_cache
        )
        validation = self.validate(workflow)
        issues.extend(validation.issues)
        result = self._result(workflow, issues, base_workflow=base_workflow)
        private_binding_claims = self._binding_claims_from_cache(
            workflow_name=str(workflow.get("name") or ""),
            binding_cache=binding_cache,
            workflow_revision=revision_token,
        )
        if private_binding_claims:
            result.binding_claims = private_binding_claims
        return result

    def _bind_pending_patch_approvals(
        self,
        workflow: dict[str, Any],
        issues: list[GraphIssue],
        context: Mapping[str, Any],
        binding_cache: dict[str, Any],
    ) -> None:
        """Bind Approval nodes only after a multi-op patch has its final graph."""

        nodes = {
            str(node.get("name") or ""): node
            for node in workflow.get("nodes") or []
            if isinstance(node, dict)
        }
        adjacency: dict[str, list[str]] = defaultdict(list)
        for source, _, _, target, _, _ in self._edge_records(
            workflow.get("connections") or {}
        ):
            adjacency[source].append(target)
        for approval_name, approval in nodes.items():
            if self._protected_node_kind(approval) != "workbench.approval":
                continue
            parameters = approval.get("parameters") or {}
            inputs = parameters.get("workflowInputs") if isinstance(parameters, Mapping) else None
            values = inputs.get("value") if isinstance(inputs, Mapping) else None
            if isinstance(values, Mapping) and _SHA256_RE.fullmatch(
                str(values.get("manifest_digest") or "")
            ):
                continue
            downstream = [
                nodes[name]
                for name in adjacency.get(approval_name, [])
                if name in nodes
            ]
            external = []
            for candidate in downstream:
                raw = {
                    "type": candidate.get("type"),
                    "parameters": candidate.get("parameters") or {},
                    "credential_aliases": {
                        str(credential_type): str(reference.get("name") or "")
                        for credential_type, reference in (candidate.get("credentials") or {}).items()
                        if isinstance(reference, Mapping)
                    },
                }
                if self._is_external_write(raw):
                    external.append((candidate, raw))
            if len(external) != 1 or len(downstream) != 1:
                issues.append(
                    GraphIssue(
                        "APPROVAL_ACTION_BINDING_REQUIRED",
                        "A patched approval gate must directly precede one external write.",
                        "needs_input",
                        node=approval_name,
                    )
                )
                continue
            target, target_raw = external[0]
            manifest = self._external_action_manifest(
                target_raw,
                node_id=str(target.get("id") or ""),
                approval_node_id=str(approval.get("id") or ""),
                issues=issues,
                node_label=str(target.get("name") or ""),
            )
            if manifest is None or not isinstance(values, dict):
                continue
            node_key = str(approval.get("id") or "")
            resolver_context = copy.deepcopy(dict(context))
            resolver_context["_approval_action_manifests"] = {node_key: manifest}
            resolved = self._resolve_binding(
                "workbench.approval",
                node_key,
                {"key": node_key, "id": node_key, "type": "workbench.approval"},
                resolver_context,
                binding_cache,
            )
            binding_id = (
                str(resolved.get("approval_binding_id") or "").strip()
                if isinstance(resolved, Mapping)
                else "wba_" + uuid.uuid5(
                    _UUID_NAMESPACE, f"approval\x00{node_key}\x00{node_key}"
                ).hex
            )
            digest = (
                str(resolved.get("manifest_digest") or "").strip()
                if isinstance(resolved, Mapping)
                else _sha256_bytes(_canonical_json(manifest).encode("utf-8"))
            )
            if not binding_id or not _SHA256_RE.fullmatch(digest):
                issues.append(
                    GraphIssue(
                        "APPROVAL_ACTION_BINDING_REQUIRED",
                        "The patched approval action could not be bound.",
                        "needs_input",
                        node=approval_name,
                    )
                )
                continue
            values["approval_binding_id"] = binding_id
            values["manifest_digest"] = digest
            values["approval_token"] = f"{binding_id}:{digest}"
            values["input"] = self._approval_input_expression(manifest)

    def _apply_one_patch(
        self,
        workflow: dict[str, Any],
        operation: Mapping[str, Any],
        index: int,
        issues: list[GraphIssue],
        context: Mapping[str, Any],
        binding_cache: dict[str, Any],
    ) -> None:
        op = str(operation.get("op") or "").strip().lower()
        nodes: list[dict[str, Any]] = workflow.setdefault("nodes", [])
        target = str(operation.get("target") or operation.get("node") or "")

        def locate() -> dict[str, Any] | None:
            return next((node for node in nodes if str(node.get("id")) == target or str(node.get("name")) == target), None)

        if op == "add":
            raw = operation.get("value") or operation.get("node")
            if not isinstance(raw, Mapping):
                issues.append(GraphIssue("PATCH_ADD_INVALID", "Add requires a node value.", path=f"operations[{index}]"))
                return
            # Planner patches are semantic.  Compile a single node through the
            # same catalog/binding/credential boundary used for new graphs;
            # raw n8n ids, typeVersion and credential ids are never trusted.
            if raw.get("key") and raw.get("type") and "typeVersion" not in raw:
                patch_spec = {
                    "schema": SPEC_SCHEMA,
                    "name": str(workflow.get("name") or "Patched Workflow"),
                    "nodes": [copy.deepcopy(dict(raw))],
                    "edges": [],
                }
                compiled = self._compile_spec(
                    patch_spec, issues, context, binding_cache
                ).get("nodes", [])
                if not compiled:
                    issues.append(
                        GraphIssue(
                            "PATCH_ADD_INVALID",
                            "Semantic node could not be compiled.",
                            path=f"operations[{index}]",
                        )
                    )
                    return
                node = compiled[0]
            else:
                issues.append(
                    GraphIssue(
                        "PATCH_RAW_NODE_FORBIDDEN",
                        "Patch add must contain a semantic node.",
                        path=f"operations[{index}]",
                    )
                )
                return
            name = str(node.get("name") or node.get("type") or "Node")
            existing = {str(value.get("name")) for value in nodes}
            candidate, suffix = name, 2
            while candidate in existing:
                candidate, suffix = f"{name} {suffix}", suffix + 1
            node["name"] = candidate
            node.setdefault("id", str(uuid.uuid5(_UUID_NAMESPACE, f"{self.workflow_digest(workflow)}\x00{candidate}")))
            node.setdefault("parameters", {})
            node.setdefault("position", [240 + len(nodes) * 260, 180])
            nodes.append(node)
            return
        node = locate() if target else None
        if op in {"update", "move", "rename", "remove"} and node is None:
            issues.append(GraphIssue("PATCH_TARGET_UNKNOWN", f"Patch target was not found: {target}", path=f"operations[{index}]"))
            return
        if op == "update":
            changes = operation.get("changes")
            if not isinstance(changes, Mapping):
                issues.append(GraphIssue("PATCH_UPDATE_INVALID", "Update requires changes.", path=f"operations[{index}]"))
                return
            protected_kind = self._protected_node_kind(node)
            allowed = {"parameters", "disabled", "notes", "credential_aliases"}
            for key, value in changes.items():
                if key not in allowed:
                    issues.append(GraphIssue("PATCH_FIELD_FORBIDDEN", f"Patch cannot update field: {key}", path=f"operations[{index}]"))
                elif protected_kind and key in {"parameters", "credential_aliases"}:
                    issues.append(
                        GraphIssue(
                            "PATCH_PROTECTED_IDENTITY_IMMUTABLE",
                            "Protected Agent/Approval identity fields cannot be patched.",
                            node=str(node.get("name") or target),
                            path=f"operations[{index}].changes.{key}",
                        )
                    )
                elif key == "parameters" and isinstance(value, Mapping):
                    current = node.setdefault("parameters", {})
                    if isinstance(current, dict):
                        current.update(copy.deepcopy(dict(value)))
                elif key == "credential_aliases":
                    if not isinstance(value, Mapping):
                        issues.append(
                            GraphIssue(
                                "CREDENTIAL_ALIASES_INVALID",
                                "Credential aliases must be an object.",
                                "needs_input",
                                node=str(node.get("name") or target),
                            )
                        )
                    else:
                        semantic = {"credential_aliases": copy.deepcopy(dict(value))}
                        node.pop("credentials", None)
                        self._resolve_credentials(
                            node, semantic, issues,
                            str(node.get("id") or node.get("name") or target),
                            context,
                        )
                else:
                    node[key] = copy.deepcopy(value)
            return
        if op == "move":
            position = operation.get("position")
            if not (isinstance(position, list) and len(position) == 2 and all(isinstance(value, (int, float)) for value in position)):
                issues.append(GraphIssue("PATCH_MOVE_INVALID", "Move requires a numeric [x,y] position.", path=f"operations[{index}]"))
            else:
                node["position"] = [int(position[0]), int(position[1])]
            return
        if op == "rename":
            new_name = str(operation.get("name") or "").strip()
            if not new_name or any(value is not node and value.get("name") == new_name for value in nodes):
                issues.append(GraphIssue("PATCH_RENAME_INVALID", "Rename requires a unique non-empty name.", path=f"operations[{index}]"))
                return
            old_name = str(node.get("name"))
            node["name"] = new_name
            connections = workflow.setdefault("connections", {})
            if old_name in connections:
                connections[new_name] = connections.pop(old_name)
            for _, _, _, target_item, _, _ in list(self._edge_records(connections)):
                del target_item
            for outputs in connections.values():
                if not isinstance(outputs, Mapping):
                    continue
                for groups in outputs.values():
                    if not isinstance(groups, list):
                        continue
                    for targets in groups:
                        if isinstance(targets, list):
                            for value in targets:
                                if isinstance(value, dict) and value.get("node") == old_name:
                                    value["node"] = new_name
            return
        if op == "remove":
            old_name = str(node.get("name"))
            nodes.remove(node)
            connections = workflow.setdefault("connections", {})
            connections.pop(old_name, None)
            for outputs in connections.values():
                if not isinstance(outputs, Mapping):
                    continue
                for groups in outputs.values():
                    if isinstance(groups, list):
                        for targets in groups:
                            if isinstance(targets, list):
                                targets[:] = [value for value in targets if not isinstance(value, Mapping) or value.get("node") != old_name]
            return
        if op in {"connect", "disconnect"}:
            source = str(operation.get("from") or "")
            target_name = str(operation.get("to") or "")
            source_node = next((node for node in nodes if node.get("id") == source or node.get("name") == source), None)
            target_node = next((node for node in nodes if node.get("id") == target_name or node.get("name") == target_name), None)
            if source_node is None or target_node is None:
                issues.append(GraphIssue("PATCH_EDGE_NODE_UNKNOWN", "Connect/disconnect node was not found.", path=f"operations[{index}]"))
                return
            output_type, input_type = str(operation.get("output") or "main"), str(operation.get("input") or "main")
            try:
                output_index, input_index = int(operation.get("output_index", 0)), int(operation.get("input_index", 0))
            except (TypeError, ValueError):
                issues.append(GraphIssue("PATCH_EDGE_PORT_INVALID", "Connect/disconnect ports must be integers.", path=f"operations[{index}]"))
                return
            outputs = workflow.setdefault("connections", {}).setdefault(source_node["name"], {}).setdefault(output_type, [])
            while len(outputs) <= output_index:
                outputs.append([])
            edge = {"node": target_node["name"], "type": input_type, "index": input_index}
            if op == "connect":
                if edge not in outputs[output_index]:
                    outputs[output_index].append(edge)
            else:
                outputs[output_index][:] = [value for value in outputs[output_index] if value != edge]
            return
        issues.append(GraphIssue("PATCH_OPERATION_UNKNOWN", f"Unknown patch operation: {op}", path=f"operations[{index}]"))

    def preview(self, workflow: Mapping[str, Any]) -> dict[str, Any]:
        nodes = workflow.get("nodes") or []
        edges = self._edge_records(workflow.get("connections") or {}) if isinstance(workflow.get("connections") or {}, Mapping) else []
        return {
            "name": str(workflow.get("name") or ""),
            "node_count": len(nodes) if isinstance(nodes, list) else 0,
            "edge_count": len(edges),
            "nodes": [
                {
                    "id": str(node.get("id") or ""),
                    "name": str(node.get("name") or ""),
                    "type": str(node.get("type") or ""),
                    "type_version": node.get("typeVersion"),
                    "position": copy.deepcopy(node.get("position")),
                    "disabled": bool(node.get("disabled", False)),
                    "credential_aliases": sorted(
                        str(value.get("name"))
                        for value in (node.get("credentials") or {}).values()
                        if isinstance(value, Mapping) and value.get("name")
                    ),
                }
                for node in nodes
                if isinstance(node, Mapping)
            ][:MAX_NODES],
            "edges": [
                {
                    "from": source,
                    "to": target,
                    "output": output_type,
                    "output_index": output_index,
                    "input": input_type,
                    "input_index": input_index,
                }
                for source, output_type, output_index, target, input_type, input_index in edges[:MAX_EDGES]
            ],
        }

    def diff(self, before: Mapping[str, Any] | None, after: Mapping[str, Any]) -> dict[str, Any]:
        before = before or {"nodes": [], "connections": {}}
        old_nodes = {str(node.get("id") or node.get("name")): node for node in before.get("nodes") or [] if isinstance(node, Mapping)}
        new_nodes = {str(node.get("id") or node.get("name")): node for node in after.get("nodes") or [] if isinstance(node, Mapping)}
        added = [self._node_fact(new_nodes[key]) for key in sorted(set(new_nodes) - set(old_nodes))]
        removed = [self._node_fact(old_nodes[key]) for key in sorted(set(old_nodes) - set(new_nodes))]
        changed: list[dict[str, Any]] = []
        for key in sorted(set(old_nodes) & set(new_nodes)):
            old, new = old_nodes[key], new_nodes[key]
            fields: dict[str, Any] = {}
            for field_name in ("name", "type", "typeVersion", "disabled", "position"):
                if old.get(field_name) != new.get(field_name):
                    fields[field_name] = {"before": copy.deepcopy(old.get(field_name)), "after": copy.deepcopy(new.get(field_name))}
            old_aliases = self._credential_aliases(old.get("credentials"))
            new_aliases = self._credential_aliases(new.get("credentials"))
            if old_aliases != new_aliases:
                fields["credential_aliases"] = {"before": old_aliases, "after": new_aliases}
            parameter_changes = self._mapping_diff(old.get("parameters") or {}, new.get("parameters") or {})
            if parameter_changes:
                fields["parameters"] = parameter_changes
            if fields:
                changed.append({"id": key, "name": str(new.get("name") or old.get("name") or ""), "changes": fields})
        old_edges = set(self._edge_records(before.get("connections") or {}))
        new_edges = set(self._edge_records(after.get("connections") or {}))
        return {
            "nodes": {"added": added, "removed": removed, "changed": changed},
            "connections": {
                "added": [self._edge_fact(value) for value in sorted(new_edges - old_edges)],
                "removed": [self._edge_fact(value) for value in sorted(old_edges - new_edges)],
            },
            "before_digest": self.workflow_digest(before),
            "after_digest": self.workflow_digest(after),
        }

    @staticmethod
    def _mapping_diff(before: Any, after: Any, prefix: str = "") -> list[dict[str, Any]]:
        if not isinstance(before, Mapping) or not isinstance(after, Mapping):
            return [] if before == after else [GraphAuthoringEngine._redacted_change(prefix or "$", True, before, True, after)]
        changes: list[dict[str, Any]] = []
        for key in sorted(set(before) | set(after), key=str):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in before:
                changes.append(GraphAuthoringEngine._redacted_change(path, False, None, True, after[key]))
            elif key not in after:
                changes.append(GraphAuthoringEngine._redacted_change(path, True, before[key], False, None))
            elif isinstance(before[key], Mapping) and isinstance(after[key], Mapping):
                changes.extend(GraphAuthoringEngine._mapping_diff(before[key], after[key], path))
            elif before[key] != after[key]:
                changes.append(GraphAuthoringEngine._redacted_change(path, True, before[key], True, after[key]))
        return changes

    @staticmethod
    def _redacted_change(path: str, before_present: bool, before: Any, after_present: bool, after: Any) -> dict[str, Any]:
        return {
            "path": path,
            "before_present": before_present,
            "after_present": after_present,
            "before_digest": _sha256_bytes(_canonical_json(before).encode("utf-8")) if before_present else None,
            "after_digest": _sha256_bytes(_canonical_json(after).encode("utf-8")) if after_present else None,
        }

    @staticmethod
    def _credential_aliases(value: Any) -> list[dict[str, str]]:
        if not isinstance(value, Mapping):
            return []
        return sorted(
            [
                {"type": str(credential_type), "alias": str(reference.get("name"))}
                for credential_type, reference in value.items()
                if isinstance(reference, Mapping) and reference.get("name")
            ],
            key=lambda item: (item["type"], item["alias"]),
        )

    @staticmethod
    def _node_fact(node: Mapping[str, Any]) -> dict[str, Any]:
        return {"id": str(node.get("id") or ""), "name": str(node.get("name") or ""), "type": str(node.get("type") or ""), "type_version": node.get("typeVersion")}

    @staticmethod
    def _edge_fact(value: tuple[str, str, int, str, str, int]) -> dict[str, Any]:
        return {"from": value[0], "output": value[1], "output_index": value[2], "to": value[3], "input": value[4], "input_index": value[5]}

    def _result(
        self,
        workflow: dict[str, Any],
        issues: list[GraphIssue],
        *,
        base_workflow: Mapping[str, Any] | None,
    ) -> GraphResult:
        deduped = self._dedupe_issues(issues)
        validation_status = self._status_for(deduped)
        status = "graph_ready" if validation_status == "ready" else validation_status
        graph_digest = self.workflow_digest(workflow) if validation_status == "ready" else None
        return GraphResult(
            status=status,
            workflow=copy.deepcopy(workflow),
            graph_preview=self.preview(workflow),
            validation_status=validation_status,
            catalog_digest=self.catalog.digest,
            graph_digest=graph_digest,
            issues=deduped,
            diff=self.diff(base_workflow, workflow),
            binding_claims=self._binding_claims(workflow),
        )

    def _binding_claims(self, workflow: Mapping[str, Any]) -> list[dict[str, Any]]:
        workflow_ids: dict[str, str] = {}
        for kind, raw in self.protected_workflows.items():
            if isinstance(raw, str):
                workflow_ids[raw] = kind
            elif isinstance(raw, Mapping) and raw.get("workflow_id"):
                workflow_ids[str(raw["workflow_id"])] = kind
        claims: list[dict[str, Any]] = []
        for node in workflow.get("nodes") or []:
            if not isinstance(node, Mapping):
                continue
            parameters = node.get("parameters") or {}
            if not isinstance(parameters, Mapping):
                continue
            selector = parameters.get("workflowId") or {}
            workflow_id = str(selector.get("value") or "") if isinstance(selector, Mapping) else str(selector or "")
            kind = workflow_ids.get(workflow_id)
            inputs = parameters.get("workflowInputs") or {}
            values = inputs.get("value") or {} if isinstance(inputs, Mapping) else {}
            if not kind or not isinstance(values, Mapping):
                continue
            binding_id = next((str(value) for key, value in values.items() if str(key).endswith("binding_id") and value), "")
            if binding_id and kind == "workbench.agent":
                workflow_revision = str(values.get("workflow_revision") or "").strip()
                claims.append(
                    {
                        "kind": kind,
                        "binding_id": binding_id,
                        "node_id": str(node.get("id") or ""),
                        "node_name": str(node.get("name") or ""),
                        "workflow_revision": workflow_revision,
                        "provisional": True,
                    }
                )
        return claims

    def _binding_claims_from_cache(
        self,
        *,
        workflow_name: str,
        binding_cache: Mapping[str, Any],
        workflow_revision: str,
    ) -> list[dict[str, Any]]:
        """Return one-time claims without leaking them into n8n parameters."""

        claims: list[dict[str, Any]] = []
        for cache_key, raw in binding_cache.items():
            if not isinstance(raw, Mapping):
                continue
            claim_id = str(raw.get("binding_claim_id") or "").strip()
            binding_id = str(
                raw.get("agent_binding_id")
                or raw.get("approval_binding_id")
                or raw.get("binding_id")
                or raw.get("id")
                or ""
            ).strip()
            if not claim_id or not binding_id:
                continue
            kind, _, key = str(cache_key).partition("\x00")
            if kind not in _RESERVED_TYPES or not key:
                continue
            claim = {
                    "kind": kind,
                    "binding_claim_id": claim_id,
                    "binding_id": binding_id,
                    "node_id": str(
                        raw.get("node_id")
                        or uuid.uuid5(_UUID_NAMESPACE, f"{workflow_name}\x00{key}")
                    ),
                    "workflow_revision": workflow_revision,
                    "provisional": True,
                }
            if kind == "workbench.approval" and _SHA256_RE.fullmatch(
                str(raw.get("manifest_digest") or "")
            ):
                claim["manifest_digest"] = str(raw["manifest_digest"])
            claims.append(claim)
        return claims

    @staticmethod
    def _status_for(issues: Sequence[GraphIssue]) -> str:
        if any(issue.severity == "blocked" for issue in issues):
            return "blocked"
        if any(issue.severity == "needs_input" for issue in issues):
            return "needs_input"
        return "ready"

    @staticmethod
    def _dedupe_issues(issues: Sequence[GraphIssue]) -> list[GraphIssue]:
        seen: set[str] = set()
        result: list[GraphIssue] = []
        for issue in issues:
            key = _canonical_json(issue.to_dict())
            if key not in seen:
                seen.add(key)
                result.append(issue)
        return result

    @staticmethod
    def _bounded_workflow_copy(workflow: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(workflow, Mapping):
            raise GraphAuthoringError("GRAPH_SHAPE_INVALID", "Workflow must be an object.")
        try:
            encoded = _canonical_json(workflow).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise GraphAuthoringError("GRAPH_NOT_JSON", "Workflow must contain only JSON values.") from exc
        if len(encoded) > MAX_WORKFLOW_BYTES:
            raise GraphAuthoringError("GRAPH_SIZE_LIMIT", f"Workflow exceeds {MAX_WORKFLOW_BYTES} bytes.")
        return copy.deepcopy(dict(workflow))


class LazyGraphAuthoringEngine:
    """Graph authoring facade that does not read the 17 MB catalog at startup."""

    def __init__(
        self,
        catalog_loader: LazyNodeCatalog | None = None,
        *,
        credential_resolver: CredentialResolver | None = None,
        protected_workflows: Mapping[str, Any] | None = None,
        binding_resolver: BindingResolver | None = None,
        revision_token_factory: Callable[[], str] | None = None,
    ):
        self.catalog_loader = catalog_loader or LazyNodeCatalog()
        self.credential_resolver = credential_resolver
        self.protected_workflows = copy.deepcopy(dict(protected_workflows or {}))
        self.binding_resolver = binding_resolver
        self.revision_token_factory = revision_token_factory
        self._engine: GraphAuthoringEngine | None = None

    @property
    def catalog(self) -> NodeCatalog:
        return self.catalog_loader.require()

    def status(self, *, probe: bool = False) -> dict[str, Any]:
        return self.catalog_loader.status(probe=probe)

    def require(self) -> GraphAuthoringEngine:
        catalog = self.catalog
        if self._engine is None or self._engine.catalog is not catalog:
            self._engine = GraphAuthoringEngine(
                catalog,
                credential_resolver=self.credential_resolver,
                protected_workflows=self.protected_workflows,
                binding_resolver=self.binding_resolver,
                revision_token_factory=self.revision_token_factory,
            )
        return self._engine

    def materialize(self, *args: Any, **kwargs: Any) -> GraphResult:
        return self.require().materialize(*args, **kwargs)

    def adopt(self, *args: Any, **kwargs: Any) -> GraphResult:
        return self.require().adopt(*args, **kwargs)

    def validate(self, *args: Any, **kwargs: Any) -> ValidationResult:
        return self.require().validate(*args, **kwargs)

    def apply_patch(self, *args: Any, **kwargs: Any) -> GraphResult:
        return self.require().apply_patch(*args, **kwargs)

    def preview(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.require().preview(*args, **kwargs)

    def diff(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.require().diff(*args, **kwargs)

    def workflow_digest(self, *args: Any, **kwargs: Any) -> str:
        return self.require().workflow_digest(*args, **kwargs)


__all__ = [
    "GraphAuthoringEngine",
    "GraphAuthoringError",
    "GraphIssue",
    "GraphResult",
    "LazyGraphAuthoringEngine",
    "LazyNodeCatalog",
    "NodeCatalog",
    "ValidationResult",
    "MAX_EDGES",
    "MAX_NODES",
    "MAX_WORKFLOW_BYTES",
    "PATCH_SCHEMA",
    "DEFAULT_N8N_RUNTIME_ROOT",
    "PINNED_N8N_VERSION",
    "PINNED_NODES_BASE_VERSION",
    "SPEC_SCHEMA",
]
