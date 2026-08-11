from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import requests


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from hermes import (  # noqa: E402
    HermesAuthenticationError,
    HermesConfig,
    HermesConfigurationError,
    HermesDisabledError,
    HermesProtocolError,
    HermesSidecarClient,
    HermesUnavailableError,
    iter_sse_events,
    validate_loopback_base_url,
)


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, lines=None):
        self.status_code = status
        self.content = json.dumps(body if body is not None else {}).encode("utf-8")
        self.headers = dict(headers or {"Content-Type": "application/json"})
        self._lines = list(lines or [])
        self.closed = False

    def close(self):
        self.closed = True

    def iter_lines(self, decode_unicode=False):
        del decode_unicode
        return iter(self._lines)

    def iter_content(self, chunk_size=1):
        for index in range(0, len(self.content), chunk_size):
            yield self.content[index : index + chunk_size]


class FakeSession:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []
        self.trust_env = True

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.error:
            raise self.error
        return self.responses.pop(0)


def enabled_config(**changes):
    values = {
        "enabled": True,
        "base_url": "http://127.0.0.1:8642",
        "api_key": "0123456789abcdef0123456789abcdef",
    }
    values.update(changes)
    return HermesConfig(**values)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:8642",
        "http://10.0.0.2:8642",
        "ftp://127.0.0.1:8642",
        "http://test-user@127.0.0.1:8642",
        "http://127.0.0.1:8642/api",
        "http://127.0.0.1:8642?target=remote",
    ],
)
def test_config_rejects_non_isolated_or_credentialed_urls(url):
    with pytest.raises(HermesConfigurationError):
        validate_loopback_base_url(url)


def test_config_is_disabled_by_default_and_reads_key_only_from_environment():
    disabled = HermesConfig.from_mapping({}, environ={})
    assert disabled.enabled is False
    with pytest.raises(HermesDisabledError):
        disabled.require_enabled()

    config = HermesConfig.from_mapping(
        {
            "hermes_enabled": True,
            "hermes_base_url": "http://[::1]:8642",
            "hermes_api_key_env": "TEST_HERMES_KEY",
        },
        environ={"TEST_HERMES_KEY": "a-secure-test-key-123456"},
    )
    assert config.enabled is True
    assert config.api_key == "a-secure-test-key-123456"


def test_enabled_config_requires_a_nontrivial_bearer_key():
    with pytest.raises(HermesConfigurationError):
        HermesConfig.from_mapping(
            {"hermes_enabled": True, "hermes_api_key_env": "MISSING"},
            environ={},
        )


def test_client_health_and_capabilities_use_auth_without_proxy_or_redirects():
    fake = FakeSession(
        [
            FakeResponse(body={"status": "ok"}),
            FakeResponse(body={"runs": True}),
        ]
    )
    client = HermesSidecarClient(enabled_config(), session=fake)

    assert fake.trust_env is False
    assert client.health() == {"status": "ok"}
    assert client.capabilities() == {"runs": True}
    assert [call[1] for call in fake.calls] == [
        "http://127.0.0.1:8642/health",
        "http://127.0.0.1:8642/v1/capabilities",
    ]
    for _method, _url, kwargs in fake.calls:
        assert kwargs["headers"]["Authorization"].startswith("Bearer ")
        assert kwargs["allow_redirects"] is False


def test_client_never_follows_redirects_or_exposes_upstream_error_body():
    response = FakeResponse(
        status=302,
        body={"secret": "upstream-secret-body"},
        headers={"Location": "http://example.com/steal"},
    )
    client = HermesSidecarClient(enabled_config(), session=FakeSession([response]))
    with pytest.raises(HermesProtocolError) as caught:
        client.health()
    assert "upstream-secret-body" not in str(caught.value)
    assert response.closed is True


def test_client_bounds_decoded_json_body_and_rejects_bad_length_header():
    oversized = FakeResponse(body={"value": "x" * 70_000})
    client = HermesSidecarClient(
        enabled_config(max_response_bytes=65_536), session=FakeSession([oversized])
    )
    with pytest.raises(HermesProtocolError):
        client.health()
    assert oversized.closed is True

    malformed = FakeResponse(body={}, headers={"Content-Length": "not-a-number"})
    client = HermesSidecarClient(enabled_config(), session=FakeSession([malformed]))
    with pytest.raises(HermesProtocolError):
        client.health()


def test_client_classifies_authentication_and_network_failures():
    auth = HermesSidecarClient(
        enabled_config(), session=FakeSession([FakeResponse(status=401)])
    )
    with pytest.raises(HermesAuthenticationError):
        auth.health()

    offline = HermesSidecarClient(
        enabled_config(), session=FakeSession(error=requests.ConnectionError("secret"))
    )
    with pytest.raises(HermesUnavailableError) as caught:
        offline.health()
    assert "secret" not in str(caught.value)
    assert caught.value.retryable is True


def test_sse_parser_handles_multiline_data_comments_ids_and_done():
    events = list(
        iter_sse_events(
            [
                b": keepalive",
                b"id: evt-1",
                b"event: delta",
                b'data: {"text":',
                b'data: "hello"}',
                b"",
                b"data: [DONE]",
                b"",
            ]
        )
    )
    assert events[0].event == "delta"
    assert events[0].event_id == "evt-1"
    assert events[0].json() == {"text": "hello"}
    assert events[1].json() is None


def test_sse_parser_rejects_oversized_and_invalid_utf8_events():
    with pytest.raises(HermesProtocolError):
        list(iter_sse_events(["data: too-large", ""], max_event_bytes=4))
    with pytest.raises(HermesProtocolError):
        list(iter_sse_events([b"data: \xff", b""]))


def test_open_sse_closes_response_even_when_consumer_stops_early():
    response = FakeResponse(
        headers={"Content-Type": "text/event-stream"},
        lines=[b"data: one", b"", b"data: two", b""],
    )
    client = HermesSidecarClient(enabled_config(), session=FakeSession([response]))
    with client.open_sse("/v1/runs/run/events") as stream:
        assert next(stream).data == "one"
    assert response.closed is True
