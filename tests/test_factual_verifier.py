from __future__ import annotations

import asyncio
import hashlib
import time

import pytest

from backend.factual_verifier import (
    AnswerFactVerifier,
    AnswerVerificationStatus,
    ClaimDraft,
    ClaimExtraction,
    ClaimKind,
    ClaimVerificationStatus,
    EntailmentDecision,
    EntailmentLabel,
    EvidenceBundle,
    EvidenceRecord,
    ExtractionStatus,
    FactualVerificationError,
    VerificationPolicy,
    evidence_from_project_knowledge_hits,
    evidence_from_project_knowledge_snapshot,
)


def evidence_record(evidence_id: str = "source-1") -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        text="法國的首都是巴黎。Workbench 的預設語言是繁體中文。",
        project_id="project-a",
        citation={
            "project_id": "project-a",
            "source_id": "guide.md",
            "title": "專案指南",
            "document_id": "document-1",
            "chunk_id": "chunk-1",
            "chunk_sha256": "a" * 64,
        },
    )


def verify(verifier: AnswerFactVerifier, answer: str, bundle: EvidenceBundle):
    return asyncio.run(verifier.verify(answer=answer, evidence=bundle))


class FixedExtractor:
    adapter_id = "fixed-extractor-v1"

    def __init__(self, *claims: ClaimDraft, complete: bool = True) -> None:
        self.claims = claims
        self.complete = complete

    def extract(self, answer, *, allowed_evidence_ids, max_claims):
        assert answer
        assert max_claims >= len(self.claims)
        assert tuple(allowed_evidence_ids)
        return ClaimExtraction(
            ExtractionStatus.COMPLETE if self.complete else ExtractionStatus.UNKNOWN,
            tuple(self.claims),
            complete=self.complete,
        )


class FixedEntailment:
    adapter_id = "fixed-entailment-v1"

    def __init__(self, label=EntailmentLabel.ENTAILED, *, evidence_id="source-1"):
        self.label = label
        self.evidence_id = evidence_id
        self.calls = []

    def evaluate(self, requests):
        self.calls.append(tuple(requests))
        return tuple(
            EntailmentDecision(
                request.claim_id,
                self.label,
                (self.evidence_id,) if self.evidence_id else (),
                0.99,
            )
            for request in requests
        )


def test_default_verifier_requires_citation_and_supports_exact_evidence():
    bundle = EvidenceBundle((evidence_record(),), project_id="project-a")
    report = verify(
        AnswerFactVerifier(),
        "法國的首都是巴黎。[evidence:source-1]",
        bundle,
    )

    assert report.status is AnswerVerificationStatus.VERIFIED
    assert report.gate_passed is True
    assert report.code == "ANSWER_VERIFIED"
    assert report.claims[0].status is ClaimVerificationStatus.SUPPORTED
    public = report.as_dict()
    assert public["claims"][0]["evidence"][0]["evidence_id"] == "source-1"
    assert public["claims"][0]["evidence"][0]["text_sha256"] == bundle.records[0].text_sha256
    assert "text" not in public["claims"][0]["evidence"][0]
    assert "法國的首都是巴黎。Workbench" not in str(public)


def test_factual_claim_without_explicit_citation_fails_closed():
    bundle = EvidenceBundle((evidence_record(),), project_id="project-a")
    report = verify(AnswerFactVerifier(), "法國的首都是巴黎。", bundle)

    assert report.status is AnswerVerificationStatus.FAILED
    assert report.gate_passed is False
    assert report.claims[0].code == "CLAIM_CITATION_REQUIRED"


def test_unknown_citation_is_unsupported_and_never_sent_to_entailment():
    adapter = FixedEntailment()
    extractor = FixedExtractor(
        ClaimDraft("法國的首都是巴黎。", cited_evidence_ids=("missing",))
    )
    bundle = EvidenceBundle((evidence_record(),), project_id="project-a")
    report = verify(
        AnswerFactVerifier(extractor=extractor, entailment=adapter),
        "法國的首都是巴黎。",
        bundle,
    )

    assert report.status is AnswerVerificationStatus.FAILED
    assert report.claims[0].code == "CLAIM_CITATION_NOT_AVAILABLE"
    assert adapter.calls == []


