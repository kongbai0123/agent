from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from n8n_gmail_secrets import N8nGmailSecretStore


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI is required")


def test_dpapi_store_creates_three_distinct_stable_keys_without_plaintext(tmp_path):
    path = tmp_path / "private" / "n8n-gmail.json"
    store = N8nGmailSecretStore(path)

    content = store.content_key()
    inbound = store.inbound_hmac_key()
    outbound = store.outbound_webhook_key()

    assert {len(content), len(inbound), len(outbound)} == {32}
    assert len({content, inbound, outbound}) == 3
    assert store.content_key() == content
    assert store.inbound_hmac_key() == inbound
    credential_value = store.inbound_hmac_credential_value()
    assert credential_value.encode("ascii") == store.inbound_hmac_verifier_key()
    assert len(credential_value) == 44
    assert credential_value.endswith("=")
    assert store.outbound_webhook_key() == outbound
    serialized = path.read_bytes()
    for secret in (content, inbound, outbound):
        assert secret not in serialized
        assert secret.hex().encode("ascii") not in serialized
    assert credential_value.encode("ascii") not in serialized
    assert store.status() == {
        "available": True,
        "provider": "windows_dpapi",
        "key_count": 3,
    }


def test_unknown_or_corrupt_store_fails_closed(tmp_path):
    path = tmp_path / "n8n-gmail.json"
    path.write_text('{"version": 99, "keys": {}}', encoding="utf-8")
    store = N8nGmailSecretStore(path)
    assert store.status()["available"] is False
    with pytest.raises(Exception):
        store.content_key()
