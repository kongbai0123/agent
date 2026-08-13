from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from n8n_gmail_crypto import AesGcmContentCipher, GmailCryptoError


def test_aes_gcm_round_trip_uses_random_nonce_and_aad():
    cipher = AesGcmContentCipher(lambda: b"k" * 32)
    first = cipher.encrypt_text("sensitive 郵件", aad="draft:one")
    second = cipher.encrypt_text("sensitive 郵件", aad="draft:one")

    assert first != second
    assert "sensitive" not in first
    assert cipher.decrypt_text(first, aad="draft:one") == "sensitive 郵件"
    with pytest.raises(GmailCryptoError):
        cipher.decrypt_text(first, aad="draft:two")


def test_aes_gcm_rejects_wrong_key_and_tampering():
    cipher = AesGcmContentCipher(lambda: b"a" * 32)
    envelope = cipher.encrypt_text("private", aad="field:1")

    with pytest.raises(GmailCryptoError):
        AesGcmContentCipher(lambda: b"b" * 32).decrypt_text(envelope, aad="field:1")
    with pytest.raises(GmailCryptoError):
        cipher.decrypt_text(envelope[:-1] + ("A" if envelope[-1] != "A" else "B"), aad="field:1")


def test_aes_gcm_requires_256_bit_key():
    cipher = AesGcmContentCipher(lambda: b"short")
    assert cipher.available is False
    with pytest.raises(GmailCryptoError):
        cipher.encrypt_text("private", aad="field:1")
