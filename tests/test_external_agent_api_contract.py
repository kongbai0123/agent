from __future__ import annotations

import base64
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from api.routes.external_agent_api import build_external_agent_api_router
from connector_secrets import ConnectorSecretStore
from external_agent_api import ExternalAgentApiService, ExternalAgentApiStore


APP_SOURCE = (ROOT / "backend" / "app.py").read_text(encoding="utf-8")
LOCAL_SESSION_SOURCE = (ROOT / "backend" / "local_session.py").read_text(
    encoding="utf-8"
)
SERVICE_SOURCE = (ROOT / "backend" / "external_agent_api.py").read_text(
    encoding="utf-8"
)
ROUTE_SOURCE = (
    ROOT / "backend" / "api" / "routes" / "external_agent_api.py"
).read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
INTEGRATION_JS = (ROOT / "frontend" / "integration-center.js").read_text(
    encoding="utf-8"
)
README = (ROOT / "README.md").read_text(encoding="utf-8")
GUIDE = (ROOT / "docs" / "EXTERNAL_AGENT_API.md").read_text(encoding="utf-8")


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
    return base64.b64decode(value).decode().removeprefix("sealed:")


def _error_payload(code, message, detail=None, recoverable=True, suggestions=None):
    return {
        "success": False,
        "code": code,
        "message": message,
        "recoverable": recoverable,
    }


def test_external_key_contract_is_installation_bound_and_one_time() -> None:
    assert "secrets.token_hex(6)" in SERVICE_SOURCE
    assert 'secret_value = f"wbk_{tag}_{random_part}"' in SERVICE_SOURCE
    assert "hashlib.sha256" in SERVICE_SOURCE
    assert "key_digest" in SERVICE_SOURCE
    assert '"secret": secret_value' in SERVICE_SOURCE
    assert "完整金鑰只會顯示這一次" in SERVICE_SOURCE
    assert '"secret"' not in SERVICE_SOURCE[
        SERVICE_SOURCE.index("def _metadata(") : SERVICE_SOURCE.index(
            "def list_keys(", SERVICE_SOURCE.index("def _metadata(")
        )
    ]
    assert "credential_recovery_required" in SERVICE_SOURCE
    assert "RESET_EXTERNAL_API" in SERVICE_SOURCE


def test_public_routes_require_bearer_and_run_creation_requires_idempotency() -> None:
    assert 'request.headers.get("authorization")' in ROUTE_SOURCE
    assert 'value.startswith("Bearer ")' in SERVICE_SOURCE
    assert 'request.headers.get("idempotency-key")' in ROUTE_SOURCE
    assert "8 <= len(candidate) <= 128" in SERVICE_SOURCE
    assert 'scope="capabilities:read"' in ROUTE_SOURCE
    assert 'scope="runs:create"' in ROUTE_SOURCE
    assert 'scope="runs:read"' in ROUTE_SOURCE
    assert 'scope="runs:cancel"' in ROUTE_SOURCE

    for path in (
        "/api/public/v1/capabilities",
        "/api/public/v1/runs",
        "/api/public/v1/runs/{run_id}",
        "/api/public/v1/runs/{run_id}/cancel",
    ):
        assert path in ROUTE_SOURCE


