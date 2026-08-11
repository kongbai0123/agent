"""Optional Hermes sidecar integration primitives (phases 1-3)."""

from .chat import HermesChatResult, HermesTextChatAdapter, normalize_text_messages
from .client import HermesSidecarClient, SSEEvent, SSEEventStream, iter_sse_events
from .config import HermesConfig, validate_loopback_base_url
from .context_budget import (
    HERMES_CONTEXT_WINDOW_TOKENS,
    HERMES_INTERNAL_RESERVE_TOKENS,
    HERMES_OUTPUT_RESERVE_TOKENS,
    HERMES_WORKBENCH_INPUT_BUDGET_TOKENS,
    HermesBudgetedContext,
    HermesContextBudgetError,
    assert_run_context_budget,
    budget_hermes_context,
    estimate_run_input_tokens,
)
from .errors import (
    HermesAuthenticationError,
    HermesConfigurationError,
    HermesConflictError,
    HermesDisabledError,
    HermesError,
    HermesProtocolError,
    HermesUnavailableError,
)
from .mapping import HermesRunMapping, HermesRunMappingStore, HermesSessionMapping
from .runs import HermesRunSnapshot, HermesRunsBridge

__all__ = [
    "HermesAuthenticationError",
    "HermesChatResult",
    "HermesConfig",
    "HermesBudgetedContext",
    "HermesContextBudgetError",
    "HermesConfigurationError",
    "HermesConflictError",
    "HermesDisabledError",
    "HermesError",
    "HermesProtocolError",
    "HermesRunMapping",
    "HermesRunMappingStore",
    "HermesRunSnapshot",
    "HermesRunsBridge",
    "HERMES_CONTEXT_WINDOW_TOKENS",
    "HERMES_INTERNAL_RESERVE_TOKENS",
    "HERMES_OUTPUT_RESERVE_TOKENS",
    "HERMES_WORKBENCH_INPUT_BUDGET_TOKENS",
    "HermesSessionMapping",
    "HermesSidecarClient",
    "HermesTextChatAdapter",
    "HermesUnavailableError",
    "SSEEvent",
    "SSEEventStream",
    "iter_sse_events",
    "normalize_text_messages",
    "assert_run_context_budget",
    "budget_hermes_context",
    "estimate_run_input_tokens",
    "validate_loopback_base_url",
]
