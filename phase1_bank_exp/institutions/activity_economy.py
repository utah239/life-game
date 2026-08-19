# -*- coding: utf-8 -*-
"""背景生産を人口・世帯・活動場所へ保存配賦する活動経済台帳。

4財の増減は従来のbarter会計が正本であり、この層は財を追加しない。
呼び出し側が渡したgross背景生産量を、生産年齢人口、名前付き観察標本、
匿名cohort、永続活動場所へ決定論的に割り当てる。入力を変更せず乱数も使わない。
"""
from __future__ import annotations

import copy

from institutions import barter
from institutions.household_agency import HOUSEHOLD_SHORTAGE_LABOR_PRIORITY
from institutions.population import PRODUCTIVE_AGE_RANGE
from institutions.residents import (
    HOUSEHOLD_LIVELIHOODS,
    record_activity_count,
    resident_age_years,
    upgrade_resident_registry,
)


ACTIVITY_ECONOMY_VERSION = 2
LEGACY_ACTIVITY_ECONOMY_VERSION = 1
ACTIVITY_GOODS = tuple(HOUSEHOLD_LIVELIHOODS)
# 共同体人口のうち、食料・医療・住居保守・道具保守という必需財部門を
# production_capacityどおりに稼働させるために必要な生産年齢人口の比率。
# 残りの生産年齢人口は会社・交易・サービス・ケア等の別活動に使えるため、
# 全員を必需財生産者として数えない。今後の較正対象であることを明示する。
ESSENTIAL_PRODUCTION_WORKER_SHARE = 0.30
ESSENTIAL_LABOR_SHORTAGE_PRIORITY = 1.0


def _allocate_capped_integer(total: int, raw_weights: dict,
                             capacities: dict) -> dict[str, int]:
    """重み付き整数配賦を、各キーの必要人数を越えずに行う。"""
    caps = {
        str(key): max(0, int(value)) for key, value in capacities.items()}
    allocation = {key: 0 for key in sorted(caps)}
    remaining = max(0, min(int(total), sum(caps.values())))
    weights = {
        key: max(0.0, float(raw_weights.get(key, 0.0)))
        for key in allocation}
    while remaining:
        eligible = [
            key for key in allocation if allocation[key] < caps[key]]
        if not eligible:
            break
        denominator = sum(weights[key] for key in eligible)
        if denominator <= 0.0:
            denominator = float(sum(
                caps[key] - allocation[key] for key in eligible))
            current_weights = {
                key: float(caps[key] - allocation[key])
                for key in eligible}
        else:
            current_weights = {key: weights[key] for key in eligible}
        exact = {
            key: remaining * current_weights[key] / denominator
            for key in eligible}
        base = {
            key: min(caps[key] - allocation[key], int(exact[key]))
            for key in eligible}
        placed = sum(base.values())
        for key, value in base.items():
            allocation[key] += value
        remaining -= placed
        if remaining <= 0:
            break
        order = sorted(eligible, key=lambda key: (
            -(exact[key] - int(exact[key])), -current_weights[key], key))
        progressed = False
        for key in order:
            if remaining <= 0:
                break
            if allocation[key] >= caps[key]:
                continue
            allocation[key] += 1
            remaining -= 1
            progressed = True
        if not progressed:
            raise RuntimeError("capped activity worker allocation failed")
    if sum(allocation.values()) != max(
            0, min(int(total), sum(caps.values()))):
        raise RuntimeError("capped activity worker allocation drift")
    return allocation


def _required_worker_weights(
        demand_scales_by_good: dict | None = None) -> dict[str, float]:
    """現在の基礎消費・摩耗量を、財別の必要労働比として読む。"""
    base = {
        "food": max(0.0, float(barter.FOOD_UPKEEP_PER_TURN)),
        "medicine": max(0.0, float(barter.MEDICINE_UPKEEP_PER_TURN)),
        "shelter": max(0.0, float(barter.SHELTER_WEAR_PER_TURN)),
        "tools": max(0.0, float(barter.TOOLS_WEAR_PER_TURN)),
    }
    return {
        good: value * max(0.0, float(
            (demand_scales_by_good or {}).get(good, 1.0)))
        for good, value in base.items()}


def _goods_references(provisioning_scale: float = 1.0,
                      demand_scales_by_good: dict | None = None) -> dict[str, float]:
    return {
        good: max(0.0, barter.goods_reference(
            good, provisioning_scale,
            demand_scale=(demand_scales_by_good or {}).get(good)))
        for good in ACTIVITY_GOODS
    }


