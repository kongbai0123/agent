"""Excel COM access, bound to one explicitly approved workbook.

The adapter used to operate on ``ActiveWorkbook``: whatever the user happened
to have in front of them at that instant. That makes the approval meaningless
-- a user approving a write to a scratch sheet could have it land in payroll if
focus moved in between, and nothing in the audit would show the difference.

Now a workbook must be *bound* first. Binding records the workbook's identity
(full path when it has been saved, plus name) and whether AutoSave is on; every
later read, write, or save re-checks that the workbook it is about to touch is
still that same one, by path, and refuses otherwise.

``set_excel_provider`` exists so the binding and refusal logic can be tested on
any platform with a fake COM object. Nothing else in this module depends on
Windows until an operation actually runs.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Dict, Optional


class WorkbookBindingError(RuntimeError):
    """The requested workbook is not the approved one (or none is approved)."""


@dataclass(frozen=True)
class WorkbookBinding:
    name: str
    path: str
    autosave: bool

    @property
    def identity(self) -> str:
        """Path when the workbook has been saved, else its name.

        An unsaved workbook has no path, so ``Book1`` is the only identity it
        has. It is still worth binding: the refusal below then catches focus
        moving to a *different* unsaved workbook.
        """
        return self.path or self.name

    def as_audit(self) -> Dict[str, Any]:
        return {"workbook": self.name, "path": self.path, "autosave": self.autosave}


_BINDING: Optional[WorkbookBinding] = None
_BINDING_LOCK = threading.Lock()
_PROVIDER: Optional[Callable[..., Any]] = None


def set_excel_provider(provider: Optional[Callable[..., Any]]) -> None:
    """Install a factory returning ``(excel, created)``. Tests only."""
    global _PROVIDER
    _PROVIDER = provider


def _com_modules():
    if __import__("sys").platform != "win32":
        raise RuntimeError("Excel COM is available only on Windows.")
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise RuntimeError("Excel COM requires pywin32 and a local Excel installation.") from exc
    return pythoncom, win32com.client


def _excel(*, start_if_missing: bool = False):
    if _PROVIDER is not None:
        return _PROVIDER(start_if_missing=start_if_missing)
    _pythoncom, client = _com_modules()
    try:
        return client.GetActiveObject("Excel.Application"), False
    except Exception as exc:
        if start_if_missing:
            try:
                excel = client.DispatchEx("Excel.Application")
                excel.Visible = True
                return excel, True
            except Exception as dispatch_exc:
                raise RuntimeError("Excel is installed but could not be started.") from dispatch_exc
        raise RuntimeError("No running Excel instance was found.") from exc


def _with_com(operation: Callable[[], Any]) -> Any:
    if _PROVIDER is not None:
        return operation()
    pythoncom, _client = _com_modules()
    pythoncom.CoInitialize()
    try:
        return operation()
    finally:
        pythoncom.CoUninitialize()


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return str(value)


def _workbook_autosave(book: Any) -> bool:
    """AutoSave is absent on older Excel builds and on non-OneDrive files."""
    for attribute in ("AutoSaveOn", "AutoSave"):
        try:
            value = getattr(book, attribute)
        except Exception:
            continue
        if value is None:
            continue
        try:
            return bool(value)
        except Exception:
            continue
    return False


def _describe(book: Any) -> WorkbookBinding:
    name = str(getattr(book, "Name", "") or "")
    try:
        path = str(getattr(book, "FullName", "") or "")
    except Exception:
        path = ""
    # Excel returns the bare name in FullName for a workbook that has never
    # been saved; that is not a filesystem identity, so it is not stored as one.
    if path == name:
        path = ""
    return WorkbookBinding(name=name, path=path, autosave=_workbook_autosave(book))


# ---------------------------------------------------------------- binding API


def current_binding() -> Optional[WorkbookBinding]:
    with _BINDING_LOCK:
        return _BINDING


def bound_workbook_autosaves() -> bool:
    """Used by the risk resolver, so it must never raise."""
    binding = current_binding()
    return bool(binding and binding.autosave)


def clear_workbook_binding() -> None:
    global _BINDING
    with _BINDING_LOCK:
        _BINDING = None


def _find_workbook(excel: Any, target: str) -> Any:
    wanted = str(target or "").strip()
    if not wanted:
        raise WorkbookBindingError("必須指定要綁定的活頁簿名稱或完整路徑。")
    wanted_cf = wanted.casefold().replace("/", "\\")
    for book in excel.Workbooks:
        candidate = _describe(book)
        for value in (candidate.path, candidate.name):
            if value and value.casefold().replace("/", "\\") == wanted_cf:
                return book
    raise WorkbookBindingError(f"找不到已開啟的活頁簿：{wanted}")


def excel_bind_workbook(workbook: str) -> str:
    """Approve exactly one workbook for this session's reads and writes."""

    def operation() -> str:
        global _BINDING
        excel, _created = _excel()
        binding = _describe(_find_workbook(excel, workbook))
        with _BINDING_LOCK:
            _BINDING = binding
        autosave_note = "；AutoSave 已開啟，寫入將立即落盤" if binding.autosave else ""
        return f"Bound workbook: {binding.identity}{autosave_note}"

    return _with_com(operation)


