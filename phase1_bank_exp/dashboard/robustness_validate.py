#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""選抜済みbarter候補を未使用seed・局所摂動・基準値分離で確認する。

この検証は新しい最適値を探索する工程ではない。事前に固定した候補について、
選抜に未使用のseedで再現するか、近傍の小さな摂動で崩れないか、初期在庫と
shortfall referenceの結合に結果が依存しないかをCPU版simulate_policyで測る。
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import hashlib
import html
import json
import math
import os
from pathlib import Path
import random
import sqlite3
import statistics
import sys
import time


PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from dashboard import batch_sweep  # noqa: E402
from dashboard.batch_worker import SCENARIOS  # noqa: E402
from dashboard.experiment_parameters import (  # noqa: E402
    PARAMETER_SPECS,
    validate_request,
)


DEFAULT_SOURCE_DB = batch_sweep.DASHBOARD_DIR / "sweeps" / "barter_matrix.sqlite3"
DEFAULT_DB = batch_sweep.DASHBOARD_DIR / "sweeps" / "barter_robustness.sqlite3"
DEFAULT_SUMMARY = (
    batch_sweep.DASHBOARD_DIR / "sweeps" / "barter_robustness_validation.json")
DEFAULT_REPORT = (
    batch_sweep.DEFAULT_REPORT_DIR / "barter_sweep_robustness_confirmatory.html")
DEFAULT_CSV = (
    batch_sweep.DEFAULT_REPORT_DIR / "barter_robustness_confirmatory.csv")
TUNABLE_SPECS = tuple(spec for spec in PARAMETER_SPECS if spec.target_attr)
TUNABLE_KEYS = tuple(spec.key for spec in TUNABLE_SPECS)


def parse_config_ids(value: str) -> tuple[int, ...]:
    ids = tuple(dict.fromkeys(
        int(part.strip()) for part in value.split(",") if part.strip()))
    if not ids or any(config_id < 1 for config_id in ids):
        raise ValueError("config IDs must be positive integers")
    return ids


def load_source_parameters(path: Path,
                           source_ids: tuple[int, ...]) -> dict[int, dict]:
    if not path.is_file():
        raise ValueError(f"source database does not exist: {path}")
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            f"SELECT id,parameters_json FROM configs WHERE id IN "
            f"({','.join('?' for _ in source_ids)})", source_ids).fetchall()
    finally:
        connection.close()
    by_id = {int(row[0]): json.loads(row[1]) for row in rows}
    missing = [config_id for config_id in source_ids if config_id not in by_id]
    if missing:
        raise ValueError(f"source configs do not exist: {missing}")
    return by_id


def _quantize_near(spec, value: float) -> int | float:
    value = max(spec.minimum, min(spec.maximum, value))
    steps = round((value - spec.minimum) / spec.step)
    quantized = spec.minimum + steps * spec.step
    decimals = len(f"{spec.step:.12f}".rstrip("0").split(".", 1)[1]) \
        if "." in f"{spec.step:.12f}".rstrip("0") else 0
    quantized = round(quantized, decimals)
    return int(quantized) if spec.kind == "integer" else quantized


def _validated_tunable(values: dict) -> dict:
    full = {"values": dict(values, seed=1, turns=1920, policy="cautious",
                            talent="random", safety_floor=30, bins=60)}
    validated = validate_request(full)["values"]
    return {key: validated[key] for key in TUNABLE_KEYS}


def generate_local_neighbors(center: dict, count: int, seed: int,
                             radius_fraction: float,
                             forbidden_hashes: set[str] | None = None) -> list[dict]:
    """全調整値を許容rangeの±radius内で揺らした有効な近傍を作る。"""
    if count < 0 or not 0.0 < radius_fraction <= 0.5:
        raise ValueError("count must be non-negative and radius must be in (0, 0.5]")
    center = _validated_tunable(center)
    rng = random.Random(seed)
    seen = set(forbidden_hashes or ())
    seen.add(batch_sweep.config_hash(center))
    neighbors = []
    attempts = 0
    while len(neighbors) < count:
        attempts += 1
        if attempts > max(10000, count * 1000):
            raise RuntimeError("could not generate enough valid local neighbors")
        row = {}
        for spec in TUNABLE_SPECS:
            span = spec.maximum - spec.minimum
            raw = center[spec.key] + rng.uniform(
                -radius_fraction * span, radius_fraction * span)
            row[spec.key] = _quantize_near(spec, raw)
        try:
            row = _validated_tunable(row)
        except ValueError:
            continue
        digest = batch_sweep.config_hash(row)
        if digest in seen:
            continue
        seen.add(digest)
        neighbors.append(row)
    return neighbors