def background_production_labor_plan(
        population: int, productive_population: int,
        goods: dict | None = None,
        provisioning_scale: float = 1.0,
        demand_scales_by_good: dict | None = None,
        household_pressure_by_good: dict | None = None) -> dict:
    """必需財背景生産の財別必要人数・実働人数・充足率を返す。

    必要人数の総計は従来どおり共同体人口の明示比率から求める。その人数を
    現在の基礎消費・摩耗量で4財へ整数配賦し、人手不足時だけ現在在庫の不足度を
    優先重みへ加える。したがって労働者総数を増やさず、どの財へ人手を振ったかを
    月次状態として残せる。``goods=None``は全財の不足度0として扱う。
    """
    population = max(0, int(population))
    productive_population = max(
        0, min(population, int(productive_population)))
    required_workers = (max(
        1, round(population * ESSENTIAL_PRODUCTION_WORKER_SHARE))
        if population else 0)
    active_workers = min(productive_population, required_workers)
    required_by_good = _allocate_capped_integer(
        required_workers, _required_worker_weights(demand_scales_by_good), {
            good: required_workers for good in ACTIVITY_GOODS})
    goods = goods or {}
    scale = barter.normalize_provisioning_scale(provisioning_scale)
    references = _goods_references(scale, demand_scales_by_good)
    urgency = {}
    for good in ACTIVITY_GOODS:
        reference = references[good]
        current = max(0.0, float(goods.get(good, reference)))
        shortfall = (max(0.0, min(1.0, (reference - current) / reference))
                     if reference > 0.0 else 0.0)
        household_pressure = max(0.0, min(1.0, float(
            (household_pressure_by_good or {}).get(good, 0.0))))
        urgency[good] = required_by_good[good] * (
            1.0 + shortfall * ESSENTIAL_LABOR_SHORTAGE_PRIORITY
            + household_pressure * HOUSEHOLD_SHORTAGE_LABOR_PRIORITY)
    active_by_good = _allocate_capped_integer(
        active_workers, urgency, required_by_good)
    factors_by_good = {
        good: (round(active_by_good[good] / required_by_good[good], 6)
               if required_by_good[good] else 0.0)
        for good in ACTIVITY_GOODS}
    labor_factor = (round(active_workers / required_workers, 6)
                    if required_workers else 0.0)
    return {
        "provisioning_scale": scale,
        "demand_scales_by_good": {
            good: max(0.0, float(
                (demand_scales_by_good or {}).get(good, scale)))
            for good in ACTIVITY_GOODS},
        "productive_population": productive_population,
        "required_worker_count": required_workers,
        "active_worker_count": active_workers,
        "required_worker_count_by_good": required_by_good,
        "active_worker_count_by_good": active_by_good,
        "labor_factor_by_good": factors_by_good,
        "unassigned_productive_population": max(
            0, productive_population - active_workers),
        "labor_factor": labor_factor,
    }


def background_production_labor_factor(
        population: int, productive_population: int) -> float:
    """集落の背景生産を担える生産年齢人口の充足率を返す。

    財在庫は加算可能な物量であり、別のprovisioning_scaleがその共同体の
    基準必要量を表す。ここでは物量の大小を労働力と取り違えず、必要な
    必需財労働力の充足率を使う。必要人数を満たせば1.0、下回れば必要人数に
    対する比率、人口または生産年齢人口が0なら0.0。
    これにより無人設備だけが食料等を生産し続けることも、子ども・高齢者を
    暗黙の労働力に数えることもない。
    """
    return background_production_labor_plan(
        population, productive_population)["labor_factor"]


def initial_activity_economy_state() -> dict:
    return {
        "version": ACTIVITY_ECONOMY_VERSION,
        "updated_turn": 0,
        "communities": {},
        "cumulative_output_by_good": {
            good: 0.0 for good in ACTIVITY_GOODS},
    }


def _weights(raw: dict) -> dict:
    return {
        str(key): max(0.0, float(value))
        for key, value in raw.items()
        if float(value) > 0.0}


