#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GPU事前走査と同じ式を標準Pythonで表した参照モデル。

このモデルは既存のoffline_simulation.simulate_policyを置き換えない。可変長の
契約/NPCとPython randomを含む正規シミュレーターをCUDAへそのまま移すとseed単位
の同一性を保証できないため、物々交換制度の27パラメータを全候補について広く
評価する「近似スクリーニング」専用である。最終判断は必ずCPU正規実装で行う。

GPUカーネルと比較しやすいよう、状態は固定長の数値だけ、乱数はturn等から決まる
counter-basedな32bit hashだけを使う。候補間では同じscenario/seed/policyに同じ
乱数を与える(common random numbers)ので、順位比較のノイズを抑えられる。
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from dashboard.batch_worker import SCENARIOS


POLICIES = ("cautious", "ambitious", "family")

PARAMETER_KEYS = (
    "food_initial", "medicine_initial", "shelter_initial", "tools_initial",
    "production_initial", "food_upkeep", "medicine_upkeep", "shelter_wear",
    "tools_wear", "production_disruption", "production_reversion",
    "barter_food_gain", "barter_medicine_gain", "subsistence_food_gain",
    "subsistence_medicine_gain", "subsistence_shelter_repair",
    "subsistence_tools_repair", "subsistence_production_gain",
    "survival_value_scale", "stage1_exit", "stage1_enter", "stage2_exit",
    "stage2_enter", "stage3_exit", "stage3_enter",
    "shortage_energy_penalty", "shortage_health_penalty",
)
PARAMETER_INDEX = {key: index for index, key in enumerate(PARAMETER_KEYS)}

OUTPUT_KEYS = (
    "survived", "death_turn", "final_stage", "worst_stage", "shortage_turns",
    "barter_choices", "subsistence_choices", "final_worst_shortfall",
    "minimum_good", "final_production", "transition_count", "recovery_count",
    "activated_turn", "final_health", "alternative_turns", "observed_turns",
)

MODEL_VERSION = "barter_gpu_screen_v2"


@dataclass(frozen=True)
class ScreenRun:
    survived: float
    death_turn: float
    final_stage: float
    worst_stage: float
    shortage_turns: float
    barter_choices: float
    subsistence_choices: float
    final_worst_shortfall: float
    minimum_good: float
    final_production: float
    transition_count: float
    recovery_count: float
    activated_turn: float
    final_health: float
    alternative_turns: float
    observed_turns: float

    def as_tuple(self) -> tuple[float, ...]:
        return tuple(getattr(self, key) for key in OUTPUT_KEYS)


def parameter_row(parameters: dict) -> tuple[float, ...]:
    """公開パラメータdictを、GPUと共有する固定列順へ変換する。"""
    missing = [key for key in PARAMETER_KEYS if key not in parameters]
    if missing:
        raise ValueError(f"missing GPU screen parameters: {', '.join(missing)}")
    return tuple(float(parameters[key]) for key in PARAMETER_KEYS)


def screen_matrix_size(candidate_count: int, scenario_count: int,
                       seed_count: int, policy_count: int,
                       output_count: int = len(OUTPUT_KEYS),
                       bytes_per_number: int = 4) -> dict:
    runs = candidate_count * scenario_count * seed_count * policy_count
    return {
        "runs": runs,
        "output_bytes": runs * output_count * bytes_per_number,
        "parameter_bytes": candidate_count * len(PARAMETER_KEYS) * bytes_per_number,
    }


def _u32(value: int) -> int:
    return value & 0xFFFFFFFF


def hash32(seed: int, turn: int, lane: int, policy_index: int,
           scenario_index: int) -> int:
    """GPUでも同じ演算ができる、状態を持たない32bit hash。"""
    value = _u32(seed)
    value ^= _u32(turn * 0x9E3779B9)
    value ^= _u32(lane * 0x85EBCA6B)
    value ^= _u32((policy_index + 1) * 0xC2B2AE35)
    value ^= _u32((scenario_index + 1) * 0x27D4EB2F)
    value ^= value >> 16
    value = _u32(value * 0x7FEB352D)
    value ^= value >> 15
    value = _u32(value * 0x846CA68B)
    value ^= value >> 16
    return _u32(value)


