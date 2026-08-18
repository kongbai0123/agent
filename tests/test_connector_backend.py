from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from api.routes.connectors import (
    build_connector_callback_router,
    build_connectors_router,
)
from connector_secrets import ConnectorSecretStore
from connector_service import ConnectorService, ConnectorServiceError
from connector_store import ConnectorConflictError, ConnectorStore
from tool_runtime import (
    ToolDispatcher,
    ToolExecutionError,
    ToolExecutionUnknownError,
    ToolRegistry,
    ToolScopeState,
)


def _connection_factory(path: Path):
    @contextmanager
    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    return connect


def _protect(value: str) -> str:
    return base64.b64encode(f"sealed:{value}".encode()).decode()


def _unprotect(value: str) -> str:
    decoded = base64.b64decode(value).decode()
    assert decoded.startswith("sealed:")
    return decoded.removeprefix("sealed:")


class FakeResponse:
    def __init__(self, payload=None, *, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code
        self.content = b"" if status_code == 204 else json.dumps(payload).encode()

    def json(self):
        return self.payload


class InvalidJsonResponse(FakeResponse):
    def __init__(self, *, status_code: int):
        super().__init__(None, status_code=status_code)
        self.content = b"{"

    def json(self):
        raise ValueError("invalid provider JSON")


class FakeHttp:
    def __init__(self):
        self.trust_env = True
        self.responses = []
        self.calls = []

    def add(self, method: str, url: str, payload=None, *, status_code: int = 200):
        self.responses.append((method, url, FakeResponse(payload, status_code=status_code)))

    def add_error(self, method: str, url: str, error: requests.RequestException):
        self.responses.append((method, url, error))

    def add_invalid_json(self, method: str, url: str, *, status_code: int):
        self.responses.append((method, url, InvalidJsonResponse(status_code=status_code)))

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        assert self.responses, f"Unexpected HTTP request: {method} {url}"
        expected_method, expected_url, response = self.responses.pop(0)
        assert (method, url) == (expected_method, expected_url)
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.fixture()
def components(tmp_path: Path):
    store = ConnectorStore(_connection_factory(tmp_path / "connector.db"))
    secrets = ConnectorSecretStore(
        tmp_path / "secrets" / "connectors.json",
        protect=_protect,
        unprotect=_unprotect,
    )
    http = FakeHttp()
    service = ConnectorService(
        store=store,
        secrets_store=secrets,
        http_session=http,
        project_exists=lambda project_id: project_id == "project-1",
    )
    service.initialize()
    return service, store, secrets, http, tmp_path


def _configure(service: ConnectorService, connector_id: str):
    return service.configure_auth_profile(
        connector_id,
        client_id=f"{connector_id}-client",
        client_secret=f"{connector_id}-super-secret",
        callback_uri=f"http://127.0.0.1:8765/oauth/callback/{connector_id}",
    )


def _state(started: dict) -> str:
    return parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]


def _connect_github(service: ConnectorService, http: FakeHttp) -> dict:
    _configure(service, "github")
    started = service.start_oauth("github")
    http.add(
        "POST",
        "https://github.com/login/oauth/access_token",
        {
            "access_token": "github-access-token",
            "refresh_token": "github-refresh-token",
            "token_type": "bearer",
            "expires_in": 28_800,
        },
    )
    http.add(
        "GET",
        "https://api.github.com/user",
        {
            "id": 42,
            "login": "octocat",
            "name": "Octo Cat",
            "avatar_url": "https://avatars.example/42",
            "html_url": "https://github.com/octocat",
        },
    )
    return service.complete_oauth("github", state=_state(started), code="one-time-code")