def _allocate_integer(total: int, raw_weights: dict) -> dict[str, int]:
    """最大剰余法で整数合計を保存する。"""
    total = max(0, int(total))
    weights = _weights(raw_weights)
    if not weights:
        return {}
    denominator = sum(weights.values())
    exact = {
        key: total * value / denominator
        for key, value in weights.items()}
    allocation = {key: int(value) for key, value in exact.items()}
    remaining = total - sum(allocation.values())
    for key in sorted(weights, key=lambda item: (
            -(exact[item] - allocation[item]), item))[:remaining]:
        allocation[key] += 1
    if sum(allocation.values()) != total:
        raise RuntimeError("activity worker allocation drift")
    return allocation


def _allocate_float(total: float, raw_weights: dict,
                    digits: int = 6) -> dict[str, float]:
    """最後の配賦先へ丸め差を吸収し、gross出力合計を保存する。"""
    weights = _weights(raw_weights)
    keys = sorted(weights)
    if not keys:
        return {}
    denominator = sum(weights.values())
    allocation = {}
    used = 0.0
    for key in keys[:-1]:
        value = round(float(total) * weights[key] / denominator, digits)
        allocation[key] = value
        used = round(used + value, digits)
    allocation[keys[-1]] = round(float(total) - used, digits)
    return allocation


def _active_sites(spatial_state: dict) -> tuple[dict, dict]:
    by_community = {}
    by_household = {}
    for site in sorted(spatial_state.get("sites", {}).values(),
                       key=lambda row: str(row.get("id"))):
        if not site.get("active", True):
            continue
        community_id = str(site.get("account_id"))
        by_community.setdefault(community_id, []).append(site)
        by_household[str(site.get("household_id"))] = site
    return by_community, by_household


def _productive_residents_by_community(registry: dict,
                                       turn: int) -> dict[str, list[dict]]:
    """名前付き住民を1回だけ走査し、生産年齢の候補索引を作る。"""
    low, high = PRODUCTIVE_AGE_RANGE
    grouped: dict[str, list[dict]] = {}
    for resident in registry.get("residents", {}).values():
        if (not resident.get("alive", True)
                or not low <= resident_age_years(resident, turn) <= high):
            continue
        grouped.setdefault(
            str(resident.get("settlement_id")), []).append(resident)
    for residents in grouped.values():
        residents.sort(key=lambda resident: resident["id"])
    return grouped


def _pick_named_workers(registry: dict, candidates_by_community: dict,
                        community_id: str, good: str,
                        count: int, used: set[str],
                        household_priorities: dict | None = None) -> list[dict]:
    households = registry.get("households", {})
    candidates = [
        resident for resident in candidates_by_community.get(
            community_id, ())
        if resident["id"] not in used]
    candidates.sort(key=lambda resident: (
        0 if (household_priorities or {}).get(
            str(resident.get("household_id"))) == good else
        1 if str(resident.get("household_id")) not in (
            household_priorities or {}) else 2,
        households.get(resident.get("household_id"), {}).get(
            "livelihood") != good,
        int(resident.get("activity_count", 0)),
        int(households.get(resident.get("household_id"), {}).get(
            "activity_count", 0)),
        resident["id"],
    ))
    selected = candidates[:max(0, int(count))]
    used.update(row["id"] for row in selected)
    return selected


def _record_named_activity(registry: dict, residents: list[dict],
                           good: str, turn: int) -> list[str]:
    household_ids = []
    grouped = {}
    for resident in residents:
        record_activity_count(resident, good)
        resident["last_activity_turn"] = int(turn)
        resident["last_activity"] = good
        grouped.setdefault(resident["household_id"], []).append(resident)
    for household_id, members in grouped.items():
        household = registry.get("households", {}).get(household_id)
        if household is None:
            continue
        record_activity_count(household, good)
        household["last_activity_turn"] = int(turn)
        household["last_activity"] = good
        household["last_actor_id"] = members[0]["id"]
        household_ids.append(household_id)
    return sorted(household_ids)


