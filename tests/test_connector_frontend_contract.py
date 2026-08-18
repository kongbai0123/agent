from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONNECTORS = (ROOT / "frontend" / "connector-center.js").read_text(encoding="utf-8")


def test_frontend_uses_public_connection_id_contract():
    assert "function connectionId(connection)" in CONNECTORS
    assert "connection?.connection_id" in CONNECTORS
    assert "encodeURIComponent(connection.id)" not in CONNECTORS
    assert "connection.display_name" in CONNECTORS


def test_blank_secret_update_and_remote_revoke_error_contract():
    assert "if (secret.value) body.client_secret = secret.value" in CONNECTORS
    assert "CONNECTOR_REVOKE_FAILED" in CONNECTORS
    assert "payload?.detail?.code || payload?.code" in CONNECTORS
    assert "const configured = state.profiles" not in CONNECTORS
    assert "Workbench 位址已變更" in CONNECTORS


def test_oauth_polling_detects_a_new_or_changed_connection():
    assert "/api/connectors/connections?connector_id=" in CONNECTORS
    assert "connectionVersion(connection)" in CONNECTORS
    assert "baseline.has(id)" in CONNECTORS
    assert "result.expires_at" in CONNECTORS
    assert "clearTimeout(state.polling.timer)" in CONNECTORS


def test_resource_picker_uses_revision_and_recovers_from_conflict():
    assert "revision: Number(current.revision || 0)" in CONNECTORS
    assert "RESOURCE_BINDING_REVISION_CONFLICT" in CONNECTORS
    assert "await renderResourcePicker(container, connector, connection)" in CONNECTORS
    assert "connection.binding = bindingUpdate.binding" in CONNECTORS
    assert "save.disabled = !available.length" not in CONNECTORS


def test_project_selection_and_extension_gate_are_shared_with_extension_center():
    assert "function setProject(projectId)" in CONNECTORS
    assert "workbench:connector-project-change" in CONNECTORS
    assert "Object.prototype.hasOwnProperty.call(options, 'projectId')" in CONNECTORS
    assert "request(`/api/extensions${projectQuery}`).catch(() => null)" in CONNECTORS
    assert "state.extensionCatalogReady" in CONNECTORS
    assert "extension?.installed" in CONNECTORS
    assert "extension?.trusted" in CONNECTORS
    assert "extension?.effective_enabled" in CONNECTORS
    assert "connectionRow(connector, connection, extensionReady)" in CONNECTORS
    assert "window.workbenchExtensions?.open?.(" in CONNECTORS
