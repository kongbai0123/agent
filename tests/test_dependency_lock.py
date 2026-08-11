"""The production dependency lock must cover every managed requirement."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "backend" / "requirements.txt"
LOCK = ROOT / "backend" / "requirements.lock"
LOCK_LINE = re.compile(
    r"^(?P<name>[A-Za-z0-9_.-]+)==(?P<version>\S+) --hash=sha256:(?P<hash>[a-f0-9]{64})$"
)


def _canonical(name: str) -> str:
    return name.casefold().replace("_", "-")


def test_the_transitive_lock_is_fully_pinned_and_hashed():
    assert LOCK.exists(), "generate backend/requirements.lock from a fresh pip report"
    locked = {}
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        match = LOCK_LINE.fullmatch(line)
        assert match, f"unlocked or unhashed dependency: {line}"
        name = _canonical(match.group("name"))
        assert name not in locked, f"duplicate lock entry: {name}"
        locked[name] = match.group("version")
    assert len(locked) > 50, "this is only a top-level list, not a transitive lock"


def test_every_managed_requirement_is_present_at_the_same_version():
    locked = {}
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        match = LOCK_LINE.fullmatch(line)
        if match:
            locked[_canonical(match.group("name"))] = match.group("version")

    missing = []
    mismatched = []
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, version = stripped.split("==", 1)
        actual = locked.get(_canonical(name))
        if actual is None:
            missing.append(name)
        elif actual != version:
            mismatched.append(f"{name}: requirements={version}, lock={actual}")
    assert not missing, f"requirements absent from lock: {missing}"
    assert not mismatched, f"requirements/lock version drift: {mismatched}"


def test_the_lock_has_a_committed_reproducible_generator():
    source = (ROOT / "scripts" / "lock_dependencies.py").read_text(encoding="utf-8")
    assert "--ignore-installed" in source
    assert "sha256" in source
    assert "dependency-lock-report.json" in source


def test_every_windows_job_installs_the_hashed_lock():
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflows, "at least one Windows workflow must be committed"
    for workflow in workflows:
        source = workflow.read_text(encoding="utf-8")
        if "runs-on: windows-" not in source:
            continue
        assert "backend/requirements.lock" in source or r"backend\requirements.lock" in source
        assert "--require-hashes" in source
