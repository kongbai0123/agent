"""Unified host-side model boundary for Hook-governed transport calls."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterator, Mapping, Optional, TypeVar

try:
    from hook_runtime import (
        GuardAction,
        HookContext,
        HookDispatcher,
        HookTransformFailed,
        get_hook_dispatcher,
    )
except ImportError:  # pragma: no cover - package import compatibility
    from backend.hook_runtime import (
        GuardAction,
        HookContext,
        HookDispatcher,
        HookTransformFailed,
        get_hook_dispatcher,
    )


class ModelGatewayDenied(RuntimeError):
    code = "MODEL_REQUEST_DENIED"


PayloadValidator = Callable[[Any], Mapping[str, Any]]
_ResultT = TypeVar("_ResultT")


@dataclass
class ModelGatewayCall:
    context: HookContext
    payload: dict[str, Any]
    response: Any
    terminal_event: Optional[str] = None


class ModelGateway:
    """Apply the same transform/guard/observe contract to every host model call."""

    def __init__(self, hooks: Optional[HookDispatcher] = None) -> None:
        self._hooks = hooks

    @property
    def hooks(self) -> HookDispatcher:
        return self._hooks or get_hook_dispatcher()

    async def start(
        self,
        *,
        context: HookContext,
        payload: Mapping[str, Any],
        transport: Callable[..., Any],
        transport_args: tuple[Any, ...] = (),
        transport_kwargs: Optional[Mapping[str, Any]] = None,
        validator: Optional[PayloadValidator] = None,
    ) -> ModelGatewayCall:
        if context.event != "model.request.transform":
            raise ValueError("model gateway context must start at model.request.transform")
        transformed = await self.hooks.transform(
            "model.request.transform", context, dict(payload)
        )
        try:
            validated = validator(transformed) if validator else transformed
            if not isinstance(validated, Mapping):
                raise TypeError("model request payload must remain an object")
            governed = dict(validated)
        except HookTransformFailed:
            raise
        except Exception as error:
            raise HookTransformFailed(
                "model request transform violated the host payload contract"
            ) from error
        guard = await self.hooks.guard(
            "model.request.guard", context.for_event("model.request.guard")
        )
        if guard.action is GuardAction.DENY:
            raise ModelGatewayDenied(
                guard.reason or "A trusted hook denied the model request."
            )
        if guard.action is GuardAction.REQUIRE_APPROVAL:
            raise ModelGatewayDenied(
                "Model transport approval is unsupported, so the request was denied."
            )
        await self.hooks.observe("model.started", context.for_event("model.started"))
        try:
            if inspect.iscoroutinefunction(transport):
                response = await transport(
                    governed, *transport_args, **dict(transport_kwargs or {})
                )
            else:
                response = await asyncio.to_thread(
                    transport,
                    governed,
                    *transport_args,
                    **dict(transport_kwargs or {}),
                )
                if inspect.isawaitable(response):
                    response = await response
        except BaseException:
            await self.hooks.observe(
                "model.failed", context.for_event("model.failed")
            )
            raise
        return ModelGatewayCall(context=context, payload=governed, response=response)

    async def completed(self, call: ModelGatewayCall) -> None:
        if call.terminal_event is not None:
            return
        call.terminal_event = "model.completed"
        await self.hooks.observe(
            "model.completed", call.context.for_event("model.completed")
        )

    async def failed(self, call: ModelGatewayCall) -> None:
        if call.terminal_event is not None:
            return
        call.terminal_event = "model.failed"
        await self.hooks.observe(
            "model.failed", call.context.for_event("model.failed")
        )

    @staticmethod
    def _run_sync(factory: Callable[[], Awaitable[_ResultT]]) -> _ResultT:
        """Run one gateway coroutine from synchronous host code.

        Most host-side callers run in worker threads, where ``asyncio.run`` is
        sufficient.  A few tests and embedders invoke those same synchronous
        adapters while an event loop is already active.  Nesting an event loop
        would fail (and scheduling onto the blocked caller loop would deadlock),
        so that uncommon case is isolated in one short-lived worker thread.
        The coroutine is created inside the thread to avoid binding it to the
        caller's loop.
        """

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(factory())
        with ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="model-gateway-sync"
        ) as executor:
            return executor.submit(lambda: asyncio.run(factory())).result()

    def start_sync(
        self,
        *,
        context: HookContext,
        payload: Mapping[str, Any],
        transport: Callable[..., Any],
        transport_args: tuple[Any, ...] = (),
        transport_kwargs: Optional[Mapping[str, Any]] = None,
        validator: Optional[PayloadValidator] = None,
    ) -> ModelGatewayCall:
        return self._run_sync(
            lambda: self.start(
                context=context,
                payload=payload,
                transport=transport,
                transport_args=transport_args,
                transport_kwargs=transport_kwargs,
                validator=validator,
            )
        )

    def completed_sync(self, call: ModelGatewayCall) -> None:
        self._run_sync(lambda: self.completed(call))

    def failed_sync(self, call: ModelGatewayCall) -> None:
        self._run_sync(lambda: self.failed(call))

    @contextmanager
    def sync_call(
        self,
        *,
        context: HookContext,
        payload: Mapping[str, Any],
        transport: Callable[..., Any],
        transport_args: tuple[Any, ...] = (),
        transport_kwargs: Optional[Mapping[str, Any]] = None,
        validator: Optional[PayloadValidator] = None,
    ) -> Iterator[ModelGatewayCall]:
        """Govern a complete synchronous model call and its terminal event.

        Parsing and status validation should stay inside this context.  Any
        exception then produces ``model.failed``; a normal exit produces
        ``model.completed``.  Terminal delivery is idempotent on the call.
        """

        call = self.start_sync(
            context=context,
            payload=payload,
            transport=transport,
            transport_args=transport_args,
            transport_kwargs=transport_kwargs,
            validator=validator,
        )
        try:
            yield call
        except BaseException:
            try:
                self.failed_sync(call)
            except BaseException:
                # Never hide the model/consumer failure behind observer cleanup.
                pass
            raise
        else:
            self.completed_sync(call)

    @contextmanager
    def post_chat_sync(
        self,
        *,
        context: HookContext,
        settings: Mapping[str, Any],
        payload: Mapping[str, Any],
        post_chat: Callable[..., Any],
        post_chat_kwargs: Optional[Mapping[str, Any]] = None,
        validator: Optional[PayloadValidator] = None,
    ) -> Iterator[ModelGatewayCall]:
        """Preserve the injected ``post_chat(settings, payload, **kwargs)`` API."""

        kwargs = dict(post_chat_kwargs or {})
        with self.sync_call(
            context=context,
            payload=payload,
            transport=lambda governed: post_chat(settings, governed, **kwargs),
            validator=validator,
        ) as call:
            yield call


def model_hook_context(
    *,
    runtime: str,
    model: str,
    project_id: Any = None,
    session_id: Any = None,
    run_id: Any = None,
    retry_of_run_id: Any = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> HookContext:
    """Build the same minimal, secret-redacted context for host model callers."""

    def optional_id(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    safe_metadata = dict(metadata or {})
    safe_metadata.update(
        {"runtime": str(runtime or "host"), "model": str(model or "")}
    )
    return HookContext(
        event="model.request.transform",
        project_id=optional_id(project_id),
        session_id=optional_id(session_id),
        run_id=optional_id(run_id),
        call_id=f"model_{uuid.uuid4().hex}",
        retry_of_run_id=optional_id(retry_of_run_id),
        metadata=safe_metadata,
    )


def validate_tool_free_model_payload(value: Any) -> dict[str, Any]:
    """Reassert the fixed no-tools policy after request transforms."""

    if not isinstance(value, Mapping):
        raise ValueError("model hook returned a non-object payload")
    result = dict(value)
    model = result.get("model")
    if not isinstance(model, str) or not model.strip() or len(model) > 512:
        raise ValueError("model hook returned an invalid model")
    if not isinstance(result.get("messages"), list):
        raise ValueError("model hook returned invalid messages")
    if result.get("stream") is not False:
        raise ValueError("tool-free host model calls must remain non-streaming")
    forbidden = {"tools", "tool_choice", "functions", "function_call"}
    if forbidden.intersection(result):
        raise ValueError("tool-free host model calls cannot expose tools")
    return result


_DEFAULT_GATEWAY = ModelGateway()


def get_model_gateway() -> ModelGateway:
    return _DEFAULT_GATEWAY


def configure_model_gateway(gateway: Optional[ModelGateway]) -> ModelGateway:
    global _DEFAULT_GATEWAY
    if gateway is not None and not isinstance(gateway, ModelGateway):
        raise TypeError("gateway must be ModelGateway or None")
    _DEFAULT_GATEWAY = gateway or ModelGateway()
    return _DEFAULT_GATEWAY


__all__ = [
    "ModelGateway",
    "ModelGatewayCall",
    "ModelGatewayDenied",
    "configure_model_gateway",
    "get_model_gateway",
    "model_hook_context",
    "validate_tool_free_model_payload",
]
