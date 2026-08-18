#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1パラメータ組を隔離プロセスで複数seed・方針評価する内部worker。

大量走査では設定ごとにこのworkerを新しく起動する。barter定数とgame.pyが
プロセスをまたいで残らないため、既存のグローバル設定・RNGを安全に隔離できる。
stdoutはcoordinatorへ返すJSONだけに限定する。
"""
import json
from pathlib import Path
import sys


PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from dashboard.experiment_parameters import (  # noqa: E402
    apply_barter_overrides,
    default_values,
    validate_request,
)
from institutions import barter  # noqa: E402


SCENARIOS = (
    "natural",
    "forced_alternative",
    "currency_abandoned",
    "local_credit_isolated",
    "enforcement_none",
    "compound_collapse",
)


def apply_world_scenario(game_module, scenario: str) -> None:
    """走査worker内だけで上位制度の崩壊条件を固定する。

    本番定数やsimulate_policy()本体は変更せず、game.pyの動的依存解決を使う。
    workerは設定・scenarioごとに新規プロセスなのでmonkeypatchはrun間で残らない。
    """
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario: {scenario}")
    if scenario == "natural":
        return
    if scenario == "forced_alternative":
        game_module.engine.alternative_economy_triggered = (
            lambda *_args, **_kwargs: True)
        game_module.alternative_economy_triggered = (
            lambda *_args, **_kwargs: True)
        return
    if scenario in ("currency_abandoned", "compound_collapse"):
        game_module.currency_stage_next = (
            lambda *_args, **_kwargs: game_module.CURRENCY_STAGE_ABANDONED)
    if scenario in ("local_credit_isolated", "compound_collapse"):
        game_module.local_credit_stage_next = (
            lambda *_args, **_kwargs: game_module.LOCAL_CREDIT_STAGE_ISOLATED)
    if scenario in ("enforcement_none", "compound_collapse"):
        game_module.enforcement_stage_next = (
            lambda *_args, **_kwargs: game_module.ENFORCEMENT_STAGE_NONE)
    if scenario == "compound_collapse":
        game_module.bank_stage_next = (
            lambda *_args, **_kwargs: game_module.BANK_STAGE_COLLAPSED)


def _transition_metrics(tracking: dict, rapid_window_turns: int = 12) -> dict:
    """保存前に遷移履歴を集約し、長い履歴自体は捨てられる形にする。"""
    history = tracking.get("transition_history", [])
    reversals = 0
    rapid_recrosses = 0
    for previous, current in zip(history, history[1:]):
        previous_direction = previous["to_stage"] - previous["from_stage"]
        current_direction = current["to_stage"] - current["from_stage"]
        reversed_direction = previous_direction * current_direction < 0
        if reversed_direction:
            reversals += 1
        same_boundary = ({previous["from_stage"], previous["to_stage"]}
                         == {current["from_stage"], current["to_stage"]})
        if (reversed_direction and same_boundary
                and current["turn"] - previous["turn"] <= rapid_window_turns):
            rapid_recrosses += 1
    observed_turns = sum(tracking.get("stage_turns", {}).values())
    return {
        "direction_reversal_count": reversals,
        "rapid_same_boundary_recross_count": rapid_recrosses,
        "transition_rate_per_100_turns": (
            len(history) / observed_turns * 100.0 if observed_turns else 0.0),
    }


def _compact_institution(tracking: dict) -> dict:
    keys = (
        "stage_turns", "first_reached_turn", "first_left_healthy_turn",
        "transition_count", "recovery_count", "worst_stage", "final_stage",
        "settlement_counts_by_stage",
    )
    compact = {key: tracking[key] for key in keys if key in tracking}
    compact.update(_transition_metrics(tracking))
    return compact


def summarize_run(result: dict, criteria: list, requested_turns: int,
                  seed: int, policy: str, scenario: str = "natural") -> dict:
    """探索と比較に必要な値だけを約4KBへ集約する。"""
    death_turn = result.get("death_turn")
    observed_turns = death_turn if death_turn is not None else requested_turns
    counts = result.get("counts", {})
    normal_counts = {
        key.split(":", 1)[1]: value
        for key, value in counts.items() if key.startswith("通常:")
    }
    settlement_counts = {
        key.split(":", 1)[1]: value
        for key, value in counts.items() if key.startswith("清算:")
    }
    total_settlements = sum(settlement_counts.values())
    criteria_rows = [
        {"name": row["name"], "passed": row["passed"],
         "category": row.get("category", "uncategorized")}
        for row in criteria
    ]
    pass_count = sum(row["passed"] is True for row in criteria_rows)
    fail_count = sum(row["passed"] is False for row in criteria_rows)
    na_count = sum(row["passed"] is None for row in criteria_rows)
    final_keys = (
        "bank_trust", "bank_stage", "currency_confidence", "currency_stage",
        "community_trust", "local_credit_stage", "enforcement_capacity",
        "enforcement_stage", "food", "medicine", "shelter", "tools",
        "production_capacity", "barter_stage", "real_money",
    )
    return {
        "seed": seed,
        "policy": policy,
        "scenario": scenario,
        "turns_requested": requested_turns,
        "observed_turns": observed_turns,
        "survived": death_turn is None,
        "death_turn": death_turn,
        "talent": result.get("talent"),
        "blocked": result.get("blocked", 0),
        "overridden": result.get("overridden", 0),
        "fires": result.get("fires", 0),
        "bank_crisis_count": result.get("bank_crisis_count", 0),
        "barter_active": result.get("barter_active", False),
        "barter_activated_turn": result.get("barter_activated_turn"),
        "barter_shortage_penalty_applied_count": result.get(
            "barter_shortage_penalty_applied_count", 0),
        "normal_counts": normal_counts,
        "settlement_counts": settlement_counts,
        "default_rate": (settlement_counts.get("avoid", 0) / total_settlements
                         if total_settlements else None),
        "resources": result.get("resources", {}),
        "traits": result.get("traits", {}),
        "min_seen": result.get("min_seen", {}),
        "goods_min_seen": result.get("goods_min_seen", {}),
        "finals": {key: result.get(key) for key in final_keys},
        "institutions": {
            name: _compact_institution(tracking)
            for name, tracking in result.get("institution_trajectories", {}).items()
        },
        "criteria": criteria_rows,
        "criteria_tally": {
            "pass": pass_count, "fail": fail_count, "na": na_count,
        },
    }


def execute(payload: dict) -> dict:
    parameters = payload["parameters"]
    turns = int(payload["turns"])
    seeds = list(payload["seeds"])
    policies = list(payload["policies"])
    safety_floor = int(payload.get("safety_floor", 30))
    talent_value = payload.get("talent", "random")
    scenario = payload.get("scenario", "natural")
    shortfall_reference_mode = payload.get(
        "shortfall_reference_mode", "coupled")
    if shortfall_reference_mode not in ("coupled", "fixed"):
        raise ValueError(
            "shortfall_reference_mode must be 'coupled' or 'fixed'")
    if not seeds or not policies:
        raise ValueError("seeds and policies must not be empty")

    # 公開フォームと同じallowlist・境界検証を通す。実際のseed/policyは下の
    # ループで変えるが、ここでは設定値全体の正当性を一度検証する。
    values = default_values()
    values.update(parameters)
    values.update({
        "turns": turns, "seed": seeds[0], "policy": policies[0],
        "safety_floor": safety_floor, "talent": talent_value,
    })
    config = validate_request({"values": values})
    apply_barter_overrides(
        barter, config["barter_overrides"],
        couple_shortfall_references=(shortfall_reference_mode == "coupled"))

    # game/engineがbarter定数をimportする前にoverrideを完了させる。
    import game
    apply_world_scenario(game, scenario)

    talent = None if talent_value == "random" else talent_value
    evaluations = []
    for policy in policies:
        for seed in seeds:
            result = game.simulate_policy(
                policy, turns, seed, safety_floor, talent=talent)
            criteria = game.check_trajectory_criteria(result)
            evaluations.append(summarize_run(
                result, criteria, turns, seed, policy, scenario))
    return {"evaluations": evaluations}


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        json.dump(execute(payload), sys.stdout, ensure_ascii=False,
                  separators=(",", ":"))
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
