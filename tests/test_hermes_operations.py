from __future__ import annotations

import ast
import inspect
import json

import pytest

import backend.hermes_operations as operations_module
from backend.hermes_operations import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
    HealthConfig,
    HealthStatus,
    HermesOperationsConfig,
    HermesOperationsController,
    HermesSidecarManifest,
    OperationsConfigError,
    RolloutConfig,
    RolloutMode,
    RoutingTarget,
    SidecarTransport,
)
from backend.hermes_metrics import InMemoryHermesMetricsStore


COMMIT = "a" * 40
DIGEST = "sha256:" + "b" * 64


class FakeClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def manifest(**overrides) -> HermesSidecarManifest:
    values = {
        "schema_version": 1,
        "release": "v0.3.2",
        "source_commit": COMMIT,
        "artifact_digest": DIGEST,
        "transport": "http",
        "endpoint": "http://127.0.0.1:8787",
        "expected_capabilities": ("chat.stream", "runs.create"),
        "max_concurrency": 4,
        "api_key_ref": "secret://hermes/sidecar-api-key",
    }
    values.update(overrides)
    return HermesSidecarManifest(**values)


def controller(
    rollout: RolloutConfig,
    *,
    clock: FakeClock | None = None,
    circuit: CircuitBreakerConfig | None = None,
    health: HealthConfig | None = None,
) -> tuple[HermesOperationsController, FakeClock]:
    fake_clock = clock or FakeClock()
    config = HermesOperationsConfig(
        manifest=manifest(),
        rollout=rollout,
        circuit_breaker=circuit or CircuitBreakerConfig(),
        health=health or HealthConfig(),
    )
    return (
        HermesOperationsController(
            config,
            wall_clock=fake_clock,
            monotonic_clock=fake_clock,
            metrics_store=InMemoryHermesMetricsStore(clock=fake_clock),
        ),
        fake_clock,
    )


def test_pinned_loopback_manifest_is_valid_and_public_view_redacts_reference() -> None:
    item = manifest()

    public = item.public_dict()
    encoded = json.dumps(public, sort_keys=True)

    assert item.transport is SidecarTransport.HTTP
    assert len(item.manifest_id) == 64
    assert public["api_key_ref_configured"] is True
    assert "secret://" not in encoded
    assert "sidecar-api-key" not in encoded


def test_gateway_manifest_uses_a_local_logical_endpoint() -> None:
    item = manifest(transport="gateway", endpoint="gateway://hermes-local")
    assert item.transport is SidecarTransport.GATEWAY


def test_https_loopback_manifest_is_valid() -> None:
    assert manifest(endpoint="https://[::1]:8787").endpoint.startswith("https://")


@pytest.mark.parametrize(
    "overrides",
    [
        {"release": "latest"},
        {"release": "*"},
        {"source_commit": "abc123"},
        {"artifact_digest": "sha256:abc123"},
        {"endpoint": "http://example.com:8787"},
        {"endpoint": "http://127.0.0.1"},
        {"endpoint": "http://127.0.0.1:8787/api"},
        {"endpoint": "http://test-user@127.0.0.1:8787"},
        {"api_key_ref": None},
        {"api_key_ref": "raw-secret-shaped-test-value"},
        {"schema_version": True},
        {"expected_capabilities": {"chat.stream": True}},
        {"expected_capabilities": (True,)},
        {"expected_capabilities": ("chat.stream", "chat.stream")},
        {"expected_capabilities": ()},
    ],
)
def test_manifest_rejects_unpinned_remote_or_unsafe_configuration(overrides) -> None:
    with pytest.raises(OperationsConfigError):
        manifest(**overrides)


def test_manifest_mapping_is_strict() -> None:
    raw = {
        "schema_version": 1,
        "release": "v0.3.2",
        "source_commit": COMMIT,
        "artifact_digest": DIGEST,
        "transport": "http",
        "endpoint": "http://localhost:8787",
        "expected_capabilities": ["chat.stream"],
        "unexpected": True,
    }
    with pytest.raises(OperationsConfigError, match="unknown manifest fields"):
        HermesSidecarManifest.from_mapping(raw)


