#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""config 86708を中心にproduction_disruptionだけをCPU正規検証する。"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
from pathlib import Path
import sys


PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from dashboard import batch_sweep  # noqa: E402
from dashboard import robustness_validate as robustness  # noqa: E402
from dashboard.batch_worker import SCENARIOS  # noqa: E402
from dashboard.experiment_parameters import SPEC_BY_KEY, validate_request  # noqa: E402


PARAMETER_KEY = "production_disruption"
DEFAULT_SOURCE_DB = robustness.DEFAULT_SOURCE_DB
DEFAULT_DB = (
    batch_sweep.DASHBOARD_DIR / "sweeps" /
    "barter_production_disruption_oat.sqlite3")
DEFAULT_SUMMARY = (
    batch_sweep.DASHBOARD_DIR / "sweeps" /
    "barter_production_disruption_oat.json")
DEFAULT_REPORT = (
    batch_sweep.DEFAULT_REPORT_DIR /
    "barter_sweep_production_disruption_oat.html")
DEFAULT_CSV = (
    batch_sweep.DEFAULT_REPORT_DIR /
    "barter_sweep_production_disruption_oat.csv")


def parse_values(raw: str) -> tuple[float, ...]:
    spec = SPEC_BY_KEY[PARAMETER_KEY]
    values = []
    for part in raw.split(","):
        if not part.strip():
            continue
        value = float(part)
        if value < spec.minimum or value > spec.maximum:
            raise ValueError(
                f"{PARAMETER_KEY} must be between {spec.minimum} and {spec.maximum}")
        steps = round((value - spec.minimum) / spec.step)
        normalized = round(spec.minimum + steps * spec.step, 10)
        if abs(normalized - value) > 1e-9:
            raise ValueError(f"{value} is not aligned to step {spec.step}")
        if normalized not in values:
            values.append(normalized)
    if not values:
        raise ValueError("at least one value is required")
    return tuple(values)


def build_candidates(source: dict, values: tuple[float, ...]) -> list[dict]:
    candidates = []
    for value in values:
        row = dict(source)
        row[PARAMETER_KEY] = value
        full = {"values": dict(
            row, seed=1, turns=1920, policy="cautious", talent="random",
            safety_floor=30, bins=60)}
        validated = validate_request(full)["values"]
        candidates.append({
            key: validated[key] for key in batch_sweep.TUNABLE_KEYS})
    if len({batch_sweep.config_hash(row) for row in candidates}) != len(candidates):
        raise ValueError("one-factor values produced duplicate configurations")
    return candidates