def build_candidates(source_parameters: dict[int, dict],
                     source_ids: tuple[int, ...], neighbors_per_candidate: int,
                     neighborhood_seed: int,
                     radius_fraction: float) -> tuple[list[dict], list[dict]]:
    candidates = []
    descriptors = []
    seen = set()
    for source_id in source_ids:
        center = _validated_tunable(source_parameters[source_id])
        digest = batch_sweep.config_hash(center)
        if digest in seen:
            raise ValueError(f"duplicate source parameter set: {source_id}")
        seen.add(digest)
        candidates.append(center)
        descriptors.append({
            "source_config_id": source_id, "kind": "center",
            "neighbor_index": None, "config_hash": digest,
        })
    for source_id in source_ids:
        center = _validated_tunable(source_parameters[source_id])
        neighbors = generate_local_neighbors(
            center, neighbors_per_candidate,
            neighborhood_seed + source_id * 1009, radius_fraction, seen)
        for index, row in enumerate(neighbors, 1):
            digest = batch_sweep.config_hash(row)
            seen.add(digest)
            candidates.append(row)
            descriptors.append({
                "source_config_id": source_id, "kind": "neighbor",
                "neighbor_index": index, "config_hash": digest,
            })
    return candidates, descriptors


def validation_key(settings: dict) -> str:
    payload = json.dumps(settings, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def attach_local_ids(database: batch_sweep.SweepDatabase,
                     descriptors: list[dict]) -> list[dict]:
    ids = {row["config_hash"]: row["id"] for row in database.connection.execute(
        "SELECT id,config_hash FROM configs")}
    return [dict(row, local_config_id=ids[row["config_hash"]])
            for row in descriptors]


def _matrix_stage_names(prefix: str) -> tuple[str, ...]:
    return tuple(f"{prefix}_{scenario}" for scenario in SCENARIOS)


def run_matrix_parallel(database: batch_sweep.SweepDatabase, prefix: str,
                        candidate_ids: list[int], turns: int,
                        seeds: tuple[int, ...], policies: tuple[str, ...],
                        safety_floor: int, talent: str, workers: int,
                        timeout: int, progress_every: int,
                        reference_mode: str) -> str:
    """config×scenarioを同時投入し、少数centerでも全coreを使う。"""
    if reference_mode not in ("coupled", "fixed"):
        raise ValueError("reference_mode must be coupled or fixed")
    tasks = []
    for scenario in SCENARIOS:
        stage = batch_sweep.StageSpec(
            f"{prefix}_{scenario}", turns, seeds, None, scenario)
        pending = database.pending_ids(stage.name, candidate_ids)
        tasks.extend((config_id, stage) for config_id in pending)
    total = len(candidate_ids) * len(SCENARIOS)
    print(f"\n[{prefix}] {turns} turns / {len(seeds)} seeds / "
          f"{len(policies)} policies / {reference_mode} references / "
          f"tasks={total} (resume済み={total - len(tasks)})")
    if tasks:
        started = time.perf_counter()
        failures = []
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="robust") as executor:
            futures = {}
            pending = iter(tasks)

            def submit_next():
                try:
                    config_id, stage = next(pending)
                except StopIteration:
                    return
                database.mark_running(config_id, stage.name)
                future = executor.submit(
                    batch_sweep.run_worker,
                    database.config_parameters(config_id), stage, policies,
                    safety_floor, talent, timeout, reference_mode)
                futures[future] = (config_id, stage)

            for _ in range(min(len(tasks), workers * 2)):
                submit_next()
            done = 0
            while futures:
                future = next(as_completed(tuple(futures)))
                config_id, stage = futures.pop(future)
                done += 1
                try:
                    evaluations = future.result()
                    database.save_success(
                        config_id, stage.name, evaluations,
                        batch_sweep.aggregate_evaluations(evaluations))
                except Exception as exc:
                    database.save_failure(config_id, stage.name, str(exc))
                    failures.append((config_id, stage.name, str(exc)))
                submit_next()
                if done % max(1, progress_every) == 0 or done == len(tasks):
                    elapsed = time.perf_counter() - started
                    rate = done / elapsed if elapsed else 0.0
                    remaining = (len(tasks) - done) / rate if rate else 0.0
                    print(f"  {done}/{len(tasks)} task / {rate:.2f} task/s / "
                          f"ETA {remaining / 60:.1f} min / "
                          f"DB {batch_sweep._human_bytes(database.database_bytes())}")
        if failures:
            sample = "; ".join(
                f"#{cid}/{stage}: {error}" for cid, stage, error in failures[:3])
            raise RuntimeError(f"{len(failures)} tasks failed: {sample}")
    combined = f"{prefix}_all"
    for config_id in candidate_ids:
        evaluations = database.evaluation_summaries(
            config_id, _matrix_stage_names(prefix))
        if not evaluations:
            raise RuntimeError(f"#{config_id}: no evaluations for {prefix}")
        database.save_aggregate_only(
            config_id, combined,
            batch_sweep.aggregate_evaluations(evaluations))
    rows = database.completed_stage_rows(combined)
    scored, selected = batch_sweep.score_and_select(rows, len(rows))
    database.apply_scoring(combined, scored, selected)
    return combined


