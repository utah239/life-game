#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全候補を途中選抜せず、制度崩壊scenario x seed x policyで総当たりする。

27パラメータの全刻み直積(約3.9e52候補)ではなく、決定論的に生成した広域
Latin-hypercube候補の全件について、指定した全scenario/seed/policyを評価する。
SQLiteへconfig単位でcommitするため、Ctrl+C後は同じコマンドで再開できる。
"""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys


PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from dashboard import batch_sweep
from dashboard.batch_worker import SCENARIOS


DEFAULT_DB_PATH = batch_sweep.DASHBOARD_DIR / "sweeps" / "barter_matrix.sqlite3"
DEFAULT_REPORT_DIR = batch_sweep.DEFAULT_REPORT_DIR
DEFAULT_BYTES_PER_EVALUATION = 2500


def parse_scenarios(value: str) -> tuple[str, ...]:
    scenarios = tuple(dict.fromkeys(
        part.strip() for part in value.split(",") if part.strip()))
    unknown = sorted(set(scenarios) - set(SCENARIOS))
    if not scenarios or unknown:
        raise ValueError(
            f"scenarios must be selected from {SCENARIOS}; unknown={unknown}")
    return scenarios


def matrix_size(candidate_count: int, scenario_count: int, seed_count: int,
                policy_count: int, turns: int,
                bytes_per_evaluation: int = DEFAULT_BYTES_PER_EVALUATION) -> dict:
    evaluations = candidate_count * scenario_count * seed_count * policy_count
    return {
        "configs": candidate_count,
        "evaluations": evaluations,
        "simulated_turns_upper_bound": evaluations * turns,
        "estimated_database_bytes": evaluations * bytes_per_evaluation,
    }


def _human_duration(seconds: float) -> str:
    days, remainder = divmod(seconds, 86400)
    hours = remainder / 3600
    return f"{int(days)}d {hours:.1f}h" if days else f"{hours:.1f}h"


def build_matrix_plan(args, stages: list[batch_sweep.StageSpec],
                      policies: tuple[str, ...], estimate: dict) -> dict:
    return {
        "mode": "exhaustive_matrix_v1",
        "candidate_count": args.candidates,
        "search_seed": args.search_seed,
        "local_fraction": args.local_fraction,
        "policies": list(policies),
        "safety_floor": args.safety_floor,
        "talent": args.talent,
        "stages": [asdict(stage) for stage in stages],
        "estimate": estimate,
    }


def score_large_matrix(rows: list[dict], keep: int,
                       pinned_ids: set[int] | None = None) -> tuple[list[dict], list[int]]:
    """O(n^2) Pareto判定を、各目的上位の候補poolだけに限定する。"""
    if not rows:
        return rows, []
    keep = min(keep, len(rows))
    pool_limit = min(len(rows), max(keep * 8, 2000))
    orders = []
    for profile in batch_sweep.PROFILE_NAMES:
        orders.append(sorted(
            rows, key=lambda row, name=profile: row["profile_scores"][name],
            reverse=True)[:pool_limit])
    for metric in (
            "survival_rate", "mean_lifespan_ratio", "pass_rate",
            "alternative_observation_score", "policy_diversity",
            "regime_diversity", "recovery_ratio"):
        orders.append(sorted(
            rows, key=lambda row, name=metric: row[name], reverse=True)[:pool_limit])
    orders.append(sorted(rows, key=lambda row: row["shortage_rate"])[:pool_limit])
    orders.append(sorted(rows, key=lambda row: row["rapid_recross_rate"])[:pool_limit])
    pool_by_id = {}
    cursors = [0] * len(orders)
    while len(pool_by_id) < pool_limit:
        progressed = False
        for index, order in enumerate(orders):
            while cursors[index] < len(order):
                row = order[cursors[index]]
                cursors[index] += 1
                if row["config_id"] not in pool_by_id:
                    pool_by_id[row["config_id"]] = row
                    progressed = True
                    break
            if len(pool_by_id) >= pool_limit:
                break
        if not progressed:
            break
    for pinned_id in pinned_ids or set():
        match = next((row for row in rows if row["config_id"] == pinned_id), None)
        if match is not None:
            pool_by_id[pinned_id] = match
    for row in rows:
        row["pareto_front"] = False
    pool = list(pool_by_id.values())
    _, selected = batch_sweep.score_and_select(
        pool, keep, pinned_ids or set())
    return rows, selected


def combine_matrix_stages(database: batch_sweep.SweepDatabase,
                          config_ids: list[int], source_stages: tuple[str, ...],
                          stage_name: str, recommendations: int,
                          progress_every: int) -> tuple[list[dict], list[int]]:
    pending = database.pending_ids(stage_name, config_ids)
    for index, config_id in enumerate(pending, 1):
        evaluations = database.evaluation_summaries(config_id, source_stages)
        if not evaluations:
            raise RuntimeError(f"#{config_id}: source evaluations are missing")
        database.save_aggregate_only(
            config_id, stage_name,
            batch_sweep.aggregate_evaluations(evaluations))
        if index % max(1, progress_every) == 0 or index == len(pending):
            print(f"  aggregate {index}/{len(pending)} config / "
                  f"DB {batch_sweep._human_bytes(database.database_bytes())}")
    rows = database.completed_stage_rows(stage_name)
    scored, selected = score_large_matrix(
        rows, recommendations, {database.baseline_config_id()})
    database.apply_scoring(stage_name, scored, selected)
    return scored, selected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--candidates", type=int, default=100000)
    parser.add_argument("--workers", type=int,
                        default=max(1, os.cpu_count() or 1))
    parser.add_argument("--search-seed", type=int, default=20260817)
    parser.add_argument("--local-fraction", type=float, default=0.10)
    parser.add_argument("--scenarios", default=",".join(SCENARIOS))
    parser.add_argument("--policies", default=",".join(batch_sweep.POLICIES))
    parser.add_argument("--seeds", default="1-10")
    parser.add_argument("--turns", type=int, default=1920)
    parser.add_argument("--safety-floor", type=int, default=30)
    parser.add_argument("--talent",
                        choices=("random", "dexterity", "intellect", "skill", "health"),
                        default="random")
    parser.add_argument("--worker-timeout", type=int, default=3600)
    parser.add_argument("--progress-every", type=int, default=50)
    parser.add_argument("--report-top", type=int, default=500)
    parser.add_argument("--recommendations", type=int, default=500)
    parser.add_argument("--bytes-per-evaluation", type=int,
                        default=DEFAULT_BYTES_PER_EVALUATION)
    parser.add_argument("--max-estimated-db-gib", type=float, default=50.0)
    parser.add_argument("--assumed-turns-per-second", type=float, default=12500.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        scenarios = parse_scenarios(args.scenarios)
        seeds = batch_sweep.parse_seed_spec(args.seeds)
    except ValueError as exc:
        parser.error(str(exc))
    policies = tuple(dict.fromkeys(
        part.strip() for part in args.policies.split(",") if part.strip()))
    if not policies or any(policy not in batch_sweep.POLICIES for policy in policies):
        parser.error(f"--policies must be a subset of {batch_sweep.POLICIES}")
    if args.candidates < 1 or args.workers < 1 or args.turns < 1:
        parser.error("--candidates, --workers and --turns must be at least 1")
    if not 0.0 <= args.local_fraction <= 1.0:
        parser.error("--local-fraction must be between 0 and 1")
    if args.bytes_per_evaluation < 1 or args.max_estimated_db_gib <= 0:
        parser.error("database estimate options must be positive")

    estimate = matrix_size(
        args.candidates, len(scenarios), len(seeds), len(policies), args.turns,
        args.bytes_per_evaluation)
    estimated_gib = estimate["estimated_database_bytes"] / 1024 ** 3
    if estimated_gib > args.max_estimated_db_gib:
        parser.error(
            f"estimated DB {estimated_gib:.1f}GiB exceeds "
            f"--max-estimated-db-gib={args.max_estimated_db_gib:.1f}")
    stages = [
        batch_sweep.StageSpec(
            name=f"matrix_{scenario}", turns=args.turns, seeds=seeds,
            keep=None, scenario=scenario)
        for scenario in scenarios
    ]
    plan = build_matrix_plan(args, stages, policies, estimate)
    seconds = (estimate["simulated_turns_upper_bound"]
               / args.assumed_turns_per_second)
    print(f"candidates={args.candidates:,} / scenarios={len(scenarios)} / "
          f"seeds={len(seeds)} / policies={len(policies)}")
    print(f"evaluations={estimate['evaluations']:,} / "
          f"turn upper bound={estimate['simulated_turns_upper_bound']:,}")
    print(f"estimated DB={estimated_gib:.1f}GiB / "
          f"runtime upper estimate={_human_duration(seconds)}")
    print(f"workers={args.workers} / DB={args.db} / reports={args.report_dir}")
    if args.dry_run:
        print("dry-run: candidate generation and simulation were skipped")
        return

    candidates = batch_sweep.generate_candidates(
        args.candidates, args.search_seed, args.local_fraction)
    database = batch_sweep.SweepDatabase(args.db)
    try:
        database.initialize(
            plan, batch_sweep.compute_code_fingerprint(), candidates)
        config_ids = database.all_config_ids()
        for stage in stages:
            batch_sweep.run_stage(
                database, stage, config_ids, policies, args.safety_floor,
                args.talent, args.workers, args.worker_timeout,
                args.progress_every)
            batch_sweep.write_reports(
                database, stage.name, args.report_dir, args.report_top)
        source_names = tuple(stage.name for stage in stages)
        scored, selected = combine_matrix_stages(
            database, config_ids, source_names, "matrix_all",
            args.recommendations, args.progress_every)
        paths = batch_sweep.write_reports(
            database, "matrix_all", args.report_dir, args.report_top)
        print(f"matrix_all: candidates={len(scored):,}, "
              f"recommendations={len(selected):,}, report={paths[2]}")
        print(f"complete: DB {batch_sweep._human_bytes(database.database_bytes())}")
    finally:
        database.close()


if __name__ == "__main__":
    main()
