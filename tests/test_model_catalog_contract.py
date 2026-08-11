"""One model catalog, served by the backend.

The frontend used to carry its own list of eight models with its own hardware
advice, while the backend served four with a real compatibility check against
the machine's RAM and VRAM. Both were shown to the user as fact. Whichever the
user believed, the other one was wrong.

The fix is structural rather than a re-sync: the UI now has no list of its own,
so the two cannot disagree again. These tests hold that structure in place and
check that the payload still carries every field the UI renders.
"""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
APP_JS = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
MODELS_PY = (BACKEND / "api" / "routes" / "models.py").read_text(encoding="utf-8")

sys.path.insert(0, str(BACKEND))

#: Keys the Model Manager reads out of each catalog entry.
UI_FIELDS = ("name", "installed", "purposes", "size_gb", "recommended_ram_gb", "context", "compatibility")


def catalog_entries():
    """Read MODEL_CATALOG without importing the FastAPI stack."""
    tree = ast.parse(MODELS_PY)
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "MODEL_CATALOG":
            return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if getattr(target, "id", "") == "MODEL_CATALOG":
                    return ast.literal_eval(node.value)
    raise AssertionError("models.py no longer defines MODEL_CATALOG")


def test_the_frontend_no_longer_ships_its_own_catalog():
    assert "const MODEL_CATALOG" not in APP_JS, (
        "a second hardcoded catalog in app.js will drift from the backend again"
    )
    assert "${API_BASE}/api/models/catalog" in APP_JS


def test_the_model_manager_renders_from_the_fetched_catalog():
    for symbol in ("loadModelCatalog", "normalizeCatalogEntry", "modelCatalogCache"):
        assert symbol in APP_JS, symbol
    # Both listing panes must go through the fetch, not a local array.
    for renderer in ("renderMmRecommended", "renderMmAvailable"):
        match = re.search(rf"async function {renderer}\([^)]*\)\s*\{{(.+?)\n\}}", APP_JS, re.S)
        assert match, f"{renderer} is no longer an async function"
        assert "loadModelCatalog" in match.group(1), f"{renderer} does not use the API catalog"


def test_hardware_advice_comes_from_the_backend_with_a_browser_fallback():
    assert "hardwareSummaryText" in APP_JS
    match = re.search(r"function hardwareSummaryText\(hardware\)\s*\{(.+?)\n\}", APP_JS, re.S)
    assert match and "detectHardwareString()" in match.group(1), (
        "the browser estimate must remain only as a fallback when the backend is unreachable"
    )


def test_every_catalog_entry_carries_what_the_ui_needs():
    entries = catalog_entries()
    assert entries, "the catalog is empty"
    required = {
        "name", "display_name", "category", "size_gb_estimated",
        "min_ram_gb", "recommended_ram_gb", "min_vram_gb", "recommended_vram_gb",
        "context_window",
    }
    for entry in entries:
        missing = required - set(entry)
        assert not missing, f"{entry.get('name')} is missing {sorted(missing)}"
        assert isinstance(entry["category"], list) and entry["category"]


def test_catalog_names_are_unique():
    names = [entry["name"] for entry in catalog_entries()]
    assert len(names) == len(set(names))


def test_the_response_shape_the_frontend_normalizes_is_the_one_served():
    """The UI reads these keys; the serializer has to emit them."""
    for field in UI_FIELDS:
        assert f'"{field}"' in MODELS_PY, f"/api/models/catalog no longer emits {field}"
    assert '"recommended"' in MODELS_PY and '"hardware"' in MODELS_PY


def test_compatibility_carries_a_level_and_a_readable_label():
    """normalizeCatalogEntry reads compatibility.level / .label / .reason."""
    for key in ('"level"', '"label"', '"reason"', '"fit"'):
        assert key in MODELS_PY, f"_model_fit no longer returns {key}"


def test_the_parallel_workbench_shell_is_no_longer_shipped():
    """It was never loaded by index.html; keeping it invited a third catalog.

    The local copy lives under archive/, which .gitignore excludes -- so a fresh
    clone will not have it and this test only asserts what every checkout can
    see: it is gone from frontend/ and nothing references it.
    """
    assert not (ROOT / "frontend" / "workbench.js").exists()
    index_html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "workbench.js" not in index_html
    for path in (ROOT / "frontend").rglob("*.js"):
        assert "workbench.js" not in path.read_text(encoding="utf-8", errors="ignore"), path
