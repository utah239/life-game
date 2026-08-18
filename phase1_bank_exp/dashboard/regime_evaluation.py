#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""制度レジームの挙動だけで物々交換候補を比較する評価器。

生存率、寿命、health、energyは結末を説明する参考値として集計するが、
profile score、Pareto front、候補選抜には一切使わない。絶滅を失敗として
最適化せず、制度経路の分岐、行動への反映、復旧、過剰往復の少なさを評価する。
"""
from __future__ import annotations

from collections import Counter
import itertools
import math
import statistics


EVALUATION_VERSION = "regime_structural_v2"
SEVERE_ENERGY_FLOOR = -100.0
CATASTROPHIC_ENERGY_FLOOR = -1000.0
PROFILE_NAMES = (
    "structural_balance",
    "regime_diversity",
    "behavior",
    "recovery",
    "collapse_expression",
)

# policy-checkのうち、生存状態を直接目的化する項目。N/Aを含む他の制度基準は
# neutral=0.5として扱い、早期終了で未観測項目が増えても得点が上がらないようにする。
REFERENCE_ONLY_CRITERION_PREFIXES = ("生存(", "health(")


def _mean(values) -> float:
    values = list(values)
    return statistics.fmean(values) if values else 0.0


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _normalized_entropy(values, cardinality: int) -> float:
    values = list(values)
    if not values or cardinality <= 1:
        return 0.0
    counts = Counter(values)
    entropy = -sum(
        (count / len(values)) * math.log(count / len(values))
        for count in counts.values())
    return _clamp01(entropy / math.log(cardinality))


def _mean_pairwise_distance(values: list[float], span: float) -> float:
    if len(values) < 2 or span <= 0.0:
        return 0.0
    distances = [abs(left - right) / span
                 for left, right in itertools.combinations(values, 2)]
    return _clamp01(_mean(distances))


def is_reference_only_criterion(row: dict) -> bool:
    name = str(row.get("name", ""))
    return name.startswith(REFERENCE_ONLY_CRITERION_PREFIXES)


def _structural_criteria(evaluations: list[dict]) -> dict:
    rows = [criterion for evaluation in evaluations
            for criterion in evaluation.get("criteria", ())
            if not is_reference_only_criterion(criterion)]
    passed = sum(row.get("passed") is True for row in rows)
    failed = sum(row.get("passed") is False for row in rows)
    na = sum(row.get("passed") is None for row in rows)
    total = passed + failed + na
    return {
        "pass": passed,
        "fail": failed,
        "na": na,
        # N/Aを除外すると早期終了や制度不成立が有利になるため中立点を与える。
        "score": ((passed + 0.5 * na) / total if total else 0.5),
        "applicability_rate": ((passed + failed) / total if total else 0.0),
    }


def _policy_choice_diversity(evaluations: list[dict]) -> float:
    keys = sorted({key for row in evaluations
                   for key in row.get("normal_counts", {})})
    by_policy = {}
    for row in evaluations:
        bucket = by_policy.setdefault(row.get("policy", "unknown"), Counter())
        bucket.update(row.get("normal_counts", {}))
    shares = {}
    for policy, counts in by_policy.items():
        total = sum(counts.values())
        shares[policy] = [counts.get(key, 0) / total if total else 0.0
                          for key in keys]
    distances = []
    for left, right in itertools.combinations(sorted(shares), 2):
        distances.append(sum(abs(a - b) for a, b in zip(
            shares[left], shares[right])) / 2.0)
    return _clamp01(_mean(distances))


def _all_institution_diversity(evaluations: list[dict]) -> float:
    names = sorted({name for row in evaluations
                    for name in row.get("institutions", {})})
    scores = []
    for name in names:
        stages = [tracking.get("final_stage", 0) for row in evaluations
                  if (tracking := row.get("institutions", {}).get(name))]
        cardinality = max(2, max(stages, default=0) + 1)
        scores.append(_normalized_entropy(stages, cardinality))
    return _mean(scores)


def _group_mean_stage(evaluations: list[dict], group_key: str) -> list[float]:
    grouped = {}
    for row in evaluations:
        tracking = row.get("institutions", {}).get("barter", {})
        grouped.setdefault(row.get(group_key, "unknown"), []).append(
            float(tracking.get("final_stage", 0)))
    return [_mean(values) for _, values in sorted(grouped.items())]


def aggregate_regime_evaluations(evaluations: list[dict]) -> dict:
    """生存結果を採点せず、制度挙動と参考値を分離して集約する。"""
    if not evaluations:
        raise ValueError("evaluations must not be empty")
    requested_turns = int(evaluations[0].get("turns_requested", 0))
    observed_total = sum(max(0, int(row.get("observed_turns", 0)))
                         for row in evaluations)
    active_rows = [row for row in evaluations if row.get("barter_active")]
    active_used = sum(
        row.get("normal_counts", {}).get("barter", 0)
        + row.get("normal_counts", {}).get("subsistence", 0) > 0
        for row in active_rows)
    normal_total = sum(sum(row.get("normal_counts", {}).values())
                       for row in evaluations)
    alternative_total = sum(
        row.get("normal_counts", {}).get("barter", 0)
        + row.get("normal_counts", {}).get("subsistence", 0)
        for row in evaluations)

    barter_tracking = [row.get("institutions", {}).get("barter", {})
                       for row in evaluations]
    final_stages = [int(row.get("final_stage", 0)) for row in barter_tracking]
    worst_stages = [int(row.get("worst_stage", row.get("final_stage", 0)))
                    for row in barter_tracking]
    transitions = sum(int(row.get("transition_count", 0))
                      for row in barter_tracking)
    recoveries = sum(int(row.get("recovery_count", 0))
                     for row in barter_tracking)
    rapid_recrosses = sum(int(row.get("rapid_same_boundary_recross_count", 0))
                          for row in barter_tracking)
    transitioned = [row for row in barter_tracking
                    if int(row.get("transition_count", 0)) > 0]
    recovered = sum(int(row.get("recovery_count", 0)) > 0
                    for row in transitioned)
    transition_rate = transitions / observed_total * 100.0 if observed_total else 0.0
    recross_rate = rapid_recrosses / observed_total * 100.0 if observed_total else 0.0
    stage3_rows = [row for row, worst in zip(evaluations, worst_stages)
                   if worst >= 3]
    stage3_penalty_consistency = (
        sum(row.get("barter_shortage_penalty_applied_count", 0) > 0
            for row in stage3_rows) / len(stage3_rows)
        if stage3_rows else 0.0)

    criteria = _structural_criteria(evaluations)
    activation_rate = len(active_rows) / len(evaluations)
    usage_coverage = active_used / len(active_rows) if active_rows else 0.0
    alternative_choice_rate = (
        alternative_total / normal_total if normal_total else 0.0)
    choice_balance = 1.0 - abs(2.0 * alternative_choice_rate - 1.0)
    scenario_separation = _mean_pairwise_distance(
        _group_mean_stage(evaluations, "scenario"), 3.0)
    policy_regime_separation = _mean_pairwise_distance(
        _group_mean_stage(evaluations, "policy"), 3.0)

    reference_survival_rate = _mean(
        bool(row.get("survived")) for row in evaluations)
    reference_lifespan_ratio = _mean(
        (row.get("observed_turns", 0) / requested_turns
         if requested_turns else 0.0) for row in evaluations)
    reference_mean_health = _mean(
        row.get("traits", {}).get("health", 0.0) for row in evaluations)
    reference_mean_min_energy = _mean(
        row.get("min_seen", {}).get("energy", 0.0) for row in evaluations)
    energy_values = [float(row.get("min_seen", {}).get("energy", 0.0))
                     for row in evaluations]
    finite_values = all(math.isfinite(value) for value in energy_values)
    catastrophic_rate = (
        sum(value < CATASTROPHIC_ENERGY_FLOOR for value in energy_values)
        / len(energy_values))
    numerical_guard = {
        "passed": (
            finite_values
            and reference_mean_min_energy >= SEVERE_ENERGY_FLOOR
            and catastrophic_rate <= 0.01),
        "all_finite": finite_values,
        "mean_min_energy": reference_mean_min_energy,
        "worst_min_energy": min(energy_values),
        "catastrophic_run_rate": catastrophic_rate,
        "severe_floor": SEVERE_ENERGY_FLOOR,
        "catastrophic_floor": CATASTROPHIC_ENERGY_FLOOR,
    }

    return {
        "evaluation_version": EVALUATION_VERSION,
        "evaluation_count": len(evaluations),
        "turns": requested_turns,
        "structural_criteria_score": criteria["score"],
        "structural_criteria_applicability": criteria["applicability_rate"],
        "structural_criteria_tally": {
            key: criteria[key] for key in ("pass", "fail", "na")},
        "policy_diversity": _policy_choice_diversity(evaluations),
        "institution_regime_diversity": _all_institution_diversity(evaluations),
        "barter_final_stage_entropy": _normalized_entropy(final_stages, 4),
        "barter_worst_stage_entropy": _normalized_entropy(worst_stages, 4),
        "scenario_regime_separation": scenario_separation,
        "policy_regime_separation": policy_regime_separation,
        "activation_rate": activation_rate,
        "active_usage_coverage": usage_coverage,
        "alternative_observation_score": activation_rate * usage_coverage,
        "alternative_choice_rate": alternative_choice_rate,
        "alternative_choice_balance": _clamp01(choice_balance),
        "transition_presence_rate": len(transitioned) / len(evaluations),
        "transition_rate_per_100_turns": transition_rate,
        "recovery_presence_rate": (
            recovered / len(transitioned) if transitioned else 0.0),
        "recovery_ratio": recoveries / transitions if transitions else 0.0,
        "rapid_recross_rate_per_100_turns": recross_rate,
        "chatter_score": 1.0 / (1.0 + recross_rate),
        "stage3_reach_rate": len(stage3_rows) / len(evaluations),
        "stage3_penalty_consistency": stage3_penalty_consistency,
        "shortage_exposure_rate": (
            sum(row.get("barter_shortage_penalty_applied_count", 0)
                for row in evaluations) / observed_total
            if observed_total else 0.0),
        # 結末の良し悪しではなく、有限値と資源境界の破綻だけを検出するhard guard。
        # profile scoreには含めない。
        "numerical_guard": numerical_guard,
        "reference_outcomes": {
            "survival_rate": reference_survival_rate,
            "mean_lifespan_ratio": reference_lifespan_ratio,
            "mean_final_health": reference_mean_health,
            "mean_min_energy": reference_mean_min_energy,
            "observed_turns": observed_total,
        },
    }


def _percentile_ranks(rows: list[dict], key: str) -> dict[int, float]:
    ordered = sorted((float(row[key]), int(row["config_id"])) for row in rows)
    if len(ordered) == 1:
        return {ordered[0][1]: 1.0}
    ranks = {}
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][0] == ordered[index][0]:
            end += 1
        rank = ((index + end - 1) / 2.0) / (len(ordered) - 1)
        for _, config_id in ordered[index:end]:
            ranks[config_id] = rank
        index = end
    return ranks


PROFILE_WEIGHTS = {
    "structural_balance": {
        "structural_criteria_score": 0.15,
        "policy_diversity": 0.15,
        "barter_final_stage_entropy": 0.15,
        "scenario_regime_separation": 0.15,
        "alternative_observation_score": 0.15,
        "recovery_presence_rate": 0.10,
        "chatter_score": 0.15,
    },
    "regime_diversity": {
        "barter_final_stage_entropy": 0.30,
        "barter_worst_stage_entropy": 0.15,
        "institution_regime_diversity": 0.15,
        "scenario_regime_separation": 0.20,
        "policy_regime_separation": 0.10,
        "policy_diversity": 0.10,
    },
    "behavior": {
        "alternative_observation_score": 0.35,
        "alternative_choice_balance": 0.20,
        "policy_diversity": 0.25,
        "structural_criteria_score": 0.20,
    },
    "recovery": {
        "recovery_presence_rate": 0.30,
        "recovery_ratio": 0.20,
        "chatter_score": 0.25,
        "barter_final_stage_entropy": 0.15,
        "alternative_observation_score": 0.10,
    },
    "collapse_expression": {
        "stage3_reach_rate": 0.20,
        "stage3_penalty_consistency": 0.15,
        "barter_final_stage_entropy": 0.20,
        "scenario_regime_separation": 0.25,
        "alternative_observation_score": 0.20,
    },
}


PARETO_KEYS = (
    "structural_criteria_score",
    "policy_diversity",
    "barter_final_stage_entropy",
    "scenario_regime_separation",
    "alternative_observation_score",
    "recovery_presence_rate",
    "chatter_score",
)


def score_regime_rows(rows: list[dict]) -> list[dict]:
    """候補集合内percentileで複数profileを採点する。

    絶対値の尺度差で一項目が消えないようpercentile化する。生存関連値は
    reference_outcomes内にしか存在せず、ここから参照できない構造にしている。
    """
    if not rows:
        return []
    rank_keys = sorted({key for weights in PROFILE_WEIGHTS.values()
                        for key in weights})
    ranks = {key: _percentile_ranks(rows, key) for key in rank_keys}
    for row in rows:
        config_id = int(row["config_id"])
        row["profile_scores"] = {
            profile: sum(weight * ranks[key][config_id]
                         for key, weight in weights.items())
            for profile, weights in PROFILE_WEIGHTS.items()
        }
    return rows


def pareto_front_ids(rows: list[dict]) -> set[int]:
    vectors = [(int(row["config_id"]), tuple(float(row[key])
                for key in PARETO_KEYS)) for row in rows]
    front = set()
    for config_id, vector in vectors:
        dominated = any(
            other_id != config_id
            and all(a >= b for a, b in zip(other, vector))
            and any(a > b for a, b in zip(other, vector))
            for other_id, other in vectors)
        if not dominated:
            front.add(config_id)
    return front


def select_portfolio(rows: list[dict], keep: int,
                     pinned_ids: set[int] | None = None,
                     excluded_ids: set[int] | None = None) -> list[int]:
    """Paretoと5つの制度profileからround-robinで候補群を作る。"""
    if keep < 1:
        raise ValueError("keep must be positive")
    score_regime_rows(rows)
    excluded_ids = set(excluded_ids or ())
    eligible = [row for row in rows
                if int(row["config_id"]) not in excluded_ids
                and row.get("numerical_guard", {}).get("passed", True)
                and row.get("semantic_guard", {}).get("passed", True)]
    if not eligible:
        raise ValueError("no eligible rows remain")
    front = pareto_front_ids(eligible)
    for row in rows:
        row["pareto_front"] = int(row["config_id"]) in front
        excluded = int(row["config_id"]) in excluded_ids
        numerical_failure = not row.get("numerical_guard", {}).get("passed", True)
        semantic_failure = not row.get("semantic_guard", {}).get("passed", True)
        row["selection_eligible"] = (
            not excluded and not numerical_failure and not semantic_failure)
        row["selection_exclusion_reason"] = (
            "legacy_or_baseline_control" if excluded
            else "numerical_guard_failed" if numerical_failure
            else "semantic_guard_failed" if semantic_failure else None)
    orders = [sorted(
        (row for row in eligible if row["pareto_front"]),
        key=lambda row: (-row["profile_scores"]["structural_balance"],
                         int(row["config_id"])))]
    orders.extend(sorted(
        eligible, key=lambda row, name=name: (-row["profile_scores"][name],
                                              int(row["config_id"])))
        for name in PROFILE_NAMES)
    selected = []
    seen = set()
    cursors = [0] * len(orders)
    keep = min(keep, len(eligible))
    while len(selected) < keep:
        progressed = False
        for order_index, order in enumerate(orders):
            while cursors[order_index] < len(order):
                row = order[cursors[order_index]]
                cursors[order_index] += 1
                config_id = int(row["config_id"])
                if config_id not in seen:
                    selected.append(config_id)
                    seen.add(config_id)
                    progressed = True
                    break
            if len(selected) >= keep:
                break
        if not progressed:
            break
    for config_id in sorted(pinned_ids or set()):
        if config_id in excluded_ids or config_id in seen or not any(
                int(row["config_id"]) == config_id for row in rows):
            continue
        if len(selected) >= keep:
            removed = selected.pop()
            seen.remove(removed)
        selected.append(config_id)
        seen.add(config_id)
    ranks = {config_id: index + 1 for index, config_id in enumerate(selected)}
    for row in rows:
        row["selected_for_next"] = int(row["config_id"]) in ranks
        row["selection_rank"] = ranks.get(int(row["config_id"]))
    return selected
