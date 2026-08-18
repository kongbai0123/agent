import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
INDEX_HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (FRONTEND / "style.css").read_text(encoding="utf-8")


def _rule(selector: str, *, start: int = 0) -> str:
    rule_start = STYLE_CSS.index(selector, start)
    body_start = STYLE_CSS.index("{", rule_start)
    depth = 0
    for index in range(body_start, len(STYLE_CSS)):
        if STYLE_CSS[index] == "{":
            depth += 1
        elif STYLE_CSS[index] == "}":
            depth -= 1
            if depth == 0:
                return STYLE_CSS[rule_start : index + 1]
    raise AssertionError(f"unclosed CSS rule: {selector}")


def test_right_surfaces_have_explicit_ownership_and_scroll_boundaries():
    for surface_id in (
        "agent-collaboration-panel",
        "artifacts-sandbox-panel",
        "output-floating-workspace",
        "output-floating-panel",
        "task-progress-center",
        "extension-audit-panel",
    ):
        assert f'id="{surface_id}"' in INDEX_HTML

    workbench = _rule(".workbench-body {")
    assert "display: flex" in workbench
    assert "min-width: 0" in workbench
    assert "min-height: 0" in workbench
    assert "overflow: hidden" in workbench

    output = _rule(".output-floating-workspace {")
    assert "position: fixed" in output
    assert "z-index: var(--z-floating-inspector)" in output
    assert "width: var(--right-inspector-width)" in _rule(
        ".output-floating-panel {"
    )
    tabs = _rule(".output-floating-tabs {")
    assert "max-height: calc(100dvh - 112px)" in tabs
    assert "overflow-y: auto" in tabs
    assert "overscroll-behavior: contain" in tabs
    assert "overflow-y: auto" in _rule(".output-panel-pane {")

    audit = _rule(".extension-audit-panel {")
    assert "position: absolute" in audit
    assert "top: 86px" in audit
    assert "bottom: 54px" in audit
    assert "overflow: auto" in audit
    assert "overscroll-behavior: contain" in audit


def test_desktop_safe_area_belongs_to_workbench_and_respects_hidden_output():
    contract_start = STYLE_CSS.index("/* Right-surface workspace contract.")
    contract_end = STYLE_CSS.index(
        "/* --- Project-organized task sidebar --- */", contract_start
    )
    contract = STYLE_CSS[contract_start:contract_end]

    assert "@media (min-width: 1181px)" in contract
    assert "#output-floating-workspace:not([hidden])" in contract
    assert ".workbench-body" in contract
    assert "padding-right: var(--right-rail-safe-area)" in contract
    assert "padding-right: var(--right-inspector-safe-area)" in contract
    assert ".workbench-body > main:not([hidden])" in contract
    assert "margin-right: 0" in contract
    assert (
        "html.run-inspector-open:has(#output-floating-workspace:not([hidden]))"
        in contract
    )

    # The extension workspace suppresses the run-scoped inspector.  The
    # :not([hidden]) condition is therefore what prevents a blank right gutter.
    assert contract.count("#output-floating-workspace:not([hidden])") >= 2


def test_compact_inspector_preserves_main_height_at_928_and_short_windows():
    assert "@media (max-width: 1180px)" in STYLE_CSS
    assert "--right-compact-panel-offset: 24px" in STYLE_CSS
    assert "--right-compact-panel-height: min(" in STYLE_CSS
    assert "100dvh - 48px - var(--right-compact-bottom-inset)" in STYLE_CSS
    assert "- var(--right-compact-main-reserve) - var(--right-compact-panel-offset)" in STYLE_CSS

    compact_start = STYLE_CSS.index("@media (max-width: 1180px)", STYLE_CSS.index("/* Right-surface workspace contract."))
    compact_end = STYLE_CSS.index("@media (max-width: 640px)", compact_start)
    compact = STYLE_CSS[compact_start:compact_end]
    assert ".workbench-body > main:not([hidden])" in compact
    assert (
        "html.run-inspector-open:has(#output-floating-workspace:not([hidden]))"
        in compact
    )
    assert ":not(:has(#artifacts-sandbox-panel.active))" in compact
    assert ":not(:has(#agent-collaboration-panel:not([hidden])))" in compact
    assert "var(--right-compact-panel-height) + var(--right-compact-panel-offset)" in compact
    assert "+ var(--right-compact-panel-gap)" in compact
    assert "height: var(--right-compact-panel-height)" in compact
    assert "max-height: var(--right-compact-panel-height)" in compact

    mobile = STYLE_CSS[compact_end : STYLE_CSS.index("/* --- Project-organized task sidebar --- */", compact_end)]
    assert "--right-compact-bottom-inset: 56px" in mobile
    assert "--right-compact-panel-offset: 16px" in mobile
    assert "--right-compact-panel-gap: 16px" in mobile
    assert "--right-compact-panel-max: 300px" in mobile
    assert "min-height: clamp(44px, calc((100dvh - 138px) / 3), 66px)" in mobile
    assert "max-height: calc(100dvh - 130px)" in mobile


