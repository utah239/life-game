#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""個体詳細を持たない人口を、遠景水槽用の匿名粒子群へ投影する。

正本の人口会計と、HTMLへ送る名前付きparticle packetの差だけを保持する。
匿名人口一人ずつのID・座標は生成しない。ブラウザ側はこの人数を画面画素数
以下の決定論的なセルへ一括加算するため、人口が数百万人でもpayloadと描画用
メモリは人口に比例しない。
"""
from __future__ import annotations


PARTICLE_COHORT_VERSION = 2


def _population_map(turn_row: dict) -> dict[str, int]:
    for key in ("activity_community_populations", "settlement_populations"):
        source = turn_row.get(key)
        if isinstance(source, dict) and source:
            return {
                str(settlement_id): max(0, int(population))
                for settlement_id, population in source.items()
                if int(population) > 0
            }
    total = turn_row.get("total_population", turn_row.get("population"))
    if total is None:
        return {}
    settlement_id = str(turn_row.get("settlement_id") or "home")
    return {settlement_id: max(0, int(total))}


def _alive_at(resident: dict, turn: int) -> bool:
    born = resident.get("birth_turn")
    registered = resident.get("registered_turn")
    died = resident.get("died_turn")
    return ((born is None or int(born) <= turn)
            and (registered is None or int(registered) <= turn)
            and (died is None or int(died) > turn))


def _represented_population(residents: list, spatial_state: dict, turn: int,
                            packed: bool) -> dict[str, int]:
    counts: dict[str, int] = {}
    positions = spatial_state.get("residents", {}) if spatial_state else {}
    sites = spatial_state.get("sites", {}) if spatial_state else {}
    for resident in residents:
        if not _alive_at(resident, turn):
            continue
        settlement_id = resident.get("settlement_id")
        if packed:
            position = positions.get(resident.get("id"))
            site = sites.get(position.get("site_id")) if position else None
            if not isinstance(site, dict) or not site.get("active", True):
                # build_particle_packet()にも載らない住民なので、名前付き粒子と
                # しては数えない。人口会計との差として匿名側へ残る。
                continue
            settlement_id = site.get("account_id", settlement_id)
        key = str(settlement_id or "unassigned")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _migration_rows(resident_events: list | None) -> dict[str, list[tuple]]:
    rows: dict[str, list[tuple]] = {}
    for event in resident_events or ():
        if event.get("kind") != "residents_migrated":
            continue
        turn = int(event["turn"])
        source = str(event.get("from_settlement") or "unassigned")
        target = str(event.get("to_settlement") or "unassigned")
        for resident in event.get("residents", ()):
            resident_id = str(resident.get("resident_id"))
            rows.setdefault(resident_id, []).append((turn, source, target))
    for events in rows.values():
        # 同じ月に複数段階の移動が記録されても、trace内の順序を保つ。
        events.sort(key=lambda row: row[0])
    return rows


def _allocate_anonymous(populations: dict[str, int],
                        represented: dict[str, int],
                        anonymous_total: int) -> dict[str, int]:
    """既知個体を差し引ける範囲で差し引き、残りを整数配賦する。"""
    if anonymous_total <= 0:
        return {}
    capacities = {
        settlement_id: max(
            0, population - min(population, represented.get(settlement_id, 0)))
        for settlement_id, population in populations.items()
    }
    capacity_total = sum(capacities.values())
    if capacity_total < anonymous_total:
        raise ValueError("anonymous population exceeds settlement capacity")
    if capacity_total == anonymous_total:
        return {key: value for key, value in capacities.items() if value}

    allocated = {}
    fractions = []
    used = 0
    for order, (settlement_id, capacity) in enumerate(capacities.items()):
        ideal = capacity * anonymous_total / capacity_total
        count = min(capacity, int(ideal))
        allocated[settlement_id] = count
        used += count
        fractions.append((ideal - count, -order, settlement_id, capacity))
    remaining = anonymous_total - used
    for _, _, settlement_id, capacity in sorted(fractions, reverse=True):
        if remaining <= 0:
            break
        if allocated[settlement_id] < capacity:
            allocated[settlement_id] += 1
            remaining -= 1
    if remaining:
        # 浮動小数の同率処理に依存せず、容量が残る順に最後まで保存する。
        for settlement_id, capacity in capacities.items():
            take = min(remaining, capacity - allocated[settlement_id])
            allocated[settlement_id] += take
            remaining -= take
            if not remaining:
                break
    if remaining or sum(allocated.values()) != anonymous_total:
        raise ValueError("anonymous population allocation is not conserved")
    return {key: value for key, value in allocated.items() if value}


def _latest_cohorts(turns: list, residents: list, spatial_state: dict,
                    particle_frame: dict | None) -> dict:
    row = turns[-1]
    turn = int(row["t"] if "t" in row else row["turn"])
    populations = _population_map(row)
    represented = _represented_population(
        residents, spatial_state or {}, turn, particle_frame is not None)
    unknown_accounts = sorted(set(represented) - set(populations))
    if unknown_accounts:
        raise ValueError(
            "named particle account is absent from population accounting: "
            + ", ".join(unknown_accounts))
    named_count = sum(represented.values())
    if particle_frame is not None and named_count != int(
            particle_frame.get("count", 0)):
        raise ValueError(
            "named spatial population does not match particle packet count")
    total = sum(populations.values())
    if named_count > total:
        raise ValueError("named particle count exceeds population accounting")
    anonymous = _allocate_anonymous(
        populations, represented, total - named_count)
    return {
        "turn": turn, "populations": populations,
        "represented": represented, "anonymous": anonymous,
        "total_population": total, "named_particle_count": named_count,
        "anonymous_particle_count": total - named_count,
    }


def build_particle_cohorts(turns: list, residents: list, spatial_state: dict,
                           particle_frame: dict | None,
                           resident_events: list | None = None) -> dict:
    """全観察月の数値人口と名前付き粒子の差をrun-length履歴にする。

    個体を月ごとに走査しない。出生・死亡・移住scheduleを一度作り、月ごとには
    集落別countだけを更新する。匿名mapを含む状態が変化した月だけframeへ残す。
    """
    if not turns:
        return {
            "version": PARTICLE_COHORT_VERSION, "turn": None,
            "settlement_ids": [], "cohorts": [],
            "total_population": 0, "named_particle_count": 0,
            "anonymous_particle_count": 0,
            "source_turn_count": 0, "frames": [],
        }

    ordered_turns = [
        (int(row["t"] if "t" in row else row["turn"]), row)
        for row in turns]
    if any(right[0] <= left[0]
           for left, right in zip(ordered_turns, ordered_turns[1:])):
        raise ValueError("particle cohort turns must be strictly increasing")
    first_turn = ordered_turns[0][0]
    migrations = _migration_rows(resident_events)
    resident_by_id = {str(row.get("id")): row for row in residents}
    initial_location = {}
    for resident_id, resident in resident_by_id.items():
        future = [row for row in migrations.get(resident_id, ())
                  if row[0] >= first_turn]
        initial_location[resident_id] = (
            future[0][1] if future
            else str(resident.get("settlement_id") or "unassigned"))

    births: dict[int, list[str]] = {}
    deaths: dict[int, list[str]] = {}
    migration_schedule: dict[int, list[tuple]] = {}
    active = {}
    represented = {}

    def add(resident_id: str, settlement_id: str) -> None:
        active[resident_id] = settlement_id
        represented[settlement_id] = represented.get(settlement_id, 0) + 1

    def remove(resident_id: str) -> None:
        settlement_id = active.pop(resident_id, None)
        if settlement_id is None:
            return
        represented[settlement_id] -= 1
        if not represented[settlement_id]:
            del represented[settlement_id]

    for resident_id, resident in resident_by_id.items():
        born = resident.get("birth_turn")
        registered = resident.get("registered_turn")
        died = resident.get("died_turn")
        born_value = first_turn if born is None else int(born)
        entry_value = max(
            born_value,
            born_value if registered is None else int(registered))
        died_value = None if died is None else int(died)
        if entry_value <= first_turn and (
                died_value is None or died_value > first_turn):
            add(resident_id, initial_location[resident_id])
        elif entry_value > first_turn:
            births.setdefault(entry_value, []).append(resident_id)
        if died_value is not None and died_value > first_turn:
            deaths.setdefault(died_value, []).append(resident_id)
    for resident_id, rows in migrations.items():
        if resident_id not in resident_by_id:
            continue
        for turn, source, target in rows:
            if turn >= first_turn:
                migration_schedule.setdefault(turn, []).append(
                    (resident_id, source, target))

    settlement_ids = []
    settlement_index = {}

    def index(settlement_id: str) -> int:
        if settlement_id not in settlement_index:
            settlement_index[settlement_id] = len(settlement_ids)
            settlement_ids.append(settlement_id)
        return settlement_index[settlement_id]

    frames = []
    previous_signature = None
    scheduled_turns = sorted(set(births) | set(deaths)
                             | set(migration_schedule))
    schedule_cursor = 0
    for turn, row in ordered_turns:
        # traceの観察月が疎でも、前回観察月との間に起きた個体変化を一度ずつ
        # 適用する。simulation本体と同じく出生/死亡→移住の順を保つ。
        while (schedule_cursor < len(scheduled_turns)
               and scheduled_turns[schedule_cursor] <= turn):
            event_turn = scheduled_turns[schedule_cursor]
            if event_turn != first_turn:
                for resident_id in births.get(event_turn, ()):
                    add(resident_id, initial_location[resident_id])
                for resident_id in deaths.get(event_turn, ()):
                    remove(resident_id)
            for resident_id, _source, target in migration_schedule.get(
                    event_turn, ()):
                if resident_id not in active:
                    continue
                remove(resident_id)
                add(resident_id, target)
            schedule_cursor += 1

        populations = _population_map(row)
        total = sum(populations.values())
        named_count = len(active)
        if named_count > total:
            raise ValueError(
                f"named population exceeds population accounting at T{turn}: "
                f"{named_count} > {total}")
        anonymous = _allocate_anonymous(
            populations, represented, total - named_count)
        for settlement_id in populations:
            index(settlement_id)
        cohort_rows = [
            [index(settlement_id), count]
            for settlement_id, count in anonymous.items()]
        anonymous_count = sum(anonymous.values())
        signature = (
            total, named_count, anonymous_count,
            tuple((settlement_ids[row[0]], row[1]) for row in cohort_rows),
        )
        if signature != previous_signature:
            frames.append([
                turn, total, named_count, anonymous_count, cohort_rows])
            previous_signature = signature

    latest = _latest_cohorts(
        turns, residents, spatial_state, particle_frame)
    latest_rows = [
        [index(settlement_id), count]
        for settlement_id, count in latest["anonymous"].items()]
    latest_signature = (
        latest["total_population"], latest["named_particle_count"],
        latest["anonymous_particle_count"],
        tuple((settlement_ids[row[0]], row[1]) for row in latest_rows),
    )
    if latest_signature != previous_signature:
        if frames and frames[-1][0] == latest["turn"]:
            frames[-1] = [
                latest["turn"], latest["total_population"],
                latest["named_particle_count"],
                latest["anonymous_particle_count"], latest_rows]
        else:
            frames.append([
                latest["turn"], latest["total_population"],
                latest["named_particle_count"],
                latest["anonymous_particle_count"], latest_rows])

    return {
        "version": PARTICLE_COHORT_VERSION,
        "turn": latest["turn"],
        "settlement_ids": settlement_ids,
        "cohorts": latest_rows,
        "total_population": latest["total_population"],
        "named_particle_count": latest["named_particle_count"],
        "anonymous_particle_count": latest["anonymous_particle_count"],
        "source_turn_count": len(ordered_turns),
        "frames": frames,
    }
