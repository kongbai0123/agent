"""Typed, redaction-safe failures for the optional Hermes sidecar."""

from __future__ import annotations


class HermesError(RuntimeError):
    """Base error whose message is safe to expose to Workbench callers."""

    code = "HERMES_ERROR"
    retryable = False

    def __init__(self, message: str = "Hermes request failed.") -> None:
        super().__init__(message)


class HermesDisabledError(HermesError):
    code = "HERMES_DISABLED"
    retryable = True


class HermesConfigurationError(HermesError):
    code = "HERMES_CONFIGURATION_ERROR"


class HermesAuthenticationError(HermesError):
    code = "HERMES_AUTHENTICATION_FAILED"


class HermesUnavailableError(HermesError):
    code = "HERMES_UNAVAILABLE"
    retryable = True


class HermesProtocolError(HermesError):
    code = "HERMES_PROTOCOL_ERROR"


class HermesConflictError(HermesError):
    code = "HERMES_MAPPING_CONFLICT"
