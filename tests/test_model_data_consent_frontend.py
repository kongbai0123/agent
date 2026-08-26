from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
STYLE = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")


def _section(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    return source[begin : source.index(end, begin)]


def test_document_consent_uses_a_dedicated_accessible_dialog():
    dialog = _section(
        INDEX,
        '<dialog id="model-data-consent-dialog"',
        "</dialog>",
    )
    assert 'aria-labelledby="model-data-consent-title"' in dialog
    assert 'aria-describedby="model-data-consent-description"' in dialog
    assert 'id="model-data-consent-provider"' in dialog
    assert 'id="model-data-consent-model"' in dialog
    assert 'id="model-data-consent-data-type"' in dialog
    assert 'id="model-data-consent-risk"' in dialog
    assert 'id="model-data-consent-consequences"' in dialog
    assert "10 分鐘內有效" in dialog
    assert "只能使用一次" in dialog
    assert 'id="model-data-consent-cancel"' in dialog
    assert 'id="model-data-consent-once"' in dialog
    assert 'id="model-data-consent-remember"' in dialog
    assert "圖片或文件" in dialog


def test_chat_conflict_loop_approves_once_or_remembers_then_resends_same_payload():
    conflict = _section(
        APP,
        "if (response.status !== 409) break;",
        "if (!response.ok)",
    )
    assert "detail.code === 'MODEL_DATA_CONSENT_REQUIRED'" in conflict
    assert "await requestModelDataConsent(proposal)" in conflict
    assert "rememberProject = consentChoice === 'remember'" in conflict
    assert "JSON.stringify({ remember_project: rememberProject })" in conflict
    assert "payload.routing_proposal_id = proposal.proposal_id" in conflict
    assert "MODEL_DATA_CONSENT_CANCELLED" in conflict
    assert "本次未將任何圖片或文件內容送往雲端" in conflict
    assert "rememberButton.disabled = !canRememberProject" in APP


def test_cancelled_document_consent_has_correct_recovery_action():
    recovery = _section(APP, "function chatRecoveryProfile", "function renderConnectionErrorCard")
    bindings = _section(APP, "function bindChatRecoveryActions", "// ---- 生成狀態 UI")
    assert "已取消資料上雲" in recovery
    assert "model_data_policy" in recovery
    assert "window.workbenchCloudLlm?.openTab?.('budgets')" in bindings


def test_document_consent_layer_is_responsive_scrollable_and_above_shell_surfaces():
    assert "原生 dialog 進入 top layer" in STYLE
    dialog_css = _section(
        STYLE,
        ".model-data-consent-dialog {",
        "/* --- Command Palette --- */",
    )
    assert "z-index: calc(var(--z-modal) + 2)" in dialog_css
    assert "max-height: min(760px, calc(100dvh - 32px))" in dialog_css
    assert "overflow-y: auto" in dialog_css
    assert "overscroll-behavior: contain" in dialog_css
    assert "@media (max-width: 640px)" in dialog_css
    assert "grid-template-columns: 1fr" in dialog_css
    assert "flex-direction: column-reverse" in dialog_css
