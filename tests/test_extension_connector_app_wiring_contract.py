from __future__ import annotations

import ast
import asyncio
import copy
import json
from pathlib import Path

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from backend.structured_log import redact


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "backend" / "app.py").read_text(encoding="utf-8")
CONNECTOR_ROUTES = (
    ROOT / "backend" / "api" / "routes" / "connectors.py"
).read_text(encoding="utf-8")
CONNECTOR_SCHEMAS = (
    ROOT / "backend" / "api" / "schemas" / "connectors.py"
).read_text(encoding="utf-8")
CONNECTOR_SERVICE = (ROOT / "backend" / "connector_service.py").read_text(
    encoding="utf-8"
)
CONNECTOR_SECRETS = (ROOT / "backend" / "connector_secrets.py").read_text(
    encoding="utf-8"
)
CHAT_RUNTIME = (ROOT / "backend" / "chat" / "runtime.py").read_text(
    encoding="utf-8"
)
TOOL_RUNTIME = (ROOT / "backend" / "tool_runtime.py").read_text(
    encoding="utf-8"
)
APP_TREE = ast.parse(APP)


def _function_source(name: str) -> str:
    node = next(
        item
        for item in APP_TREE.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == name
    )
    lines = APP.splitlines()
    return "\n".join(lines[node.lineno - 1 : node.end_lineno])


def _load_app_function(name: str):
    """Compile one app function without importing the side-effectful app module."""

    node = copy.deepcopy(
        next(
            item
            for item in APP_TREE.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name == name
        )
    )
    node.decorator_list = []
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    namespace = {
        "JSONResponse": JSONResponse,
        "Request": Request,
        "RequestValidationError": RequestValidationError,
        "redact": redact,
    }
    exec(compile(module, str(ROOT / "backend" / "app.py"), "exec"), namespace)
    return namespace[name]


def test_oauth_callback_router_precedes_the_static_catch_all():
    callback_factory = APP.index(
        "connector_callbacks_router = build_connector_callback_router("
    )
    domain_registration = APP.index("app.include_router(domain_router)")
    chat_registration = APP.index("app.include_router(chat_router)")
    static_mount = APP.index(
        'app.mount("/", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend")'
    )

    assert callback_factory < domain_registration < chat_registration < static_mount
    domain_block = APP[
        APP.index("for domain_router in (") : domain_registration
    ]
    assert "connector_callbacks_router," in domain_block
    assert '@router.get("/oauth/callback/github", include_in_schema=False)' in CONNECTOR_ROUTES
    assert '@router.get("/oauth/callback/notion", include_in_schema=False)' in CONNECTOR_ROUTES


def test_oauth_callback_is_cookie_independent_and_returns_only_minimal_html():
    callback_builder = CONNECTOR_ROUTES[
        CONNECTOR_ROUTES.index("def build_connector_callback_router") :
    ]
    callback_html = CONNECTOR_ROUTES[
        CONNECTOR_ROUTES.index("def _callback_html") :
        CONNECTOR_ROUTES.index("def build_connector_callback_router")
    ]

    assert "def build_connector_callback_router(" in callback_builder
    assert "service: ConnectorService" in callback_builder
    assert "require_extension:" in callback_builder
    assert "Request" not in callback_builder
    assert "service.complete_oauth(" in callback_builder
    assert "except Exception:" in callback_builder
    assert "Cache-Control" in callback_html and "no-store" in callback_html
    assert "default-src 'none'" in callback_html
    assert '"Referrer-Policy": "no-referrer"' in callback_html
    assert "provider_error" not in callback_html
    assert "authorization_code" not in callback_html