def test_public_http_boundary_rejects_missing_auth_and_idempotency(tmp_path: Path) -> None:
    service = ExternalAgentApiService(
        store=ExternalAgentApiStore(_connection_factory(tmp_path / "external.db")),
        secret_store=ConnectorSecretStore(
            tmp_path / "external-secrets.json",
            protect=_protect,
            unprotect=_unprotect,
        ),
        project_exists=lambda project_id: project_id == "project-a",
        policy_guard=lambda _project_id, _scope: True,
        installation_label="契約測試工作站",
    )
    service.initialize()
    issued = service.issue_key(
        name="契約測試",
        project_id="project-a",
        scopes=["runs:create", "capabilities:read"],
        expires_at=None,
        rate_limit_per_minute=60,
        request_limit_daily=1000,
    )

    app = FastAPI()
    app.include_router(
        build_external_agent_api_router(
            service=service,
            require_local=lambda _request: None,
            error_payload=_error_payload,
            submit_run=lambda *_args: {},
            get_run=lambda *_args: {},
            cancel_run=lambda *_args: {},
            capabilities=lambda auth: {
                "project_id": auth["project_id"],
                "chat": True,
            },
        )
    )
    client = TestClient(app)

    no_auth = client.get("/api/public/v1/capabilities")
    assert no_auth.status_code == 401
    assert no_auth.headers["www-authenticate"] == "Bearer"
    assert no_auth.json()["detail"]["code"] == "EXTERNAL_API_AUTH_REQUIRED"

    no_idempotency = client.post(
        "/api/public/v1/runs",
        headers={"Authorization": f"Bearer {issued['secret']}"},
        json={"message": "執行契約測試"},
    )
    assert no_idempotency.status_code == 422
    assert (
        no_idempotency.json()["detail"]["code"]
        == "EXTERNAL_API_IDEMPOTENCY_KEY_INVALID"
    )
    assert issued["secret"] not in no_idempotency.text


def test_public_boundary_is_narrow_and_management_routes_remain_local() -> None:
    assert '"/api/public/v1/"' in LOCAL_SESSION_SOURCE
    prefix_block = LOCAL_SESSION_SOURCE[
        LOCAL_SESSION_SOURCE.index("SERVICE_AUTH_API_PREFIXES =") :
        LOCAL_SESSION_SOURCE.index("TOKEN_FILENAME", LOCAL_SESSION_SOURCE.index("SERVICE_AUTH_API_PREFIXES ="))
    ]
    assert "/api/integration-center/" not in prefix_block
    assert "require_local(request)" in ROUTE_SOURCE
    assert '@router.post("/api/integration-center/api-keys"' in ROUTE_SOURCE
    assert '@router.post("/api/integration-center/installation/reset")' in ROUTE_SOURCE


def test_public_mutation_body_is_bounded_before_route_parsing() -> None:
    assert "_PUBLIC_AGENT_API_BODY_LIMIT = 128 * 1024" in APP_SOURCE
    middleware = APP_SOURCE[
        APP_SOURCE.index('async def limit_public_agent_api_body') :
        APP_SOURCE.index('\n@app.', APP_SOURCE.index('async def limit_public_agent_api_body'))
    ]
    assert 'request.url.path.startswith("/api/public/v1/")' in middleware
    assert 'request.method in {"POST", "PUT", "PATCH"}' in middleware
    assert "declared > _PUBLIC_AGENT_API_BODY_LIMIT" in middleware
    assert "size > _PUBLIC_AGENT_API_BODY_LIMIT" in middleware
    assert 'status_code=413' in middleware
    assert "EXTERNAL_API_REQUEST_TOO_LARGE" in middleware


def test_integration_workspace_exposes_safe_key_management_entry() -> None:
    assert 'id="rail-integrations"' in INDEX_HTML
    assert 'aria-controls="integration-center-workspace"' in INDEX_HTML
    assert 'id="integration-center-workspace"' in INDEX_HTML
    assert 'data-integration-tab="api"' in INDEX_HTML
    assert "由這台電腦的 Workbench 產生並綁定此安裝" in INDEX_HTML
    assert "只顯示這一次" in INDEX_HTML
    assert "完整秘密" in INDEX_HTML
    assert "state.oneTimeSecret" in INTEGRATION_JS
    assert "clearSecret();" in INTEGRATION_JS
    assert "localStorage" not in INTEGRATION_JS
    assert "sessionStorage" not in INTEGRATION_JS


def test_public_api_guide_is_linked_and_describes_security_boundaries() -> None:
    assert "[Workbench 對外 Agent API](docs/EXTERNAL_AGENT_API.md)" in README
    for phrase in (
        "完整金鑰只顯示一次",
        "Windows DPAPI",
        "Authorization: Bearer",
        "Idempotency-Key",
        "128 KiB",
        "Project 整合權限",
        "請勿把本機後端埠直接暴露到網際網路",
    ):
        assert phrase in GUIDE
