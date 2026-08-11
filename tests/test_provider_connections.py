from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

import app as workbench_app
import local_session
import model_client as model_client_module
from api.routes.system import configured_model_summaries
from provider_connections import (
    ProviderConnectionFailure,
    catalog_payload,
    normalize_provider_endpoint,
    normalize_provider_settings,
    normalize_provider_source_url,
    model_id_from_source_url,
    test_provider_connection as run_connection_test,
    test_provider_model_response as run_model_test,
    test_provider_tool_call as run_tool_test,
)


def _response(status_code: int, payload: dict | None = None) -> Mock:
    response = Mock()
    response.status_code = status_code
    response.json.return_value = payload or {}
    return response


def test_catalog_has_official_gemini_nvidia_and_openai_entries():
    catalog = {item["id"]: item for item in catalog_payload()}
    assert catalog["gemini"]["official_url"] == "https://aistudio.google.com/apikey"
    assert catalog["nvidia"]["official_url"] == "https://build.nvidia.com/models"
    assert catalog["nvidia"]["base_url"] == "https://integrate.api.nvidia.com/v1"
    assert catalog["openai"]["base_url"] == "https://api.openai.com/v1"
    assert catalog["openai_compatible"]["endpoint_editable"] is True


def test_official_endpoints_are_fixed_but_custom_endpoint_is_allowed():
    assert normalize_provider_endpoint(
        "nvidia",
        "https://integrate.api.nvidia.com/v1/",
    ) == "https://integrate.api.nvidia.com/v1"
    with pytest.raises(ValueError, match="cannot be changed"):
        normalize_provider_endpoint("nvidia", "https://attacker.example/v1")
    assert normalize_provider_endpoint(
        "openai_compatible",
        "http://127.0.0.1:1234/v1",
    ) == "http://127.0.0.1:1234/v1"


def test_nvidia_model_page_url_is_pasteable_and_persisted():
    source_url = (
        "https://build.nvidia.com/nvidia/example-model"
        "?integrate_nim=true"
    )
    assert normalize_provider_source_url("nvidia", source_url) == source_url
    normalized = normalize_provider_settings([{
        "id": "nvidia",
        "provider_type": "nvidia",
        "label": "NVIDIA",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "source_url": source_url,
    }])
    assert normalized[0]["source_url"] == source_url
    assert normalized[0]["selected_model"] == "nvidia/example-model"
    assert normalized[0]["model_kind"] == "unknown"
    assert normalized[0]["enabled"] is False
    assert normalized[0].get("supports_tools", False) is False
    normalized_with_model = normalize_provider_settings([{
        **normalized[0],
        "selected_model": "nvidia/riva-translate-4b-instruct-v2",
    }])
    assert normalized_with_model[0]["selected_model"] == "nvidia/example-model"
    with pytest.raises(ValueError, match="selected provider"):
        normalize_provider_source_url("nvidia", "https://attacker.example/model")

def test_riva_profile_is_persisted_as_translation_and_not_chat_eligible():
    normalized = normalize_provider_settings([{
        "id": "nvidia",
        "provider_type": "nvidia",
        "label": "NVIDIA",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "enabled": False,
        "selected_model": "nvidia/riva-translate-4b-instruct-v2",
        "model_kind": "translation",
        "supports_tools": True,
        "language_pair": "en-zh-tw",
    }])[0]
    assert normalized["enabled"] is False
    assert normalized["model_kind"] == "translation"
    assert normalized["supports_tools"] is False
    assert normalized["language_pair"] == "en-zh-tw"
    assert normalized["capability_profile"]["eligible_for_primary"] is False
    assert normalized["capability_profile"]["eligible_for_subagent"] is False


