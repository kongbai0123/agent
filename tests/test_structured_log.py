"""Logging that can be filtered, cannot leak, and does not grow forever.

The three properties, in the order they bite:

* a swallowed failure leaves a record naming the component and the *real*
  exception -- otherwise a capability can stop working while the product still
  reports success;
* secrets are removed by key name **and** by value shape, because the token
  that ends up in a log usually arrives under a key nobody thought to redact;
* files rotate and expire, because a log that fills the disk gets deleted
  wholesale by whoever is on call, taking the evidence with it.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from structured_log import (  # noqa: E402
    LOG_BASENAME,
    REDACTED,
    clear_registered_secrets,
    degraded,
    log_event,
    purge_expired,
    read_events,
    redact,
    register_secret,
    summarize_degraded,
)


def teardown_function(_function):
    clear_registered_secrets()


# ------------------------------------------------------------------ redaction


def test_secret_keys_are_removed_whatever_they_contain():
    payload = {"api_key": "abc123", "authorization": "Bearer x", "cookie": "a=b", "tool": "read_file"}
    result = redact(payload)
    assert result["api_key"] == REDACTED
    assert result["authorization"] == REDACTED
    assert result["cookie"] == REDACTED
    assert result["tool"] == "read_file"


def test_a_secret_under_an_innocent_key_is_still_removed():
    """This is the case key-name redaction always misses."""
    credential = "sk-" + "ABCDEFGHIJKLMNOPQRSTUV"
    result = redact({"note": f"use {credential} to authenticate"})
    assert credential not in result["note"]
    assert REDACTED in result["note"]


def test_a_runtime_literal_can_be_registered_for_redaction():
    register_secret("live-session-token-value")
    result = redact({"detail": "header was live-session-token-value"})
    assert "live-session-token-value" not in result["detail"]


def test_nested_structures_are_redacted_all_the_way_down():
    result = redact({"outer": {"inner": {"password": "hunter2", "keep": 1}}})
    assert result["outer"]["inner"]["password"] == REDACTED
    assert result["outer"]["inner"]["keep"] == 1


def test_redaction_is_bounded_so_a_huge_payload_cannot_stall_a_write():
    result = redact({"blob": "x" * 100000, "many": list(range(500))})
    assert len(result["blob"]) <= 4000
    assert len(result["many"]) <= 60


# -------------------------------------------------------------------- writing


def test_an_event_is_one_json_line(tmp_path: Path):
    log_event("tool_start", directory=tmp_path, tool="read_file", sequence=1)
    lines = (tmp_path / LOG_BASENAME).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "tool_start"
    assert record["tool"] == "read_file"
    assert record["ts"].endswith("+00:00") or "T" in record["ts"]


def test_a_degraded_record_names_the_component_and_the_real_error(tmp_path: Path):
    degraded("browser", "install boundary handlers", RuntimeError("page is closed"), directory=tmp_path)
    record = read_events(tmp_path)[0]
    assert record["event"] == "degraded"
    assert record["component"] == "browser"
    assert record["action"] == "install boundary handlers"
    assert record["error_type"] == "RuntimeError"
    assert "page is closed" in record["error"]


def test_writing_never_raises_even_when_the_directory_is_impossible(tmp_path: Path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    record = log_event("tool_end", directory=blocker / "under-a-file", tool="x")
    assert record["event"] == "tool_end"  # the caller still gets its record


def test_degraded_events_can_be_counted_per_component(tmp_path: Path):
    degraded("browser", "close", RuntimeError("a"), directory=tmp_path)
    degraded("browser", "close", RuntimeError("b"), directory=tmp_path)
    degraded("excel", "release", RuntimeError("c"), directory=tmp_path)
    counts = summarize_degraded(read_events(tmp_path))
    assert counts == {"browser:close": 2, "excel:release": 1}


# ----------------------------------------------------- rotation and retention


def test_the_log_rotates_once_it_passes_the_size_limit(tmp_path: Path):
    for index in range(50):
        log_event("noise", directory=tmp_path, max_bytes=200, index=index, filler="y" * 50)
    rotated = sorted(tmp_path.glob("workbench.*.jsonl"))
    assert rotated, "the log grew past its limit without rotating"
    assert len(rotated) > 1, "same-second rotations overwrote or collided with each other"
    assert (tmp_path / LOG_BASENAME).stat().st_size < 2000

    all_paths = [*rotated, tmp_path / LOG_BASENAME]
    records = [
        json.loads(line)
        for path in all_paths
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert sorted(record["index"] for record in records) == list(range(50))


def test_expired_rotations_are_purged_but_the_current_log_is_kept(tmp_path: Path):
    log_event("keep", directory=tmp_path)
    old = tmp_path / "workbench.20200101T000000.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    ancient = time.time() - 40 * 86400
    os.utime(old, (ancient, ancient))

    removed = purge_expired(tmp_path, retention_days=14)
    assert removed == [old.name]
    assert not old.exists()
    assert (tmp_path / LOG_BASENAME).exists(), "retention must never delete the live log"


def test_recent_rotations_survive_the_purge(tmp_path: Path):
    recent = tmp_path / "workbench.20990101T000000.jsonl"
    recent.write_text("{}\n", encoding="utf-8")
    assert purge_expired(tmp_path, retention_days=14) == []
    assert recent.exists()
