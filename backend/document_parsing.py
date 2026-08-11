"""Which PDF parser is actually in use, and why.

The RAG engine used to decide this with:

    try:
        from docling.document_converter import DocumentConverter
        HAS_DOCLING = True
    except ImportError:
        print("[RAG] Warning: docling package not found. Will fallback to pypdf.")

Docling *is* installed. What actually happens on this machine is

    ImportError: cannot import name 'BoundingBox'
        from docling_core.types.legacy_doc.base

-- an incompatible ``docling-core``, which is also an ``ImportError``. So the
handler reported "package not found", someone reinstalled the package it said
was missing, nothing changed, and the real cause stayed invisible while every
PDF quietly went through the weaker parser. Chinese layout, tables and scans
degrade first, and nothing in the product said so.

This module keeps the fallback -- a broken parser must not stop ingestion --
but records *what failed*, distinguishes "not installed" from "installed but
incompatible", and reports the versions involved so the next person can act on
it instead of guessing.
"""

from __future__ import annotations

import importlib
import importlib.metadata as importlib_metadata
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

#: Packages whose versions decide whether Docling can import at all.
DOCLING_DISTRIBUTIONS = ("docling", "docling-core", "docling-ibm-models", "docling-parse")

BACKEND_DOCLING = "docling"
BACKEND_PYPDF = "pypdf"

REASON_OK = "ok"
REASON_NOT_INSTALLED = "not_installed"
REASON_INCOMPATIBLE = "incompatible_dependency"
REASON_ERROR = "import_failed"

#: The shape of the failure this module exists for: the package imports, one of
#: its own dependencies does not provide what it expects.
_INCOMPATIBLE_MARKERS = ("cannot import name", "no attribute", "unexpected keyword")


@dataclass(frozen=True)
class ParserBackend:
    """What the ingestion path will use for PDFs, and why."""

    name: str
    available: bool
    reason: str
    detail: str = ""
    versions: Dict[str, str] = field(default_factory=dict)
    missing_module: str = ""

    @property
    def degraded(self) -> bool:
        return self.name != BACKEND_DOCLING

    def as_status(self) -> Dict[str, Any]:
        """The payload /api/status and the startup screen show."""
        return {
            "backend": self.name,
            "available": self.available,
            "degraded": self.degraded,
            "reason": self.reason,
            "detail": self.detail,
            "versions": dict(self.versions),
            "message": self.message(),
        }

    def message(self) -> str:
        if self.reason == REASON_OK:
            return "PDF 解析使用 Docling（版面、表格與掃描件品質較佳）。"
        if self.reason == REASON_NOT_INSTALLED:
            return (
                f"未安裝 {self.missing_module or 'docling'}，PDF 解析退回 pypdf："
                "表格與掃描件會遺失結構。"
            )
        if self.reason == REASON_INCOMPATIBLE:
            versions = ", ".join(f"{name}={value}" for name, value in sorted(self.versions.items()))
            return (
                "Docling 已安裝但與相依套件版本不相容，PDF 解析退回 pypdf。"
                f"實際錯誤：{self.detail}（{versions or '版本未知'}）"
            )
        return f"Docling 匯入失敗，PDF 解析退回 pypdf。實際錯誤：{self.detail}"


def installed_versions(
    distributions: Optional[List[str]] = None,
    *,
    version_reader: Callable[[str], str] = importlib_metadata.version,
) -> Dict[str, str]:
    """Version of each Docling distribution, or "(not installed)"."""
    result: Dict[str, str] = {}
    for name in distributions or DOCLING_DISTRIBUTIONS:
        try:
            result[name] = str(version_reader(name))
        except Exception:  # noqa: BLE001 - absence is the answer, not an error
            result[name] = "(not installed)"
    return result


def classify_import_error(error: BaseException, *, target_module: str = "docling") -> ParserBackend:
    """Turn an import failure into something a human can act on.

    ``ModuleNotFoundError`` for the target itself means "install it". Anything
    else -- including a ``ModuleNotFoundError`` for a *different* module, and
    the "cannot import name X from Y" form -- means the stack is inconsistent,
    and reinstalling the target will not help.
    """
    detail = f"{type(error).__name__}: {error}"
    versions = installed_versions()
    missing = getattr(error, "name", "") or ""

    if isinstance(error, ModuleNotFoundError):
        target_root = target_module.split(".")[0]
        if str(missing) == target_root:
            return ParserBackend(
                BACKEND_PYPDF,
                False,
                REASON_NOT_INSTALLED,
                detail,
                versions,
                missing_module=target_root,
            )
        return ParserBackend(
            BACKEND_PYPDF,
            False,
            REASON_INCOMPATIBLE,
            detail,
            versions,
            missing_module=str(missing),
        )

    if isinstance(error, ImportError):
        text = str(error).casefold()
        if any(marker in text for marker in _INCOMPATIBLE_MARKERS):
            return ParserBackend(BACKEND_PYPDF, False, REASON_INCOMPATIBLE, detail, versions)
        return ParserBackend(BACKEND_PYPDF, False, REASON_ERROR, detail, versions)

    # AttributeError, TypeError and friends: the package imported and then blew
    # up on its own internals. Same practical meaning as an incompatible pin.
    return ParserBackend(BACKEND_PYPDF, False, REASON_ERROR, detail, versions)


def probe_docling(importer: Callable[[str], Any] = importlib.import_module) -> ParserBackend:
    """Import Docling for real and report the outcome. Never raises."""
    target = "docling.document_converter"
    try:
        importer(target)
    except BaseException as error:  # noqa: BLE001 - classified below, never hidden
        # Passing the full target distinguishes a genuinely absent top-level
        # package (missing name == "docling") from a corrupt/incomplete install
        # whose distribution metadata exists but whose submodule is missing.
        return classify_import_error(error, target_module=target)
    return ParserBackend(BACKEND_DOCLING, True, REASON_OK, "", installed_versions())


_CACHED: Optional[ParserBackend] = None


def parser_backend(refresh: bool = False) -> ParserBackend:
    """The process-wide answer. Probed once; imports are not free."""
    global _CACHED
    if _CACHED is None or refresh:
        _CACHED = probe_docling()
    return _CACHED


def parser_backend_status(refresh: bool = False) -> Dict[str, Any]:
    return parser_backend(refresh).as_status()


def reset_cache() -> None:
    """Tests only."""
    global _CACHED
    _CACHED = None
