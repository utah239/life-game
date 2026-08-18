#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""物々交換・自給パラメータの並列段階走査。

標準ライブラリだけで、候補生成、隔離workerの並列実行、SQLiteへの再開可能な
保存、Pareto/複数観点による次段階選抜、JSON/CSV/HTMLレポート生成まで行う。
重い走査はPCで行い、生成した自己完結HTMLだけをNASへ置ける設計。
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import html
import itertools
import json
import math
import os
from pathlib import Path
import random
import sqlite3
import statistics
import subprocess
import sys
import time
import zlib


PROJECT_DIR = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = PROJECT_DIR / "dashboard"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from dashboard.experiment_parameters import (  # noqa: E402
    PARAMETER_SPECS,
    default_values,
    validate_request,
)


SWEEP_SCHEMA_VERSION = 1
WORKER_PATH = DASHBOARD_DIR / "batch_worker.py"
DEFAULT_DB_PATH = DASHBOARD_DIR / "sweeps" / "barter_sweep.sqlite3"
DEFAULT_REPORT_DIR = DASHBOARD_DIR / "reports"
POLICIES = ("cautious", "ambitious", "family")
TUNABLE_SPECS = tuple(spec for spec in PARAMETER_SPECS if spec.target_attr)
TUNABLE_KEYS = tuple(spec.key for spec in TUNABLE_SPECS)
STAGE_KEYS = tuple(
    f"stage{stage}_{direction}"
    for stage in (1, 2, 3) for direction in ("exit", "enter")
)
NON_STAGE_SPECS = tuple(spec for spec in TUNABLE_SPECS if spec.key not in STAGE_KEYS)
PROFILE_NAMES = ("balanced", "survival", "diversity", "recovery", "transition")


@dataclass(frozen=True)
class StageSpec:
    name: str
    turns: int
    seeds: tuple[int, ...]
    keep: int | None
    scenario: str = "natural"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_seed_spec(value: str) -> tuple[int, ...]:
    """`1-5,20,30-31`を重複のないseed列へ変換する。"""
    seeds = []
    for raw_part in value.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            start, end = int(left), int(right)
            if start > end:
                raise ValueError(f"invalid seed range: {part}")
            seeds.extend(range(start, end + 1))
        else:
            seeds.append(int(part))
    unique = tuple(dict.fromkeys(seeds))
    if not unique or any(seed < 0 or seed > 2147483647 for seed in unique):
        raise ValueError("seed must be between 0 and 2147483647")
    return unique


def _decimal_places(step: float) -> int:
    text = f"{step:.12f}".rstrip("0")
    return len(text.split(".", 1)[1]) if "." in text else 0


def _quantize(spec, unit_value: float) -> float:
    """0<=unit_value<=1をParameterSpecの刻みへ写す。"""
    unit_value = max(0.0, min(1.0, unit_value))
    steps = int(round((spec.maximum - spec.minimum) / spec.step))
    index = min(steps, int(math.floor(unit_value * (steps + 1))))
    value = spec.minimum + index * spec.step
    value = round(value, _decimal_places(spec.step))
    return int(value) if spec.kind == "integer" else value


def _random_stage_values(rng: random.Random, *, local: bool = False) -> dict:
    specs = {spec.key: spec for spec in TUNABLE_SPECS if spec.key in STAGE_KEYS}
    defaults = default_values()
    for _ in range(10000):
        values = {}
        for key, spec in specs.items():
            if local:
                radius = 0.18 * (spec.maximum - spec.minimum)
                raw = rng.uniform(defaults[key] - radius, defaults[key] + radius)
                unit = (raw - spec.minimum) / (spec.maximum - spec.minimum)
            else:
                unit = rng.random()
            values[key] = _quantize(spec, unit)
        exits = [values[f"stage{stage}_exit"] for stage in (1, 2, 3)]
        enters = [values[f"stage{stage}_enter"] for stage in (1, 2, 3)]
        if (all(a < b for a, b in zip(exits, exits[1:]))
                and all(a < b for a, b in zip(enters, enters[1:]))
                and all(a < b for a, b in zip(exits, enters))):
            return values
    raise RuntimeError("could not generate valid stage boundaries")


