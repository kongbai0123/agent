"""One model catalog, served by the backend.

The frontend used to carry its own list of eight models with its own hardware
advice, while the backend served four with a real compatibility check against
the machine's RAM and VRAM. Both were shown to the user as fact. Whichever the
user believed, the other one was wrong.

The fix is structural rather than a re-sync: the UI now has no list of its own,
so the two cannot disagree again. These tests hold that structure in place and
check that the payload still carries every field the UI renders.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
APP_JS = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")
MODELS_PY = (BACKEND / "api" / "routes" / "models.py").read_text(encoding="utf-8")

sys.path.insert(0, str(BACKEND))
from model_catalog import MODEL_CATALOG  # noqa: E402

#: Keys the Model Manager reads out of each catalog entry.
UI_FIELDS = ("name", "installed", "purposes", "size_gb", "recommended_ram_gb", "context", "compatibility")


def catalog_entries():
    """Read the dependency-free backend catalog without importing FastAPI."""
    return MODEL_CATALOG


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


def test_available_models_are_uninstalled_and_refresh_after_pull_completion():
    render = re.search(r"async function renderMmAvailable\([^)]*\)\s*\{(.+?)\n\}", APP_JS, re.S)
    assert render
    assert ".filter(m => !m.installed)" in render.group(1)
    monitor = re.search(r"function monitorModelInstall\([^)]*\)\s*\{(.+?)\n\}", APP_JS, re.S)
    assert monitor and "await loadModelCatalog(true)" in monitor.group(1)


def test_model_manager_supports_category_search_and_safe_custom_ollama_tags():
    for element_id in (
        "mm-category-filter", "mm-catalog-summary", "mm-custom-model", "mm-custom-install-btn",
    ):
        assert f'id="{element_id}"' in INDEX_HTML
        assert f"getElementById('{element_id}')" in APP_JS
    assert "OLLAMA_MODEL_REFERENCE" in APP_JS
    assert "isSafeOllamaModelReference" in APP_JS
    assert "https://ollama.com/library" in INDEX_HTML
    assert 'rel="noopener noreferrer"' in INDEX_HTML
    assert ".mm-catalog-toolbar" in STYLE_CSS
    assert ".mm-custom-install" in STYLE_CSS


def test_catalog_cards_use_friendly_names_and_search_provenance():
    assert "title: m.display_name || m.name" in APP_JS
    assert "searchText:" in APP_JS
    for field in ("publisher", "license", "displayName"):
        assert field in APP_JS


def test_every_catalog_entry_carries_what_the_ui_needs():
    entries = catalog_entries()
    assert entries, "the catalog is empty"
    required = {
        "name", "display_name", "category", "size_gb_estimated",
        "min_ram_gb", "recommended_ram_gb", "min_vram_gb", "recommended_vram_gb",
        "context_window", "publisher", "license", "source_url", "capabilities",
    }
    for entry in entries:
        missing = required - set(entry)
        assert not missing, f"{entry.get('name')} is missing {sorted(missing)}"
        assert isinstance(entry["category"], list) and entry["category"]
        assert entry["source_url"].startswith("https://ollama.com/library/")
        assert entry["recommended_ram_gb"] >= entry["min_ram_gb"] > 0
        assert entry["recommended_vram_gb"] >= entry["min_vram_gb"] >= 0
        assert entry["size_gb_estimated"] > 0
        assert entry["context_window"] > 0


def test_catalog_covers_current_open_model_families_without_cloud_or_specialized_endpoints():
    entries = catalog_entries()
    names = {entry["name"] for entry in entries}
    assert len(entries) >= 60
    for expected in {
        "qwen3.5:4b", "qwen3.6:27b", "gemma4:e4b", "granite4:3b",
        "ministral-3:8b", "gpt-oss:20b", "deepseek-r1:8b",
        "qwen3-coder:30b", "devstral-small-2:24b", "qwen3-vl:4b",
        "llama3.2:3b", "qwen2.5-coder:7b", "llava:7b",
    }:
        assert expected in names
    assert all("cloud" not in name.casefold() for name in names)
    assert all(not re.search(r"embed|rerank|classifier|guard", name, re.I) for name in names)


def test_catalog_uses_safe_explicit_ollama_references():
    model_ref = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*(?::[A-Za-z0-9][A-Za-z0-9._-]*)?$")
    entries = catalog_entries()
    names = {entry["name"] for entry in entries}
    aliases = []
    for entry in entries:
        assert model_ref.fullmatch(entry["name"]), entry["name"]
        assert "//" not in entry["name"]
        assert all(segment not in {"", ".", ".."} for segment in entry["name"].split(":", 1)[0].split("/"))
        aliases.extend(entry.get("aliases") or [])
    assert len(aliases) == len(set(aliases))
    assert not names.intersection(aliases)


def test_catalog_names_are_unique():
    names = [entry["name"] for entry in catalog_entries()]
    assert len(names) == len(set(names))


def test_the_response_shape_the_frontend_normalizes_is_the_one_served():
    """The UI reads these keys; the serializer has to emit them."""
    catalog_fields = set().union(*(entry.keys() for entry in catalog_entries()))
    for field in UI_FIELDS:
        assert field in catalog_fields or f'"{field}"' in MODELS_PY, f"/api/models/catalog no longer emits {field}"
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
