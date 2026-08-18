# -*- coding: utf-8 -*-
"""複数集落の初期配置と、人口移住の純粋ルール。

銀行・通貨・契約執行は世界共有、人口・必需財・生産力・物々交換Stage・
地域信用・共同体健康は集落ローカルという境界を採用する。このモジュールは人口と
ローカル経済のdictを運ぶだけで、財のupkeepや人口出生死亡の式は既存の
barter/populationモジュールを呼ぶ側に残す。

移住は人口の生成・消滅ではない。より高い生活圧力の集落から、存続中で圧力の
低い集落へ整数人口と再生産人口を移し、端数はsourceのcarryへ保存する。
"""
from __future__ import annotations

import copy

from institutions.population import (
    AGE_COHORT_CHILDREN,
    AGE_COHORT_ELDERLY,
    AGE_COHORT_KEYS,
    AGE_COHORT_PRODUCTIVE,
    POPULATION_STAGE_ABANDONED,
    POPULATION_STAGE_EXTINCT,
    initial_settlement,
    population_stage_next,
    upgrade_settlement_demography,
)
from institutions.local_credit import (
    LOCAL_CREDIT_STAGE_HEALTHY,
    LOCAL_CREDIT_TRUST_INITIAL,
)
from institutions.production_practice import (
    initial_production_practice,
    normalize_production_practice,
)
from institutions.barter import (
    GOODS_ACCOUNTING_VERSION,
    PROVISIONING_SCALE_INITIAL,
    normalize_provisioning_scale,
)
from institutions.household_needs import (
    REFERENCE_PROVISIONING_SCALE,
    demand_scales_by_good,
)


SETTLEMENT_NETWORK_VERSION = 6
RIVERSIDE_SETTLEMENT_ID = "riverside"
UPLAND_SETTLEMENT_ID = "upland"

INITIAL_SETTLEMENT_SPECS = (
    {"id": "home", "name": "はじまりの集落", "population": 120,
     "reproductive_population": 42, "economy_factors": (1, 1, 1, 1, 1),
     "trust_offset": 0.0},
    {"id": RIVERSIDE_SETTLEMENT_ID, "name": "川辺の集落", "population": 78,
     "reproductive_population": 28,
     "economy_factors": (1.05, 0.85, 1.10, 1.30, 1.35),
     "trust_offset": 6.0},
    {"id": UPLAND_SETTLEMENT_ID, "name": "高台の集落", "population": 52,
     "reproductive_population": 18,
     "economy_factors": (0.80, 1.05, 1.30, 0.75, 0.80),
     "trust_offset": -6.0},
)
INITIAL_TOTAL_POPULATION = sum(
    int(row["population"]) for row in INITIAL_SETTLEMENT_SPECS)
INITIAL_REPRODUCTIVE_POPULATION = sum(
    int(row["reproductive_population"]) for row in INITIAL_SETTLEMENT_SPECS)

COMMUNITY_HEALTH_INITIAL = 80.0
COMMUNITY_HEALTH_REVERSION_RATE = 0.02
MIGRATION_MIN_PRESSURE_GAP = 0.12
MIGRATION_RATE = 0.035
MIGRATION_MAX_PER_TURN = 6


def _local_economy(home_economy: dict, factors: tuple,
                   trust_offset: float = 0.0,
                   provisioning_scale: float = 1.0,
                   demand_scales: dict | None = None) -> dict:
    scale = normalize_provisioning_scale(provisioning_scale)
    goods = ("food", "medicine", "shelter", "tools")
    economy = {
        key: round(float(home_economy[key]) * factor * scale, 6)
        for key, factor in zip(goods, factors[:4])
    }
    economy["production_capacity"] = round(
        float(home_economy["production_capacity"]) * factors[4], 6)
    economy.update({
        "goods_accounting_version": GOODS_ACCOUNTING_VERSION,
        "provisioning_scale": scale,
        "demand_scales_by_good": dict(demand_scales or {
            good: scale for good in goods}),
        "barter_stage": int(home_economy["barter_stage"]),
        "community_trust": max(0.0, min(
            100.0, float(home_economy.get(
                "community_trust", LOCAL_CREDIT_TRUST_INITIAL)) + trust_offset)),
        "local_credit_stage": int(home_economy.get(
            "local_credit_stage", LOCAL_CREDIT_STAGE_HEALTHY)),
        "community_health": COMMUNITY_HEALTH_INITIAL,
        "worst_shortfall": 0.0,
        "worst_good": None,
        "trade_sent_total": 0.0,
        "trade_received_total": 0.0,
        "trade_events_total": 0,
        # 財在庫と違う0〜100の強度値。活動共同体の分裂では複製継承、
        # 合流では人口加重平均するため、加算可能フィールドへは入れない。
        "production_practice_by_good": initial_production_practice(),
    })
    return economy


