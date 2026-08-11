"""The PDF parser backend must report what it is and why.

The bug this file guards is not "Docling is broken" -- it is that a broken
Docling looked exactly like a missing Docling. The handler said "package not
found" for an ``ImportError`` raised *by an installed package*, so the obvious
fix (reinstall it) changed nothing and the real message never surfaced.
Meanwhile every PDF silently went through the weaker parser.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from document_parsing import (  # noqa: E402
    BACKEND_DOCLING,
    BACKEND_PYPDF,
    REASON_ERROR,
    REASON_INCOMPATIBLE,
    REASON_NOT_INSTALLED,
    REASON_OK,
    ParserBackend,
    classify_import_error,
    parser_backend_status,
    probe_docling,
    reset_cache,
)

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "pdf"

#: The exact failure observed with docling 2.15.0 / docling-core 2.84.0.
REAL_ERROR = ImportError(
    "cannot import name 'BoundingBox' from 'docling_core.types.legacy_doc.base'"
)


def teardown_function(_function):
    reset_cache()


# ------------------------------------------------------------ classification


def test_the_observed_failure_is_reported_as_a_version_conflict():
    backend = classify_import_error(REAL_ERROR)
    assert backend.reason == REASON_INCOMPATIBLE
    assert backend.name == BACKEND_PYPDF
    assert "BoundingBox" in backend.detail
    message = backend.message()
    assert "不相容" in message, "the message must not send anyone to reinstall the package"
    assert "未安裝" not in message


def test_a_genuinely_missing_package_still_says_so():
    error = ModuleNotFoundError("No module named 'docling'", name="docling")
    backend = classify_import_error(error)
    assert backend.reason == REASON_NOT_INSTALLED
    assert backend.missing_module == "docling"
    assert "未安裝" in backend.message()


def test_a_missing_dependency_is_not_a_missing_package():
    """`docling` is present; one of *its* imports is not. Reinstalling it won't help."""
    error = ModuleNotFoundError("No module named 'docling_core'", name="docling_core")
    backend = classify_import_error(error)
    assert backend.reason == REASON_INCOMPATIBLE
    assert backend.missing_module == "docling_core"


def test_an_incomplete_install_missing_the_converter_is_not_reported_as_uninstalled():
    def missing_converter(_name):
        raise ModuleNotFoundError(
            "No module named 'docling.document_converter'",
            name="docling.document_converter",
        )

    backend = probe_docling(importer=missing_converter)
    assert backend.reason == REASON_INCOMPATIBLE
    assert "未安裝" not in backend.message()


def test_a_non_import_failure_is_reported_verbatim_rather_than_guessed():
    backend = classify_import_error(AttributeError("module 'x' has no attribute 'y'"))
    assert backend.reason == REASON_ERROR
    assert "AttributeError" in backend.detail


def test_every_classification_carries_the_versions_that_decide_it():
    backend = classify_import_error(REAL_ERROR)
    assert "docling" in backend.versions and "docling-core" in backend.versions
    # Absence is an answer, not a crash.
    assert all(isinstance(value, str) and value for value in backend.versions.values())


def test_probing_never_raises_whatever_the_import_does():
    def explode(_name):
        raise RuntimeError("something entirely unexpected")

    backend = probe_docling(importer=explode)
    assert backend.name == BACKEND_PYPDF
    assert backend.available is False
    assert "RuntimeError" in backend.detail


def test_a_successful_import_reports_docling():
    backend = probe_docling(importer=lambda name: object())
    assert backend.name == BACKEND_DOCLING
    assert backend.available and backend.reason == REASON_OK
    assert backend.degraded is False


def test_the_status_payload_is_shaped_for_the_ui():
    status = ParserBackend(BACKEND_PYPDF, False, REASON_INCOMPATIBLE, "boom").as_status()
    assert set(status) == {"backend", "available", "degraded", "reason", "detail", "versions", "message"}
    assert status["degraded"] is True


def test_this_machine_reports_a_backend_without_crashing():
    """Whatever is installed here, status must be answerable."""
    status = parser_backend_status(refresh=True)
    assert status["backend"] in {BACKEND_DOCLING, BACKEND_PYPDF}
    assert status["message"]
    if status["backend"] == BACKEND_PYPDF:
        assert status["reason"] != REASON_OK
        assert status["detail"], "a degraded backend must say what failed"


# ----------------------------------------------------------------- fixtures


def test_the_three_pdf_fixtures_exist_and_are_what_they_claim():
    from pypdf import PdfReader

    text = PdfReader(str(FIXTURES / "chinese_text.pdf"))
    extracted = "\n".join((page.extract_text() or "") for page in text.pages)
    assert "斑馬魚" in extracted, "the CJK text layer did not survive"
    assert "QX-4471" in extracted

    table = PdfReader(str(FIXTURES / "chinese_table.pdf"))
    table_text = "\n".join((page.extract_text() or "") for page in table.pages)
    assert "存活率" in table_text and "97.5%" in table_text
    # pypdf flattens the grid: this is precisely the fidelity Docling is for.
    assert "存活率" in table_text.split("A-01")[0]

    scanned = PdfReader(str(FIXTURES / "scanned_page.pdf"))
    scanned_text = "".join((page.extract_text() or "") for page in scanned.pages)
    assert scanned_text.strip() == "", (
        "the scanned fixture has a text layer, so it cannot prove anything about OCR"
    )


def test_the_fixtures_are_reproducible_from_a_committed_script():
    script = Path(__file__).resolve().parents[1] / "scripts" / "build_pdf_fixtures.py"
    assert script.exists(), "binary fixtures with no generator become unmaintainable"
    source = script.read_text(encoding="utf-8")
    for name in ("chinese_text.pdf", "chinese_table.pdf", "scanned_page.pdf"):
        assert name in source