def test_operations_config_defaults_to_disabled_rollout() -> None:
    raw = {
        "manifest": {
            "schema_version": 1,
            "release": "v0.3.2",
            "source_commit": COMMIT,
            "artifact_digest": DIGEST,
            "transport": "http",
            "endpoint": "http://localhost:8787",
            "expected_capabilities": ["chat.stream"],
            "api_key_ref": "env://HERMES_API_SERVER_KEY",
        }
    }

    config = HermesOperationsConfig.from_mapping(raw)

    assert config.rollout.mode is RolloutMode.DISABLED
    assert config.rollout.percentage == 0


@pytest.mark.parametrize(
    ("config", "subject", "selected"),
    [
        (RolloutConfig(), "session-1", False),
        (
            RolloutConfig(
                mode="canary", canary_subjects=frozenset({"session-canary"})
            ),
            "session-canary",
            True,
        ),
        (
            RolloutConfig(
                mode="canary", canary_subjects=frozenset({"session-canary"})
            ),
            "session-other",
            False,
        ),
        (RolloutConfig(mode="all", percentage=100), "session-1", True),
    ],
)
def test_rollout_modes_select_only_intended_subjects(config, subject, selected) -> None:
    assert config.select(subject)[0] is selected


def test_percentage_rollout_is_deterministic_and_salt_sensitive() -> None:
    first = RolloutConfig(mode="percentage", percentage=37.5, selection_salt="a")
    same = RolloutConfig(mode="percentage", percentage=37.5, selection_salt="a")
    other = RolloutConfig(mode="percentage", percentage=37.5, selection_salt="b")
    subjects = [f"session-{index}" for index in range(100)]

    first_results = [first.select(subject)[0] for subject in subjects]
    same_results = [same.select(subject)[0] for subject in subjects]
    other_results = [other.select(subject)[0] for subject in subjects]

    assert first_results == same_results
    assert first_results != other_results
    assert 20 <= sum(first_results) <= 55


@pytest.mark.parametrize(
    "factory",
    [
        lambda: RolloutConfig(mode="disabled", percentage=1),
        lambda: RolloutConfig(mode="canary"),
        lambda: RolloutConfig(mode="percentage", percentage=0),
        lambda: RolloutConfig(mode="percentage", percentage=100),
        lambda: RolloutConfig(mode="all", percentage=99),
    ],
)
def test_invalid_rollout_combinations_are_rejected(factory) -> None:
    with pytest.raises(OperationsConfigError):
        factory()


def test_health_is_fail_closed_until_fresh_healthy_probe() -> None:
    item, clock = controller(
        RolloutConfig(mode="all", percentage=100),
        health=HealthConfig(stale_after_seconds=10),
    )

    unknown = item.decide("session-1")
    item.health.record_probe(
        HealthStatus.DEGRADED, latency_ms=12, reason="dependency_degraded"
    )
    degraded = item.decide("session-1")
    item.health.record_probe(HealthStatus.HEALTHY, latency_ms=5, reason="probe_ok")
    healthy = item.decide("session-1")
    item.complete(healthy, success=True)
    clock.advance(10)
    stale = item.decide("session-1")

    assert (unknown.target, unknown.reason) == (
        RoutingTarget.BASIC_CHAT,
        "health_unknown",
    )
    assert degraded.reason == "health_degraded"
    assert healthy.target is RoutingTarget.HERMES
    assert stale.reason == "health_stale"


