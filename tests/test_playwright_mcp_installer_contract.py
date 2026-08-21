from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_playwright_browser_mcp.ps1"


def test_playwright_mcp_installer_is_pinned_and_explicit() -> None:
    source = INSTALLER.read_text(encoding="utf-8")

    assert '$packageVersion = "0.0.79"' in source
    assert "@playwright/mcp@$packageVersion" in source
    assert "@latest" not in source
    assert "--ignore-scripts" in source
    assert "enabled = [bool]$Enable" in source


def test_playwright_mcp_installer_uses_isolated_chrome_and_safe_allowlist() -> None:
    source = INSTALLER.read_text(encoding="utf-8")

    assert '"--browser", "chrome"' in source
    assert '"--isolated"' in source
    assert '"--image-responses", "omit"' in source
    assert "expected_executable_sha256" in source
    assert "allowed_cwd_roots" in source
    assert "browser_navigate" in source
    assert "browser_snapshot" in source
    assert "browser_click" in source
    assert "browser_run_code_unsafe" not in source
    assert "browser_evaluate" not in source
    assert "browser_file_upload" not in source
    assert "browser_drop" not in source