def _site_allocations(sites: list[dict], site_by_household: dict,
                      named_workers: list[dict], anonymous_workers: int,
                      good: str, gross_output: float) -> list[dict]:
    # 労働者0のときに既存siteへ出力0の空活動を配ると、水槽上では誰も
    # 働いていない場所を当月の活動場所として見せてしまう。legacy入力等で
    # grossだけが残る場合も、担い手を捏造せず呼び出し側の
    # unlocated_gross_outputへ全量を残す。
    if not named_workers and max(0, int(anonymous_workers)) == 0:
        return []
    specialist = [
        site for site in sites if site.get("livelihood") == good]
    candidates = specialist or sites
    named_by_site = {}
    for resident in named_workers:
        site = site_by_household.get(str(resident.get("household_id")))
        if site is not None:
            named_by_site[site["id"]] = named_by_site.get(site["id"], 0) + 1
    anonymous_by_site = _allocate_integer(
        anonymous_workers, {
            site["id"]: max(1, int(site.get("population_weight", 0)))
            for site in candidates}) if candidates else {}
    site_ids = sorted(set(named_by_site) | set(anonymous_by_site))
    if not site_ids and sites:
        site_ids = [sites[0]["id"]]
    site_lookup = {site["id"]: site for site in sites}
    workers = {
        site_id: named_by_site.get(site_id, 0)
        + anonymous_by_site.get(site_id, 0)
        for site_id in site_ids}
    output_weights = workers if sum(workers.values()) else {
        site_id: max(1, int(site_lookup[site_id].get(
            "population_weight", 0))) for site_id in site_ids}
    output_by_site = _allocate_float(gross_output, output_weights)
    rows = []
    for site_id in site_ids:
        named_count = named_by_site.get(site_id, 0)
        anonymous_count = anonymous_by_site.get(site_id, 0)
        rows.append({
            "site_id": site_id,
            "gross_output": output_by_site.get(site_id, 0.0),
            "worker_count": named_count + anonymous_count,
            "named_worker_count": named_count,
            "anonymous_worker_count": anonymous_count,
        })
    if rows and sum(row["worker_count"] for row in rows) != (
            len(named_workers) + max(0, int(anonymous_workers))):
        raise RuntimeError("activity site worker allocation drift")
    if rows and round(sum(row["gross_output"] for row in rows), 6) != round(
            float(gross_output), 6):
        raise RuntimeError("activity output allocation drift")
    return rows


