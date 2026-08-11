"""One line of JSON per event, with secrets removed, rotated, and expired.

`print()` was the logging strategy. It has three problems that matter here:
nobody can filter it, it disappears when the launcher runs the backend hidden,
and it happily writes a session token or an API key into a file that then lives
forever.

This module is deliberately small -- no logging framework, no config file. It
writes JSON lines, redacts by key name *and* by value shape (a token that
arrives under an innocent key is still a token), rotates by size, and deletes
files past the retention window. Callers get two verbs:

    log_event("tool_start", tool="read_file")
    degraded("browser", "install boundary handlers", error)

``degraded`` is the one that matters for M9: a swallowed ``except Exception:
pass`` leaves no trace anywhere, so a capability can stop working while the
product still reports success. Every such site now leaves a record that names
the component, what it was doing, and the real exception.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

#: Key names whose values never belong in a log.
SECRET_KEY_PARTS = (
    "token", "password", "secret", "api_key", "apikey", "authorization",
    "cookie", "credential", "session_key", "private_key",
)

#: Value shapes that are secrets regardless of the key they arrived under.
SECRET_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}\b", re.IGNORECASE),
    re.compile(r"\bX-Workbench-Token:\s*\S+", re.IGNORECASE),
)

REDACTED = "[redacted]"

DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_RETENTION_DAYS = 14
LOG_BASENAME = "workbench.jsonl"

_LOCK = threading.Lock()
_EXTRA_SECRETS: List[str] = []


def register_secret(value: str) -> None:
    """Redact a literal that only becomes known at runtime (e.g. the session token)."""
    text = str(value or "").strip()
    if len(text) >= 8 and text not in _EXTRA_SECRETS:
        _EXTRA_SECRETS.append(text)


def clear_registered_secrets() -> None:
    _EXTRA_SECRETS.clear()


def _redact_text(text: str) -> str:
    result = str(text)
    for literal in _EXTRA_SECRETS:
        result = result.replace(literal, REDACTED)
    for pattern in SECRET_VALUE_PATTERNS:
        result = pattern.sub(REDACTED, result)
    return result


def redact(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Remove secrets by key name, by value shape, and by registered literal."""
    if any(part in key.casefold() for part in SECRET_KEY_PARTS):
        return REDACTED
    if depth >= 6:
        return "[depth-limited]"
    if isinstance(value, dict):
        return {
            str(item_key)[:100]: redact(item_value, key=str(item_key), depth=depth + 1)
            for item_key, item_value in list(value.items())[:60]
        }
    if isinstance(value, (list, tuple, set)):
        return [redact(item, depth=depth + 1) for item in list(value)[:60]]
    if isinstance(value, str):
        return _redact_text(value)[:4000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(repr(value))[:1000]


def _log_dir() -> Path:
    configured = os.environ.get("WORKBENCH_LOG_DIR")
    if configured:
        return Path(configured)
    try:
        from paths import LOGS_DIR

        return Path(LOGS_DIR)
    except Exception:  # noqa: BLE001 - logging must work before paths does
        return Path.cwd() / "runtime" / "logs"


def _rotate_if_needed(path: Path, max_bytes: int) -> None:
    try:
        if path.exists() and path.stat().st_size >= max_bytes:
            # A second-resolution name collides as soon as a burst crosses the
            # threshold more than once in the same second.  On Windows rename
            # then fails because the destination exists; on platforms that
            # replace the destination it can silently discard an earlier
            # rotation.  Include microseconds and the process id, and still
            # probe for a free suffix so a frozen clock cannot lose evidence.
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f")
            stem = f"{path.stem}.{stamp}.{os.getpid()}"
            rotated = path.with_name(f"{stem}{path.suffix}")
            sequence = 1
            while rotated.exists():
                rotated = path.with_name(f"{stem}.{sequence}{path.suffix}")
                sequence += 1
            path.rename(rotated)
    except OSError as error:
        # Logging must not take the observed operation down, but a failed
        # rotation cannot be silent: it means retention and disk bounds are no
        # longer being enforced.
        print(f"[LOG] could not rotate {path}: {error}")


def purge_expired(directory: Optional[Path] = None, retention_days: int = DEFAULT_RETENTION_DAYS) -> List[str]:
    """Delete rotated logs past the retention window. Returns what it removed."""
    folder = Path(directory) if directory else _log_dir()
    if not folder.exists():
        return []
    cutoff = time.time() - retention_days * 86400
    removed: List[str] = []
    for candidate in folder.glob(f"{Path(LOG_BASENAME).stem}.*{Path(LOG_BASENAME).suffix}"):
        try:
            if candidate.stat().st_mtime < cutoff:
                candidate.unlink()
                removed.append(candidate.name)
        except OSError:
            continue
    return removed


def log_event(
    event: str,
    *,
    directory: Optional[Path] = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    **fields: Any,
) -> Dict[str, Any]:
    """Append one redacted JSON record. Never raises."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "event": str(event),
        **{key: redact(value, key=key) for key, value in fields.items()},
    }
    folder = Path(directory) if directory else _log_dir()
    line = json.dumps(record, ensure_ascii=False, default=repr)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / LOG_BASENAME
        with _LOCK:
            _rotate_if_needed(path, max_bytes)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except OSError as error:
        # Logging is not allowed to break the thing it is observing, but the
        # failure still has to be visible somewhere.
        print(f"[LOG] could not write {event}: {error}")
    return record


def degraded(
    component: str,
    action: str,
    error: BaseException,
    *,
    directory: Optional[Path] = None,
    **fields: Any,
) -> Dict[str, Any]:
    """Record a swallowed failure: what stopped working, and the real exception.

    Use this instead of ``except Exception: pass``. The call site keeps its
    resilience -- execution continues -- but the product stops pretending
    nothing happened.
    """
    return log_event(
        "degraded",
        directory=directory,
        component=str(component),
        action=str(action),
        error_type=type(error).__name__,
        error=str(error),
        **fields,
    )


def read_events(directory: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Parse the current log. For tests and the inspector."""
    path = (Path(directory) if directory else _log_dir()) / LOG_BASENAME
    if not path.exists():
        return []
    events: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            events.append({"event": "unparsable", "raw": line[:500]})
    return events


def summarize_degraded(events: Optional[Iterable[Dict[str, Any]]] = None) -> Dict[str, int]:
    """How many times each component degraded. Cheap health signal."""
    counts: Dict[str, int] = {}
    for record in events if events is not None else read_events():
        if record.get("event") == "degraded":
            key = f"{record.get('component')}:{record.get('action')}"
            counts[key] = counts.get(key, 0) + 1
    return counts