def _proportional_counts(total: int, weights: list[int],
                         capacities: list[int] | None = None) -> list[int]:
    """整数合計を最大剰余法で決定論的に配る。"""
    if isinstance(total, bool) or int(total) != total or int(total) < 0:
        raise ValueError("population total must be a non-negative integer")
    total = int(total)
    if capacities is not None and total > sum(capacities):
        raise ValueError("population allocation exceeds capacity")
    allocations = [0] * len(weights)
    remaining = total
    available = set(range(len(weights)))
    while remaining and available:
        weight_total = sum(max(0, int(weights[index])) for index in available)
        if weight_total <= 0:
            order = sorted(available)
            for index in order:
                if not remaining:
                    break
                capacity = (remaining if capacities is None else
                            capacities[index] - allocations[index])
                add = min(remaining, max(0, capacity))
                allocations[index] += add
                remaining -= add
            break
        exact = {
            index: remaining * max(0, int(weights[index])) / weight_total
            for index in available}
        added = 0
        for index in sorted(available):
            capacity = (remaining if capacities is None else
                        capacities[index] - allocations[index])
            add = min(max(0, capacity), int(exact[index]))
            allocations[index] += add
            added += add
        remaining -= added
        if not remaining:
            break
        order = sorted(available, key=lambda index: (
            -(exact[index] - int(exact[index])), index))
        progressed = False
        for index in order:
            if not remaining:
                break
            capacity = (remaining if capacities is None else
                        capacities[index] - allocations[index])
            if capacity <= 0:
                available.discard(index)
                continue
            allocations[index] += 1
            remaining -= 1
            progressed = True
        if not progressed:
            break
        if capacities is not None:
            available = {
                index for index in available
                if allocations[index] < capacities[index]}
    if remaining or sum(allocations) != total:
        raise RuntimeError("population allocation failed")
    return allocations


def initial_settlement_network(home_economy: dict, turn: int = 1, *,
                               total_population: int | None = None) -> dict:
    """異なる生活基盤を持つ3集落を生成する。入力dictは変更しない。

    ``total_population`` を指定した場合は、既定3集落の人口比・再生産人口比を
    保ちながら整数配賦する。省略時は従来どおり合計250人であり、既定経路の
    数値・RNG・集落構成を一切変えない。
    """
    if total_population is None:
        populations = [
            int(row["population"]) for row in INITIAL_SETTLEMENT_SPECS]
        reproductive_populations = [
            int(row["reproductive_population"])
            for row in INITIAL_SETTLEMENT_SPECS]
    else:
        if (isinstance(total_population, bool)
                or int(total_population) != total_population
                or int(total_population) < 1):
            raise ValueError("total_population must be a positive integer")
        total_population = int(total_population)
        population_weights = [
            int(row["population"]) for row in INITIAL_SETTLEMENT_SPECS]
        populations = _proportional_counts(
            total_population, population_weights)
        reproductive_total = round(
            total_population * INITIAL_REPRODUCTIVE_POPULATION
            / INITIAL_TOTAL_POPULATION)
        reproductive_populations = _proportional_counts(
            reproductive_total,
            [int(row["reproductive_population"])
             for row in INITIAL_SETTLEMENT_SPECS],
            populations)
    settlements = {}
    for spec, population, reproductive_population in zip(
            INITIAL_SETTLEMENT_SPECS, populations,
            reproductive_populations):
        row = initial_settlement(
            spec["id"], spec["name"], population,
            reproductive_population)
        # 既定250人世界の生活基盤規模3.0を人口比で場所へ配る。初期人口を
        # 拡大した世界は同じ1人当たり設備量で開始し、120人集落と52人集落が
        # 同じ設備量を持つ旧近似を解消する。需要は年齢コホートから独立計算。
        provisioning_scale = round(
            REFERENCE_PROVISIONING_SCALE * population
            / INITIAL_TOTAL_POPULATION, 12)
        demand_scales = demand_scales_by_good(row)
        row.update({
            "founded_turn": int(turn),
            "migration_carry": 0.0,
            "immigrants_total": 0,
            "emigrants_total": 0,
            "local_economy": _local_economy(
                home_economy, spec["economy_factors"], spec["trust_offset"],
                provisioning_scale, demand_scales),
        })
        settlements[row["id"]] = row
    return settlements