def _bound_workbook(excel: Any) -> Any:
    binding = current_binding()
    if binding is None:
        raise WorkbookBindingError(
            "尚未綁定活頁簿。請先以 excel_bind_workbook 取得使用者批准後再存取。"
        )
    for book in excel.Workbooks:
        candidate = _describe(book)
        if candidate.identity.casefold() == binding.identity.casefold():
            if candidate.autosave != binding.autosave:
                # AutoSave flipped after approval, so the risk the user agreed
                # to is not the risk that would run now. Fail closed.
                clear_workbook_binding()
                raise WorkbookBindingError(
                    f"活頁簿 {binding.identity} 的 AutoSave 狀態已變更，綁定失效，請重新批准。"
                )
            return book
    raise WorkbookBindingError(f"已綁定的活頁簿不再開啟：{binding.identity}")


# ------------------------------------------------------------- capability API


def excel_open_application() -> str:
    def operation() -> str:
        excel, created = _excel(start_if_missing=True)
        if created and int(excel.Workbooks.Count) == 0:
            # A new out-of-process Excel instance exits as soon as the final
            # COM proxy is released when it has no workbook. Keeping one blank
            # workbook makes the visible application persist for the user.
            excel.Workbooks.Add()
            excel.Visible = True
        return "Excel started and is visible." if created else "Excel is already running."

    return _with_com(operation)


def excel_list_workbooks() -> str:
    def operation() -> str:
        excel, _created = _excel()
        return "\n".join(book.Name for book in excel.Workbooks) or "(no open workbooks)"

    return _with_com(operation)


def excel_read_range(sheet: str, cell_range: str) -> Any:
    def operation() -> Any:
        excel, _created = _excel()
        book = _bound_workbook(excel)
        return _json_value(book.Worksheets(sheet).Range(cell_range).Value)

    return _with_com(operation)


def excel_write_range(sheet: str, cell_range: str, value: Any) -> str:
    def operation() -> str:
        excel, _created = _excel()
        book = _bound_workbook(excel)
        target = book.Worksheets(sheet).Range(cell_range)
        target.Value = value
        binding = current_binding()
        saved = "（AutoSave 已即時儲存）" if binding and binding.autosave else ""
        return f"Updated {book.Name}!{sheet}!{cell_range}{saved}"

    return _with_com(operation)


def excel_save_active_workbook() -> str:
    def operation() -> str:
        excel, _created = _excel()
        book = _bound_workbook(excel)
        book.Save()
        return f"Saved {book.Name}"

    return _with_com(operation)