def _connect_notion(service: ConnectorService, http: FakeHttp) -> dict:
    _configure(service, "notion")
    started = service.start_oauth("notion")
    http.add(
        "POST",
        "https://api.notion.com/v1/oauth/token",
        {
            "access_token": "notion-access-token",
            "refresh_token": "notion-refresh-token",
            "token_type": "bearer",
            "workspace_id": "workspace-1",
            "workspace_name": "Workbench Docs",
            "bot_id": "bot-1",
        },
    )
    http.add(
        "GET",
        "https://api.notion.com/v1/users/me",
        {"id": "bot-1", "name": "Workbench"},
    )
    return service.complete_oauth("notion", state=_state(started), code="one-time-code")


def _github_write_dispatcher(
    service: ConnectorService,
    store: ConnectorStore,
    http: FakeHttp,
) -> tuple[ToolDispatcher, str]:
    connection = _connect_github(service, http)
    connection_id = connection["connection_id"]
    service.put_project_binding(
        project_id="project-1",
        connection_id=connection_id,
        enabled=True,
        mode="read_write",
    )
    store.replace_resource_bindings(
        project_id="project-1",
        connection_id=connection_id,
        expected_revision=0,
        resources=[
            {
                "resource_type": "repository",
                "resource_id": "openai/example",
                "display_label": "openai/example",
            }
        ],
    )
    definition = next(
        item
        for item in service.runtime_tool_definitions(
            "project-1", {"connector.github": "a" * 64}
        )
        if item.name == "github.create_issue"
    )

    def scope(_definition, call):
        invocation = service.resolve_tool_invocation(
            call.project_id,
            call.tool_name,
            call.arguments,
        )
        return ToolScopeState(
            installed=True,
            trusted=True,
            enabled=True,
            healthy=True,
            resource_allowed=True,
            manifest_sha256="a" * 64,
            resource_revision=invocation["resource_revision"],
            connection_enabled=True,
            connection_id=invocation["connection_id"],
            resource_id=invocation["resource_id"],
        )

    return (
        ToolDispatcher(ToolRegistry((definition,)), scope_resolver=scope),
        connection_id,
    )


def test_connector_secret_vault_is_atomic_and_contains_no_plaintext(tmp_path: Path):
    path = tmp_path / "private" / "connectors.json"
    vault = ConnectorSecretStore(path, protect=_protect, unprotect=_unprotect)

    vault.set("connection", "conn_1", {"access_token": "never-write-this-token"})

    assert vault.get("connection", "conn_1") == {
        "access_token": "never-write-this-token"
    }
    serialized = path.read_text(encoding="utf-8")
    assert "never-write-this-token" not in serialized
    assert not list(path.parent.glob("*.tmp-*"))
    assert vault.delete_record("conn_1") == 1
    assert vault.get("connection", "conn_1") == {}


def test_oauth_state_is_single_use_and_sqlite_contains_no_secrets(components):
    service, store, secrets, http, tmp_path = components
    _configure(service, "github")
    started = service.start_oauth("github")
    state = _state(started)
    http.add(
        "POST",
        "https://github.com/login/oauth/access_token",
        {
            "access_token": "top-secret-access",
            "refresh_token": "top-secret-refresh",
            "expires_in": 3600,
        },
    )
    http.add(
        "GET",
        "https://api.github.com/user",
        {"id": 7, "login": "safe-user"},
    )

    connection = service.complete_oauth("github", state=state, code="code-1")

    assert connection["connector_id"] == "github"
    assert "access_token" not in json.dumps(connection)
    assert secrets.get("connection", connection["connection_id"])["access_token"] == "top-secret-access"
    database_bytes = (tmp_path / "connector.db").read_bytes()
    assert b"top-secret-access" not in database_bytes
    assert b"top-secret-refresh" not in database_bytes
    with pytest.raises(ConnectorConflictError) as replay:
        service.complete_oauth("github", state=state, code="code-2")
    assert replay.value.code == "OAUTH_STATE_REPLAYED"