def test_provider_flags_are_strict_booleans_and_are_always_persisted():
    provider = {
        "id": "remote",
        "provider_type": "openai_compatible",
        "base_url": "https://provider.example/v1",
        "enabled": True,
        "supports_tools": True,
    }
    normalized = normalize_provider_settings([provider])[0]
    assert normalized["enabled"] is True
    assert normalized["supports_tools"] is True

    for field in ("enabled", "supports_tools"):
        with pytest.raises(ValueError, match=f"{field} must be a boolean"):
            normalize_provider_settings([{**provider, field: "false"}])


def test_known_translation_model_rejects_explicit_chat_override():
    with pytest.raises(ValueError, match="conflicts with known specialized"):
        normalize_provider_settings([{
            "id": "nvidia",
            "provider_type": "nvidia",
            "label": "NVIDIA",
            "base_url": "https://integrate.api.nvidia.com/v1",
            "selected_model": "nvidia/riva-translate-4b-instruct-v2",
            "model_kind": "chat",
            "supports_tools": True,
        }])


def test_known_chat_model_replaces_stale_translation_profile():
    normalized = normalize_provider_settings([{
        "id": "nvidia",
        "provider_type": "nvidia",
        "label": "NVIDIA",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "source_url": "https://build.nvidia.com/nvidia/nemotron-3-super-120b-a12b",
        "selected_model": "nvidia/riva-translate-4b-instruct-v2",
        "model_kind": "translation",
        "language_pair": "en-zh-tw",
    }])[0]

    assert normalized["selected_model"] == "nvidia/nemotron-3-super-120b-a12b"
    assert normalized["model_kind"] == "chat"
    assert "language_pair" not in normalized
    assert normalized["capability_profile"]["supports_chat"] is True
    assert normalized["capability_profile"]["eligible_for_primary"] is True
    assert normalized["capability_profile"]["eligible_for_subagent"] is True


def test_known_chat_model_replaces_stale_unknown_profile():
    normalized = normalize_provider_settings([{
        "id": "nvidia",
        "provider_type": "nvidia",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "source_url": "https://build.nvidia.com/nvidia/nemotron-3-super-120b-a12b",
        "selected_model": "vendor/opaque-model",
        "model_kind": "unknown",
    }])[0]

    assert normalized["selected_model"] == "nvidia/nemotron-3-super-120b-a12b"
    assert normalized["model_kind"] == "chat"
    assert normalized["capability_profile"]["eligible_for_primary"] is True


def test_explicit_specialized_kind_remains_valid_for_opaque_model_id():
    normalized = normalize_provider_settings([{
        "id": "remote",
        "provider_type": "openai_compatible",
        "base_url": "https://provider.example/v1",
        "selected_model": "vendor/model-2026-07",
        "model_kind": "translation",
        "language_pair": "en-zh-tw",
    }])[0]

    assert normalized["model_kind"] == "translation"
    assert normalized["language_pair"] == "en-zh-tw"
    assert normalized["capability_profile"]["eligible_for_primary"] is False


def test_capability_change_invalidates_old_tool_attestation():
    base = {
        "id": "remote",
        "provider_type": "openai_compatible",
        "label": "Remote",
        "base_url": "https://provider.example/v1",
        "selected_model": "vendor/chat-instruct",
        "model_kind": "chat",
        "supports_tools": True,
    }
    first = normalize_provider_settings([base])[0]
    fingerprint = first["capability_profile"]["fingerprint"]
    attested = normalize_provider_settings([{
        **base,
        "tool_attestation": {
            "profile_fingerprint": fingerprint,
            "verified_at": "2026-07-30T00:00:00Z",
            "method": "synthetic_tool_call",
            "passed": True,
        },
    }])[0]
    assert attested["tool_attestation"]["profile_fingerprint"] == fingerprint
    assert attested["tool_attestation"]["passed"] is True
    missing_pass = normalize_provider_settings([{
        **base,
        "tool_attestation": {
            "profile_fingerprint": fingerprint,
            "verified_at": "2026-07-30T00:00:00Z",
            "method": "synthetic_tool_call",
        },
    }])[0]
    assert "tool_attestation" not in missing_pass
    changed = normalize_provider_settings([{
        **base,
        "selected_model": "vendor/other-chat-instruct",
        "tool_attestation": attested["tool_attestation"],
    }])[0]
    assert "tool_attestation" not in changed


