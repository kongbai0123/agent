from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")


def test_chat_http_errors_keep_backend_governance_details():
    assert "throw chatResponseError(response, payload.detail || payload)" in APP
    assert "failure.code = safeDetail.code" in APP
    assert "failure.actions" in APP


def test_cloud_provider_failure_is_not_mislabeled_as_ollama_failure():
    profile = APP[APP.index("function chatRecoveryProfile") : APP.index("function renderConnectionErrorCard")]
    assert "code.startsWith('PROVIDER_')" in profile
    assert "檢查 API 連線" in profile
    assert "查看用量與健康" in profile
    assert "無法連線至本機 Ollama 模型服務" in profile
    assert profile.index("code.startsWith('PROVIDER_')") < profile.index("code.startsWith('OLLAMA_')")
    assert "const isOllamaModel = !!normalizedModel && !normalizedModel.includes('::')" in profile
    assert "code.startsWith('OLLAMA_') || isOllamaModel" in profile


def test_declined_route_is_a_cancelled_run_not_a_connection_failure():
    assert "cancelled.code = 'MODEL_ROUTE_CANCELLED'" in APP
    assert "chatProgressStatus = 'cancelled'" in APP
    assert "已取消模型切換" in APP
    assert "已保留原模型，尚未送出新的模型請求" in APP