def test_initialize_invalidates_every_incomplete_oauth_flow_and_keeps_connections(
    components,
):
    service, store, secrets, http, tmp_path = components
    connection = _connect_github(service, http)

    pending = service.start_oauth("github")
    _configure(service, "notion")
    exchanging = service.start_oauth("notion")
    store.claim_oauth_flow(
        connector_id="notion",
        raw_state=_state(exchanging),
    )
    assert secrets.exists("flow", pending["flow_id"])
    assert secrets.exists("flow", exchanging["flow_id"])
    assert secrets.exists("connection", connection["connection_id"])

    result = service.initialize()

    assert result == {
        "expired_oauth_flows": 0,
        "invalidated_oauth_flows": 2,
        "cleaned_flow_secrets": 2,
    }
    assert not secrets.exists("flow", pending["flow_id"])
    assert not secrets.exists("flow", exchanging["flow_id"])
    assert secrets.exists("connection", connection["connection_id"])
    assert service.get_connection(connection["connection_id"])["status"] == "connected"

    with sqlite3.connect(tmp_path / "connector.db") as db:
        rows = {
            row[0]: (row[1], row[2])
            for row in db.execute(
                """
                SELECT flow_id, status, error_code
                FROM connector_oauth_flows
                WHERE flow_id IN (?, ?)
                """,
                (pending["flow_id"], exchanging["flow_id"]),
            )
        }
    assert rows == {
        pending["flow_id"]: (
            "expired",
            "OAUTH_FLOW_INVALIDATED_ON_RESTART",
        ),
        exchanging["flow_id"]: (
            "expired",
            "OAUTH_FLOW_INVALIDATED_ON_RESTART",
        ),
    }


def test_disabled_extension_consumes_oauth_state_before_any_token_exchange(components):
    service, _store, _secrets, http, _tmp_path = components
    _configure(service, "github")
    started = service.start_oauth("github")
    state = _state(started)

    with pytest.raises(RuntimeError, match="disabled"):
        service.complete_oauth(
            "github",
            state=state,
            code="must-not-be-exchanged",
            authorize=lambda _connector_id: (_ for _ in ()).throw(
                RuntimeError("disabled")
            ),
        )

    assert http.calls == []
    with pytest.raises(ConnectorConflictError) as replay:
        service.complete_oauth("github", state=state, code="replay")
    assert replay.value.code == "OAUTH_STATE_REPLAYED"


def test_connector_management_writes_require_enabled_extension(components):
    service, _store, _secrets, _http, _tmp_path = components
    calls = []

    def gate(extension_id, project_id=None):
        calls.append((extension_id, project_id))
        return False

    app = FastAPI()
    app.include_router(
        build_connectors_router(
            service=service,
            require_local=lambda _request: None,
            error_payload=lambda code, message, **kwargs: {
                "success": False,
                "code": code,
                "message": message,
                **kwargs,
            },
            require_extension=gate,
        )
    )
    client = TestClient(app)
    denied = client.put(
        "/api/connectors/github/auth-profile",
        json={
            "client_id": "client",
            "client_secret": "secret-value",
            "callback_uri": "http://127.0.0.1:8765/oauth/callback/github",
        },
    )
    assert denied.status_code == 409
    assert denied.json()["detail"]["code"] == "EXTENSION_DISABLED"
    assert service.auth_profile_status("github") is None
    assert calls == [("connector.github", None)]