def test_unknown_model_kind_is_persisted_fail_closed():
    normalized = normalize_provider_settings([{
        "id": "remote",
        "provider_type": "openai_compatible",
        "base_url": "https://provider.example/v1",
        "selected_model": "vendor/opaque-model",
    }])[0]
    assert normalized["model_kind"] == "unknown"
    assert normalized["capability_profile"]["eligible_for_primary"] is False
    assert normalized["capability_profile"]["eligible_roles"] == []


@pytest.mark.parametrize(
    ("provider_type", "source_url", "expected"),
    [
        (
            "nvidia",
            "https://build.nvidia.com/nvidia/riva-translate-4b-instruct-v2"
            "?integrate_nim=true&hosted_api=true",
            "nvidia/riva-translate-4b-instruct-v2",
        ),
        (
            "openai",
            "https://platform.openai.com/docs/models/gpt-4.1",
            "gpt-4.1",
        ),
        (
            "openai_compatible",
            "https://models.example/catalog?model=vendor%2Fmodel-a",
            "vendor/model-a",
        ),
    ],
)
def test_model_id_is_derived_from_provider_urls(provider_type, source_url, expected):
    assert model_id_from_source_url(provider_type, source_url) == expected


def test_source_scoped_connection_returns_only_the_url_model():
    target = "nvidia/riva-translate-4b-instruct-v2"
    response = _response(200, {
        "data": [{"id": "other/model"}, {"id": target}, {"id": "third/model"}],
    })
    with patch("provider_connections.requests.get", return_value=response):
        result = run_connection_test(
            provider_type="nvidia",
            base_url="https://integrate.api.nvidia.com/v1",
            api_key="nvapi-test-secret",
            source_url=f"https://build.nvidia.com/{target}?hosted_api=true",
        )
    assert result["model_count"] == 1
    assert result["models"] == [target]


def test_connection_test_lists_models_without_sending_project_content():
    response = _response(200, {"data": [{"id": "model-a"}, {"id": "model-b"}]})
    with patch("provider_connections.requests.get", return_value=response) as get:
        result = run_connection_test(
            provider_type="nvidia",
            base_url="https://integrate.api.nvidia.com/v1",
            api_key="nvapi-test-secret",
        )
    assert result["status"] == "connected"
    assert result["model_count"] == 2
    assert result["models"] == ["model-a", "model-b"]
    assert get.call_args.args == ("https://integrate.api.nvidia.com/v1/models",)
    assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer nvapi-test-secret"
    assert "data" not in get.call_args.kwargs


def test_model_test_selects_exact_model_and_returns_its_reply():
    model_id = "nvidia/riva-translate-4b-instruct-v2"
    listed = _response(200, {"data": [{"id": model_id}]})
    completed = _response(200, {
        "choices": [{"message": {"role": "assistant", "content": "模型連線測試成功。"}}],
    })
    with patch(
        "provider_connections.requests.get",
        return_value=listed,
    ), patch(
        "provider_connections.requests.post",
        return_value=completed,
    ) as post:
        result = run_model_test(
            provider_type="nvidia",
            base_url="https://integrate.api.nvidia.com/v1",
            api_key="nvapi-test-secret",
            model=model_id,
            system_prompt="en-zh-tw",
            prompt="Hello.",
        )

    assert result == {
        "status": "responded",
        "selected_model": model_id,
        "response": "模型連線測試成功。",
        "model_profile": {
            "kind": "translation",
            "adapter": "language_pair_system",
            "supports_chat": False,
            "supports_stream": True,
            "supports_tools": False,
            "eligible_for_primary": False,
            "eligible_for_subagent": False,
            "eligible_roles": [],
            "language_pair": "en-zh-tw",
        },
    }
    assert post.call_args.args == (
        "https://integrate.api.nvidia.com/v1/chat/completions",
    )
    assert post.call_args.kwargs["json"]["model"] == model_id
    assert post.call_args.kwargs["json"]["messages"] == [
        {"role": "system", "content": "en-zh-tw"},
        {"role": "user", "content": "Hello."},
    ]
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer nvapi-test-secret"


