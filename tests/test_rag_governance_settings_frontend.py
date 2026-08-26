"""Non-technical UI contract for semantic retrieval and answer verification."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")


def test_rag_settings_offer_baseline_provider_and_existing_local_model_choices():
    for element_id in (
        "setting-rag-embedding-backend",
        "setting-rag-embedding-local-path",
        "setting-rag-reranker-backend",
        "setting-rag-reranker-local-path",
    ):
        assert f'id="{element_id}"' in HTML
    assert HTML.count("本機保守基線（不需下載模型）") >= 2
    assert "Workbench 不會自動下載模型" in HTML
    assert "第一次把專案文件送到雲端前仍須取得專案同意" in APP
    assert "provider.model_kind" in APP
    assert "modelKind === 'embedding'" in APP
    assert "String(provider.model_kind || '').trim().toLowerCase() === modelKind" in APP


def test_rag_backend_and_answer_verification_values_round_trip_through_settings_api():
    for key in (
        "rag_embedding_provider_id",
        "rag_reranker_provider_id",
        "rag_local_embedding_model_path",
        "rag_local_reranker_model_path",
        "answer_verification_mode",
    ):
        assert key in APP
    assert "collectRagModelBackendSettings()" in APP
    assert "refreshRagModelBackendOptions(data.model_providers || [], data)" in APP
    assert "['warn', 'strict', 'off'].includes(data.answer_verification_mode)" in APP


def test_answer_verification_copy_explains_each_mode_and_keeps_fixed_safety_checks():
    assert 'id="setting-answer-verification-mode"' in HTML
    rag_start = HTML.index('id="tab-settings-rag"')
    agent_start = HTML.index('id="tab-settings-agent"')
    verification_start = HTML.index('id="setting-answer-verification-mode"')
    assert rag_start < verification_start < agent_start
    assert '<option value="warn" selected>提醒（建議）</option>' in HTML
    assert '<option value="strict">嚴格</option>' in HTML
    assert '<option value="off">關閉</option>' in HTML
    assert "證據不足或引用不一致" in HTML
    assert "固定安全政策與工具結果驗證仍然有效" in HTML