def upgrade_single_settlement(settlements: dict, home_economy: dict) -> dict:
    """旧checkpointの単一集落へローカル状態だけを補う。

    保存済み世界へ住民を突然追加しないため、新規の隣接集落は作らない。
    新しく開始する世界だけが ``initial_settlement_network`` の3集落を持つ。
    """
    upgraded = copy.deepcopy(settlements)
    for settlement_id, row in list(upgraded.items()):
        row = upgrade_settlement_demography(row)
        upgraded[settlement_id] = row
        row.setdefault("founded_turn", 1)
        row.setdefault("migration_carry", 0.0)
        row.setdefault("immigrants_total", 0)
        row.setdefault("emigrants_total", 0)
        if "local_economy" not in row:
            row["local_economy"] = _local_economy(
                home_economy, (1, 1, 1, 1, 1),
                demand_scales=demand_scales_by_good(row))
        economy = row["local_economy"]
        economy.setdefault("community_trust", float(home_economy.get(
            "community_trust", LOCAL_CREDIT_TRUST_INITIAL)))
        economy.setdefault("local_credit_stage", int(home_economy.get(
            "local_credit_stage", LOCAL_CREDIT_STAGE_HEALTHY)))
        economy.setdefault("trade_sent_total", 0.0)
        economy.setdefault("trade_received_total", 0.0)
        economy.setdefault("trade_events_total", 0)
        economy["goods_accounting_version"] = GOODS_ACCOUNTING_VERSION
        economy["provisioning_scale"] = normalize_provisioning_scale(
            economy.get("provisioning_scale"))
        economy["demand_scales_by_good"] = demand_scales_by_good(row)
        economy["production_practice_by_good"] = (
            normalize_production_practice(
                economy.get("production_practice_by_good")))
    return upgraded


def settlement_pressure(settlement: dict) -> float:
    """移住圧力を0〜1へ正規化する。平均化せず最も悪い要因を採る。"""
    if int(settlement.get("population", 0)) <= 0:
        return 1.0
    economy = settlement.get("local_economy", {})
    shortage = max(0.0, min(100.0, float(
        economy.get("worst_shortfall", 0.0)))) / 100.0
    health_stress = max(0.0, min(1.0, (
        COMMUNITY_HEALTH_INITIAL - float(
            economy.get("community_health", COMMUNITY_HEALTH_INITIAL)))
        / COMMUNITY_HEALTH_INITIAL))
    stage = int(settlement.get("stage", 0))
    stage_pressure = {0: 0.0, 1: 0.25, 2: 0.6, 3: 0.9, 4: 1.0}.get(stage, 1.0)
    credit_stage = int(economy.get(
        "local_credit_stage", LOCAL_CREDIT_STAGE_HEALTHY))
    credit_pressure = {0: 0.0, 1: 0.15, 2: 0.35, 3: 0.55}.get(
        credit_stage, 0.55)
    return round(max(
        shortage, health_stress, stage_pressure, credit_pressure), 6)


def _stage_after_population_change(row: dict) -> int:
    return population_stage_next(
        int(row.get("stage", 0)), int(row["population"]),
        int(row["reproductive_population"]),
        float(row.get("last_expected_net_change", 0.0)))