def test_preflight_and_runtime_share_the_exact_chat_payload_builder():
    model_id = "vendor/chat-instruct"
    listed = _response(200, {"data": [{"id": model_id}]})
    completed = _response(200, {
        "choices": [{"message": {"content": "ok"}}],
    })
    with patch(
        "provider_connections.requests.get",
        return_value=listed,
    ), patch(
        "provider_connections.requests.post",
        return_value=completed,
    ) as preflight_post:
        run_model_test(
            provider_type="openai_compatible",
            base_url="https://provider.example/v1",
            api_key="test-secret",
            model=model_id,
            model_kind="chat",
            system_prompt="You are helpful.",
            prompt="Hello.",
        )
    preflight_payload = preflight_post.call_args.kwargs["json"]

    settings = {
        "model_providers": [{
            "id": "remote",
            "base_url": "https://provider.example/v1",
            "enabled": True,
            "selected_model": model_id,
            "model_kind": "chat",
            "supports_tools": False,
        }],
    }
    original_gate = model_client_module._PROVIDER_EXTENSION_GATE
    model_client_module.configure_provider_extension_gate(None)
    try:
        with patch.object(
            model_client_module,
            "get_provider_secret",
            return_value="test-secret",
        ), patch.object(
            model_client_module.requests,
            "post",
            return_value=completed,
        ) as runtime_post:
            model_client_module.post_chat(
                settings,
                {
                    "model": f"remote::{model_id}",
                    "messages": [
                        {"role": "system", "content": "You are helpful."},
                        {"role": "user", "content": "Hello."},
                    ],
                    "max_tokens": 512,
                    "temperature": 0.2,
                },
                stream=False,
            )
    finally:
        model_client_module.configure_provider_extension_gate(original_gate)
    assert runtime_post.call_args.kwargs["json"] == preflight_payload


def test_tool_attestation_requires_call_and_completion_handshake():
    model_id = "vendor/chat-instruct"
    nonce = "one-time-nonce-123"
    listed = _response(200, {"data": [{"id": model_id}]})
    called = _response(200, {
        "choices": [{
            "message": {
                "tool_calls": [{
                    "id": "call-probe-1",
                    "type": "function",
                    "function": {
                        "name": "workbench_capability_probe",
                        "arguments": json.dumps({"nonce": nonce}),
                    },
                }],
            },
        }],
    })
    completed = _response(200, {
        "choices": [{"message": {"content": f"Verified {nonce}"}}],
    })
    with patch(
        "provider_connections.secrets.token_urlsafe",
        return_value=nonce,
    ), patch(
        "provider_connections.requests.get",
        return_value=listed,
    ), patch(
        "provider_connections.requests.post",
        side_effect=[called, completed],
    ) as post:
        result = run_tool_test(
            provider_type="openai_compatible",
            base_url="https://provider.example/v1",
            api_key="private-test-key",
            model=model_id,
            model_kind="chat",
            supports_tools=True,
        )

    first_payload = post.call_args_list[0].kwargs["json"]
    second_payload = post.call_args_list[1].kwargs["json"]
    assert first_payload["tool_choice"] == {
        "type": "function",
        "function": {"name": "workbench_capability_probe"},
    }
    assert first_payload["tools"][0]["function"]["name"] == (
        "workbench_capability_probe"
    )
    assert second_payload["tool_choice"] == "none"
    assert second_payload["messages"][1]["tool_calls"][0]["id"] == "call-probe-1"
    assert second_payload["messages"][2] == {
        "role": "tool",
        "tool_call_id": "call-probe-1",
        "name": "workbench_capability_probe",
        "content": json.dumps(
            {"nonce": nonce, "status": "verified"},
            separators=(",", ":"),
        ),
    }
    assert result["status"] == "tool_call_verified"
    assert result["tool_attestation"]["passed"] is True
    assert result["tool_attestation"]["method"] == "synthetic_tool_call"
    assert "private-test-key" not in json.dumps(result)