def wilson_interval(successes: int, total: int,
                    z: float = 1.959963984540054) -> tuple[float, float]:
    if total <= 0:
        return 0.0, 0.0
    proportion = successes / total
    denominator = 1.0 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    radius = z * math.sqrt(
        proportion * (1 - proportion) / total + z * z / (4 * total * total)
    ) / denominator
    return max(0.0, center - radius), min(1.0, center + radius)


def mean_interval(values: list[float],
                  z: float = 1.959963984540054) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    mean = statistics.fmean(values)
    if len(values) == 1:
        return mean, mean
    radius = z * statistics.stdev(values) / math.sqrt(len(values))
    return mean - radius, mean + radius


def quantile(values: list[float], probability: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    left = math.floor(position)
    right = math.ceil(position)
    if left == right:
        return ordered[left]
    return ordered[left] + (ordered[right] - ordered[left]) * (position - left)


def summarize_exact(evaluations: list[dict], severe_energy: float,
                    catastrophic_energy: float) -> dict:
    aggregate = batch_sweep.aggregate_evaluations(evaluations)
    survival = wilson_interval(
        sum(row["survived"] for row in evaluations), len(evaluations))
    lifespan_values = [
        row["observed_turns"] / row["turns_requested"] for row in evaluations]
    pass_values = []
    for row in evaluations:
        tally = row["criteria_tally"]
        denominator = tally["pass"] + tally["fail"]
        pass_values.append(tally["pass"] / denominator if denominator else 0.0)
    energies = [row["min_seen"].get("energy", 0.0) for row in evaluations]
    catastrophic_count = sum(value < catastrophic_energy for value in energies)
    energy = {
        "mean_min_energy": statistics.fmean(energies),
        "worst_min_energy": min(energies),
        "severe_run_rate": sum(value < severe_energy for value in energies)
        / len(energies),
        "catastrophic_run_rate": catastrophic_count / len(energies),
    }
    # baselineの旧finalで平均min energy=-70.6だったため、平均-100を健全性の
    # 下限にする。単発の破局も1%を越えれば探索上の抜け道とみなす。
    energy["guard_pass"] = (
        energy["mean_min_energy"] >= severe_energy
        and energy["catastrophic_run_rate"] <= 0.01)
    aggregate.update({
        "survival_ci95": list(survival),
        "lifespan_ci95": list(mean_interval(lifespan_values)),
        "run_pass_rate": statistics.fmean(pass_values),
        "run_pass_rate_ci95": list(mean_interval(pass_values)),
        "energy_guard": energy,
    })
    return aggregate


def _by_scenario(evaluations: list[dict], severe_energy: float,
                 catastrophic_energy: float) -> dict[str, dict]:
    result = {}
    for scenario in SCENARIOS:
        rows = [row for row in evaluations if row["scenario"] == scenario]
        result[scenario] = summarize_exact(
            rows, severe_energy, catastrophic_energy)
    return result


def build_confirmation(primary: dict, baseline: dict,
                       primary_scenarios: dict, baseline_scenarios: dict,
                       pass_drop_limit: float = 0.05,
                       scenario_lifespan_drop_limit: float = 0.05) -> dict:
    checks = {
        "survival_ci_above_baseline": (
            primary["survival_ci95"][0] > baseline["survival_ci95"][1]),
        "lifespan_ci_above_baseline": (
            primary["lifespan_ci95"][0] > baseline["lifespan_ci95"][1]),
        "pass_rate_drop_within_limit": (
            primary["pass_rate"] >= baseline["pass_rate"] - pass_drop_limit),
        "shortage_not_worse": (
            primary["shortage_rate"]
            <= max(0.005, baseline["shortage_rate"] * 1.25)),
        "energy_guard": primary["energy_guard"]["guard_pass"],
        "no_scenario_lifespan_cliff": all(
            primary_scenarios[name]["mean_lifespan_ratio"]
            >= baseline_scenarios[name]["mean_lifespan_ratio"]
            - scenario_lifespan_drop_limit for name in SCENARIOS),
    }
    return {"passed": all(checks.values()), "checks": checks}


def _describe(rows: list[dict], key: str) -> dict:
    values = [row[key] for row in rows]
    return {
        "min": min(values), "p10": quantile(values, 0.10),
        "median": quantile(values, 0.50), "p90": quantile(values, 0.90),
        "max": max(values),
    }


def analyze_sensitivity(database: batch_sweep.SweepDatabase,
                        stage_prefix: str, descriptors: list[dict],
                        severe_energy: float,
                        catastrophic_energy: float) -> tuple[dict, dict[int, list[dict]]]:
    names = _matrix_stage_names(stage_prefix)
    metrics_by_local = {}
    for descriptor in descriptors:
        local_id = descriptor["local_config_id"]
        metrics_by_local[local_id] = summarize_exact(
            database.evaluation_summaries(local_id, names),
            severe_energy, catastrophic_energy)
    groups = {}
    accepted_by_source = {}
    for source_id in sorted({row["source_config_id"] for row in descriptors}):
        members = [row for row in descriptors
                   if row["source_config_id"] == source_id]
        center_descriptor = next(row for row in members if row["kind"] == "center")
        center = metrics_by_local[center_descriptor["local_config_id"]]
        neighbor_descriptors = [row for row in members if row["kind"] == "neighbor"]
        neighbor_metrics = [
            dict(metrics_by_local[row["local_config_id"]], descriptor=row)
            for row in neighbor_descriptors]
        accepted = []
        for row in neighbor_metrics:
            okay = (
                row["energy_guard"]["guard_pass"]
                and row["survival_rate"] >= center["survival_rate"] - 0.10
                and row["mean_lifespan_ratio"]
                >= center["mean_lifespan_ratio"] - 0.05
                and row["pass_rate"] >= center["pass_rate"] - 0.05
                and row["shortage_rate"]
                <= max(0.005, center["shortage_rate"] * 2.0))
            row["locally_acceptable"] = okay
            if okay:
                accepted.append(row["descriptor"])
        accepted_rate = len(accepted) / len(neighbor_metrics) if neighbor_metrics else 1.0
        groups[source_id] = {
            "center": center,
            "neighbor_count": len(neighbor_metrics),
            "acceptable_count": len(accepted),
            "acceptable_rate": accepted_rate,
            "stable": accepted_rate >= 0.75,
            "distributions": {
                key: _describe(neighbor_metrics, key) for key in (
                    "survival_rate", "mean_lifespan_ratio", "pass_rate",
                    "shortage_rate", "mean_min_energy")
            },
        }
        accepted_by_source[source_id] = accepted
    return groups, accepted_by_source


def parameter_ranges(database: batch_sweep.SweepDatabase,
                     center_descriptor: dict,
                     accepted_descriptors: list[dict]) -> dict:
    ids = [center_descriptor["local_config_id"]] + [
        row["local_config_id"] for row in accepted_descriptors]
    values = [database.config_parameters(config_id) for config_id in ids]
    result = {}
    for key in TUNABLE_KEYS:
        rows = [float(row[key]) for row in values]
        result[key] = {
            "min": min(rows), "p25": quantile(rows, 0.25),
            "median": quantile(rows, 0.50), "p75": quantile(rows, 0.75),
            "max": max(rows),
        }
    return result


def build_summary(database: batch_sweep.SweepDatabase, key: str,
                  settings: dict, descriptors: list[dict],
                  holdout_prefix: str, reference_prefix: str | None,
                  sensitivity_prefix: str | None,
                  primary_id: int, baseline_id: int,
                  severe_energy: float, catastrophic_energy: float) -> dict:
    centers = {row["source_config_id"]: row for row in descriptors
               if row["kind"] == "center"}
    holdout = {}
    holdout_scenarios = {}
    for source_id, descriptor in centers.items():
        evaluations = database.evaluation_summaries(
            descriptor["local_config_id"], _matrix_stage_names(holdout_prefix))
        holdout[source_id] = summarize_exact(
            evaluations, severe_energy, catastrophic_energy)
        holdout_scenarios[source_id] = _by_scenario(
            evaluations, severe_energy, catastrophic_energy)
    reference = {}
    if reference_prefix:
        for source_id, descriptor in centers.items():
            evaluations = database.evaluation_summaries(
                descriptor["local_config_id"],
                _matrix_stage_names(reference_prefix))
            fixed = summarize_exact(evaluations, severe_energy, catastrophic_energy)
            reference[source_id] = {
                "coupled": holdout[source_id], "fixed": fixed,
                "delta": {
                    key: fixed[key] - holdout[source_id][key] for key in (
                        "survival_rate", "mean_lifespan_ratio", "pass_rate",
                        "shortage_rate", "mean_min_energy")
                },
            }
    sensitivity = {}
    ranges = {}
    if sensitivity_prefix:
        sensitivity, accepted = analyze_sensitivity(
            database, sensitivity_prefix, descriptors,
            severe_energy, catastrophic_energy)
        primary_descriptor = centers[primary_id]
        ranges = parameter_ranges(
            database, primary_descriptor, accepted[primary_id])
    confirmation = build_confirmation(
        holdout[primary_id], holdout[baseline_id],
        holdout_scenarios[primary_id], holdout_scenarios[baseline_id])
    return {
        "schema_version": 1,
        "validation_key": key,
        "purpose": "confirmatory robustness; not a new optimization pass",
        "settings": settings,
        "primary_source_config_id": primary_id,
        "baseline_source_config_id": baseline_id,
        "confirmation": confirmation,
        "holdout": {str(key): value for key, value in holdout.items()},
        "holdout_by_scenario": {
            str(key): value for key, value in holdout_scenarios.items()},
        "reference_coupling": {str(key): value for key, value in reference.items()},
        "sensitivity": {str(key): value for key, value in sensitivity.items()},
        "primary_recommended_ranges": ranges,
        "descriptors": descriptors,
    }


def _pct(value: float) -> str:
    return f"{value:.1%}"


def write_outputs(summary: dict, summary_path: Path, report_path: Path,
                  csv_path: Path) -> None:
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(
        summary, ensure_ascii=False, indent=2), encoding="utf-8")
    holdout = summary["holdout"]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fields = [
            "source_config_id", "survival_rate", "survival_ci_low",
            "survival_ci_high", "mean_lifespan_ratio", "pass_rate",
            "shortage_rate", "mean_min_energy", "worst_min_energy",
            "catastrophic_run_rate", "energy_guard_pass",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for source_id, row in holdout.items():
            writer.writerow({
                "source_config_id": source_id,
                "survival_rate": row["survival_rate"],
                "survival_ci_low": row["survival_ci95"][0],
                "survival_ci_high": row["survival_ci95"][1],
                "mean_lifespan_ratio": row["mean_lifespan_ratio"],
                "pass_rate": row["pass_rate"],
                "shortage_rate": row["shortage_rate"],
                "mean_min_energy": row["energy_guard"]["mean_min_energy"],
                "worst_min_energy": row["energy_guard"]["worst_min_energy"],
                "catastrophic_run_rate": row["energy_guard"]["catastrophic_run_rate"],
                "energy_guard_pass": row["energy_guard"]["guard_pass"],
            })
    checks = summary["confirmation"]["checks"]
    check_rows = "".join(
        f"<li class={'ok' if passed else 'bad'}>{'PASS' if passed else 'FAIL'} — "
        f"{html.escape(name)}</li>" for name, passed in checks.items())
    holdout_rows = []
    for source_id, row in holdout.items():
        energy = row["energy_guard"]
        holdout_rows.append(
            f"<tr><td>#{source_id}</td><td>{_pct(row['survival_rate'])}</td>"
            f"<td>{_pct(row['survival_ci95'][0])}–{_pct(row['survival_ci95'][1])}</td>"
            f"<td>{_pct(row['mean_lifespan_ratio'])}</td>"
            f"<td>{_pct(row['pass_rate'])}</td><td>{_pct(row['shortage_rate'])}</td>"
            f"<td>{energy['mean_min_energy']:.2f}</td>"
            f"<td class={'ok' if energy['guard_pass'] else 'bad'}>"
            f"{'PASS' if energy['guard_pass'] else 'FAIL'}</td></tr>")
    sensitivity_rows = []
    for source_id, row in summary["sensitivity"].items():
        survival = row["distributions"]["survival_rate"]
        sensitivity_rows.append(
            f"<tr><td>#{source_id}</td><td>{row['acceptable_count']}/"
            f"{row['neighbor_count']}</td><td>{_pct(row['acceptable_rate'])}</td>"
            f"<td>{_pct(survival['p10'])} / {_pct(survival['median'])} / "
            f"{_pct(survival['p90'])}</td>"
            f"<td class={'ok' if row['stable'] else 'bad'}>"
            f"{'安定' if row['stable'] else '不安定'}</td></tr>")
    reference_rows = []
    for source_id, row in summary["reference_coupling"].items():
        delta = row["delta"]
        reference_rows.append(
            f"<tr><td>#{source_id}</td>"
            f"<td>{delta['survival_rate']:+.2%}</td>"
            f"<td>{delta['mean_lifespan_ratio']:+.2%}</td>"
            f"<td>{delta['pass_rate']:+.2%}</td>"
            f"<td>{delta['shortage_rate']:+.3%}</td>"
            f"<td>{delta['mean_min_energy']:+.2f}</td></tr>")
    range_rows = []
    for key, row in summary["primary_recommended_ranges"].items():
        range_rows.append(
            f"<tr><td>{html.escape(key)}</td><td>{row['min']:g}</td>"
            f"<td>{row['p25']:g}</td><td>{row['median']:g}</td>"
            f"<td>{row['p75']:g}</td><td>{row['max']:g}</td></tr>")
    verdict = summary["confirmation"]["passed"]
    report_path.write_text(f"""<!doctype html><html lang=ja><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<title>Barter confirmatory robustness</title><style>
:root{{color-scheme:dark;--bg:#101617;--panel:#192123;--ink:#e7ebe4;--muted:#9aa69d;--rule:#34403c;--accent:#d3a55e;--ok:#7fd6a0;--bad:#f08b8b}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,sans-serif}}main{{max-width:1400px;margin:auto;padding:24px}}h1,h2{{margin-top:0}}.hero,.panel{{background:var(--panel);border:1px solid var(--rule);border-radius:9px;padding:16px;margin:14px 0}}.hero b{{font-size:28px;color:{'var(--ok)' if verdict else 'var(--bad)'}}}.muted{{color:var(--muted)}}.ok{{color:var(--ok)}}.bad{{color:var(--bad)}}table{{width:100%;border-collapse:collapse;white-space:nowrap}}th,td{{padding:7px;border-bottom:1px solid var(--rule);text-align:right}}th:first-child,td:first-child{{text-align:left}}.scroll{{overflow:auto}}code{{color:var(--accent)}}</style><main>
<h1>物々交換パラメータ 頑健性検証</h1><p class=muted>validation={html.escape(summary['validation_key'])} / 未使用seedによる確認。新しい最適化には使用しない。</p>
<section class=hero><span>事前指定primary #{summary['primary_source_config_id']}</span><br><b>{'CONFIRMED' if verdict else 'NOT CONFIRMED'}</b><ul>{check_rows}</ul></section>
<section class='panel scroll'><h2>未使用seedの総合結果</h2><table><thead><tr><th>source</th><th>生存率</th><th>95% CI</th><th>寿命比</th><th>PASS率</th><th>不足率</th><th>平均min energy</th><th>energy guard</th></tr></thead><tbody>{''.join(holdout_rows)}</tbody></table></section>
<section class='panel scroll'><h2>局所感度</h2><p class=muted>各中心の近傍でenergy健全性・生存・寿命・PASS・不足の許容条件を同時に満たした割合。</p><table><thead><tr><th>source</th><th>許容近傍</th><th>割合</th><th>生存率 p10 / median / p90</th><th>判定</th></tr></thead><tbody>{''.join(sensitivity_rows)}</tbody></table></section>
<section class='panel scroll'><h2>初期在庫とshortfall referenceの分離感度</h2><p class=muted>fixed reference − 現行coupled。同一未使用seedで比較。</p><table><thead><tr><th>source</th><th>生存率Δ</th><th>寿命比Δ</th><th>PASS率Δ</th><th>不足率Δ</th><th>min energyΔ</th></tr></thead><tbody>{''.join(reference_rows)}</tbody></table></section>
<section class='panel scroll'><h2>primaryの許容近傍レンジ</h2><table><thead><tr><th>parameter</th><th>min</th><th>p25</th><th>median</th><th>p75</th><th>max</th></tr></thead><tbody>{''.join(range_rows)}</tbody></table></section>
</main></html>""", encoding="utf-8")
    # NASの/sweeps一覧にも通常レポートと同じ形で現れるようindexを再構築する。
    html_files = sorted(report_path.parent.glob("*.html"))
    links = "".join(
        f'<li><a href="{html.escape(path.name)}">{html.escape(path.stem)}</a></li>'
        for path in html_files if path.name != "index.html")
    (report_path.parent / "index.html").write_text(
        "<!doctype html><html lang=ja><meta charset=utf-8><meta name=viewport "
        "content='width=device-width,initial-scale=1'><title>走査レポート</title>"
        "<style>body{font:16px system-ui;max-width:900px;margin:40px auto;"
        "padding:0 20px}li{margin:12px 0}</style><h1>パラメータ走査レポート"
        "</h1><ul>" + links + "</ul></html>", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE_DB)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--source-candidates",
                        default="86708,38130,50126,32400,1")
    parser.add_argument("--primary", type=int, default=86708)
    parser.add_argument("--baseline", type=int, default=1)
    parser.add_argument("--turns", type=int, default=1920)
    parser.add_argument("--holdout-seeds", default="101-200")
    parser.add_argument("--sensitivity-seeds", default="201-220")
    parser.add_argument("--neighbors", type=int, default=24)
    parser.add_argument("--radius", type=float, default=0.05,
                        help="各parameterの許容rangeに対する局所摂動半径")
    parser.add_argument("--neighborhood-seed", type=int, default=20260818)
    parser.add_argument("--workers", type=int,
                        default=max(1, os.cpu_count() or 1))
    parser.add_argument("--safety-floor", type=int, default=30)
    parser.add_argument("--talent",
                        choices=("random", "dexterity", "intellect", "skill", "health"),
                        default="random")
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument("--progress-every", type=int, default=20)
    parser.add_argument("--severe-energy", type=float, default=-100.0)
    parser.add_argument("--catastrophic-energy", type=float, default=-1000.0)
    parser.add_argument("--skip-reference-check", action="store_true")
    parser.add_argument("--stop-after",
                        choices=("holdout", "reference", "sensitivity"),
                        default="sensitivity")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        source_ids = parse_config_ids(args.source_candidates)
        holdout_seeds = batch_sweep.parse_seed_spec(args.holdout_seeds)
        sensitivity_seeds = batch_sweep.parse_seed_spec(args.sensitivity_seeds)
        source_parameters = load_source_parameters(args.source_db, source_ids)
    except ValueError as exc:
        parser.error(str(exc))
    if args.primary not in source_ids or args.baseline not in source_ids:
        parser.error("primary and baseline must be included in source-candidates")
    if args.workers < 1 or args.turns < 1 or args.neighbors < 0:
        parser.error("workers/turns must be positive and neighbors non-negative")
    if not 0.0 < args.radius <= 0.5:
        parser.error("radius must be in (0, 0.5]")
    if args.catastrophic_energy >= args.severe_energy:
        parser.error("catastrophic-energy must be lower than severe-energy")
    candidates, descriptors = build_candidates(
        source_parameters, source_ids, args.neighbors,
        args.neighborhood_seed, args.radius)
    settings = {
        "source_config_ids": source_ids,
        "source_config_hashes": [batch_sweep.config_hash(source_parameters[x])
                                 for x in source_ids],
        "primary": args.primary, "baseline": args.baseline,
        "turns": args.turns, "holdout_seeds": holdout_seeds,
        "sensitivity_seeds": sensitivity_seeds,
        "neighbors_per_candidate": args.neighbors,
        "radius_fraction": args.radius,
        "neighborhood_seed": args.neighborhood_seed,
        "policies": batch_sweep.POLICIES, "scenarios": SCENARIOS,
        "safety_floor": args.safety_floor, "talent": args.talent,
        "severe_energy": args.severe_energy,
        "catastrophic_energy": args.catastrophic_energy,
        "reference_check": not args.skip_reference_check,
    }
    key = validation_key(settings)
    center_count = len(source_ids)
    sensitivity_count = len(candidates)
    holdout_evaluations = (
        center_count * len(SCENARIOS) * len(holdout_seeds)
        * len(batch_sweep.POLICIES))
    sensitivity_evaluations = (
        sensitivity_count * len(SCENARIOS) * len(sensitivity_seeds)
        * len(batch_sweep.POLICIES))
    total_evaluations = holdout_evaluations + sensitivity_evaluations
    if not args.skip_reference_check:
        total_evaluations += holdout_evaluations
    print(f"robustness={key} / centers={center_count} / "
          f"neighbors={sensitivity_count - center_count}")
    print(f"evaluations={total_evaluations:,} / "
          f"turn upper bound={total_evaluations * args.turns:,} / "
          f"workers={args.workers}")
    print(f"DB={args.db} / report={args.report}")
    if args.dry_run:
        return
    plan = {"mode": "barter_confirmatory_robustness_v1", **settings}
    database = batch_sweep.SweepDatabase(args.db)
    try:
        database.initialize(
            plan, batch_sweep.compute_code_fingerprint(), candidates)
        descriptors = attach_local_ids(database, descriptors)
        centers = [row["local_config_id"] for row in descriptors
                   if row["kind"] == "center"]
        all_ids = [row["local_config_id"] for row in descriptors]
        prefix = f"robust_{key}"
        holdout_prefix = f"{prefix}_holdout_coupled"
        run_matrix_parallel(
            database, holdout_prefix, centers, args.turns, holdout_seeds,
            batch_sweep.POLICIES, args.safety_floor, args.talent,
            args.workers, args.timeout, args.progress_every, "coupled")
        if args.stop_after == "holdout":
            reference_prefix = None
            sensitivity_prefix = None
        else:
            reference_prefix = None
            if not args.skip_reference_check:
                reference_prefix = f"{prefix}_holdout_fixed"
                run_matrix_parallel(
                    database, reference_prefix, centers, args.turns,
                    holdout_seeds, batch_sweep.POLICIES, args.safety_floor,
                    args.talent, args.workers, args.timeout,
                    args.progress_every, "fixed")
            sensitivity_prefix = None
            if args.stop_after == "sensitivity":
                sensitivity_prefix = f"{prefix}_sensitivity_coupled"
                run_matrix_parallel(
                    database, sensitivity_prefix, all_ids, args.turns,
                    sensitivity_seeds, batch_sweep.POLICIES,
                    args.safety_floor, args.talent, args.workers,
                    args.timeout, args.progress_every, "coupled")
        summary = build_summary(
            database, key, settings, descriptors, holdout_prefix,
            reference_prefix, sensitivity_prefix, args.primary, args.baseline,
            args.severe_energy, args.catastrophic_energy)
        write_outputs(summary, args.summary, args.report, args.csv)
        print(f"complete: confirmed={summary['confirmation']['passed']} / "
              f"summary={args.summary} / report={args.report}")
    finally:
        database.close()


if __name__ == "__main__":
    main()