def test_circuit_breaker_opens_recovers_through_probe_and_closes() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(
        CircuitBreakerConfig(
            failure_threshold=2, recovery_seconds=5, half_open_max_calls=1
        ),
        clock=clock,
    )

    assert breaker.acquire().allowed
    breaker.record_failure()
    assert breaker.snapshot()["state"] == CircuitState.CLOSED.value
    breaker.record_failure()
    assert breaker.snapshot()["state"] == CircuitState.OPEN.value
    assert breaker.acquire().reason == "circuit_open"

    clock.advance(5)
    probe = breaker.acquire()
    blocked_probe = breaker.acquire()
    assert probe.allowed and probe.probe
    assert blocked_probe.reason == "circuit_half_open_capacity"
    breaker.record_success(probe=True)
    assert breaker.snapshot()["state"] == CircuitState.CLOSED.value


def test_failed_half_open_probe_reopens_and_late_success_cannot_close() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(
        CircuitBreakerConfig(failure_threshold=1, recovery_seconds=5), clock=clock
    )
    breaker.record_failure()
    clock.advance(5)
    probe = breaker.acquire()
    breaker.record_failure(probe=probe.probe)
    breaker.record_success(probe=True)

    assert breaker.snapshot()["state"] == CircuitState.OPEN.value


def test_abandoned_half_open_probe_releases_capacity_without_changing_state() -> None:
    item, clock = controller(
        RolloutConfig(mode="all", percentage=100),
        circuit=CircuitBreakerConfig(
            failure_threshold=1, recovery_seconds=5, half_open_max_calls=1
        ),
    )
    item.health.record_probe(HealthStatus.HEALTHY, reason="probe_ok")
    first = item.decide("session-failure")
    item.complete(first, success=False, failure_kind="transport_error")
    clock.advance(5)

    cancelled_probe = item.decide("session-cancelled")
    assert cancelled_probe.circuit_probe
    assert item.circuit_breaker.snapshot()["half_open_in_flight"] == 1

    assert item.abandon(cancelled_probe, reason="deadline") is None
    after_abandon = item.circuit_breaker.snapshot()
    assert after_abandon["state"] == CircuitState.HALF_OPEN.value
    assert after_abandon["half_open_in_flight"] == 0
    assert after_abandon["consecutive_failures"] == 1
    assert item.status()["outcome_counts"]["abandoned"] == 1

    replacement_probe = item.decide("session-replacement")
    assert replacement_probe.use_hermes and replacement_probe.circuit_probe


def test_abandon_rejects_fallback_decisions_and_invalid_reason() -> None:
    item, _ = controller(RolloutConfig())
    fallback = item.decide("session-fallback")
    with pytest.raises(ValueError, match="Hermes routing decision"):
        item.abandon(fallback)

    enabled, _ = controller(RolloutConfig(mode="all", percentage=100))
    enabled.health.record_probe(HealthStatus.HEALTHY, reason="probe_ok")
    hermes = enabled.decide("session-hermes")
    with pytest.raises(OperationsConfigError, match="abandon reason"):
        enabled.abandon(hermes, reason="Not human text")


def test_controller_returns_fallback_and_opens_breaker_after_failures() -> None:
    item, clock = controller(
        RolloutConfig(mode="all", percentage=100),
        circuit=CircuitBreakerConfig(failure_threshold=2, recovery_seconds=5),
    )
    item.health.record_probe(HealthStatus.HEALTHY, reason="probe_ok")

    first = item.decide("session-1")
    first_fallback = item.complete(
        first, success=False, failure_kind="transport_error"
    )
    second = item.decide("session-2")
    item.complete(second, success=False, failure_kind="timeout")
    blocked = item.decide("session-3")

    assert first_fallback.target is RoutingTarget.BASIC_CHAT
    assert first_fallback.reason == "sidecar_failure:transport_error"
    assert blocked.reason == "circuit_open"

    clock.advance(5)
    probe = item.decide("session-4")
    assert probe.use_hermes and probe.circuit_probe
    assert item.complete(probe, success=True) is None
    assert item.circuit_breaker.snapshot()["state"] == "closed"