def test_project_resource_revision_and_github_write_approval(components):
    service, store, _secrets, http, _tmp_path = components
    connection = _connect_github(service, http)
    connection_id = connection["connection_id"]
    assert service.extension_health("connector.github")[0] == "ready"
    unbound_listing = service.list_connections(project_id="project-1")
    assert unbound_listing[0]["connection_id"] == connection_id
    assert unbound_listing[0]["binding"] is None
    service.put_project_binding(
        project_id="project-1",
        connection_id=connection_id,
        enabled=True,
        mode="read_write",
    )
    http.add(
        "GET",
        "https://api.github.com/repos/openai/example",
        {
            "full_name": "openai/example",
            "private": True,
            "default_branch": "main",
            "html_url": "https://github.com/openai/example",
        },
    )
    bound = service.replace_resources(
        project_id="project-1",
        connection_id=connection_id,
        expected_revision=0,
        resources=[
            {
                "resource_type": "repository",
                "resource_id": "openai/example",
                "display_label": "openai/example",
            }
        ],
    )
    assert bound["revision"] == 1
    assert bound["mode"] == "read_write"
    assert bound["binding"]["revision"] == 1
    project_listing = service.list_connections(project_id="project-1")
    assert project_listing[0]["binding"]["mode"] == "read_write"
    with pytest.raises(ConnectorConflictError) as stale:
        store.replace_resource_bindings(
            project_id="project-1",
            connection_id=connection_id,
            expected_revision=0,
            resources=[],
        )
    assert stale.value.code == "RESOURCE_BINDING_REVISION_CONFLICT"

    app = FastAPI()
    app.include_router(
        build_connectors_router(
            service=service,
            require_local=lambda _request: None,
            error_payload=lambda code, message, **kwargs: {
                "success": False,
                "code": code,
                "message": message,
                **kwargs,
            },
        )
    )
    stale_response = TestClient(app).put(
        f"/api/projects/project-1/connections/{connection_id}/resources",
        json={"revision": 0, "resources": []},
    )
    assert stale_response.status_code == 409
    assert stale_response.json()["detail"]["code"] == "RESOURCE_BINDING_REVISION_CONFLICT"

    definitions = service.list_tool_definitions("project-1")
    assert "github.create_issue" in {
        item["function"]["name"] for item in definitions
    }
    runtime_definitions = service.runtime_tool_definitions(
        "project-1", {"connector.github": "a" * 64}
    )
    assert {item.name for item in runtime_definitions} == {
        item["function"]["name"] for item in definitions
    }
    resolved = service.resolve_tool_invocation(
        "project-1",
        "github.create_issue",
        {"repository": "openai/example", "title": "Safe issue"},
    )
    assert resolved["connection_id"] == connection_id
    assert resolved["resource_revision"] == 1
    assert resolved["approval_required"] is True

    call_count = len(http.calls)
    with pytest.raises(ConnectorServiceError) as approval:
        service.execute_tool(
            "project-1",
            "github.create_issue",
            {"repository": "openai/example", "title": "Safe issue"},
        )
    assert approval.value.code == "CONNECTOR_WRITE_APPROVAL_REQUIRED"
    assert len(http.calls) == call_count

    http.add(
        "POST",
        "https://api.github.com/repos/openai/example/issues",
        {"number": 10, "title": "Safe issue"},
        status_code=201,
    )
    result = service.execute_tool(
        "project-1",
        "github.create_issue",
        {"repository": "openai/example", "title": "Safe issue"},
        approved=True,
    )
    assert result["result"]["number"] == 10
    assert "github-access-token" not in json.dumps(store.list_audits("github"))


@pytest.mark.parametrize(
    ("transport_error", "provider_status"),
    [
        (requests.Timeout("write timed out"), None),
        (requests.ConnectionError("connection interrupted"), None),
        (requests.exceptions.ChunkedEncodingError("response stream interrupted"), None),
        (None, 500),
        (None, 503),
    ],
    ids=[
        "timeout",
        "connection-error",
        "request-interrupted",
        "http-500",
        "http-503",
    ],
)
def test_uncertain_provider_write_maps_to_execution_unknown(
    components,
    transport_error,
    provider_status,
):
    service, store, _secrets, http, _tmp_path = components
    dispatcher, connection_id = _github_write_dispatcher(service, store, http)
    url = "https://api.github.com/repos/openai/example/issues"
    if transport_error is not None:
        http.add_error("POST", url, transport_error)
    else:
        http.add("POST", url, {"message": "provider failed"}, status_code=provider_status)

    with pytest.raises(ToolExecutionUnknownError) as unknown:
        asyncio.run(
            dispatcher.execute(
                run_id="run-unknown",
                project_id="project-1",
                tool_name="github.create_issue",
                arguments={"repository": "openai/example", "title": "One write"},
                connection_id=connection_id,
                resource_id="openai/example",
                approval_callback=lambda _request: True,
            )
        )

    assert isinstance(unknown.value.__cause__, ConnectorServiceError)
    assert unknown.value.__cause__.execution_state_unknown is True
    assert http.responses == []