def test_tool_attestation_fails_when_completion_does_not_consume_result():
    model_id = "vendor/chat-instruct"
    nonce = "one-time-nonce-456"
    listed = _response(200, {"data": [{"id": model_id}]})
    called = _response(200, {
        "choices": [{
            "message": {
                "tool_calls": [{
                    "id": "call-probe-2",
                    "function": {
                        "name": "workbench_capability_probe",
                        "arguments": json.dumps({"nonce": nonce}),
                    },
                }],
            },
        }],
    })
    ignored = _response(200, {
        "choices": [{"message": {"content": "done"}}],
    })
    with patch(
        "provider_connections.secrets.token_urlsafe",
        return_value=nonce,
    ), patch(
        "provider_connections.requests.get",
        return_value=listed,
    ), patch(
        "provider_connections.requests.post",
        side_effect=[called, ignored],
    ), pytest.raises(ProviderConnectionFailure) as caught:
        run_tool_test(
            provider_type="openai_compatible",
            base_url="https://provider.example/v1",
            api_key="private-test-key",
            model=model_id,
            model_kind="chat",
            supports_tools=True,
        )
    assert caught.value.code == "PROVIDER_TOOL_ATTESTATION_FAILED"
    assert "private-test-key" not in str(caught.value)


def test_model_test_refuses_a_model_not_returned_for_the_key():
    with patch(
        "provider_connections.requests.get",
        return_value=_response(200, {"data": [{"id": "available/model"}]}),
    ), patch("provider_connections.requests.post") as post, pytest.raises(
        ProviderConnectionFailure,
    ) as failure:
        run_model_test(
            provider_type="nvidia",
            base_url="https://integrate.api.nvidia.com/v1",
            api_key="nvapi-test-secret",
            model="missing/model",
            model_kind="chat",
            system_prompt="",
            prompt="Hello.",
        )
    assert failure.value.code == "PROVIDER_MODEL_NOT_FOUND"
    post.assert_not_called()


def test_model_preflight_400_keeps_safe_reason_without_sensitive_values():
    model_id = "vendor/chat-instruct"
    listed = _response(200, {"data": [{"id": model_id}]})
    rejected = _response(400)
    rejected.text = json.dumps({
        "error": {
            "message": (
                "Invalid request for tenant tenant-private; "
                "nvapi-ABCDEFGHIJKLMNOP admin@example.com "
                "123e4567-e89b-42d3-a456-426614174000 <em>bad</em>"
            ),
        },
    })
    with patch(
        "provider_connections.requests.get",
        return_value=listed,
    ), patch(
        "provider_connections.requests.post",
        return_value=rejected,
    ), pytest.raises(ProviderConnectionFailure) as caught:
        run_model_test(
            provider_type="openai_compatible",
            base_url="https://provider.example/v1",
            api_key="nvapi-ABCDEFGHIJKLMNOP",
            model=model_id,
            model_kind="chat",
            system_prompt="You are helpful.",
            prompt="Hello.",
        )
    message = str(caught.value)
    assert "Invalid request" in message
    assert "tenant-private" not in message
    assert "nvapi-" not in message
    assert "admin@example.com" not in message
    assert "123e4567" not in message
    assert "<em>" not in message


