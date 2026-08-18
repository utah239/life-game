#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生存・寿命を採点せず、10万候補から制度レジーム候補を再探索する。"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import html
import json
import math
import os
from pathlib import Path
import sqlite3
import sys


PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from dashboard import batch_sweep, regime_evaluation, robustness_validate  # noqa: E402
from dashboard.batch_worker import SCENARIOS  # noqa: E402


DEFAULT_DB = batch_sweep.DASHBOARD_DIR / "sweeps" / "barter_matrix.sqlite3"
DEFAULT_SUMMARY = (
    batch_sweep.DASHBOARD_DIR / "sweeps" / "regime_candidate_search.json")
DEFAULT_REPORT = (
    batch_sweep.DEFAULT_REPORT_DIR / "barter_sweep_regime_candidates.html")
DEFAULT_CSV = (
    batch_sweep.DEFAULT_REPORT_DIR / "barter_sweep_regime_candidates.csv")
LEGACY_CONFIG_ID = 86708
LEGACY_PRODUCTION_DISRUPTION = 0.1


def semantic_parameter_guard(parameters: dict) -> dict:
    """制度を無効化して得点を稼ぐ縮退パラメータを候補外にする。"""
    checks = {
        "production_disruption_enabled": (
            float(parameters.get("production_disruption", 0.0)) > 0.0),
        "shortage_consequence_enabled": (
            float(parameters.get("shortage_energy_penalty", 0.0)) < 0.0
            or float(parameters.get("shortage_health_penalty", 0.0)) < 0.0),
        "barter_output_enabled": (
            float(parameters.get("barter_food_gain", 0.0)) > 0.0
            or float(parameters.get("barter_medicine_gain", 0.0)) > 0.0),
        "subsistence_stock_output_enabled": any(
            float(parameters.get(key, 0.0)) > 0.0 for key in (
                "subsistence_food_gain", "subsistence_medicine_gain")),
        "subsistence_capital_output_enabled": any(
            float(parameters.get(key, 0.0)) > 0.0 for key in (
                "subsistence_shelter_repair", "subsistence_tools_repair",
                "subsistence_production_gain")),
    }
    return {"passed": all(checks.values()), "checks": checks}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def latest_gpu_matrix_key(connection: sqlite3.Connection) -> str:
    row = connection.execute("""
        SELECT matrix_key FROM gpu_screen_results
        GROUP BY matrix_key ORDER BY COUNT(*) DESC, MAX(completed_at) DESC LIMIT 1
    """).fetchone()
    if row is None:
        raise ValueError("GPU screen results are missing")
    return str(row[0])


def _percentile_map(rows: list[dict], key: str) -> dict[int, float]:
    ordered = sorted((float(row[key]), int(row["config_id"])) for row in rows)
    if len(ordered) == 1:
        return {ordered[0][1]: 1.0}
    return {config_id: index / (len(ordered) - 1)
            for index, (_, config_id) in enumerate(ordered)}


def select_gpu_structural_portfolio(rows: list[dict], keep: int,
                                    pinned_ids: set[int]) -> list[int]:
    """GPUの制度代理指標だけを使い、異なる挙動の候補を幅広く残す。"""
    if keep < 1 or not rows:
        raise ValueError("rows and a positive keep are required")
    rank_keys = (
        "alternative_use_rate", "shortage_rate", "mean_goods_safety",
        "transition_per_run", "recovery_ratio")
    ranks = {key: _percentile_map(rows, key) for key in rank_keys}
    for row in rows:
        config_id = int(row["config_id"])
        use = ranks["alternative_use_rate"][config_id]
        shortage = ranks["shortage_rate"][config_id]
        safety = ranks["mean_goods_safety"][config_id]
        transitions = ranks["transition_per_run"][config_id]
        recovery = ranks["recovery_ratio"][config_id]
        row["gpu_structural_profiles"] = {
            "utilization": 0.55 * use + 0.25 * transitions + 0.20 * recovery,
            "recovery": 0.50 * recovery + 0.25 * transitions + 0.25 * use,
            "dynamic": 0.50 * transitions + 0.25 * recovery + 0.25 * use,
            "collapse": 0.45 * shortage + 0.30 * (1.0 - safety) + 0.25 * use,
            "preservation": 0.45 * safety + 0.30 * use + 0.25 * recovery,
        }
    names = tuple(rows[0]["gpu_structural_profiles"])
    orders = [sorted(rows, key=lambda row, name=name: (
        -row["gpu_structural_profiles"][name], int(row["config_id"])))
        for name in names]
    selected = []
    seen = set()
    cursors = [0] * len(orders)
    keep = min(keep, len(rows))
    while len(selected) < keep:
        for index, order in enumerate(orders):
            while cursors[index] < len(order):
                config_id = int(order[cursors[index]]["config_id"])
                cursors[index] += 1
                if config_id not in seen:
                    selected.append(config_id)
                    seen.add(config_id)
                    break
            if len(selected) >= keep:
                break
    available = {int(row["config_id"]) for row in rows}
    for config_id in sorted(pinned_ids & available):
        if config_id in seen:
            continue
        if len(selected) >= keep:
            seen.remove(selected.pop())
        selected.append(config_id)
        seen.add(config_id)
    return selected


