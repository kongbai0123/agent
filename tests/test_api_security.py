"""HTTP 層的安全契約測試。

這是整個 repo 第一組使用 TestClient 的測試。既有的 190 個測試都是直接呼叫 Python
函式，驗證輸入輸出；那類測試看不見「這個路由有沒有被保護」，因為保護發生在
middleware，不在函式裡。

本檔最重要的是 test_every_api_route_requires_a_token：它自動列舉 app.routes，
不需要維護清單，所以未來任何新增的端點都會自動被涵蓋。如果有人新增路由卻沒有
納入保護，這個測試就會失敗——這是唯一能防止「88 個路由裡有 70 個沒保護」重演的
機制。
"""

from __future__ import annotations

import sys
import os
import subprocess
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

import app as app_module  # noqa: E402
import local_session  # noqa: E402


LOCAL_ORIGIN = "http://127.0.0.1:8080"
EXTERNAL_ORIGIN = "https://evil.example"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app_module.app)


@pytest.fixture(scope="module")
def token() -> str:
    return local_session.session_token()


def _concrete(path: str) -> str:
    """把 /api/projects/{project_id} 這種樣板換成可實際請求的路徑。

    用不存在的 id 即可：我們只關心「有沒有被擋在 401」，不關心資源是否存在。
    被保護的端點必須在查資料庫之前就回 401。
    """
    return (
        path.replace("{model_name:path}", "nonexistent-model")
        .replace(":path", "")
        .replace("{", "nonexistent-")
        .replace("}", "")
    )


def _api_routes():
    seen = set()
    pending = [(route, "") for route in app_module.app.routes]
    while pending:
        route, inherited_prefix = pending.pop(0)
        # FastAPI 0.141 keeps included APIRouters lazy. Their concrete routes
        # live on original_router instead of being flattened into app.routes.
        nested = getattr(getattr(route, "original_router", None), "routes", None)
        if nested is not None:
            context = getattr(route, "include_context", None)
            prefix = inherited_prefix + str(getattr(context, "prefix", "") or "")
            pending[0:0] = [(child, prefix) for child in nested]
            continue
        path = inherited_prefix + str(getattr(route, "path", "") or "")
        if path.startswith("/api/"):
            for method in sorted(
                (getattr(route, "methods", None) or set()) - {"HEAD", "OPTIONS"}
            ):
                key = (method, path)
                if key not in seen:
                    seen.add(key)
                    yield method, path


def test_the_route_inventory_is_not_empty():
    """防止上面的列舉邏輯壞掉之後，其他測試變成空轉而假性通過。"""
    routes = list(_api_routes())
    assert len(routes) > 50, f"只列舉到 {len(routes)} 個 API 路由，列舉邏輯可能已失效"


def test_api_route_contract_snapshot():
    """Physical router moves must not rename or drop an API by accident."""
    routes = sorted(_api_routes())
    payload = json.dumps(routes, separators=(",", ":"))
    assert len(routes) == 171
    assert hashlib.sha256(payload.encode("utf-8")).hexdigest() == (
        "8b4c6210ec268e1848673e67197d9e45449be1159522cd640ca2f51af658f6a4"
    )


def test_every_api_route_requires_a_token(client):
    """每個 /api/ 端點在沒有 token 時都必須回 401。

    新增路由若沒有納入保護，這個測試會列出來並失敗。
    要豁免某個端點，必須明確加進 local_session.PUBLIC_API_PATHS。
    """
    unprotected = []
    for method, path in _api_routes():
        if path in local_session.PUBLIC_API_PATHS:
            continue
        response = client.request(
            method,
            _concrete(path),
            headers={"Origin": LOCAL_ORIGIN},
        )
        if response.status_code != 401:
            unprotected.append(f"{method:6s} {path}  ->  {response.status_code}")

    assert not unprotected, (
        "以下端點在沒有本機工作階段憑證時仍可存取：\n  "
        + "\n  ".join(unprotected)
        + "\n\n若為刻意公開，請加入 local_session.PUBLIC_API_PATHS。"
    )


def test_public_paths_stay_minimal():
    """公開端點清單是這套機制唯一的缺口，必須保持極小且不外洩 token。"""
    assert local_session.PUBLIC_API_PATHS == {"/api/health", "/api/startup/status"}


