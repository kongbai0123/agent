"""Compare a pip-audit report against the accepted baseline.

The milestone's acceptance is "audit 趨勢只下降" -- the count may fall, never
rise. A plain threshold cannot express that, and a plain "fail on any finding"
would have failed from day one with 69 known issues and been switched off
within a week.

So this is a ratchet. Every vulnerability that exists today is written into
``security/audit-baseline.json`` with the reason it is still there. A new one
fails the build; a fixed one has to be dropped from the baseline (the tool
refuses to leave stale entries behind, otherwise the file slowly becomes a
list of things that are no longer true).

    python scripts/check_dependency_audit.py --audit artifacts/pip-audit.json
    python scripts/check_dependency_audit.py --audit ... --accept "upgrade planned in M8 group 3"

Exit codes: 0 clean, 1 new vulnerabilities, 2 stale baseline entries, 3 usage.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = REPO_ROOT / "security" / "audit-baseline.json"


def finding_key(package: str, version: str, identifier: str) -> str:
    """One vulnerability in one pinned version. Version matters: an upgrade
    that fixes CVE-X in Pillow 10 must not silently inherit its acceptance."""
    return f"{package}=={version}::{identifier}"


def parse_audit(payload: Any) -> Dict[str, Dict[str, Any]]:
    """Read pip-audit JSON in either of the shapes it emits."""
    if isinstance(payload, dict):
        entries = payload.get("dependencies") or payload.get("results") or []
    else:
        entries = payload
    findings: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        package = str(entry.get("name") or entry.get("package") or "")
        version = str(entry.get("version") or "")
        for vulnerability in entry.get("vulns") or entry.get("vulnerabilities") or []:
            identifier = str(vulnerability.get("id") or "")
            if not (package and identifier):
                continue
            findings[finding_key(package, version, identifier)] = {
                "package": package,
                "version": version,
                "id": identifier,
                "fix_versions": vulnerability.get("fix_versions") or [],
                "description": str(vulnerability.get("description") or "")[:300],
            }
    return findings


def load_baseline(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"accepted": {}, "note": ""}
    return json.loads(path.read_text(encoding="utf-8"))


def compare(
    findings: Dict[str, Dict[str, Any]], baseline: Dict[str, Any]
) -> Tuple[List[str], List[str]]:
    accepted: Set[str] = set(baseline.get("accepted", {}))
    current: Set[str] = set(findings)
    return sorted(current - accepted), sorted(accepted - current)


def write_baseline(path: Path, findings: Dict[str, Dict[str, Any]], reason: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    accepted = {
        key: {
            "package": item["package"],
            "version": item["version"],
            "id": item["id"],
            "fix_versions": item["fix_versions"],
            "accepted_because": reason,
        }
        for key, item in sorted(findings.items())
    }
    path.write_text(
        json.dumps(
            {
                "note": (
                    "每一筆都是「已知且暫時接受」的弱點。新增弱點會讓 CI 失敗；"
                    "修好的弱點必須從這裡移除，否則這個檔案會慢慢變成一份不再為真的清單。"
                ),
                "accepted": accepted,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", required=True, help="pip-audit --format json output")
    parser.add_argument("--baseline", default=str(BASELINE_PATH))
    parser.add_argument(
        "--accept",
        default="",
        help="rewrite the baseline from this audit, recording the given reason",
    )
    args = parser.parse_args(argv)

    audit_path = Path(args.audit)
    if not audit_path.exists():
        print(f"audit report not found: {audit_path}", file=sys.stderr)
        return 3

    findings = parse_audit(json.loads(audit_path.read_text(encoding="utf-8")))
    baseline_path = Path(args.baseline)

    if args.accept:
        write_baseline(baseline_path, findings, args.accept)
        print(f"baseline rewritten with {len(findings)} accepted findings -> {baseline_path}")
        return 0

    baseline = load_baseline(baseline_path)
    accepted_count = len(baseline.get("accepted", {}))
    new, stale = compare(findings, baseline)

    if not accepted_count and findings:
        # First run. Everything is "new" because nothing has been accepted yet,
        # which is the correct fail-closed answer -- but say what to do about it
        # instead of leaving a wall of findings and no next step.
        print(
            f"the baseline at {baseline_path} is empty, so all {len(findings)} findings count as new.\n"
            "Seed it once, review the file, and commit it:\n"
            f"  python scripts/check_dependency_audit.py --audit {audit_path} "
            '--accept "M8 分組升級前的已知清單"'
        )

    print(f"known-accepted: {accepted_count}; current: {len(findings)}")
    if stale:
        print("\nfixed since the baseline was taken (remove them from the baseline):")
        for key in stale:
            print(f"  - {key}")
    if new:
        print("\nNEW vulnerabilities, not previously accepted:")
        for key in new:
            item = findings[key]
            fixes = ", ".join(item["fix_versions"]) or "no fixed version published"
            print(f"  - {key}  (fix: {fixes})")
        return 1
    if stale:
        # Not a security failure, but the baseline is now lying, and a lying
        # baseline is how a ratchet stops ratcheting.
        return 2
    print("no new vulnerabilities")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