def load_gpu_rows(connection: sqlite3.Connection, matrix_key: str) -> list[dict]:
    rows = []
    for row in connection.execute("""
        SELECT config_id,alternative_use_rate,shortage_rate,mean_goods_safety,
               transition_count,recovery_ratio,run_count
        FROM gpu_screen_results WHERE matrix_key=?
    """, (matrix_key,)):
        rows.append({
            "config_id": int(row[0]),
            "alternative_use_rate": float(row[1]),
            "shortage_rate": float(row[2]),
            "mean_goods_safety": float(row[3]),
            "transition_per_run": float(row[4]) / max(1, int(row[6])),
            "recovery_ratio": float(row[5]),
        })
    if not rows:
        raise ValueError(f"GPU matrix is empty: {matrix_key}")
    return rows


def aggregate_candidates(database: batch_sweep.SweepDatabase,
                         config_ids: list[int], stage_prefix: str) -> list[dict]:
    stage_names = tuple(f"{stage_prefix}_{scenario}" for scenario in SCENARIOS)
    records = []
    for config_id in config_ids:
        evaluations = database.evaluation_summaries(config_id, stage_names)
        if not evaluations:
            raise RuntimeError(f"#{config_id}: evaluations are missing")
        aggregate = regime_evaluation.aggregate_regime_evaluations(evaluations)
        parameters = database.config_parameters(config_id)
        aggregate.update({
            "config_id": config_id,
            "parameters": parameters,
            "semantic_guard": semantic_parameter_guard(parameters),
        })
        records.append(aggregate)
    regime_evaluation.score_regime_rows(records)
    return records