def test_adapter_cannot_widen_the_host_evidence_scope():
    extractor = FixedExtractor(
        ClaimDraft("法國的首都是巴黎。", cited_evidence_ids=("source-1",))
    )
    adapter = FixedEntailment(evidence_id="source-2")
    bundle = EvidenceBundle(
        (evidence_record("source-1"), evidence_record("source-2")),
        project_id="project-a",
    )
    report = verify(
        AnswerFactVerifier(extractor=extractor, entailment=adapter),
        "法國的首都是巴黎。",
        bundle,
    )

    assert report.status is AnswerVerificationStatus.UNKNOWN
    assert report.code == "VERIFIER_SCOPE_VIOLATION"
    assert report.gate_passed is False


def test_adapter_cannot_claim_entailment_without_evidence():
    extractor = FixedExtractor(
        ClaimDraft("法國的首都是巴黎。", cited_evidence_ids=("source-1",))
    )
    report = verify(
        AnswerFactVerifier(
            extractor=extractor,
            entailment=FixedEntailment(evidence_id=""),
        ),
        "法國的首都是巴黎。",
        EvidenceBundle((evidence_record(),), project_id="project-a"),
    )

    assert report.status is AnswerVerificationStatus.UNKNOWN
    assert report.code == "VERIFIER_OUTPUT_INVALID"
    assert report.gate_passed is False


@pytest.mark.parametrize(
    ("label", "expected_status", "expected_claim_status"),
    [
        (
            EntailmentLabel.CONTRADICTED,
            AnswerVerificationStatus.FAILED,
            ClaimVerificationStatus.CONTRADICTED,
        ),
        (
            EntailmentLabel.INSUFFICIENT,
            AnswerVerificationStatus.FAILED,
            ClaimVerificationStatus.UNSUPPORTED,
        ),
        (
            EntailmentLabel.UNKNOWN,
            AnswerVerificationStatus.UNKNOWN,
            ClaimVerificationStatus.UNKNOWN,
        ),
    ],
)
def test_non_supported_entailment_never_passes_gate(
    label, expected_status, expected_claim_status
):
    evidence_id = "source-1" if label is EntailmentLabel.CONTRADICTED else ""
    extractor = FixedExtractor(
        ClaimDraft("法國的首都是里昂。", cited_evidence_ids=("source-1",))
    )
    report = verify(
        AnswerFactVerifier(
            extractor=extractor,
            entailment=FixedEntailment(label, evidence_id=evidence_id),
        ),
        "法國的首都是里昂。",
        EvidenceBundle((evidence_record(),), project_id="project-a"),
    )

    assert report.status is expected_status
    assert report.gate_passed is False
    assert report.claims[0].status is expected_claim_status


def test_incomplete_extraction_and_no_factual_claims_are_unknown():
    bundle = EvidenceBundle((evidence_record(),), project_id="project-a")
    incomplete = verify(
        AnswerFactVerifier(
            extractor=FixedExtractor(
                ClaimDraft("法國的首都是巴黎。", cited_evidence_ids=("source-1",)),
                complete=False,
            )
        ),
        "法國的首都是巴黎。",
        bundle,
    )
    non_factual = verify(
        AnswerFactVerifier(
            extractor=FixedExtractor(
                ClaimDraft("是否要繼續？", kind=ClaimKind.NON_FACTUAL)
            )
        ),
        "是否要繼續？",
        bundle,
    )

    assert incomplete.code == "VERIFIER_EXTRACTION_INCOMPLETE"
    assert incomplete.gate_passed is False
    assert non_factual.code == "VERIFIER_NO_FACTUAL_CLAIMS"
    assert non_factual.gate_passed is False


