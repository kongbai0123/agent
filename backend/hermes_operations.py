"""Fail-closed rollout and operational controls for a Hermes sidecar.

The objects in this module validate configuration and make deterministic
routing decisions.  They do not start a process, open a socket, perform a
health request, or invoke Hermes.  Integration code owns those effects and
reports their results back to :class:`HermesOperationsController`.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.parse import urlsplit

try:  # Support both Workbench's flat backend path and package-style tests.
    from hermes_approvals import ApprovalError, normalize_capability
except ModuleNotFoundError:  # pragma: no cover - exercised by package importers
    from backend.hermes_approvals import ApprovalError, normalize_capability

try:
    from hermes_metrics import (
        HermesMetricsStore,
        InMemoryHermesMetricsStore,
        METRIC_KINDS,
        unavailable_metrics_snapshot,
    )
except ModuleNotFoundError:  # pragma: no cover - exercised by package importers
    from backend.hermes_metrics import (
        HermesMetricsStore,
        InMemoryHermesMetricsStore,
        METRIC_KINDS,
        unavailable_metrics_snapshot,
    )


_PINNED_RELEASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-fA-F]{64}$")
_SECRET_REF_RE = re.compile(
    r"^(?:env|secret|keyring)://[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$"
)
_GATEWAY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REASON_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_MUTABLE_RELEASES = {"latest", "main", "master", "head", "dev", "nightly", "stable"}
_MAX_OBSERVABILITY_COUNTER = 9_000_000_000_000_000
_MAX_REASON_BUCKETS = 64


class OperationsConfigError(ValueError):
    """Raised when Hermes production controls are not safely configured."""


class RolloutMode(str, Enum):
    DISABLED = "disabled"
    CANARY = "canary"
    PERCENTAGE = "percentage"
    ALL = "all"


class SidecarTransport(str, Enum):
    HTTP = "http"
    GATEWAY = "gateway"


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class HealthStatus(str, Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    STALE = "stale"


class RoutingTarget(str, Enum):
    HERMES = "hermes"
    BASIC_CHAT = "basic_chat"


def _enum_value(enum_type: type[Enum], value: Any, field: str) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        choices = ", ".join(item.value for item in enum_type)
        raise OperationsConfigError(f"{field} must be one of: {choices}") from exc


def _number(value: Any, field: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OperationsConfigError(f"{field} must be a number")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise OperationsConfigError(
            f"{field} must be between {minimum:g} and {maximum:g}"
        )
    return result


def _clock_value(clock: Callable[[], float], field: str) -> float:
    try:
        result = float(clock())
    except (TypeError, ValueError) as exc:
        raise OperationsConfigError(f"{field} returned an invalid timestamp") from exc
    if not math.isfinite(result) or result < 0:
        raise OperationsConfigError(f"{field} returned an invalid timestamp")
    return result


def _positive_int(value: Any, field: str, *, maximum: int = 1_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise OperationsConfigError(
            f"{field} must be an integer between 1 and {maximum}"
        )
    return value


def _required_text(value: Any, field: str, *, maximum: int = 512) -> str:
    text = str(value or "").strip()
    if not text:
        raise OperationsConfigError(f"{field} is required")
    if len(text) > maximum:
        raise OperationsConfigError(f"{field} is too long")
    return text


def _reason_code(value: Any, field: str = "reason") -> str:
    text = str(value or "").strip().lower()
    if not _REASON_RE.fullmatch(text):
        raise OperationsConfigError(
            f"{field} must be a lowercase machine-readable reason code"
        )
    return text


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise OperationsConfigError(f"unknown {label} fields: {sorted(unknown)}")


def _increment_counter(
    counter: Counter[str],
    key: str,
    *,
    maximum_keys: int = _MAX_REASON_BUCKETS,
) -> None:
    bucket = key if key in counter or len(counter) < maximum_keys else "other"
    counter[bucket] = min(
        _MAX_OBSERVABILITY_COUNTER,
        counter[bucket] + 1,
    )


@dataclass(frozen=True)
class HermesSidecarManifest:
    """Immutable sidecar identity plus a local-only transport description."""

    schema_version: int
    release: str
    source_commit: str
    artifact_digest: str
    transport: SidecarTransport
    endpoint: str
    expected_capabilities: tuple[str, ...]
    max_concurrency: int = 1
    api_key_ref: Optional[str] = None

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or not isinstance(
            self.schema_version, int
        ) or self.schema_version != 1:
            raise OperationsConfigError("manifest schema_version must be 1")

        if not isinstance(self.release, str):
            raise OperationsConfigError("release must be a string")
        release = _required_text(self.release, "release", maximum=128)
        if (
            not _PINNED_RELEASE_RE.fullmatch(release)
            or release.casefold() in _MUTABLE_RELEASES
            or "*" in release
        ):
            raise OperationsConfigError("release must be an immutable, pinned identifier")
        object.__setattr__(self, "release", release)

        if not isinstance(self.source_commit, str):
            raise OperationsConfigError("source_commit must be a string")
        commit = _required_text(self.source_commit, "source_commit", maximum=40)
        if not _COMMIT_RE.fullmatch(commit):
            raise OperationsConfigError("source_commit must be a full 40-character git SHA")
        object.__setattr__(self, "source_commit", commit.lower())

        if not isinstance(self.artifact_digest, str):
            raise OperationsConfigError("artifact_digest must be a string")
        digest = _required_text(self.artifact_digest, "artifact_digest", maximum=71)
        if not _DIGEST_RE.fullmatch(digest):
            raise OperationsConfigError(
                "artifact_digest must be a complete sha256:<64 hex> digest"
            )
        object.__setattr__(self, "artifact_digest", digest.lower())

        transport = _enum_value(SidecarTransport, self.transport, "transport")
        object.__setattr__(self, "transport", transport)
        if not isinstance(self.endpoint, str):
            raise OperationsConfigError("endpoint must be a string")
        endpoint = _required_text(self.endpoint, "endpoint", maximum=2048)
        self._validate_endpoint(transport, endpoint)
        object.__setattr__(self, "endpoint", endpoint)

        raw_capabilities: Iterable[Any]
        if not isinstance(self.expected_capabilities, (list, tuple)):
            raise OperationsConfigError("expected_capabilities must be a list")
        raw_capabilities = self.expected_capabilities
        try:
            if not all(isinstance(capability, str) for capability in raw_capabilities):
                raise OperationsConfigError(
                    "expected_capabilities entries must be strings"
                )
            capabilities = tuple(
                normalize_capability(capability) for capability in raw_capabilities
            )
        except (TypeError, ApprovalError) as exc:
            raise OperationsConfigError(str(exc)) from exc
        if not capabilities:
            raise OperationsConfigError("expected_capabilities cannot be empty")
        if len(set(capabilities)) != len(capabilities):
            raise OperationsConfigError("expected_capabilities cannot contain duplicates")
        object.__setattr__(self, "expected_capabilities", capabilities)
        object.__setattr__(
            self,
            "max_concurrency",
            _positive_int(self.max_concurrency, "max_concurrency", maximum=10_000),
        )

        if transport is SidecarTransport.HTTP and self.api_key_ref is None:
            raise OperationsConfigError(
                "HTTP sidecar manifest requires an api_key_ref secret reference"
            )
        if self.api_key_ref is not None:
            if not isinstance(self.api_key_ref, str):
                raise OperationsConfigError("api_key_ref must be a string reference")
            reference = _required_text(self.api_key_ref, "api_key_ref", maximum=300)
            if not _SECRET_REF_RE.fullmatch(reference):
                raise OperationsConfigError(
                    "api_key_ref must be an env://, secret://, or keyring:// reference; "
                    "raw credentials are not accepted"
                )
            object.__setattr__(self, "api_key_ref", reference)

    @staticmethod
    def _validate_endpoint(transport: SidecarTransport, endpoint: str) -> None:
        parsed = urlsplit(endpoint)
        if transport is SidecarTransport.HTTP:
            if parsed.scheme not in {"http", "https"}:
                raise OperationsConfigError(
                    "HTTP sidecars must use an http:// or https:// endpoint"
                )
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise OperationsConfigError("HTTP sidecar endpoint must be loopback-only")
            try:
                port = parsed.port
            except ValueError as exc:
                raise OperationsConfigError("HTTP sidecar endpoint has an invalid port") from exc
            if port is None:
                raise OperationsConfigError("HTTP sidecar endpoint must include an explicit port")
            if (
                parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
            ):
                raise OperationsConfigError(
                    "HTTP sidecar endpoint cannot contain credentials, path, query, or fragment"
                )
            return

        if (
            parsed.scheme != "gateway"
            or not parsed.netloc
            or not _GATEWAY_NAME_RE.fullmatch(parsed.netloc)
        ):
            raise OperationsConfigError(
                "gateway sidecar endpoint must use gateway://<local-name>"
            )
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise OperationsConfigError("gateway endpoint must contain only a local name")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "HermesSidecarManifest":
        if not isinstance(value, Mapping):
            raise OperationsConfigError("manifest must be an object")
        fields = {
            "schema_version",
            "release",
            "source_commit",
            "artifact_digest",
            "transport",
            "endpoint",
            "expected_capabilities",
            "max_concurrency",
            "api_key_ref",
        }
        _reject_unknown(value, fields, "manifest")
        required = fields - {"max_concurrency", "api_key_ref"}
        missing = required - set(value)
        if missing:
            raise OperationsConfigError(f"missing manifest fields: {sorted(missing)}")
        return cls(
            schema_version=value["schema_version"],
            release=value["release"],
            source_commit=value["source_commit"],
            artifact_digest=value["artifact_digest"],
            transport=value["transport"],
            endpoint=value["endpoint"],
            expected_capabilities=value["expected_capabilities"],
            max_concurrency=value.get("max_concurrency", 1),
            api_key_ref=value.get("api_key_ref"),
        )

    @property
    def manifest_id(self) -> str:
        canonical = json.dumps(
            {
                "schema_version": self.schema_version,
                "release": self.release,
                "source_commit": self.source_commit,
                "artifact_digest": self.artifact_digest,
                "transport": self.transport.value,
                "endpoint": self.endpoint,
                "expected_capabilities": self.expected_capabilities,
                "max_concurrency": self.max_concurrency,
                "api_key_ref": self.api_key_ref,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def public_dict(self) -> dict[str, Any]:
        """Return an observability-safe view; never expose the secret reference."""
        return {
            "schema_version": self.schema_version,
            "manifest_id": self.manifest_id,
            "release": self.release,
            "source_commit": self.source_commit,
            "artifact_digest": self.artifact_digest,
            "transport": self.transport.value,
            "endpoint": self.endpoint,
            "expected_capabilities": list(self.expected_capabilities),
            "max_concurrency": self.max_concurrency,
            "api_key_ref_configured": self.api_key_ref is not None,
        }


@dataclass(frozen=True)
class RolloutConfig:
    mode: RolloutMode = RolloutMode.DISABLED
    percentage: float = 0.0
    canary_subjects: frozenset[str] = frozenset()
    selection_salt: str = "hermes-rollout-v1"

    def __post_init__(self) -> None:
        mode = _enum_value(RolloutMode, self.mode, "rollout.mode")
        object.__setattr__(self, "mode", mode)
        percentage = _number(
            self.percentage, "rollout.percentage", minimum=0.0, maximum=100.0
        )
        object.__setattr__(self, "percentage", percentage)

        if isinstance(self.canary_subjects, (str, bytes, Mapping)):
            raise OperationsConfigError("rollout.canary_subjects must be a list")
        try:
            subjects = frozenset(
                _required_text(subject, "canary subject", maximum=512)
                for subject in self.canary_subjects
            )
        except TypeError as exc:
            raise OperationsConfigError(
                "rollout.canary_subjects must be a list"
            ) from exc
        object.__setattr__(self, "canary_subjects", subjects)
        object.__setattr__(
            self,
            "selection_salt",
            _required_text(self.selection_salt, "rollout.selection_salt", maximum=256),
        )

        if mode is RolloutMode.DISABLED:
            if percentage != 0.0 or subjects:
                raise OperationsConfigError(
                    "disabled rollout must have percentage 0 and no canary subjects"
                )
        elif mode is RolloutMode.CANARY:
            if percentage != 0.0 or not subjects:
                raise OperationsConfigError(
                    "canary rollout requires subjects and percentage 0"
                )
        elif mode is RolloutMode.PERCENTAGE:
            if not 0.0 < percentage < 100.0 or subjects:
                raise OperationsConfigError(
                    "percentage rollout must be between 0 and 100 with no canary subjects"
                )
        elif mode is RolloutMode.ALL:
            if percentage != 100.0 or subjects:
                raise OperationsConfigError(
                    "all rollout must have percentage 100 and no canary subjects"
                )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RolloutConfig":
        if not isinstance(value, Mapping):
            raise OperationsConfigError("rollout must be an object")
        fields = {"mode", "percentage", "canary_subjects", "selection_salt"}
        _reject_unknown(value, fields, "rollout")
        mode = _enum_value(RolloutMode, value.get("mode", "disabled"), "rollout.mode")
        default_percentage = 100.0 if mode is RolloutMode.ALL else 0.0
        raw_subjects = value.get("canary_subjects", ())
        if isinstance(raw_subjects, (str, bytes, Mapping)) or raw_subjects is None:
            raise OperationsConfigError("rollout.canary_subjects must be a list")
        try:
            subjects = frozenset(raw_subjects)
        except TypeError as exc:
            raise OperationsConfigError(
                "rollout.canary_subjects must be a list"
            ) from exc
        return cls(
            mode=mode,
            percentage=value.get("percentage", default_percentage),
            canary_subjects=subjects,
            selection_salt=value.get("selection_salt", "hermes-rollout-v1"),
        )

    def select(self, subject_id: str) -> tuple[bool, str]:
        subject = _required_text(subject_id, "subject_id", maximum=512)
        if self.mode is RolloutMode.DISABLED:
            return False, "rollout_disabled"
        if self.mode is RolloutMode.ALL:
            return True, "selected_all"
        if self.mode is RolloutMode.CANARY:
            return (
                (True, "selected_canary")
                if subject in self.canary_subjects
                else (False, "not_selected_canary")
            )
        digest = hashlib.sha256(
            f"{self.selection_salt}\0{subject}".encode("utf-8")
        ).digest()
        bucket = int.from_bytes(digest[:8], "big") % 1_000_000
        selected = bucket < self.percentage * 10_000
        return (
            (True, "selected_percentage")
            if selected
            else (False, "not_selected_percentage")
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "percentage": self.percentage,
            "canary_subject_count": len(self.canary_subjects),
        }


@dataclass(frozen=True)
class CircuitBreakerConfig:
    failure_threshold: int = 3
    recovery_seconds: float = 30.0
    half_open_max_calls: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "failure_threshold",
            _positive_int(self.failure_threshold, "circuit_breaker.failure_threshold"),
        )
        object.__setattr__(
            self,
            "recovery_seconds",
            _number(
                self.recovery_seconds,
                "circuit_breaker.recovery_seconds",
                minimum=0.001,
                maximum=86_400.0,
            ),
        )
        object.__setattr__(
            self,
            "half_open_max_calls",
            _positive_int(
                self.half_open_max_calls, "circuit_breaker.half_open_max_calls", maximum=100
            ),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CircuitBreakerConfig":
        if not isinstance(value, Mapping):
            raise OperationsConfigError("circuit_breaker must be an object")
        fields = {"failure_threshold", "recovery_seconds", "half_open_max_calls"}
        _reject_unknown(value, fields, "circuit_breaker")
        return cls(
            failure_threshold=value.get("failure_threshold", 3),
            recovery_seconds=value.get("recovery_seconds", 30.0),
            half_open_max_calls=value.get("half_open_max_calls", 1),
        )


@dataclass(frozen=True)
class HealthConfig:
    stale_after_seconds: float = 30.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "stale_after_seconds",
            _number(
                self.stale_after_seconds,
                "health.stale_after_seconds",
                minimum=0.001,
                maximum=86_400.0,
            ),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "HealthConfig":
        if not isinstance(value, Mapping):
            raise OperationsConfigError("health must be an object")
        _reject_unknown(value, {"stale_after_seconds"}, "health")
        return cls(stale_after_seconds=value.get("stale_after_seconds", 30.0))


@dataclass(frozen=True)
class HermesOperationsConfig:
    manifest: HermesSidecarManifest
    rollout: RolloutConfig = RolloutConfig()
    circuit_breaker: CircuitBreakerConfig = CircuitBreakerConfig()
    health: HealthConfig = HealthConfig()

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, HermesSidecarManifest):
            raise OperationsConfigError("manifest must be a HermesSidecarManifest")
        if not isinstance(self.rollout, RolloutConfig):
            raise OperationsConfigError("rollout must be a RolloutConfig")
        if not isinstance(self.circuit_breaker, CircuitBreakerConfig):
            raise OperationsConfigError(
                "circuit_breaker must be a CircuitBreakerConfig"
            )
        if not isinstance(self.health, HealthConfig):
            raise OperationsConfigError("health must be a HealthConfig")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "HermesOperationsConfig":
        if not isinstance(value, Mapping):
            raise OperationsConfigError("Hermes operations config must be an object")
        fields = {"manifest", "rollout", "circuit_breaker", "health"}
        _reject_unknown(value, fields, "Hermes operations config")
        if "manifest" not in value:
            raise OperationsConfigError("manifest is required")
        return cls(
            manifest=HermesSidecarManifest.from_mapping(value["manifest"]),
            rollout=RolloutConfig.from_mapping(value.get("rollout", {})),
            circuit_breaker=CircuitBreakerConfig.from_mapping(
                value.get("circuit_breaker", {})
            ),
            health=HealthConfig.from_mapping(value.get("health", {})),
        )


@dataclass(frozen=True)
class CircuitPermit:
    allowed: bool
    reason: str
    probe: bool = False


class CircuitBreaker:
    """Small thread-safe breaker; state changes only from reported outcomes."""

    def __init__(
        self,
        config: CircuitBreakerConfig = CircuitBreakerConfig(),
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(config, CircuitBreakerConfig):
            raise OperationsConfigError("config must be a CircuitBreakerConfig")
        self.config = config
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: Optional[float] = None
        self._half_open_in_flight = 0
        self._lock = threading.RLock()

    def acquire(self) -> CircuitPermit:
        with self._lock:
            now = _clock_value(self._clock, "circuit breaker clock")
            if self._state is CircuitState.OPEN:
                assert self._opened_at is not None
                if now - self._opened_at < self.config.recovery_seconds:
                    return CircuitPermit(False, "circuit_open")
                self._state = CircuitState.HALF_OPEN
                self._half_open_in_flight = 0

            if self._state is CircuitState.HALF_OPEN:
                if self._half_open_in_flight >= self.config.half_open_max_calls:
                    return CircuitPermit(False, "circuit_half_open_capacity")
                self._half_open_in_flight += 1
                return CircuitPermit(True, "circuit_half_open_probe", probe=True)
            return CircuitPermit(True, "circuit_closed")

    def record_success(self, *, probe: bool = False) -> None:
        with self._lock:
            if probe and self._half_open_in_flight > 0:
                self._half_open_in_flight -= 1
            if probe and self._state is not CircuitState.HALF_OPEN:
                # A late probe result must not undo a newer open transition.
                return
            if not probe and self._state is not CircuitState.CLOSED:
                # Likewise, a request that began before the breaker opened is
                # not evidence that the recovery probe succeeded.
                return
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._opened_at = None
            self._half_open_in_flight = 0

    def record_failure(self, *, probe: bool = False) -> None:
        with self._lock:
            now = _clock_value(self._clock, "circuit breaker clock")
            if probe and self._half_open_in_flight > 0:
                self._half_open_in_flight -= 1
            self._consecutive_failures += 1
            if (
                probe
                or self._state is CircuitState.HALF_OPEN
                or self._consecutive_failures >= self.config.failure_threshold
            ):
                self._state = CircuitState.OPEN
                self._opened_at = now
                self._half_open_in_flight = 0

    def abandon(self, *, probe: bool = False) -> None:
        """Release an unused permit without treating cancellation as an outcome.

        A cancelled/deadline-expired half-open probe must free its one in-flight
        slot, but it is neither evidence of recovery nor a sidecar failure.
        """
        with self._lock:
            if probe and self._half_open_in_flight > 0:
                self._half_open_in_flight -= 1

    def restore(self, snapshot: Mapping[str, Any]) -> None:
        """Restore a redacted durable state without trusting stale in-flight work.

        A persisted half-open state is restored as an open breaker whose
        recovery delay has elapsed.  The next caller must therefore acquire a
        fresh, capacity-controlled probe instead of inheriting a crashed one.
        """

        if not isinstance(snapshot, Mapping):
            raise OperationsConfigError("circuit snapshot must be an object")
        state = _enum_value(CircuitState, snapshot.get("state"), "circuit state")
        failures = snapshot.get("consecutive_failures", 0)
        if (
            isinstance(failures, bool)
            or not isinstance(failures, int)
            or not 0 <= failures <= 1_000_000
        ):
            raise OperationsConfigError("circuit consecutive_failures is invalid")
        retry_after = _number(
            snapshot.get("retry_after_seconds", 0.0),
            "circuit retry_after_seconds",
            minimum=0.0,
            maximum=86_400.0,
        )
        with self._lock:
            now = _clock_value(self._clock, "circuit breaker clock")
            self._consecutive_failures = failures
            self._half_open_in_flight = 0
            if state is CircuitState.CLOSED:
                self._state = CircuitState.CLOSED
                self._opened_at = None
                return
            remaining = (
                min(retry_after, self.config.recovery_seconds)
                if state is CircuitState.OPEN
                else 0.0
            )
            elapsed = self.config.recovery_seconds - remaining
            self._state = CircuitState.OPEN
            self._opened_at = max(0.0, now - elapsed)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = _clock_value(self._clock, "circuit breaker clock")
            retry_after = 0.0
            if self._state is CircuitState.OPEN and self._opened_at is not None:
                retry_after = max(
                    0.0,
                    self.config.recovery_seconds - (now - self._opened_at),
                )
            return {
                "state": self._state.value,
                "consecutive_failures": self._consecutive_failures,
                "failure_threshold": self.config.failure_threshold,
                "retry_after_seconds": retry_after,
                "half_open_in_flight": self._half_open_in_flight,
                "half_open_max_calls": self.config.half_open_max_calls,
            }


class HealthTracker:
    """Stores externally supplied health probes; it never performs a probe."""

    def __init__(
        self,
        config: HealthConfig = HealthConfig(),
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(config, HealthConfig):
            raise OperationsConfigError("config must be a HealthConfig")
        self.config = config
        self._clock = clock
        self._reported_status = HealthStatus.UNKNOWN
        self._checked_at: Optional[float] = None
        self._latency_ms: Optional[float] = None
        self._reason = "not_checked"
        self._lock = threading.RLock()

    def record_probe(
        self,
        status: HealthStatus | str,
        *,
        latency_ms: Optional[float] = None,
        reason: str,
    ) -> None:
        resolved = _enum_value(HealthStatus, status, "health status")
        if resolved not in {
            HealthStatus.HEALTHY,
            HealthStatus.DEGRADED,
            HealthStatus.UNHEALTHY,
        }:
            raise OperationsConfigError(
                "reported health status must be healthy, degraded, or unhealthy"
            )
        latency: Optional[float] = None
        if latency_ms is not None:
            latency = _number(
                latency_ms, "health latency_ms", minimum=0.0, maximum=3_600_000.0
            )
        with self._lock:
            self._reported_status = resolved
            self._checked_at = _clock_value(self._clock, "health clock")
            self._latency_ms = latency
            self._reason = _reason_code(reason, "health reason")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = _clock_value(self._clock, "health clock")
            age: Optional[float] = None
            status = self._reported_status
            if self._checked_at is not None:
                age = max(0.0, now - self._checked_at)
                if age >= self.config.stale_after_seconds:
                    status = HealthStatus.STALE
            return {
                "status": status.value,
                "reported_status": self._reported_status.value,
                "reason": self._reason,
                "checked_at": self._checked_at,
                "age_seconds": age,
                "stale_after_seconds": self.config.stale_after_seconds,
                "latency_ms": self._latency_ms,
            }


@dataclass(frozen=True)
class RoutingDecision:
    target: RoutingTarget
    reason: str
    subject_hash: str
    rollout_selected: bool
    circuit_probe: bool
    timestamp: float

    @property
    def use_hermes(self) -> bool:
        return self.target is RoutingTarget.HERMES

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": self.target.value,
            "reason": self.reason,
            "subject_hash": self.subject_hash,
            "rollout_selected": self.rollout_selected,
            "circuit_probe": self.circuit_probe,
            "timestamp": self.timestamp,
        }


class HermesOperationsController:
    """Select Hermes only when rollout, health, and circuit state all permit it."""

    def __init__(
        self,
        config: HermesOperationsConfig,
        *,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
        decision_history_limit: int = 100,
        metrics_store: Optional[HermesMetricsStore] = None,
        metrics_cohort_scope: str = "default",
    ) -> None:
        if not isinstance(config, HermesOperationsConfig):
            raise OperationsConfigError("config must be a HermesOperationsConfig")
        self.config = config
        self._wall_clock = wall_clock
        self.circuit_breaker = CircuitBreaker(
            config.circuit_breaker, clock=monotonic_clock
        )
        self.health = HealthTracker(config.health, clock=wall_clock)
        history_limit = _positive_int(
            decision_history_limit, "decision_history_limit", maximum=10_000
        )
        self._history: deque[RoutingDecision] = deque(maxlen=history_limit)
        self._routing_counts: Counter[str] = Counter()
        self._outcome_counts: Counter[str] = Counter()
        self._fallback_counts: Counter[str] = Counter()
        self._lock = threading.RLock()
        self._metrics_store = (
            metrics_store
            if metrics_store is not None
            else InMemoryHermesMetricsStore(clock=wall_clock)
        )
        cohort_scope = _required_text(
            metrics_cohort_scope, "metrics_cohort_scope", maximum=128
        )
        if any(ord(char) < 32 or ord(char) == 127 for char in cohort_scope):
            raise OperationsConfigError("metrics_cohort_scope is invalid")
        self._metrics_cohort = self._rollout_cohort(config, cohort_scope)
        self._metrics_compromised = False
        try:
            durable = self._metrics_store.snapshot(cohort=self._metrics_cohort)
            circuit = durable.get("circuit_breaker")
            if not isinstance(circuit, Mapping):
                raise OperationsConfigError("durable circuit state is unavailable")
            self.circuit_breaker.restore(circuit)
        except Exception:
            # Observability is part of the production safety boundary.  A
            # corrupt or unavailable store closes the gate without exposing
            # database or exception details in public status.
            self._metrics_compromised = True

    @staticmethod
    def _rollout_cohort(
        config: HermesOperationsConfig,
        cohort_scope: str,
    ) -> str:
        rollout = config.rollout
        material = json.dumps(
            {
                "manifest_id": config.manifest.manifest_id,
                # Scope distinguishes reviewed runtime surfaces such as
                # ProjectReadOnly and NoTools.  Only its digest is exposed.
                "runtime_scope": cohort_scope,
                "mode": rollout.mode.value,
                "percentage": rollout.percentage,
                "canary_subjects": sorted(rollout.canary_subjects),
                "selection_salt": rollout.selection_salt,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(material).hexdigest()[:12]
        percentage = int(round(rollout.percentage * 1000))
        return f"{rollout.mode.value}:{percentage:06d}:{digest}"

    def _record_metric(
        self,
        kind: str,
        *,
        latency_ms: Optional[float] = None,
    ) -> bool:
        if kind not in METRIC_KINDS:
            raise OperationsConfigError("metric kind is not supported")
        with self._lock:
            if self._metrics_compromised:
                return False
        try:
            self._metrics_store.record(
                kind,
                cohort=self._metrics_cohort,
                latency_ms=latency_ms,
            )
        except Exception:
            with self._lock:
                self._metrics_compromised = True
            return False
        return True

    def _sync_circuit(self) -> bool:
        with self._lock:
            if self._metrics_compromised:
                return False
        circuit = self.circuit_breaker.snapshot()
        try:
            self._metrics_store.update_circuit(
                circuit["state"],
                consecutive_failures=circuit["consecutive_failures"],
                retry_after_seconds=circuit["retry_after_seconds"],
            )
        except Exception:
            with self._lock:
                self._metrics_compromised = True
            return False
        return True

    def _unavailable_metrics_snapshot(self) -> dict[str, Any]:
        return unavailable_metrics_snapshot(self._metrics_cohort)

    def _metrics_snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._metrics_compromised:
                return self._unavailable_metrics_snapshot()
        try:
            result = self._metrics_store.snapshot(cohort=self._metrics_cohort)
            if result.get("available") is not True:
                raise OperationsConfigError("metrics snapshot is unavailable")
        except Exception:
            with self._lock:
                self._metrics_compromised = True
            return self._unavailable_metrics_snapshot()
        return result

    def _health_gate(
        self,
        health: Mapping[str, Any],
        circuit: Mapping[str, Any],
        metrics: Mapping[str, Any],
    ) -> dict[str, Any]:
        health_status = str(health.get("status") or "unknown")
        circuit_state = str(circuit.get("state") or "unknown")
        allowed = False
        if metrics.get("available") is not True:
            reason = "metrics_unavailable"
        elif health_status != HealthStatus.HEALTHY.value:
            reason = f"health_{health_status}"
        elif circuit_state == CircuitState.OPEN.value:
            if float(circuit.get("retry_after_seconds") or 0.0) > 0:
                reason = "circuit_open"
            else:
                allowed = True
                reason = "circuit_recovery_probe"
        elif circuit_state == CircuitState.HALF_OPEN.value:
            if int(circuit.get("half_open_in_flight") or 0) >= int(
                circuit.get("half_open_max_calls") or 0
            ):
                reason = "circuit_half_open_capacity"
            else:
                allowed = True
                reason = "circuit_half_open_probe"
        elif circuit_state == CircuitState.CLOSED.value:
            allowed = True
            reason = "ready"
        else:
            reason = "circuit_unknown"
        return {
            "allowed": allowed,
            "reason": reason,
            "health_status": health_status,
            "circuit_state": circuit_state,
            "metrics_available": metrics.get("available") is True,
            "evaluated_at": _clock_value(self._wall_clock, "wall clock"),
        }

    @staticmethod
    def _subject_hash(subject_id: str) -> str:
        subject = _required_text(subject_id, "subject_id", maximum=512)
        return hashlib.sha256(subject.encode("utf-8")).hexdigest()[:16]

    def _record_decision(
        self,
        decision: RoutingDecision,
        *,
        record_metric: bool = True,
    ) -> RoutingDecision:
        fallback = decision.target is RoutingTarget.BASIC_CHAT
        with self._lock:
            self._history.append(decision)
            _increment_counter(self._routing_counts, decision.target.value)
            if fallback:
                _increment_counter(self._fallback_counts, decision.reason)
        if record_metric and fallback and decision.rollout_selected:
            self._record_metric("fallback")
        return decision

    def _fallback(
        self,
        *,
        reason: str,
        subject_hash: str,
        selected: bool,
        record_metric: bool = True,
    ) -> RoutingDecision:
        return self._record_decision(
            RoutingDecision(
                target=RoutingTarget.BASIC_CHAT,
                reason=_reason_code(reason),
                subject_hash=subject_hash,
                rollout_selected=selected,
                circuit_probe=False,
                timestamp=_clock_value(self._wall_clock, "wall clock"),
            ),
            record_metric=record_metric,
        )

    def decide(self, subject_id: str) -> RoutingDecision:
        subject_hash = self._subject_hash(subject_id)
        selected, rollout_reason = self.config.rollout.select(subject_id)
        if not selected:
            return self._fallback(
                reason=rollout_reason,
                subject_hash=subject_hash,
                selected=False,
            )

        metrics = self._metrics_snapshot()
        if metrics.get("available") is not True:
            return self._fallback(
                reason="metrics_unavailable",
                subject_hash=subject_hash,
                selected=True,
            )

        health = self.health.snapshot()
        if health["status"] != HealthStatus.HEALTHY.value:
            return self._fallback(
                reason=f"health_{health['status']}",
                subject_hash=subject_hash,
                selected=True,
            )

        permit = self.circuit_breaker.acquire()
        if not self._sync_circuit():
            self.circuit_breaker.abandon(probe=permit.probe)
            return self._fallback(
                reason="metrics_unavailable",
                subject_hash=subject_hash,
                selected=True,
            )
        if not permit.allowed:
            return self._fallback(
                reason=permit.reason,
                subject_hash=subject_hash,
                selected=True,
            )
        return self._record_decision(
            RoutingDecision(
                target=RoutingTarget.HERMES,
                reason=permit.reason if permit.probe else rollout_reason,
                subject_hash=subject_hash,
                rollout_selected=True,
                circuit_probe=permit.probe,
                timestamp=_clock_value(self._wall_clock, "wall clock"),
            )
        )

    def complete(
        self,
        decision: RoutingDecision,
        *,
        success: bool,
        failure_kind: str = "sidecar_request_failed",
        record_fallback: bool = True,
    ) -> Optional[RoutingDecision]:
        """Report a Hermes attempt and return a basic-chat fallback on failure."""
        if not isinstance(decision, RoutingDecision) or not decision.use_hermes:
            raise ValueError("only a Hermes routing decision can be completed")
        if type(success) is not bool:
            raise ValueError("success must be a boolean")
        if type(record_fallback) is not bool:
            raise ValueError("record_fallback must be a boolean")
        latency_ms = max(
            0.0,
            (
                _clock_value(self._wall_clock, "wall clock")
                - decision.timestamp
            )
            * 1000.0,
        )
        if success:
            self.circuit_breaker.record_success(probe=decision.circuit_probe)
            self._record_metric("success", latency_ms=latency_ms)
            self._sync_circuit()
            with self._lock:
                _increment_counter(self._outcome_counts, "success")
            return None

        failure_kind = _reason_code(failure_kind, "failure_kind")
        self.circuit_breaker.record_failure(probe=decision.circuit_probe)
        self._record_metric("failure", latency_ms=latency_ms)
        if failure_kind in {
            "tool_policy_denied",
            "tool_event_denied",
            "hermes_tool_event_denied",
        }:
            self._record_metric("tool_policy_denial")
        self._sync_circuit()
        with self._lock:
            _increment_counter(self._outcome_counts, "failure")
        return self._fallback(
            reason=f"sidecar_failure:{failure_kind}",
            subject_hash=decision.subject_hash,
            selected=True,
            record_metric=record_fallback,
        )

    def abandon(
        self,
        decision: RoutingDecision,
        *,
        reason: str = "cancelled",
    ) -> None:
        """Release a Hermes permit after neutral cancellation or deadline expiry."""
        if not isinstance(decision, RoutingDecision) or not decision.use_hermes:
            raise ValueError("only a Hermes routing decision can be abandoned")
        _reason_code(reason, "abandon reason")
        self.circuit_breaker.abandon(probe=decision.circuit_probe)
        self._sync_circuit()
        with self._lock:
            _increment_counter(self._outcome_counts, "abandoned")

    def record_tool_policy_denial(self) -> None:
        """Record a Workbench policy rejection without retaining its payload."""

        self._record_metric("tool_policy_denial")

    def record_probe_failure(self) -> None:
        """Record a failed readiness probe without retaining exception text."""

        self._record_metric("probe_failure")

    def record_fallback(self) -> None:
        """Record one selected-cohort basic-chat fallback attempt."""

        self._record_metric("fallback")

    def status(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot without raw subjects or secret references."""
        # Snapshot components before taking the controller lock.  Outcome
        # reporting takes the component lock first, so this order avoids a
        # controller/circuit lock inversion under concurrent traffic.
        health = self.health.snapshot()
        circuit = self.circuit_breaker.snapshot()
        metrics = self._metrics_snapshot()
        health_gate = self._health_gate(health, circuit, metrics)
        with self._lock:
            return {
                "manifest": self.config.manifest.public_dict(),
                "rollout": self.config.rollout.public_dict(),
                "health": health,
                "health_gate": health_gate,
                "circuit_breaker": circuit,
                "metrics": metrics,
                "routing_counts": dict(sorted(self._routing_counts.items())),
                "outcome_counts": dict(sorted(self._outcome_counts.items())),
                "fallback_counts": dict(sorted(self._fallback_counts.items())),
                "recent_decisions": [item.as_dict() for item in self._history],
                "generated_at": _clock_value(self._wall_clock, "wall clock"),
            }


__all__ = [
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitPermit",
    "CircuitState",
    "HealthConfig",
    "HealthStatus",
    "HealthTracker",
    "HermesOperationsConfig",
    "HermesOperationsController",
    "HermesSidecarManifest",
    "OperationsConfigError",
    "RolloutConfig",
    "RolloutMode",
    "RoutingDecision",
    "RoutingTarget",
    "SidecarTransport",
]