def test_model_test_reports_listed_but_unavailable_model():
    with patch(
        "provider_connections.requests.get",
        return_value=_response(200, {"data": [{"id": "listed/model"}]}),
    ), patch(
        "provider_connections.requests.post",
        return_value=_response(404, {"detail": "private upstream account detail"}),
    ), pytest.raises(ProviderConnectionFailure) as failure:
        run_model_test(
            provider_type="nvidia",
            base_url="https://integrate.api.nvidia.com/v1",
            api_key="nvapi-test-secret",
            model="listed/model",
            model_kind="chat",
            system_prompt="",
            prompt="Hello.",
        )
    assert failure.value.code == "PROVIDER_MODEL_UNAVAILABLE"
    assert failure.value.status_code == 409
    assert "private upstream account detail" not in str(failure.value)


@pytest.mark.parametrize(
    ("status_code", "code"),
    [
        (401, "PROVIDER_AUTH_FAILED"),
        (403, "PROVIDER_AUTH_FAILED"),
        (429, "PROVIDER_RATE_LIMITED"),
        (503, "PROVIDER_UPSTREAM_ERROR"),
    ],
)
def test_connection_test_classifies_provider_failures(status_code, code):
    with patch(
        "provider_connections.requests.get",
        return_value=_response(status_code),
    ), pytest.raises(ProviderConnectionFailure) as failure:
        run_connection_test(
            provider_type="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            api_key="gemini-test-secret",
        )
    assert failure.value.code == code
    assert "gemini-test-secret" not in str(failure.value)


@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [
        (429, "PROVIDER_RATE_LIMITED"),
        (503, "PROVIDER_UPSTREAM_ERROR"),
    ],
)
def test_preflight_rate_and_server_errors_keep_only_safe_reason(
    status_code,
    expected_code,
):
    rejected = _response(status_code)
    rejected.text = json.dumps({
        "error": {
            "message": (
                "Capacity temporarily exhausted for tenant private-tenant; "
                "nvapi-ABCDEFGHIJKLMNOP admin@example.com"
            ),
        },
    })
    with patch(
        "provider_connections.requests.get",
        return_value=rejected,
    ), pytest.raises(ProviderConnectionFailure) as failure:
        run_connection_test(
            provider_type="nvidia",
            base_url="https://integrate.api.nvidia.com/v1",
            api_key="nvapi-ABCDEFGHIJKLMNOP",
        )
    message = str(failure.value)
    assert failure.value.code == expected_code
    assert "Capacity temporarily exhausted" in message
    assert "private-tenant" not in message
    assert "nvapi-" not in message
    assert "admin@example.com" not in message


def test_connection_test_classifies_timeout_without_leaking_secret():
    with patch(
        "provider_connections.requests.get",
        side_effect=requests.Timeout("request carried super-secret-key"),
    ), pytest.raises(ProviderConnectionFailure) as failure:
        run_connection_test(
            provider_type="openai",
            base_url="https://api.openai.com/v1",
            api_key="super-secret-key",
        )
    assert failure.value.code == "PROVIDER_TIMEOUT"
    assert "super-secret-key" not in str(failure.value)


def test_provider_catalog_and_test_api_never_return_secret(tmp_path):
    settings_path = tmp_path / "settings.json"
    secret_path = tmp_path / "provider-secrets.json"
    headers = {
        "Origin": "http://127.0.0.1:8080",
        "X-Workbench-Token": local_session.session_token(),
    }
    secret = "nvapi-route-test-secret"
    with patch.dict(
        os.environ,
        {
            "WORKBENCH_SETTINGS_PATH": str(settings_path),
            "WORKBENCH_SECRET_STORE_PATH": str(secret_path),
        },
        clear=False,
    ), patch(
        "provider_connections.requests.get",
        return_value=_response(200, {"data": [{"id": "nvidia/model"}]}),
    ), TestClient(workbench_app.app) as client:
        catalog = client.get("/api/settings/providers/catalog", headers=headers)
        assert catalog.status_code == 200
        assert "https://build.nvidia.com/models" in catalog.text

        tested = client.post(
            "/api/settings/providers/test",
            headers=headers,
            json={
                "provider_id": "nvidia",
                "provider_type": "nvidia",
                "base_url": "https://integrate.api.nvidia.com/v1",
                "api_key": secret,
            },
        )
        assert tested.status_code == 200
        assert tested.json()["model_count"] == 1
        assert secret not in tested.text