def config_hash(parameters: dict) -> str:
    canonical = json.dumps(
        {key: parameters[key] for key in sorted(TUNABLE_KEYS)},
        ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_tunable_parameters(parameters: dict) -> dict:
    values = default_values()
    values.update(parameters)
    validated = validate_request({"values": values})["values"]
    return {key: validated[key] for key in TUNABLE_KEYS}


def generate_candidates(count: int, search_seed: int,
                        local_fraction: float = 0.25) -> list[dict]:
    """既定値+global Latin-hypercube+既定値周辺探索を決定論的に生成する。"""
    if count < 1:
        raise ValueError("candidate count must be at least 1")
    if not 0.0 <= local_fraction <= 1.0:
        raise ValueError("local_fraction must be between 0 and 1")
    rng = random.Random(search_seed)
    defaults = _validate_tunable_parameters(default_values())
    candidates = [defaults]
    seen = {config_hash(defaults)}
    generated_count = count - 1
    if generated_count == 0:
        return candidates

    permutations = {}
    for spec in NON_STAGE_SPECS:
        order = list(range(generated_count))
        rng.shuffle(order)
        permutations[spec.key] = order

    local_start = generated_count - round(generated_count * local_fraction)
    for index in range(generated_count):
        local = index >= local_start
        row = {}
        for spec in NON_STAGE_SPECS:
            if local:
                radius = 0.18 * (spec.maximum - spec.minimum)
                raw = rng.uniform(defaults[spec.key] - radius,
                                  defaults[spec.key] + radius)
                unit = (raw - spec.minimum) / (spec.maximum - spec.minimum)
            else:
                stratum = permutations[spec.key][index]
                unit = (stratum + rng.random()) / generated_count
            row[spec.key] = _quantize(spec, unit)
        row.update(_random_stage_values(rng, local=local))
        row = _validate_tunable_parameters(row)
        digest = config_hash(row)
        if digest not in seen:
            candidates.append(row)
            seen.add(digest)

    # 刻みへの量子化で重複した場合だけ、独立乱数で不足分を埋める。
    while len(candidates) < count:
        row = {}
        for spec in NON_STAGE_SPECS:
            row[spec.key] = _quantize(spec, rng.random())
        row.update(_random_stage_values(rng))
        row = _validate_tunable_parameters(row)
        digest = config_hash(row)
        if digest not in seen:
            candidates.append(row)
            seen.add(digest)
    return candidates


def compute_code_fingerprint() -> str:
    """結果の混在を防ぐため、シミュレーションに関わるPythonを指紋化する。"""
    paths = list(PROJECT_DIR.glob("*.py"))
    paths.extend((PROJECT_DIR / "institutions").glob("*.py"))
    paths.extend((DASHBOARD_DIR / name for name in (
        "experiment_parameters.py", "batch_worker.py")))
    digest = hashlib.sha256()
    for path in sorted(paths):
        if path.name.startswith("test_"):
            continue
        digest.update(str(path.relative_to(PROJECT_DIR)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS configs (
    id INTEGER PRIMARY KEY,
    config_hash TEXT NOT NULL UNIQUE,
    generation_index INTEGER NOT NULL,
    parameters_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stage_results (
    config_id INTEGER NOT NULL REFERENCES configs(id),
    stage_name TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    aggregate_json TEXT,
    survival_rate REAL,
    mean_lifespan_ratio REAL,
    pass_rate REAL,
    policy_diversity REAL,
    regime_diversity REAL,
    alternative_observation_score REAL,
    shortage_rate REAL,
    rapid_recross_rate REAL,
    pareto_front INTEGER NOT NULL DEFAULT 0,
    selected_for_next INTEGER NOT NULL DEFAULT 0,
    selection_rank INTEGER,
    started_at TEXT,
    completed_at TEXT,
    PRIMARY KEY(config_id, stage_name)
);
CREATE TABLE IF NOT EXISTS evaluations (
    config_id INTEGER NOT NULL REFERENCES configs(id),
    stage_name TEXT NOT NULL,
    seed INTEGER NOT NULL,
    policy TEXT NOT NULL,
    turns INTEGER NOT NULL,
    survived INTEGER NOT NULL,
    lifespan_turns INTEGER NOT NULL,
    pass_count INTEGER NOT NULL,
    fail_count INTEGER NOT NULL,
    na_count INTEGER NOT NULL,
    barter_choices INTEGER NOT NULL,
    subsistence_choices INTEGER NOT NULL,
    normal_choice_total INTEGER NOT NULL,
    shortage_count INTEGER NOT NULL,
    barter_stage INTEGER,
    enforcement_stage INTEGER,
    local_credit_stage INTEGER,
    currency_stage INTEGER,
    bank_stage INTEGER,
    min_energy REAL,
    final_health REAL,
    min_food REAL,
    min_medicine REAL,
    min_shelter REAL,
    min_tools REAL,
    summary_zlib BLOB NOT NULL,
    PRIMARY KEY(config_id, stage_name, seed, policy)
);
CREATE INDEX IF NOT EXISTS idx_stage_status
    ON stage_results(stage_name, status);
CREATE INDEX IF NOT EXISTS idx_eval_stage
    ON evaluations(stage_name, config_id);
"""


class SweepDatabase:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.executescript(SCHEMA_SQL)

    def close(self):
        self.connection.close()

    def metadata(self) -> dict:
        return {row["key"]: row["value"]
                for row in self.connection.execute("SELECT key, value FROM metadata")}

    def initialize(self, plan: dict, code_fingerprint: str,
                   candidates: list[dict]) -> None:
        expected = {
            "schema_version": str(SWEEP_SCHEMA_VERSION),
            "plan_json": json.dumps(plan, ensure_ascii=False, separators=(",", ":"),
                                    sort_keys=True),
            "code_fingerprint": code_fingerprint,
        }
        existing = self.metadata()
        if existing:
            for key, value in expected.items():
                if existing.get(key) != value:
                    raise ValueError(
                        f"既存DBの{key}が現在の走査条件と異なります。"
                        "別の--dbを指定してください")
            count = self.connection.execute("SELECT COUNT(*) FROM configs").fetchone()[0]
            if count != len(candidates):
                raise ValueError("既存DBの候補数が現在の走査条件と異なります")
            return
        with self.connection:
            for key, value in expected.items():
                self.connection.execute(
                    "INSERT INTO metadata(key,value) VALUES (?,?)", (key, value))
            self.connection.execute(
                "INSERT INTO metadata(key,value) VALUES (?,?)", ("created_at", utc_now()))
            self.connection.executemany(
                "INSERT INTO configs(config_hash,generation_index,parameters_json,created_at) "
                "VALUES (?,?,?,?)",
                [(config_hash(parameters), index,
                  json.dumps(parameters, ensure_ascii=False, separators=(",", ":"),
                             sort_keys=True), utc_now())
                 for index, parameters in enumerate(candidates)])

    def all_config_ids(self) -> list[int]:
        return [row[0] for row in self.connection.execute(
            "SELECT id FROM configs ORDER BY generation_index")]

    def baseline_config_id(self) -> int:
        return self.connection.execute(
            "SELECT id FROM configs WHERE generation_index=0").fetchone()[0]

    def config_parameters(self, config_id: int) -> dict:
        row = self.connection.execute(
            "SELECT parameters_json FROM configs WHERE id=?", (config_id,)).fetchone()
        return json.loads(row[0])

    def pending_ids(self, stage_name: str, candidate_ids: list[int]) -> list[int]:
        completed = {row[0] for row in self.connection.execute(
            "SELECT config_id FROM stage_results WHERE stage_name=? AND status='completed'",
            (stage_name,))}
        return [config_id for config_id in candidate_ids if config_id not in completed]

    def mark_running(self, config_id: int, stage_name: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO stage_results(config_id,stage_name,status,started_at) "
                "VALUES (?,?,?,?) ON CONFLICT(config_id,stage_name) DO UPDATE SET "
                "status='running', error=NULL, started_at=excluded.started_at",
                (config_id, stage_name, "running", utc_now()))

    def save_failure(self, config_id: int, stage_name: str, error: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO stage_results(config_id,stage_name,status,error,completed_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(config_id,stage_name) DO UPDATE SET "
                "status='failed', error=excluded.error, completed_at=excluded.completed_at",
                (config_id, stage_name, "failed", error[-4000:], utc_now()))

    def save_success(self, config_id: int, stage_name: str,
                     evaluations: list[dict], aggregate: dict) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM evaluations WHERE config_id=? AND stage_name=?",
                (config_id, stage_name))
            for row in evaluations:
                normal = row["normal_counts"]
                tally = row["criteria_tally"]
                finals = row["finals"]
                goods = row["goods_min_seen"]
                packed = zlib.compress(json.dumps(
                    row, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)
                self.connection.execute(
                    """INSERT INTO evaluations(
                        config_id,stage_name,seed,policy,turns,survived,lifespan_turns,
                        pass_count,fail_count,na_count,barter_choices,subsistence_choices,
                        normal_choice_total,shortage_count,barter_stage,enforcement_stage,
                        local_credit_stage,currency_stage,bank_stage,min_energy,final_health,
                        min_food,min_medicine,min_shelter,min_tools,summary_zlib)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (config_id, stage_name, row["seed"], row["policy"],
                     row["turns_requested"], int(row["survived"]), row["observed_turns"],
                     tally["pass"], tally["fail"], tally["na"],
                     normal.get("barter", 0), normal.get("subsistence", 0),
                     sum(normal.values()), row["barter_shortage_penalty_applied_count"],
                     finals.get("barter_stage"), finals.get("enforcement_stage"),
                     finals.get("local_credit_stage"), finals.get("currency_stage"),
                     finals.get("bank_stage"), row["min_seen"].get("energy"),
                     row["traits"].get("health"), goods.get("food"),
                     goods.get("medicine"), goods.get("shelter"), goods.get("tools"),
                     packed))
            self.connection.execute(
                """INSERT INTO stage_results(
                    config_id,stage_name,status,aggregate_json,survival_rate,
                    mean_lifespan_ratio,pass_rate,policy_diversity,regime_diversity,
                    alternative_observation_score,shortage_rate,rapid_recross_rate,
                    completed_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(config_id,stage_name) DO UPDATE SET
                    status='completed',error=NULL,aggregate_json=excluded.aggregate_json,
                    survival_rate=excluded.survival_rate,
                    mean_lifespan_ratio=excluded.mean_lifespan_ratio,
                    pass_rate=excluded.pass_rate,policy_diversity=excluded.policy_diversity,
                    regime_diversity=excluded.regime_diversity,
                    alternative_observation_score=excluded.alternative_observation_score,
                    shortage_rate=excluded.shortage_rate,
                    rapid_recross_rate=excluded.rapid_recross_rate,
                    completed_at=excluded.completed_at""",
                (config_id, stage_name, "completed",
                 json.dumps(aggregate, ensure_ascii=False, separators=(",", ":")),
                 aggregate["survival_rate"], aggregate["mean_lifespan_ratio"],
                 aggregate["pass_rate"], aggregate["policy_diversity"],
                 aggregate["regime_diversity"],
                 aggregate["alternative_observation_score"],
                 aggregate["shortage_rate"], aggregate["rapid_recross_rate"], utc_now()))

    def evaluation_summaries(self, config_id: int,
                             stage_names: tuple[str, ...]) -> list[dict]:
        placeholders = ",".join("?" for _ in stage_names)
        rows = self.connection.execute(
            f"SELECT summary_zlib FROM evaluations WHERE config_id=? "
            f"AND stage_name IN ({placeholders}) ORDER BY stage_name,policy,seed",
            (config_id, *stage_names)).fetchall()
        return [json.loads(zlib.decompress(row[0])) for row in rows]

    def save_aggregate_only(self, config_id: int, stage_name: str,
                            aggregate: dict) -> None:
        """複数stageを合わせた検証用の仮想stageを保存する。"""
        with self.connection:
            self.connection.execute(
                """INSERT INTO stage_results(
                    config_id,stage_name,status,aggregate_json,survival_rate,
                    mean_lifespan_ratio,pass_rate,policy_diversity,regime_diversity,
                    alternative_observation_score,shortage_rate,rapid_recross_rate,
                    completed_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(config_id,stage_name) DO UPDATE SET
                    status='completed',error=NULL,aggregate_json=excluded.aggregate_json,
                    survival_rate=excluded.survival_rate,
                    mean_lifespan_ratio=excluded.mean_lifespan_ratio,
                    pass_rate=excluded.pass_rate,policy_diversity=excluded.policy_diversity,
                    regime_diversity=excluded.regime_diversity,
                    alternative_observation_score=excluded.alternative_observation_score,
                    shortage_rate=excluded.shortage_rate,
                    rapid_recross_rate=excluded.rapid_recross_rate,
                    completed_at=excluded.completed_at""",
                (config_id, stage_name, "completed",
                 json.dumps(aggregate, ensure_ascii=False, separators=(",", ":")),
                 aggregate["survival_rate"], aggregate["mean_lifespan_ratio"],
                 aggregate["pass_rate"], aggregate["policy_diversity"],
                 aggregate["regime_diversity"],
                 aggregate["alternative_observation_score"],
                 aggregate["shortage_rate"], aggregate["rapid_recross_rate"], utc_now()))

    def completed_stage_rows(self, stage_name: str) -> list[dict]:
        rows = self.connection.execute(
            "SELECT config_id,aggregate_json FROM stage_results "
            "WHERE stage_name=? AND status='completed'", (stage_name,)).fetchall()
        return [dict(json.loads(row["aggregate_json"]), config_id=row["config_id"])
                for row in rows]

    def apply_scoring(self, stage_name: str, scored_rows: list[dict],
                      selected_ids: list[int]) -> None:
        selected_rank = {config_id: rank + 1
                         for rank, config_id in enumerate(selected_ids)}
        with self.connection:
            self.connection.execute(
                "UPDATE stage_results SET pareto_front=0,selected_for_next=0,selection_rank=NULL "
                "WHERE stage_name=?", (stage_name,))
            for row in scored_rows:
                self.connection.execute(
                    "UPDATE stage_results SET aggregate_json=?,pareto_front=?,"
                    "selected_for_next=?,selection_rank=? WHERE config_id=? AND stage_name=?",
                    (json.dumps(row, ensure_ascii=False, separators=(",", ":")),
                     int(row.get("pareto_front", False)),
                     int(row["config_id"] in selected_rank),
                     selected_rank.get(row["config_id"]), row["config_id"], stage_name))

    def database_bytes(self) -> int:
        total = self.path.stat().st_size if self.path.exists() else 0
        for suffix in ("-wal", "-shm"):
            extra = Path(str(self.path) + suffix)
            if extra.exists():
                total += extra.stat().st_size
        return total


def _mean(values) -> float:
    values = list(values)
    return statistics.fmean(values) if values else 0.0


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _policy_diversity(evaluations: list[dict]) -> float:
    by_policy = {}
    keys = set()
    for row in evaluations:
        bucket = by_policy.setdefault(row["policy"], {})
        for key, count in row["normal_counts"].items():
            bucket[key] = bucket.get(key, 0) + count
            keys.add(key)
    shares = {}
    for policy, counts in by_policy.items():
        total = sum(counts.values())
        shares[policy] = {key: counts.get(key, 0) / total if total else 0.0
                          for key in keys}
    distances = []
    for left, right in itertools.combinations(sorted(shares), 2):
        distances.append(sum(abs(shares[left][key] - shares[right][key])
                             for key in keys) / 2.0)
    return _mean(distances)


def _regime_diversity(evaluations: list[dict]) -> float:
    if len(evaluations) < 2:
        return 0.0
    institution_names = sorted({
        name for row in evaluations for name in row["institutions"]
    })
    values = []
    denominator = min(4, len(evaluations)) - 1
    for name in institution_names:
        distinct = len({row["institutions"][name]["final_stage"]
                        for row in evaluations if name in row["institutions"]})
        values.append(_clamp01((distinct - 1) / denominator))
    return _mean(values)


def aggregate_evaluations(evaluations: list[dict]) -> dict:
    if not evaluations:
        raise ValueError("evaluations must not be empty")
    requested_turns = evaluations[0]["turns_requested"]
    observed_total = sum(row["observed_turns"] for row in evaluations)
    normal_total = sum(sum(row["normal_counts"].values()) for row in evaluations)
    alt_total = sum(row["normal_counts"].get("barter", 0)
                    + row["normal_counts"].get("subsistence", 0)
                    for row in evaluations)
    active_rows = [row for row in evaluations if row["barter_active"]]
    active_used = sum(
        row["normal_counts"].get("barter", 0)
        + row["normal_counts"].get("subsistence", 0) > 0
        for row in active_rows)
    transitions = 0
    recoveries = 0
    rapid_recrosses = 0
    for row in evaluations:
        for institution in row["institutions"].values():
            transitions += institution.get("transition_count", 0)
            recoveries += institution.get("recovery_count", 0)
            rapid_recrosses += institution.get("rapid_same_boundary_recross_count", 0)
    pass_count = sum(row["criteria_tally"]["pass"] for row in evaluations)
    fail_count = sum(row["criteria_tally"]["fail"] for row in evaluations)
    survival_rate = _mean(int(row["survived"]) for row in evaluations)
    lifespan_ratio = _mean(
        row["observed_turns"] / requested_turns for row in evaluations)
    pass_rate = pass_count / (pass_count + fail_count) if pass_count + fail_count else 0.0
    activation_rate = len(active_rows) / len(evaluations)
    active_usage_coverage = active_used / len(active_rows) if active_rows else 0.0
    shortage_rate = (sum(row["barter_shortage_penalty_applied_count"]
                         for row in evaluations) / observed_total
                     if observed_total else 0.0)
    rapid_recross_rate = (rapid_recrosses / observed_total * 100.0
                          if observed_total else 0.0)
    recovery_ratio = recoveries / transitions if transitions else 0.0
    policy_diversity = _policy_diversity(evaluations)
    regime_diversity = _regime_diversity(evaluations)
    alternative_observation_score = activation_rate * active_usage_coverage
    mean_final_health = _mean(row["traits"].get("health", 0.0)
                              for row in evaluations)
    mean_min_energy = _mean(row["min_seen"].get("energy", 0.0)
                            for row in evaluations)
    shortage_score = 1.0 - _clamp01(shortage_rate)
    chatter_score = 1.0 - _clamp01(rapid_recross_rate)
    health_score = _clamp01(mean_final_health / 100.0)
    energy_score = _clamp01(mean_min_energy / 100.0)
    alt_choice_rate = alt_total / normal_total if normal_total else 0.0
    scores = {
        "balanced": (
            0.25 * survival_rate + 0.15 * lifespan_ratio + 0.15 * pass_rate
            + 0.10 * policy_diversity + 0.10 * regime_diversity
            + 0.10 * alternative_observation_score + 0.10 * shortage_score
            + 0.05 * chatter_score),
        "survival": (
            0.40 * survival_rate + 0.25 * lifespan_ratio + 0.15 * health_score
            + 0.10 * energy_score + 0.10 * shortage_score),
        "diversity": (
            0.35 * policy_diversity + 0.25 * regime_diversity
            + 0.15 * alternative_observation_score + 0.10 * pass_rate
            + 0.15 * survival_rate),
        "recovery": (
            0.30 * recovery_ratio + 0.20 * chatter_score + 0.15 * pass_rate
            + 0.15 * survival_rate + 0.10 * alternative_observation_score
            + 0.10 * lifespan_ratio),
        "transition": (
            0.30 * alternative_observation_score + 0.15 * alt_choice_rate
            + 0.15 * regime_diversity + 0.15 * survival_rate
            + 0.15 * pass_rate + 0.10 * shortage_score),
    }
    return {
        "evaluation_count": len(evaluations),
        "turns": requested_turns,
        "survival_rate": survival_rate,
        "mean_lifespan_ratio": lifespan_ratio,
        "pass_rate": pass_rate,
        "activation_rate": activation_rate,
        "active_usage_coverage": active_usage_coverage,
        "alternative_observation_score": alternative_observation_score,
        "alternative_choice_rate": alt_choice_rate,
        "shortage_rate": shortage_rate,
        "rapid_recross_rate": rapid_recross_rate,
        "recovery_ratio": recovery_ratio,
        "policy_diversity": policy_diversity,
        "regime_diversity": regime_diversity,
        "mean_final_health": mean_final_health,
        "mean_min_energy": mean_min_energy,
        "profile_scores": scores,
    }


PARETO_KEYS = (
    "survival_rate", "pass_rate", "policy_diversity",
    "alternative_observation_score",
)


def _pareto_vector(row: dict) -> tuple[float, ...]:
    return tuple(row[key] for key in PARETO_KEYS) + (
        1.0 - _clamp01(row["shortage_rate"]),
        1.0 - _clamp01(row["rapid_recross_rate"]),
    )


def pareto_front_ids(rows: list[dict]) -> set[int]:
    """全目的が同等以上かつ1目的以上が良い候補だけをfrontとして返す。"""
    vectors = [(row["config_id"], _pareto_vector(row)) for row in rows]
    front = set()
    for config_id, vector in vectors:
        dominated = False
        for other_id, other in vectors:
            if other_id == config_id:
                continue
            if all(a >= b for a, b in zip(other, vector)) and any(
                    a > b for a, b in zip(other, vector)):
                dominated = True
                break
        if not dominated:
            front.add(config_id)
    return front


def score_and_select(rows: list[dict], keep: int | None,
                     pinned_ids: set[int] | None = None) -> tuple[list[dict], list[int]]:
    front = pareto_front_ids(rows)
    for row in rows:
        row["pareto_front"] = row["config_id"] in front
    if keep is None:
        return rows, []
    keep = min(keep, len(rows))
    orders = [
        sorted((row for row in rows if row["pareto_front"]),
               key=lambda row: row["profile_scores"]["balanced"], reverse=True),
    ]
    orders.extend(sorted(rows, key=lambda row, profile=profile:
                         row["profile_scores"][profile], reverse=True)
                  for profile in PROFILE_NAMES)
    selected = []
    seen = set()
    cursors = [0] * len(orders)
    while len(selected) < keep:
        progressed = False
        for list_index, order in enumerate(orders):
            while cursors[list_index] < len(order):
                row = order[cursors[list_index]]
                cursors[list_index] += 1
                if row["config_id"] not in seen:
                    selected.append(row["config_id"])
                    seen.add(row["config_id"])
                    progressed = True
                    break
            if len(selected) >= keep:
                break
        if not progressed:
            break
    # 既定値は探索候補の評価基準でもあるため、ランキングが低くても全段階で
    # 対照群として残す。先頭へ置いて「最良」と誤読させず、末尾の1枠を使う。
    available_ids = {row["config_id"] for row in rows}
    for pinned_id in sorted((pinned_ids or set()) & available_ids):
        if pinned_id in seen:
            continue
        if len(selected) >= keep:
            removed = selected.pop()
            seen.remove(removed)
        selected.append(pinned_id)
        seen.add(pinned_id)
    return rows, selected


def run_worker(parameters: dict, stage: StageSpec, policies: tuple[str, ...],
               safety_floor: int, talent: str, timeout_seconds: int,
               shortfall_reference_mode: str = "coupled") -> list[dict]:
    payload = {
        "parameters": parameters,
        "turns": stage.turns,
        "seeds": list(stage.seeds),
        "policies": list(policies),
        "safety_floor": safety_floor,
        "talent": talent,
        "scenario": stage.scenario,
        "shortfall_reference_mode": shortfall_reference_mode,
    }
    proc = subprocess.run(
        [sys.executable, str(WORKER_PATH)], cwd=PROJECT_DIR,
        input=json.dumps(payload, ensure_ascii=False), text=True,
        capture_output=True, encoding="utf-8", timeout=timeout_seconds,
        check=False)
    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "worker failed"
        raise RuntimeError(detail)
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"worker returned invalid JSON: {exc}") from exc
    return result["evaluations"]


def _human_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024.0 or unit == "TiB":
            return f"{amount:.1f}{unit}"
        amount /= 1024.0
    return f"{amount:.1f}TiB"


def run_stage(database: SweepDatabase, stage: StageSpec, candidate_ids: list[int],
              policies: tuple[str, ...], safety_floor: int, talent: str,
              workers: int, timeout_seconds: int, progress_every: int,
              shortfall_reference_mode: str = "coupled") -> None:
    if shortfall_reference_mode not in ("coupled", "fixed"):
        raise ValueError(
            "shortfall_reference_mode must be 'coupled' or 'fixed'")
    pending = database.pending_ids(stage.name, candidate_ids)
    completed_before = len(candidate_ids) - len(pending)
    print(f"\n[{stage.name}] {stage.turns} turns / {len(stage.seeds)} seeds / "
          f"{len(policies)} policies / candidates={len(candidate_ids)} "
          f"(resume済み={completed_before}, pending={len(pending)})")
    if not pending:
        return
    started = time.perf_counter()
    failures = []
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sweep")
    futures = {}
    pending_iter = iter(pending)

    def submit_next() -> bool:
        try:
            config_id = next(pending_iter)
        except StopIteration:
            return False
        database.mark_running(config_id, stage.name)
        parameters = database.config_parameters(config_id)
        future = executor.submit(
            run_worker, parameters, stage, policies, safety_floor, talent,
            timeout_seconds, shortfall_reference_mode)
        futures[future] = config_id
        return True

    # 何千件ものfutureを一括投入すると、Ctrl+C時にも全件がqueueに残って
    # 終了を待つことになる。実行中+次の1波だけに抑え、中断後の再開を軽くする。
    for _ in range(min(len(pending), workers * 2)):
        submit_next()
    done_count = 0
    try:
        while futures:
            future = next(as_completed(tuple(futures)))
            config_id = futures.pop(future)
            done_count += 1
            try:
                evaluations = future.result()
                aggregate = aggregate_evaluations(evaluations)
                database.save_success(config_id, stage.name, evaluations, aggregate)
            except Exception as exc:
                database.save_failure(config_id, stage.name, str(exc))
                failures.append((config_id, str(exc)))
            submit_next()
            if (done_count % max(1, progress_every) == 0
                    or done_count == len(pending)):
                elapsed = time.perf_counter() - started
                rate = done_count / elapsed if elapsed else 0.0
                remaining = (len(pending) - done_count) / rate if rate else 0.0
                print(f"  {done_count}/{len(pending)} config / "
                      f"{rate:.2f} config/s / ETA {remaining / 60:.1f} min / "
                      f"DB {_human_bytes(database.database_bytes())}")
    except KeyboardInterrupt:
        for future in futures:
            future.cancel()
        print("\n中断しました。完了済みconfigはDBに保存済みです。")
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    if failures:
        sample = "; ".join(f"#{config_id}: {error}" for config_id, error in failures[:3])
        raise RuntimeError(
            f"{stage.name}: {len(failures)} configs failed ({sample}). "
            "再実行するとfailed/running分だけ再試行します")


def _report_records(database: SweepDatabase, stage_name: str) -> list[dict]:
    records = []
    query = """SELECT c.id,c.config_hash,c.parameters_json,s.aggregate_json,
               s.pareto_front,s.selected_for_next,s.selection_rank
               FROM stage_results s JOIN configs c ON c.id=s.config_id
               WHERE s.stage_name=? AND s.status='completed'"""
    for row in database.connection.execute(query, (stage_name,)):
        aggregate = json.loads(row["aggregate_json"])
        aggregate.update({
            "config_id": row["id"], "config_hash": row["config_hash"],
            "parameters": json.loads(row["parameters_json"]),
            "pareto_front": bool(row["pareto_front"]),
            "selected_for_next": bool(row["selected_for_next"]),
            "selection_rank": row["selection_rank"],
        })
        records.append(aggregate)
    return sorted(records, key=lambda row: (
        not row["selected_for_next"], not row["pareto_front"],
        -row["profile_scores"]["balanced"]))


def _write_csv(path: Path, records: list[dict]) -> None:
    metric_keys = (
        "config_id", "config_hash", "pareto_front", "selected_for_next",
        "selection_rank", "evaluation_count", "turns", "survival_rate",
        "mean_lifespan_ratio", "pass_rate", "activation_rate",
        "active_usage_coverage", "alternative_observation_score",
        "alternative_choice_rate", "shortage_rate", "rapid_recross_rate",
        "recovery_ratio", "policy_diversity", "regime_diversity",
        "mean_final_health", "mean_min_energy",
    )
    fields = list(metric_keys)
    fields.extend(f"score_{profile}" for profile in PROFILE_NAMES)
    fields.extend(TUNABLE_KEYS)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in records:
            flat = {key: row.get(key) for key in metric_keys}
            flat.update({f"score_{name}": row["profile_scores"][name]
                         for name in PROFILE_NAMES})
            flat.update(row["parameters"])
            writer.writerow(flat)


def _build_report_html(stage_name: str, records: list[dict], database_bytes: int,
                       top_limit: int) -> str:
    selected_count = sum(row["selected_for_next"] for row in records)
    pareto_count = sum(row["pareto_front"] for row in records)
    top = records[:top_limit]
    points = [{
        "id": row["config_id"], "x": row["pass_rate"],
        "y": row["survival_rate"], "a": row["alternative_observation_score"],
        "selected": row["selected_for_next"], "pareto": row["pareto_front"],
    } for row in records]
    table_rows = []
    for row in top:
        params = html.escape(json.dumps(row["parameters"], ensure_ascii=False, indent=2))
        table_rows.append(
            "<tr>"
            f"<td>#{row['config_id']}</td>"
            f"<td>{'●' if row['pareto_front'] else ''}</td>"
            f"<td>{row['selection_rank'] or ''}</td>"
            f"<td>{row['survival_rate']:.1%}</td>"
            f"<td>{row['mean_lifespan_ratio']:.1%}</td>"
            f"<td>{row['pass_rate']:.1%}</td>"
            f"<td>{row['policy_diversity']:.3f}</td>"
            f"<td>{row['regime_diversity']:.3f}</td>"
            f"<td>{row['alternative_observation_score']:.3f}</td>"
            f"<td>{row['shortage_rate']:.3%}</td>"
            f"<td>{row['profile_scores']['balanced']:.3f}</td>"
            f"<td><details><summary>表示</summary><pre>{params}</pre></details></td>"
            "</tr>")
    points_json = json.dumps(points, separators=(",", ":"))
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Life Game sweep — {html.escape(stage_name)}</title>
<style>
:root{{color-scheme:light dark;--bg:#101617;--panel:#192123;--ink:#e7ebe4;--muted:#9aa69d;--rule:#34403c;--accent:#d3a55e}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,sans-serif}}
main{{max-width:1500px;margin:auto;padding:24px}}h1{{margin:0 0 4px}}.sub{{color:var(--muted)}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin:20px 0}}
.card,.plot,.table{{background:var(--panel);border:1px solid var(--rule);border-radius:8px;padding:14px}}.card b{{display:block;font-size:24px;color:var(--accent)}}
canvas{{width:100%;height:360px}}.table{{overflow:auto;margin-top:14px}}table{{border-collapse:collapse;width:100%;white-space:nowrap}}th,td{{padding:7px;border-bottom:1px solid var(--rule);text-align:right}}th:first-child,td:first-child,td:last-child{{text-align:left}}pre{{white-space:pre-wrap;min-width:300px}}summary{{cursor:pointer;color:var(--accent)}}
</style></head><body><main>
<h1>パラメータ走査レポート</h1><p class="sub">stage={html.escape(stage_name)} / {html.escape(utc_now())}</p>
<section class="cards"><div class="card"><span>評価候補</span><b>{len(records):,}</b></div>
<div class="card"><span>Pareto front</span><b>{pareto_count:,}</b></div>
<div class="card"><span>次段階へ選抜</span><b>{selected_count:,}</b></div>
<div class="card"><span>SQLite使用量</span><b>{html.escape(_human_bytes(database_bytes))}</b></div></section>
<section class="plot"><h2>生存率 × 適用基準PASS率</h2><p class="sub">色が明るいほど代替経済が実際に発火・利用された。白枠=選抜、橙枠=Pareto。</p><canvas id="plot" width="1200" height="360"></canvas></section>
<section class="table"><table><thead><tr><th>ID</th><th>Pareto</th><th>選抜順</th><th>生存率</th><th>寿命比</th><th>PASS率</th><th>方針差</th><th>制度差</th><th>代替観測</th><th>不足率</th><th>均衡score</th><th>パラメータ</th></tr></thead><tbody>{''.join(table_rows)}</tbody></table></section>
</main><script>
const points={points_json},c=document.getElementById('plot'),x=c.getContext('2d'),m=38,w=c.width-2*m,h=c.height-2*m;
x.strokeStyle='#66736d';x.fillStyle='#9aa69d';x.font='12px system-ui';x.beginPath();x.moveTo(m,m);x.lineTo(m,m+h);x.lineTo(m+w,m+h);x.stroke();
for(let i=0;i<=10;i++){{let px=m+w*i/10,py=m+h-h*i/10;x.fillText((i*10)+'%',px-10,m+h+20);x.fillText((i*10)+'%',4,py+4)}}
for(const p of points){{let px=m+w*p.x,py=m+h-h*p.y,light=Math.round(80+175*Math.min(1,p.a));x.beginPath();x.arc(px,py,p.selected?5:3,0,Math.PI*2);x.fillStyle=`rgb(${{light}},${{150+Math.round(90*p.a)}},80)`;x.fill();if(p.selected||p.pareto){{x.strokeStyle=p.pareto?'#d3a55e':'#fff';x.lineWidth=2;x.stroke()}}}}
</script></body></html>"""


def write_reports(database: SweepDatabase, stage_name: str, report_dir: Path,
                  top_limit: int = 200) -> tuple[Path, Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    records = _report_records(database, stage_name)
    json_path = report_dir / f"barter_sweep_{stage_name}.json"
    csv_path = report_dir / f"barter_sweep_{stage_name}.csv"
    html_path = report_dir / f"barter_sweep_{stage_name}.html"
    json_path.write_text(json.dumps({
        "schema_version": SWEEP_SCHEMA_VERSION, "stage": stage_name,
        "generated_at": utc_now(), "records": records,
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    _write_csv(csv_path, records)
    html_path.write_text(_build_report_html(
        stage_name, records, database.database_bytes(), top_limit), encoding="utf-8")
    html_files = sorted(report_dir.glob("barter_sweep_*.html"))
    links = "".join(
        f'<li><a href="{html.escape(path.name)}">{html.escape(path.stem)}</a></li>'
        for path in html_files)
    (report_dir / "index.html").write_text(
        "<!doctype html><html lang=ja><meta charset=utf-8><meta name=viewport "
        "content='width=device-width,initial-scale=1'><title>走査レポート</title>"
        "<style>body{font:16px system-ui;max-width:800px;margin:40px auto;padding:0 20px}"
        "li{margin:12px 0}</style><h1>パラメータ走査レポート</h1><ul>"
        + links + "</ul></html>", encoding="utf-8")
    return json_path, csv_path, html_path


def build_validated_stage(database: SweepDatabase, config_ids: list[int],
                          source_stages: tuple[str, ...] = ("final", "robust"),
                          stage_name: str = "validated",
                          recommendation_count: int = 20) -> tuple[list[dict], list[int]]:
    """既知seedと未使用seedを合算し、偶然でない最終候補を再評価する。"""
    for config_id in config_ids:
        evaluations = database.evaluation_summaries(config_id, source_stages)
        if not evaluations:
            raise RuntimeError(f"#{config_id}: validated source evaluations are missing")
        database.save_aggregate_only(
            config_id, stage_name, aggregate_evaluations(evaluations))
    rows = database.completed_stage_rows(stage_name)
    scored, selected = score_and_select(
        rows, min(recommendation_count, len(rows)))
    database.apply_scoring(stage_name, scored, selected)
    return scored, selected


def build_plan(args, stages: list[StageSpec], policies: tuple[str, ...]) -> dict:
    return {
        "candidate_count": args.candidates,
        "search_seed": args.search_seed,
        "local_fraction": args.local_fraction,
        "policies": list(policies),
        "safety_floor": args.safety_floor,
        "talent": args.talent,
        "stages": [asdict(stage) for stage in stages],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--candidates", type=int, default=5000)
    parser.add_argument("--workers", type=int,
                        default=min(10, max(1, (os.cpu_count() or 2) - 2)))
    parser.add_argument("--search-seed", type=int, default=20260816)
    parser.add_argument("--local-fraction", type=float, default=0.25)
    parser.add_argument("--policies", default=",".join(POLICIES))
    parser.add_argument("--safety-floor", type=int, default=30)
    parser.add_argument("--talent", choices=("random", "dexterity", "intellect", "skill", "health"),
                        default="random")
    parser.add_argument("--screen-turns", type=int, default=240)
    parser.add_argument("--screen-seeds", default="1-2")
    parser.add_argument("--screen-keep", type=int, default=500)
    parser.add_argument("--refine-turns", type=int, default=960)
    parser.add_argument("--refine-seeds", default="1-5")
    parser.add_argument("--refine-keep", type=int, default=50)
    parser.add_argument("--final-turns", type=int, default=1920)
    parser.add_argument("--final-seeds", default="1-5")
    parser.add_argument("--final-keep", type=int, default=50)
    parser.add_argument("--robust-seeds", default="101-105")
    parser.add_argument("--stop-after", choices=("screen", "refine", "final", "robust"),
                        default="robust")
    parser.add_argument("--worker-timeout", type=int, default=900)
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument("--report-top", type=int, default=200)
    parser.add_argument("--recommendations", type=int, default=20,
                        help="final+robust合算後にレポートで候補表示する件数")
    parser.add_argument("--dry-run", action="store_true",
                        help="候補生成・計画表示だけ行い、DB作成とsimulationをしない")
    args = parser.parse_args()

    policies = tuple(part.strip() for part in args.policies.split(",") if part.strip())
    if not policies or any(policy not in POLICIES for policy in policies):
        parser.error(f"--policies must be a subset of {POLICIES}")
    if args.workers < 1 or args.candidates < 1:
        parser.error("--workers and --candidates must be at least 1")
    if args.recommendations < 1:
        parser.error("--recommendations must be at least 1")
    if not 0 <= args.safety_floor <= 100:
        parser.error("--safety-floor must be between 0 and 100")
    try:
        stages = [
            StageSpec("screen", args.screen_turns, parse_seed_spec(args.screen_seeds),
                      args.screen_keep),
            StageSpec("refine", args.refine_turns, parse_seed_spec(args.refine_seeds),
                      args.refine_keep),
            StageSpec("final", args.final_turns, parse_seed_spec(args.final_seeds),
                      args.final_keep),
            StageSpec("robust", args.final_turns, parse_seed_spec(args.robust_seeds), None),
        ]
    except ValueError as exc:
        parser.error(str(exc))
    if any(stage.turns < 1 or (stage.keep is not None and stage.keep < 1) for stage in stages):
        parser.error("turns and keep values must be at least 1")

    candidates = generate_candidates(args.candidates, args.search_seed, args.local_fraction)
    plan = build_plan(args, stages, policies)
    screen_count = args.candidates
    refine_count = min(screen_count, stages[0].keep)
    final_count = min(refine_count, stages[1].keep)
    robust_count = min(final_count, stages[2].keep)
    evaluations = (
        screen_count * len(stages[0].seeds) * len(policies)
        + refine_count * len(stages[1].seeds) * len(policies)
        + final_count * len(stages[2].seeds) * len(policies)
        + robust_count * len(stages[3].seeds) * len(policies)
    )
    print(f"候補={len(candidates):,}, 最大評価数={evaluations:,}, workers={args.workers}")
    print(f"DB={args.db}\nreports={args.report_dir}")
    if args.dry_run:
        print("dry-run: simulationとDB作成は行いません")
        return

    database = SweepDatabase(args.db)
    try:
        database.initialize(plan, compute_code_fingerprint(), candidates)
        candidate_ids = database.all_config_ids()
        stop_index = [stage.name for stage in stages].index(args.stop_after)
        for index, stage in enumerate(stages[:stop_index + 1]):
            run_stage(
                database, stage, candidate_ids, policies, args.safety_floor,
                args.talent, args.workers, args.worker_timeout, args.progress_every)
            rows = database.completed_stage_rows(stage.name)
            if len(rows) != len(candidate_ids):
                raise RuntimeError(
                    f"{stage.name}: completed={len(rows)} / expected={len(candidate_ids)}")
            scored, selected = score_and_select(
                rows, stage.keep, {database.baseline_config_id()})
            database.apply_scoring(stage.name, scored, selected)
            paths = write_reports(database, stage.name, args.report_dir, args.report_top)
            print(f"  Pareto={sum(row['pareto_front'] for row in scored)}, "
                  f"selected={len(selected)}, reports={paths[2]}")
            if index < len(stages) - 1:
                candidate_ids = selected
        if args.stop_after == "robust":
            scored, selected = build_validated_stage(
                database, candidate_ids, recommendation_count=args.recommendations)
            paths = write_reports(
                database, "validated", args.report_dir, args.report_top)
            print(f"  validated(final+robust): "
                  f"Pareto={sum(row['pareto_front'] for row in scored)}, "
                  f"recommendations={len(selected)}, reports={paths[2]}")
        print(f"\n完了: DB {_human_bytes(database.database_bytes())}")
        print(f"レポート: {args.report_dir / 'index.html'}")
    finally:
        database.close()


if __name__ == "__main__":
    main()
