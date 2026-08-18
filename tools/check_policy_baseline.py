#!/usr/bin/env python3
"""Run --policy-check and compare its regime-aware aggregate baseline."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


TOTAL_PATTERN = re.compile(
    r"=== 合計: PASS (\d+) / FAIL (\d+) / N/A (\d+) \(分母(\d+)件")
CATEGORY_PATTERN = re.compile(
    r"([a-z_]+): PASS(\d+)/FAIL(\d+)/N(\d+)")


def parse_output(output: str) -> dict:
    total = TOTAL_PATTERN.search(output)
    if total is None:
        raise ValueError("policy-check total line was not found")
    categories = {
        name: {"pass": int(passed), "fail": int(failed), "na": int(na)}
        for name, passed, failed, na in CATEGORY_PATTERN.findall(output)}
    if not categories:
        raise ValueError("policy-check category line was not found")
    return {
        "pass": int(total.group(1)),
        "fail": int(total.group(2)),
        "na": int(total.group(3)),
        "denominator": int(total.group(4)),
        "categories": categories,
    }


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    project = repo_root / "phase1_bank_exp"
    baseline_path = project / "policy_check_baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    expected_exit = int(baseline.pop("exit_code"))
    baseline.pop("schema_version", None)
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, "game.py", "--policy-check"], cwd=project,
        env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8")
    try:
        observed = parse_output(result.stdout)
    except ValueError as exc:
        print(result.stdout, file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1
    failures = []
    if result.returncode != expected_exit:
        failures.append(
            f"exit code: expected {expected_exit}, observed {result.returncode}")
    if observed != baseline:
        failures.append(
            "aggregate baseline changed:\n"
            f"expected={json.dumps(baseline, ensure_ascii=False, sort_keys=True)}\n"
            f"observed={json.dumps(observed, ensure_ascii=False, sort_keys=True)}")
    if failures:
        print("Policy baseline check failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        print(
            "If the regime criteria changed intentionally, update "
            "phase1_bank_exp/policy_check_baseline.json in the same PR.",
            file=sys.stderr)
        return 1
    print(
        "Policy baseline passed: "
        f"PASS {observed['pass']} / FAIL {observed['fail']} / "
        f"N/A {observed['na']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
