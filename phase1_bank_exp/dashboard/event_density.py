#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""遠景水槽向けに人口・移住・生業イベントを匿名密度へ集約する。"""
from __future__ import annotations


EVENT_DENSITY_VERSION = 1
ACTIVITY_KEYS = ("food", "medicine", "shelter", "tools", "unknown")
_ACTIVITY_CODE = {name: index for index, name in enumerate(ACTIVITY_KEYS)}

# 人口がこの値を越えた観察projectionでは、一人ずつの出来事を初期HTMLへ
# 重複収録しない。人口会計と密度履歴は常に残る。
DEFAULT_DETAIL_POPULATION_LIMIT = 10_000
INDIVIDUAL_OBSERVER_KINDS = frozenset({
    "resident_born", "resident_died", "household_activity",
    "residents_migrated", "household_split", "household_closed",
})


def build_event_density_history(population_events: list | None,
                                resident_events: list | None) -> dict:
    settlement_ids = []
    settlement_index = {}

    def settlement(value) -> int:
        key = str(value or "unassigned")
        if key not in settlement_index:
            settlement_index[key] = len(settlement_ids)
            settlement_ids.append(key)
        return settlement_index[key]

    population_counts = {}
    migration_counts = {}
    for event in population_events or ():
        kind = event.get("kind")
        if kind == "population_changed":
            key = (int(event["turn"]),
                   settlement(event.get("settlement_id")))
            # 同じ月・同じ集落へ複数の会計eventが将来追加されても、流量は
            # 合算し、残高は最後に観測した値を採る。
            row = population_counts.setdefault(key, [0, 0, 0, 0])
            row[0] += int(event.get("births", 0))
            row[1] += int(event.get("deaths", 0))
            row[2] = int(event.get("population", 0))
            row[3] = int(event.get("reproductive_population", 0))
        elif kind == "population_migrated":
            key = (
                int(event["turn"]),
                settlement(event.get("from_settlement")),
                settlement(event.get("to_settlement")),
            )
            row = migration_counts.setdefault(key, [0, 0])
            row[0] += int(event.get("migrants", 0))
            row[1] += int(event.get("reproductive_migrants", 0))

    population = [
        [turn, settlement_id, births, deaths, total, reproductive]
        for (turn, settlement_id), (births, deaths, total, reproductive)
        in sorted(population_counts.items())]
    migrations = [
        [turn, source, target, migrants, reproductive]
        for (turn, source, target), (migrants, reproductive)
        in sorted(migration_counts.items())]

    activity_counts = {}
    for event in resident_events or ():
        kind = event.get("kind")
        if kind not in ("household_activity", "production_activity"):
            continue
        activity = event.get("activity") or event.get("livelihood") or "unknown"
        key = (
            int(event["turn"]), settlement(event.get("settlement_id")),
            _ACTIVITY_CODE.get(activity, _ACTIVITY_CODE["unknown"]),
        )
        row = activity_counts.setdefault(key, [0, 0])
        # 旧household eventは1件=1担い手。新しいproduction eventは
        # 共同体・財ごとの集約なので、event件数ではなくworker数を足す。
        row[0] += (max(0, int(event.get("worker_count", 0)))
                   if kind == "production_activity" else 1)
        if event.get("responding_to_shortage"):
            row[1] += 1
    activities = [
        [turn, settlement_id, activity, count, shortage]
        for (turn, settlement_id, activity), (count, shortage)
        in sorted(activity_counts.items())]

    return {
        "version": EVENT_DENSITY_VERSION,
        "settlement_ids": settlement_ids,
        "activity_keys": list(ACTIVITY_KEYS),
        "population": population,
        "migrations": migrations,
        "activities": activities,
    }


def observer_events_for_population(events: list, resident_total: int, *,
                                   limit: int = DEFAULT_DETAIL_POPULATION_LIMIT
                                   ) -> tuple[list, str]:
    """大人口時だけ個人eventを匿名密度へ委譲する。"""
    if int(resident_total) <= int(limit):
        return events, "full"
    return [
        event for event in events
        if event.get("kind") not in INDIVIDUAL_OBSERVER_KINDS
    ], "density"
