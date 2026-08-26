from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_mlops_is_a_primary_workspace_not_a_modal():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    module = (ROOT / "frontend" / "mlops-workspace.js").read_text(encoding="utf-8")
    assert 'id="rail-mlops"' in html
    assert '<main class="mlops-workspace"' in html
    assert 'id="mlops-workspace"' in html
    assert "'mlops'" in app
    assert "window.workbenchMLOps" in module
    assert "modal" not in module.casefold()


def test_mlops_ui_is_traditional_chinese_first():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    section = html.split('<main class="mlops-workspace"', 1)[1].split('</main>', 1)[0]
    for phrase in ("資料集", "實驗與訓練", "模型登錄", "健康與政策", "文字分類基準訓練"):
        assert phrase in section


def test_mlops_entry_remains_reachable_in_short_and_compact_windows():
    css = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")
    rail = css.split(".icon-rail {", 1)[1].split("}", 1)[0]
    assert "overflow-y: auto" in rail
    compact = css.split("@media (max-width: 640px)", 1)[1]
    assert "overflow-x: auto" in compact
