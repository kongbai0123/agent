from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")
JS = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")


def rule(selector: str, marker: str = "") -> str:
    matches = re.findall(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert matches, f"missing CSS selector: {selector}"
    if marker:
        return next((body for body in matches if marker in body), "")
    return matches[-1]


def test_chat_messages_share_a_centered_reading_column():
    message = rule(".message", "max-width: 880px")
    assistant = rule(".message.assistant .message-content-wrapper", "flex: 1 1 auto")
    user = rule(".message.user .message-content-wrapper", "max-width: min(68%, 560px)")

    assert "max-width: 880px" in message
    assert "align-items: flex-start" in message
    assert "flex: 1 1 auto" in assistant
    assert "max-width: min(68%, 560px)" in user


def test_user_and_assistant_bubbles_have_balanced_intrinsic_spacing():
    assistant = rule(".message.assistant .message-bubble", "padding: 18px 20px")
    user = rule(".message.user .message-bubble", "padding: 11px 16px")

    assert "padding: 18px 20px" in assistant
    assert "width: 100%" in assistant
    assert "width: fit-content" in user
    assert "min-height: 0" in user
    assert "height: auto" in user
    assert "padding: 11px 16px" in user
    assert '? `<div class="assistant-answer-content">${renderedContent}</div>`' in JS
    assert '<div class="message-bubble">${content}</div>' in JS


def test_mobile_layout_preserves_readable_margins():
    mobile = CSS.rsplit("@media (max-width: 640px)", 1)[1]

    assert "padding: 18px 12px 22px" in mobile
    assert "width: 32px" in mobile
    assert "max-width: min(82%, 520px)" in mobile