def uniform01(seed: int, turn: int, lane: int, policy_index: int,
              scenario_index: int) -> float:
    return hash32(seed, turn, lane, policy_index, scenario_index) / 4294967296.0


def _clip(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _round6(value: float) -> float:
    # CUDA側と同じhalf-away-from-zero。正規実装のPython roundとは意図的に
    # 別物なので、このモデルをseed単位のgoldenとして扱ってはいけない。
    scaled = value * 1_000_000.0
    rounded = math.floor(scaled + 0.5) if scaled >= 0 else math.ceil(scaled - 0.5)
    return rounded / 1_000_000.0


def _shortfall(value: float, reference: float) -> float:
    return max(0.0, (reference - value) / reference * 100.0) if reference else 0.0


def _worst_shortfall(values: tuple[float, ...], references: tuple[float, ...]) -> float:
    return max(_shortfall(value, reference)
               for value, reference in zip(values, references))


def _stage_next(stage: int, shortfall: float, p: tuple[float, ...]) -> int:
    if shortfall >= p[PARAMETER_INDEX["stage3_enter"]]:
        return 3
    if shortfall >= p[PARAMETER_INDEX["stage2_enter"]] and stage < 2:
        return 2
    if shortfall >= p[PARAMETER_INDEX["stage1_enter"]] and stage < 1:
        return 1
    if stage == 3:
        return 2 if shortfall <= p[PARAMETER_INDEX["stage3_exit"]] else 3
    if stage == 2:
        return 1 if shortfall <= p[PARAMETER_INDEX["stage2_exit"]] else 2
    if stage == 1:
        return 0 if shortfall <= p[PARAMETER_INDEX["stage1_exit"]] else 1
    return stage


def _natural_activation_turn(seed: int, policy_index: int, turns: int) -> int:
    # 正規実装の上位4制度を再現するものではない。自然崩壊scenarioでも候補間に
    # 共通の「早い/遅い/未発火」を与えるための外生ショックである。
    gate = uniform01(seed, 0, 901, policy_index, 0)
    threshold = (0.58, 0.68, 0.62)[policy_index]
    if gate >= threshold:
        return 0
    lower = min(360, turns)
    span = max(1, turns - lower + 1)
    return lower + int(uniform01(seed, 0, 902, policy_index, 0) * span)


def _choice_values(choice: int, values: tuple[float, ...],
                   references: tuple[float, ...], p: tuple[float, ...]) -> tuple:
    food, medicine, shelter, tools, production = values
    factor = 0.5 + _clip(production, 0.0, 100.0) / 100.0
    if choice == 1:  # barter
        effects = (
            _round6(p[PARAMETER_INDEX["barter_food_gain"]] * factor),
            _round6(p[PARAMETER_INDEX["barter_medicine_gain"]] * factor),
            0.0, 0.0, 0.0,
        )
    else:  # subsistence
        effects = (
            _round6(p[PARAMETER_INDEX["subsistence_food_gain"]] * factor),
            _round6(p[PARAMETER_INDEX["subsistence_medicine_gain"]] * factor),
            p[PARAMETER_INDEX["subsistence_shelter_repair"]],
            p[PARAMETER_INDEX["subsistence_tools_repair"]],
            p[PARAMETER_INDEX["subsistence_production_gain"]],
        )
    after = tuple(_clip(value + delta, 0.0, 100.0)
                  for value, delta in zip(values, effects))
    before_shortfalls = tuple(_shortfall(value, reference)
                              for value, reference in zip(values, references))
    after_shortfalls = tuple(_shortfall(value, reference)
                             for value, reference in zip(after, references))
    avoided = sum(max(0.0, before - after_value) * (1.0 + 2.0 * before / 100.0)
                  for before, after_value in zip(before_shortfalls, after_shortfalls))
    survival_value = avoided * p[PARAMETER_INDEX["survival_value_scale"]]
    return after, survival_value


def simulate_screen_run(parameters: dict | tuple[float, ...], turns: int,
                        seed: int, policy_index: int,
                        scenario_index: int) -> ScreenRun:
    """1候補×1scenario×1seed×1policyの近似参照計算。"""
    p = parameter_row(parameters) if isinstance(parameters, dict) else tuple(parameters)
    if len(p) != len(PARAMETER_KEYS):
        raise ValueError("unexpected GPU parameter row width")
    if not 0 <= policy_index < len(POLICIES):
        raise ValueError("invalid policy index")
    if not 0 <= scenario_index < len(SCENARIOS):
        raise ValueError("invalid scenario index")

    food = p[0]
    medicine = p[1]
    shelter = p[2]
    tools = p[3]
    production = p[4]
    references = (food, medicine, shelter, tools, production)
    stage = 0
    worst_stage = 0
    transitions = recoveries = shortage_turns = 0
    barter_choices = subsistence_choices = 0
    minimum_good = min(food, medicine, shelter, tools)
    health = 80.0
    death_turn = 0
    alternative_turns = 0
    activation_turn = (_natural_activation_turn(seed, policy_index, turns)
                       if scenario_index == 0 else 1)

    observed_turns = 0
    for turn in range(1, turns + 1):
        observed_turns = turn
        alternative = activation_turn > 0 and turn >= activation_turn
        if alternative:
            alternative_turns += 1

        bounded_production = _clip(production, 0.0, 100.0)
        # institutions.barter.background_goods_productionは分母に現在の
        # PRODUCTION_CAPACITY_INITIALを使う。候補のproduction_initialを同時に
        # shortfall referenceへ使う現行ダッシュボード仕様もそのまま写す。
        production_factor = (bounded_production / p[4]) if p[4] else 0.0
        food = _clip(food + _round6(-p[5] + p[5] * production_factor), 0.0, 100.0)
        medicine = _clip(medicine + _round6(-p[6] + p[6] * production_factor), 0.0, 100.0)
        shelter = _clip(shelter + _round6(-p[7] + p[7] * production_factor), 0.0, 100.0)
        tools = _clip(tools + _round6(-p[8] + p[8] * production_factor), 0.0, 100.0)
        reversion = _round6((production - p[4]) * p[10])
        production_delta = _round6(-reversion - (p[9] if alternative else 0.0))
        production = _clip(production + production_delta, 0.0, 100.0)

        values = (food, medicine, shelter, tools, production)
        worst = _worst_shortfall(values, references)
        old_stage = stage
        stage = _stage_next(stage, worst, p)
        if stage != old_stage:
            transitions += 1
            if stage < old_stage:
                recoveries += 1
        worst_stage = max(worst_stage, stage)
        if stage == 3:
            shortage_turns += 1
            health = _clip(health + p[26], 0.0, 100.0)

        # 通常行動が発生する月だけ代替経路を比較する。hashは候補に依存させず、
        # common random numbersを保つ。scenario差は上位制度崩壊時に残る通常
        # 選択肢の違いを、行動機会率の小さな差としてだけ表す。
        normal_rate = (0.72, 0.78, 0.70)[policy_index]
        normal_rate += (0.0, 0.02, -0.02, 0.01, 0.03, -0.04)[scenario_index]
        normal_action = (uniform01(seed, turn, 10, policy_index, scenario_index)
                         < normal_rate)
        if normal_action:
            conventional_cost = 11.0 + 25.0 * uniform01(
                seed, turn, 11, policy_index, scenario_index)
            best_choice = 0
            best_score = -conventional_cost
            if alternative:
                # Stage0/1だけbarter可。subsistenceは全Stageで可。
                first = 1 if stage <= 1 else 2
                for choice in range(first, 3):
                    after, survival_value = _choice_values(choice, values, references, p)
                    jitter_energy = uniform01(seed, turn, 20 + choice, policy_index, scenario_index)
                    jitter_peace = uniform01(seed, turn, 30 + choice, policy_index, scenario_index)
                    if choice == 1:
                        energy_cost = 10.0 + 15.0 * jitter_energy
                        peace_cost = 5.0 + 5.0 * jitter_peace
                    else:
                        energy_cost = 15.0 + 20.0 * jitter_energy
                        peace_cost = 5.0 + 10.0 * jitter_peace
                    energy_weight = (1.0, 0.3, 0.5)[policy_index]
                    peace_weight = (1.0, 0.1, 1.0)[policy_index]
                    score = (survival_value
                             - 1.5 * energy_weight * energy_cost
                             - peace_weight * peace_cost)
                    if score > best_score:
                        best_score = score
                        best_choice = choice
            if best_choice:
                values, _ = _choice_values(best_choice, values, references, p)
                food, medicine, shelter, tools, production = values
                if best_choice == 1:
                    barter_choices += 1
                else:
                    subsistence_choices += 1
            elif uniform01(seed, turn, 50, policy_index, scenario_index) < 0.33:
                # 通常経済側のmoney/laborによる健康回復の粗い外生近似。
                # v1はこの回復をalternative発火後だけに誤って限定していた。
                care = (2.2, 3.0, 2.4)[policy_index]
                health = _clip(health + care * (1.0 - health / 100.0), 0.0, 100.0)

        # 正規モデルと同じ線形老化式。通常行動のhealth回復は上の近似だけ。
        health = _clip(health - (0.02 + 0.004 * turn / 12.0), 0.0, 100.0)
        minimum_good = min(minimum_good, food, medicine, shelter, tools)
        if health <= 0.0:
            death_turn = turn
            break

    final_values = (food, medicine, shelter, tools, production)
    final_worst = _worst_shortfall(final_values, references)
    return ScreenRun(
        1.0 if death_turn == 0 else 0.0, float(death_turn), float(stage),
        float(worst_stage), float(shortage_turns), float(barter_choices),
        float(subsistence_choices), final_worst, minimum_good, production,
        float(transitions), float(recoveries), float(activation_turn), health,
        float(alternative_turns), float(observed_turns),
    )


def aggregate_screen_runs(runs: list[ScreenRun], requested_turns: int) -> dict:
    """GPU chunkと同じ候補単位の集約値を参照モデルから作る。"""
    if not runs:
        raise ValueError("runs must not be empty")
    count = len(runs)
    survived = sum(run.survived for run in runs)
    lifespan = sum((run.death_turn if run.death_turn else requested_turns)
                   / max(1.0, requested_turns) for run in runs)
    shortage = sum(run.shortage_turns / max(1.0, run.observed_turns) for run in runs)
    used = sum((run.barter_choices + run.subsistence_choices) > 0 for run in runs)
    health = sum(run.final_health for run in runs)
    safety = sum(max(0.0, 100.0 - run.final_worst_shortfall) for run in runs)
    transitions = sum(run.transition_count for run in runs)
    recoveries = sum(run.recovery_count for run in runs)
    survival_rate = survived / count
    lifespan_ratio = lifespan / count
    shortage_rate = shortage / count
    alternative_use_rate = used / count
    mean_health = health / count
    mean_goods_safety = safety / count
    recovery_ratio = recoveries / transitions if transitions else 0.0
    score = (4.0 * survival_rate + 2.0 * lifespan_ratio
             + 2.0 * (1.0 - shortage_rate) + alternative_use_rate
             + mean_health / 100.0 + mean_goods_safety / 100.0
             + 0.5 * recovery_ratio)
    return {
        "run_count": count, "screen_score": score,
        "survival_proxy_rate": survival_rate,
        "mean_lifespan_ratio": lifespan_ratio, "shortage_rate": shortage_rate,
        "alternative_use_rate": alternative_use_rate,
        "mean_final_health": mean_health, "mean_goods_safety": mean_goods_safety,
        "transition_count": transitions, "recovery_count": recoveries,
        "recovery_ratio": recovery_ratio,
    }