def test_non_factual_label_cannot_hide_an_unverified_sentence():
    extractor = FixedExtractor(
        ClaimDraft("法國的首都是巴黎。", cited_evidence_ids=("source-1",)),
        ClaimDraft("另一個未驗證敘述。", kind=ClaimKind.NON_FACTUAL),
    )
    report = verify(
        AnswerFactVerifier(extractor=extractor),
        "法國的首都是巴黎。另一個未驗證敘述。",
        EvidenceBundle((evidence_record(),), project_id="project-a"),
    )

    assert report.status is AnswerVerificationStatus.UNKNOWN
    assert report.gate_passed is False


def test_host_adds_blocking_envelope_for_claim_extractor_omission():
    extractor = FixedExtractor(
        ClaimDraft("法國的首都是巴黎。", cited_evidence_ids=("source-1",))
    )
    report = verify(
        AnswerFactVerifier(extractor=extractor),
        "法國的首都是巴黎。[evidence:source-1]\n未引用的第二項敘述。",
        EvidenceBundle((evidence_record(),), project_id="project-a"),
    )

    assert report.status is AnswerVerificationStatus.FAILED
    assert report.gate_passed is False
    assert [claim.code for claim in report.claims] == [
        "CLAIM_SUPPORTED",
        "CLAIM_CITATION_REQUIRED",
    ]


def test_extracted_claim_must_be_bound_to_exact_answer_source_text():
    extractor = FixedExtractor(
        ClaimDraft(
            "法國的首都是巴黎。",
            cited_evidence_ids=("source-1",),
            source_text="這段文字根本不在回答中。",
        )
    )
    report = verify(
        AnswerFactVerifier(extractor=extractor),
        "法國的首都是巴黎。[evidence:source-1]",
        EvidenceBundle((evidence_record(),), project_id="project-a"),
    )

    assert report.status is AnswerVerificationStatus.UNKNOWN
    assert report.code == "VERIFIER_CLAIM_SCOPE_VIOLATION"
    assert report.gate_passed is False


def test_adapter_failure_and_timeout_are_masked_unknown_results():
    class FailingExtractor:
        adapter_id = "failing-extractor-v1"

        def extract(self, *args, **kwargs):
            raise RuntimeError("secret upstream response")

    class SlowExtractor:
        adapter_id = "slow-extractor-v1"

        def extract(self, *args, **kwargs):
            time.sleep(0.2)
            return ClaimExtraction(ExtractionStatus.COMPLETE, ())

    bundle = EvidenceBundle((evidence_record(),), project_id="project-a")
    failed = verify(AnswerFactVerifier(extractor=FailingExtractor()), "回答", bundle)
    timed_out = verify(
        AnswerFactVerifier(
            extractor=SlowExtractor(),
            policy=VerificationPolicy(adapter_timeout_seconds=0.05),
        ),
        "回答",
        bundle,
    )

    assert failed.code == "VERIFIER_EXTRACTION_FAILED"
    assert "secret upstream response" not in str(failed.as_dict())
    assert timed_out.code == "VERIFIER_EXTRACTION_TIMEOUT"
    assert failed.gate_passed is timed_out.gate_passed is False


def test_evidence_bundle_enforces_project_scope_uniqueness_and_size():
    with pytest.raises(FactualVerificationError) as mismatch:
        EvidenceRecord(
            "source-1",
            "內容",
            project_id="project-a",
            citation={"project_id": "project-b"},
        )
    assert mismatch.value.code == "VERIFICATION_EVIDENCE_SCOPE_MISMATCH"

    with pytest.raises(FactualVerificationError, match="unique"):
        EvidenceBundle(
            (evidence_record("same"), evidence_record("same")),
            project_id="project-a",
        )

    inferred = EvidenceBundle((evidence_record(),))
    assert inferred.project_id == "project-a"
    with pytest.raises(FactualVerificationError) as mixed:
        EvidenceBundle(
            (
                evidence_record("project-a-source"),
                EvidenceRecord(
                    "project-b-source",
                    "另一專案內容",
                    project_id="project-b",
                    citation={"project_id": "project-b"},
                ),
            )
        )
    assert mixed.value.code == "VERIFICATION_EVIDENCE_SCOPE_MISMATCH"


