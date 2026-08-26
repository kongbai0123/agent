"""Provider-neutral semantic embedding and reranking adapters.

The knowledge index owns chunking, project isolation, and durable vectors.  This
module owns the two execution boundaries that can produce semantic scores:

* trusted local Sentence Transformers models loaded from an existing absolute
  directory, with downloads and remote model code disabled; and
* configured HTTP providers, guarded by the Workbench extension gate, project
  data consent, provider health, budgets, and the usage ledger.

No adapter receives a filesystem path, API key, or text from another project.
Callers must create one :class:`SemanticRequestContext` from the authoritative
project/run before an operation.  HTTP errors are intentionally sanitized and
are never retried here; the provider state machine decides when a later call may
perform a half-open probe.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit, urlunsplit

import requests


MAX_PROVIDER_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_LOCAL_MODEL_ENTRIES = 50_000
_PROVIDER_ID = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class SemanticRetrievalError(RuntimeError):
    """Stable, sanitized failure for a semantic adapter boundary."""

    code = "SEMANTIC_RETRIEVAL_FAILED"
    status_code = 502

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        retry_at: str | None = None,
    ) -> None:
        super().__init__(message)
        if code:
            self.code = code
        if status_code is not None:
            self.status_code = int(status_code)
        self.retry_at = retry_at


class SemanticConsentRequired(SemanticRetrievalError):
    code = "SEMANTIC_DATA_CONSENT_REQUIRED"
    status_code = 409

    def __init__(
        self,
        message: str,
        *,
        provider_id: str = "",
        model_reference: str = "",
    ) -> None:
        super().__init__(message, code=self.code, status_code=self.status_code)
        # These are non-secret routing identifiers used by the Host to create
        # an exact, model-bound consent proposal.  They are never accepted from
        # provider response text.
        self.provider_id = str(provider_id or "").strip().casefold()
        self.model_reference = str(model_reference or "").strip()


@dataclass(frozen=True)
class SemanticRequestContext:
    """Authority carried through one project-scoped semantic operation.

    ``consent_proposal_id`` is optional because remembered project policy can
    authorize a provider.  When supplied it must be the approved, model-bound
    proposal for this exact project and run.  The governance bridge consumes it
    once and permits the bounded batches that form that same logical operation.
    """

    project_id: str
    run_id: str = ""
    consent_proposal_id: str = ""
    requested_model: str = ""
    budget_override_id: str = ""

    def __post_init__(self) -> None:
        project = str(self.project_id or "").strip()
        if not _PROJECT_ID.fullmatch(project):
            raise SemanticRetrievalError(
                "A valid project is required for semantic retrieval.",
                code="SEMANTIC_PROJECT_REQUIRED",
                status_code=422,
            )
        object.__setattr__(self, "project_id", project)
        for label, value, maximum in (
            ("run_id", self.run_id, 160),
            ("consent_proposal_id", self.consent_proposal_id, 160),
            ("requested_model", self.requested_model, 240),
            ("budget_override_id", self.budget_override_id, 160),
        ):
            text = str(value or "")
            if len(text) > maximum or _CONTROL.search(text):
                raise SemanticRetrievalError(
                    f"{label} is invalid.",
                    code="SEMANTIC_CONTEXT_INVALID",
                    status_code=422,
                )


def _is_loopback_host(hostname: str | None) -> bool:
    host = str(hostname or "").strip().casefold().rstrip(".")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _normalized_base_url(value: str) -> str:
    raw = str(value or "").strip().rstrip("/")
    parsed = urlsplit(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise SemanticRetrievalError(
            "Semantic provider URL is invalid.",
            code="SEMANTIC_PROVIDER_CONFIG_INVALID",
            status_code=422,
        )
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        raise SemanticRetrievalError(
            "Remote semantic providers must use HTTPS.",
            code="SEMANTIC_PROVIDER_INSECURE",
            status_code=422,
        )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _capability_url(base_url: str, configured: str, default_name: str) -> str:
    base = _normalized_base_url(base_url)
    endpoint = str(configured or "").strip()
    if not endpoint:
        return f"{base}/{default_name}"
    parsed = urlsplit(endpoint)
    if parsed.scheme:
        normalized = _normalized_base_url(endpoint)
        left = urlsplit(base)
        right = urlsplit(normalized)
        if (left.scheme, left.hostname, left.port) != (
            right.scheme,
            right.hostname,
            right.port,
        ):
            raise SemanticRetrievalError(
                "Semantic capability endpoint must use the provider origin.",
                code="SEMANTIC_PROVIDER_CONFIG_INVALID",
                status_code=422,
            )
        return normalized
    if not endpoint.startswith("/") or "?" in endpoint or "#" in endpoint:
        raise SemanticRetrievalError(
            "Semantic capability endpoint must be an absolute path.",
            code="SEMANTIC_PROVIDER_CONFIG_INVALID",
            status_code=422,
        )
    origin = urlsplit(base)
    return urlunsplit((origin.scheme, origin.netloc, endpoint, "", ""))


@dataclass(frozen=True)
class SemanticProviderRoute:
    """One configured model route; wire contracts remain independently pluggable."""

    provider_id: str
    model_id: str
    base_url: str
    embedding_endpoint: str = ""
    rerank_endpoint: str = ""
    document_input_type: str = ""
    query_input_type: str = ""
    input_cost_per_million: float = 0.0
    currency: str = "USD"

    def __post_init__(self) -> None:
        provider = str(self.provider_id or "").strip().casefold()
        model = str(self.model_id or "").strip()
        if not _PROVIDER_ID.fullmatch(provider):
            raise SemanticRetrievalError(
                "Semantic provider ID is invalid.",
                code="SEMANTIC_PROVIDER_CONFIG_INVALID",
                status_code=422,
            )
        if not model or len(model) > 200 or _CONTROL.search(model):
            raise SemanticRetrievalError(
                "Semantic model ID is invalid.",
                code="SEMANTIC_PROVIDER_CONFIG_INVALID",
                status_code=422,
            )
        base = _normalized_base_url(self.base_url)
        # Validate configured endpoints eagerly; this also prevents a second
        # origin from being smuggled into an otherwise trusted provider card.
        _capability_url(base, self.embedding_endpoint, "embeddings")
        _capability_url(base, self.rerank_endpoint, "rerank")
        try:
            rate = float(self.input_cost_per_million or 0.0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise SemanticRetrievalError(
                "Semantic provider cost is invalid.",
                code="SEMANTIC_PROVIDER_CONFIG_INVALID",
                status_code=422,
            ) from exc
        if not math.isfinite(rate) or rate < 0:
            raise SemanticRetrievalError(
                "Semantic provider cost is invalid.",
                code="SEMANTIC_PROVIDER_CONFIG_INVALID",
                status_code=422,
            )
        for label, value in (
            ("document_input_type", self.document_input_type),
            ("query_input_type", self.query_input_type),
        ):
            text = str(value or "").strip()
            if len(text) > 64 or _CONTROL.search(text):
                raise SemanticRetrievalError(
                    f"{label} is invalid.",
                    code="SEMANTIC_PROVIDER_CONFIG_INVALID",
                    status_code=422,
                )
            object.__setattr__(self, label, text)
        object.__setattr__(self, "provider_id", provider)
        object.__setattr__(self, "model_id", model)
        object.__setattr__(self, "base_url", base)
        object.__setattr__(self, "input_cost_per_million", rate)
        object.__setattr__(self, "currency", str(self.currency or "USD").upper()[:8])

    @property
    def model_reference(self) -> str:
        return f"{self.provider_id}::{self.model_id}"

    @property
    def is_loopback(self) -> bool:
        return _is_loopback_host(urlsplit(self.base_url).hostname)

    def endpoint_for(self, capability: str) -> str:
        if capability == "embedding":
            return _capability_url(self.base_url, self.embedding_endpoint, "embeddings")
        if capability == "rerank":
            return _capability_url(self.base_url, self.rerank_endpoint, "rerank")
        raise SemanticRetrievalError(
            "Semantic capability is invalid.",
            code="SEMANTIC_PROVIDER_CONFIG_INVALID",
            status_code=422,
        )

    def identity(self, capability: str, contract_id: str) -> str:
        payload = "\x00".join(
            (
                self.provider_id,
                self.model_id,
                self.endpoint_for(capability),
                str(contract_id),
                self.document_input_type,
                self.query_input_type,
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def semantic_route_from_provider(
    provider: Mapping[str, Any],
    *,
    capability: str,
) -> SemanticProviderRoute:
    """Build a route only when the provider's declared model kind matches.

    This is a configuration helper, not the execution gate.  The client still
    rechecks the authoritative Extension Store state for every project call.
    Endpoint overrides are transport metadata and remain same-origin.
    """

    kind = str(capability or "").strip().casefold()
    if kind not in {"embedding", "rerank"}:
        raise SemanticRetrievalError(
            "Semantic provider capability is invalid.",
            code="SEMANTIC_PROVIDER_CONFIG_INVALID",
            status_code=422,
        )
    model = str(provider.get("selected_model") or "").strip()
    try:
        try:
            from .model_capabilities import model_capability_profile
        except ImportError:  # pragma: no cover - packaged backend entrypoint
            from model_capabilities import model_capability_profile

        profile = model_capability_profile(
            model,
            model_kind=str(provider.get("model_kind") or ""),
            supports_tools=bool(provider.get("supports_tools", False)),
            language_pair=str(provider.get("language_pair") or ""),
        )
    except ValueError as exc:
        raise SemanticRetrievalError(
            "Semantic provider capability metadata is invalid.",
            code="SEMANTIC_PROVIDER_CAPABILITY_INVALID",
            status_code=422,
        ) from exc
    if profile.kind != kind:
        raise SemanticRetrievalError(
            f"Configured model is not a verified {kind} model.",
            code="SEMANTIC_PROVIDER_CAPABILITY_MISMATCH",
            status_code=409,
        )
    return SemanticProviderRoute(
        provider_id=str(provider.get("id") or ""),
        model_id=model,
        base_url=str(provider.get("base_url") or ""),
        embedding_endpoint=str(provider.get("embedding_endpoint") or ""),
        rerank_endpoint=str(provider.get("rerank_endpoint") or ""),
        document_input_type=str(provider.get("document_input_type") or ""),
        query_input_type=str(provider.get("query_input_type") or ""),
        input_cost_per_million=float(provider.get("input_cost_per_million") or 0.0),
        currency=str(provider.get("currency") or "USD"),
    )


class SemanticWireContract(Protocol):
    contract_id: str


class EmbeddingWireContract(SemanticWireContract, Protocol):
    def request_body(
        self,
        route: SemanticProviderRoute,
        texts: Sequence[str],
        *,
        purpose: str,
    ) -> Mapping[str, Any]: ...

    def parse_response(
        self,
        payload: Mapping[str, Any],
        *,
        expected_count: int,
    ) -> Sequence[Sequence[float]]: ...


class RerankWireContract(SemanticWireContract, Protocol):
    def request_body(
        self,
        route: SemanticProviderRoute,
        query: str,
        candidates: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]: ...

    def parse_response(
        self,
        payload: Mapping[str, Any],
        *,
        expected_count: int,
    ) -> Sequence[float]: ...


class OpenAIEmbeddingContract:
    """The common ``input``/``data[].embedding`` JSON contract."""

    contract_id = "openai-embedding-v1"

    def request_body(
        self,
        route: SemanticProviderRoute,
        texts: Sequence[str],
        *,
        purpose: str,
    ) -> Mapping[str, Any]:
        result: dict[str, Any] = {"model": route.model_id, "input": list(texts)}
        input_type = (
            route.document_input_type if purpose == "document" else route.query_input_type
        )
        if input_type:
            result["input_type"] = input_type
        return result

    def parse_response(
        self,
        payload: Mapping[str, Any],
        *,
        expected_count: int,
    ) -> Sequence[Sequence[float]]:
        rows = payload.get("data")
        if not isinstance(rows, list) or len(rows) != expected_count:
            raise SemanticRetrievalError("Embedding provider returned an invalid response.")
        ordered: list[Sequence[float] | None] = [None] * expected_count
        for fallback_index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise SemanticRetrievalError("Embedding provider returned an invalid response.")
            try:
                index = int(row.get("index", fallback_index))
            except (TypeError, ValueError, OverflowError) as exc:
                raise SemanticRetrievalError(
                    "Embedding provider returned an invalid response."
                ) from exc
            vector = row.get("embedding")
            if (
                index < 0
                or index >= expected_count
                or ordered[index] is not None
                or not isinstance(vector, (list, tuple))
            ):
                raise SemanticRetrievalError("Embedding provider returned an invalid response.")
            ordered[index] = vector
        if any(vector is None for vector in ordered):
            raise SemanticRetrievalError("Embedding provider returned an invalid response.")
        return [vector for vector in ordered if vector is not None]


class DocumentsRerankContract:
    """Generic query/documents rerank contract used by multiple providers."""

    contract_id = "documents-rerank-v1"

    def request_body(
        self,
        route: SemanticProviderRoute,
        query: str,
        candidates: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        return {
            "model": route.model_id,
            "query": query,
            "documents": [str(candidate.get("text") or "") for candidate in candidates],
            "top_n": len(candidates),
        }

    def parse_response(
        self,
        payload: Mapping[str, Any],
        *,
        expected_count: int,
    ) -> Sequence[float]:
        rows = payload.get("results", payload.get("data"))
        if not isinstance(rows, list):
            raise SemanticRetrievalError("Rerank provider returned an invalid response.")
        scores: list[float | None] = [None] * expected_count
        for row in rows:
            if not isinstance(row, Mapping):
                raise SemanticRetrievalError("Rerank provider returned an invalid response.")
            try:
                index = int(row.get("index"))
                score = float(row.get("relevance_score", row.get("score")))
            except (TypeError, ValueError, OverflowError) as exc:
                raise SemanticRetrievalError(
                    "Rerank provider returned an invalid response."
                ) from exc
            if (
                index < 0
                or index >= expected_count
                or scores[index] is not None
                or not math.isfinite(score)
            ):
                raise SemanticRetrievalError("Rerank provider returned an invalid response.")
            scores[index] = score
        if any(score is None for score in scores):
            raise SemanticRetrievalError("Rerank provider returned incomplete scores.")
        return [float(score) for score in scores if score is not None]


class PassagesRerankContract:
    """Query-object/passages contract, kept separate from any provider name."""

    contract_id = "passages-rerank-v1"

    def request_body(
        self,
        route: SemanticProviderRoute,
        query: str,
        candidates: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        return {
            "model": route.model_id,
            "query": {"text": query},
            "passages": [
                {"text": str(candidate.get("text") or "")} for candidate in candidates
            ],
            "truncate": "END",
        }

    def parse_response(
        self,
        payload: Mapping[str, Any],
        *,
        expected_count: int,
    ) -> Sequence[float]:
        rows = payload.get("rankings", payload.get("data"))
        if not isinstance(rows, list):
            raise SemanticRetrievalError("Rerank provider returned an invalid response.")
        scores: list[float | None] = [None] * expected_count
        for row in rows:
            if not isinstance(row, Mapping):
                raise SemanticRetrievalError("Rerank provider returned an invalid response.")
            try:
                index = int(row.get("index"))
                score = float(row.get("logit", row.get("score")))
            except (TypeError, ValueError, OverflowError) as exc:
                raise SemanticRetrievalError(
                    "Rerank provider returned an invalid response."
                ) from exc
            if (
                index < 0
                or index >= expected_count
                or scores[index] is not None
                or not math.isfinite(score)
            ):
                raise SemanticRetrievalError("Rerank provider returned an invalid response.")
            scores[index] = score
        if any(score is None for score in scores):
            raise SemanticRetrievalError("Rerank provider returned incomplete scores.")
        return [float(score) for score in scores if score is not None]


class SemanticAccessPolicy(Protocol):
    def authorize(
        self,
        route: SemanticProviderRoute,
        context: SemanticRequestContext,
        *,
        data_type: str,
    ) -> None: ...


class ModelGovernanceSemanticPolicy:
    """Bridge existing project routing consent into semantic operations."""

    def __init__(self, governance: Any) -> None:
        self.governance = governance

    def _audit(
        self,
        action: str,
        route: SemanticProviderRoute,
        context: SemanticRequestContext,
        reason: str,
    ) -> None:
        try:
            self.governance.audit(
                action,
                provider_id=route.provider_id,
                model_id=route.model_id,
                project_id=context.project_id,
                run_id=context.run_id or None,
                detail={"reason": reason, "capability": "semantic_retrieval"},
            )
        except Exception:
            # Audit transport must not make a local route less safe.  Denials
            # still fail closed below and provider calls are never attempted.
            pass

    def authorize(
        self,
        route: SemanticProviderRoute,
        context: SemanticRequestContext,
        *,
        data_type: str,
    ) -> None:
        if data_type not in {"documents", "text"}:
            raise SemanticRetrievalError(
                "Semantic data type is invalid.",
                code="SEMANTIC_CONTEXT_INVALID",
                status_code=422,
            )
        # Loopback calls remain inside the machine. Extension enablement and
        # health/budget checks still run in the provider client.
        if route.is_loopback:
            self._audit("semantic_data_allowed", route, context, "loopback")
            return

        policy = self.governance.get_routing_policy(context.project_id)
        remembered = bool(
            policy.get("mode") == "auto_within_policy"
            and route.provider_id in set(policy.get("allowed_providers") or [])
            and (policy.get("data_consent") or {}).get("documents") is True
        )
        if remembered:
            self._audit("semantic_data_allowed", route, context, "project_policy")
            return

        proposal_id = str(context.consent_proposal_id or "").strip()
        requested = str(context.requested_model or route.model_reference).strip()
        if proposal_id:
            granted = self.governance.proposal_grants_data(
                proposal_id,
                data_type="documents",
                project_id=context.project_id,
                run_id=context.run_id or None,
                requested_model=requested,
                selected_model=route.model_reference,
            )
            if not granted:
                selected = self.governance.consume_proposal(
                    proposal_id,
                    project_id=context.project_id,
                    requested_model=requested,
                    run_id=context.run_id or None,
                )
                granted = bool(
                    selected == route.model_reference
                    and self.governance.proposal_grants_data(
                        proposal_id,
                        data_type="documents",
                        project_id=context.project_id,
                        run_id=context.run_id or None,
                        requested_model=requested,
                        selected_model=route.model_reference,
                    )
                )
            if granted:
                self._audit("semantic_data_allowed", route, context, "one_time_consent")
                return

        self._audit("semantic_data_denied", route, context, "consent_required")
        raise SemanticConsentRequired(
            "將專案文件內容傳送到此模型前，需要取得專案資料同意。",
            provider_id=route.provider_id,
            model_reference=route.model_reference,
        )


def _usage_tokens(payload: Mapping[str, Any]) -> int:
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        return 0
    for key in ("prompt_tokens", "input_tokens", "total_tokens"):
        try:
            value = int(usage.get(key) or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if value > 0:
            return value
    return 0


def _bounded_response_json(response: Any) -> Mapping[str, Any]:
    headers = getattr(response, "headers", None)
    if isinstance(headers, Mapping):
        try:
            declared = int(headers.get("Content-Length") or 0)
        except (TypeError, ValueError, OverflowError):
            declared = 0
        if declared > MAX_PROVIDER_RESPONSE_BYTES:
            raise SemanticRetrievalError(
                "Semantic provider response exceeds the safety limit.",
                code="SEMANTIC_PROVIDER_RESPONSE_TOO_LARGE",
                status_code=502,
            )
    chunks: list[bytes] = []
    total = 0
    iterator = getattr(response, "iter_content", None)
    try:
        if callable(iterator):
            source = iterator(chunk_size=64 * 1024)
        else:
            source = (getattr(response, "content", b"") or b"",)
        for raw_chunk in source:
            if not raw_chunk:
                continue
            chunk = (
                raw_chunk.encode("utf-8")
                if isinstance(raw_chunk, str)
                else bytes(raw_chunk)
            )
            total += len(chunk)
            if total > MAX_PROVIDER_RESPONSE_BYTES:
                raise SemanticRetrievalError(
                    "Semantic provider response exceeds the safety limit.",
                    code="SEMANTIC_PROVIDER_RESPONSE_TOO_LARGE",
                    status_code=502,
                )
            chunks.append(chunk)
    except SemanticRetrievalError:
        raise
    except Exception as exc:
        raise SemanticRetrievalError(
            "Semantic provider response could not be read."
        ) from exc
    raw = b"".join(chunks)
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SemanticRetrievalError("Semantic provider returned invalid JSON.") from exc
    if not isinstance(decoded, Mapping):
        raise SemanticRetrievalError("Semantic provider returned invalid JSON.")
    return decoded


class GovernedSemanticProviderClient:
    """Single no-retry HTTP boundary shared by embedding and reranking."""

    def __init__(
        self,
        route: SemanticProviderRoute,
        *,
        governance: Any,
        access_policy: SemanticAccessPolicy,
        provider_access_check: Callable[[str, str], None],
        secret_resolver: Callable[[str], str],
        session: Any | None = None,
        timeout_seconds: float = 30.0,
        require_verified: bool = True,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise ValueError("timeout_seconds must be between 0 and 60")
        self.route = route
        self.governance = governance
        self.access_policy = access_policy
        self.provider_access_check = provider_access_check
        self.secret_resolver = secret_resolver
        self.session = session or requests.Session()
        self.timeout_seconds = float(timeout_seconds)
        self.require_verified = bool(require_verified)

    def _preflight(
        self,
        context: SemanticRequestContext,
        *,
        capability: str,
        reserve_tokens: int,
    ) -> tuple[str, tuple[Mapping[str, Any], ...]]:
        try:
            self.provider_access_check(self.route.provider_id, context.project_id)
        except SemanticRetrievalError:
            raise
        except Exception as exc:
            raise SemanticRetrievalError(
                "Semantic provider is not enabled for this project.",
                code="SEMANTIC_PROVIDER_DISABLED",
                status_code=403,
            ) from exc
        self.access_policy.authorize(self.route, context, data_type="documents")
        if self.require_verified and not self.route.is_loopback:
            metadata = self.governance.credential_metadata(self.route.provider_id)
            if not metadata.get("last_verified_at"):
                raise SemanticRetrievalError(
                    "Semantic provider must be verified before use.",
                    code="SEMANTIC_PROVIDER_NOT_VERIFIED",
                    status_code=409,
                )
        decision = self.governance.operational_decision(
            self.route.provider_id,
            model_id=self.route.model_id,
            endpoint=self.route.base_url,
        )
        if not decision.allowed:
            raise SemanticRetrievalError(
                decision.message or "Semantic provider is unavailable.",
                code=decision.code or "SEMANTIC_PROVIDER_UNAVAILABLE",
                status_code=409,
                retry_at=decision.retry_at,
            )
        call_id = f"semantic_{uuid.uuid4().hex}"
        run_id = context.run_id or call_id
        reserve_cost = (
            max(0, int(reserve_tokens))
            * self.route.input_cost_per_million
            / 1_000_000
        )
        budget = self.governance.budget_decision(
            project_id=context.project_id,
            run_id=run_id,
            call_id=call_id,
            reserve_tokens=max(0, int(reserve_tokens)),
            reserve_cost=reserve_cost,
            currency=self.route.currency,
            override_id=context.budget_override_id or None,
        )
        if not budget.allowed:
            raise SemanticRetrievalError(
                budget.message or "Semantic provider budget is unavailable.",
                code=budget.code or "MODEL_BUDGET_EXCEEDED",
                status_code=409,
            )
        return call_id, tuple(budget.warnings or ())

    def post_json(
        self,
        context: SemanticRequestContext,
        *,
        capability: str,
        payload: Mapping[str, Any],
        reserve_tokens: int,
        response_parser: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> Any:
        call_id, _warnings = self._preflight(
            context,
            capability=capability,
            reserve_tokens=reserve_tokens,
        )
        endpoint = self.route.endpoint_for(capability)
        try:
            secret = str(self.secret_resolver(self.route.provider_id) or "")
        except Exception as exc:
            secret = ""
            secret_error: Exception | None = exc
        else:
            secret_error = None
        if not secret and not self.route.is_loopback:
            state = self.governance.observe_failure(
                self.route.provider_id,
                model_id=self.route.model_id,
                endpoint=self.route.base_url,
                status_code=401,
                capability=capability,
            )
            self.governance.record_usage(
                call_id=call_id,
                provider_id=self.route.provider_id,
                model_id=self.route.model_id,
                capability=capability,
                project_id=context.project_id,
                run_id=context.run_id or None,
                status="failed",
                provider_signal=str(state.get("state") or "auth_required"),
            )
            raise SemanticRetrievalError(
                "Semantic provider credential is unavailable.",
                code="SEMANTIC_PROVIDER_CREDENTIAL_MISSING",
                status_code=401,
            ) from secret_error
        headers = {"Content-Type": "application/json"}
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        started = time.monotonic()
        response: Any | None = None
        try:
            response = self.session.post(
                endpoint,
                headers=headers,
                json=dict(payload),
                timeout=self.timeout_seconds,
                stream=True,
            )
        except requests.RequestException as exc:
            self.governance.observe_failure(
                self.route.provider_id,
                model_id=self.route.model_id,
                endpoint=self.route.base_url,
                transport_error=True,
                capability=capability,
            )
            self.governance.record_usage(
                call_id=call_id,
                provider_id=self.route.provider_id,
                model_id=self.route.model_id,
                capability=capability,
                project_id=context.project_id,
                run_id=context.run_id or None,
                status="failed",
                latency_ms=int((time.monotonic() - started) * 1000),
                provider_signal="transport_error",
            )
            raise SemanticRetrievalError(
                "Semantic provider could not be reached.",
                code="SEMANTIC_PROVIDER_UNREACHABLE",
                status_code=503,
            ) from exc

        try:
            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code < 200 or status_code >= 300:
                retry_after = (
                    response.headers.get("Retry-After")
                    if isinstance(getattr(response, "headers", None), Mapping)
                    else None
                )
                state = self.governance.observe_failure(
                    self.route.provider_id,
                    model_id=self.route.model_id,
                    endpoint=self.route.base_url,
                    status_code=status_code,
                    retry_after=retry_after,
                    capability=capability,
                )
                self.governance.record_usage(
                    call_id=call_id,
                    provider_id=self.route.provider_id,
                    model_id=self.route.model_id,
                    capability=capability,
                    project_id=context.project_id,
                    run_id=context.run_id or None,
                    status="failed",
                    latency_ms=int((time.monotonic() - started) * 1000),
                    provider_signal=str(state.get("state") or f"http_{status_code}"),
                    retry_at=state.get("retry_at"),
                )
                raise SemanticRetrievalError(
                    f"Semantic provider rejected the request (HTTP {status_code}).",
                    code=f"SEMANTIC_PROVIDER_HTTP_{status_code}",
                    status_code=502,
                    retry_at=state.get("retry_at"),
                )
            decoded = _bounded_response_json(response)
            try:
                parsed_result = response_parser(decoded) if response_parser else decoded
            except SemanticRetrievalError:
                raise
            except Exception as exc:
                raise SemanticRetrievalError(
                    "Semantic provider returned an invalid response."
                ) from exc
            used_tokens = _usage_tokens(decoded)
            self.governance.observe_success(
                self.route.provider_id,
                model_id=self.route.model_id,
                endpoint=self.route.base_url,
            )
            self.governance.record_usage(
                call_id=call_id,
                provider_id=self.route.provider_id,
                model_id=self.route.model_id,
                capability=capability,
                project_id=context.project_id,
                run_id=context.run_id or None,
                prompt_tokens=used_tokens,
                latency_ms=int((time.monotonic() - started) * 1000),
                estimated_cost=(
                    used_tokens * self.route.input_cost_per_million / 1_000_000
                ),
                currency=self.route.currency,
            )
            return parsed_result
        except SemanticRetrievalError:
            # A malformed successful response is a provider failure as well.
            # If usage has already been reconciled, record_usage is idempotent.
            if 200 <= int(getattr(response, "status_code", 0) or 0) < 300:
                self.governance.observe_failure(
                    self.route.provider_id,
                    model_id=self.route.model_id,
                    endpoint=self.route.base_url,
                    status_code=502,
                    capability=capability,
                )
                self.governance.record_usage(
                    call_id=call_id,
                    provider_id=self.route.provider_id,
                    model_id=self.route.model_id,
                    capability=capability,
                    project_id=context.project_id,
                    run_id=context.run_id or None,
                    status="failed",
                    latency_ms=int((time.monotonic() - started) * 1000),
                    provider_signal="invalid_response",
                )
            raise
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()


class GovernedProviderEmbeddingAdapter:
    def __init__(
        self,
        client: GovernedSemanticProviderClient,
        *,
        contract: EmbeddingWireContract | None = None,
    ) -> None:
        self.client = client
        self.contract = contract or OpenAIEmbeddingContract()
        self.adapter_id = (
            f"provider-embedding-{client.route.identity('embedding', self.contract.contract_id)}"
        )

    def embed_for_project(
        self,
        texts: Sequence[str],
        *,
        purpose: str,
        context: SemanticRequestContext,
    ) -> Sequence[Sequence[float]]:
        body = self.contract.request_body(self.client.route, texts, purpose=purpose)
        reserve = max(1, sum(len(str(text)) for text in texts) // 4)
        response = self.client.post_json(
            context,
            capability="embedding",
            payload=body,
            reserve_tokens=reserve,
            response_parser=lambda payload: self.contract.parse_response(
                payload, expected_count=len(texts)
            ),
        )
        return response


class GovernedProviderRerankerAdapter:
    def __init__(
        self,
        client: GovernedSemanticProviderClient,
        *,
        contract: RerankWireContract | None = None,
    ) -> None:
        self.client = client
        self.contract = contract or DocumentsRerankContract()
        self.adapter_id = (
            f"provider-rerank-{client.route.identity('rerank', self.contract.contract_id)}"
        )

    def rerank_for_project(
        self,
        query: str,
        candidates: Sequence[Mapping[str, Any]],
        *,
        context: SemanticRequestContext,
    ) -> Sequence[float]:
        body = self.contract.request_body(self.client.route, query, candidates)
        reserve = max(
            1,
            (
                len(query)
                + sum(len(str(candidate.get("text") or "")) for candidate in candidates)
            )
            // 4,
        )
        response = self.client.post_json(
            context,
            capability="rerank",
            payload=body,
            reserve_tokens=reserve,
            response_parser=lambda payload: self.contract.parse_response(
                payload, expected_count=len(candidates)
            ),
        )
        return response


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return True
    return bool(
        stat.S_ISLNK(info.st_mode)
        or int(getattr(info, "st_file_attributes", 0) or 0)
        & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
    )


def _trusted_local_model_path(value: str | Path) -> tuple[Path, str]:
    configured = Path(value).expanduser()
    if not configured.is_absolute() or not configured.exists() or not configured.is_dir():
        raise SemanticRetrievalError(
            "Local semantic model must be an existing absolute directory.",
            code="LOCAL_SEMANTIC_MODEL_INVALID",
            status_code=422,
        )
    if configured.resolve() != configured.absolute() or _is_link_or_reparse(configured):
        raise SemanticRetrievalError(
            "Linked or reparse-point semantic model paths are not permitted.",
            code="LOCAL_SEMANTIC_MODEL_UNTRUSTED",
            status_code=422,
        )
    manifest: list[str] = []
    count = 0
    for root, directories, files in os.walk(configured, followlinks=False):
        root_path = Path(root)
        if _is_link_or_reparse(root_path):
            raise SemanticRetrievalError(
                "Linked or reparse-point semantic model paths are not permitted.",
                code="LOCAL_SEMANTIC_MODEL_UNTRUSTED",
                status_code=422,
            )
        for name in [*directories, *files]:
            candidate = root_path / name
            count += 1
            if count > MAX_LOCAL_MODEL_ENTRIES or _is_link_or_reparse(candidate):
                raise SemanticRetrievalError(
                    "Local semantic model tree is unsafe or too large.",
                    code="LOCAL_SEMANTIC_MODEL_UNTRUSTED",
                    status_code=422,
                )
            info = candidate.lstat()
            relative = candidate.relative_to(configured).as_posix()
            manifest.append(f"{relative}\x00{info.st_size}\x00{info.st_mtime_ns}")
    digest = hashlib.sha256("\n".join(sorted(manifest)).encode("utf-8")).hexdigest()[:24]
    return configured, digest


class LocalSentenceTransformerEmbeddingAdapter:
    """Offline semantic embeddings; never resolves a Hub model name."""

    def __init__(
        self,
        model_path: str | Path,
        *,
        device: str = "cpu",
        batch_size: int = 32,
        model: Any | None = None,
    ) -> None:
        self.model_path, digest = _trusted_local_model_path(model_path)
        if batch_size < 1 or batch_size > 64:
            raise ValueError("batch_size must be between 1 and 64")
        self.device = str(device or "cpu")[:32]
        self.batch_size = int(batch_size)
        self.adapter_id = f"local-sentence-transformer-{digest}"
        self._model = model
        self._lock = threading.RLock()

    def _load(self) -> Any:
        with self._lock:
            if self._model is None:
                try:
                    from sentence_transformers import SentenceTransformer

                    self._model = SentenceTransformer(
                        str(self.model_path),
                        device=self.device,
                        trust_remote_code=False,
                        local_files_only=True,
                    )
                except Exception as exc:
                    raise SemanticRetrievalError(
                        "Local embedding model could not be loaded.",
                        code="LOCAL_EMBEDDING_UNAVAILABLE",
                        status_code=503,
                    ) from exc
            return self._model

    def embed(self, texts: Sequence[str], *, purpose: str) -> Sequence[Sequence[float]]:
        if purpose not in {"document", "query"}:
            raise ValueError("purpose must be document or query")
        try:
            raw = self._load().encode(
                list(texts),
                batch_size=min(self.batch_size, max(1, len(texts))),
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            return raw.tolist() if hasattr(raw, "tolist") else list(raw)
        except SemanticRetrievalError:
            raise
        except Exception as exc:
            raise SemanticRetrievalError(
                "Local embedding inference failed.",
                code="LOCAL_EMBEDDING_FAILED",
                status_code=503,
            ) from exc


class LocalCrossEncoderRerankerAdapter:
    """Offline cross-encoder reranking from an already installed model."""

    def __init__(
        self,
        model_path: str | Path,
        *,
        device: str = "cpu",
        batch_size: int = 16,
        model: Any | None = None,
    ) -> None:
        self.model_path, digest = _trusted_local_model_path(model_path)
        if batch_size < 1 or batch_size > 64:
            raise ValueError("batch_size must be between 1 and 64")
        self.device = str(device or "cpu")[:32]
        self.batch_size = int(batch_size)
        self.adapter_id = f"local-cross-encoder-{digest}"
        self._model = model
        self._lock = threading.RLock()

    def _load(self) -> Any:
        with self._lock:
            if self._model is None:
                try:
                    from sentence_transformers import CrossEncoder

                    self._model = CrossEncoder(
                        str(self.model_path),
                        device=self.device,
                        trust_remote_code=False,
                        local_files_only=True,
                    )
                except Exception as exc:
                    raise SemanticRetrievalError(
                        "Local reranker model could not be loaded.",
                        code="LOCAL_RERANKER_UNAVAILABLE",
                        status_code=503,
                    ) from exc
            return self._model

    def rerank(
        self,
        query: str,
        candidates: Sequence[Mapping[str, Any]],
    ) -> Sequence[float]:
        pairs = [(query, str(candidate.get("text") or "")) for candidate in candidates]
        try:
            raw = self._load().predict(
                pairs,
                batch_size=min(self.batch_size, max(1, len(pairs))),
                show_progress_bar=False,
            )
            values = raw.tolist() if hasattr(raw, "tolist") else list(raw)
            if any(isinstance(value, (list, tuple)) for value in values):
                raise ValueError("cross encoder returned multi-class scores")
            return [float(value) for value in values]
        except SemanticRetrievalError:
            raise
        except Exception as exc:
            raise SemanticRetrievalError(
                "Local reranking inference failed.",
                code="LOCAL_RERANKER_FAILED",
                status_code=503,
            ) from exc


__all__ = [
    "DocumentsRerankContract",
    "EmbeddingWireContract",
    "GovernedProviderEmbeddingAdapter",
    "GovernedProviderRerankerAdapter",
    "GovernedSemanticProviderClient",
    "LocalCrossEncoderRerankerAdapter",
    "LocalSentenceTransformerEmbeddingAdapter",
    "MAX_PROVIDER_RESPONSE_BYTES",
    "ModelGovernanceSemanticPolicy",
    "OpenAIEmbeddingContract",
    "PassagesRerankContract",
    "RerankWireContract",
    "SemanticAccessPolicy",
    "SemanticConsentRequired",
    "SemanticProviderRoute",
    "SemanticRequestContext",
    "SemanticRetrievalError",
    "semantic_route_from_provider",
]
