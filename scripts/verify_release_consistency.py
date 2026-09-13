"""Verify the local release evidence points to one candidate version.

This check is intentionally local and read-only. It does not validate external
services, real tenants, model endpoints, or payment channels.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run_git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def fail(message: str) -> None:
    raise SystemExit(f"[FAIL] {message}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="release/v1.0.0-rc5-candidate")
    parser.add_argument("--expected-commit", default=None)
    args = parser.parse_args()

    head = run_git("rev-parse", "HEAD")
    tag_commit = run_git("rev-list", "-n", "1", args.tag)
    expected = args.expected_commit.lower() if args.expected_commit else None
    if expected and not head.lower().startswith(expected):
        fail(f"HEAD {head[:12]} does not match expected commit {expected}")
    # Documentation/evidence-only commits may follow the frozen tag. Require
    # the tag commit to remain an ancestor instead of forcing HEAD equality.
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", tag_commit, head],
        cwd=ROOT,
    )
    if ancestry.returncode != 0:
        fail(f"candidate tag {tag_commit[:12]} is not an ancestor of HEAD {head[:12]}")

    go_no_go = (ROOT / "evidence/prod-go-live/release-manager/GO_NO_GO.md").read_text(
        encoding="utf-8"
    )
    rc5_report = (
        ROOT / "evidence/prod-go-live/release-manager/RC5_PROD_READINESS_MASTER_REPORT.md"
    ).read_text(encoding="utf-8")
    acceptance_report = json.loads(
        (ROOT / "evidence/acceptance_report.json").read_text(encoding="utf-8")
    )

    required = {
        "Go/No-Go current tag": args.tag,
        "RC5 report tag": args.tag,
    }
    for label, needle in required.items():
        source = go_no_go if label.startswith("Go/") else rc5_report
        if needle not in source:
            fail(f"{label} missing: {needle}")

    pytest_result = acceptance_report.get("pytest", {})
    if acceptance_report.get("exit_code") != 0:
        fail("acceptance report exit_code is not 0")
    if pytest_result.get("passed") != 438 or pytest_result.get("skipped") != 36:
        fail(
            "acceptance report count mismatch: expected 438 passed / 36 skipped, "
            f"got {pytest_result.get('passed')} passed / {pytest_result.get('skipped')} skipped"
        )

    stale_rc4 = re.search(r"当前发布锚点[^\n]*rc4-candidate", go_no_go, re.IGNORECASE)
    if stale_rc4:
        fail("Go/No-Go still declares rc4-candidate as the current anchor")

    print(f"[PASS] release evidence is consistent for {args.tag} at {head[:12]}")
    print("[INFO] This check does not prove PostgreSQL, CrewAI, model, tenant, or payment readiness.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
