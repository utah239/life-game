# -*- coding: utf-8 -*-
"""人口・年齢構成を、必需財需要へ保存投影する純粋台帳。

財の正本は各活動共同体の ``local_economy`` にある在庫であり、この層は財を
生産も消費もしない。人口の正本である settlement の整数 ``age_cohorts`` から
財別需要規模を計算し、観察可能な名前付き世帯と匿名cohortへ同じ総量を配賦する。

重要な境界は次の通り。

* 需要は人に属する。出生・死亡で増減し、年齢区分と一緒に移住すれば世界合計は
  変わらない。
* 在庫・生産設備・在庫上限は場所に属する。ここでは変更しない。
* 名前付き住民は最大4096人の観察標本であり、匿名人口を加えた需要総量だけが
  数値計算の正本である。観察粒度を変えても需要は変わらない。

年齢係数はv1の明示的な仮値である。基準世界(子ども54・生産年齢154・高齢42、
合計250人、生活基盤3単位)の財別需要規模が正確に3.0になるよう財ごとに正規化
する。この正規化により既定世界の総需要を不連続に増減させず、年齢構成の違い
だけを相対需要として表現できる。
"""
from __future__ import annotations

from institutions import barter
from institutions.population import (
    AGE_COHORT_CHILDREN,
    AGE_COHORT_ELDERLY,
    AGE_COHORT_KEYS,
    AGE_COHORT_PRODUCTIVE,
    age_cohort_for_age,
    upgrade_settlement_demography,
)
from institutions.residents import (
    anonymous_population_count,
    resident_age_years,
)


HOUSEHOLD_NEEDS_VERSION = 1
NEED_GOODS = ("food", "medicine", "shelter", "tools")

# initial_settlement_networkの3集落を合計した実際の整数年齢構成。
REFERENCE_AGE_COHORTS = {
    AGE_COHORT_CHILDREN: 54,
    AGE_COHORT_PRODUCTIVE: 154,
    AGE_COHORT_ELDERLY: 42,
}
REFERENCE_PROVISIONING_SCALE = 3.0

# 1人当たり相対需要。絶対量は下の正規化で既定世界へ合わせる。
AGE_DEMAND_WEIGHTS_BY_GOOD = {
    "food": {
        AGE_COHORT_CHILDREN: 0.80,
        AGE_COHORT_PRODUCTIVE: 1.00,
        AGE_COHORT_ELDERLY: 0.85,
    },
    "medicine": {
        AGE_COHORT_CHILDREN: 0.80,
        AGE_COHORT_PRODUCTIVE: 0.70,
        AGE_COHORT_ELDERLY: 1.60,
    },
    "shelter": {
        AGE_COHORT_CHILDREN: 1.00,
        AGE_COHORT_PRODUCTIVE: 1.00,
        AGE_COHORT_ELDERLY: 1.00,
    },
    "tools": {
        AGE_COHORT_CHILDREN: 0.10,
        AGE_COHORT_PRODUCTIVE: 1.00,
        AGE_COHORT_ELDERLY: 0.35,
    },
}


def _reference_weight_per_scale(good: str) -> float:
    if good not in NEED_GOODS:
        raise KeyError(good)
    weighted = sum(
        REFERENCE_AGE_COHORTS[cohort]
        * AGE_DEMAND_WEIGHTS_BY_GOOD[good][cohort]
        for cohort in AGE_COHORT_KEYS)
    return weighted / REFERENCE_PROVISIONING_SCALE


def _demand_scales_from_cohorts(cohorts: dict) -> dict[str, float]:
    scales = {}
    for good in NEED_GOODS:
        weighted = sum(
            int(cohorts[cohort])
            * AGE_DEMAND_WEIGHTS_BY_GOOD[good][cohort]
            for cohort in AGE_COHORT_KEYS)
        scales[good] = round(
            weighted / _reference_weight_per_scale(good), 12)
    return scales


def demand_scales_by_good(settlement: dict) -> dict[str, float]:
    """集落の整数年齢コホートから、財別の基準共同体換算需要を返す。"""
    row = upgrade_settlement_demography(settlement)
    return _demand_scales_from_cohorts(row["age_cohorts"])


def demand_quantities_by_good(demand_scales: dict) -> dict[str, float]:
    """需要規模を、barter会計と同じ財別物量へ写像する。"""
    return {
        good: round(
            barter.goods_reference(
                good, demand_scale=max(0.0, float(demand_scales[good]))),
            12)
        for good in NEED_GOODS}


def _living_named_by_household(registry: dict | None,
                               settlement_id: str,
                               turn: int) -> dict[str, dict]:
    if not isinstance(registry, dict):
        return {}
    grouped: dict[str, dict] = {}
    households = registry.get("households", {})
    for resident in registry.get("residents", {}).values():
        if (not resident.get("alive", True)
                or str(resident.get("settlement_id")) != settlement_id):
            continue
        household_id = str(resident.get("household_id"))
        household = households.get(household_id, {})
        row = grouped.setdefault(household_id, {
            "household_id": household_id,
            "household_name": household.get("name", household_id),
            "population": 0,
            "weighted_population_by_good": {
                good: 0.0 for good in NEED_GOODS},
        })
        row["population"] += 1
        cohort = age_cohort_for_age(resident_age_years(resident, turn))
        for good in NEED_GOODS:
            row["weighted_population_by_good"][good] += (
                AGE_DEMAND_WEIGHTS_BY_GOOD[good][cohort])
    return grouped


