"""本機工作階段憑證（Local session token）。

這不是帳號系統，也不會出現登入畫面。

它驗證的是「**哪一個程式**在呼叫 API」，不是「哪一個人」。Workbench 是個人本機
工具，使用者不需要證明自己是誰；但同一台電腦上除了 Workbench 前端之外，還跑著
瀏覽器、瀏覽器裡開著的每一個網頁、以及每一個瀏覽器擴充功能——這些全都連得到
127.0.0.1:8000。後端必須能分辨「我的前端」與「使用者剛好開著的某個網頁」，否則
任何網頁都能驅動具備終端執行能力的 agent。

Token 在後端啟動時隨機產生。瀏覽器主介面由後端同源提供，首次載入時以
HttpOnly、SameSite=Strict Cookie 接收憑證；JavaScript、sessionStorage 與 URL
都無法接觸 token。CLI 與 regression runner 則可從 runtime 檔案讀取 token，
再透過 X-Workbench-Token 標頭呼叫。

刻意不接受 query-string token，避免憑證出現在瀏覽歷程、日誌、截圖或 referrer。
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path
from typing import Any, Callable, Optional


#: 允許的本機來源。主介面與 API 同源；啟動畫面會從另一個 loopback 埠
#: 輪詢兩個公開健康端點。
LOCAL_ORIGIN_PATTERN = re.compile(
    r"^https?://(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?$",
    re.IGNORECASE,
)

#: 不需要 token 的端點。前端在拿到 token 之前，只需要能確認後端是否已就緒。
#:
#: 警告：絕對不要在這裡加入任何會回傳 token 的端點（例如 /api/bootstrap）。
#: 那等於把鑰匙掛在門口，整個機制就失效了。
PUBLIC_API_PATHS = frozenset(
    {
        "/api/health",
        "/api/startup/status",
    }
)

# These paths are not public.  They deliberately bypass the browser session
# token because the n8n service authenticates every request with a separate,
# route-level HMAC/one-time-token contract.  Keeping the prefix exact prevents
# an integration credential from becoming a general Workbench API credential.
SERVICE_AUTH_API_PREFIXES = (
    "/api/integrations/n8n/v1/gmail/",
    "/api/integrations/n8n/v1/agent/",
)

TOKEN_FILENAME = "workbench-session-token"
SESSION_COOKIE_NAME = "workbench_session"

_TOKEN = secrets.token_urlsafe(32)


def session_token() -> str:
    """回傳目前的工作階段 token。"""
    return _TOKEN


def rotate_session_token() -> str:
    """重新產生 token。測試用，或未來提供「登出所有前端」功能時用。"""
    global _TOKEN
    _TOKEN = secrets.token_urlsafe(32)
    return _TOKEN


def token_file_path(runtime_dir: Optional[Path] = None) -> Path:
    """token 檔的位置。預設取 paths.RUNTIME_ROOT，而不是自行推導。

    RUNTIME_ROOT 可由環境變數 WORKBENCH_RUNTIME_DIR 覆寫（CI 就會設），
    所以任何自行組出 `<repo>/runtime` 的寫法都會在被覆寫時指到錯的地方。
    前端靜態伺服器也必須用同一個來源取值，兩邊才不會分歧。
    """
    if runtime_dir is None:
        from paths import RUNTIME_ROOT

        runtime_dir = RUNTIME_ROOT
    return Path(runtime_dir) / TOKEN_FILENAME


def write_token_file(runtime_dir: Optional[Path] = None) -> Path:
    """把 token 寫進 runtime 目錄，供前端靜態伺服器以同源方式轉交給前端。

    這個檔案的保護來自檔案系統權限：只有目前的使用者帳戶讀得到。瀏覽器裡的
    網頁沒有檔案系統存取權，所以拿不到。
    """
    if runtime_dir is None:
        from paths import RUNTIME_ROOT

        runtime_dir = RUNTIME_ROOT
    runtime_dir = Path(runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    path = runtime_dir / TOKEN_FILENAME
    path.write_text(_TOKEN, encoding="utf-8")
    return path


def is_local_origin(origin: Optional[str]) -> bool:
    """Origin 是否來自本機工作台。沒有 Origin 標頭時回傳 True（交由 token 把關）。"""
    if not origin:
        return True
    return bool(LOCAL_ORIGIN_PATTERN.match(origin))


def token_matches(supplied: Optional[str]) -> bool:
    """定時比較，避免以回應時間推測 token。"""
    if not supplied:
        return False
    return secrets.compare_digest(str(supplied), _TOKEN)


def install_local_session_guard(app: Any, error_payload: Callable[..., dict]) -> None:
    """為 FastAPI app 掛上全域的本機工作階段驗證。

    刻意做成全域 middleware 而不是逐路由的相依項，因為逐路由的做法要求每個新增
    路由的人都記得加上——原本 88 個路由裡只有 18 個記得，剩下 70 個沒有保護。
    全域 middleware 的預設是「保護」，要放行必須明確列進 PUBLIC_API_PATHS，
    方向相反，也就不會再出現「忘記加」這種漏洞。
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse

    @app.middleware("http")
    async def enforce_local_session(request: Request, call_next):
        path = request.url.path or ""

        # 非 API 路徑（靜態檔案等）不受影響。
        if not path.startswith("/api/"):
            return await call_next(request)

        # CORS preflight 必須讓 CORSMiddleware 處理。這個 middleware 掛在更外層，
        # 所以會先看到 OPTIONS，必須主動放行。
        if request.method == "OPTIONS":
            return await call_next(request)

        if not is_local_origin(request.headers.get("origin")):
            return JSONResponse(
                status_code=403,
                content=error_payload(
                    "LOCAL_ORIGIN_REQUIRED",
                    "此功能只允許本機工作台使用。",
                    recoverable=False,
                ),
            )

        if path in PUBLIC_API_PATHS:
            return await call_next(request)

        if any(path.startswith(prefix) for prefix in SERVICE_AUTH_API_PREFIXES):
            return await call_next(request)

        # Browser traffic uses a same-origin HttpOnly cookie.  The header remains
        # available for trusted local CLI/regression clients that read the runtime
        # token file.  Query-string credentials are intentionally unsupported:
        # URLs leak through history, logs, screenshots, and referrer metadata.
        header_token = request.headers.get("x-workbench-token")
        cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
        supplied = header_token or cookie_token or ""
        if cookie_token and not header_token:
            origin = request.headers.get("origin")
            expected_origin = str(request.base_url).rstrip("/")
            if origin and origin.rstrip("/") != expected_origin:
                return JSONResponse(
                    status_code=403,
                    content=error_payload(
                        "SAME_ORIGIN_REQUIRED",
                        "瀏覽器工作階段只允許由同源工作台使用。",
                        recoverable=False,
                    ),
                )
        if not token_matches(supplied):
            return JSONResponse(
                status_code=401,
                content=error_payload(
                    "AUTH_REQUIRED",
                    "缺少或無效的本機工作階段憑證。請重新從啟動器開啟 Workbench。",
                    recoverable=False,
                ),
            )

        return await call_next(request)

    return None
