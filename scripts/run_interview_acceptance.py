"""Run the reproducible interview acceptance suite and emit JSON evidence.

The report records only observed pytest results plus explicit environment
metadata. It never upgrades skipped tests or external-model checks to PASS.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


SUMMARY = re.compile(
    r"(?:(?P<failed>\d+) failed, )?(?:(?P<errors>\d+) errors, )?"
    r"(?:(?P<passed>\d+) passed)?(?:, )?(?:(?P<skipped>\d+) skipped)?"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="evidence/interview_acceptance_report.json")
    args = parser.parse_args()
    command = [sys.executable, "-m", "pytest", "-q"]
    result = subprocess.run(command, text=True, capture_output=True)
    output = (result.stdout + "\n" + result.stderr).strip()
    summary = {}
    for line in reversed(output.splitlines()):
        match = SUMMARY.search(line)
        if match and any(value is not None for value in match.groupdict().values()):
            summary = {key: int(value or 0) for key, value in match.groupdict().items()}
            break
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "command": " ".join(command),
        "exit_code": result.returncode,
        "pytest": summary,
        "evidence_boundaries": {
            "unit_and_contract": "observed_by_pytest",
            "postgres_rls": "only_pass_if_postgres_tests_are_not_skipped",
            "external_model": "not_claimed_by_this_report",
            "production": "NO-GO",
        },
    }
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
