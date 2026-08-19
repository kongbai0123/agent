"""Static contracts for the M4 browser trust boundary."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _first_party_javascript() -> dict[Path, str]:
    """All authored JavaScript, excluding vendored and minified libraries."""

    return {
        path: _text(path)
        for path in sorted(FRONTEND.rglob("*.js"))
        if "vendor" not in path.parts and not path.name.endswith(".min.js")
    }


def _all_first_party_javascript() -> str:
    return "\n".join(_first_party_javascript().values())


def test_executable_assets_are_local_and_version_pinned():
    html = _text(FRONTEND / "index.html")
    script_sources = re.findall(r'<script[^>]+src=["\']([^"\']+)', html, re.IGNORECASE)
    assert script_sources
    assert all(not source.startswith(("http://", "https://", "//")) for source in script_sources)
    assert "fonts.googleapis.com" not in html
    assert "fonts.gstatic.com" not in html
    assert "unpkg.com" not in html
    assert "cdn.jsdelivr.net" not in html
    assert '<script src="theme-init.js"></script>' in html
    assert not re.search(r"<script(?![^>]+\bsrc=)[^>]*>", html, re.IGNORECASE)


def test_vendored_asset_hashes_are_locked():
    expected = {
        "dompurify-3.1.5.min.js": "20b0b3840a73da51d3a66c51f073e3ed6da1042b3741c5861c1b7a36693dc928",
        "lucide-1.27.0.min.js": "e37f337f85a50b1af4c830cb46e32545201ab6625f00deacf42721bf33ff0de0",
        "marked-12.0.2.min.js": "15fabce5b65898b32b03f5ed25e9f891a729ad4c0d6d877110a7744aa847a894",
    }
    for filename, digest in expected.items():
        payload = (FRONTEND / "vendor" / filename).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == digest


def test_frontend_never_reads_or_places_session_credentials_in_urls():
    javascript = _all_first_party_javascript()
    forbidden = (
        "session-token.json",
        "workbench_token",
        "X-Workbench-Token",
        "workbench_backend",
    )
    assert all(value not in javascript for value in forbidden)
    assert "const API_BASE = location.origin;" in javascript
    assert "credentials: 'same-origin'" in javascript
    assert "fetch('/session/bootstrap'" in javascript


def test_startup_server_does_not_publish_credentials():
    server = _text(ROOT / "scripts" / "startup_http_server.py")
    assert "session-token.json" not in server
    assert "token_file_path" not in server


def test_launcher_opens_the_backend_origin_for_the_main_ui():
    launcher = _text(ROOT / "scripts" / "start_workbench.ps1")
    assert '$websiteUrl = "$backendUrl/index.html?v=$frontendVersion"' in launcher
    assert '$encodedTarget = [Uri]::EscapeDataString($websiteUrl)' in launcher


def test_frontend_cache_keys_match_the_application_version():
    app = _text(ROOT / "backend" / "app.py")
    html = _text(FRONTEND / "index.html")
    launcher = _text(ROOT / "scripts" / "start_workbench.ps1")
    version = re.search(r'^APP_VERSION = "([^"]+)"$', app, re.MULTILINE)
    assert version
    expected = version.group(1)
    assert f'style.css?v={expected}' in html
    assert f'app.js?v={expected}' in html
    assert f'$frontendVersion = "{expected}"' in launcher


def test_artifact_iframe_stays_origin_isolated():
    html = _text(FRONTEND / "index.html")
    match = re.search(r'<iframe[^>]+id="sandbox-iframe"[^>]*>', html)
    assert match
    tag = match.group(0)
    assert 'sandbox="allow-scripts"' in tag
    assert "allow-same-origin" not in tag


def test_csp_incompatible_inline_event_handlers_are_absent():
    sources = _text(FRONTEND / "index.html") + _all_first_party_javascript()
    assert not re.search(r"<[^>]+\son[a-z]+\s*=", sources, re.IGNORECASE)


def test_every_authored_javascript_file_is_covered_by_the_security_scan():
    files = _first_party_javascript()
    assert FRONTEND / "app.js" in files
    assert FRONTEND / "theme-init.js" in files
    assert all("vendor" not in path.parts for path in files)
    assert all(not path.name.endswith(".min.js") for path in files)
