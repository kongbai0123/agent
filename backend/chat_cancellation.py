import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Set


class ChatRunCancelled(RuntimeError):
    pass


class ChatRunDeadlineExceeded(ChatRunCancelled):
    """The run exhausted its absolute wall-clock budget.

    Subclassing :class:`ChatRunCancelled` is deliberate: every existing cleanup
    path (close the HTTP response, release the model slot, verify the unload)
    already handles cancellation correctly, and a deadline is just a
    cancellation the runtime issued to itself. Callers that need to tell the two
    apart ask :meth:`ChatRunControl.deadline_exceeded`.
    """


#: Phase totals that are always present in the metrics payload, even at zero,
#: so a report can prove a phase did not happen rather than merely omitting it.
PHASE_NAMES = (
    "model_load",
    "approval_wait",
    "generation",
    "tool_execution",
    "repair",
    "validation",
)

_MAX_PHASE_SPANS = 400


@dataclass
class ChatRunControl:
    run_id: str
    session_id: str
    turn_id: str
    model: str
    mode: str
    prompt_digest: str = ""
    project_id: Optional[str] = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    _responses: Dict[int, Any] = field(default_factory=dict)
    _models: Set[str] = field(default_factory=set)
    _preexisting_models: Set[str] = field(default_factory=set)
    _preexisting_snapshot_known: bool = False
    _cleanup_report: Optional[Dict[str, Any]] = None
    _external_models: Set[str] = field(default_factory=set)
    _usage_events: list[Dict[str, Any]] = field(default_factory=list)
    _billing: Dict[str, Any] = field(default_factory=dict)
    _phase_totals: Dict[str, float] = field(default_factory=dict)
    _phase_spans: List[Dict[str, Any]] = field(default_factory=list)
    _phase_spans_dropped: int = 0
    _budget_seconds: Optional[float] = None
    _deadline_at: Optional[float] = None
    _deadline_started_at: Optional[float] = None
    _expired: bool = False
    _expired_after_seconds: Optional[float] = None
    _leased_models: Set[str] = field(default_factory=set)
    _model_leases_in_release: Set[str] = field(default_factory=set)
    _model_lease_closing: bool = False
    _lease_grants: int = 0
    _lease_release_events: List[Dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        # Namespaced models execute remotely and must never be sent to
        # Ollama's local unload endpoint during cancellation cleanup.
        if "::" not in str(self.model or ""):
            self.track_model(self.model)

    def attach(self, response: Any) -> None:
        with self._lock:
            if self.cancelled.is_set():
                try:
                    response.close()
                finally:
                    raise ChatRunCancelled("Chat run was cancelled")
            self._responses[id(response)] = response

    def detach(self, response: Any) -> None:
        with self._lock:
            self._responses.pop(id(response), None)

    def cancel(self) -> int:
        self.cancelled.set()
        with self._lock:
            responses = list(self._responses.values())
            self._responses.clear()
        for response in responses:
            try:
                response.close()
            except Exception:
                pass
        return len(responses)

    def track_model(self, model: str) -> None:
        normalized = str(model or "").strip()
        if not normalized:
            return
        with self._lock:
            self._models.add(normalized)

    def tracked_models(self) -> Set[str]:
        with self._lock:
            return set(self._models)

    def set_preexisting_models(self, models: Optional[Set[str]]) -> None:
        with self._lock:
            self._preexisting_snapshot_known = models is not None
            self._preexisting_models = set(models or set())

    def cleanup_protection(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "preexisting_snapshot_known": self._preexisting_snapshot_known,
                "preexisting_models": set(self._preexisting_models),
            }

    def model_was_preexisting(self, model: str) -> bool:
        normalized = str(model or "").strip()
        with self._lock:
            return self._preexisting_snapshot_known and normalized in self._preexisting_models

    def set_external_models(self, models: Set[str]) -> None:
        with self._lock:
            self._external_models = set(models)

    def external_models(self) -> Set[str]:
        with self._lock:
            return set(self._external_models)

    def record_usage(
        self,
        *,
        agent_id: str,
        role: str,
        model: str,
        metrics: Optional[Dict[str, Any]],
    ) -> None:
        payload = dict(metrics or {})
        prompt_tokens = max(0, int(payload.get("prompt_tokens") or 0))
        completion_tokens = max(0, int(payload.get("completion_tokens") or 0))
        load_duration_ns = max(0, int(payload.get("load_duration_ns") or 0))
        with self._lock:
            self._usage_events.append({
                "agent_id": str(agent_id),
                "role": str(role),
                "model": str(model),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "load_duration_ns": load_duration_ns,
                "eval_duration_ns": max(0, int(payload.get("eval_duration_ns") or 0)),
            })
            # Ollama reports weight-loading time separately from evaluation time,
            # so model_load_ms is measured rather than inferred. It stays near
            # zero once a run holds a lease on the model.
            if load_duration_ns:
                self._record_phase_locked(
                    "model_load",
                    load_duration_ns / 1_000_000,
                    detail={"agent_id": str(agent_id), "model": str(model)},
                )

    def configure_billing(
        self,
        *,
        provider: str,
        input_cost_per_million: float = 0.0,
        output_cost_per_million: float = 0.0,
        currency: str = "USD",
    ) -> None:
        with self._lock:
            self._billing = {
                "provider": str(provider or "ollama"),
                "input_cost_per_million": max(0.0, float(input_cost_per_million or 0.0)),
                "output_cost_per_million": max(0.0, float(output_cost_per_million or 0.0)),
                "currency": str(currency or "USD").upper()[:8],
            }

    def usage_summary(self) -> Dict[str, Any]:
        with self._lock:
            events = [dict(item) for item in self._usage_events]
            billing = dict(self._billing)
        prompt_tokens = sum(item["prompt_tokens"] for item in events)
        completion_tokens = sum(item["completion_tokens"] for item in events)
        input_rate = float(billing.get("input_cost_per_million") or 0.0)
        output_rate = float(billing.get("output_cost_per_million") or 0.0)
        estimated_cost = prompt_tokens * input_rate / 1_000_000 + completion_tokens * output_rate / 1_000_000
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "load_duration_ns": sum(item["load_duration_ns"] for item in events),
            "eval_duration_ns": sum(item["eval_duration_ns"] for item in events),
            "provider": billing.get("provider") or "ollama",
            "estimated_cost": round(estimated_cost, 8),
            "currency": billing.get("currency") or "USD",
            "pricing": {
                "input_per_million": input_rate,
                "output_per_million": output_rate,
            },
            "by_agent": events,
        }

    def set_cleanup_report(self, report: Dict[str, Any]) -> None:
        with self._lock:
            self._cleanup_report = dict(report)

    def cleanup_report(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._cleanup_report) if self._cleanup_report is not None else None

    # ------------------------------------------------------------------
    # Phase timings
    # ------------------------------------------------------------------
    def _record_phase_locked(
        self,
        phase: str,
        duration_ms: float,
        *,
        started_at: Optional[float] = None,
        ended_at: Optional[float] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        key = str(phase or "").strip()
        if not key:
            return
        amount = max(0.0, float(duration_ms or 0.0))
        self._phase_totals[key] = self._phase_totals.get(key, 0.0) + amount
        if len(self._phase_spans) >= _MAX_PHASE_SPANS:
            self._phase_spans_dropped += 1
            return
        span: Dict[str, Any] = {"phase": key, "duration_ms": round(amount, 3)}
        if started_at is not None:
            span["started_at"] = round(float(started_at), 6)
        if ended_at is not None:
            span["ended_at"] = round(float(ended_at), 6)
        for detail_key, detail_value in dict(detail or {}).items():
            span[str(detail_key)] = detail_value
        self._phase_spans.append(span)

    def record_phase(
        self,
        phase: str,
        duration_ms: float,
        *,
        started_at: Optional[float] = None,
        ended_at: Optional[float] = None,
        **detail: Any,
    ) -> None:
        with self._lock:
            self._record_phase_locked(
                phase, duration_ms, started_at=started_at, ended_at=ended_at, detail=detail
            )

    @contextmanager
    def track_phase(self, phase: str, **detail: Any) -> Iterator[None]:
        """Time a synchronous block and attribute it to one phase."""
        started_wall = time.time()
        started = time.monotonic()
        try:
            yield
        finally:
            duration_ms = (time.monotonic() - started) * 1000
            self.record_phase(
                phase, duration_ms, started_at=started_wall, ended_at=time.time(), **detail
            )

    def phase_timings(self) -> Dict[str, Any]:
        with self._lock:
            totals = dict(self._phase_totals)
            spans = [dict(item) for item in self._phase_spans]
            dropped = int(self._phase_spans_dropped)
        report: Dict[str, Any] = {f"{name}_ms": 0 for name in PHASE_NAMES}
        for name, value in totals.items():
            report[f"{name}_ms"] = int(round(value))
        report["phase_spans"] = spans
        report["phase_spans_dropped"] = dropped
        return report

    # ------------------------------------------------------------------
    # Absolute wall-clock deadline
    # ------------------------------------------------------------------
    def start_deadline(self, budget_seconds: Optional[float]) -> None:
        """Arm an absolute budget for the whole run.

        The deadline is anchored once, at arming time. Nothing that happens
        later -- a streamed token, a tool result, an approval decision -- moves
        it, which is the difference between this and the per-request HTTP
        timeout it complements.
        """
        value = float(budget_seconds or 0)
        with self._lock:
            if value <= 0:
                self._budget_seconds = None
                self._deadline_at = None
                self._deadline_started_at = None
                return
            self._budget_seconds = value
            self._deadline_started_at = time.monotonic()
            self._deadline_at = self._deadline_started_at + value

    def deadline_remaining(self) -> Optional[float]:
        with self._lock:
            if self._deadline_at is None:
                return None
            return self._deadline_at - time.monotonic()

    def deadline_exceeded(self) -> bool:
        with self._lock:
            if self._expired:
                return True
            return self._deadline_at is not None and time.monotonic() >= self._deadline_at

    def bounded_timeout(self, requested_seconds: float, *, minimum: float = 1.0) -> float:
        """Clamp a per-call timeout so it cannot outlive the run budget."""
        requested = max(0.0, float(requested_seconds or 0.0))
        remaining = self.deadline_remaining()
        if remaining is None:
            return requested
        return max(float(minimum), min(requested, remaining))

    def raise_if_over_deadline(self) -> None:
        if not self.deadline_exceeded():
            return
        first_observation = False
        with self._lock:
            if not self._expired:
                self._expired = True
                if self._deadline_started_at is not None:
                    self._expired_after_seconds = time.monotonic() - self._deadline_started_at
                first_observation = True
            budget = self._budget_seconds
        if first_observation:
            # Close the sockets exactly as a user cancellation would, so a
            # model that is still emitting tokens cannot keep the run alive.
            self.cancel()
        raise ChatRunDeadlineExceeded(
            f"Chat run exceeded its wall-clock budget of {budget if budget is not None else 0:.0f}s"
        )

    def raise_if_cancelled_or_expired(self) -> None:
        self.raise_if_over_deadline()
        self.raise_if_cancelled()

    def deadline_report(self) -> Dict[str, Any]:
        with self._lock:
            budget = self._budget_seconds
            expired = bool(self._expired)
            expired_after = self._expired_after_seconds
            deadline_at = self._deadline_at
            remaining = None if deadline_at is None else deadline_at - time.monotonic()
        return {
            "budget_seconds": budget,
            "armed": budget is not None,
            "exceeded": expired,
            "expired_after_seconds": round(expired_after, 3) if expired_after is not None else None,
            "remaining_seconds": round(remaining, 3) if remaining is not None else None,
        }

    # ------------------------------------------------------------------
    # Same-task model lease
    # ------------------------------------------------------------------
    def hold_model_lease(self, model: str) -> bool:
        """Declare that this run keeps ``model`` resident between its calls."""
        normalized = str(model or "").strip()
        if not normalized:
            return False
        with self._lock:
            # Once cancellation or run teardown owns lease settlement, a late
            # call must not create a second cleanup obligation. It will use the
            # ordinary per-call release path instead.
            if self._model_lease_closing:
                return False
            newly_leased = normalized not in self._leased_models
            self._leased_models.add(normalized)
            if newly_leased:
                self._lease_grants += 1
            return newly_leased

    def holds_model_lease(self, model: str) -> bool:
        normalized = str(model or "").strip()
        with self._lock:
            return (
                normalized in self._leased_models
                or normalized in self._model_leases_in_release
            )

    def leased_models(self) -> Set[str]:
        with self._lock:
            return set(self._leased_models)

    def claim_model_leases(
        self,
        models: Optional[Set[str]] = None,
        *,
        closing: bool = False,
    ) -> Set[str]:
        """Atomically take ownership of selected model-release obligations."""
        requested = (
            None
            if models is None
            else {str(model or "").strip() for model in models if str(model or "").strip()}
        )
        with self._lock:
            if closing:
                self._model_lease_closing = True
            claimed = set(self._leased_models)
            if requested is not None:
                claimed &= requested
            self._leased_models.difference_update(claimed)
            self._model_leases_in_release.update(claimed)
            return claimed

    def model_leases_in_release(self) -> Set[str]:
        with self._lock:
            return set(self._model_leases_in_release)

    def clear_model_lease(self, model: str) -> None:
        normalized = str(model or "").strip()
        with self._lock:
            self._leased_models.discard(normalized)
            self._model_leases_in_release.discard(normalized)

    def record_model_lease_release(self, model: str, report: Dict[str, Any]) -> None:
        normalized = str(model or "").strip()
        if not normalized:
            return
        event = {
            "model": normalized,
            "reason": str(report.get("reason") or "unknown"),
            "state": str(report.get("state") or "unknown"),
            "released": bool(report.get("released", False)),
            "shared": bool(report.get("shared", False)),
            "remaining_holders": [
                str(item) for item in report.get("remaining_holders") or []
            ],
        }
        with self._lock:
            self._lease_release_events.append(event)

    def model_lease_report(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "granted": int(self._lease_grants),
                "release_count": len(self._lease_release_events),
                "releases": [dict(item) for item in self._lease_release_events],
                "held": sorted(self._leased_models | self._model_leases_in_release),
            }

    def raise_if_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise ChatRunCancelled("Chat run was cancelled")


_CONTROLS: Dict[str, ChatRunControl] = {}
_RECENT_CONTROLS: Dict[str, tuple[float, ChatRunControl]] = {}
_PENDING_CANCELS: Dict[str, float] = {}
_CONTROLS_LOCK = threading.Lock()
_RECENT_CONTROL_TTL_SECONDS = 60


def _prune_locked(now: Optional[float] = None) -> None:
    current = time.time() if now is None else now
    pending_cutoff = current - 300
    recent_cutoff = current - _RECENT_CONTROL_TTL_SECONDS
    for pending_id, created_at in list(_PENDING_CANCELS.items()):
        if created_at < pending_cutoff:
            _PENDING_CANCELS.pop(pending_id, None)
    for recent_id, (released_at, _control) in list(_RECENT_CONTROLS.items()):
        if released_at < recent_cutoff:
            _RECENT_CONTROLS.pop(recent_id, None)


def register_chat_run(
    run_id: str,
    session_id: str,
    turn_id: str,
    model: str,
    mode: str,
    *,
    prompt_digest: str = "",
) -> ChatRunControl:
    control = ChatRunControl(
        run_id,
        session_id,
        turn_id,
        model,
        mode,
        prompt_digest=str(prompt_digest or ""),
    )
    with _CONTROLS_LOCK:
        _prune_locked()
        _RECENT_CONTROLS.pop(run_id, None)
        previous = _CONTROLS.get(run_id)
        if previous:
            previous.cancel()
        _CONTROLS[run_id] = control
        pending_cancel = _PENDING_CANCELS.pop(run_id, None)
    if pending_cancel is not None:
        control.cancel()
    return control


def get_chat_run(run_id: str) -> Optional[ChatRunControl]:
    with _CONTROLS_LOCK:
        return _CONTROLS.get(run_id)


def get_chat_run_for_cancel(run_id: str) -> Optional[ChatRunControl]:
    with _CONTROLS_LOCK:
        _prune_locked()
        active = _CONTROLS.get(run_id)
        if active:
            return active
        recent = _RECENT_CONTROLS.get(run_id)
        return recent[1] if recent else None


def active_chat_models(exclude_run_id: Optional[str] = None) -> Set[str]:
    with _CONTROLS_LOCK:
        controls = list(_CONTROLS.values())
    models: Set[str] = set()
    for control in controls:
        if control.run_id == exclude_run_id or control.cancelled.is_set():
            continue
        models.update(control.tracked_models())
    return models


def has_active_chat_run(session_id: str) -> bool:
    """Return whether a non-cancelled Run currently owns the Session scope."""

    with _CONTROLS_LOCK:
        return any(
            control.session_id == session_id and not control.cancelled.is_set()
            for control in _CONTROLS.values()
        )


def cancel_session_chat_runs(
    session_id: str,
    *,
    exclude_run_id: Optional[str] = None,
) -> List[str]:
    """Supersede active turns in one session without affecting other chats."""

    with _CONTROLS_LOCK:
        controls = [
            control for control in _CONTROLS.values()
            if control.session_id == session_id
            and control.run_id != exclude_run_id
            and not control.cancelled.is_set()
        ]
    for control in controls:
        control.cancel()
    return [control.run_id for control in controls]


def cancel_chat_run(run_id: str) -> Optional[Dict[str, Any]]:
    control = get_chat_run_for_cancel(run_id)
    if not control:
        return None
    closed_responses = control.cancel()
    return {"run_id": run_id, "cancelled": True, "closed_responses": closed_responses}


def cancel_or_defer_chat_run(run_id: str) -> Dict[str, Any]:
    result = cancel_chat_run(run_id)
    if result:
        return result
    with _CONTROLS_LOCK:
        _PENDING_CANCELS[run_id] = time.time()
    return {"run_id": run_id, "cancelled": True, "closed_responses": 0, "pending_registration": True}


def release_chat_run(run_id: str, control: ChatRunControl) -> None:
    with _CONTROLS_LOCK:
        if _CONTROLS.get(run_id) is control:
            _CONTROLS.pop(run_id, None)
            _RECENT_CONTROLS[run_id] = (time.time(), control)
        _prune_locked()