def _settings_key(settings: dict) -> str:
    payload = json.dumps(settings, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def _write_outputs(records: list[dict], settings: dict, stage: str,
                   legacy_record: dict | None, report: Path, csv_path: Path,
                   summary: Path) -> None:
    records = sorted(records, key=lambda row: (
        not row.get("selected_for_next", False),
        -row["profile_scores"]["structural_balance"], row["config_id"]))
    payload = {
        "schema_version": 1,
        "evaluation_version": regime_evaluation.EVALUATION_VERSION,
        "generated_at": utc_now(),
        "stage": stage,
        "settings": settings,
        "evaluation_policy": {
            "optimized": [
                "institutional path diversity", "scenario responsiveness",
                "alternative-economy use", "recovery", "transition quality"],
            "reference_only": [
                "survival rate", "lifespan", "health", "energy"],
            "hard_guards": ["finite/resource bounds", "mechanism not disabled"],
        },
        "legacy_candidate": {
            "config_id": LEGACY_CONFIG_ID,
            "production_disruption": LEGACY_PRODUCTION_DISRUPTION,
            "status": "reference_only_legacy_survival_weighted",
            "record": legacy_record,
        },
        "selected_ids": [row["config_id"] for row in records
                         if row.get("selected_for_next")],
        "records": records,
    }
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "config_id", "selected_for_next", "selection_rank", "pareto_front",
        *regime_evaluation.PROFILE_NAMES,
        "structural_criteria_score", "policy_diversity",
        "barter_final_stage_entropy", "scenario_regime_separation",
        "alternative_observation_score", "recovery_presence_rate",
        "chatter_score", "stage3_reach_rate", "shortage_exposure_rate",
        "numerical_guard_passed", "semantic_guard_passed",
        "reference_survival_rate", "reference_lifespan_ratio",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in records:
            flat = {key: row.get(key) for key in fields}
            flat.update(row["profile_scores"])
            flat["reference_survival_rate"] = row["reference_outcomes"]["survival_rate"]
            flat["reference_lifespan_ratio"] = row["reference_outcomes"]["mean_lifespan_ratio"]
            flat["numerical_guard_passed"] = row["numerical_guard"]["passed"]
            flat["semantic_guard_passed"] = row["semantic_guard"]["passed"]
            writer.writerow(flat)

    table_rows = []
    for row in records[:100]:
        params = html.escape(json.dumps(row["parameters"], ensure_ascii=False, indent=2))
        reference = row["reference_outcomes"]
        table_rows.append(
            "<tr>"
            f"<td>#{row['config_id']}</td>"
            f"<td>{row.get('selection_rank') or ''}</td>"
            f"<td>{row['profile_scores']['structural_balance']:.3f}</td>"
            f"<td>{row['barter_final_stage_entropy']:.3f}</td>"
            f"<td>{row['scenario_regime_separation']:.3f}</td>"
            f"<td>{row['policy_diversity']:.3f}</td>"
            f"<td>{row['alternative_observation_score']:.3f}</td>"
            f"<td>{row['recovery_presence_rate']:.3f}</td>"
            f"<td>{row['chatter_score']:.3f}</td>"
            f"<td>{'OK' if row['numerical_guard']['passed'] else '除外'}</td>"
            f"<td>{'OK' if row['semantic_guard']['passed'] else '除外'}</td>"
            f"<td>{reference['survival_rate']:.1%}</td>"
            f"<td>{reference['mean_lifespan_ratio']:.1%}</td>"
            f"<td><details><summary>表示</summary><pre>{params}</pre></details></td>"
            "</tr>")
    selected_count = sum(row.get("selected_for_next", False) for row in records)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(f"""<!doctype html><html lang=ja><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<title>制度レジーム再評価</title><style>
:root{{color-scheme:dark;--bg:#101617;--panel:#192123;--ink:#e7ebe4;--muted:#9aa69d;--accent:#d3a55e}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,sans-serif}}
main{{max-width:1500px;margin:auto;padding:24px}}.note,.table{{background:var(--panel);padding:16px;border-radius:9px;margin:14px 0}}
.note b{{color:var(--accent)}}.table{{overflow:auto}}table{{border-collapse:collapse;width:100%;white-space:nowrap}}
th,td{{padding:7px;border-bottom:1px solid #34403c;text-align:right}}th:first-child,td:first-child,td:last-child{{text-align:left}}
summary{{color:var(--accent);cursor:pointer}}pre{{white-space:pre-wrap;min-width:320px}}.ref{{color:var(--muted)}}
</style><main><h1>制度レジーム中心の候補再評価</h1>
<section class=note><b>評価方針を変更</b><br>生存率・寿命・health・energyは参考表示のみ。制度経路の多様性、scenario応答、代替経済の実利用、復旧、遷移品質から候補を選抜する。数値破綻と制度効果の無効化だけはhard guardで除外する。</section>
<section class=note>stage={html.escape(stage)} / 評価={len(records):,} / 選抜={selected_count:,}<br>
旧候補 <b>#{LEGACY_CONFIG_ID}</b> / production_disruption={LEGACY_PRODUCTION_DISRUPTION} は「旧・生存重視評価の参考候補」へ格下げ。</section>
<section class=table><table><thead><tr><th>ID</th><th>選抜順</th><th>構造score</th><th>Stage entropy</th><th>scenario差</th><th>方針差</th><th>代替利用</th><th>復旧観測</th><th>非chatter</th><th>数値guard</th><th>意味guard</th><th class=ref>生存(参考)</th><th class=ref>寿命(参考)</th><th>値</th></tr></thead>
<tbody>{''.join(table_rows)}</tbody></table></section></main></html>""", encoding="utf-8")


def _stage(database: batch_sweep.SweepDatabase, prefix: str,
           config_ids: list[int], turns: int, seeds: tuple[int, ...],
           policies: tuple[str, ...], args) -> list[dict]:
    robustness_validate.run_matrix_parallel(
        database, prefix, config_ids, turns, seeds, policies,
        args.safety_floor, args.talent, args.workers, args.timeout,
        args.progress_every, "coupled")
    return aggregate_candidates(database, config_ids, prefix)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--matrix-key", default="")
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--workers", type=int, default=max(1, os.cpu_count() or 1))
    parser.add_argument("--screen-count", type=int, default=500)
    parser.add_argument("--screen-turns", type=int, default=240)
    parser.add_argument("--screen-seeds", default="201-203")
    parser.add_argument("--refine-keep", type=int, default=80)
    parser.add_argument("--refine-turns", type=int, default=960)
    parser.add_argument("--refine-seeds", default="211-215")
    parser.add_argument("--final-keep", type=int, default=24)
    parser.add_argument("--final-turns", type=int, default=1920)
    parser.add_argument("--final-seeds", default="221-230")
    parser.add_argument("--recommendations", type=int, default=12)
    parser.add_argument("--policies", default=",".join(batch_sweep.POLICIES))
    parser.add_argument("--safety-floor", type=int, default=30)
    parser.add_argument("--talent", default="random")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--progress-every", type=int, default=10)
    parser.add_argument("--stop-after", choices=("screen", "refine", "final"),
                        default="final")
    args = parser.parse_args()
    if min(args.workers, args.screen_count, args.refine_keep, args.final_keep,
           args.recommendations) < 1:
        parser.error("counts and workers must be positive")
    policies = tuple(dict.fromkeys(
        value.strip() for value in args.policies.split(",") if value.strip()))
    try:
        screen_seeds = batch_sweep.parse_seed_spec(args.screen_seeds)
        refine_seeds = batch_sweep.parse_seed_spec(args.refine_seeds)
        final_seeds = batch_sweep.parse_seed_spec(args.final_seeds)
    except ValueError as exc:
        parser.error(str(exc))

    database = batch_sweep.SweepDatabase(args.db)
    try:
        matrix_key = args.matrix_key or latest_gpu_matrix_key(database.connection)
        gpu_rows = load_gpu_rows(database.connection, matrix_key)
        baseline_id = database.baseline_config_id()
        screen_ids = select_gpu_structural_portfolio(
            gpu_rows, args.screen_count, {baseline_id, LEGACY_CONFIG_ID})
        settings = {
            "evaluation_version": regime_evaluation.EVALUATION_VERSION,
            "matrix_key": matrix_key,
            "screen_count": args.screen_count,
            "screen_turns": args.screen_turns,
            "screen_seeds": list(screen_seeds),
            "refine_keep": args.refine_keep,
            "refine_turns": args.refine_turns,
            "refine_seeds": list(refine_seeds),
            "final_keep": args.final_keep,
            "final_turns": args.final_turns,
            "final_seeds": list(final_seeds),
            "policies": list(policies),
        }
        prefix = f"regime_{_settings_key(settings)}"
        screen = _stage(database, f"{prefix}_screen", screen_ids,
                        args.screen_turns, screen_seeds, policies, args)
        refine_ids = regime_evaluation.select_portfolio(
            screen, args.refine_keep, {baseline_id, LEGACY_CONFIG_ID})
        if args.stop_after == "screen":
            records, stage = screen, "screen"
        else:
            refine = _stage(database, f"{prefix}_refine", refine_ids,
                            args.refine_turns, refine_seeds, policies, args)
            final_ids = regime_evaluation.select_portfolio(
                refine, args.final_keep, {baseline_id, LEGACY_CONFIG_ID})
            if args.stop_after == "refine":
                records, stage = refine, "refine"
            else:
                final = _stage(database, f"{prefix}_final", final_ids,
                               args.final_turns, final_seeds, policies, args)
                regime_evaluation.select_portfolio(
                    final, min(args.recommendations, len(final)),
                    excluded_ids={baseline_id, LEGACY_CONFIG_ID})
                records, stage = final, "final"
        legacy_record = next(
            (row for row in records if row["config_id"] == LEGACY_CONFIG_ID), None)
        _write_outputs(records, settings, stage, legacy_record,
                       args.report, args.csv, args.summary)
        selected = [row["config_id"] for row in records
                    if row.get("selected_for_next")]
        lead = max((row for row in records if row.get("selection_eligible", True)),
                   key=lambda row: row["profile_scores"]["structural_balance"])
        print(f"complete: stage={stage} / lead=#{lead['config_id']} / "
              f"portfolio={selected} / report={args.report}")
    finally:
        database.close()


if __name__ == "__main__":
    main()