def test_connector_secret_store_is_the_only_app_wiring_secret_boundary():
    connector_wiring = APP[
        APP.index("connector_service = ConnectorService(") :
        APP.index("connector_service.initialize()") + len("connector_service.initialize()")
    ]
    assert "secrets_store=ConnectorSecretStore()" in connector_wiring
    for secret_name in ("client_secret", "access_token", "refresh_token", "code_verifier"):
        assert secret_name not in APP

    assert "client_secret: Optional[SecretStr]" in CONNECTOR_SCHEMAS
    assert "from secret_store import _protect, _unprotect" in CONNECTOR_SECRETS
    assert "_assert_safe_chain(self.path.parent)" in CONNECTOR_SECRETS
    assert "os.replace(temporary, self.path)" in CONNECTOR_SECRETS
    assert '"ciphertext": ciphertext' in CONNECTOR_SECRETS
    public_connection = CONNECTOR_SERVICE[
        CONNECTOR_SERVICE.index("def public_connection") :
        CONNECTOR_SERVICE.index("def get_connection", CONNECTOR_SERVICE.index("def public_connection"))
    ]
    for secret_name in ("client_secret", "access_token", "refresh_token", "code_verifier"):
        assert secret_name not in public_connection


def test_request_validation_error_never_echoes_oauth_secret_input_or_context():
    handler = _load_app_function("redacted_request_validation_error")
    marker = "OAUTH-CLIENT-SECRET-MUST-NOT-ECHO"
    validation_error = RequestValidationError(
        [
            {
                "type": "missing",
                "loc": ("body", "client_id"),
                "msg": "Field required",
                "input": {"client_secret": marker},
                "ctx": {"provider_error": marker},
            }
        ]
    )

    response = asyncio.run(handler(None, validation_error))
    payload = json.loads(response.body)
    encoded = response.body.decode("utf-8")

    assert response.status_code == 422
    assert payload == {
        "detail": [
            {
                "type": "missing",
                "loc": ["body", "client_id"],
                "msg": "Field required",
            }
        ]
    }
    assert marker not in encoded
    assert '"input"' not in encoded
    assert '"ctx"' not in encoded


def test_connector_tool_discovery_and_execution_recheck_extension_scope():
    prepare = _function_source("_prepare_project_tools")
    resolve_scope = _function_source("_resolve_tool_scope")

    assert "connector_service.runtime_tool_definitions(" in prepare
    assert "if extension_is_enabled(definition.extension_id, project_id)" in prepare
    assert "tool_registry.replace_project(" in prepare
    assert "extension_registry.get(" in resolve_scope
    assert "synchronize=False" in resolve_scope
    assert 'enabled=bool(item.get("effective_enabled"))' in resolve_scope
    assert 'healthy=str(connection.get("status") or "") == "connected"' in resolve_scope
    assert 'resource_revision=int(invocation.get("resource_revision") or 0)' in resolve_scope
    assert "execute_tool(" not in CONNECTOR_ROUTES

    assert "host_tool_runtime.dispatcher.execute(" in CHAT_RUNTIME
    assert "await self._resolve_scope(definition, call)" in TOOL_RUNTIME
    assert "scope = await self._resolve_scope(definition, call)" in TOOL_RUNTIME


def test_connector_writes_flow_through_bound_single_use_approval():
    connector_adapter = CONNECTOR_SERVICE[
        CONNECTOR_SERVICE.index("def runtime_tool_definitions") :
        CONNECTOR_SERVICE.index("def _ensure_github_scope")
    ]
    execute = TOOL_RUNTIME[
        TOOL_RUNTIME.index("async def execute(") :
        TOOL_RUNTIME.index("__all__")
    ]

    assert "access=ToolAccess.WRITE if write else ToolAccess.READ" in connector_adapter
    assert "approved=_write" in connector_adapter
    assert "definition.access is ToolAccess.WRITE" in execute
    assert "current_scope = await self._resolve_scope(definition, call)" in execute
    assert "await self.approvals.consume(str(used_approval_id or \"\"), binding)" in execute
    for binding_field in (
        "connection_id=call.connection_id",
        "resource_id=call.resource_id",
        "manifest_sha256=scope.manifest_sha256",
        "resource_revision=scope.resource_revision",
        "arguments_sha256=hashlib.sha256",
    ):
        assert binding_field in TOOL_RUNTIME


def test_connector_api_is_covered_by_the_global_local_session_guard():
    guard_install = APP.index("install_local_session_guard(app, error_payload)")
    connector_registration = APP.index("connectors_router = build_connectors_router(")
    assert guard_install < connector_registration
    assert "require_local=require_local_workbench" in APP[
        connector_registration : APP.index(
            "connector_callbacks_router =", connector_registration
        )
    ]