def test_project_knowledge_hits_get_stable_scoped_evidence_ids():
    hits = [
        {
            "text": "法國的首都是巴黎。",
            "score": 0.9,
            "citation": {
                "project_id": "project-a",
                "document_id": "document-1",
                "chunk_id": "chunk-1",
                "source_id": "guide.md",
                "title": "指南",
                "chunk_sha256": "b" * 64,
            },
        }
    ]
    first = evidence_from_project_knowledge_hits(hits, project_id="project-a")
    second = evidence_from_project_knowledge_hits(hits, project_id="project-a")

    assert first.snapshot_sha256 == second.snapshot_sha256
    assert first.records[0].evidence_id == "knowledge:chunk-1"
    assert first.records[0].citation["project_id"] == "project-a"
    with pytest.raises(FactualVerificationError) as mismatch:
        evidence_from_project_knowledge_hits(hits, project_id="project-b")
    assert mismatch.value.code == "VERIFICATION_EVIDENCE_SCOPE_MISMATCH"


def test_project_knowledge_prompt_snapshot_uses_digest_bound_sections():
    first = "第一項證據。"
    second = "第二項證據。"
    sources = [
        {
            "project_id": "project-a",
            "source": "甲",
            "chunk_id": "chunk-a",
            "snippet_sha256": hashlib.sha256(first.encode()).hexdigest(),
            "citation": {
                "project_id": "project-a",
                "document_id": "document-a",
                "chunk_id": "chunk-a",
            },
        },
        {
            "project_id": "project-a",
            "source": "乙",
            "chunk_id": "chunk-b",
            "snippet_sha256": hashlib.sha256(second.encode()).hexdigest(),
            "citation": {
                "project_id": "project-a",
                "document_id": "document-b",
                "chunk_id": "chunk-b",
            },
        },
    ]
    context = (
        f"[evidence:knowledge:chunk-a]\n[知識來源 1：甲]\n{first}\n\n"
        f"[evidence:knowledge:chunk-b]\n[知識來源 2：乙]\n{second}"
    )

    bundle = evidence_from_project_knowledge_snapshot(
        context, sources, project_id="project-a"
    )

    assert [item.evidence_id for item in bundle.records] == [
        "knowledge:chunk-a",
        "knowledge:chunk-b",
    ]
    assert [item.text for item in bundle.records] == [first, second]

    tampered = context.replace(first, "遭竄改")
    with pytest.raises(FactualVerificationError) as invalid:
        evidence_from_project_knowledge_snapshot(
            tampered, sources, project_id="project-a"
        )
    assert invalid.value.code == "VERIFICATION_EVIDENCE_SNAPSHOT_INVALID"

    wrong_marker = context.replace(
        "[evidence:knowledge:chunk-b]", "[evidence:knowledge:chunk-a]"
    )
    with pytest.raises(FactualVerificationError) as marker_mismatch:
        evidence_from_project_knowledge_snapshot(
            wrong_marker, sources, project_id="project-a"
        )
    assert marker_mismatch.value.code == "VERIFICATION_EVIDENCE_SNAPSHOT_INVALID"


def test_truncated_project_knowledge_snapshot_uses_only_visible_partial_tail():
    full = "模型只看得到這一段後面還有更多內容"
    visible = "模型只看得到這一段"
    sources = [
        {
            "project_id": "project-a",
            "source": "甲",
            "chunk_id": "chunk-a",
            "snippet_sha256": hashlib.sha256(full.encode()).hexdigest(),
            "citation": {
                "project_id": "project-a",
                "document_id": "document-a",
                "chunk_id": "chunk-a",
            },
        }
    ]
    bundle = evidence_from_project_knowledge_snapshot(
        f"[evidence:knowledge:chunk-a]\n[知識來源 1：甲]\n{visible}",
        sources,
        project_id="project-a",
        context_is_truncated=True,
    )

    assert bundle.records[0].text == visible


def test_security_policy_cannot_be_configured_fail_open():
    policy = VerificationPolicy()
    assert policy.require_explicit_citations is True
    assert policy.unknown_blocks is True
    assert policy.unsupported_blocks is True
    with pytest.raises(TypeError):
        VerificationPolicy(unknown_blocks=False)  # type: ignore[call-arg]