def plan_activity_economy(state: dict | None, registry: dict,
                          spatial_state: dict, settlements: dict,
                          gross_production_by_community: dict,
                          turn: int,
                          labor_plans_by_community: dict | None = None,
                          household_priorities: dict | None = None) -> dict:
    """1か月のgross背景生産を活動主体へ保存配賦する。

    財在庫やsettlementは変更しない。戻り値のregistryだけが名前付き住民と
    世帯の当月activity episodeを更新する。
    """
    before = state or initial_activity_economy_state()
    if int(before.get("version", 0)) not in (
            LEGACY_ACTIVITY_ECONOMY_VERSION, ACTIVITY_ECONOMY_VERSION):
        raise ValueError("unsupported activity economy state version")
    after_registry = upgrade_resident_registry(registry)
    sites_by_community, site_by_household = _active_sites(spatial_state)
    candidates_by_community = _productive_residents_by_community(
        after_registry, turn)
    cumulative = copy.deepcopy(before.get(
        "cumulative_output_by_good", {}))
    for good in ACTIVITY_GOODS:
        cumulative.setdefault(good, 0.0)
    communities = {}
    events = []
    used_named_workers: set[str] = set()

    for community_id in sorted(set(str(key) for key in settlements)
                               | set(str(key) for key in
                                     gross_production_by_community)):
        settlement = settlements.get(community_id, {})
        productive_population = max(
            0, int(settlement.get("productive_population", 0)))
        labor_plan = copy.deepcopy((labor_plans_by_community or {}).get(
            community_id))
        if labor_plan is None:
            economy = settlement.get("local_economy", {})
            labor_plan = background_production_labor_plan(
                settlement.get("population", 0), productive_population,
                goods={good: economy.get(good) for good in ACTIVITY_GOODS},
                provisioning_scale=economy.get(
                    "provisioning_scale", 1.0))
        if int(labor_plan.get(
                "productive_population", productive_population)) != (
                productive_population):
            raise ValueError("activity labor plan productive population drift")
        if (int(labor_plan.get("active_worker_count", 0))
                + int(labor_plan.get(
                    "unassigned_productive_population", 0))
                != productive_population):
            raise ValueError("activity labor plan population drift")
        required_by_good = {
            good: max(0, int(labor_plan.get(
                "required_worker_count_by_good", {}).get(good, 0)))
            for good in ACTIVITY_GOODS}
        active_by_good = {
            good: max(0, int(labor_plan.get(
                "active_worker_count_by_good", {}).get(good, 0)))
            for good in ACTIVITY_GOODS}
        if not any(required_by_good) and int(
                labor_plan.get("required_worker_count", 0)):
            required_by_good = _allocate_integer(
                int(labor_plan["required_worker_count"]),
                _required_worker_weights())
        if not any(active_by_good) and int(
                labor_plan.get("active_worker_count", 0)):
            active_by_good = _allocate_capped_integer(
                int(labor_plan["active_worker_count"]),
                required_by_good, required_by_good)
        if sum(required_by_good.values()) != int(
                labor_plan.get("required_worker_count", 0)):
            raise ValueError("activity required worker plan drift")
        if sum(active_by_good.values()) != int(
                labor_plan.get("active_worker_count", 0)):
            raise ValueError("activity active worker plan drift")
        if any(active_by_good[good] > required_by_good[good]
               for good in ACTIVITY_GOODS):
            raise ValueError("activity workers exceed per-good requirement")
        gross = {
            good: max(0.0, float(
                gross_production_by_community.get(
                    community_id, {}).get(good, 0.0)))
            for good in ACTIVITY_GOODS}
        worker_counts = active_by_good
        activities = {}
        sites = sites_by_community.get(community_id, [])
        if productive_population > 0 and not sites:
            raise RuntimeError(
                "productive activity community has no active site")
        for good in ACTIVITY_GOODS:
            gross_output = round(gross[good], 6)
            worker_count = int(worker_counts.get(good, 0))
            named_workers = _pick_named_workers(
                after_registry, candidates_by_community,
                community_id, good,
                worker_count, used_named_workers,
                household_priorities)
            household_ids = _record_named_activity(
                after_registry, named_workers, good, turn)
            responding_household_ids = sorted(
                household_id for household_id in household_ids
                if (household_priorities or {}).get(household_id) == good)
            anonymous_workers = max(0, worker_count - len(named_workers))
            site_allocations = _site_allocations(
                sites, site_by_household, named_workers,
                anonymous_workers, good, gross_output)
            allocated_output = round(sum(
                row["gross_output"] for row in site_allocations), 6)
            unlocated_output = round(gross_output - allocated_output, 6)
            activities[good] = {
                "gross_output": gross_output,
                "worker_count": worker_count,
                "named_worker_ids": [row["id"] for row in named_workers],
                "named_household_ids": household_ids,
                "responding_household_ids": responding_household_ids,
                "anonymous_worker_count": anonymous_workers,
                "site_allocations": site_allocations,
                # compact済みの無人共同体など、既知siteすら残らない場合だけ
                # 位置を偽造せず未定位として保存する。
                "unlocated_gross_output": unlocated_output,
            }
            cumulative[good] = round(
                float(cumulative.get(good, 0.0)) + gross_output, 6)
            if gross_output > 0.0:
                event = {
                    "turn": int(turn), "kind": "production_activity",
                    "settlement_id": community_id, "activity": good,
                    "gross_output": gross_output,
                    "worker_count": worker_count,
                    "named_worker_count": len(named_workers),
                    "anonymous_worker_count": anonymous_workers,
                    "responding_household_count": len(
                        responding_household_ids),
                    "site_count": len(site_allocations),
                }
                if named_workers:
                    actor = named_workers[0]
                    household = after_registry.get(
                        "households", {}).get(actor["household_id"], {})
                    event.update({
                        "resident_id": actor["id"],
                        "resident_name": actor.get("name"),
                        "resident_age": resident_age_years(actor, turn),
                        "household_id": actor["household_id"],
                        "household_name": household.get("name"),
                    })
                events.append(event)
        communities[community_id] = {
            "community_id": community_id,
            "provisioning_scale": float(labor_plan.get(
                "provisioning_scale", 1.0)),
            "productive_population": productive_population,
            "required_worker_count": labor_plan["required_worker_count"],
            "active_worker_count": labor_plan["active_worker_count"],
            "required_worker_count_by_good": required_by_good,
            "active_worker_count_by_good": active_by_good,
            "background_production_labor_factor_by_good": {
                good: round(
                    active_by_good[good] / required_by_good[good], 6)
                if required_by_good[good] else 0.0
                for good in ACTIVITY_GOODS},
            "unassigned_productive_population": labor_plan[
                "unassigned_productive_population"],
            "background_production_labor_factor": labor_plan["labor_factor"],
            "activities": activities,
        }

    return {
        "state": {
            "version": ACTIVITY_ECONOMY_VERSION,
            "updated_turn": int(turn),
            "communities": communities,
            "cumulative_output_by_good": cumulative,
        },
        "registry": after_registry,
        "events": events,
    }
