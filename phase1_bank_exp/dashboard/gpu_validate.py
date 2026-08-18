#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GPU近似上位を現行CPU simulate_policyで段階的に正規検証する。

GPU順位の上位だけを盲信せず、全順位から等間隔に取った層化対照群と既定値も
screenへ混ぜる。240turn screen → 960turn refine → 1920turn finalの全段階で
6scenarioを使い、結果は既存batch_sweepの厳密evaluation形式へ保存する。
中断後は同じコマンドでcompleted済みconfigを飛ばして再開できる。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys


PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from dashboard import batch_sweep, exhaustive_sweep  # noqa: E402
from dashboard.batch_worker import SCENARIOS  # noqa: E402
from dashboard.gpu_barter_screen import DEFAULT_DB_PATH, DEFAULT_TOP_PATH  # noqa: E402


DEFAULT_REPORT_DIR = batch_sweep.DEFAULT_REPORT_DIR


def load_gpu_manifest(path: Path) -> dict:
    if not path.is_file():
        raise ValueError(f"GPU top file does not exist: {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    required = {"model_version", "matrix_key", "settings", "candidates"}
    missing = sorted(required - set(manifest))
    if missing:
        raise ValueError(f"GPU top file is missing: {', '.join(missing)}")
    return manifest


def select_screen_candidates(connection: sqlite3.Connection, matrix_key: str,
                             top_count: int, exploration_count: int,
                             baseline_id: int) -> tuple[list[int], dict[int, int]]:
    """GPU上位+全順位の等間隔sample+baselineを返す。rankは1始まり。"""
    ranked = [int(row[0]) for row in connection.execute("""
        SELECT config_id FROM gpu_screen_results WHERE matrix_key=?
        ORDER BY screen_score DESC, config_id ASC
    """, (matrix_key,))]
    if not ranked:
        raise ValueError(f"no GPU results for matrix_key={matrix_key}")
    rank_by_id = {config_id: index + 1 for index, config_id in enumerate(ranked)}
    selected = ranked[:min(top_count, len(ranked))]
    seen = set(selected)
    if exploration_count:
        # bin中央を使い、最上位/最下位だけに偏らない層化対照群にする。
        for index in range(exploration_count):
            position = min(
                len(ranked) - 1,
                int((index + 0.5) * len(ranked) / exploration_count))
            config_id = ranked[position]
            if config_id not in seen:
                selected.append(config_id)
                seen.add(config_id)
    if baseline_id not in seen:
        selected.append(baseline_id)
    return selected, rank_by_id


def validation_key(matrix_key: str, settings: dict) -> str:
    payload = json.dumps(
        {"matrix_key": matrix_key, **settings}, sort_keys=True,
        ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def run_scenario_matrix(database: batch_sweep.SweepDatabase, prefix: str,
                        turns: int, seeds: tuple[int, ...],
                        candidate_ids: list[int], policies: tuple[str, ...],
                        safety_floor: int, talent: str, workers: int,
                        timeout: int, progress_every: int,
                        report_dir: Path, keep: int) -> tuple[list[dict], list[int], str]:
    source_names = []
    for scenario in SCENARIOS:
        name = f"{prefix}_{scenario}"
        source_names.append(name)
        batch_sweep.run_stage(
            database,
            batch_sweep.StageSpec(
                name=name, turns=turns, seeds=seeds, keep=None,
                scenario=scenario),
            candidate_ids, policies, safety_floor, talent, workers, timeout,
            progress_every)
    combined_name = f"{prefix}_all"
    scored, selected = exhaustive_sweep.combine_matrix_stages(
        database, candidate_ids, tuple(source_names), combined_name, keep,
        progress_every)
    paths = batch_sweep.write_reports(
        database, combined_name, report_dir, min(500, len(scored)))
    print(f"[{combined_name}] candidates={len(scored)} selected={len(selected)} "
          f"report={paths[2]}")
    return scored, selected, combined_name


def gpu_cpu_rank_pairs(database: batch_sweep.SweepDatabase, matrix_key: str,
                       cpu_stage: str) -> list[tuple[int, int, int]]:
    """(config_id, GPU rank, CPU balanced-score rank)を作る。"""
    gpu_ids = [row[0] for row in database.connection.execute("""
        SELECT config_id FROM gpu_screen_results WHERE matrix_key=?
        ORDER BY screen_score DESC, config_id ASC
    """, (matrix_key,))]
    gpu_rank = {config_id: index + 1 for index, config_id in enumerate(gpu_ids)}
    cpu_rows = database.completed_stage_rows(cpu_stage)
    cpu_rows.sort(
        key=lambda row: row["profile_scores"]["balanced"], reverse=True)
    return [(row["config_id"], gpu_rank[row["config_id"]], index + 1)
            for index, row in enumerate(cpu_rows)]


def write_validation_summary(path: Path, manifest: dict, key: str,
                             stages: list[dict], rank_pairs: list[tuple[int, int, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "gpu_model_version": manifest["model_version"],
        "gpu_matrix_key": manifest["matrix_key"],
        "validation_key": key,
        "warning": "CPU exact results are authoritative; GPU rank is screening only",
        "stages": stages,
        "gpu_cpu_rank_pairs": [
            {"config_id": cid, "gpu_rank": gpu_rank, "cpu_rank": cpu_rank}
            for cid, gpu_rank, cpu_rank in rank_pairs
        ],
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--gpu-top", type=Path, default=DEFAULT_TOP_PATH)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--summary", type=Path,
                        default=batch_sweep.DASHBOARD_DIR / "sweeps" /
                        "gpu_cpu_validation.json")
    parser.add_argument("--workers", type=int, default=max(1, os.cpu_count() or 1))
    parser.add_argument("--top-candidates", type=int, default=500)
    parser.add_argument("--exploration-candidates", type=int, default=50)
    parser.add_argument("--screen-turns", type=int, default=240)
    parser.add_argument("--screen-seeds", default="1-3")
    parser.add_argument("--screen-keep", type=int, default=100)
    parser.add_argument("--refine-turns", type=int, default=960)
    parser.add_argument("--refine-seeds", default="1-5")
    parser.add_argument("--refine-keep", type=int, default=20)
    parser.add_argument("--final-turns", type=int, default=1920)
    parser.add_argument("--final-seeds", default="1-10")
    parser.add_argument("--safety-floor", type=int, default=30)
    parser.add_argument("--talent",
                        choices=("random", "dexterity", "intellect", "skill", "health"),
                        default="random")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--progress-every", type=int, default=10)
    parser.add_argument("--stop-after", choices=("screen", "refine", "final"),
                        default="final")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    positive = (args.workers, args.top_candidates, args.screen_turns,
                args.screen_keep, args.refine_turns, args.refine_keep,
                args.final_turns, args.timeout, args.progress_every)
    if any(value < 1 for value in positive) or args.exploration_candidates < 0:
        parser.error("counts/turns/workers must be positive")
    if not args.db.is_file():
        parser.error(f"database does not exist: {args.db}")
    try:
        manifest = load_gpu_manifest(args.gpu_top)
        screen_seeds = batch_sweep.parse_seed_spec(args.screen_seeds)
        refine_seeds = batch_sweep.parse_seed_spec(args.refine_seeds)
        final_seeds = batch_sweep.parse_seed_spec(args.final_seeds)
    except ValueError as exc:
        parser.error(str(exc))
    policies = batch_sweep.POLICIES
    settings = {
        "top_candidates": args.top_candidates,
        "exploration_candidates": args.exploration_candidates,
        "screen": [args.screen_turns, screen_seeds, args.screen_keep],
        "refine": [args.refine_turns, refine_seeds, args.refine_keep],
        "final": [args.final_turns, final_seeds],
        "policies": policies, "scenarios": SCENARIOS,
        "safety_floor": args.safety_floor, "talent": args.talent,
    }
    key = validation_key(manifest["matrix_key"], settings)
    database = batch_sweep.SweepDatabase(args.db)
    stages = []
    try:
        baseline = database.baseline_config_id()
        screen_ids, rank_by_id = select_screen_candidates(
            database.connection, manifest["matrix_key"], args.top_candidates,
            args.exploration_candidates, baseline)
        print(f"validation={key} / GPU matrix={manifest['matrix_key']}")
        print(f"screen candidates={len(screen_ids)} "
              f"(GPU top={args.top_candidates}, stratified={args.exploration_candidates}, "
              f"baseline rank={rank_by_id[baseline]})")
        estimated_turns = (
            len(screen_ids) * len(SCENARIOS) * len(screen_seeds) * len(policies)
            * args.screen_turns)
        print(f"screen simulated-turn upper bound={estimated_turns:,}")
        if args.dry_run:
            return
        _, refine_ids, screen_stage = run_scenario_matrix(
            database, f"gpu_{key}_screen", args.screen_turns, screen_seeds,
            screen_ids, policies, args.safety_floor, args.talent, args.workers,
            args.timeout, args.progress_every, args.report_dir, args.screen_keep)
        stages.append({"name": screen_stage, "candidates": len(screen_ids),
                       "selected": len(refine_ids)})
        rank_pairs = gpu_cpu_rank_pairs(
            database, manifest["matrix_key"], screen_stage)
        write_validation_summary(args.summary, manifest, key, stages, rank_pairs)
        if args.stop_after == "screen":
            return
        _, final_ids, refine_stage = run_scenario_matrix(
            database, f"gpu_{key}_refine", args.refine_turns, refine_seeds,
            refine_ids, policies, args.safety_floor, args.talent, args.workers,
            args.timeout, args.progress_every, args.report_dir, args.refine_keep)
        stages.append({"name": refine_stage, "candidates": len(refine_ids),
                       "selected": len(final_ids)})
        write_validation_summary(args.summary, manifest, key, stages, rank_pairs)
        if args.stop_after == "refine":
            return
        _, selected, final_stage = run_scenario_matrix(
            database, f"gpu_{key}_final", args.final_turns, final_seeds,
            final_ids, policies, args.safety_floor, args.talent, args.workers,
            args.timeout, args.progress_every, args.report_dir, len(final_ids))
        stages.append({"name": final_stage, "candidates": len(final_ids),
                       "selected": len(selected)})
        write_validation_summary(args.summary, manifest, key, stages, rank_pairs)
        print(f"complete: summary={args.summary}")
    finally:
        database.close()


if __name__ == "__main__":
    main()