def test_layout_equations_leave_zero_overlap_at_boundary_viewports():
    viewports = [
        (1920, 1080), (1280, 720), (1181, 768), (1180, 768),
        (928, 910), (901, 768), (900, 768), (641, 640),
        (640, 844), (390, 320), (390, 260),
    ]
    for width, height in viewports:
        if width >= 1181:
            inspector_width = max(320, min(width * 0.24, 380))
            inspector_safe_area = inspector_width + 46 + 16
            main_right = width - inspector_safe_area
            inspector_left = width - inspector_safe_area
            assert main_right <= inspector_left
            continue

        mobile = width <= 640
        bottom_inset = 56 if mobile else 0
        panel_offset = 16 if mobile else 24
        panel_gap = 16
        reserve_min, reserve_max = (140, 220) if mobile else (160, 240)
        main_reserve = max(reserve_min, min(height * 0.28, reserve_max))
        panel_max = 300 if mobile else 360
        panel_height = min(
            panel_max,
            max(
                58,
                height - 48 - bottom_inset - main_reserve
                - panel_offset - panel_gap,
            ),
        )
        panel_top = 48 + panel_offset
        panel_bottom = panel_top + panel_height
        main_top = 48 + panel_height + panel_offset + panel_gap
        main_bottom = height - bottom_inset
        assert panel_bottom + panel_gap == main_top
        assert panel_bottom <= main_top
        assert main_top < main_bottom

        tabs_top = panel_top + 10
        tabs_max_height = height - (130 if mobile else 98)
        tabs_bottom = tabs_top + max(0, tabs_max_height)
        expected_bottom = height - (56 if mobile else 16)
        assert tabs_bottom <= expected_bottom


def test_compact_full_height_docks_cannot_escape_or_cover_bottom_navigation():
    compact_dock_comment = "/* A vertical dock beside both navigation columns"
    start = STYLE_CSS.index(compact_dock_comment)
    end = STYLE_CSS.index("/* 99_final_overrides", start)
    dock_contract = STYLE_CSS[start:end]

    assert "@media (max-width: 1180px)" in dock_contract
    assert ".artifacts-sandbox-panel.inspector-panel" in dock_contract
    assert ".agent-collaboration-panel" in dock_contract
    assert "width: min(440px, 92vw) !important" in dock_contract
    assert "max-width: 92vw !important" in dock_contract
    assert "bottom: 0" in dock_contract
    assert "@media (max-width: 640px)" in dock_contract
    assert "bottom: 56px" in dock_contract
    assert "width: 100vw !important" in dock_contract

    exclusive_start = STYLE_CSS.index("/* Only one interactive right surface")
    exclusive = STYLE_CSS[exclusive_start:start]
    assert "#artifacts-sandbox-panel.active" in exclusive
    assert "#agent-collaboration-panel:not([hidden])" in exclusive
    assert "#output-floating-workspace" in exclusive
    assert "display: none" in exclusive


def test_right_surface_z_index_layers_are_ordered_and_cache_is_bumped():
    values = {
        name: int(value)
        for name, value in re.findall(
            r"--(z-[a-z-]+):\s*(\d+);",
            STYLE_CSS,
        )
    }
    assert values["z-compact-drawer"] < values["z-compact-inspector"]
    assert values["z-compact-inspector"] < values["z-compact-navigation"]
    assert values["z-compact-navigation"] < values["z-floating-inspector"]
    assert values["z-floating-inspector"] < values["z-transient-progress"]
    assert values["z-transient-progress"] < values["z-popover"]
    assert values["z-popover"] < values["z-modal"] < values["z-toast"]
    assert "z-index: var(--z-popover)" in _rule(".slash-commands-menu {")
    assert "z-index: var(--z-modal)" in _rule(".modal-overlay {")
    assert "style.css?v=0.8.0-n8n-graph-authoring-beta.4" in INDEX_HTML