def test_status_is_json_safe_observable_and_does_not_leak_subject_or_secret_ref() -> None:
    item, _ = controller(RolloutConfig(mode="all", percentage=100))
    item.health.record_probe(HealthStatus.HEALTHY, latency_ms=3, reason="probe_ok")
    decision = item.decide("private-session-identifier")
    item.complete(decision, success=False, failure_kind="timeout")

    status = item.status()
    encoded = json.dumps(status, sort_keys=True)

    assert status["routing_counts"] == {"basic_chat": 1, "hermes": 1}
    assert status["outcome_counts"] == {"failure": 1}
    assert status["fallback_counts"] == {"sidecar_failure:timeout": 1}
    assert status["health"]["status"] == "healthy"
    assert "private-session-identifier" not in encoded
    assert "secret://" not in encoded
    assert "sidecar-api-key" not in encoded


def test_metrics_include_cohort_latency_policy_denial_and_health_gate() -> None:
    item, clock = controller(
        RolloutConfig(mode="all", percentage=100),
        circuit=CircuitBreakerConfig(failure_threshold=3),
    )
    item.health.record_probe(HealthStatus.HEALTHY, reason="probe_ok")

    succeeded = item.decide("private-session-success")
    clock.advance(0.125)
    item.complete(succeeded, success=True)
    failed = item.decide("private-session-policy-denial")
    clock.advance(0.250)
    item.complete(
        failed,
        success=False,
        failure_kind="tool_policy_denied",
    )

    status = item.status()
    metrics = status["metrics"]

    assert status["health_gate"]["allowed"] is True
    assert status["health_gate"]["reason"] == "ready"
    assert metrics["cohort"].startswith("all:100000:")
    assert metrics["totals"] == {
        "failure": 1,
        "fallback": 1,
        "probe_failure": 0,
        "success": 1,
        "tool_policy_denial": 1,
    }
    assert metrics["window"]["success_rate"] == 0.5
    assert metrics["latency_ms"] == {
        "count": 2,
        "average": 187.5,
        "maximum": 250,
        "p95_recent": 250,
    }
    encoded = json.dumps(metrics, sort_keys=True)
    assert "private-session" not in encoded


def test_rollout_not_selected_fallback_does_not_pollute_promotion_evidence() -> None:
    rollout = RolloutConfig(
        mode="percentage",
        percentage=5,
        selection_salt="promotion-stage-5",
    )
    item, _clock = controller(rollout)
    item.health.record_probe(HealthStatus.HEALTHY, reason="probe_ok")
    subject = next(
        f"session-{index}"
        for index in range(10_000)
        if rollout.select(f"session-{index}")[0] is False
    )

    decision = item.decide(subject)
    metrics = item.status()["metrics"]

    assert decision.target is RoutingTarget.BASIC_CHAT
    assert decision.rollout_selected is False
    assert metrics["totals"]["fallback"] == 0
    assert metrics["window"]["sample_count"] == 0
    assert metrics["window"]["completed_attempts"] == 0


def test_durable_open_circuit_is_restored_for_a_new_controller_generation() -> None:
    clock = FakeClock()
    store = InMemoryHermesMetricsStore(clock=clock)
    config = HermesOperationsConfig(
        manifest=manifest(),
        rollout=RolloutConfig(mode="all", percentage=100),
        circuit_breaker=CircuitBreakerConfig(
            failure_threshold=1,
            recovery_seconds=5,
        ),
    )
    first = HermesOperationsController(
        config,
        wall_clock=clock,
        monotonic_clock=clock,
        metrics_store=store,
    )
    first.health.record_probe(HealthStatus.HEALTHY, reason="probe_ok")
    decision = first.decide("session-first-generation")
    first.complete(decision, success=False, failure_kind="timeout")
    assert first.status()["metrics"]["circuit_breaker"]["state"] == "open"

    restarted = HermesOperationsController(
        config,
        wall_clock=clock,
        monotonic_clock=clock,
        metrics_store=store,
    )
    restarted.health.record_probe(HealthStatus.HEALTHY, reason="probe_ok")
    blocked = restarted.decide("session-after-restart")
    assert blocked.reason == "circuit_open"

    clock.advance(5)
    probe = restarted.decide("session-recovery-probe")
    assert probe.use_hermes is True
    assert probe.circuit_probe is True


