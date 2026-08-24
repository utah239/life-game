# -*- coding: utf-8 -*-
"""世帯の継続不足を、既存の集落間移住枠へ接続する純粋ルール。

人口移住の人数と行先は ``settlement_network.plan_migration`` が正本である。
この層はその人数を増減させず、前月末の世帯対応台帳から「誰が先に移るか」だけを
決める。移住先で同じ財の不足圧力が十分に低い場合だけ候補にするため、単なる
不足世帯の機械的追放にはしない。名前付き世帯だけを最大件数付きで扱い、匿名人口
一人ずつへの状態展開や乱数消費は行わない。
"""
from __future__ import annotations

import copy

from institutions.household_goods import HOUSEHOLD_GOODS, ROUND_DIGITS


PERSISTENT_SHORTAGE_MONTHS = 3
DESTINATION_RELIEF_MARGIN = 0.10
HOUSEHOLD_MIGRATION_COOLDOWN_MONTHS = 12
HOUSEHOLD_MIGRATION_PREFERENCE_LIMIT = 16


def _living_household_sizes(registry: dict, settlement_id: str) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for resident in registry.get("residents", {}).values():
        if (not resident.get("alive", True)
                or str(resident.get("settlement_id")) != str(settlement_id)):
            continue
        household_id = str(resident.get("household_id"))
        sizes[household_id] = sizes.get(household_id, 0) + 1
    return sizes


def plan_household_migration(
        migration_event: dict, household_agency_state: dict | None,
        registry: dict, turn: int) -> dict:
    """移住eventへ、長期不足に基づく世帯優先順を付加する。

    戻り値は入力eventのcopy。既存の人口・再生産人口・年齢cohort値には触れず、
    ``preferred_household_ids`` と判断根拠だけを追加する。移住先の共同体別不足
    圧力が観測できない場合は、安全な改善先だと推測せず候補を作らない。
    """
    event = copy.deepcopy(migration_event)
    source_id = str(event.get("from_settlement"))
    destination_id = str(event.get("to_settlement"))
    migrant_limit = max(0, int(event.get("migrants", 0)))
    agency = household_agency_state or {}
    destination = agency.get("communities", {}).get(destination_id)
    sizes = _living_household_sizes(registry, source_id)
    households = registry.get("households", {})
    candidates = []

    if destination is not None and migrant_limit > 0:
        destination_pressure = destination.get(
            "priority_pressure_by_good", {})
        for household_id, response in agency.get("households", {}).items():
            household_id = str(household_id)
            household = households.get(household_id, {})
            size = int(sizes.get(household_id, 0))
            priority_good = response.get("priority_good")
            shortage_months = int(response.get(
                "consecutive_shortage_months", 0))
            last_migration_turn = household.get("last_migration_turn")
            if (size <= 0 or size > migrant_limit
                    or not household.get("active", True)
                    or str(household.get("settlement_id")) != source_id
                    or str(response.get("settlement_id")) != source_id
                    or priority_good not in HOUSEHOLD_GOODS
                    or shortage_months < PERSISTENT_SHORTAGE_MONTHS
                    or (last_migration_turn is not None
                        and int(turn) - int(last_migration_turn)
                        < HOUSEHOLD_MIGRATION_COOLDOWN_MONTHS)):
                continue
            source_shortfall = max(0.0, min(1.0, float(
                response.get("shortfall_by_good", {}).get(
                    priority_good, 0.0))))
            target_pressure = max(0.0, min(1.0, float(
                destination_pressure.get(priority_good, 1.0))))
            expected_relief = source_shortfall - target_pressure
            if expected_relief + 1e-12 < DESTINATION_RELIEF_MARGIN:
                continue
            motive = {
                "household_id": household_id,
                "priority_good": priority_good,
                "consecutive_shortage_months": shortage_months,
                "source_shortfall": round(source_shortfall, ROUND_DIGITS),
                "destination_pressure": round(target_pressure, ROUND_DIGITS),
                "expected_relief": round(expected_relief, ROUND_DIGITS),
                "household_size": size,
            }
            candidates.append(motive)

    candidates.sort(key=lambda row: (
        -row["consecutive_shortage_months"],
        -row["expected_relief"],
        -row["source_shortfall"],
        row["household_size"], row["household_id"]))
    candidates = candidates[:HOUSEHOLD_MIGRATION_PREFERENCE_LIMIT]
    event["preferred_household_ids"] = [
        row["household_id"] for row in candidates]
    event["household_migration_motives"] = candidates
    return event
