"""AES-256-GCM envelopes for private n8n Gmail content."""

from __future__ import annotations

import base64
import os
from typing import Callable, Protocol, Union


class GmailCryptoError(ValueError):
    """Raised when encryption is unavailable or authentication fails."""


class GmailKeyProvider(Protocol):
    def __call__(self) -> bytes: ...


KeyProvider = Union[GmailKeyProvider, Callable[[], bytes]]


def _aesgcm_type():
    # Import lazily so Workbench can still start and show the disabled profile.
    # Enabling the integration fails closed until the locked dependency exists.
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - exercised in dependency-free installs
        raise GmailCryptoError(
            "AES-GCM support is unavailable; install the locked cryptography dependency."
        ) from exc
    return AESGCM


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
        # Reject alternate spellings that only change unused trailing Base64
        # bits.  AES-GCM authenticates bytes, so accepting a non-canonical
        # spelling would otherwise let a visibly modified envelope decode to
        # the original authenticated ciphertext.
        if _b64encode(decoded) != value:
            raise ValueError("non-canonical base64url")
        return decoded
    except Exception as exc:
        raise GmailCryptoError("Invalid encrypted Gmail envelope.") from exc


class AesGcmContentCipher:
    """Versioned, random-nonce AES-256-GCM content encryption."""

    VERSION = "ag1"

    def __init__(self, key_provider: KeyProvider) -> None:
        self._key_provider = key_provider

    @property
    def available(self) -> bool:
        try:
            _aesgcm_type()
            self._key()
            return True
        except GmailCryptoError:
            return False

    def assert_available(self) -> None:
        _aesgcm_type()
        self._key()

    def _key(self) -> bytes:
        try:
            key = bytes(self._key_provider())
        except Exception as exc:
            raise GmailCryptoError("Gmail encryption key is unavailable.") from exc
        if len(key) != 32:
            raise GmailCryptoError("Gmail encryption key must contain exactly 32 bytes.")
        return key

    def encrypt_text(self, value: str, *, aad: str) -> str:
        if not isinstance(value, str) or not isinstance(aad, str) or not aad:
            raise GmailCryptoError("Gmail encryption requires text and non-empty AAD.")
        nonce = os.urandom(12)
        ciphertext_and_tag = _aesgcm_type()(self._key()).encrypt(
            nonce, value.encode("utf-8"), aad.encode("utf-8")
        )
        return ".".join((self.VERSION, _b64encode(nonce), _b64encode(ciphertext_and_tag)))

    def decrypt_text(self, envelope: str, *, aad: str) -> str:
        if not isinstance(envelope, str) or not isinstance(aad, str) or not aad:
            raise GmailCryptoError("Gmail decryption requires an envelope and non-empty AAD.")
        parts = envelope.split(".")
        if len(parts) != 3 or parts[0] != self.VERSION:
            raise GmailCryptoError("Unsupported encrypted Gmail envelope.")
        nonce, ciphertext_and_tag = map(_b64decode, parts[1:])
        if len(nonce) != 12 or len(ciphertext_and_tag) < 16:
            raise GmailCryptoError("Invalid encrypted Gmail envelope.")
        try:
            plaintext = _aesgcm_type()(self._key()).decrypt(
                nonce, ciphertext_and_tag, aad.encode("utf-8")
            )
            return plaintext.decode("utf-8")
        except GmailCryptoError:
            raise
        except Exception as exc:
            raise GmailCryptoError("Encrypted Gmail content failed authentication.") from exc