def test_metrics_evidence_does_not_cross_tool_policy_surfaces() -> None:
    clock = FakeClock()
    store = InMemoryHermesMetricsStore(clock=clock)
    config = HermesOperationsConfig(
        manifest=manifest(),
        rollout=RolloutConfig(mode="canary", canary_subjects={"session-canary"}),
    )
    readonly = HermesOperationsController(
        config,
        wall_clock=clock,
        monotonic_clock=clock,
        metrics_store=store,
        metrics_cohort_scope="project-readonly-v1",
    )
    readonly.health.record_probe(HealthStatus.HEALTHY, reason="probe_ok")
    decision = readonly.decide("session-canary")
    readonly.complete(decision, success=True)

    no_tools = HermesOperationsController(
        config,
        wall_clock=clock,
        monotonic_clock=clock,
        metrics_store=store,
        metrics_cohort_scope="no-tools-v1",
    )
    readonly_metrics = readonly.status()["metrics"]
    no_tools_metrics = no_tools.status()["metrics"]

    assert readonly_metrics["window"]["completed_attempts"] == 1
    assert no_tools_metrics["window"]["completed_attempts"] == 0
    assert readonly_metrics["cohort"] != no_tools_metrics["cohort"]


def test_metrics_store_failure_closes_the_health_gate_without_error_details() -> None:
    class BrokenStore:
        def snapshot(self, *, cohort):
            raise RuntimeError("secret prompt and C:/private/path")

        def record(self, kind, *, cohort, latency_ms=None):
            raise RuntimeError("secret prompt and C:/private/path")

        def update_circuit(
            self,
            state,
            *,
            consecutive_failures,
            retry_after_seconds,
        ):
            raise RuntimeError("secret prompt and C:/private/path")

    clock = FakeClock()
    item = HermesOperationsController(
        HermesOperationsConfig(
            manifest=manifest(),
            rollout=RolloutConfig(mode="all", percentage=100),
        ),
        wall_clock=clock,
        monotonic_clock=clock,
        metrics_store=BrokenStore(),
    )
    item.health.record_probe(HealthStatus.HEALTHY, reason="probe_ok")

    decision = item.decide("session-metrics-failure")
    status = item.status()
    encoded = json.dumps(status, sort_keys=True)

    assert decision.reason == "metrics_unavailable"
    assert status["health_gate"]["allowed"] is False
    assert status["health_gate"]["reason"] == "metrics_unavailable"
    assert status["metrics"]["available"] is False
    assert "secret prompt" not in encoded
    assert "private/path" not in encoded


def test_failed_metric_write_cannot_be_masked_by_a_later_circuit_write() -> None:
    clock = FakeClock()

    class PartialStore(InMemoryHermesMetricsStore):
        def record(self, kind, *, cohort, latency_ms=None):
            raise OSError("disk unavailable")

    store = PartialStore(clock=clock)
    item = HermesOperationsController(
        HermesOperationsConfig(
            manifest=manifest(),
            rollout=RolloutConfig(mode="all", percentage=100),
        ),
        wall_clock=clock,
        monotonic_clock=clock,
        metrics_store=store,
    )
    item.health.record_probe(HealthStatus.HEALTHY, reason="probe_ok")

    decision = item.decide("session-write-failure")
    item.complete(decision, success=True)
    status = item.status()

    assert status["metrics"]["available"] is False
    assert status["health_gate"]["allowed"] is False
    assert status["health_gate"]["reason"] == "metrics_unavailable"


def test_operations_module_has_no_process_or_network_effect_imports() -> None:
    tree = ast.parse(inspect.getsource(operations_module))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])

    assert imported_roots.isdisjoint(
        {"asyncio", "httpx", "requests", "socket", "subprocess", "urllib3"}
    )