def plan_migration(settlements: dict, turn: int) -> dict:
    """1か月分の集落間移住を計画し、更新済みcopyとイベントを返す。"""
    after = {
        settlement_id: upgrade_settlement_demography(copy.deepcopy(row))
        for settlement_id, row in settlements.items()}
    pressures = {sid: settlement_pressure(row) for sid, row in after.items()}
    destinations = [
        sid for sid, row in after.items()
        if int(row.get("population", 0)) > 0
        and int(row.get("stage", 0)) < POPULATION_STAGE_ABANDONED
    ]
    events = []
    if len(destinations) < 1:
        return {"settlements": after, "events": events, "migrants": 0}

    source_ids = sorted(
        (sid for sid, row in after.items()
         if int(row.get("population", 0)) > 1),
        key=lambda sid: (-pressures[sid], sid))
    total = 0
    for source_id in source_ids:
        target_options = [sid for sid in destinations if sid != source_id]
        if not target_options:
            continue
        target_id = min(target_options, key=lambda sid: (pressures[sid], sid))
        gap = pressures[source_id] - pressures[target_id]
        source = after[source_id]
        if gap < MIGRATION_MIN_PRESSURE_GAP:
            source["migration_carry"] = 0.0
            continue
        expected = (float(source.get("migration_carry", 0.0))
                    + source["population"] * MIGRATION_RATE * gap)
        migrants = min(
            MIGRATION_MAX_PER_TURN, max(0, source["population"] - 1),
            int(expected))
        source["migration_carry"] = round(expected - int(expected), 12)
        if migrants <= 0:
            continue
        target = after[target_id]
        reproductive = min(
            migrants,
            round(migrants * source["reproductive_population"]
                  / max(1, source["population"])))
        cohort_before = dict(source["age_cohorts"])
        cohort_values = _proportional_counts(
            migrants,
            [cohort_before[key] for key in AGE_COHORT_KEYS],
            [cohort_before[key] for key in AGE_COHORT_KEYS])
        cohort_migrants = dict(zip(AGE_COHORT_KEYS, cohort_values))
        # reproductive_populationは生産年齢人口の部分集合として扱う。
        # 小人数移住の丸めでproductive側が不足した場合は、非生産年齢側から
        # 同人数を振り替え、人口合計を変えずに包含関係を守る。
        productive_minimum = min(
            reproductive, migrants, cohort_before[AGE_COHORT_PRODUCTIVE])
        missing_productive = max(
            0, productive_minimum
            - cohort_migrants[AGE_COHORT_PRODUCTIVE])
        for donor in (AGE_COHORT_ELDERLY, AGE_COHORT_CHILDREN):
            moved = min(missing_productive, cohort_migrants[donor])
            cohort_migrants[donor] -= moved
            cohort_migrants[AGE_COHORT_PRODUCTIVE] += moved
            missing_productive -= moved
        if missing_productive:
            raise RuntimeError("productive migrant allocation failed")

        source_carry = source["age_transition_carry"]
        target_carry = target["age_transition_carry"]
        carry_transfers = {}
        for carry_key, cohort_key in (
                ("children_to_productive", AGE_COHORT_CHILDREN),
                ("productive_to_elderly", AGE_COHORT_PRODUCTIVE)):
            denominator = cohort_before[cohort_key]
            transfer = (float(source_carry[carry_key])
                        * cohort_migrants[cohort_key]
                        / denominator if denominator else 0.0)
            transfer = round(transfer, 12)
            source_carry[carry_key] = round(
                float(source_carry[carry_key]) - transfer, 12)
            target_carry[carry_key] = round(
                float(target_carry[carry_key]) + transfer, 12)
            carry_transfers[carry_key] = transfer
        for key in AGE_COHORT_KEYS:
            source["age_cohorts"][key] -= cohort_migrants[key]
            target["age_cohorts"][key] += cohort_migrants[key]
        source["population"] -= migrants
        source["reproductive_population"] = max(
            0, source["reproductive_population"] - reproductive)
        source["emigrants_total"] = int(source.get("emigrants_total", 0)) + migrants
        target["population"] += migrants
        target["reproductive_population"] = min(
            target["population"], target["reproductive_population"] + reproductive)
        source["productive_population"] = source["age_cohorts"][
            AGE_COHORT_PRODUCTIVE]
        target["productive_population"] = target["age_cohorts"][
            AGE_COHORT_PRODUCTIVE]
        target["immigrants_total"] = int(target.get("immigrants_total", 0)) + migrants
        source["stage"] = _stage_after_population_change(source)
        target["stage"] = _stage_after_population_change(target)
        event = {
            "turn": int(turn), "kind": "population_migrated",
            "from_settlement": source_id, "to_settlement": target_id,
            "migrants": migrants, "reproductive_migrants": reproductive,
            "productive_migrants": cohort_migrants[AGE_COHORT_PRODUCTIVE],
            "age_cohort_migrants": cohort_migrants,
            "age_transition_carry_migrants": carry_transfers,
            "source_pressure": pressures[source_id],
            "destination_pressure": pressures[target_id],
            "source_population": source["population"],
            "destination_population": target["population"],
        }
        events.append(event)
        total += migrants
    return {"settlements": after, "events": events, "migrants": total}


def select_focus_settlement(settlements: dict) -> str | None:
    """焦点集落が消滅したときの移動先。人口最大、同数ならID順。"""
    candidates = [
        (sid, row) for sid, row in settlements.items()
        if int(row.get("population", 0)) > 0
        and int(row.get("stage", 0)) != POPULATION_STAGE_EXTINCT
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda item: (-item[1]["population"], item[0]))[0]