def test_importing_app_does_not_publish_a_session_token(tmp_path):
    """測試收集只會匯入模組，不得使正在運行的 GUI 憑證失效。"""
    env = os.environ.copy()
    env["WORKBENCH_RUNTIME_DIR"] = str(tmp_path)
    env["PYTHONPATH"] = str(ROOT / "backend")
    result = subprocess.run(
        [sys.executable, "-c", "import app"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / local_session.TOKEN_FILENAME).exists()


def test_external_origin_is_rejected_even_with_a_valid_token(client, token):
    """縱深防禦：就算 token 外洩，非本機來源仍然被擋。"""
    response = client.post(
        "/api/chat",
        json={"messages": []},
        headers={"Origin": EXTERNAL_ORIGIN, "X-Workbench-Token": token},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "LOCAL_ORIGIN_REQUIRED"


def test_token_in_header_passes_the_gate(client, token):
    """帶著正確 token 的請求不應被 401/403 擋下（後續狀態碼由業務邏輯決定）。"""
    response = client.get(
        "/api/models",
        headers={"Origin": LOCAL_ORIGIN, "X-Workbench-Token": token},
    )
    assert response.status_code not in (401, 403)


def test_token_in_query_param_is_rejected(client, token):
    """Credentials must never be accepted from URLs, where they leak into logs/history."""
    response = client.get(f"/api/models?workbench_token={token}")
    assert response.status_code == 401


def test_http_only_cookie_passes_the_gate(token):
    with TestClient(app_module.app, base_url=LOCAL_ORIGIN) as browser:
        browser.cookies.set(local_session.SESSION_COOKIE_NAME, token)
        response = browser.get("/api/models", headers={"Origin": LOCAL_ORIGIN})
    assert response.status_code not in (401, 403)


def test_cookie_is_rejected_from_a_different_loopback_origin(token):
    with TestClient(app_module.app, base_url=LOCAL_ORIGIN) as browser:
        browser.cookies.set(local_session.SESSION_COOKIE_NAME, token)
        response = browser.get(
            "/api/models",
            headers={"Origin": "http://127.0.0.1:9999"},
        )
    assert response.status_code == 403
    assert response.json()["code"] == "SAME_ORIGIN_REQUIRED"


def test_wrong_token_is_rejected(client):
    response = client.get(
        "/api/models",
        headers={"Origin": LOCAL_ORIGIN, "X-Workbench-Token": "not-the-real-token"},
    )
    assert response.status_code == 401


def test_public_health_endpoint_needs_no_token(client):
    response = client.get("/api/health", headers={"Origin": LOCAL_ORIGIN})
    assert response.status_code == 200
    assert response.json()["success"] is True


def test_status_endpoint_requires_token(client):
    response = client.get("/api/status", headers={"Origin": LOCAL_ORIGIN})
    assert response.status_code == 401


def test_the_documented_attack_chain_is_closed(client):
    """回歸測試：2026-07-27 稽核發現的攻擊鏈。

    原本任何網頁都能 (1) POST /api/projects 建立一個 root 指向任意目錄、
    權限預設為 workspace_write 的專案，再 (2) POST /api/chat 帶
    permission_mode=auto_workspace 驅動 agent 執行任意 PowerShell。
    兩個步驟現在都必須失敗。
    """
    step_one = client.post(
        "/api/projects",
        json={"name": "attack-probe", "root_path": "C:\\\\", "root_kind": "linked"},
        headers={"Origin": EXTERNAL_ORIGIN},
    )
    assert step_one.status_code in (401, 403)

    step_two = client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "run a shell command"}],
              "permission_mode": "auto_workspace"},
        headers={"Origin": EXTERNAL_ORIGIN},
    )
    assert step_two.status_code in (401, 403)


def test_index_sets_a_strict_http_only_cookie_and_csp():
    with TestClient(app_module.app, base_url=LOCAL_ORIGIN) as browser:
        response = browser.get("/")
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert f"{local_session.SESSION_COOKIE_NAME}=" in cookie
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "path=/" in cookie
    assert local_session.session_token() not in response.text
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self' 'nonce-" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "{{CSP_NONCE}}" not in response.text


def test_same_origin_bootstrap_refreshes_cookie():
    with TestClient(app_module.app, base_url=LOCAL_ORIGIN) as browser:
        response = browser.post("/session/bootstrap", headers={"Origin": LOCAL_ORIGIN})
    assert response.status_code == 204
    cookie = response.headers["set-cookie"].lower()
    assert f"{local_session.SESSION_COOKIE_NAME}=" in cookie
    assert "httponly" in cookie


def test_bootstrap_rejects_external_origin():
    with TestClient(app_module.app, base_url=LOCAL_ORIGIN) as browser:
        response = browser.post(
            "/session/bootstrap",
            headers={"Origin": EXTERNAL_ORIGIN},
        )
    assert response.status_code == 403