def test_successful_provider_write_with_invalid_json_maps_to_execution_unknown(
    components,
):
    service, store, _secrets, http, _tmp_path = components
    dispatcher, connection_id = _github_write_dispatcher(service, store, http)
    http.add_invalid_json(
        "POST",
        "https://api.github.com/repos/openai/example/issues",
        status_code=201,
    )

    with pytest.raises(ToolExecutionUnknownError) as unknown:
        asyncio.run(
            dispatcher.execute(
                run_id="run-invalid-response",
                project_id="project-1",
                tool_name="github.create_issue",
                arguments={"repository": "openai/example", "title": "One write"},
                connection_id=connection_id,
                resource_id="openai/example",
                approval_callback=lambda _request: True,
            )
        )

    assert isinstance(unknown.value.__cause__, ConnectorServiceError)
    assert unknown.value.__cause__.code == "CONNECTOR_RESPONSE_INVALID"
    assert unknown.value.__cause__.execution_state_unknown is True


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_non_mutating_transport_interruption_is_not_execution_unknown(
    components,
    method,
):
    service, _store, _secrets, http, _tmp_path = components
    url = "https://api.example.test/read"
    http.add_error(
        method,
        url,
        requests.exceptions.ChunkedEncodingError("read stream interrupted"),
    )

    with pytest.raises(ConnectorServiceError) as failed:
        service._request_json(method, url, mutation=False)

    assert failed.value.execution_state_unknown is False


@pytest.mark.parametrize("provider_status", [400, 401, 403, 404, 422, 429])
def test_definite_provider_4xx_write_is_not_execution_unknown(
    components,
    provider_status,
):
    service, store, _secrets, http, _tmp_path = components
    dispatcher, connection_id = _github_write_dispatcher(service, store, http)
    http.add(
        "POST",
        "https://api.github.com/repos/openai/example/issues",
        {"message": "request rejected"},
        status_code=provider_status,
    )

    with pytest.raises(ToolExecutionError) as failed:
        asyncio.run(
            dispatcher.execute(
                run_id="run-definite",
                project_id="project-1",
                tool_name="github.create_issue",
                arguments={"repository": "openai/example", "title": "One write"},
                connection_id=connection_id,
                resource_id="openai/example",
                approval_callback=lambda _request: True,
            )
        )

    assert type(failed.value) is ToolExecutionError
    assert isinstance(failed.value.__cause__, ConnectorServiceError)
    assert failed.value.__cause__.execution_state_unknown is False
    assert http.responses == []


def test_notion_bound_root_read_and_write_gate(components):
    service, _store, _secrets, http, _tmp_path = components
    connection = _connect_notion(service, http)
    connection_id = connection["connection_id"]
    page_id = "12345678-1234-1234-1234-1234567890ab"
    service.put_project_binding(
        project_id="project-1",
        connection_id=connection_id,
        enabled=True,
        mode="read_write",
    )
    page_payload = {
        "object": "page",
        "id": page_id,
        "url": "https://notion.so/page",
        "parent": {"type": "workspace", "workspace": True},
        "properties": {
            "Name": {
                "type": "title",
                "title": [{"plain_text": "Project knowledge"}],
            }
        },
    }
    http.add("GET", f"https://api.notion.com/v1/pages/{page_id}", page_payload)
    service.replace_resources(
        project_id="project-1",
        connection_id=connection_id,
        expected_revision=0,
        resources=[
            {
                "resource_type": "page",
                "resource_id": page_id,
                "display_label": "Project knowledge",
            }
        ],
    )

    http.add("GET", f"https://api.notion.com/v1/pages/{page_id}", page_payload)
    http.add(
        "GET",
        f"https://api.notion.com/v1/blocks/{page_id}/children",
        {"results": [{"object": "block", "type": "paragraph"}], "has_more": False},
    )
    read = service.execute_tool(
        "project-1", "notion.retrieve_page", {"page_id": page_id}
    )
    assert read["result"]["page"]["id"] == page_id

    call_count = len(http.calls)
    with pytest.raises(ConnectorServiceError) as approval:
        service.execute_tool(
            "project-1",
            "notion.append_blocks",
            {"page_id": page_id, "children": [{"object": "block", "type": "paragraph"}]},
        )
    assert approval.value.code == "CONNECTOR_WRITE_APPROVAL_REQUIRED"
    assert len(http.calls) == call_count