def test_provider_test_api_rejects_nonlocal_mutation(tmp_path):
    settings_path = tmp_path / "settings.json"
    with patch.dict(
        os.environ,
        {"WORKBENCH_SETTINGS_PATH": str(settings_path)},
        clear=False,
    ), TestClient(workbench_app.app) as client:
        response = client.post(
            "/api/settings/providers/test",
            headers={"Origin": "https://attacker.example"},
            json={
                "provider_id": "nvidia",
                "provider_type": "nvidia",
                "base_url": "https://integrate.api.nvidia.com/v1",
                "api_key": "never-sent",
            },
        )
    assert response.status_code in {401, 403}


def test_provider_model_test_api_returns_reply_without_echoing_secret(tmp_path):
    model_id = "nvidia/riva-translate-4b-instruct-v2"
    secret = "nvapi-route-model-secret"
    headers = {
        "Origin": "http://127.0.0.1:8080",
        "X-Workbench-Token": local_session.session_token(),
    }
    with patch.dict(
        os.environ,
        {"WORKBENCH_SETTINGS_PATH": str(tmp_path / "settings.json")},
        clear=False,
    ), patch(
        "provider_connections.requests.get",
        return_value=_response(200, {"data": [{"id": model_id}]}),
    ), patch(
        "provider_connections.requests.post",
        return_value=_response(200, {
            "choices": [{"message": {"content": "已由指定模型回覆"}}],
        }),
    ), TestClient(workbench_app.app) as client:
        response = client.post(
            "/api/settings/providers/model-test",
            headers=headers,
            json={
                "provider_id": "nvidia",
                "provider_type": "nvidia",
                "base_url": "https://integrate.api.nvidia.com/v1",
                "api_key": secret,
                "model": model_id,
                "system_prompt": "en-zh-tw",
                "prompt": "Hello.",
            },
        )
    assert response.status_code == 200
    assert response.json()["selected_model"] == model_id
    assert response.json()["response"] == "已由指定模型回覆"
    assert "tool_attestation" not in response.json()
    assert secret not in response.text


def test_provider_tool_test_api_returns_only_passed_attestation(tmp_path):
    model_id = "vendor/chat-instruct"
    nonce = "route-nonce-789"
    secret = "route-private-key"
    headers = {
        "Origin": "http://127.0.0.1:8080",
        "X-Workbench-Token": local_session.session_token(),
    }
    listed = _response(200, {"data": [{"id": model_id}]})
    called = _response(200, {
        "choices": [{"message": {"tool_calls": [{
            "id": "route-call-1",
            "function": {
                "name": "workbench_capability_probe",
                "arguments": json.dumps({"nonce": nonce}),
            },
        }]}}],
    })
    completed = _response(200, {
        "choices": [{"message": {"content": f"Verified {nonce}"}}],
    })
    with patch.dict(
        os.environ,
        {"WORKBENCH_SETTINGS_PATH": str(tmp_path / "settings.json")},
        clear=False,
    ), patch(
        "provider_connections.secrets.token_urlsafe",
        return_value=nonce,
    ), patch(
        "provider_connections.requests.get",
        return_value=listed,
    ), patch(
        "provider_connections.requests.post",
        side_effect=[called, completed],
    ), TestClient(workbench_app.app) as client:
        response = client.post(
            "/api/settings/providers/tool-test",
            headers=headers,
            json={
                "provider_id": "remote",
                "provider_type": "openai_compatible",
                "base_url": "https://provider.example/v1",
                "api_key": secret,
                "model": model_id,
                "model_kind": "chat",
                "supports_tools": True,
            },
        )
    assert response.status_code == 200
    assert response.json()["tool_attestation"]["passed"] is True
    assert response.json()["tool_attestation"]["method"] == "synthetic_tool_call"
    assert secret not in response.text


