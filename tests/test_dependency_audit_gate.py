"""The audit ratchet: known issues may shrink, never grow.

69 known vulnerabilities cannot be fixed in one commit, and a gate that fails
on all of them from day one gets disabled within a week. So the gate compares
against an explicit baseline: everything accepted today is listed with a
reason, a *new* finding fails the build, and a *fixed* finding must be removed
from the baseline -- otherwise the file gradually becomes a list of things that
are no longer true, and the ratchet stops ratcheting.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from check_dependency_audit import (  # noqa: E402
    compare,
    finding_key,
    load_baseline,
    parse_audit,
    write_baseline,
)

AUDIT_SAMPLE = {
    "dependencies": [
        {
            "name": "pillow",
            "version": "10.3.0",
            "vulns": [
                {"id": "GHSA-aaaa", "fix_versions": ["10.4.0"], "description": "x"},
                {"id": "GHSA-bbbb", "fix_versions": [], "description": "y"},
            ],
        },
        {"name": "starlette", "version": "0.37.2", "vulns": [{"id": "GHSA-cccc", "fix_versions": []}]},
        {"name": "requests", "version": "2.34.2", "vulns": []},
    ]
}


def test_the_report_is_flattened_into_one_key_per_vulnerability():
    findings = parse_audit(AUDIT_SAMPLE)
    assert set(findings) == {
        "pillow==10.3.0::GHSA-aaaa",
        "pillow==10.3.0::GHSA-bbbb",
        "starlette==0.37.2::GHSA-cccc",
    }
    assert findings["pillow==10.3.0::GHSA-aaaa"]["fix_versions"] == ["10.4.0"]


def test_a_clean_package_contributes_nothing():
    assert not any(key.startswith("requests") for key in parse_audit(AUDIT_SAMPLE))


def test_the_key_includes_the_version_so_acceptance_does_not_carry_forward():
    """An upgrade must re-answer the question, not inherit the old answer."""
    assert finding_key("pillow", "10.3.0", "GHSA-aaaa") != finding_key("pillow", "11.0.0", "GHSA-aaaa")


def test_a_new_vulnerability_is_reported(tmp_path: Path):
    baseline_path = tmp_path / "baseline.json"
    write_baseline(baseline_path, parse_audit(AUDIT_SAMPLE), "accepted for the test")

    worse = json.loads(json.dumps(AUDIT_SAMPLE))
    worse["dependencies"].append(
        {"name": "lxml", "version": "5.2.1", "vulns": [{"id": "GHSA-dddd", "fix_versions": ["5.3.0"]}]}
    )
    new, stale = compare(parse_audit(worse), load_baseline(baseline_path))
    assert new == ["lxml==5.2.1::GHSA-dddd"]
    assert not stale


def test_a_fixed_vulnerability_is_reported_as_a_stale_baseline_entry(tmp_path: Path):
    baseline_path = tmp_path / "baseline.json"
    write_baseline(baseline_path, parse_audit(AUDIT_SAMPLE), "accepted for the test")

    better = json.loads(json.dumps(AUDIT_SAMPLE))
    better["dependencies"][0]["vulns"] = [better["dependencies"][0]["vulns"][1]]
    new, stale = compare(parse_audit(better), load_baseline(baseline_path))
    assert not new
    assert stale == ["pillow==10.3.0::GHSA-aaaa"]


def test_an_unchanged_audit_passes(tmp_path: Path):
    baseline_path = tmp_path / "baseline.json"
    write_baseline(baseline_path, parse_audit(AUDIT_SAMPLE), "accepted for the test")
    new, stale = compare(parse_audit(AUDIT_SAMPLE), load_baseline(baseline_path))
    assert not new and not stale


def test_every_accepted_entry_records_why(tmp_path: Path):
    baseline_path = tmp_path / "baseline.json"
    write_baseline(baseline_path, parse_audit(AUDIT_SAMPLE), "M8 group 5 排程升級中")
    saved = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert saved["accepted"]
    for entry in saved["accepted"].values():
        assert entry["accepted_because"] == "M8 group 5 排程升級中"
        assert entry["package"] and entry["id"]


def test_a_missing_baseline_means_everything_is_new(tmp_path: Path):
    """Fail closed: no baseline is not the same as nothing to report."""
    new, stale = compare(parse_audit(AUDIT_SAMPLE), load_baseline(tmp_path / "absent.json"))
    assert len(new) == 3 and not stale
