"""Governed local OAuth connectors for GitHub and Notion.

The service owns provider HTTP traffic, token refresh, project/resource scope
checks and normalized tool metadata.  Access tokens never leave this module.
Callers must still route write tools through the host approval dispatcher and
pass ``approved=True`` only after a single-use approval has been consumed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import quote, urlencode, urlsplit

import requests

from connector_secrets import ConnectorSecretError, ConnectorSecretStore
from connector_store import (
    ConnectorConflictError,
    ConnectorNotFoundError,
    ConnectorStore,
    ConnectorStoreError,
    normalize_connector_id,
)


GITHUB_API = "https://api.github.com"
GITHUB_AUTHORIZE = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN = "https://github.com/login/oauth/access_token"
NOTION_API = "https://api.notion.com/v1"
NOTION_AUTHORIZE = "https://api.notion.com/v1/oauth/authorize"
NOTION_TOKEN = "https://api.notion.com/v1/oauth/token"
NOTION_VERSION = "2022-06-28"
HTTP_TIMEOUT = (5, 30)

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_NOTION_ID = re.compile(r"^[A-Fa-f0-9-]{16,64}$")
_SAFE_REF = re.compile(r"^[A-Za-z0-9._/@+-]{1,255}$")
_WRITE_TOOLS = {
    "github.create_issue",
    "github.update_issue",
    "github.add_issue_comment",
    "notion.create_page",
    "notion.update_page",
    "notion.append_blocks",
}


CONNECTOR_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "github",
        "extension_id": "connector.github",
        "name": "GitHub",
        "description": "Read repositories and collaborate through Issues and PR conversations.",
        "auth_mode": "github_app_oauth2_pkce",
        "callback_path": "/oauth/callback/github",
        "resource_types": ["repository"],
        "capabilities": [
            "repository.read",
            "issue.read",
            "issue.write",
            "pull_request.read",
            "pull_request.comment",
            "checks.read",
        ],
        "permissions": [
            {"name": "metadata", "access": "read"},
            {"name": "contents", "access": "read"},
            {"name": "pull_requests", "access": "read"},
            {"name": "checks", "access": "read"},
            {"name": "issues", "access": "write"},
        ],
    },
    {
        "id": "notion",
        "extension_id": "connector.notion",
        "name": "Notion",
        "description": "Read selected knowledge roots and create or update pages after approval.",
        "auth_mode": "oauth2",
        "callback_path": "/oauth/callback/notion",
        "resource_types": ["page", "database"],
        "capabilities": ["content.read", "content.insert", "content.update"],
        "permissions": [
            {"name": "content", "access": "read"},
            {"name": "content", "access": "insert"},
            {"name": "content", "access": "update"},
        ],
    },
)


def _object_schema(properties: Mapping[str, Any], required: Sequence[str] = ()) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": "object",
        "properties": dict(properties),
        "additionalProperties": False,
    }
    if required:
        result["required"] = list(required)
    return result


def _tool(
    name: str,
    description: str,
    properties: Mapping[str, Any],
    required: Sequence[str] = (),
    *,
    write: bool = False,
) -> dict[str, Any]:
    connector_id = name.split(".", 1)[0]
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": _object_schema(properties, required),
        },
        "metadata": {
            "connector_id": connector_id,
            "extension_id": f"connector.{connector_id}",
            "risk": "write" if write else "read",
            "approval_required": write,
        },
    }


_CONNECTION_PROPERTY = {
    "type": "string",
    "description": "Connection ID. Required only when this project has multiple accounts for the connector.",
}
_REPO_PROPERTY = {"type": "string", "description": "Allowed repository in owner/name form."}
_NOTION_PAGE_PROPERTY = {"type": "string", "description": "A page ID within an allowed Notion root."}

TOOL_DEFINITIONS: tuple[dict[str, Any], ...] = (
    _tool("github.list_repositories", "List repositories allowed for this project.", {"connection_id": _CONNECTION_PROPERTY}),
    _tool(
        "github.read_file",
        "Read a text file from an allowed repository.",
        {
            "connection_id": _CONNECTION_PROPERTY,
            "repository": _REPO_PROPERTY,
            "path": {"type": "string"},
            "ref": {"type": "string"},
        },
        ("repository", "path"),
    ),
    _tool(
        "github.list_commits",
        "List recent commits in an allowed repository.",
        {"connection_id": _CONNECTION_PROPERTY, "repository": _REPO_PROPERTY, "ref": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
        ("repository",),
    ),
    _tool(
        "github.list_issues",
        "List issues in an allowed repository.",
        {"connection_id": _CONNECTION_PROPERTY, "repository": _REPO_PROPERTY, "state": {"type": "string", "enum": ["open", "closed", "all"]}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
        ("repository",),
    ),
    _tool(
        "github.get_issue",
        "Read one issue from an allowed repository.",
        {"connection_id": _CONNECTION_PROPERTY, "repository": _REPO_PROPERTY, "issue_number": {"type": "integer", "minimum": 1}},
        ("repository", "issue_number"),
    ),
    _tool(
        "github.list_pull_requests",
        "List pull requests in an allowed repository.",
        {"connection_id": _CONNECTION_PROPERTY, "repository": _REPO_PROPERTY, "state": {"type": "string", "enum": ["open", "closed", "all"]}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
        ("repository",),
    ),
    _tool(
        "github.get_pull_request",
        "Read one pull request from an allowed repository.",
        {"connection_id": _CONNECTION_PROPERTY, "repository": _REPO_PROPERTY, "pull_number": {"type": "integer", "minimum": 1}},
        ("repository", "pull_number"),
    ),
    _tool(
        "github.get_check_runs",
        "Read check runs for a commit or branch in an allowed repository.",
        {"connection_id": _CONNECTION_PROPERTY, "repository": _REPO_PROPERTY, "ref": {"type": "string"}},
        ("repository", "ref"),
    ),
    _tool(
        "github.create_issue",
        "Create an issue after explicit approval.",
        {"connection_id": _CONNECTION_PROPERTY, "repository": _REPO_PROPERTY, "title": {"type": "string"}, "body": {"type": "string"}},
        ("repository", "title"),
        write=True,
    ),
    _tool(
        "github.update_issue",
        "Update an issue title, body or state after explicit approval.",
        {"connection_id": _CONNECTION_PROPERTY, "repository": _REPO_PROPERTY, "issue_number": {"type": "integer", "minimum": 1}, "title": {"type": "string"}, "body": {"type": "string"}, "state": {"type": "string", "enum": ["open", "closed"]}},
        ("repository", "issue_number"),
        write=True,
    ),
    _tool(
        "github.add_issue_comment",
        "Add a general Issue or PR conversation comment after explicit approval.",
        {"connection_id": _CONNECTION_PROPERTY, "repository": _REPO_PROPERTY, "issue_number": {"type": "integer", "minimum": 1}, "body": {"type": "string"}},
        ("repository", "issue_number", "body"),
        write=True,
    ),
    _tool("notion.search", "List allowed Notion page and database roots for this project.", {"connection_id": _CONNECTION_PROPERTY, "query": {"type": "string"}}),
    _tool(
        "notion.retrieve_page",
        "Read a Notion page within an allowed root.",
        {"connection_id": _CONNECTION_PROPERTY, "page_id": _NOTION_PAGE_PROPERTY},
        ("page_id",),
    ),
    _tool(
        "notion.retrieve_database",
        "Read an allowed Notion database.",
        {"connection_id": _CONNECTION_PROPERTY, "database_id": {"type": "string"}},
        ("database_id",),
    ),
    _tool(
        "notion.create_page",
        "Create a page under an allowed page or database after explicit approval.",
        {"connection_id": _CONNECTION_PROPERTY, "parent_type": {"type": "string", "enum": ["page_id", "database_id"]}, "parent_id": {"type": "string"}, "properties": {"type": "object"}, "children": {"type": "array"}},
        ("parent_type", "parent_id", "properties"),
        write=True,
    ),
    _tool(
        "notion.update_page",
        "Update page properties after explicit approval; archive/delete is not supported.",
        {"connection_id": _CONNECTION_PROPERTY, "page_id": _NOTION_PAGE_PROPERTY, "properties": {"type": "object"}},
        ("page_id", "properties"),
        write=True,
    ),
    _tool(
        "notion.append_blocks",
        "Append blocks to a page within an allowed root after explicit approval.",
        {"connection_id": _CONNECTION_PROPERTY, "page_id": _NOTION_PAGE_PROPERTY, "children": {"type": "array"}},
        ("page_id", "children"),
        write=True,
    ),
)


class ConnectorServiceError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        recoverable: bool = False,
        execution_state_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.recoverable = recoverable
        self.execution_state_unknown = bool(execution_state_unknown)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime] = None) -> str:
    return (value or _now()).isoformat()


def _parse_time(value: Any) -> Optional[datetime]:
    try:
        result = datetime.fromisoformat(str(value))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _bounded_json(value: Any, *, maximum: int = 250_000) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > maximum:
        raise ConnectorServiceError("CONNECTOR_PAYLOAD_TOO_LARGE", "The connector payload is too large.", status_code=413)
    return json.loads(encoded)


def _catalog(connector_id: str) -> dict[str, Any]:
    connector = normalize_connector_id(connector_id)
    return deepcopy(next(item for item in CONNECTOR_CATALOG if item["id"] == connector))


def _callback_uri(connector_id: str, value: str) -> str:
    connector = normalize_connector_id(connector_id)
    candidate = str(value or "").strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise ConnectorServiceError("INVALID_CALLBACK_URI", "The OAuth callback URI is invalid.") from exc
    host = (parsed.hostname or "").casefold()
    expected_path = f"/oauth/callback/{connector}"
    if (
        parsed.scheme not in {"http", "https"}
        or host not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != expected_path
        or port is None
    ):
        raise ConnectorServiceError(
            "INVALID_CALLBACK_URI",
            f"The callback must be an exact loopback URL ending in {expected_path}.",
        )
    return candidate


class ConnectorService:
    def __init__(
        self,
        *,
        store: Optional[ConnectorStore] = None,
        secrets_store: Optional[ConnectorSecretStore] = None,
        http_session: Optional[requests.Session] = None,
        project_exists: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self.store = store or ConnectorStore()
        self.secrets = secrets_store or ConnectorSecretStore()
        self.http = http_session or requests.Session()
        if hasattr(self.http, "trust_env"):
            self.http.trust_env = False
        self.project_exists = project_exists or (lambda _project_id: True)
        self._refresh_locks: dict[str, threading.Lock] = {}
        self._refresh_locks_guard = threading.RLock()

    def initialize(self) -> dict[str, int]:
        self.store.ensure_schema()
        expired = self.store.expire_oauth_flows()
        invalidated = self.store.invalidate_incomplete_oauth_flows()
        cleaned = 0
        for flow_id in self.store.oauth_flow_ids(statuses={"expired", "failed", "completed"}):
            cleaned += int(self.secrets.delete("flow", flow_id))
        return {
            "expired_oauth_flows": expired,
            "invalidated_oauth_flows": len(invalidated),
            "cleaned_flow_secrets": cleaned,
        }

    @staticmethod
    def catalog() -> list[dict[str, Any]]:
        return deepcopy(list(CONNECTOR_CATALOG))

    def extension_health(self, identifier: str) -> tuple[str, Any]:
        connector_id = str(identifier or "").strip().casefold()
        if connector_id.startswith("connector."):
            connector_id = connector_id.split(".", 1)[1]
        connector = normalize_connector_id(connector_id)
        connections = self.store.list_connections(connector_id=connector)
        if not connections:
            return "unavailable", {"reason": "no_account_connected"}
        if any(item["status"] == "connected" for item in connections):
            return "ready", {
                "connected_accounts": sum(
                    item["status"] == "connected" for item in connections
                )
            }
        if any(item["status"] == "degraded" for item in connections):
            return "degraded", {"reason": "connection_needs_attention"}
        return "unavailable", {"reason": "reconnect_required"}

    def configure_auth_profile(
        self,
        connector_id: str,
        *,
        client_id: str,
        client_secret: Optional[str],
        callback_uri: str,
    ) -> dict[str, Any]:
        connector = normalize_connector_id(connector_id)
        safe_client_id = str(client_id or "").strip()
        safe_secret = str(client_secret or "").strip()
        if not safe_client_id or len(safe_client_id) > 512:
            raise ConnectorServiceError("INVALID_CLIENT_ID", "The OAuth client ID is invalid.")
        existing = self.store.get_auth_profile(connector)
        existing_secret = bool(
            existing
            and self.secrets.exists("profile", str(existing["profile_id"]))
        )
        if len(safe_secret) > 16_384 or (not safe_secret and not existing_secret):
            raise ConnectorServiceError(
                "INVALID_CLIENT_SECRET",
                "The OAuth client secret is required for first-time setup.",
            )
        callback = _callback_uri(connector, callback_uri)
        profile = self.store.upsert_auth_profile(
            connector_id=connector,
            client_id=safe_client_id,
            callback_uri=callback,
            auth_mode=_catalog(connector)["auth_mode"],
        )
        if safe_secret:
            try:
                self.secrets.set(
                    "profile", profile["profile_id"], {"client_secret": safe_secret}
                )
            except Exception:
                self.store.audit(
                    connector_id=connector,
                    action="auth_profile.configure",
                    status="failed",
                    error_code="CONNECTOR_SECRET_STORE_ERROR",
                )
                raise
        self.store.audit(
            connector_id=connector,
            action="auth_profile.configure",
            status="completed",
            details={"client_secret_changed": bool(safe_secret)},
        )
        return self.auth_profile_status(connector)

    def auth_profile_status(self, connector_id: str) -> Optional[dict[str, Any]]:
        connector = normalize_connector_id(connector_id)
        profile = self.store.get_auth_profile(connector)
        if profile is None:
            return None
        result = dict(profile)
        result["configured"] = self.secrets.exists("profile", profile["profile_id"])
        return result

    def delete_auth_profile(self, connector_id: str) -> bool:
        connector = normalize_connector_id(connector_id)
        profile = self.store.get_auth_profile(connector)
        if profile is None:
            return False
        if self.store.list_connections(connector_id=connector):
            raise ConnectorConflictError(
                "CONNECTOR_PROFILE_IN_USE", "Disconnect every account before deleting the OAuth profile."
            )
        self.secrets.delete("profile", profile["profile_id"])
        deleted = self.store.delete_auth_profile(connector)
        self.store.audit(connector_id=connector, action="auth_profile.delete", status="completed")
        return deleted

    def start_oauth(
        self, connector_id: str, *, connection_id: Optional[str] = None
    ) -> dict[str, Any]:
        connector = normalize_connector_id(connector_id)
        profile = self.store.get_auth_profile(connector)
        if profile is None or not self.secrets.exists("profile", profile["profile_id"]):
            raise ConnectorServiceError(
                "CONNECTOR_AUTH_PROFILE_REQUIRED",
                "Configure the local OAuth application before connecting an account.",
                status_code=409,
            )
        if connection_id:
            connection = self.store.get_connection(connection_id)
            if connection is None or connection["connector_id"] != connector:
                raise ConnectorNotFoundError("The connector connection was not found.")
        state = secrets.token_urlsafe(48)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        flow_id = f"oauth_{uuid.uuid4().hex}"
        flow = self.store.create_oauth_flow(
            flow_id=flow_id,
            connector_id=connector,
            profile_id=profile["profile_id"],
            connection_id=connection_id,
            state_sha256=hashlib.sha256(state.encode("utf-8")).hexdigest(),
            redirect_uri=profile["callback_uri"],
            ttl_seconds=600,
        )
        try:
            self.secrets.set("flow", flow_id, {"code_verifier": verifier})
        except Exception:
            self.store.finish_oauth_flow(
                flow_id, success=False, error_code="CONNECTOR_SECRET_STORE_ERROR"
            )
            raise
        if connector == "github":
            params = {
                "client_id": profile["client_id"],
                "redirect_uri": profile["callback_uri"],
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "allow_signup": "false",
            }
            authorization_url = f"{GITHUB_AUTHORIZE}?{urlencode(params)}"
        else:
            params = {
                "client_id": profile["client_id"],
                "response_type": "code",
                "owner": "user",
                "redirect_uri": profile["callback_uri"],
                "state": state,
            }
            authorization_url = f"{NOTION_AUTHORIZE}?{urlencode(params)}"
        self.store.audit(
            connector_id=connector,
            connection_id=connection_id,
            action="oauth.start",
            status="completed",
            details={"flow_id": flow_id},
        )
        return {
            "flow_id": flow_id,
            "connector_id": connector,
            "authorization_url": authorization_url,
            "expires_at": flow["expires_at"],
        }

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        expected: Sequence[int] = (200,),
        mutation: bool = False,
        **kwargs: Any,
    ) -> Any:
        headers = {"User-Agent": "Local-AI-Workbench-Connector/1", **kwargs.pop("headers", {})}
        try:
            response = self.http.request(
                method,
                url,
                headers=headers,
                timeout=HTTP_TIMEOUT,
                allow_redirects=False,
                **kwargs,
            )
        except requests.Timeout as exc:
            raise ConnectorServiceError(
                "CONNECTOR_TIMEOUT",
                "The connector request timed out.",
                status_code=504,
                recoverable=True,
                execution_state_unknown=mutation,
            ) from exc
        except requests.ConnectionError as exc:
            raise ConnectorServiceError(
                "CONNECTOR_UNAVAILABLE",
                "The connector connection was interrupted.",
                status_code=502,
                recoverable=True,
                execution_state_unknown=mutation,
            ) from exc
        except requests.RequestException as exc:
            raise ConnectorServiceError(
                "CONNECTOR_UNAVAILABLE",
                "The connector service is unavailable.",
                status_code=502,
                recoverable=True,
                execution_state_unknown=mutation,
            ) from exc
        if response.status_code not in expected:
            if response.status_code in {401, 403}:
                code, message, recoverable = (
                    "CONNECTOR_AUTH_REQUIRED",
                    "The connector authorization is no longer valid.",
                    True,
                )
            elif response.status_code == 429:
                code, message, recoverable = (
                    "CONNECTOR_RATE_LIMITED",
                    "The connector rate limit was reached.",
                    True,
                )
            else:
                code, message, recoverable = (
                    "CONNECTOR_PROVIDER_ERROR",
                    "The connector provider rejected the request.",
                    response.status_code >= 500,
                )
            raise ConnectorServiceError(
                code,
                message,
                status_code=502,
                recoverable=recoverable,
                execution_state_unknown=bool(mutation and response.status_code >= 500),
            )
        if response.status_code == 204 or not getattr(response, "content", b""):
            return {}
        try:
            return response.json()
        except (TypeError, ValueError) as exc:
            raise ConnectorServiceError(
                "CONNECTOR_RESPONSE_INVALID",
                "The connector returned an invalid response.",
                status_code=502,
                execution_state_unknown=mutation,
            ) from exc

    @staticmethod
    def _token_values(payload: Mapping[str, Any], previous: Optional[Mapping[str, str]] = None) -> dict[str, str]:
        old = previous or {}
        access_token = str(payload.get("access_token") or old.get("access_token") or "").strip()
        refresh_token = str(payload.get("refresh_token") or old.get("refresh_token") or "").strip()
        token_type = str(payload.get("token_type") or old.get("token_type") or "bearer").strip()
        if not access_token:
            raise ConnectorServiceError(
                "OAUTH_TOKEN_EXCHANGE_FAILED",
                "The OAuth provider did not return an access token.",
                status_code=502,
            )
        values = {"access_token": access_token, "token_type": token_type}
        if refresh_token:
            values["refresh_token"] = refresh_token
        return values

    @staticmethod
    def _token_expiry(payload: Mapping[str, Any]) -> Optional[str]:
        try:
            seconds = int(payload.get("expires_in") or 0)
        except (TypeError, ValueError):
            seconds = 0
        return _iso(_now() + timedelta(seconds=max(1, seconds))) if seconds > 0 else None

    def _exchange_github(
        self,
        *,
        profile: Mapping[str, Any],
        client_secret: str,
        code: str,
        verifier: str,
    ) -> tuple[dict[str, str], Optional[str], dict[str, Any]]:
        token_payload = self._request_json(
            "POST",
            GITHUB_TOKEN,
            headers={"Accept": "application/json"},
            data={
                "client_id": profile["client_id"],
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": profile["callback_uri"],
                "code_verifier": verifier,
            },
        )
        if not isinstance(token_payload, Mapping) or token_payload.get("error"):
            raise ConnectorServiceError(
                "OAUTH_TOKEN_EXCHANGE_FAILED", "GitHub rejected the OAuth code.", status_code=502
            )
        tokens = self._token_values(token_payload)
        user = self._request_json(
            "GET",
            f"{GITHUB_API}/user",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {tokens['access_token']}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        if not isinstance(user, Mapping) or not user.get("id"):
            raise ConnectorServiceError(
                "CONNECTOR_ACCOUNT_INVALID", "GitHub did not return a valid user account.", status_code=502
            )
        identity = {
            "account_id": str(user["id"]),
            "display_name": str(user.get("name") or user.get("login") or user["id"]),
            "workspace_id": None,
            "metadata": {
                "login": str(user.get("login") or ""),
                "avatar_url": str(user.get("avatar_url") or ""),
                "html_url": str(user.get("html_url") or ""),
            },
        }
        return tokens, self._token_expiry(token_payload), identity

    def _exchange_notion(
        self,
        *,
        profile: Mapping[str, Any],
        client_secret: str,
        code: str,
    ) -> tuple[dict[str, str], Optional[str], dict[str, Any]]:
        basic = base64.b64encode(
            f"{profile['client_id']}:{client_secret}".encode("utf-8")
        ).decode("ascii")
        token_payload = self._request_json(
            "POST",
            NOTION_TOKEN,
            headers={
                "Accept": "application/json",
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/json",
                "Notion-Version": NOTION_VERSION,
            },
            json={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": profile["callback_uri"],
            },
        )
        if not isinstance(token_payload, Mapping):
            raise ConnectorServiceError(
                "OAUTH_TOKEN_EXCHANGE_FAILED", "Notion rejected the OAuth code.", status_code=502
            )
        tokens = self._token_values(token_payload)
        user = self._request_json(
            "GET",
            f"{NOTION_API}/users/me",
            headers={
                "Authorization": f"Bearer {tokens['access_token']}",
                "Notion-Version": NOTION_VERSION,
            },
        )
        if not isinstance(user, Mapping) or not user.get("id"):
            raise ConnectorServiceError(
                "CONNECTOR_ACCOUNT_INVALID", "Notion did not return a valid bot account.", status_code=502
            )
        workspace_id = str(token_payload.get("workspace_id") or "") or None
        owner = token_payload.get("owner") if isinstance(token_payload.get("owner"), Mapping) else {}
        workspace_name = str(token_payload.get("workspace_name") or "")
        identity = {
            "account_id": str(user["id"]),
            "display_name": workspace_name or str(user.get("name") or user["id"]),
            "workspace_id": workspace_id,
            "metadata": {
                "workspace_name": workspace_name,
                "workspace_icon": str(token_payload.get("workspace_icon") or ""),
                "bot_id": str(token_payload.get("bot_id") or user.get("id") or ""),
                "owner_type": str(owner.get("type") or ""),
            },
        }
        return tokens, self._token_expiry(token_payload), identity

    def complete_oauth(
        self,
        connector_id: str,
        *,
        state: str,
        code: Optional[str],
        provider_error: Optional[str] = None,
        authorize: Optional[Callable[[str], Any]] = None,
    ) -> dict[str, Any]:
        connector = normalize_connector_id(connector_id)
        flow = self.store.claim_oauth_flow(connector_id=connector, raw_state=state)
        try:
            if authorize is not None:
                outcome = authorize(connector)
                if outcome is False:
                    raise ConnectorServiceError(
                        "EXTENSION_DISABLED",
                        "The connector extension is disabled.",
                        status_code=409,
                        recoverable=True,
                    )
            if provider_error:
                raise ConnectorServiceError(
                    "OAUTH_PROVIDER_DENIED",
                    "The OAuth provider did not authorize the connection.",
                    status_code=400,
                    recoverable=True,
                )
            authorization_code = str(code or "").strip()
            if not authorization_code or len(authorization_code) > 4096:
                raise ConnectorServiceError("OAUTH_CODE_REQUIRED", "The OAuth code is missing.")
            profile = self.store.get_auth_profile(connector)
            if profile is None or profile["profile_id"] != flow["profile_id"]:
                raise ConnectorServiceError(
                    "OAUTH_PROFILE_CHANGED",
                    "The OAuth profile changed while authorization was in progress.",
                    status_code=409,
                )
            if profile["callback_uri"] != flow["redirect_uri"]:
                raise ConnectorServiceError(
                    "OAUTH_CALLBACK_CHANGED",
                    "The OAuth callback changed while authorization was in progress.",
                    status_code=409,
                )
            profile_secret = self.secrets.get("profile", profile["profile_id"])
            flow_secret = self.secrets.get("flow", flow["flow_id"])
            client_secret = profile_secret.get("client_secret", "")
            verifier = flow_secret.get("code_verifier", "")
            if not client_secret or not verifier:
                raise ConnectorServiceError(
                    "CONNECTOR_SECRET_MISSING", "The local OAuth secret is unavailable.", status_code=409
                )
            if connector == "github":
                tokens, token_expiry, identity = self._exchange_github(
                    profile=profile,
                    client_secret=client_secret,
                    code=authorization_code,
                    verifier=verifier,
                )
            else:
                tokens, token_expiry, identity = self._exchange_notion(
                    profile=profile,
                    client_secret=client_secret,
                    code=authorization_code,
                )
            target_id = str(flow.get("connection_id") or "")
            if target_id:
                target = self.store.get_connection(target_id)
                if target is None or target["connector_id"] != connector:
                    raise ConnectorNotFoundError("The reconnect target was not found.")
            else:
                target = next(
                    (
                        item
                        for item in self.store.list_connections(connector_id=connector)
                        if item["account_id"] == identity["account_id"]
                    ),
                    None,
                )
                target_id = str(target["connection_id"]) if target else f"conn_{uuid.uuid4().hex}"
            self.secrets.set("connection", target_id, tokens)
            descriptor = _catalog(connector)
            requested = [
                f"{item['name']}:{item['access']}" for item in descriptor["permissions"]
            ]
            connection = self.store.save_connection(
                connection_id=target_id,
                connector_id=connector,
                auth_profile_id=profile["profile_id"],
                account_id=identity["account_id"],
                display_name=identity["display_name"],
                workspace_id=identity["workspace_id"],
                metadata=identity["metadata"],
                requested_permissions=requested,
                granted_permissions=requested,
                token_expires_at=token_expiry,
            )
            self.store.finish_oauth_flow(flow["flow_id"], success=True)
            self.store.audit(
                connector_id=connector,
                connection_id=connection["connection_id"],
                action="oauth.complete",
                status="completed",
            )
            return self.public_connection(connection)
        except Exception as exc:
            error_code = getattr(exc, "code", "OAUTH_CALLBACK_FAILED")
            try:
                self.store.finish_oauth_flow(
                    flow["flow_id"], success=False, error_code=error_code
                )
            except ConnectorStoreError:
                pass
            self.store.audit(
                connector_id=connector,
                connection_id=flow.get("connection_id"),
                action="oauth.complete",
                status="failed",
                error_code=error_code,
            )
            raise
        finally:
            self.secrets.delete("flow", flow["flow_id"])

    @staticmethod
    def public_connection(connection: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "connection_id",
            "connector_id",
            "status",
            "account_id",
            "display_name",
            "workspace_id",
            "metadata",
            "requested_permissions",
            "granted_permissions",
            "token_expires_at",
            "error_code",
            "created_at",
            "updated_at",
            "validated_at",
            "revoked_at",
            "binding",
        }
        return {key: deepcopy(value) for key, value in connection.items() if key in allowed}

    def get_connection(self, connection_id: str) -> dict[str, Any]:
        connection = self.store.get_connection(connection_id)
        if connection is None:
            raise ConnectorNotFoundError("The connector connection was not found.")
        return self.public_connection(connection)

    def list_connections(
        self, *, connector_id: Optional[str] = None, project_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        if project_id:
            if not self.project_exists(project_id):
                raise ConnectorNotFoundError("The project was not found.")
            items = self.store.list_connections(connector_id=connector_id)
            for item in items:
                item["binding"] = self.store.get_project_binding(
                    project_id=project_id,
                    connection_id=item["connection_id"],
                )
            return [self.public_connection(item) for item in items]
        return [
            self.public_connection(item)
            for item in self.store.list_connections(
                connector_id=connector_id
            )
        ]

    def _refresh_lock(self, connection_id: str) -> threading.Lock:
        with self._refresh_locks_guard:
            return self._refresh_locks.setdefault(connection_id, threading.Lock())

    def _refresh_token(self, connection: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        connection_id = str(connection["connection_id"])
        connector = str(connection["connector_id"])
        with self._refresh_lock(connection_id):
            current = self.store.get_connection(connection_id)
            if current is None:
                raise ConnectorNotFoundError("The connector connection was not found.")
            expiry = _parse_time(current.get("token_expires_at"))
            secrets_value = self.secrets.get("connection", connection_id)
            if expiry is None or expiry > _now() + timedelta(seconds=60):
                access = secrets_value.get("access_token", "")
                if not access:
                    raise ConnectorServiceError(
                        "CONNECTOR_AUTH_REQUIRED", "The connector token is unavailable.", status_code=409
                    )
                return access, current
            refresh_token = secrets_value.get("refresh_token", "")
            if not refresh_token:
                self.store.update_connection_status(
                    connection_id, status="refresh_required", error_code="CONNECTOR_REFRESH_REQUIRED"
                )
                raise ConnectorServiceError(
                    "CONNECTOR_REFRESH_REQUIRED",
                    "Reconnect the account to continue.",
                    status_code=409,
                    recoverable=True,
                )
            profile = self.store.get_auth_profile(connector)
            if profile is None:
                raise ConnectorServiceError(
                    "CONNECTOR_AUTH_PROFILE_REQUIRED", "The OAuth profile is unavailable.", status_code=409
                )
            profile_secret = self.secrets.get("profile", profile["profile_id"])
            client_secret = profile_secret.get("client_secret", "")
            if not client_secret:
                raise ConnectorServiceError(
                    "CONNECTOR_SECRET_MISSING", "The local OAuth secret is unavailable.", status_code=409
                )
            if connector == "github":
                payload = self._request_json(
                    "POST",
                    GITHUB_TOKEN,
                    headers={"Accept": "application/json"},
                    data={
                        "client_id": profile["client_id"],
                        "client_secret": client_secret,
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                    },
                )
            else:
                basic = base64.b64encode(
                    f"{profile['client_id']}:{client_secret}".encode("utf-8")
                ).decode("ascii")
                payload = self._request_json(
                    "POST",
                    NOTION_TOKEN,
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Basic {basic}",
                        "Content-Type": "application/json",
                        "Notion-Version": NOTION_VERSION,
                    },
                    json={"grant_type": "refresh_token", "refresh_token": refresh_token},
                )
            if not isinstance(payload, Mapping):
                raise ConnectorServiceError(
                    "CONNECTOR_REFRESH_FAILED", "The provider returned an invalid refresh response.", status_code=502
                )
            tokens = self._token_values(payload, secrets_value)
            self.secrets.set("connection", connection_id, tokens)
            current = self.store.update_connection_status(
                connection_id,
                status="connected",
                token_expires_at=self._token_expiry(payload),
                validated=True,
            )
            self.store.audit(
                connector_id=connector,
                connection_id=connection_id,
                action="token.refresh",
                status="completed",
            )
            return tokens["access_token"], current

    def _access_token(self, connection: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        return self._refresh_token(connection)

    def health(self, connection_id: str) -> dict[str, Any]:
        connection = self.store.get_connection(connection_id)
        if connection is None:
            raise ConnectorNotFoundError("The connector connection was not found.")
        connector = connection["connector_id"]
        try:
            access_token, current = self._access_token(connection)
            if connector == "github":
                self._request_json(
                    "GET",
                    f"{GITHUB_API}/user",
                    headers=self._github_headers(access_token),
                )
            else:
                self._request_json(
                    "GET",
                    f"{NOTION_API}/users/me",
                    headers=self._notion_headers(access_token),
                )
            current = self.store.update_connection_status(
                connection_id, status="connected", validated=True
            )
            self.store.audit(
                connector_id=connector,
                connection_id=connection_id,
                action="connection.health",
                status="completed",
            )
            return self.public_connection(current)
        except Exception as exc:
            code = getattr(exc, "code", "CONNECTOR_HEALTH_FAILED")
            status = "refresh_required" if code in {
                "CONNECTOR_AUTH_REQUIRED",
                "CONNECTOR_REFRESH_REQUIRED",
            } else "degraded"
            current = self.store.update_connection_status(
                connection_id, status=status, error_code=code
            )
            self.store.audit(
                connector_id=connector,
                connection_id=connection_id,
                action="connection.health",
                status="failed",
                error_code=code,
            )
            if isinstance(exc, (ConnectorServiceError, ConnectorStoreError, ConnectorSecretError)):
                raise
            raise ConnectorServiceError(
                "CONNECTOR_HEALTH_FAILED", "The connector health check failed.", status_code=502
            ) from exc

    def disconnect(self, connection_id: str, *, force_local: bool = False) -> dict[str, Any]:
        connection = self.store.get_connection(connection_id)
        if connection is None:
            raise ConnectorNotFoundError("The connector connection was not found.")
        connector = connection["connector_id"]
        if not force_local:
            try:
                token_values = self.secrets.get("connection", connection_id)
                access_token = token_values.get("access_token", "")
                profile = self.store.get_auth_profile(connector)
                profile_secret = (
                    self.secrets.get("profile", profile["profile_id"]) if profile else {}
                )
                if access_token and profile and profile_secret.get("client_secret"):
                    basic = base64.b64encode(
                        f"{profile['client_id']}:{profile_secret['client_secret']}".encode("utf-8")
                    ).decode("ascii")
                    if connector == "github":
                        self._request_json(
                            "DELETE",
                            f"{GITHUB_API}/applications/{quote(str(profile['client_id']), safe='')}/grant",
                            expected=(204,),
                            mutation=True,
                            headers={
                                "Accept": "application/vnd.github+json",
                                "Authorization": f"Basic {basic}",
                                "X-GitHub-Api-Version": "2022-11-28",
                            },
                            json={"access_token": access_token},
                        )
                    else:
                        self._request_json(
                            "POST",
                            f"{NOTION_API}/oauth/revoke",
                            mutation=True,
                            headers={
                                "Accept": "application/json",
                                "Authorization": f"Basic {basic}",
                                "Content-Type": "application/json",
                                "Notion-Version": NOTION_VERSION,
                            },
                            json={"token": access_token},
                        )
            except Exception as exc:
                code = getattr(exc, "code", "CONNECTOR_REVOKE_FAILED")
                current = self.store.update_connection_status(
                    connection_id, status="revoke_failed", error_code=code
                )
                self.store.audit(
                    connector_id=connector,
                    connection_id=connection_id,
                    action="connection.disconnect",
                    status="failed",
                    error_code=code,
                )
                raise ConnectorServiceError(
                    "CONNECTOR_REVOKE_FAILED",
                    "Remote revocation failed. Confirm local-only removal to forget the credentials.",
                    status_code=502,
                    recoverable=True,
                    execution_state_unknown=bool(
                        getattr(exc, "execution_state_unknown", False)
                    ),
                ) from exc
        self.secrets.delete_record(connection_id)
        self.store.delete_connection(connection_id)
        self.store.audit(
            connector_id=connector,
            connection_id=connection_id,
            action="connection.disconnect",
            status="completed",
            details={"force_local": force_local},
        )
        return {"connection_id": connection_id, "disconnected": True, "force_local": force_local}

    @staticmethod
    def _github_headers(access_token: str) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {access_token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @staticmethod
    def _notion_headers(access_token: str) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Notion-Version": NOTION_VERSION,
        }

    def put_project_binding(
        self,
        *,
        project_id: str,
        connection_id: str,
        enabled: bool,
        mode: str,
    ) -> dict[str, Any]:
        if not self.project_exists(project_id):
            raise ConnectorNotFoundError("The project was not found.")
        connection = self.store.get_connection(connection_id)
        if connection is None:
            raise ConnectorNotFoundError("The connector connection was not found.")
        binding = self.store.put_project_binding(
            project_id=project_id,
            connection_id=connection_id,
            enabled=enabled,
            mode=mode,
        )
        self.store.audit(
            connector_id=connection["connector_id"],
            connection_id=connection_id,
            project_id=project_id,
            action="project_binding.update",
            status="completed",
            details={"enabled": enabled, "mode": mode},
        )
        return binding

    def get_bound_resources(self, *, project_id: str, connection_id: str) -> dict[str, Any]:
        return self.store.list_resource_bindings(
            project_id=project_id, connection_id=connection_id
        )

    @staticmethod
    def _github_resource(item: Mapping[str, Any], installation_id: Any = None) -> dict[str, Any]:
        full_name = str(item.get("full_name") or "")
        return {
            "resource_type": "repository",
            "resource_id": full_name,
            "parent_id": str(installation_id or item.get("installation_id") or "") or None,
            "display_label": full_name,
            "metadata": {
                "private": bool(item.get("private")),
                "default_branch": str(item.get("default_branch") or ""),
                "html_url": str(item.get("html_url") or ""),
            },
        }

    @staticmethod
    def _notion_title(item: Mapping[str, Any]) -> str:
        source: Any = None
        if item.get("object") in {"database", "data_source"}:
            source = item.get("title")
        else:
            properties = item.get("properties")
            if isinstance(properties, Mapping):
                for prop in properties.values():
                    if isinstance(prop, Mapping) and prop.get("type") == "title":
                        source = prop.get("title")
                        break
        if isinstance(source, list):
            text = "".join(
                str(part.get("plain_text") or "")
                for part in source
                if isinstance(part, Mapping)
            ).strip()
            if text:
                return text[:512]
        return str(item.get("url") or item.get("id") or "Untitled")[:512]

    @classmethod
    def _notion_resource(cls, item: Mapping[str, Any]) -> dict[str, Any]:
        object_type = str(item.get("object") or "").casefold()
        resource_type = "database" if object_type in {"database", "data_source"} else "page"
        parent = item.get("parent") if isinstance(item.get("parent"), Mapping) else {}
        parent_id = next(
            (
                str(parent.get(key))
                for key in ("page_id", "database_id", "data_source_id")
                if parent.get(key)
            ),
            None,
        )
        return {
            "resource_type": resource_type,
            "resource_id": str(item.get("id") or ""),
            "parent_id": parent_id,
            "display_label": cls._notion_title(item),
            "metadata": {
                "url": str(item.get("url") or ""),
                "object": object_type,
            },
        }

    def list_resources(
        self,
        connection_id: str,
        *,
        resource_type: Optional[str] = None,
        query: str = "",
    ) -> list[dict[str, Any]]:
        connection = self.store.get_connection(connection_id)
        if connection is None:
            raise ConnectorNotFoundError("The connector connection was not found.")
        access_token, _ = self._access_token(connection)
        connector = connection["connector_id"]
        q = str(query or "").strip().casefold()[:512]
        requested_type = str(resource_type or "").strip().casefold()
        resources: list[dict[str, Any]] = []
        if connector == "github":
            if requested_type and requested_type != "repository":
                return []
            installations = self._request_json(
                "GET",
                f"{GITHUB_API}/user/installations?per_page=100",
                headers=self._github_headers(access_token),
            )
            if not isinstance(installations, Mapping):
                raise ConnectorServiceError(
                    "CONNECTOR_RESPONSE_INVALID", "GitHub returned an invalid installation list.", status_code=502
                )
            for installation in list(installations.get("installations") or [])[:100]:
                if not isinstance(installation, Mapping) or not installation.get("id"):
                    continue
                installation_id = installation["id"]
                for page in range(1, 11):
                    payload = self._request_json(
                        "GET",
                        f"{GITHUB_API}/user/installations/{quote(str(installation_id), safe='')}/repositories?per_page=100&page={page}",
                        headers=self._github_headers(access_token),
                    )
                    items = payload.get("repositories") if isinstance(payload, Mapping) else None
                    if not isinstance(items, list):
                        raise ConnectorServiceError(
                            "CONNECTOR_RESPONSE_INVALID", "GitHub returned an invalid repository list.", status_code=502
                        )
                    resources.extend(
                        self._github_resource(item, installation_id)
                        for item in items
                        if isinstance(item, Mapping) and item.get("full_name")
                    )
                    if len(items) < 100:
                        break
        else:
            if requested_type and requested_type not in {"page", "database"}:
                return []
            cursor: Optional[str] = None
            for _page in range(10):
                body: dict[str, Any] = {"page_size": 100}
                if query:
                    body["query"] = str(query)[:512]
                if cursor:
                    body["start_cursor"] = cursor
                payload = self._request_json(
                    "POST",
                    f"{NOTION_API}/search",
                    headers=self._notion_headers(access_token),
                    json=body,
                )
                items = payload.get("results") if isinstance(payload, Mapping) else None
                if not isinstance(items, list):
                    raise ConnectorServiceError(
                        "CONNECTOR_RESPONSE_INVALID", "Notion returned an invalid search result.", status_code=502
                    )
                for item in items:
                    if not isinstance(item, Mapping) or not item.get("id"):
                        continue
                    resource = self._notion_resource(item)
                    if not requested_type or resource["resource_type"] == requested_type:
                        resources.append(resource)
                cursor = str(payload.get("next_cursor") or "") or None
                if not payload.get("has_more") or not cursor:
                    break
        deduplicated: dict[tuple[str, str], dict[str, Any]] = {}
        for resource in resources:
            if q and q not in resource["display_label"].casefold() and q not in resource["resource_id"].casefold():
                continue
            deduplicated[(resource["resource_type"], resource["resource_id"])] = resource
        return sorted(
            deduplicated.values(),
            key=lambda item: (item["resource_type"], item["display_label"].casefold()),
        )

    def _verify_resource(
        self,
        connection: Mapping[str, Any],
        resource: Mapping[str, Any],
    ) -> dict[str, Any]:
        connector = connection["connector_id"]
        access_token, _ = self._access_token(connection)
        kind = str(resource.get("resource_type") or "").strip().casefold()
        identity = str(resource.get("resource_id") or "").strip()
        if connector == "github":
            repository = self._repository(identity)
            payload = self._request_json(
                "GET",
                f"{GITHUB_API}/repos/{repository}",
                headers=self._github_headers(access_token),
            )
            if not isinstance(payload, Mapping) or not payload.get("full_name"):
                raise ConnectorServiceError("RESOURCE_NOT_FOUND", "The repository was not found.", status_code=404)
            return self._github_resource(payload, resource.get("parent_id"))
        notion_id = self._notion_id(identity)
        if kind == "page":
            endpoint = f"{NOTION_API}/pages/{quote(notion_id, safe='')}"
        elif kind == "database":
            endpoint = f"{NOTION_API}/databases/{quote(notion_id, safe='')}"
        else:
            raise ConnectorServiceError("INVALID_RESOURCE_TYPE", "The Notion resource type is invalid.")
        payload = self._request_json(
            "GET", endpoint, headers=self._notion_headers(access_token)
        )
        if not isinstance(payload, Mapping) or not payload.get("id"):
            raise ConnectorServiceError("RESOURCE_NOT_FOUND", "The Notion resource was not found.", status_code=404)
        return self._notion_resource(payload)

    def replace_resources(
        self,
        *,
        project_id: str,
        connection_id: str,
        expected_revision: int,
        resources: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        connection = self.store.get_connection(connection_id)
        if connection is None:
            raise ConnectorNotFoundError("The connector connection was not found.")
        binding = self.store.get_project_binding(
            project_id=project_id, connection_id=connection_id
        )
        if binding is None:
            raise ConnectorNotFoundError("The project connection binding was not found.")
        verified = [self._verify_resource(connection, resource) for resource in resources]
        result = self.store.replace_resource_bindings(
            project_id=project_id,
            connection_id=connection_id,
            expected_revision=expected_revision,
            resources=verified,
        )
        self.store.audit(
            connector_id=connection["connector_id"],
            connection_id=connection_id,
            project_id=project_id,
            action="resource_binding.replace",
            status="completed",
            details={"count": len(verified), "revision": result["revision"]},
        )
        return result

    @staticmethod
    def _repository(value: Any) -> str:
        repository = str(value or "").strip()
        if not _REPOSITORY.fullmatch(repository) or ".." in repository:
            raise ConnectorServiceError("INVALID_REPOSITORY", "The repository name is invalid.")
        return repository

    @staticmethod
    def _notion_id(value: Any) -> str:
        notion_id = str(value or "").strip()
        if not _NOTION_ID.fullmatch(notion_id):
            raise ConnectorServiceError("INVALID_NOTION_ID", "The Notion ID is invalid.")
        return notion_id

    @staticmethod
    def _positive_int(value: Any, label: str, *, maximum: int = 2_147_483_647) -> int:
        if isinstance(value, bool):
            raise ConnectorServiceError("INVALID_TOOL_ARGUMENTS", f"{label} is invalid.")
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise ConnectorServiceError("INVALID_TOOL_ARGUMENTS", f"{label} is invalid.") from exc
        if result < 1 or result > maximum:
            raise ConnectorServiceError("INVALID_TOOL_ARGUMENTS", f"{label} is invalid.")
        return result

    @staticmethod
    def _notion_key(value: Any) -> str:
        return str(value or "").replace("-", "").casefold()

    def _resolve_tool_connection(
        self,
        *,
        project_id: str,
        connector_id: str,
        requested_id: Optional[str],
        write: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        candidates = [
            item
            for item in self.store.list_project_connections(project_id, enabled_only=True)
            if item["connector_id"] == connector_id
            and item["status"] in {"connected", "degraded"}
        ]
        if requested_id:
            candidates = [item for item in candidates if item["connection_id"] == requested_id]
        if not candidates:
            raise ConnectorServiceError(
                "CONNECTOR_NOT_AVAILABLE", "No enabled connector connection is available for this project.", status_code=409
            )
        if len(candidates) != 1:
            raise ConnectorServiceError(
                "CONNECTOR_CONNECTION_AMBIGUOUS",
                "Specify connection_id because this project has multiple accounts.",
                status_code=409,
            )
        connection = candidates[0]
        binding = connection["binding"]
        if write and binding["mode"] != "read_write":
            raise ConnectorServiceError(
                "CONNECTOR_WRITE_DISABLED", "This project connection is read-only.", status_code=403
            )
        resources = self.store.list_resource_bindings(
            project_id=project_id, connection_id=connection["connection_id"]
        )
        if not resources["resources"]:
            raise ConnectorServiceError(
                "RESOURCE_BINDING_REQUIRED", "Select at least one resource for this project.", status_code=409
            )
        return connection, resources

    def list_tool_definitions(self, project_id: str) -> list[dict[str, Any]]:
        connections = self.store.list_project_connections(project_id, enabled_only=True)
        active: dict[str, list[dict[str, Any]]] = {}
        for connection in connections:
            if connection["status"] not in {"connected", "degraded"}:
                continue
            try:
                resources = self.store.list_resource_bindings(
                    project_id=project_id, connection_id=connection["connection_id"]
                )["resources"]
            except ConnectorStoreError:
                continue
            if resources:
                active.setdefault(connection["connector_id"], []).append(connection)
        results: list[dict[str, Any]] = []
        for raw in TOOL_DEFINITIONS:
            connector = raw["metadata"]["connector_id"]
            candidates = active.get(connector, [])
            if not candidates:
                continue
            if raw["metadata"]["approval_required"] and not any(
                item["binding"]["mode"] == "read_write" for item in candidates
            ):
                continue
            tool = deepcopy(raw)
            connection_property = tool["function"]["parameters"]["properties"]["connection_id"]
            connection_property["enum"] = [item["connection_id"] for item in candidates]
            connection_property["description"] = "Available account connections: " + ", ".join(
                f"{item['connection_id']} ({item['display_name']})" for item in candidates
            )
            if len(candidates) > 1:
                required = tool["function"]["parameters"].setdefault("required", [])
                if "connection_id" not in required:
                    required.append("connection_id")
            results.append(tool)
        return results

    def runtime_tool_definitions(
        self,
        project_id: str,
        manifest_sha256: Mapping[str, str] | Callable[[str], str],
    ) -> list[Any]:
        """Adapt active connector tools to ``tool_runtime.ToolDefinition``.

        ``ToolDispatcher`` owns approval consumption.  Therefore the generated
        handler marks a write as approved only after the dispatcher invokes it.
        """

        from tool_runtime import ToolAccess, ToolDefinition

        definitions = []
        for raw in self.list_tool_definitions(project_id):
            function = raw["function"]
            metadata = raw["metadata"]
            extension_id = metadata["extension_id"]
            digest = (
                manifest_sha256(extension_id)
                if callable(manifest_sha256)
                else manifest_sha256.get(extension_id, "")
            )
            name = function["name"]
            write = metadata["approval_required"]

            def handler(call: Any, *, _name: str = name, _write: bool = write) -> Any:
                return self.execute_tool(
                    call.project_id,
                    _name,
                    call.arguments,
                    approved=_write,
                )

            definitions.append(
                ToolDefinition(
                    name=name,
                    description=function["description"],
                    input_schema=function["parameters"],
                    access=ToolAccess.WRITE if write else ToolAccess.READ,
                    handler=handler,
                    extension_id=extension_id,
                    manifest_sha256=str(digest),
                    risk_level="external_write" if write else "external_read",
                    timeout_seconds=30.0,
                    max_result_bytes=16 * 1024,
                    requires_connection=True,
                    requires_resource=name not in {
                        "github.list_repositories",
                        "notion.search",
                    },
                )
            )
        return definitions

    def resolve_host_call_context(
        self,
        project_id: str,
        _definition: Any,
        arguments: Mapping[str, Any],
    ) -> dict[str, Optional[str]]:
        name = str(getattr(_definition, "name", "") or "")
        resolved = self.resolve_tool_invocation(
            project_id,
            name,
            arguments,
            verify_remote_scope=True,
        )
        return {
            "connection_id": resolved["connection_id"],
            "resource_id": resolved["resource_id"],
        }

    def _ensure_github_scope(
        self, *, project_id: str, connection_id: str, repository: str
    ) -> None:
        if not self.store.resource_is_bound(
            project_id=project_id,
            connection_id=connection_id,
            resource_type="repository",
            resource_id=repository,
        ):
            raise ConnectorServiceError(
                "RESOURCE_NOT_BOUND", "The repository is not allowed for this project.", status_code=403
            )

    def _ensure_notion_scope(
        self,
        *,
        project_id: str,
        connection: Mapping[str, Any],
        resource_id: str,
        resource_type: str = "page",
    ) -> None:
        connection_id = connection["connection_id"]
        bound = self.store.list_resource_bindings(
            project_id=project_id, connection_id=connection_id
        )["resources"]
        allowed = {self._notion_key(item["resource_id"]) for item in bound}
        current = self._notion_id(resource_id)
        if self._notion_key(current) in allowed:
            return
        access_token, _ = self._access_token(connection)
        current_type = resource_type
        visited: set[str] = set()
        for _depth in range(32):
            key = self._notion_key(current)
            if key in allowed:
                return
            if key in visited:
                break
            visited.add(key)
            if current_type == "database":
                endpoint = f"{NOTION_API}/databases/{quote(current, safe='')}"
            else:
                endpoint = f"{NOTION_API}/pages/{quote(current, safe='')}"
            payload = self._request_json(
                "GET", endpoint, headers=self._notion_headers(access_token)
            )
            parent = payload.get("parent") if isinstance(payload, Mapping) else None
            if not isinstance(parent, Mapping):
                break
            parent_id = None
            for parent_key, parent_type in (
                ("page_id", "page"),
                ("database_id", "database"),
                ("data_source_id", "database"),
            ):
                if parent.get(parent_key):
                    parent_id = str(parent[parent_key])
                    current_type = parent_type
                    break
            if not parent_id:
                break
            current = parent_id
        raise ConnectorServiceError(
            "RESOURCE_NOT_BOUND", "The Notion item is outside the allowed project roots.", status_code=403
        )

    def _github_call(
        self,
        *,
        project_id: str,
        connection: Mapping[str, Any],
        resources: Mapping[str, Any],
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> Any:
        connection_id = str(connection["connection_id"])
        if tool_name == "github.list_repositories":
            return {
                "repositories": [
                    item
                    for item in resources["resources"]
                    if item["resource_type"] == "repository"
                ]
            }
        repository = self._repository(arguments.get("repository"))
        self._ensure_github_scope(
            project_id=project_id,
            connection_id=connection_id,
            repository=repository,
        )
        access_token, _ = self._access_token(connection)
        headers = self._github_headers(access_token)
        repo_path = f"{GITHUB_API}/repos/{repository}"
        if tool_name == "github.read_file":
            path = str(arguments.get("path") or "").strip().replace("\\", "/")
            if not path or len(path) > 1024 or path.startswith("/") or any(
                part in {"", ".", ".."} for part in path.split("/")
            ):
                raise ConnectorServiceError("INVALID_FILE_PATH", "The repository file path is invalid.")
            params: dict[str, Any] = {}
            if arguments.get("ref"):
                ref = str(arguments["ref"]).strip()
                if not _SAFE_REF.fullmatch(ref):
                    raise ConnectorServiceError("INVALID_GIT_REF", "The Git ref is invalid.")
                params["ref"] = ref
            payload = self._request_json(
                "GET",
                f"{repo_path}/contents/{quote(path, safe='/')}",
                headers=headers,
                params=params,
            )
            if not isinstance(payload, Mapping) or payload.get("type") != "file":
                raise ConnectorServiceError("RESOURCE_NOT_FILE", "The GitHub resource is not a file.", status_code=422)
            if int(payload.get("size") or 0) > 262_144:
                raise ConnectorServiceError("TOOL_RESULT_TOO_LARGE", "The repository file is larger than 256 KiB.", status_code=413)
            content = str(payload.get("content") or "")
            try:
                decoded = base64.b64decode(content.encode("ascii"), validate=False).decode(
                    "utf-8", errors="replace"
                )
            except (ValueError, UnicodeError) as exc:
                raise ConnectorServiceError("CONNECTOR_RESPONSE_INVALID", "GitHub returned invalid file content.", status_code=502) from exc
            return {
                "repository": repository,
                "path": str(payload.get("path") or path),
                "sha": str(payload.get("sha") or ""),
                "size": int(payload.get("size") or len(decoded.encode("utf-8"))),
                "content": decoded,
            }
        if tool_name == "github.list_commits":
            limit = self._positive_int(arguments.get("limit") or 30, "limit", maximum=100)
            params: dict[str, Any] = {"per_page": limit}
            if arguments.get("ref"):
                ref = str(arguments["ref"]).strip()
                if not _SAFE_REF.fullmatch(ref):
                    raise ConnectorServiceError("INVALID_GIT_REF", "The Git ref is invalid.")
                params["sha"] = ref
            return self._request_json("GET", f"{repo_path}/commits", headers=headers, params=params)
        if tool_name == "github.list_issues":
            state = str(arguments.get("state") or "open").casefold()
            if state not in {"open", "closed", "all"}:
                raise ConnectorServiceError("INVALID_TOOL_ARGUMENTS", "Issue state is invalid.")
            limit = self._positive_int(arguments.get("limit") or 30, "limit", maximum=100)
            return self._request_json(
                "GET", f"{repo_path}/issues", headers=headers, params={"state": state, "per_page": limit}
            )
        if tool_name == "github.get_issue":
            number = self._positive_int(arguments.get("issue_number"), "issue_number")
            return self._request_json("GET", f"{repo_path}/issues/{number}", headers=headers)
        if tool_name == "github.list_pull_requests":
            state = str(arguments.get("state") or "open").casefold()
            if state not in {"open", "closed", "all"}:
                raise ConnectorServiceError("INVALID_TOOL_ARGUMENTS", "Pull request state is invalid.")
            limit = self._positive_int(arguments.get("limit") or 30, "limit", maximum=100)
            return self._request_json(
                "GET", f"{repo_path}/pulls", headers=headers, params={"state": state, "per_page": limit}
            )
        if tool_name == "github.get_pull_request":
            number = self._positive_int(arguments.get("pull_number"), "pull_number")
            return self._request_json("GET", f"{repo_path}/pulls/{number}", headers=headers)
        if tool_name == "github.get_check_runs":
            ref = str(arguments.get("ref") or "").strip()
            if not _SAFE_REF.fullmatch(ref):
                raise ConnectorServiceError("INVALID_GIT_REF", "The Git ref is invalid.")
            return self._request_json(
                "GET", f"{repo_path}/commits/{quote(ref, safe='')}/check-runs", headers=headers
            )
        if tool_name == "github.create_issue":
            title = str(arguments.get("title") or "").strip()
            body = str(arguments.get("body") or "")
            if not title or len(title) > 256 or len(body) > 65_536:
                raise ConnectorServiceError("INVALID_TOOL_ARGUMENTS", "The Issue title or body is invalid.")
            return self._request_json(
                "POST",
                f"{repo_path}/issues",
                expected=(201,),
                mutation=True,
                headers=headers,
                json={"title": title, "body": body},
            )
        if tool_name == "github.update_issue":
            number = self._positive_int(arguments.get("issue_number"), "issue_number")
            body: dict[str, Any] = {}
            if "title" in arguments:
                title = str(arguments.get("title") or "").strip()
                if not title or len(title) > 256:
                    raise ConnectorServiceError("INVALID_TOOL_ARGUMENTS", "The Issue title is invalid.")
                body["title"] = title
            if "body" in arguments:
                content = str(arguments.get("body") or "")
                if len(content) > 65_536:
                    raise ConnectorServiceError("INVALID_TOOL_ARGUMENTS", "The Issue body is too long.")
                body["body"] = content
            if "state" in arguments:
                state = str(arguments.get("state") or "").casefold()
                if state not in {"open", "closed"}:
                    raise ConnectorServiceError("INVALID_TOOL_ARGUMENTS", "The Issue state is invalid.")
                body["state"] = state
            if not body:
                raise ConnectorServiceError("INVALID_TOOL_ARGUMENTS", "At least one Issue field is required.")
            return self._request_json(
                "PATCH",
                f"{repo_path}/issues/{number}",
                mutation=True,
                headers=headers,
                json=body,
            )
        if tool_name == "github.add_issue_comment":
            number = self._positive_int(arguments.get("issue_number"), "issue_number")
            body = str(arguments.get("body") or "").strip()
            if not body or len(body) > 65_536:
                raise ConnectorServiceError("INVALID_TOOL_ARGUMENTS", "The comment body is invalid.")
            return self._request_json(
                "POST",
                f"{repo_path}/issues/{number}/comments",
                expected=(201,),
                mutation=True,
                headers=headers,
                json={"body": body},
            )
        raise ConnectorServiceError("CONNECTOR_TOOL_NOT_FOUND", "The connector tool was not found.", status_code=404)

    def _notion_call(
        self,
        *,
        project_id: str,
        connection: Mapping[str, Any],
        resources: Mapping[str, Any],
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> Any:
        if tool_name == "notion.search":
            query = str(arguments.get("query") or "").strip().casefold()
            return {
                "results": [
                    item
                    for item in resources["resources"]
                    if not query
                    or query in item["display_label"].casefold()
                    or query in item["resource_id"].casefold()
                ]
            }
        access_token, _ = self._access_token(connection)
        headers = self._notion_headers(access_token)
        if tool_name == "notion.retrieve_page":
            page_id = self._notion_id(arguments.get("page_id"))
            self._ensure_notion_scope(
                project_id=project_id, connection=connection, resource_id=page_id
            )
            page = self._request_json(
                "GET", f"{NOTION_API}/pages/{quote(page_id, safe='')}", headers=headers
            )
            children = self._request_json(
                "GET",
                f"{NOTION_API}/blocks/{quote(page_id, safe='')}/children",
                headers=headers,
                params={"page_size": 100},
            )
            return {"page": page, "children": children}
        if tool_name == "notion.retrieve_database":
            database_id = self._notion_id(arguments.get("database_id"))
            self._ensure_notion_scope(
                project_id=project_id,
                connection=connection,
                resource_id=database_id,
                resource_type="database",
            )
            return self._request_json(
                "GET", f"{NOTION_API}/databases/{quote(database_id, safe='')}", headers=headers
            )
        if tool_name == "notion.create_page":
            parent_type = str(arguments.get("parent_type") or "").casefold()
            if parent_type not in {"page_id", "database_id"}:
                raise ConnectorServiceError("INVALID_TOOL_ARGUMENTS", "The Notion parent type is invalid.")
            parent_id = self._notion_id(arguments.get("parent_id"))
            self._ensure_notion_scope(
                project_id=project_id,
                connection=connection,
                resource_id=parent_id,
                resource_type="database" if parent_type == "database_id" else "page",
            )
            properties = arguments.get("properties")
            children = arguments.get("children", [])
            if not isinstance(properties, Mapping) or not isinstance(children, list) or len(children) > 100:
                raise ConnectorServiceError("INVALID_TOOL_ARGUMENTS", "The Notion page payload is invalid.")
            body = _bounded_json(
                {"parent": {parent_type: parent_id}, "properties": properties, "children": children}
            )
            return self._request_json(
                "POST",
                f"{NOTION_API}/pages",
                expected=(200, 201),
                mutation=True,
                headers=headers,
                json=body,
            )
        if tool_name == "notion.update_page":
            page_id = self._notion_id(arguments.get("page_id"))
            self._ensure_notion_scope(
                project_id=project_id, connection=connection, resource_id=page_id
            )
            properties = arguments.get("properties")
            if not isinstance(properties, Mapping) or not properties:
                raise ConnectorServiceError("INVALID_TOOL_ARGUMENTS", "Notion properties are required.")
            body = _bounded_json({"properties": properties})
            return self._request_json(
                "PATCH",
                f"{NOTION_API}/pages/{quote(page_id, safe='')}",
                mutation=True,
                headers=headers,
                json=body,
            )
        if tool_name == "notion.append_blocks":
            page_id = self._notion_id(arguments.get("page_id"))
            self._ensure_notion_scope(
                project_id=project_id, connection=connection, resource_id=page_id
            )
            children = arguments.get("children")
            if not isinstance(children, list) or not children or len(children) > 100:
                raise ConnectorServiceError("INVALID_TOOL_ARGUMENTS", "One to 100 Notion blocks are required.")
            body = _bounded_json({"children": children})
            return self._request_json(
                "PATCH",
                f"{NOTION_API}/blocks/{quote(page_id, safe='')}/children",
                mutation=True,
                headers=headers,
                json=body,
            )
        raise ConnectorServiceError("CONNECTOR_TOOL_NOT_FOUND", "The connector tool was not found.", status_code=404)

    def resolve_tool_invocation(
        self,
        project_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        verify_remote_scope: bool = False,
    ) -> dict[str, Any]:
        """Resolve immutable approval/scope facts without provider I/O.

        The host ToolDispatcher can digest this payload, ask for approval and
        call it again immediately before execution to detect binding changes.
        """

        definitions = {
            item["function"]["name"]: item for item in TOOL_DEFINITIONS
        }
        if tool_name not in definitions:
            raise ConnectorServiceError(
                "CONNECTOR_TOOL_NOT_FOUND", "The connector tool was not found.", status_code=404
            )
        if not isinstance(arguments, Mapping):
            raise ConnectorServiceError("INVALID_TOOL_ARGUMENTS", "Tool arguments must be an object.")
        safe_arguments = _bounded_json(arguments, maximum=250_000)
        definition = definitions[tool_name]
        allowed_keys = set(definition["function"]["parameters"]["properties"])
        unknown = sorted(set(safe_arguments) - allowed_keys)
        if unknown:
            raise ConnectorServiceError(
                "INVALID_TOOL_ARGUMENTS", f"Unknown tool argument: {unknown[0]}."
            )
        missing = [
            key
            for key in definition["function"]["parameters"].get("required", [])
            if key not in safe_arguments
        ]
        if missing:
            raise ConnectorServiceError(
                "INVALID_TOOL_ARGUMENTS", f"Missing tool argument: {missing[0]}."
            )
        connector = tool_name.split(".", 1)[0]
        write = tool_name in _WRITE_TOOLS
        requested_connection = str(safe_arguments.pop("connection_id", "") or "") or None
        connection, resources = self._resolve_tool_connection(
            project_id=project_id,
            connector_id=connector,
            requested_id=requested_connection,
            write=write,
        )
        resource_type = "connector"
        resource_id = "*"
        if connector == "github" and safe_arguments.get("repository"):
            resource_type = "repository"
            resource_id = self._repository(safe_arguments["repository"])
            self._ensure_github_scope(
                project_id=project_id,
                connection_id=connection["connection_id"],
                repository=resource_id,
            )
        elif connector == "notion":
            for key, kind in (
                ("page_id", "page"),
                ("database_id", "database"),
                ("parent_id", str(safe_arguments.get("parent_type") or "parent").removesuffix("_id")),
            ):
                if safe_arguments.get(key):
                    resource_type = kind
                    resource_id = self._notion_id(safe_arguments[key])
                    break
            if verify_remote_scope and resource_id != "*":
                self._ensure_notion_scope(
                    project_id=project_id,
                    connection=connection,
                    resource_id=resource_id,
                    resource_type=(
                        "database"
                        if resource_type in {"database", "database_id"}
                        else "page"
                    ),
                )
        return {
            "connector_id": connector,
            "extension_id": f"connector.{connector}",
            "connection_id": connection["connection_id"],
            "project_id": project_id,
            "tool_name": tool_name,
            "risk": "write" if write else "read",
            "approval_required": write,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "resource_revision": resources["revision"],
            "resource_roots": [
                {
                    "resource_type": item["resource_type"],
                    "resource_id": item["resource_id"],
                }
                for item in resources["resources"]
            ],
            "arguments": safe_arguments,
            "arguments_sha256": _digest(safe_arguments),
        }

    def execute_tool(
        self,
        project_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        approved: bool = False,
    ) -> dict[str, Any]:
        invocation = self.resolve_tool_invocation(project_id, tool_name, arguments)
        connector = invocation["connector_id"]
        write = invocation["approval_required"]
        safe_arguments = invocation["arguments"]
        connection = self.store.get_connection(invocation["connection_id"])
        if connection is None:
            raise ConnectorNotFoundError("The connector connection was not found.")
        resources = self.store.list_resource_bindings(
            project_id=project_id, connection_id=connection["connection_id"]
        )
        if write and not approved:
            raise ConnectorServiceError(
                "CONNECTOR_WRITE_APPROVAL_REQUIRED",
                "This external write requires a single-use approval.",
                status_code=409,
                recoverable=True,
            )
        audit_details = {
            "tool_name": tool_name,
            "arguments_sha256": invocation["arguments_sha256"],
            "resource_revision": resources["revision"],
            "approved": approved,
        }
        try:
            if connector == "github":
                result = self._github_call(
                    project_id=project_id,
                    connection=connection,
                    resources=resources,
                    tool_name=tool_name,
                    arguments=safe_arguments,
                )
            else:
                result = self._notion_call(
                    project_id=project_id,
                    connection=connection,
                    resources=resources,
                    tool_name=tool_name,
                    arguments=safe_arguments,
                )
            safe_result = _bounded_json(result, maximum=1_048_576)
            self.store.audit(
                connector_id=connector,
                connection_id=connection["connection_id"],
                project_id=project_id,
                action="tool.execute",
                status="completed",
                details=audit_details,
            )
            return {
                "connector_id": connector,
                "connection_id": connection["connection_id"],
                "tool_name": tool_name,
                "resource_revision": resources["revision"],
                "result": safe_result,
            }
        except Exception as exc:
            self.store.audit(
                connector_id=connector,
                connection_id=connection["connection_id"],
                project_id=project_id,
                action="tool.execute",
                status="failed",
                details=audit_details,
                error_code=getattr(exc, "code", "CONNECTOR_TOOL_FAILED"),
            )
            raise


__all__ = [
    "CONNECTOR_CATALOG",
    "TOOL_DEFINITIONS",
    "ConnectorService",
    "ConnectorServiceError",
]
