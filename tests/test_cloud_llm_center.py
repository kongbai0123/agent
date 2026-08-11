from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
CENTER_JS = (ROOT / "frontend" / "cloud-llm-center.js").read_text(encoding="utf-8")
PROVIDER_JS = (ROOT / "frontend" / "extension-center.js").read_text(encoding="utf-8")
APP_JS = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")


def test_cloud_llm_is_an_independent_navigation_feature():
    assert 'id="rail-cloud-llm"' in HTML
    assert 'id="cloud-llm-modal"' in HTML
    assert 'id="btn-open-cloud-llm-settings"' in HTML
    assert HTML.count('id="model-provider-list"') == 1
    assert HTML.index('id="cloud-llm-modal"') < HTML.index('id="model-provider-list"')
    assert "window.workbenchCloudLlm?.open()" in APP_JS


def test_library_exposes_search_filters_classification_and_delete():
    required_ids = {
        "cloud-llm-search",
        "cloud-llm-provider-filter",
        "cloud-llm-kind-filter",
        "cloud-llm-status-filter",
        "cloud-llm-library-list",
    }
    for element_id in required_ids:
        assert f'id="{element_id}"' in HTML
    for marker in (
        "data-cloud-provider-id",
        "data-cloud-edit",
        "data-cloud-delete",
        "providerKind(provider)",
        "missing-key",
    ):
        assert marker in CENTER_JS


def test_new_imports_use_unique_records_and_fail_closed():
    assert "state.deps.nextProviderId()" in CENTER_JS
    assert "nextModelProviderId" in APP_JS
    assert "while (existing.has(candidate))" in PROVIDER_JS
    assert "enabled: false" in CENTER_JS
    assert "MAX_PROVIDERS = 8" in CENTER_JS
    assert "new Set(ids).size !== ids.length" in CENTER_JS


def test_library_persists_the_complete_provider_array_and_secrets_separately():
    assert "model_providers: records" in CENTER_JS
    assert "await state.deps.saveSecrets()" in CENTER_JS
    assert "/api/settings/secrets" in PROVIDER_JS
    assert "removedProviderSecrets" in PROVIDER_JS
    assert "api_key" not in CENTER_JS
    assert "status.last4" in CENTER_JS


def test_delete_failure_is_not_silently_reported_as_success():
    delete_block = PROVIDER_JS.split("for (const providerId of removedProviderSecrets)", 1)[1]
    assert "if (!response.ok)" in delete_block
    assert "throw new Error" in delete_block
    assert "removedProviderSecrets.clear()" in delete_block


def test_modal_and_lists_are_bounded_and_scrollable():
    overlay_rule = CSS.split("#cloud-llm-modal {", 1)[1].split("}", 1)[0]
    modal_rule = CSS.split(".cloud-llm-modal-box {", 1)[1].split("}", 1)[0]
    list_rule = CSS.split(".cloud-llm-library-list {", 1)[1].split("}", 1)[0]
    editor_rule = CSS.split(".cloud-llm-editor-list {", 1)[1].split("}", 1)[0]
    toolbar_rule = CSS.split(".cloud-llm-editor-toolbar {", 1)[1].split("}", 1)[0]
    toolbar_button_rule = CSS.split(".cloud-llm-editor-toolbar > .btn {", 1)[1].split("}", 1)[0]
    assert "z-index: 900" in overlay_rule
    assert "calc(100vh - 32px)" in modal_rule
    assert "padding: 0" in modal_rule
    assert "overflow: hidden" in modal_rule
    assert "overflow-y: auto" in list_rule
    assert "overflow-y: auto" in editor_rule
    assert "grid-template-columns: auto minmax(0, 1fr)" in toolbar_rule
    assert "width: auto" in toolbar_button_rule


def test_editor_form_has_labeled_balanced_sections():
    for label in ("API 供應商", "連線名稱", "模型或整合頁網址", "API Key"):
        assert f"<span>{label}" in PROVIDER_JS
    assert "model-provider-identity-grid" in PROVIDER_JS
    assert "model-provider-secret-grid" in PROVIDER_JS
    assert "model-provider-connectivity-row" in PROVIDER_JS


def test_cloud_controller_loads_before_the_app_initializer():
    center_script = HTML.index('src="cloud-llm-center.js')
    app_script = HTML.index('src="app.js')
    assert center_script < app_script
