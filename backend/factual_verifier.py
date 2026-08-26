"""Provider-neutral, fail-safe factual verification primitives.

The verifier is deliberately separate from the chat runtime.  It accepts an
answer plus a host-authorized evidence bundle, extracts atomic claims, and asks
an interchangeable entailment adapter to classify only the evidence explicitly
cited by each claim.  Adapter output is never allowed to widen the evidence
scope or relax the host gate.

This module does not claim that lexical matching is semantic verification.  The
deterministic adapters are conservative offline fixtures and a safe fallback;
production deployments can provide governed model adapters through the same
typed contracts.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence


MAX_ANSWER_CHARS = 64 * 1024
MAX_CLAIMS = 32
MAX_CLAIM_CHARS = 2 * 1024
MAX_EVIDENCE_ITEMS = 64
MAX_EVIDENCE_TEXT_BYTES = 128 * 1024
MAX_EVIDENCE_PER_CLAIM = 12
MAX_PUBLIC_LABEL_CHARS = 512
MAX_ADAPTER_ID_CHARS = 128

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CITATION_MARKER = re.compile(
    r"\[\s*evidence\s*:\s*([A-Za-z0-9][A-Za-z0-9._:-]{0,159})\s*\]",
    re.IGNORECASE,
)
_SENTENCE_BOUNDARY = re.compile(
    r"(?<=[。！？!?；;])(?!\s*\[\s*evidence\s*:)(?:\s+|(?=\S))|\n+",
    re.IGNORECASE,
)
_SPACE = re.compile(r"\s+")


class FactualVerificationError(ValueError):
    """Invalid host input or a malformed typed adapter contract."""

    code = "FACTUAL_VERIFICATION_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class ClaimKind(str, Enum):
    FACTUAL = "factual"
    NON_FACTUAL = "non_factual"


class ExtractionStatus(str, Enum):
    COMPLETE = "complete"
    UNKNOWN = "unknown"


class EntailmentLabel(str, Enum):
    ENTAILED = "entailed"
    CONTRADICTED = "contradicted"
    INSUFFICIENT = "insufficient"
    UNKNOWN = "unknown"


class ClaimVerificationStatus(str, Enum):
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class AnswerVerificationStatus(str, Enum):
    VERIFIED = "verified"
    FAILED = "failed"
    UNKNOWN = "unknown"


def _clean_text(value: Any, *, label: str, maximum: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text or len(text) > maximum or _CONTROL.search(text):
        raise FactualVerificationError(
            f"{label} must contain 1-{maximum} safe characters.",
            code="INVALID_VERIFICATION_INPUT",
        )
    return text


def _identifier(value: Any, *, label: str) -> str:
    result = str(value or "").strip()
    if not _IDENTIFIER.fullmatch(result):
        raise FactualVerificationError(
            f"{label} is invalid.", code="INVALID_VERIFICATION_INPUT"
        )
    return result


def _safe_label(value: Any) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = "".join(character for character in text if ord(character) >= 32)
    return text[:MAX_PUBLIC_LABEL_CHARS]


def _digest_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class EvidenceRecord:
    """One ephemeral evidence snippet with durable citation provenance."""

    evidence_id: str
    text: str = field(repr=False)
    kind: str = "project_knowledge"
    project_id: str | None = None
    citation: Mapping[str, Any] = field(default_factory=dict)
    text_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        evidence_id = _identifier(self.evidence_id, label="Evidence ID")
        text = _clean_text(
            self.text,
            label="Evidence text",
            maximum=MAX_EVIDENCE_TEXT_BYTES,
        )
        if len(text.encode("utf-8")) > MAX_EVIDENCE_TEXT_BYTES:
            raise FactualVerificationError(
                "Evidence text exceeds the byte limit.",
                code="VERIFICATION_EVIDENCE_TOO_LARGE",
            )
        kind = _identifier(self.kind, label="Evidence kind")
        project_id = (
            _identifier(self.project_id, label="Project ID")
            if self.project_id is not None
            else None
        )
        raw_citation = self.citation if isinstance(self.citation, Mapping) else {}
        citation: dict[str, Any] = {}
        for key in (
            "source_id",
            "title",
            "document_id",
            "chunk_id",
            "locator",
        ):
            label = _safe_label(raw_citation.get(key))
            if label:
                citation[key] = label
        for key in ("ordinal", "start_offset", "end_offset"):
            try:
                if raw_citation.get(key) is not None:
                    citation[key] = max(0, int(raw_citation.get(key)))
            except (TypeError, ValueError):
                continue
        for key in ("document_sha256", "chunk_sha256"):
            digest = str(raw_citation.get(key) or "").strip().casefold()
            if _SHA256.fullmatch(digest):
                citation[key] = digest
        citation_project = str(raw_citation.get("project_id") or "").strip()
        if citation_project and project_id is None:
            project_id = _identifier(citation_project, label="Citation project ID")
        if project_id is not None:
            if citation_project and citation_project != project_id:
                raise FactualVerificationError(
                    "Evidence citation belongs to another project.",
                    code="VERIFICATION_EVIDENCE_SCOPE_MISMATCH",
                )
            citation["project_id"] = project_id

        object.__setattr__(self, "evidence_id", evidence_id)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "citation", citation)
        object.__setattr__(
            self, "text_sha256", hashlib.sha256(text.encode("utf-8")).hexdigest()
        )

    def public_reference(self) -> dict[str, Any]:
        """Return provenance without exposing the evidence snippet."""

        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "project_id": self.project_id,
            "text_sha256": self.text_sha256,
            "citation": dict(self.citation),
        }


@dataclass(frozen=True)
class EvidenceBundle:
    """A host-authorized, single-project evidence snapshot."""

    records: tuple[EvidenceRecord, ...]
    project_id: str | None = None
    snapshot_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        records = tuple(self.records)
        if len(records) > MAX_EVIDENCE_ITEMS:
            raise FactualVerificationError(
                "Evidence item limit exceeded.",
                code="VERIFICATION_EVIDENCE_TOO_LARGE",
            )
        project_id = (
            _identifier(self.project_id, label="Project ID")
            if self.project_id is not None
            else None
        )
        seen: set[str] = set()
        total_bytes = 0
        record_projects: set[str] = set()
        for record in records:
            if not isinstance(record, EvidenceRecord):
                raise FactualVerificationError(
                    "Evidence bundle contains an invalid record.",
                    code="INVALID_VERIFICATION_INPUT",
                )
            if record.evidence_id in seen:
                raise FactualVerificationError(
                    "Evidence IDs must be unique.",
                    code="INVALID_VERIFICATION_INPUT",
                )
            seen.add(record.evidence_id)
            total_bytes += len(record.text.encode("utf-8"))
            if record.project_id is not None:
                record_projects.add(record.project_id)
            if project_id is not None and record.project_id != project_id:
                raise FactualVerificationError(
                    "Evidence record belongs to another project.",
                    code="VERIFICATION_EVIDENCE_SCOPE_MISMATCH",
                )
        if project_id is None and len(record_projects) > 1:
            raise FactualVerificationError(
                "Evidence bundle cannot mix projects.",
                code="VERIFICATION_EVIDENCE_SCOPE_MISMATCH",
            )
        if project_id is None and record_projects:
            project_id = next(iter(record_projects))
        if total_bytes > MAX_EVIDENCE_TEXT_BYTES:
            raise FactualVerificationError(
                "Evidence bundle exceeds the byte limit.",
                code="VERIFICATION_EVIDENCE_TOO_LARGE",
            )
        public_manifest = [record.public_reference() for record in records]
        object.__setattr__(self, "records", records)
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "snapshot_sha256", _digest_json(public_manifest))

    def by_id(self) -> dict[str, EvidenceRecord]:
        return {record.evidence_id: record for record in self.records}


@dataclass(frozen=True)
class ClaimDraft:
    """Provider-neutral claim extraction output before host ID assignment."""

    text: str
    kind: ClaimKind | str = ClaimKind.FACTUAL
    cited_evidence_ids: tuple[str, ...] = ()
    source_text: str | None = None

    def __post_init__(self) -> None:
        text = _clean_text(self.text, label="Claim", maximum=MAX_CLAIM_CHARS)
        try:
            kind = self.kind if isinstance(self.kind, ClaimKind) else ClaimKind(str(self.kind))
        except ValueError as exc:
            raise FactualVerificationError(
                "Claim kind is invalid.", code="VERIFIER_OUTPUT_INVALID"
            ) from exc
        evidence_ids: list[str] = []
        for value in self.cited_evidence_ids:
            evidence_id = _identifier(value, label="Cited evidence ID")
            if evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)
        if len(evidence_ids) > MAX_EVIDENCE_PER_CLAIM:
            raise FactualVerificationError(
                "Claim citation limit exceeded.", code="VERIFIER_OUTPUT_INVALID"
            )
        source_text = _clean_text(
            self.source_text if self.source_text is not None else text,
            label="Claim source text",
            maximum=MAX_CLAIM_CHARS,
        )
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "cited_evidence_ids", tuple(evidence_ids))
        object.__setattr__(self, "source_text", source_text)


@dataclass(frozen=True)
class ClaimExtraction:
    status: ExtractionStatus | str
    claims: tuple[ClaimDraft, ...] = ()
    complete: bool = True

    def __post_init__(self) -> None:
        try:
            status = (
                self.status
                if isinstance(self.status, ExtractionStatus)
                else ExtractionStatus(str(self.status))
            )
        except ValueError as exc:
            raise FactualVerificationError(
                "Claim extraction status is invalid.", code="VERIFIER_OUTPUT_INVALID"
            ) from exc
        claims = tuple(self.claims)
        if len(claims) > MAX_CLAIMS or any(
            not isinstance(claim, ClaimDraft) for claim in claims
        ):
            raise FactualVerificationError(
                "Claim extraction output is invalid or truncated.",
                code="VERIFIER_OUTPUT_INVALID",
            )
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "claims", claims)
        object.__setattr__(self, "complete", bool(self.complete))


@dataclass(frozen=True)
class EntailmentRequest:
    claim_id: str
    claim_text: str
    evidence: tuple[EvidenceRecord, ...]


@dataclass(frozen=True)
class EntailmentDecision:
    claim_id: str
    label: EntailmentLabel | str
    evidence_ids: tuple[str, ...] = ()
    confidence: float | None = None

    def __post_init__(self) -> None:
        claim_id = _identifier(self.claim_id, label="Claim ID")
        try:
            label = (
                self.label
                if isinstance(self.label, EntailmentLabel)
                else EntailmentLabel(str(self.label))
            )
        except ValueError as exc:
            raise FactualVerificationError(
                "Entailment label is invalid.", code="VERIFIER_OUTPUT_INVALID"
            ) from exc
        evidence_ids: list[str] = []
        for value in self.evidence_ids:
            evidence_id = _identifier(value, label="Entailment evidence ID")
            if evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)
        if len(evidence_ids) > MAX_EVIDENCE_PER_CLAIM:
            raise FactualVerificationError(
                "Entailment citation limit exceeded.", code="VERIFIER_OUTPUT_INVALID"
            )
        confidence = self.confidence
        if confidence is not None:
            try:
                confidence = float(confidence)
            except (TypeError, ValueError) as exc:
                raise FactualVerificationError(
                    "Entailment confidence is invalid.", code="VERIFIER_OUTPUT_INVALID"
                ) from exc
            if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
                raise FactualVerificationError(
                    "Entailment confidence is invalid.", code="VERIFIER_OUTPUT_INVALID"
                )
        object.__setattr__(self, "claim_id", claim_id)
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "evidence_ids", tuple(evidence_ids))
        object.__setattr__(self, "confidence", confidence)


class ClaimExtractionAdapter(Protocol):
    adapter_id: str

    def extract(
        self,
        answer: str,
        *,
        allowed_evidence_ids: Sequence[str],
        max_claims: int,
    ) -> ClaimExtraction:
        """Extract bounded claims without adding evidence outside the allowlist."""


class EntailmentAdapter(Protocol):
    adapter_id: str

    def evaluate(
        self, requests: Sequence[EntailmentRequest]
    ) -> Sequence[EntailmentDecision]:
        """Classify the exact, host-scoped evidence supplied in each request."""


@dataclass(frozen=True)
class VerificationPolicy:
    """Host-owned limits; security outcomes are intentionally not configurable."""

    max_claims: int = 24
    adapter_timeout_seconds: float = 8.0
    require_explicit_citations: bool = field(default=True, init=False)
    unknown_blocks: bool = field(default=True, init=False)
    unsupported_blocks: bool = field(default=True, init=False)

    def __post_init__(self) -> None:
        if not 1 <= int(self.max_claims) <= MAX_CLAIMS:
            raise ValueError(f"max_claims must be between 1 and {MAX_CLAIMS}")
        timeout = float(self.adapter_timeout_seconds)
        if not 0.05 <= timeout <= 30.0:
            raise ValueError("adapter_timeout_seconds must be between 0.05 and 30")
        object.__setattr__(self, "max_claims", int(self.max_claims))
        object.__setattr__(self, "adapter_timeout_seconds", timeout)


@dataclass(frozen=True)
class ClaimVerification:
    claim_id: str
    text: str
    kind: ClaimKind
    status: ClaimVerificationStatus
    code: str
    evidence: tuple[Mapping[str, Any], ...] = ()
    confidence: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "text": self.text,
            "kind": self.kind.value,
            "status": self.status.value,
            "code": self.code,
            "evidence": [dict(reference) for reference in self.evidence],
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class AnswerVerificationReport:
    status: AnswerVerificationStatus
    code: str
    answer_sha256: str
    evidence_snapshot_sha256: str
    extractor_id: str
    entailment_adapter_id: str
    claims: tuple[ClaimVerification, ...] = ()

    @property
    def gate_passed(self) -> bool:
        """Fixed host gate: only a fully verified answer can pass."""

        return self.status is AnswerVerificationStatus.VERIFIED

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "code": self.code,
            "gate_passed": self.gate_passed,
            "answer_sha256": self.answer_sha256,
            "evidence_snapshot_sha256": self.evidence_snapshot_sha256,
            "extractor_id": self.extractor_id,
            "entailment_adapter_id": self.entailment_adapter_id,
            "claims": [claim.as_dict() for claim in self.claims],
        }


class DeterministicClaimExtractor:
    """Conservative offline extractor using explicit ``[evidence:ID]`` markers."""

    adapter_id = "deterministic-claim-extractor-v1"

    def extract(
        self,
        answer: str,
        *,
        allowed_evidence_ids: Sequence[str],
        max_claims: int,
    ) -> ClaimExtraction:
        del allowed_evidence_ids  # Host validates citations after extraction.
        parts = [part.strip(" \t-*•") for part in _SENTENCE_BOUNDARY.split(answer)]
        claims: list[ClaimDraft] = []
        for part in parts:
            if not part:
                continue
            citations = tuple(match.group(1) for match in _CITATION_MARKER.finditer(part))
            clean = _CITATION_MARKER.sub("", part).strip()
            if not clean:
                continue
            question = clean.endswith(("?", "？")) or clean.startswith(
                ("請問", "是否", "為何", "如何")
            )
            claims.append(
                ClaimDraft(
                    text=clean,
                    kind=ClaimKind.NON_FACTUAL if question else ClaimKind.FACTUAL,
                    cited_evidence_ids=citations,
                    source_text=clean,
                )
            )
            if len(claims) > max_claims:
                return ClaimExtraction(
                    ExtractionStatus.UNKNOWN,
                    tuple(claims[:max_claims]),
                    complete=False,
                )
        return ClaimExtraction(ExtractionStatus.COMPLETE, tuple(claims), complete=True)


class ConservativeExactEntailment:
    """Offline baseline that entails only normalized exact textual support."""

    adapter_id = "conservative-exact-entailment-v1"

    @staticmethod
    def _normalize(value: str) -> str:
        return _SPACE.sub(" ", value).strip().casefold().rstrip("。.!！")

    def evaluate(
        self, requests: Sequence[EntailmentRequest]
    ) -> tuple[EntailmentDecision, ...]:
        decisions: list[EntailmentDecision] = []
        for request in requests:
            claim = self._normalize(request.claim_text)
            supporting = tuple(
                record.evidence_id
                for record in request.evidence
                if claim and claim in self._normalize(record.text)
            )
            decisions.append(
                EntailmentDecision(
                    claim_id=request.claim_id,
                    label=(
                        EntailmentLabel.ENTAILED
                        if supporting
                        else EntailmentLabel.INSUFFICIENT
                    ),
                    evidence_ids=supporting,
                    confidence=1.0 if supporting else None,
                )
            )
        return tuple(decisions)


async def _invoke_adapter(method: Any, *args: Any, timeout: float, **kwargs: Any) -> Any:
    async def invoke() -> Any:
        if inspect.iscoroutinefunction(method):
            return await method(*args, **kwargs)
        result = await asyncio.to_thread(method, *args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    return await asyncio.wait_for(invoke(), timeout=timeout)


def _adapter_id(adapter: Any, fallback: str) -> str:
    value = str(getattr(adapter, "adapter_id", "") or "").strip()
    if not value or len(value) > MAX_ADAPTER_ID_CHARS or not _IDENTIFIER.fullmatch(value):
        return fallback
    return value


class AnswerFactVerifier:
    """Run extraction and entailment under a non-relaxable host policy."""

    def __init__(
        self,
        *,
        extractor: ClaimExtractionAdapter | None = None,
        entailment: EntailmentAdapter | None = None,
        policy: VerificationPolicy | None = None,
    ) -> None:
        self.extractor = extractor or DeterministicClaimExtractor()
        self.entailment = entailment or ConservativeExactEntailment()
        self.policy = policy or VerificationPolicy()

    def _unknown_report(
        self,
        *,
        code: str,
        answer_sha256: str,
        evidence: EvidenceBundle,
        claims: Sequence[ClaimVerification] = (),
    ) -> AnswerVerificationReport:
        return AnswerVerificationReport(
            status=AnswerVerificationStatus.UNKNOWN,
            code=code,
            answer_sha256=answer_sha256,
            evidence_snapshot_sha256=evidence.snapshot_sha256,
            extractor_id=_adapter_id(self.extractor, "invalid-extractor"),
            entailment_adapter_id=_adapter_id(self.entailment, "invalid-entailment"),
            claims=tuple(claims),
        )

    async def verify(
        self, *, answer: str, evidence: EvidenceBundle
    ) -> AnswerVerificationReport:
        """Verify one answer; every uncertain path returns a blocking report."""

        if not isinstance(evidence, EvidenceBundle):
            raise FactualVerificationError(
                "A typed evidence bundle is required.",
                code="INVALID_VERIFICATION_INPUT",
            )
        answer_text = str(answer or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        answer_sha256 = hashlib.sha256(answer_text.encode("utf-8")).hexdigest()
        if _adapter_id(self.extractor, "") == "" or _adapter_id(self.entailment, "") == "":
            return self._unknown_report(
                code="VERIFIER_ADAPTER_ID_INVALID",
                answer_sha256=answer_sha256,
                evidence=evidence,
            )
        if (
            not answer_text
            or len(answer_text) > MAX_ANSWER_CHARS
            or _CONTROL.search(answer_text)
        ):
            return self._unknown_report(
                code="VERIFICATION_ANSWER_INVALID",
                answer_sha256=answer_sha256,
                evidence=evidence,
            )

        try:
            extraction = await _invoke_adapter(
                self.extractor.extract,
                answer_text,
                allowed_evidence_ids=tuple(record.evidence_id for record in evidence.records),
                max_claims=self.policy.max_claims,
                timeout=self.policy.adapter_timeout_seconds,
            )
            if not isinstance(extraction, ClaimExtraction):
                raise FactualVerificationError(
                    "Claim extractor returned the wrong type.",
                    code="VERIFIER_OUTPUT_INVALID",
                )
        except asyncio.TimeoutError:
            return self._unknown_report(
                code="VERIFIER_EXTRACTION_TIMEOUT",
                answer_sha256=answer_sha256,
                evidence=evidence,
            )
        except Exception:
            return self._unknown_report(
                code="VERIFIER_EXTRACTION_FAILED",
                answer_sha256=answer_sha256,
                evidence=evidence,
            )

        if (
            extraction.status is not ExtractionStatus.COMPLETE
            or not extraction.complete
            or not extraction.claims
        ):
            return self._unknown_report(
                code="VERIFIER_EXTRACTION_INCOMPLETE",
                answer_sha256=answer_sha256,
                evidence=evidence,
            )

        # Bind every adapter-produced claim to an exact, answer-originating
        # sentence.  Then add conservative host envelope claims for uncovered
        # answer sentences.  This prevents an extractor from silently omitting
        # a difficult statement and verifying only an easier subset.
        answer_segments: list[tuple[str, tuple[str, ...]]] = []
        for raw_segment in _SENTENCE_BOUNDARY.split(answer_text):
            segment = raw_segment.strip(" \t-*•")
            if not segment:
                continue
            citations = tuple(
                match.group(1) for match in _CITATION_MARKER.finditer(segment)
            )
            clean = _CITATION_MARKER.sub("", segment).strip()
            if clean:
                answer_segments.append((clean, citations))
        segment_by_normalized = {
            _SPACE.sub(" ", text).casefold(): (text, citations)
            for text, citations in answer_segments
        }
        claims = list(extraction.claims)
        covered: set[str] = set()
        for draft in claims:
            normalized_source = _SPACE.sub(" ", str(draft.source_text)).casefold()
            if normalized_source not in segment_by_normalized:
                return self._unknown_report(
                    code="VERIFIER_CLAIM_SCOPE_VIOLATION",
                    answer_sha256=answer_sha256,
                    evidence=evidence,
                )
            covered.add(normalized_source)
        for normalized_source, (source_text, citations) in segment_by_normalized.items():
            if normalized_source not in covered:
                claims.append(
                    ClaimDraft(
                        text=source_text,
                        kind=ClaimKind.FACTUAL,
                        cited_evidence_ids=citations,
                        source_text=source_text,
                    )
                )
        if len(claims) > self.policy.max_claims:
            return self._unknown_report(
                code="VERIFIER_CLAIM_COVERAGE_INCOMPLETE",
                answer_sha256=answer_sha256,
                evidence=evidence,
            )

        evidence_by_id = evidence.by_id()
        preliminary: dict[str, ClaimVerification] = {}
        requests: list[EntailmentRequest] = []
        drafts_by_id: dict[str, ClaimDraft] = {}
        factual_count = 0
        for index, draft in enumerate(claims, start=1):
            claim_id = f"claim_{index:04d}"
            drafts_by_id[claim_id] = draft
            if draft.kind is ClaimKind.NON_FACTUAL:
                preliminary[claim_id] = ClaimVerification(
                    claim_id,
                    draft.text,
                    draft.kind,
                    ClaimVerificationStatus.NOT_APPLICABLE,
                    "CLAIM_NON_FACTUAL",
                )
                continue
            factual_count += 1
            if not draft.cited_evidence_ids:
                preliminary[claim_id] = ClaimVerification(
                    claim_id,
                    draft.text,
                    draft.kind,
                    ClaimVerificationStatus.UNSUPPORTED,
                    "CLAIM_CITATION_REQUIRED",
                )
                continue
            unknown = [
                evidence_id
                for evidence_id in draft.cited_evidence_ids
                if evidence_id not in evidence_by_id
            ]
            if unknown:
                preliminary[claim_id] = ClaimVerification(
                    claim_id,
                    draft.text,
                    draft.kind,
                    ClaimVerificationStatus.UNSUPPORTED,
                    "CLAIM_CITATION_NOT_AVAILABLE",
                )
                continue
            scoped = tuple(evidence_by_id[item] for item in draft.cited_evidence_ids)
            requests.append(EntailmentRequest(claim_id, draft.text, scoped))

        if factual_count == 0:
            ordered = tuple(
                preliminary[f"claim_{index:04d}"]
                for index in range(1, len(claims) + 1)
            )
            return self._unknown_report(
                code="VERIFIER_NO_FACTUAL_CLAIMS",
                answer_sha256=answer_sha256,
                evidence=evidence,
                claims=ordered,
            )

        decisions: dict[str, EntailmentDecision] = {}
        if requests:
            try:
                raw_decisions = await _invoke_adapter(
                    self.entailment.evaluate,
                    tuple(requests),
                    timeout=self.policy.adapter_timeout_seconds,
                )
                typed = tuple(raw_decisions)
                if len(typed) != len(requests) or any(
                    not isinstance(decision, EntailmentDecision) for decision in typed
                ):
                    raise FactualVerificationError(
                        "Entailment adapter returned the wrong result count or type.",
                        code="VERIFIER_OUTPUT_INVALID",
                    )
                for request, decision in zip(requests, typed):
                    if decision.claim_id != request.claim_id or decision.claim_id in decisions:
                        raise FactualVerificationError(
                            "Entailment adapter changed claim identity.",
                            code="VERIFIER_SCOPE_VIOLATION",
                        )
                    allowed = {record.evidence_id for record in request.evidence}
                    if any(item not in allowed for item in decision.evidence_ids):
                        raise FactualVerificationError(
                            "Entailment adapter cited evidence outside the host scope.",
                            code="VERIFIER_SCOPE_VIOLATION",
                        )
                    if decision.label in {
                        EntailmentLabel.ENTAILED,
                        EntailmentLabel.CONTRADICTED,
                    } and not decision.evidence_ids:
                        raise FactualVerificationError(
                            "Decisive entailment output requires scoped evidence.",
                            code="VERIFIER_OUTPUT_INVALID",
                        )
                    decisions[decision.claim_id] = decision
            except asyncio.TimeoutError:
                return self._unknown_report(
                    code="VERIFIER_ENTAILMENT_TIMEOUT",
                    answer_sha256=answer_sha256,
                    evidence=evidence,
                    claims=tuple(preliminary.values()),
                )
            except FactualVerificationError as exc:
                return self._unknown_report(
                    code=exc.code,
                    answer_sha256=answer_sha256,
                    evidence=evidence,
                    claims=tuple(preliminary.values()),
                )
            except Exception:
                return self._unknown_report(
                    code="VERIFIER_ENTAILMENT_FAILED",
                    answer_sha256=answer_sha256,
                    evidence=evidence,
                    claims=tuple(preliminary.values()),
                )

        results: list[ClaimVerification] = []
        for index in range(1, len(claims) + 1):
            claim_id = f"claim_{index:04d}"
            if claim_id in preliminary:
                results.append(preliminary[claim_id])
                continue
            draft = drafts_by_id[claim_id]
            decision = decisions.get(claim_id)
            if decision is None:
                results.append(
                    ClaimVerification(
                        claim_id,
                        draft.text,
                        draft.kind,
                        ClaimVerificationStatus.UNKNOWN,
                        "CLAIM_ENTAILMENT_MISSING",
                    )
                )
                continue
            references = tuple(
                evidence_by_id[item].public_reference()
                for item in decision.evidence_ids
            )
            if decision.label is EntailmentLabel.ENTAILED:
                status = ClaimVerificationStatus.SUPPORTED
                code = "CLAIM_SUPPORTED"
            elif decision.label is EntailmentLabel.CONTRADICTED:
                status = ClaimVerificationStatus.CONTRADICTED
                code = "CLAIM_CONTRADICTED"
            elif decision.label is EntailmentLabel.INSUFFICIENT:
                status = ClaimVerificationStatus.UNSUPPORTED
                code = "CLAIM_EVIDENCE_INSUFFICIENT"
            else:
                status = ClaimVerificationStatus.UNKNOWN
                code = "CLAIM_ENTAILMENT_UNKNOWN"
            results.append(
                ClaimVerification(
                    claim_id,
                    draft.text,
                    draft.kind,
                    status,
                    code,
                    references,
                    decision.confidence,
                )
            )

        factual_results = [
            result for result in results if result.kind is ClaimKind.FACTUAL
        ]
        if any(
            result.status is ClaimVerificationStatus.UNKNOWN
            for result in factual_results
        ) or any(
            result.status is ClaimVerificationStatus.NOT_APPLICABLE
            for result in results
        ):
            overall = AnswerVerificationStatus.UNKNOWN
            code = "ANSWER_VERIFICATION_UNKNOWN"
        elif any(
            result.status
            in {
                ClaimVerificationStatus.CONTRADICTED,
                ClaimVerificationStatus.UNSUPPORTED,
            }
            for result in factual_results
        ):
            overall = AnswerVerificationStatus.FAILED
            code = "ANSWER_NOT_FULLY_SUPPORTED"
        else:
            overall = AnswerVerificationStatus.VERIFIED
            code = "ANSWER_VERIFIED"

        return AnswerVerificationReport(
            status=overall,
            code=code,
            answer_sha256=answer_sha256,
            evidence_snapshot_sha256=evidence.snapshot_sha256,
            extractor_id=_adapter_id(self.extractor, "invalid-extractor"),
            entailment_adapter_id=_adapter_id(self.entailment, "invalid-entailment"),
            claims=tuple(results),
        )


def evidence_from_project_knowledge_hits(
    hits: Sequence[Mapping[str, Any]], *, project_id: str
) -> EvidenceBundle:
    """Bind raw retrieval hits to stable, answer-visible evidence IDs.

    This helper must be called before prompt construction discards raw snippets.
    The returned IDs (``knowledge:<chunk_id>``) are the only citation markers an
    answer may use for factual verification.
    """

    project = _identifier(project_id, label="Project ID")
    records: list[EvidenceRecord] = []
    for hit in tuple(hits)[:MAX_EVIDENCE_ITEMS]:
        if not isinstance(hit, Mapping):
            continue
        citation = hit.get("citation")
        if not isinstance(citation, Mapping):
            continue
        if str(citation.get("project_id") or "") != project:
            raise FactualVerificationError(
                "Knowledge hit belongs to another project.",
                code="VERIFICATION_EVIDENCE_SCOPE_MISMATCH",
            )
        chunk_id = _identifier(citation.get("chunk_id"), label="Chunk ID")
        records.append(
            EvidenceRecord(
                evidence_id=f"knowledge:{chunk_id}",
                text=str(hit.get("text") or ""),
                kind="project_knowledge",
                project_id=project,
                citation=citation,
            )
        )
    return EvidenceBundle(tuple(records), project_id=project)


def evidence_from_project_knowledge_snapshot(
    context: str,
    sources: Sequence[Mapping[str, Any]],
    *,
    project_id: str,
    context_is_truncated: bool = False,
) -> EvidenceBundle:
    """Rebuild ephemeral evidence from the exact bounded chat prompt snapshot.

    Chat retry manifests intentionally retain masked prompt text and citation
    metadata separately.  Each complete section is matched against its stored
    snippet digest before it becomes evidence.  A final clipped section may be
    used only when the caller explicitly confirms that the model prompt was
    truncated; its new text digest remains visible in the verification report.
    """

    project = _identifier(project_id, label="Project ID")
    prompt_context = str(context or "").replace("\r\n", "\n").replace("\r", "\n")
    if not prompt_context or _CONTROL.search(prompt_context):
        raise FactualVerificationError(
            "Project Knowledge prompt context is unavailable.",
            code="VERIFICATION_EVIDENCE_SNAPSHOT_INVALID",
        )
    scoped_sources = tuple(sources)
    if not scoped_sources or len(scoped_sources) > MAX_EVIDENCE_ITEMS:
        raise FactualVerificationError(
            "Project Knowledge source metadata is unavailable.",
            code="VERIFICATION_EVIDENCE_SNAPSHOT_INVALID",
        )

    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(scoped_sources, start=1):
        if not isinstance(raw, Mapping) or str(raw.get("project_id") or "") != project:
            raise FactualVerificationError(
                "Project Knowledge source belongs to another project.",
                code="VERIFICATION_EVIDENCE_SCOPE_MISMATCH",
            )
        citation = raw.get("citation")
        if not isinstance(citation, Mapping):
            raise FactualVerificationError(
                "Project Knowledge citation is unavailable.",
                code="VERIFICATION_EVIDENCE_SNAPSHOT_INVALID",
            )
        chunk_id = _identifier(
            raw.get("chunk_id") or citation.get("chunk_id"), label="Chunk ID"
        )
        title = _safe_label(
            raw.get("source") or citation.get("title") or citation.get("source_id")
        ) or "知識庫文件"
        snippet_sha256 = str(raw.get("snippet_sha256") or "").strip().casefold()
        if not _SHA256.fullmatch(snippet_sha256):
            raise FactualVerificationError(
                "Project Knowledge snippet digest is unavailable.",
                code="VERIFICATION_EVIDENCE_SNAPSHOT_INVALID",
            )
        normalized.append(
            {
                "header": (
                    f"[evidence:knowledge:{chunk_id}]\n"
                    f"[知識來源 {index}：{title}]\n"
                ),
                "chunk_id": chunk_id,
                "citation": citation,
                "snippet_sha256": snippet_sha256,
            }
        )

    if not prompt_context.startswith(normalized[0]["header"]):
        raise FactualVerificationError(
            "Project Knowledge prompt does not match its source manifest.",
            code="VERIFICATION_EVIDENCE_SNAPSHOT_INVALID",
        )

    records: list[EvidenceRecord] = []
    content_start = len(normalized[0]["header"])
    for index, source in enumerate(normalized):
        is_last = index == len(normalized) - 1
        if not is_last:
            delimiter = "\n\n" + normalized[index + 1]["header"]
            search_from = content_start
            matched_at = -1
            while True:
                candidate_at = prompt_context.find(delimiter, search_from)
                if candidate_at < 0:
                    break
                candidate_text = prompt_context[content_start:candidate_at]
                candidate_digest = hashlib.sha256(
                    candidate_text.encode("utf-8")
                ).hexdigest()
                if candidate_digest == source["snippet_sha256"]:
                    matched_at = candidate_at
                    break
                search_from = candidate_at + 1
            if matched_at < 0:
                if not context_is_truncated:
                    raise FactualVerificationError(
                        "Project Knowledge prompt does not match its source manifest.",
                        code="VERIFICATION_EVIDENCE_SNAPSHOT_INVALID",
                    )
                partial = prompt_context[content_start:].strip()
                if partial:
                    records.append(
                        EvidenceRecord(
                            evidence_id=f"knowledge:{source['chunk_id']}",
                            text=partial,
                            kind="project_knowledge",
                            project_id=project,
                            citation=source["citation"],
                        )
                    )
                break
            content = prompt_context[content_start:matched_at]
            records.append(
                EvidenceRecord(
                    evidence_id=f"knowledge:{source['chunk_id']}",
                    text=content,
                    kind="project_knowledge",
                    project_id=project,
                    citation=source["citation"],
                )
            )
            content_start = matched_at + len(delimiter)
            continue

        content = prompt_context[content_start:]
        content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if content_digest != source["snippet_sha256"] and not context_is_truncated:
            raise FactualVerificationError(
                "Project Knowledge prompt does not match its source manifest.",
                code="VERIFICATION_EVIDENCE_SNAPSHOT_INVALID",
            )
        if content.strip():
            records.append(
                EvidenceRecord(
                    evidence_id=f"knowledge:{source['chunk_id']}",
                    text=content,
                    kind="project_knowledge",
                    project_id=project,
                    citation=source["citation"],
                )
            )

    if not records:
        raise FactualVerificationError(
            "Project Knowledge prompt contains no verifiable evidence.",
            code="VERIFICATION_EVIDENCE_SNAPSHOT_INVALID",
        )
    return EvidenceBundle(tuple(records), project_id=project)


__all__ = [
    "AnswerFactVerifier",
    "AnswerVerificationReport",
    "AnswerVerificationStatus",
    "ClaimDraft",
    "ClaimExtraction",
    "ClaimExtractionAdapter",
    "ClaimKind",
    "ClaimVerification",
    "ClaimVerificationStatus",
    "ConservativeExactEntailment",
    "DeterministicClaimExtractor",
    "EntailmentAdapter",
    "EntailmentDecision",
    "EntailmentLabel",
    "EntailmentRequest",
    "EvidenceBundle",
    "EvidenceRecord",
    "ExtractionStatus",
    "FactualVerificationError",
    "VerificationPolicy",
    "evidence_from_project_knowledge_hits",
    "evidence_from_project_knowledge_snapshot",
]
