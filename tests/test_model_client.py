import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import model_client


class ModelClientTests(unittest.TestCase):
    def setUp(self):
        self.original_extension_gate = model_client._PROVIDER_EXTENSION_GATE
        model_client.configure_provider_extension_gate(None)

    def tearDown(self):
        model_client.configure_provider_extension_gate(
            self.original_extension_gate
        )

    def test_empty_provider_list_preserves_legacy_openai_compatible_settings(self):
        settings = {
            "model_provider": "openai_compatible",
            "model_providers": [],
            "openai_compatible_url": "http://127.0.0.1:1234/v1",
            "openai_api_key_env": "",
        }
        config = model_client.ModelProviderConfig.from_settings(settings)
        self.assertEqual(config.provider, "openai_compatible")
        self.assertEqual(config.base_url, "http://127.0.0.1:1234/v1")

    def test_ollama_uses_native_endpoint(self):
        response = Mock(status_code=200)
        with patch.object(model_client.requests, "post", return_value=response) as post:
            wrapped = model_client.post_chat(
                {"model_provider": "ollama", "ollama_url": "http://127.0.0.1:11434"},
                {"model": "local", "messages": []},
            )
        self.assertEqual(wrapped.provider, "ollama")
        self.assertEqual(post.call_args.args[0], "http://127.0.0.1:11434/api/chat")
        self.assertEqual(post.call_args.kwargs["json"]["options"]["num_ctx"], 8192)

    def test_ollama_context_window_is_configurable_and_bounded(self):
        response = Mock(status_code=200)
        with patch.object(model_client.requests, "post", return_value=response) as post:
            model_client.post_chat(
                {
                    "model_provider": "ollama",
                    "ollama_url": "http://127.0.0.1:11434",
                    "ollama_num_ctx": 16384,
                },
                {"model": "local", "messages": [], "options": {"temperature": 0}},
            )
        options = post.call_args.kwargs["json"]["options"]
        self.assertEqual(options["num_ctx"], 16384)
        self.assertEqual(options["temperature"], 0)

    def test_openai_compatible_maps_url_auth_and_options(self):
        response = Mock(status_code=200)
        with patch.dict(os.environ, {"TEST_MODEL_KEY": "secret"}), patch.object(
            model_client.requests, "post", return_value=response
        ) as post:
            wrapped = model_client.post_chat(
                {
                    "model_provider": "openai_compatible",
                    "openai_compatible_url": "http://127.0.0.1:1234/v1",
                    "openai_api_key_env": "TEST_MODEL_KEY",
                },
                {"model": "remote", "messages": [], "options": {"num_predict": 128}},
            )
        self.assertEqual(wrapped.provider, "openai_compatible")
        self.assertEqual(post.call_args.args[0], "http://127.0.0.1:1234/v1/chat/completions")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(post.call_args.kwargs["json"]["max_tokens"], 128)
        self.assertNotIn("options", post.call_args.kwargs["json"])

    def test_openai_payload_serializes_assistant_tool_arguments(self):
        payload = model_client._openai_payload(
            {
                "model": "remote",
                "messages": [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "github.get_issue",
                                    "arguments": {"repository": "owner/repo", "number": 7},
                                },
                            }
                        ],
                    }
                ],
            },
            True,
        )

        arguments = payload["messages"][0]["tool_calls"][0]["function"]["arguments"]
        self.assertEqual(json.loads(arguments), {"number": 7, "repository": "owner/repo"})

    def test_openai_stream_preserves_finish_reason_for_completeness_gate(self):
        response = Mock()
        response.status_code = 200
        response.iter_lines.return_value = [
            b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}',
            b'data: {"choices":[{"delta":{},"finish_reason":"length"}],"usage":{"prompt_tokens":10,"completion_tokens":20}}',
            b'data: [DONE]',
        ]
        wrapped = model_client.CompatibleChatResponse(response, "remote", "openai_compatible")
        chunks = [json.loads(item) for item in wrapped.iter_lines()]
        self.assertEqual(chunks[-1]["done_reason"], "length")

    def test_openai_sse_is_normalized_to_ollama_chunks(self):
        response = Mock(status_code=200)
        response.iter_lines.return_value = [
            b'data: {"choices":[{"delta":{"content":"hello"}}]}',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","function":{"name":"read_","arguments":"{\\"file_path\\":"}}]}}]}',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"file","arguments":"\\"a.txt\\"}"}}]}}]}',
            b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":4}}',
            b"data: [DONE]",
        ]
        chunks = [
            json.loads(line)
            for line in model_client.CompatibleChatResponse(response, "openai_compatible").iter_lines()
        ]
        self.assertEqual(chunks[0]["message"]["content"], "hello")
        self.assertEqual(chunks[-1]["message"]["tool_calls"][0]["function"]["name"], "read_file")
        self.assertEqual(chunks[-1]["message"]["tool_calls"][0]["function"]["arguments"], {"file_path": "a.txt"})
        self.assertEqual(chunks[-1]["prompt_eval_count"], 10)

    def test_model_listing_normalizes_openai_ids(self):
        response = Mock()
        response.json.return_value = {"data": [{"id": "model-a"}, {"id": "model-b"}]}
        with patch.object(model_client.requests, "get", return_value=response):
            models = model_client.list_models({
                "model_provider": "openai_compatible",
                "openai_compatible_url": "http://localhost:1234",
            })
        self.assertEqual([item["name"] for item in models], ["model-a", "model-b"])

    def test_namespaced_model_selects_provider_and_strips_prefix(self):
        response = Mock(status_code=200)
        settings = {
            "ollama_url": "http://127.0.0.1:11434",
            "model_providers": [{
                "id": "openrouter",
                "label": "OpenRouter",
                "base_url": "https://openrouter.ai/api/v1",
                "enabled": True,
                "input_cost_per_million": 1.25,
                "output_cost_per_million": 4.5,
                "currency": "USD",
            }],
        }
        with patch.object(model_client, "get_provider_secret", return_value="stored-key"), patch.object(
            model_client.requests, "post", return_value=response
        ) as post:
            wrapped = model_client.post_chat(
                settings,
                {"model": "openrouter::openai/gpt-test", "messages": []},
            )
        self.assertEqual(wrapped.provider, "openrouter")
        self.assertEqual(wrapped.protocol, "openai_compatible")
        self.assertEqual(post.call_args.args[0], "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(post.call_args.kwargs["json"]["model"], "openai/gpt-test")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer stored-key")

    def test_remote_models_do_not_use_the_local_ollama_slot(self):
        settings = {
            "ollama_url": "http://127.0.0.1:11434",
            "model_providers": [{
                "id": "nvidia",
                "base_url": "https://integrate.api.nvidia.com/v1",
                "enabled": True,
            }],
        }
        self.assertTrue(model_client.uses_local_model_slot(settings, "local-model"))
        with patch.object(model_client, "get_provider_secret", return_value="stored-key"):
            self.assertFalse(
                model_client.uses_local_model_slot(
                    settings,
                    "nvidia::meta/test-model",
                )
            )

    def test_subagent_payload_disables_thinking_only_for_local_ollama(self):
        settings = {
            "ollama_url": "http://127.0.0.1:11434",
            "model_providers": [{
                "id": "nvidia",
                "base_url": "https://integrate.api.nvidia.com/v1",
                "enabled": True,
                "model_kind": "chat",
            }],
        }
        payload = {"messages": [], "options": {"num_predict": 96}}
        local = model_client.subagent_chat_payload(settings, "local-model", payload)
        with patch.object(model_client, "get_provider_secret", return_value="stored-key"):
            remote = model_client.subagent_chat_payload(
                settings,
                "nvidia::meta/test-model",
                payload,
            )
        self.assertIs(local["think"], False)
        self.assertNotIn("think", remote)
        self.assertNotIn("think", payload)

    def test_imported_provider_does_not_receive_unverified_tool_fields(self):
        response = Mock(status_code=200)
        settings = {
            "model_providers": [{
                "id": "nvidia",
                "base_url": "https://integrate.api.nvidia.com/v1",
                "enabled": True,
                "model_kind": "chat",
            }],
        }
        payload = {
            "model": "nvidia::vendor/model",
            "messages": [],
            "tools": [{"type": "function", "function": {"name": "read_file"}}],
            "tool_choice": "auto",
        }
        with patch.object(model_client, "get_provider_secret", return_value="stored-key"), patch.object(
            model_client.requests, "post", return_value=response
        ) as post:
            model_client.post_chat(settings, payload)
        request_json = post.call_args.kwargs["json"]
        self.assertNotIn("tools", request_json)
        self.assertNotIn("tool_choice", request_json)
        self.assertIn("tools", payload)

    def test_legacy_openai_compatible_settings_keep_existing_tool_behavior(self):
        response = Mock(status_code=200)
        tools = [{"type": "function", "function": {"name": "read_file"}}]
        settings = {
            "model_provider": "openai_compatible",
            "model_providers": [],
            "openai_compatible_url": "http://127.0.0.1:1234/v1",
            "openai_api_key_env": "",
        }
        with patch.object(model_client.requests, "post", return_value=response) as post:
            model_client.post_chat(
                settings,
                {"model": "legacy-model", "messages": [], "tools": tools},
            )
        self.assertEqual(post.call_args.kwargs["json"]["tools"], tools)

    def test_imported_provider_requires_current_passed_attestation_for_tools(self):
        response = Mock(status_code=200)
        model_name = "vendor/model"
        endpoint = "https://provider.example/v1"
        declared_profile = model_client.model_capability_profile(
            model_name,
            model_kind="chat",
            supports_tools=True,
        )
        fingerprint = model_client.capability_fingerprint(
            model_name,
            endpoint,
            declared_profile,
        )
        provider = {
            "id": "remote",
            "base_url": endpoint,
            "enabled": True,
            "supports_tools": True,
            "model_kind": "chat",
        }
        tools = [{"type": "function", "function": {"name": "read_file"}}]

        def request(settings):
            with patch.object(
                model_client,
                "get_provider_secret",
                return_value="stored-key",
            ), patch.object(
                model_client.requests,
                "post",
                return_value=response,
            ) as post:
                model_client.post_chat(
                    settings,
                    {
                        "model": f"remote::{model_name}",
                        "messages": [],
                        "tools": tools,
                        "tool_choice": "auto",
                    },
                )
            return post.call_args.kwargs["json"]

        unattested = request({"model_providers": [provider]})
        self.assertNotIn("tools", unattested)
        self.assertNotIn("tool_choice", unattested)

        failed = request({
            "model_providers": [{
                **provider,
                "tool_attestation": {
                    "profile_fingerprint": fingerprint,
                    "verified_at": "2026-07-30T00:00:00Z",
                    "method": "synthetic_tool_call",
                    "passed": False,
                },
            }],
        })
        self.assertNotIn("tools", failed)

        attested = request({
            "model_providers": [{
                **provider,
                "tool_attestation": {
                    "profile_fingerprint": fingerprint,
                    "verified_at": "2026-07-30T00:00:00Z",
                    "method": "synthetic_tool_call",
                    "passed": True,
                },
            }],
        })
        self.assertEqual(attested["tools"], tools)
        self.assertEqual(attested["tool_choice"], "auto")

    def test_remote_404_is_classified_without_echoing_upstream_account_data(self):
        settings = {
            "model_providers": [{
                "id": "nvidia",
                "base_url": "https://integrate.api.nvidia.com/v1",
                "enabled": True,
            }],
        }
        upstream = "Function abc: Not found for account private-account-id"
        with patch.object(model_client, "get_provider_secret", return_value="stored-key"):
            failure = model_client.model_call_error(
                settings,
                "nvidia::meta/test-model",
                404,
                upstream,
            )
        self.assertEqual(failure["code"], "PROVIDER_MODEL_UNAVAILABLE")
        self.assertNotIn("private-account-id", json.dumps(failure))

    def test_all_model_inventory_merges_ollama_and_enabled_providers(self):
        settings = {
            "ollama_url": "http://127.0.0.1:11434",
            "model_providers": [{
                "id": "openrouter",
                "label": "OpenRouter",
                "base_url": "https://openrouter.ai/api/v1",
                "enabled": True,
                "model_kind": "chat",
                "selected_model": "remote/model",
            }],
        }
        local_response = Mock()
        local_response.json.return_value = {"models": [{"name": "local-model"}]}
        local_response.raise_for_status.return_value = None
        with patch.object(model_client, "get_provider_secret", return_value="stored-key"), patch.object(
            model_client.requests,
            "get",
            return_value=local_response,
        ) as get:
            models = model_client.list_all_models(settings)
        self.assertEqual(
            [item["name"] for item in models],
            ["local-model", "openrouter::remote/model"],
        )
        self.assertEqual(get.call_count, 1)

    def test_unscoped_configured_provider_does_not_publish_its_full_catalog(self):
        settings = {
            "ollama_url": "http://127.0.0.1:11434",
            "model_providers": [{
                "id": "remote",
                "base_url": "https://provider.example/v1",
                "enabled": True,
            }],
        }
        local_response = Mock()
        local_response.json.return_value = {"models": []}
        local_response.raise_for_status.return_value = None
        with patch.object(model_client, "get_provider_secret", return_value="stored-key"), patch.object(
            model_client.requests,
            "get",
            return_value=local_response,
        ) as get:
            models = model_client.list_all_models(settings)
        self.assertEqual(models, [])
        self.assertEqual(get.call_count, 1)

    def test_riva_translation_is_rejected_by_chat_and_uses_specialized_payload(self):
        response = Mock(status_code=200)
        settings = {
            "model_providers": [{
                "id": "nvidia",
                "base_url": "https://integrate.api.nvidia.com/v1",
                "enabled": True,
                "selected_model": "nvidia/riva-translate-4b-instruct-v2",
                "model_kind": "translation",
                "language_pair": "en-zh-tw",
            }],
        }
        payload = {
            "model": "nvidia::nvidia/riva-translate-4b-instruct-v2",
            "messages": [
                {"role": "system", "content": "You are an Agent with tools."},
                {"role": "user", "content": "old history"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "Translate only this sentence."},
            ],
            "tools": [{"type": "function", "function": {"name": "read_file"}}],
            "tool_choice": "auto",
        }
        with patch.object(
            model_client, "get_provider_secret", return_value="stored-key"
        ), patch.object(model_client.requests, "post", return_value=response) as post:
            with self.assertRaisesRegex(ValueError, "chat.*request path"):
                model_client.post_chat(settings, payload)
            post.assert_not_called()
            model_client.post_specialized_completion(
                settings,
                payload,
                model_kind="translation",
                stream=False,
            )
        request_json = post.call_args.kwargs["json"]
        self.assertEqual(request_json["messages"], [
            {"role": "system", "content": "en-zh-tw"},
            {"role": "user", "content": "Translate only this sentence."},
        ])
        self.assertNotIn("tools", request_json)
        self.assertNotIn("tool_choice", request_json)
        self.assertFalse(request_json["stream"])

    def test_translation_inventory_is_specialized_and_never_subagent_eligible(self):
        settings = {
            "ollama_url": "http://127.0.0.1:11434",
            "model_providers": [{
                "id": "nvidia",
                "label": "NVIDIA",
                "base_url": "https://integrate.api.nvidia.com/v1",
                "enabled": True,
                "selected_model": "nvidia/riva-translate-4b-instruct-v2",
                "model_kind": "translation",
                "language_pair": "en-zh-tw",
            }],
        }
        local_response = Mock()
        local_response.json.return_value = {"models": [{"name": "local-model"}]}
        local_response.raise_for_status.return_value = None
        with patch.object(
            model_client, "get_provider_secret", return_value="stored-key"
        ), patch.object(model_client.requests, "get", return_value=local_response):
            chat = model_client.list_all_models(settings)
            specialized = model_client.list_specialized_models(settings)
            with self.assertRaisesRegex(ValueError, "not eligible for Subagent"):
                model_client.subagent_chat_payload(
                    settings,
                    "nvidia::nvidia/riva-translate-4b-instruct-v2",
                    {"messages": []},
                )
        self.assertEqual([item["name"] for item in chat], ["local-model"])
        self.assertEqual(
            [item["name"] for item in specialized],
            ["nvidia::nvidia/riva-translate-4b-instruct-v2"],
        )
        self.assertEqual(specialized[0]["kind"], "translation")
        self.assertFalse(specialized[0]["profile"]["eligible_for_primary"])
        self.assertFalse(specialized[0]["profile"]["eligible_for_subagent"])

    def test_unknown_imported_model_fails_closed_before_network(self):
        settings = {
            "model_providers": [{
                "id": "remote",
                "base_url": "https://provider.example/v1",
                "enabled": True,
                "selected_model": "vendor/opaque-model",
            }],
        }
        with patch.object(
            model_client, "get_provider_secret", return_value="stored-key"
        ), patch.object(model_client.requests, "post") as post:
            with self.assertRaisesRegex(ValueError, "unknown"):
                model_client.post_chat(
                    settings,
                    {
                        "model": "remote::vendor/opaque-model",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )
        post.assert_not_called()

    def test_provider_400_keeps_safe_reason_and_redacts_identifiers(self):
        settings = {
            "model_providers": [{
                "id": "nvidia",
                "base_url": "https://integrate.api.nvidia.com/v1",
                "enabled": True,
                "selected_model": "nvidia/chat-instruct",
                "model_kind": "chat",
            }],
        }
        upstream = json.dumps({
            "error": {
                "message": (
                    "Invalid message shape for account private-account-id; "
                    "key nvapi-ABCDEFGHIJKLMNOP; admin@example.com; "
                    "123e4567-e89b-42d3-a456-426614174000 <b>bad</b> "
                    "&lt;script&gt;alert(1)&lt;/script&gt;"
                ),
            },
        })
        with patch.object(
            model_client, "get_provider_secret", return_value="nvapi-ABCDEFGHIJKLMNOP"
        ):
            failure = model_client.model_call_error(
                settings,
                "nvidia::nvidia/chat-instruct",
                400,
                upstream,
            )
        detail = failure["detail"]
        self.assertIn("Invalid message shape", failure["message"])
        self.assertIn("Invalid message shape", detail)
        self.assertNotIn("private-account-id", detail)
        self.assertNotIn("nvapi-", detail)
        self.assertNotIn("admin@example.com", detail)
        self.assertNotIn("123e4567", detail)
        self.assertNotIn("<b>", detail)
        self.assertNotIn("<script>", failure["message"])
        self.assertNotIn("alert(1)", failure["message"])
        self.assertNotIn("<script>", detail)

    def test_provider_and_extension_gates_are_both_required(self):
        def settings(enabled):
            return {
                "model_providers": [{
                    "id": "remote",
                    "base_url": "https://provider.example/v1",
                    "enabled": enabled,
                    "selected_model": "vendor/chat-instruct",
                    "model_kind": "chat",
                }],
            }

        with patch.object(
            model_client,
            "get_provider_secret",
            return_value="stored-key",
        ):
            for provider_enabled, extension_enabled in (
                (False, False),
                (False, True),
                (True, False),
            ):
                with self.subTest(
                    provider_enabled=provider_enabled,
                    extension_enabled=extension_enabled,
                ):
                    model_client.configure_provider_extension_gate(
                        lambda _extension_id, _project_id=None,
                        allowed=extension_enabled: allowed
                    )
                    with self.assertRaisesRegex(PermissionError, "disabled"):
                        model_client.provider_for_model(
                            settings(provider_enabled),
                            "remote::vendor/chat-instruct",
                        )

            model_client.configure_provider_extension_gate(
                lambda _extension_id, _project_id=None: True
            )
            config = model_client.provider_for_model(
                settings(True),
                "remote::vendor/chat-instruct",
            )
        self.assertEqual(config.provider, "remote")

    def test_missing_provider_enable_bit_fails_closed(self):
        settings = {
            "model_providers": [{
                "id": "remote",
                "base_url": "https://provider.example/v1",
                "selected_model": "vendor/chat-instruct",
                "model_kind": "chat",
            }],
        }
        model_client.configure_provider_extension_gate(
            lambda _extension_id, _project_id=None: True
        )
        with self.assertRaisesRegex(PermissionError, "disabled"):
            model_client.provider_for_model(
                settings,
                "remote::vendor/chat-instruct",
            )

    def test_production_gate_disables_implicit_legacy_tool_support(self):
        settings = {
            "model_provider": "openai_compatible",
            "model_providers": [],
            "openai_compatible_url": "https://provider.example/v1",
            "openai_api_key_env": "",
        }
        model_client.configure_provider_extension_gate(
            lambda extension_id, project_id=None: (
                extension_id == "provider.openai_compatible"
            )
        )
        self.assertFalse(
            model_client.model_supports_tools(settings, "legacy-model")
        )

        model_client.configure_provider_extension_gate(None)
        self.assertTrue(
            model_client.model_supports_tools(settings, "legacy-model")
        )


if __name__ == "__main__":
    unittest.main()