def test_router_contract_and_callbacks_are_secret_free(components):
    service, _store, secret_store, http, _tmp_path = components
    app = FastAPI()
    app.include_router(
        build_connectors_router(
            service=service,
            require_local=lambda _request: None,
            error_payload=lambda code, message, **kwargs: {
                "success": False,
                "code": code,
                "message": message,
                **kwargs,
            },
        )
    )
    app.include_router(build_connector_callback_router(service=service))
    client = TestClient(app)

    catalog = client.get("/api/connectors").json()
    assert {item["id"] for item in catalog["connectors"]} == {"github", "notion"}
    profile_response = client.put(
        "/api/connectors/github/auth-profile",
        json={
            "client_id": "client-id",
            "client_secret": "route-client-secret",
            "callback_uri": "http://127.0.0.1:8765/oauth/callback/github",
        },
    )
    assert profile_response.status_code == 200
    assert "route-client-secret" not in profile_response.text
    profile_id = profile_response.json()["profile"]["profile_id"]
    preserved = client.put(
        "/api/connectors/github/auth-profile",
        json={
            "client_id": "updated-client-id",
            "client_secret": "",
            "callback_uri": "http://127.0.0.1:8765/oauth/callback/github",
        },
    )
    assert preserved.status_code == 200
    assert preserved.json()["profile"]["client_id"] == "updated-client-id"
    assert secret_store.get("profile", profile_id)["client_secret"] == "route-client-secret"
    omitted = client.put(
        "/api/connectors/github/auth-profile",
        json={
            "client_id": "updated-again",
            "callback_uri": "http://127.0.0.1:8765/oauth/callback/github",
        },
    )
    assert omitted.status_code == 200
    assert secret_store.get("profile", profile_id)["client_secret"] == "route-client-secret"
    missing_first_secret = client.put(
        "/api/connectors/notion/auth-profile",
        json={
            "client_id": "notion-client",
            "callback_uri": "http://127.0.0.1:8765/oauth/callback/notion",
        },
    )
    assert missing_first_secret.status_code == 400
    assert missing_first_secret.json()["detail"]["code"] == "INVALID_CLIENT_SECRET"
    started = client.post("/api/connectors/github/oauth/start", json={}).json()
    state = _state(started)
    http.add(
        "POST",
        "https://github.com/login/oauth/access_token",
        {"access_token": "route-access-token", "expires_in": 3600},
    )
    http.add("GET", "https://api.github.com/user", {"id": 99, "login": "route-user"})

    callback = client.get(
        "/oauth/callback/github", params={"state": state, "code": "route-code"}
    )
    assert callback.status_code == 200
    assert "route-access-token" not in callback.text
    assert callback.headers["cache-control"].startswith("no-store")
    replay = client.get(
        "/oauth/callback/github", params={"state": state, "code": "route-code"}
    )
    assert replay.status_code == 400


def test_invalid_callback_must_be_exact_loopback(components):
    service, _store, _secrets, _http, _tmp_path = components
    with pytest.raises(ConnectorServiceError) as invalid:
        service.configure_auth_profile(
            "github",
            client_id="client",
            client_secret="secret",
            callback_uri="https://example.com/oauth/callback/github",
        )
    assert invalid.value.code == "INVALID_CALLBACK_URI"
    assert service.extension_health("github") == (
        "unavailable",
        {"reason": "no_account_connected"},
    )
