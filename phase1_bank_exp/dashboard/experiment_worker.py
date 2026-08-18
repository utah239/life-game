#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1回のパラメータ付きrunを隔離プロセスで実行する内部worker。"""
import json
from pathlib import Path
import sys

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from dashboard.build_dashboard import build_dashboard_data  # noqa: E402
from dashboard.experiment_parameters import (  # noqa: E402
    apply_barter_overrides,
    validate_request,
)
from institutions import barter  # noqa: E402


def execute(payload: dict) -> dict:
    config = validate_request(payload)
    # game/engineがbarter定数をimportする前に適用する。workerは1runで終了するため
    # 値が別runや通常CLIへ漏れることはない。
    apply_barter_overrides(barter, config["barter_overrides"])
    import game

    run = config["run"]
    trace_data = game.collect_visualize_trace(
        run["seed"], run["policy"], run["turns"], run["safety_floor"],
        run["talent"], initial_population=run["initial_population"])
    trace_data["experiment_parameters"] = config["values"]
    dashboard_data = build_dashboard_data(trace_data, run["bins"])
    dashboard_data["experiment_parameters"] = config["values"]
    return dashboard_data


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        result = execute(payload)
        json.dump(result, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