def experiment_key(settings: dict) -> str:
    payload = json.dumps(settings, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _local_ids_by_value(database: batch_sweep.SweepDatabase) -> dict[float, int]:
    return {
        float(database.config_parameters(config_id)[PARAMETER_KEY]): config_id
        for config_id in database.all_config_ids()
    }


def _acceptance(metrics: dict, reference: dict) -> dict:
    checks = {
        "energy_guard": metrics["energy_guard"]["guard_pass"],
        "survival_within_10pp": (
            metrics["survival_rate"] >= reference["survival_rate"] - 0.10),
        "lifespan_within_5pp": (
            metrics["mean_lifespan_ratio"]
            >= reference["mean_lifespan_ratio"] - 0.05),
        "pass_rate_within_5pp": (
            metrics["pass_rate"] >= reference["pass_rate"] - 0.05),
        "shortage_within_limit": (
            metrics["shortage_rate"]
            <= max(0.005, reference["shortage_rate"] * 2.0)),
    }
    return {"passed": all(checks.values()), "checks": checks}


def analyze(database: batch_sweep.SweepDatabase, stage_prefix: str,
            values: tuple[float, ...], reference_value: float,
            severe_energy: float, catastrophic_energy: float) -> dict:
    ids = _local_ids_by_value(database)
    stage_names = robustness._matrix_stage_names(stage_prefix)
    metrics = {}
    scenarios = {}
    for value in values:
        evaluations = database.evaluation_summaries(ids[value], stage_names)
        metrics[value] = robustness.summarize_exact(
            evaluations, severe_energy, catastrophic_energy)
        scenarios[value] = robustness._by_scenario(
            evaluations, severe_energy, catastrophic_energy)
    reference = metrics[reference_value]
    records = []
    for value in values:
        acceptance = _acceptance(metrics[value], reference)
        records.append({
            "value": value,
            "semantic_candidate": value > 0.0,
            "acceptance": acceptance,
            "metrics": metrics[value],
            "by_scenario": scenarios[value],
        })
    accepted_nonzero = [
        row["value"] for row in records
        if row["semantic_candidate"] and row["acceptance"]["passed"]]
    return {
        "reference_value": reference_value,
        "records": records,
        "accepted_nonzero_values": accepted_nonzero,
        "accepted_nonzero_range": (
            [min(accepted_nonzero), max(accepted_nonzero)]
            if accepted_nonzero else None),
    }


def write_outputs(summary: dict, summary_path: Path, report_path: Path,
                  csv_path: Path) -> None:
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = [
        "production_disruption", "semantic_candidate", "accepted",
        "survival_rate", "survival_ci_low", "survival_ci_high",
        "mean_lifespan_ratio", "pass_rate", "shortage_rate",
        "mean_min_energy", "worst_min_energy",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in summary["analysis"]["records"]:
            metrics = row["metrics"]
            energy = metrics["energy_guard"]
            writer.writerow({
                "production_disruption": row["value"],
                "semantic_candidate": row["semantic_candidate"],
                "accepted": row["acceptance"]["passed"],
                "survival_rate": metrics["survival_rate"],
                "survival_ci_low": metrics["survival_ci95"][0],
                "survival_ci_high": metrics["survival_ci95"][1],
                "mean_lifespan_ratio": metrics["mean_lifespan_ratio"],
                "pass_rate": metrics["pass_rate"],
                "shortage_rate": metrics["shortage_rate"],
                "mean_min_energy": energy["mean_min_energy"],
                "worst_min_energy": energy["worst_min_energy"],
            })
    rows = []
    for row in summary["analysis"]["records"]:
        metrics = row["metrics"]
        energy = metrics["energy_guard"]
        accepted = row["acceptance"]["passed"]
        label = "対照(生産低下なし)" if not row["semantic_candidate"] else (
            "許容" if accepted else "不許容")
        rows.append(
            f"<tr><td>{row['value']:.1f}</td>"
            f"<td class={'ok' if accepted else 'bad'}>{label}</td>"
            f"<td>{metrics['survival_rate']:.1%}</td>"
            f"<td>{metrics['survival_ci95'][0]:.1%}–"
            f"{metrics['survival_ci95'][1]:.1%}</td>"
            f"<td>{metrics['mean_lifespan_ratio']:.1%}</td>"
            f"<td>{metrics['pass_rate']:.1%}</td>"
            f"<td>{metrics['shortage_rate']:.3%}</td>"
            f"<td>{energy['mean_min_energy']:.2f}</td></tr>")
    scenario_rows = []
    for row in summary["analysis"]["records"]:
        for scenario in SCENARIOS:
            metrics = row["by_scenario"][scenario]
            scenario_rows.append(
                f"<tr><td>{row['value']:.1f}</td>"
                f"<td>{html.escape(scenario)}</td>"
                f"<td>{metrics['survival_rate']:.1%}</td>"
                f"<td>{metrics['mean_lifespan_ratio']:.1%}</td>"
                f"<td>{metrics['pass_rate']:.1%}</td>"
                f"<td>{metrics['shortage_rate']:.3%}</td></tr>")
    accepted_range = summary["analysis"]["accepted_nonzero_range"]
    range_text = (f"{accepted_range[0]:.1f}〜{accepted_range[1]:.1f}"
                  if accepted_range else "該当なし")
    report_path.write_text(f"""<!doctype html><html lang=ja><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<title>production_disruption 一因子検証</title><style>
:root{{color-scheme:dark;--bg:#101617;--panel:#192123;--ink:#e7ebe4;--muted:#9aa69d;--rule:#34403c;--accent:#d3a55e;--ok:#7fd6a0;--bad:#f08b8b}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,sans-serif}}main{{max-width:1300px;margin:auto;padding:24px}}.panel,.hero{{background:var(--panel);border:1px solid var(--rule);border-radius:9px;padding:16px;margin:14px 0}}.hero b{{font-size:30px;color:var(--accent)}}.muted{{color:var(--muted)}}.ok{{color:var(--ok)}}.bad{{color:var(--bad)}}.scroll{{overflow:auto}}table{{width:100%;border-collapse:collapse;white-space:nowrap}}th,td{{padding:7px;border-bottom:1px solid var(--rule);text-align:right}}th:first-child,td:first-child{{text-align:left}}</style><main>
<h1>production_disruption 一因子CPU検証</h1>
<p class=muted>config #{summary['source_config_id']}の他26値を固定 / 未使用seed / validation={summary['validation_key']}</p>
<section class=hero><span>統計・設計条件を満たす非ゼロ範囲</span><br><b>{range_text}</b><p>0.0は制度縮退中の生産低下が無いため、成績にかかわらず採用候補外。</p></section>
<section class='panel scroll'><h2>総合結果</h2><table><thead><tr><th>値</th><th>判定</th><th>生存率</th><th>95% CI</th><th>寿命比</th><th>PASS率</th><th>不足率</th><th>平均min energy</th></tr></thead><tbody>{''.join(rows)}</tbody></table></section>
<section class='panel scroll'><h2>シナリオ別</h2><table><thead><tr><th>値</th><th>シナリオ</th><th>生存率</th><th>寿命比</th><th>PASS率</th><th>不足率</th></tr></thead><tbody>{''.join(scenario_rows)}</tbody></table></section>
</main></html>""", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE_DB)
    parser.add_argument("--source-config", type=int, default=86708)
    parser.add_argument("--values", default="0.0,0.1,0.2,0.3,0.4,0.5,0.6")
    parser.add_argument("--reference-value", type=float, default=0.1)
    parser.add_argument("--seeds", default="401-500")
    parser.add_argument("--turns", type=int, default=1920)
    parser.add_argument("--workers", type=int,
                        default=max(1, os.cpu_count() or 1))
    parser.add_argument("--safety-floor", type=int, default=30)
    parser.add_argument("--talent", default="random")
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument("--progress-every", type=int, default=10)
    parser.add_argument("--severe-energy", type=float, default=-100.0)
    parser.add_argument("--catastrophic-energy", type=float, default=-1000.0)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        values = parse_values(args.values)
        seeds = batch_sweep.parse_seed_spec(args.seeds)
        source = robustness.load_source_parameters(
            args.source_db, (args.source_config,))[args.source_config]
    except ValueError as exc:
        parser.error(str(exc))
    if args.reference_value not in values:
        parser.error("reference-value must be included in values")
    if args.workers < 1 or args.turns < 1:
        parser.error("workers and turns must be positive")
    candidates = build_candidates(source, values)
    settings = {
        "source_config_id": args.source_config,
        "source_config_hash": batch_sweep.config_hash(source),
        "parameter": PARAMETER_KEY, "values": values,
        "reference_value": args.reference_value, "seeds": seeds,
        "turns": args.turns, "policies": batch_sweep.POLICIES,
        "scenarios": SCENARIOS, "safety_floor": args.safety_floor,
        "talent": args.talent, "reference_mode": "coupled",
        "severe_energy": args.severe_energy,
        "catastrophic_energy": args.catastrophic_energy,
    }
    key = experiment_key(settings)
    evaluation_count = (
        len(values) * len(seeds) * len(batch_sweep.POLICIES) * len(SCENARIOS))
    print(f"production_disruption OAT={key} / values={values}")
    print(f"evaluations={evaluation_count:,} / "
          f"turn upper bound={evaluation_count * args.turns:,} / "
          f"workers={args.workers}")
    print(f"DB={args.db} / report={args.report}")
    if args.dry_run:
        return
    plan = {"mode": "production_disruption_oat_v1", **settings}
    database = batch_sweep.SweepDatabase(args.db)
    try:
        database.initialize(
            plan, batch_sweep.compute_code_fingerprint(), candidates)
        prefix = f"production_disruption_{key}"
        robustness.run_matrix_parallel(
            database, prefix, database.all_config_ids(), args.turns, seeds,
            batch_sweep.POLICIES, args.safety_floor, args.talent,
            args.workers, args.timeout, args.progress_every, "coupled")
        analysis = analyze(
            database, prefix, values, args.reference_value,
            args.severe_energy, args.catastrophic_energy)
        summary = {
            "schema_version": 1, "validation_key": key,
            "source_config_id": args.source_config,
            "settings": settings, "analysis": analysis,
        }
        write_outputs(summary, args.summary, args.report, args.csv)
        print(f"complete: accepted_nonzero_range="
              f"{analysis['accepted_nonzero_range']} / summary={args.summary} / "
              f"report={args.report}")
    finally:
        database.close()


if __name__ == "__main__":
    main()