def test_frontend_exposes_simple_multi_api_import_without_routing_ui():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "frontend" / "extension-center.js").read_text(
        encoding="utf-8"
    )
    app_javascript = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")
    assert "新增 API" in html
    assert "API 連線" in html
    assert "https://build.nvidia.com/models" in javascript
    assert "https://aistudio.google.com/apikey" in javascript
    assert 'data-provider-field="provider_type"' in javascript
    assert 'data-provider-field="source_url"' in javascript
    assert 'data-provider-field="api_key"' in javascript
    assert 'data-provider-field="selected_model"' in javascript
    assert 'class="btn btn-primary compact model-provider-test"' in javascript
    assert 'id="cloud-llm-modal"' in html
    assert 'id="btn-add-model-provider"' in html
    assert 'id="cloud-llm-library-list"' in html
    assert "取得模型回覆" in javascript
    assert "data-test-provider" in javascript
    assert "/api/settings/providers/test" in javascript
    assert "/api/settings/providers/model-test" in javascript
    assert "功能路由" not in html
    provider_grid = css.split(".model-provider-identity-grid,", 1)[1].split("}", 1)[0]
    assert "grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)" in provider_grid
    assert "minmax(280px" not in provider_grid
    assert "configured_models" in app_javascript
    assert "啟用並切換" in app_javascript
    assert "reviewProviderModel" in javascript


def test_configured_api_model_summary_is_safe_and_namespaced():
    summaries = configured_model_summaries({
        "model_providers": [{
            "id": "nvidia",
            "label": "NVIDIA API Catalog",
            "base_url": "https://integrate.api.nvidia.com/v1",
            "api_key": "must-never-be-returned",
            "selected_model": "nvidia/riva-translate-4b-instruct-v2",
        }],
    })

    assert summaries == [{
        "name": "nvidia::nvidia/riva-translate-4b-instruct-v2",
        "provider": "nvidia",
        "provider_label": "NVIDIA API Catalog",
        "selected_model": "nvidia/riva-translate-4b-instruct-v2",
        "extension_id": "provider.nvidia",
        "model_kind": "translation",
        "eligible_for_chat": False,
        "eligible_roles": [],
    }]
    assert "api_key" not in summaries[0]
    assert "base_url" not in summaries[0]


def test_specialized_model_is_rejected_before_chat_message_or_network():
    settings = {
        "model_providers": [{
            "id": "nvidia",
            "base_url": "https://integrate.api.nvidia.com/v1",
            "enabled": True,
            "selected_model": "nvidia/riva-translate-4b-instruct-v2",
            "model_kind": "translation",
            "language_pair": "en-zh-cn",
        }],
    }
    from chat_cancellation import ChatRunControl

    control = ChatRunControl(
        "run-specialized", "session", "turn", "nvidia::nvidia/riva-translate-4b-instruct-v2", "chat"
    )
    import model_client as model_client_module
    original_gate = model_client_module._PROVIDER_EXTENSION_GATE
    configure_provider_extension_gate = model_client_module.configure_provider_extension_gate

    configure_provider_extension_gate(None)
    try:
        with pytest.raises(workbench_app.HTTPException) as caught:
            workbench_app.configure_chat_run_billing(
                control,
                settings,
                "nvidia::nvidia/riva-translate-4b-instruct-v2",
                None,
            )
    finally:
        configure_provider_extension_gate(original_gate)
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "MODEL_NOT_CHAT_CAPABLE"