def _allocate_named_quantity(total: float, named_population: int,
                             population: int, households: dict,
                             good: str) -> tuple[dict[str, float], float]:
    """名前付き標本の人口比だけを世帯へ配り、残りを匿名需要にする。"""
    if population <= 0 or named_population <= 0 or not households:
        return {}, round(float(total), 12)
    named_total = round(
        float(total) * min(population, named_population) / population, 12)
    keys = sorted(households)
    weights = {
        key: max(0.0, float(
            households[key]["weighted_population_by_good"][good]))
        for key in keys}
    denominator = sum(weights.values())
    if denominator <= 0.0:
        weights = {
            key: float(households[key]["population"]) for key in keys}
        denominator = sum(weights.values())
    allocation = {}
    used = 0.0
    for key in keys[:-1]:
        value = round(named_total * weights[key] / denominator, 12)
        allocation[key] = value
        used = round(used + value, 12)
    allocation[keys[-1]] = round(named_total - used, 12)
    return allocation, round(float(total) - named_total, 12)


def build_household_needs_state(settlements: dict,
                                resident_registry: dict | None,
                                turn: int) -> dict:
    """世界の需要を、集落→名前付き世帯/匿名cohortへ保存配賦する。"""
    communities = {}
    world_cohorts = {cohort: 0 for cohort in AGE_COHORT_KEYS}
    for settlement_id, raw in sorted(settlements.items()):
        settlement_id = str(settlement_id)
        settlement = upgrade_settlement_demography(raw)
        population = max(0, int(settlement.get("population", 0)))
        scales = demand_scales_by_good(settlement)
        quantities = demand_quantities_by_good(scales)
        named_households = _living_named_by_household(
            resident_registry, settlement_id, int(turn))
        named_population = sum(
            row["population"] for row in named_households.values())
        if named_population > population:
            raise ValueError("named population exceeds settlement population")
        anonymous_population = (
            anonymous_population_count(resident_registry, settlement_id)
            if isinstance(resident_registry, dict) else
            population - named_population)
        if named_population + anonymous_population != population:
            raise ValueError(
                "resident registry does not match settlement population")
        household_demands = {
            key: {
                "household_id": key,
                "household_name": row["household_name"],
                "population": row["population"],
                "demand_quantity_by_good": {},
            }
            for key, row in named_households.items()}
        anonymous = {}
        for good in NEED_GOODS:
            allocations, anonymous_quantity = _allocate_named_quantity(
                quantities[good], named_population, population,
                named_households, good)
            for household_id, value in allocations.items():
                household_demands[household_id][
                    "demand_quantity_by_good"][good] = value
            anonymous[good] = anonymous_quantity
        for cohort in AGE_COHORT_KEYS:
            world_cohorts[cohort] += int(settlement["age_cohorts"][cohort])
        communities[settlement_id] = {
            "settlement_id": settlement_id,
            "population": population,
            "age_cohorts": dict(settlement["age_cohorts"]),
            "named_population": named_population,
            "anonymous_population": anonymous_population,
            "demand_scale_by_good": scales,
            "demand_quantity_by_good": quantities,
            "household_demands": household_demands,
            "anonymous_demand_quantity_by_good": anonymous,
        }
    # 世界値は丸め済みcommunity値の和ではなく、保存される整数cohortの合計から
    # 直接求める。移住でcohortが移っただけならbit単位で同じ値になる。
    world_scales = _demand_scales_from_cohorts(world_cohorts)
    world_quantities = demand_quantities_by_good(world_scales)
    state = {
        "version": HOUSEHOLD_NEEDS_VERSION,
        "updated_turn": int(turn),
        "communities": communities,
        "world_demand_scale_by_good": world_scales,
        "world_demand_quantity_by_good": world_quantities,
    }
    verify_household_needs_state(state, settlements)
    return state


def verify_household_needs_state(state: dict, settlements: dict) -> bool:
    """人口と需要配賦の保存則を検証し、drift時は例外を送出する。"""
    communities = state.get("communities", {})
    if set(communities) != {str(key) for key in settlements}:
        raise ValueError("household needs community ids do not match settlements")
    world = {good: 0.0 for good in NEED_GOODS}
    for settlement_id, row in communities.items():
        population = int(settlements[settlement_id].get("population", 0))
        if int(row.get("population", -1)) != population:
            raise ValueError("household needs population drift")
        if (int(row.get("named_population", 0))
                + int(row.get("anonymous_population", 0)) != population):
            raise ValueError("named and anonymous population drift")
        for good in NEED_GOODS:
            total = float(row["demand_quantity_by_good"][good])
            named = sum(float(household["demand_quantity_by_good"][good])
                        for household in row["household_demands"].values())
            anonymous = float(
                row["anonymous_demand_quantity_by_good"][good])
            if abs(total - named - anonymous) > 1e-8:
                raise ValueError("household demand allocation drift")
            world[good] = round(world[good] + total, 12)
    for good in NEED_GOODS:
        if abs(world[good] - float(
                state["world_demand_quantity_by_good"][good])) > 1e-8:
            raise ValueError("world household demand drift")
    return True
