#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RTX向けの物々交換パラメータ近似スクリーニング。

既存のexhaustive_sweepが作ったconfigsを読み、全候補について指定した
scenario x seed x policyをCUDAで評価する。結果は既存の厳密evaluationとは
混ぜず、gpu_screen_*テーブルへ保存する。上位候補だけを現行CPU
simulate_policyで再検証するための事前順位であり、本番シミュレーションの
代替ではない。

NumPy/Numba-CUDAは通常のテスト環境には要求せず、main実行時にだけimportする。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys
import time


PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from dashboard import batch_sweep
from dashboard.gpu_barter_model import (
    MODEL_VERSION,
    OUTPUT_KEYS,
    PARAMETER_KEYS,
    POLICIES,
    parameter_row,
    screen_matrix_size,
)
from dashboard.batch_worker import SCENARIOS


DEFAULT_DB_PATH = batch_sweep.DASHBOARD_DIR / "sweeps" / "barter_matrix.sqlite3"
DEFAULT_TOP_PATH = batch_sweep.DASHBOARD_DIR / "sweeps" / "gpu_screen_top.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_names(value: str, allowed: tuple[str, ...], label: str) -> tuple[str, ...]:
    selected = tuple(dict.fromkeys(part.strip() for part in value.split(",")
                                   if part.strip()))
    unknown = sorted(set(selected) - set(allowed))
    if not selected or unknown:
        raise ValueError(f"{label} must be selected from {allowed}; unknown={unknown}")
    return selected


def matrix_key(turns: int, scenarios: tuple[str, ...], seeds: tuple[int, ...],
               policies: tuple[str, ...]) -> str:
    payload = {
        "model_version": MODEL_VERSION, "turns": turns,
        "scenarios": scenarios, "seeds": seeds, "policies": policies,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:20]


def ensure_tables(connection: sqlite3.Connection) -> None:
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS gpu_screen_metadata (
        matrix_key TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        PRIMARY KEY(matrix_key, key)
    );
    CREATE TABLE IF NOT EXISTS gpu_screen_results (
        matrix_key TEXT NOT NULL,
        config_id INTEGER NOT NULL REFERENCES configs(id),
        model_version TEXT NOT NULL,
        run_count INTEGER NOT NULL,
        screen_score REAL NOT NULL,
        survival_proxy_rate REAL NOT NULL,
        mean_lifespan_ratio REAL NOT NULL,
        shortage_rate REAL NOT NULL,
        alternative_use_rate REAL NOT NULL,
        mean_final_health REAL NOT NULL,
        mean_goods_safety REAL NOT NULL,
        transition_count INTEGER NOT NULL,
        recovery_count INTEGER NOT NULL,
        recovery_ratio REAL NOT NULL,
        completed_at TEXT NOT NULL,
        PRIMARY KEY(matrix_key, config_id)
    );
    CREATE INDEX IF NOT EXISTS idx_gpu_screen_score
        ON gpu_screen_results(matrix_key, screen_score DESC);
    """)
    connection.commit()


def available_config_count(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COUNT(*) FROM configs").fetchone()
    return int(row[0])


def pending_config_ids(connection: sqlite3.Connection, key: str, limit: int = 0,
                       offset: int = 0) -> list[int]:
    query = """
        SELECT c.id FROM configs AS c
        LEFT JOIN gpu_screen_results AS g
          ON g.config_id = c.id AND g.matrix_key = ?
        WHERE g.config_id IS NULL
        ORDER BY c.id
    """
    parameters: list = [key]
    if limit:
        query += " LIMIT ? OFFSET ?"
        parameters.extend((limit, offset))
    elif offset:
        query += " LIMIT -1 OFFSET ?"
        parameters.append(offset)
    return [int(row[0]) for row in connection.execute(query, parameters)]


def load_parameter_rows(connection: sqlite3.Connection,
                        config_ids: list[int]) -> list[tuple[float, ...]]:
    if not config_ids:
        return []
    placeholders = ",".join("?" for _ in config_ids)
    rows = connection.execute(
        f"SELECT id, parameters_json FROM configs WHERE id IN ({placeholders})",
        config_ids).fetchall()
    by_id = {int(config_id): parameter_row(json.loads(payload))
             for config_id, payload in rows}
    return [by_id[config_id] for config_id in config_ids]


def aggregate_numpy_rows(output, config_count: int, runs_per_config: int,
                         requested_turns: int) -> list[dict]:
    """GPU出力を候補単位へ集約する。NumPy配列はduck typingで受ける。"""
    shaped = output.reshape(config_count, runs_per_config, len(OUTPUT_KEYS))
    index = {key: OUTPUT_KEYS.index(key) for key in OUTPUT_KEYS}
    results = []
    for candidate in shaped:
        death = candidate[:, index["death_turn"]]
        lifespans = candidate[:, index["survived"]] * requested_turns + (
            1.0 - candidate[:, index["survived"]]) * death
        observed = candidate[:, index["observed_turns"]]
        shortage_ratio = candidate[:, index["shortage_turns"]] / observed.clip(min=1.0)
        used = ((candidate[:, index["barter_choices"]]
                 + candidate[:, index["subsistence_choices"]]) > 0.0)
        transitions = float(candidate[:, index["transition_count"]].sum())
        recoveries = float(candidate[:, index["recovery_count"]].sum())
        survival_rate = float(candidate[:, index["survived"]].mean())
        lifespan_ratio = float((lifespans / max(1, requested_turns)).mean())
        shortage_rate = float(shortage_ratio.mean())
        use_rate = float(used.mean())
        health = float(candidate[:, index["final_health"]].mean())
        goods_safety = float((100.0 - candidate[:, index[
            "final_worst_shortfall"]].clip(min=0.0, max=100.0)).mean())
        recovery_ratio = recoveries / transitions if transitions else 0.0
        score = (4.0 * survival_rate + 2.0 * lifespan_ratio
                 + 2.0 * (1.0 - shortage_rate) + use_rate
                 + health / 100.0 + goods_safety / 100.0
                 + 0.5 * recovery_ratio)
        results.append({
            "run_count": runs_per_config, "screen_score": score,
            "survival_proxy_rate": survival_rate,
            "mean_lifespan_ratio": lifespan_ratio,
            "shortage_rate": shortage_rate,
            "alternative_use_rate": use_rate,
            "mean_final_health": health, "mean_goods_safety": goods_safety,
            "transition_count": round(transitions),
            "recovery_count": round(recoveries),
            "recovery_ratio": recovery_ratio,
        })
    return results


def _build_cuda_kernel(cuda_module):
    """Numba-CUDAを通常テスト時にimportしないため、kernelを遅延構築する。"""
    # CUDA simulatorはkernel globals中の`cuda`をFakeCUDAModuleへ差し替える。
    # closureにするとcuda.grid()を差し替えられないため、実行時だけglobalへ置く。
    globals()["cuda"] = cuda_module

    @cuda_module.jit(device=True, inline=True)
    def clip(value, lower, upper):
        if value < lower:
            return lower
        if value > upper:
            return upper
        return value

    @cuda_module.jit(device=True, inline=True)
    def round6(value):
        scaled = value * 1000000.0
        if scaled >= 0.0:
            return math.floor(scaled + 0.5) / 1000000.0
        return math.ceil(scaled - 0.5) / 1000000.0

    @cuda_module.jit(device=True, inline=True)
    def hash_value(seed, turn, lane, policy_index, scenario_index):
        # simulatorのNumPy int32が大きな定数へ暗黙castされないよう、演算前に
        # Python/Numba intへ広げる。本番device上ではint64演算としてcompileされる。
        seed_value = int(seed)
        turn_value = int(turn)
        lane_value = int(lane)
        policy_value = int(policy_index)
        scenario_value = int(scenario_index)
        value = seed_value & 0xFFFFFFFF
        value ^= (turn_value * 0x9E3779B9) & 0xFFFFFFFF
        value ^= (lane_value * 0x85EBCA6B) & 0xFFFFFFFF
        value ^= ((policy_value + 1) * 0xC2B2AE35) & 0xFFFFFFFF
        value ^= ((scenario_value + 1) * 0x27D4EB2F) & 0xFFFFFFFF
        value ^= value >> 16
        value = (value * 0x7FEB352D) & 0xFFFFFFFF
        value ^= value >> 15
        value = (value * 0x846CA68B) & 0xFFFFFFFF
        value ^= value >> 16
        return value & 0xFFFFFFFF

    @cuda_module.jit(device=True, inline=True)
    def uniform(seed, turn, lane, policy_index, scenario_index):
        return (hash_value(seed, turn, lane, policy_index, scenario_index)
                / 4294967296.0)

    @cuda_module.jit(device=True, inline=True)
    def shortfall(value, reference):
        if reference == 0.0 or value >= reference:
            return 0.0
        return (reference - value) / reference * 100.0

    @cuda_module.jit(device=True, inline=True)
    def worst_shortfall(food, medicine, shelter, tools, production,
                        food_ref, medicine_ref, shelter_ref, tools_ref, production_ref):
        worst = shortfall(food, food_ref)
        score = shortfall(medicine, medicine_ref)
        if score > worst:
            worst = score
        score = shortfall(shelter, shelter_ref)
        if score > worst:
            worst = score
        score = shortfall(tools, tools_ref)
        if score > worst:
            worst = score
        score = shortfall(production, production_ref)
        if score > worst:
            worst = score
        return worst

    @cuda_module.jit(device=True, inline=True)
    def next_stage(stage, worst, p):
        if worst >= p[24]:
            return 3
        if worst >= p[22] and stage < 2:
            return 2
        if worst >= p[20] and stage < 1:
            return 1
        if stage == 3:
            return 2 if worst <= p[23] else 3
        if stage == 2:
            return 1 if worst <= p[21] else 2
        if stage == 1:
            return 0 if worst <= p[19] else 1
        return stage

    @cuda_module.jit(device=True, inline=True)
    def choice_survival(choice, food, medicine, shelter, tools, production,
                        food_ref, medicine_ref, shelter_ref, tools_ref, production_ref, p):
        factor = 0.5 + clip(production, 0.0, 100.0) / 100.0
        if choice == 1:
            df = round6(p[11] * factor)
            dm = round6(p[12] * factor)
            ds = 0.0
            dt = 0.0
            dp = 0.0
        else:
            df = round6(p[13] * factor)
            dm = round6(p[14] * factor)
            ds = p[15]
            dt = p[16]
            dp = p[17]
        before_f = shortfall(food, food_ref)
        before_m = shortfall(medicine, medicine_ref)
        before_s = shortfall(shelter, shelter_ref)
        before_t = shortfall(tools, tools_ref)
        before_p = shortfall(production, production_ref)
        after_f = shortfall(clip(food + df, 0.0, 100.0), food_ref)
        after_m = shortfall(clip(medicine + dm, 0.0, 100.0), medicine_ref)
        after_s = shortfall(clip(shelter + ds, 0.0, 100.0), shelter_ref)
        after_t = shortfall(clip(tools + dt, 0.0, 100.0), tools_ref)
        after_p = shortfall(clip(production + dp, 0.0, 100.0), production_ref)
        avoided = 0.0
        if before_f > after_f:
            avoided += (before_f - after_f) * (1.0 + 2.0 * before_f / 100.0)
        if before_m > after_m:
            avoided += (before_m - after_m) * (1.0 + 2.0 * before_m / 100.0)
        if before_s > after_s:
            avoided += (before_s - after_s) * (1.0 + 2.0 * before_s / 100.0)
        if before_t > after_t:
            avoided += (before_t - after_t) * (1.0 + 2.0 * before_t / 100.0)
        if before_p > after_p:
            avoided += (before_p - after_p) * (1.0 + 2.0 * before_p / 100.0)
        return avoided * p[18]

    @cuda_module.jit(device=True, inline=True)
    def apply_choice(choice, food, medicine, shelter, tools, production, p):
        factor = 0.5 + clip(production, 0.0, 100.0) / 100.0
        if choice == 1:
            food = clip(food + round6(p[11] * factor), 0.0, 100.0)
            medicine = clip(medicine + round6(p[12] * factor), 0.0, 100.0)
        else:
            food = clip(food + round6(p[13] * factor), 0.0, 100.0)
            medicine = clip(medicine + round6(p[14] * factor), 0.0, 100.0)
            shelter = clip(shelter + p[15], 0.0, 100.0)
            tools = clip(tools + p[16], 0.0, 100.0)
            production = clip(production + p[17], 0.0, 100.0)
        return food, medicine, shelter, tools, production

    @cuda_module.jit
    def screen_kernel(parameters, seeds, scenario_indices, policy_indices,
                      turns, output):
        run_index = cuda.grid(1)
        if run_index >= output.shape[0]:
            return
        runs_per_config = (seeds.size * scenario_indices.size
                           * policy_indices.size)
        config_index = run_index // runs_per_config
        remainder = run_index - config_index * runs_per_config
        policy_slot = remainder % policy_indices.size
        remainder //= policy_indices.size
        seed_slot = remainder % seeds.size
        scenario_slot = remainder // seeds.size
        policy_index = policy_indices[policy_slot]
        scenario_index = scenario_indices[scenario_slot]
        seed = seeds[seed_slot]
        p = parameters[config_index]

        food = p[0]
        medicine = p[1]
        shelter = p[2]
        tools = p[3]
        production = p[4]
        food_ref = food
        medicine_ref = medicine
        shelter_ref = shelter
        tools_ref = tools
        production_ref = production
        stage = 0
        worst_stage = 0
        transitions = 0
        recoveries = 0
        shortage_turns = 0
        barter_choices = 0
        subsistence_choices = 0
        minimum_good = min(min(food, medicine), min(shelter, tools))
        health = 80.0
        death_turn = 0
        alternative_turns = 0
        observed_turns = 0

        if scenario_index == 0:
            gate = uniform(seed, 0, 901, policy_index, 0)
            threshold = 0.58 if policy_index == 0 else (0.68 if policy_index == 1 else 0.62)
            if gate >= threshold:
                activation_turn = 0
            else:
                lower = min(360, turns)
                span = max(1, turns - lower + 1)
                activation_turn = lower + int(
                    uniform(seed, 0, 902, policy_index, 0) * span)
        else:
            activation_turn = 1

        for turn in range(1, turns + 1):
            observed_turns = turn
            alternative = activation_turn > 0 and turn >= activation_turn
            if alternative:
                alternative_turns += 1
            bounded_production = clip(production, 0.0, 100.0)
            production_factor = bounded_production / p[4] if p[4] else 0.0
            food = clip(food + round6(-p[5] + p[5] * production_factor), 0.0, 100.0)
            medicine = clip(medicine + round6(-p[6] + p[6] * production_factor), 0.0, 100.0)
            shelter = clip(shelter + round6(-p[7] + p[7] * production_factor), 0.0, 100.0)
            tools = clip(tools + round6(-p[8] + p[8] * production_factor), 0.0, 100.0)
            reversion = round6((production - p[4]) * p[10])
            production_delta = round6(-reversion - (p[9] if alternative else 0.0))
            production = clip(production + production_delta, 0.0, 100.0)

            worst = worst_shortfall(
                food, medicine, shelter, tools, production,
                food_ref, medicine_ref, shelter_ref, tools_ref, production_ref)
            old_stage = stage
            stage = next_stage(stage, worst, p)
            if stage != old_stage:
                transitions += 1
                if stage < old_stage:
                    recoveries += 1
            if stage > worst_stage:
                worst_stage = stage
            if stage == 3:
                shortage_turns += 1
                health = clip(health + p[26], 0.0, 100.0)

            normal_rate = 0.72 if policy_index == 0 else (0.78 if policy_index == 1 else 0.70)
            if scenario_index == 1:
                normal_rate += 0.02
            elif scenario_index == 2:
                normal_rate -= 0.02
            elif scenario_index == 3:
                normal_rate += 0.01
            elif scenario_index == 4:
                normal_rate += 0.03
            elif scenario_index == 5:
                normal_rate -= 0.04
            normal_action = (uniform(seed, turn, 10, policy_index, scenario_index)
                             < normal_rate)
            if normal_action:
                conventional_cost = 11.0 + 25.0 * uniform(
                    seed, turn, 11, policy_index, scenario_index)
                best_choice = 0
                best_score = -conventional_cost
                if alternative:
                    first = 1 if stage <= 1 else 2
                    for choice in range(first, 3):
                        survival_value = choice_survival(
                            choice, food, medicine, shelter, tools, production,
                            food_ref, medicine_ref, shelter_ref, tools_ref, production_ref, p)
                        jitter_energy = uniform(
                            seed, turn, 20 + choice, policy_index, scenario_index)
                        jitter_peace = uniform(
                            seed, turn, 30 + choice, policy_index, scenario_index)
                        if choice == 1:
                            energy_cost = 10.0 + 15.0 * jitter_energy
                            peace_cost = 5.0 + 5.0 * jitter_peace
                        else:
                            energy_cost = 15.0 + 20.0 * jitter_energy
                            peace_cost = 5.0 + 10.0 * jitter_peace
                        energy_weight = 1.0 if policy_index == 0 else (0.3 if policy_index == 1 else 0.5)
                        peace_weight = 1.0 if policy_index != 1 else 0.1
                        score = (survival_value - 1.5 * energy_weight * energy_cost
                                 - peace_weight * peace_cost)
                        if score > best_score:
                            best_score = score
                            best_choice = choice
                if best_choice:
                    food, medicine, shelter, tools, production = apply_choice(
                        best_choice, food, medicine, shelter, tools, production, p)
                    if best_choice == 1:
                        barter_choices += 1
                    else:
                        subsistence_choices += 1
                elif uniform(seed, turn, 50, policy_index, scenario_index) < 0.33:
                    care = 2.2 if policy_index == 0 else (3.0 if policy_index == 1 else 2.4)
                    health = clip(health + care * (1.0 - health / 100.0), 0.0, 100.0)

            health = clip(health - (0.02 + 0.004 * turn / 12.0), 0.0, 100.0)
            minimum_good = min(minimum_good, min(min(food, medicine), min(shelter, tools)))
            if health <= 0.0:
                death_turn = turn
                break

        final_worst = worst_shortfall(
            food, medicine, shelter, tools, production,
            food_ref, medicine_ref, shelter_ref, tools_ref, production_ref)
        output[run_index, 0] = 1.0 if death_turn == 0 else 0.0
        output[run_index, 1] = death_turn
        output[run_index, 2] = stage
        output[run_index, 3] = worst_stage
        output[run_index, 4] = shortage_turns
        output[run_index, 5] = barter_choices
        output[run_index, 6] = subsistence_choices
        output[run_index, 7] = final_worst
        output[run_index, 8] = minimum_good
        output[run_index, 9] = production
        output[run_index, 10] = transitions
        output[run_index, 11] = recoveries
        output[run_index, 12] = activation_turn
        output[run_index, 13] = health
        output[run_index, 14] = alternative_turns
        output[run_index, 15] = observed_turns

    return screen_kernel


def run_gpu_chunk(parameter_rows: list[tuple[float, ...]], turns: int,
                  seeds: tuple[int, ...], scenario_indices: tuple[int, ...],
                  policy_indices: tuple[int, ...], kernel=None):
    import numpy as np
    from numba import cuda

    if not cuda.is_available():
        raise RuntimeError("CUDA is not available in this Python/WSL environment")
    kernel = kernel or _build_cuda_kernel(cuda)
    parameters = np.asarray(parameter_rows, dtype=np.float32)
    seed_values = np.asarray(seeds, dtype=np.int64)
    scenario_values = np.asarray(scenario_indices, dtype=np.int32)
    policy_values = np.asarray(policy_indices, dtype=np.int32)
    runs = len(parameter_rows) * len(seeds) * len(scenario_indices) * len(policy_indices)
    device_parameters = cuda.to_device(parameters)
    device_seeds = cuda.to_device(seed_values)
    device_scenarios = cuda.to_device(scenario_values)
    device_policies = cuda.to_device(policy_values)
    device_output = cuda.device_array((runs, len(OUTPUT_KEYS)), dtype=np.float32)
    # 小規模self-check/CUDA simulatorで大量のinactive threadを作らない。
    # 本走査ではruns>=128なので従来どおり128 threads/block。
    threads = min(128, max(1, runs))
    blocks = (runs + threads - 1) // threads
    kernel[blocks, threads](device_parameters, device_seeds, device_scenarios,
                            device_policies, turns, device_output)
    cuda.synchronize()
    return device_output.copy_to_host(), kernel


def save_results(connection: sqlite3.Connection, key: str, config_ids: list[int],
                 aggregates: list[dict]) -> None:
    now = utc_now()
    rows = []
    for config_id, aggregate in zip(config_ids, aggregates):
        rows.append((
            key, config_id, MODEL_VERSION, aggregate["run_count"],
            aggregate["screen_score"], aggregate["survival_proxy_rate"],
            aggregate["mean_lifespan_ratio"], aggregate["shortage_rate"],
            aggregate["alternative_use_rate"], aggregate["mean_final_health"],
            aggregate["mean_goods_safety"], aggregate["transition_count"],
            aggregate["recovery_count"], aggregate["recovery_ratio"], now,
        ))
    connection.executemany("""
        INSERT OR REPLACE INTO gpu_screen_results (
            matrix_key, config_id, model_version, run_count, screen_score,
            survival_proxy_rate, mean_lifespan_ratio, shortage_rate,
            alternative_use_rate, mean_final_health, mean_goods_safety,
            transition_count, recovery_count, recovery_ratio, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    connection.commit()


def write_top_file(connection: sqlite3.Connection, key: str, path: Path,
                   top_count: int, settings: dict) -> None:
    rows = connection.execute("""
        SELECT config_id, screen_score, survival_proxy_rate, shortage_rate,
               alternative_use_rate, mean_final_health, mean_goods_safety
        FROM gpu_screen_results WHERE matrix_key = ?
        ORDER BY screen_score DESC, config_id ASC LIMIT ?
    """, (key, top_count)).fetchall()
    payload = {
        "model_version": MODEL_VERSION, "matrix_key": key,
        "warning": "approximate GPU screening only; validate with CPU simulate_policy",
        "settings": settings,
        "candidates": [
            {"config_id": row[0], "screen_score": row[1],
             "survival_proxy_rate": row[2], "shortage_rate": row[3],
             "alternative_use_rate": row[4], "mean_final_health": row[5],
             "mean_goods_safety": row[6]}
            for row in rows
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--turns", type=int, default=1920)
    parser.add_argument("--seeds", default="1-10")
    parser.add_argument("--scenarios", default=",".join(SCENARIOS))
    parser.add_argument("--policies", default=",".join(POLICIES))
    parser.add_argument("--chunk-configs", type=int, default=2048)
    parser.add_argument("--limit", type=int, default=0,
                        help="0 means all pending configs")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--top", type=int, default=5000)
    parser.add_argument("--top-file", type=Path, default=DEFAULT_TOP_PATH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.turns < 1 or args.chunk_configs < 1 or args.limit < 0 or args.offset < 0:
        parser.error("turns/chunk-configs must be positive; limit/offset must be non-negative")
    try:
        seeds = batch_sweep.parse_seed_spec(args.seeds)
        scenarios = parse_names(args.scenarios, SCENARIOS, "scenarios")
        policies = parse_names(args.policies, POLICIES, "policies")
    except ValueError as exc:
        parser.error(str(exc))
    scenario_indices = tuple(SCENARIOS.index(name) for name in scenarios)
    policy_indices = tuple(POLICIES.index(name) for name in policies)
    key = matrix_key(args.turns, scenarios, seeds, policies)
    settings = {
        "turns": args.turns, "seeds": list(seeds),
        "scenarios": list(scenarios), "policies": list(policies),
    }
    if not args.db.exists():
        parser.error(f"database does not exist: {args.db}")
    with sqlite3.connect(args.db) as connection:
        ensure_tables(connection)
        count = available_config_count(connection)
        selected_count = min(args.limit, max(0, count - args.offset)) if args.limit else max(0, count - args.offset)
        size = screen_matrix_size(selected_count, len(scenarios), len(seeds), len(policies))
        print(f"model={MODEL_VERSION} / matrix_key={key}")
        print(f"configs={selected_count:,} / runs={size['runs']:,} / "
              f"raw output={batch_sweep._human_bytes(size['output_bytes'])}")
        print("mode=approximate GPU pre-screen; CPU validation remains mandatory")
        if args.dry_run:
            return
        pending = pending_config_ids(connection, key, args.limit, args.offset)
        connection.executemany(
            "INSERT OR REPLACE INTO gpu_screen_metadata(matrix_key,key,value) VALUES(?,?,?)",
            [(key, name, json.dumps(value, ensure_ascii=False))
             for name, value in {"settings": settings, "model_version": MODEL_VERSION}.items()])
        connection.commit()
        if not pending:
            print("all selected configs are already complete")
        kernel = None
        started = time.perf_counter()
        runs_per_config = len(scenarios) * len(seeds) * len(policies)
        for start in range(0, len(pending), args.chunk_configs):
            ids = pending[start:start + args.chunk_configs]
            rows = load_parameter_rows(connection, ids)
            output, kernel = run_gpu_chunk(
                rows, args.turns, seeds, scenario_indices, policy_indices, kernel)
            aggregates = aggregate_numpy_rows(
                output, len(ids), runs_per_config, args.turns)
            save_results(connection, key, ids, aggregates)
            done = start + len(ids)
            elapsed = time.perf_counter() - started
            rate = done / elapsed if elapsed else 0.0
            eta = (len(pending) - done) / rate if rate else 0.0
            print(f"{done:,}/{len(pending):,} configs / {rate:.1f} configs/s / "
                  f"ETA {eta / 60:.1f} min", flush=True)
        write_top_file(connection, key, args.top_file, min(args.top, count), settings)
        completed = connection.execute(
            "SELECT COUNT(*) FROM gpu_screen_results WHERE matrix_key=?", (key,)).fetchone()[0]
        print(f"complete={completed:,} / top={args.top_file}")


if __name__ == "__main__":
    main()
