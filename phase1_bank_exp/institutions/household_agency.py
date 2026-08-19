# -*- coding: utf-8 -*-
"""世帯の財アクセスと不足対応を、次月の活動優先へ変換する純粋台帳。

共同体の物理在庫は ``settlement["local_economy"]``、その所有内訳は
``household_goods_state`` が正本である。このモジュールは財を生産・消費せず、
共同体共用分へのアクセスを世帯claimへ移し、アクセス後にも残る不足から
「どの財を優先して担うか」を導出する。

名前付き世帯は最大4096件の個別行、残りの人口は共同体別匿名pool 1行として
扱うため、状態量と月次処理は総人口ではなく観察標本数と共同体数に比例する。
全関数は入力を変更せず、乱数を使わない。
"""
from __future__ import annotations

import copy

from institutions.household_goods import (
    HOUSEHOLD_GOODS,
    ROUND_DIGITS,
    reconcile_household_goods_state,
    upgrade_household_goods_state,
    verify_household_goods_state,
)
from institutions.household_exchange import plan_household_barter_exchange
from institutions.local_credit import (
    LOCAL_CREDIT_STAGE_CONTRACTION,
    LOCAL_CREDIT_STAGE_HEALTHY,
    LOCAL_CREDIT_STAGE_ISOLATED,
    LOCAL_CREDIT_STAGE_PERSONAL,
)


HOUSEHOLD_AGENCY_VERSION = 1
SHORTAGE_RESPONSE_THRESHOLD = 0.05
PRIORITY_SWITCH_MARGIN = 0.05
HOUSEHOLD_SHORTAGE_LABOR_PRIORITY = 0.75
COMMON_ACCESS_FACTOR_BY_LOCAL_CREDIT_STAGE = {
    LOCAL_CREDIT_STAGE_HEALTHY: 1.0,
    LOCAL_CREDIT_STAGE_CONTRACTION: 0.70,
    LOCAL_CREDIT_STAGE_PERSONAL: 0.25,
    LOCAL_CREDIT_STAGE_ISOLATED: 0.0,
}


def _zero_goods() -> dict[str, float]:
    return {good: 0.0 for good in HOUSEHOLD_GOODS}


def _zero_counts() -> dict[str, int]:
    return {good: 0 for good in HOUSEHOLD_GOODS}


def initial_household_agency_state(turn: int = 0) -> dict:
    return {
        "version": HOUSEHOLD_AGENCY_VERSION,
        "updated_turn": int(turn),
        "households": {},
        "communities": {},
        "world_priority_household_counts_by_good": _zero_counts(),
        "world_priority_pressure_by_good": _zero_goods(),
    }


def upgrade_household_agency_state(state: dict | None) -> dict:
    if state is None:
        return initial_household_agency_state()
    if not isinstance(state, dict):
        raise TypeError("household agency state must be a dict")
    version = int(state.get("version", 0))
    if version != HOUSEHOLD_AGENCY_VERSION:
        raise ValueError(f"unsupported household agency version: {version}")
    after = copy.deepcopy(state)
    after.setdefault("updated_turn", 0)
    after.setdefault("households", {})
    after.setdefault("communities", {})
    after.setdefault(
        "world_priority_household_counts_by_good", _zero_counts())
    after.setdefault("world_priority_pressure_by_good", _zero_goods())
    return after


def _allocate_float(total: float, raw_weights: dict[str, float]) -> dict[str, float]:
    total = round(max(0.0, float(total)), ROUND_DIGITS)
    weights = {
        str(key): max(0.0, float(value))
        for key, value in raw_weights.items() if float(value) > 0.0}
    if total <= 0.0 or not weights:
        return {key: 0.0 for key in weights}
    total = min(total, round(sum(weights.values()), ROUND_DIGITS))
    denominator = sum(weights.values())
    keys = sorted(weights)
    result = {}
    used = 0.0
    for key in keys[:-1]:
        amount = round(total * weights[key] / denominator, ROUND_DIGITS)
        amount = min(amount, weights[key])
        result[key] = amount
        used = round(used + amount, ROUND_DIGITS)
    result[keys[-1]] = round(min(
        weights[keys[-1]], max(0.0, total - used)), ROUND_DIGITS)
    # 上限制約で丸め残りが出た場合だけ、余地のある行へ決定論的に戻す。
    remaining = round(total - sum(result.values()), ROUND_DIGITS)
    for key in keys:
        if remaining <= 0.0:
            break
        room = round(weights[key] - result.get(key, 0.0), ROUND_DIGITS)
        amount = min(room, remaining)
        result[key] = round(result.get(key, 0.0) + amount, ROUND_DIGITS)
        remaining = round(remaining - amount, ROUND_DIGITS)
    return result


def _community_local_credit_stage(settlement: dict) -> int:
    return int(settlement.get("local_economy", {}).get(
        "local_credit_stage", LOCAL_CREDIT_STAGE_HEALTHY))


def _access_factor(stage: int) -> float:
    if stage <= LOCAL_CREDIT_STAGE_HEALTHY:
        return 1.0
    return float(COMMON_ACCESS_FACTOR_BY_LOCAL_CREDIT_STAGE.get(
        int(stage), 0.0))


def _add_common_access_total(row: dict, good: str, amount: float) -> None:
    totals = row.setdefault("common_access_totals", _zero_goods())
    totals[good] = round(
        max(0.0, float(totals.get(good, 0.0)) + float(amount)),
        ROUND_DIGITS)


def _grant_common_access(goods_state: dict, needs_state: dict,
                         settlements: dict, turn: int) -> list[dict]:
    """共用分を需要上限まで世帯claimへ移す。物理総量は変更しない。"""
    events = []
    communities = needs_state.get("communities", {})
    for community_id, needs in sorted(communities.items()):
        community_id = str(community_id)
        settlement = settlements.get(community_id, {})
        stage = _community_local_credit_stage(settlement)
        factor = _access_factor(stage)
        common = goods_state.get("common_pool_by_community", {}).get(
            community_id, {})
        for good in HOUSEHOLD_GOODS:
            available = max(0.0, float(common.get(good, 0.0)))
            if available <= 0.0 or factor <= 0.0:
                continue
            claims = {}
            for household_id, demand_row in needs.get(
                    "household_demands", {}).items():
                account = goods_state.get("households", {}).get(
                    str(household_id))
                if account is None:
                    continue
                target = max(0.0, float(demand_row.get(
                    "demand_quantity_by_good", {}).get(good, 0.0))) * factor
                missing = max(
                    0.0, target - float(account.get(
                        "holdings", {}).get(good, 0.0)))
                if missing > 0.0:
                    claims[f"h:{household_id}"] = missing
            anonymous = goods_state.get("anonymous_pools", {}).get(
                community_id)
            if anonymous is not None:
                target = max(0.0, float(needs.get(
                    "anonymous_demand_quantity_by_good", {}).get(
                        good, 0.0))) * factor
                missing = max(
                    0.0, target - float(anonymous.get(
                        "holdings", {}).get(good, 0.0)))
                if missing > 0.0:
                    claims[f"a:{community_id}"] = missing
            grants = _allocate_float(available, claims)
            named_allocations = {}
            anonymous_allocation = 0.0
            for key, amount in grants.items():
                if amount <= 0.0:
                    continue
                if key.startswith("h:"):
                    household_id = key[2:]
                    account = goods_state["households"][household_id]
                    account["holdings"][good] = round(
                        account["holdings"][good] + amount, ROUND_DIGITS)
                    _add_common_access_total(account, good, amount)
                    named_allocations[household_id] = amount
                else:
                    anonymous["holdings"][good] = round(
                        anonymous["holdings"][good] + amount, ROUND_DIGITS)
                    _add_common_access_total(anonymous, good, amount)
                    anonymous_allocation = amount
            total = round(sum(grants.values()), ROUND_DIGITS)
            if total > 0.0:
                goods_state["common_pool_by_community"][community_id][good] = (
                    round(available - total, ROUND_DIGITS))
                events.append({
                    "turn": int(turn),
                    "kind": "household_common_goods_accessed",
                    "settlement_id": community_id,
                    "good": good,
                    "local_credit_stage": stage,
                    "access_factor": factor,
                    "named_household_count": len(named_allocations),
                    "named_household_allocations_sample": {
                        household_id: named_allocations[household_id]
                        for household_id in sorted(named_allocations)[:32]},
                    "anonymous_allocation": anonymous_allocation,
                    "total": total,
                })
    return events


def _refresh_goods_aggregates(goods_state: dict, turn: int) -> None:
    goods_state["updated_turn"] = int(turn)
    goods_state["world_household_holdings"] = {
        good: round(sum(float(row.get("holdings", {}).get(good, 0.0))
                        for row in goods_state.get(
                            "households", {}).values()), ROUND_DIGITS)
        for good in HOUSEHOLD_GOODS}
    goods_state["world_anonymous_holdings"] = {
        good: round(sum(float(row.get("holdings", {}).get(good, 0.0))
                        for row in goods_state.get(
                            "anonymous_pools", {}).values()), ROUND_DIGITS)
        for good in HOUSEHOLD_GOODS}
    goods_state["world_common_pool"] = {
        good: round(sum(float(row.get(good, 0.0))
                        for row in goods_state.get(
                            "common_pool_by_community", {}).values()),
                    ROUND_DIGITS)
        for good in HOUSEHOLD_GOODS}


def _coverage_and_shortfall(holdings: dict, demand: dict) -> tuple[dict, dict]:
    coverage = {}
    shortfall = {}
    for good in HOUSEHOLD_GOODS:
        required = max(0.0, float(demand.get(good, 0.0)))
        owned = max(0.0, float(holdings.get(good, 0.0)))
        ratio = (1.0 if required <= 0.0 else
                 max(0.0, min(1.0, owned / required)))
        coverage[good] = round(ratio * 100.0, ROUND_DIGITS)
        shortfall[good] = round(1.0 - ratio, ROUND_DIGITS)
    return coverage, shortfall


def _priority_good(shortfall: dict, previous: str | None,
                   livelihood: str | None) -> str | None:
    worst = max((float(shortfall.get(good, 0.0))
                 for good in HOUSEHOLD_GOODS), default=0.0)
    if worst <= SHORTAGE_RESPONSE_THRESHOLD:
        return None
    if (previous in HOUSEHOLD_GOODS
            and worst - float(shortfall.get(previous, 0.0))
            <= PRIORITY_SWITCH_MARGIN):
        return previous
    if (livelihood in HOUSEHOLD_GOODS
            and worst - float(shortfall.get(livelihood, 0.0)) <= 1e-9):
        return livelihood
    return min(
        HOUSEHOLD_GOODS,
        key=lambda good: (-float(shortfall.get(good, 0.0)),
                          HOUSEHOLD_GOODS.index(good)))


def plan_household_agency(
        state: dict | None, household_goods_state: dict | None,
        settlements: dict, registry: dict, household_needs_state: dict,
        organization_state: dict | None, turn: int, *,
        barter_active: bool = False) -> dict:
    """共用財アクセスを適用し、世帯ごとの次月優先財を計画する。"""
    before = upgrade_household_agency_state(state)
    if verify_household_goods_state(
            household_goods_state or {}, settlements, registry,
            household_needs_state, organization_state):
        goods_working = upgrade_household_goods_state(
            household_goods_state)
        reconciliation_events = []
    else:
        reconciled = reconcile_household_goods_state(
            household_goods_state, settlements, registry,
            household_needs_state, organization_state, turn)
        goods_working = reconciled["state"]
        reconciliation_events = reconciled["events"]
    access_events = _grant_common_access(
        goods_working, household_needs_state, settlements, turn)
    exchange_events = []
    if barter_active:
        exchange = plan_household_barter_exchange(
            goods_working, settlements, household_needs_state, turn,
            enabled=True)
        goods_working = exchange["state"]
        exchange_events = exchange["events"]
    _refresh_goods_aggregates(goods_working, turn)
    goods_after = goods_working
    if not verify_household_goods_state(
            goods_after, settlements, registry, household_needs_state,
            organization_state):
        raise RuntimeError("household common access broke goods conservation")
    households = {}
    communities = {}
    response_events = []
    world_counts = _zero_counts()
    world_unmet = _zero_goods()
    world_demand = _zero_goods()
    registry_households = registry.get("households", {})
    repeated_same_turn = int(before.get("updated_turn", -1)) == int(turn)

    for community_id, needs in sorted(household_needs_state.get(
            "communities", {}).items()):
        community_id = str(community_id)
        counts = _zero_counts()
        changed_household_ids = []
        unmet = _zero_goods()
        total_demand = _zero_goods()
        for household_id, demand_row in sorted(needs.get(
                "household_demands", {}).items()):
            household_id = str(household_id)
            account = goods_after.get("households", {}).get(
                household_id, {})
            demand = demand_row.get("demand_quantity_by_good", {})
            coverage, shortfall = _coverage_and_shortfall(
                account.get("holdings", {}), demand)
            previous_row = before.get("households", {}).get(
                household_id, {})
            livelihood = registry_households.get(
                household_id, {}).get("livelihood")
            priority = _priority_good(
                shortfall, previous_row.get("priority_good"), livelihood)
            shortage_months = (
                int(previous_row.get("consecutive_shortage_months", 0))
                + (0 if repeated_same_turn else 1)
                if priority is not None else 0)
            households[household_id] = {
                "household_id": household_id,
                "settlement_id": community_id,
                "mode": ("secure_needs" if priority is not None
                         else "routine"),
                "priority_good": priority,
                "coverage_by_good": coverage,
                "shortfall_by_good": shortfall,
                "consecutive_shortage_months": shortage_months,
            }
            if priority is not None:
                counts[priority] += 1
                world_counts[priority] += 1
            for good in HOUSEHOLD_GOODS:
                required = max(0.0, float(demand.get(good, 0.0)))
                total_demand[good] += required
                unmet[good] += required * shortfall[good]
                world_demand[good] += required
                world_unmet[good] += required * shortfall[good]
            if previous_row.get("priority_good") != priority:
                changed_household_ids.append(household_id)

        anonymous_account = goods_after.get("anonymous_pools", {}).get(
            community_id, {})
        anonymous_demand = needs.get(
            "anonymous_demand_quantity_by_good", {})
        anonymous_coverage, anonymous_shortfall = _coverage_and_shortfall(
            anonymous_account.get("holdings", {}), anonymous_demand)
        previous_anonymous = before.get("communities", {}).get(
            community_id, {}).get("anonymous", {})
        anonymous_priority = _priority_good(
            anonymous_shortfall,
            previous_anonymous.get("priority_good"), None)
        for good in HOUSEHOLD_GOODS:
            required = max(0.0, float(anonymous_demand.get(good, 0.0)))
            total_demand[good] += required
            unmet[good] += required * anonymous_shortfall[good]
            world_demand[good] += required
            world_unmet[good] += required * anonymous_shortfall[good]
        pressure = {
            good: round(
                unmet[good] / total_demand[good]
                if total_demand[good] > 0.0 else 0.0,
                ROUND_DIGITS)
            for good in HOUSEHOLD_GOODS}
        stage = _community_local_credit_stage(
            settlements.get(community_id, {}))
        changed_household_count = (
            int(before.get("communities", {}).get(
                community_id, {}).get("changed_household_count", 0))
            if repeated_same_turn else len(changed_household_ids))
        communities[community_id] = {
            "settlement_id": community_id,
            "local_credit_stage": stage,
            "common_access_factor": _access_factor(stage),
            "named_household_count": len(needs.get(
                "household_demands", {})),
            "anonymous_population": int(needs.get(
                "anonymous_population", 0)),
            "priority_household_counts_by_good": counts,
            "priority_pressure_by_good": pressure,
            "changed_household_count": changed_household_count,
            "anonymous": {
                "priority_good": anonymous_priority,
                "coverage_by_good": anonymous_coverage,
                "shortfall_by_good": anonymous_shortfall,
            },
        }
        if (changed_household_ids
                or any(value > 0.0 for value in pressure.values()) or any(
                event.get("settlement_id") == community_id
                for event in access_events)):
            response_events.append({
                "turn": int(turn),
                "kind": "household_response_summary",
                "settlement_id": community_id,
                "local_credit_stage": stage,
                "priority_household_counts_by_good": counts,
                "priority_pressure_by_good": pressure,
                "anonymous_priority_good": anonymous_priority,
                "changed_household_count": changed_household_count,
                "changed_household_ids": changed_household_ids[:32],
            })

    world_pressure = {
        good: round(
            world_unmet[good] / world_demand[good]
            if world_demand[good] > 0.0 else 0.0,
            ROUND_DIGITS)
        for good in HOUSEHOLD_GOODS}
    after = {
        "version": HOUSEHOLD_AGENCY_VERSION,
        "updated_turn": int(turn),
        "households": households,
        "communities": communities,
        "world_priority_household_counts_by_good": world_counts,
        "world_priority_pressure_by_good": world_pressure,
    }
    if not verify_household_agency_state(
            after, goods_after, household_needs_state):
        raise RuntimeError("household agency state verification failed")
    return {
        "state": after,
        "household_goods_state": goods_after,
        "events": response_events,
        "household_goods_events": (
            reconciliation_events + access_events + exchange_events),
    }


def household_priority_by_id(state: dict | None) -> dict[str, str]:
    """不足対応中の名前付き世帯だけを、活動配賦用mappingで返す。"""
    return {
        str(household_id): row["priority_good"]
        for household_id, row in (state or {}).get("households", {}).items()
        if row.get("priority_good") in HOUSEHOLD_GOODS}


def community_priority_pressure(state: dict | None,
                                settlement_id: str) -> dict[str, float]:
    row = (state or {}).get("communities", {}).get(str(settlement_id), {})
    raw = row.get("priority_pressure_by_good", {})
    return {
        good: max(0.0, min(1.0, float(raw.get(good, 0.0))))
        for good in HOUSEHOLD_GOODS}


def verify_household_agency_state(state: dict, household_goods_state: dict,
                                  household_needs_state: dict) -> bool:
    if int(state.get("version", 0)) != HOUSEHOLD_AGENCY_VERSION:
        return False
    needs_communities = household_needs_state.get("communities", {})
    if set(state.get("communities", {})) != set(needs_communities):
        return False
    expected_households = {
        str(household_id)
        for row in needs_communities.values()
        for household_id in row.get("household_demands", {})}
    if set(state.get("households", {})) != expected_households:
        return False
    if not expected_households.issubset(
            household_goods_state.get("households", {})):
        return False
    expected_world_counts = _zero_counts()
    world_unmet = _zero_goods()
    world_demand = _zero_goods()
    for community_id, needs in needs_communities.items():
        community_id = str(community_id)
        community = state["communities"][community_id]
        expected_counts = _zero_counts()
        community_unmet = _zero_goods()
        community_demand = _zero_goods()
        if int(community.get("named_household_count", -1)) != len(
                needs.get("household_demands", {})):
            return False
        if int(community.get("anonymous_population", -1)) != int(
                needs.get("anonymous_population", 0)):
            return False
        for household_id, demand_row in needs.get(
                "household_demands", {}).items():
            household_id = str(household_id)
            response = state["households"][household_id]
            account = household_goods_state["households"][household_id]
            if str(response.get("settlement_id")) != community_id:
                return False
            priority = response.get("priority_good")
            if priority not in (None, *HOUSEHOLD_GOODS):
                return False
            if priority is not None:
                expected_counts[priority] += 1
                expected_world_counts[priority] += 1
            demand = demand_row.get("demand_quantity_by_good", {})
            coverage = response.get("coverage_by_good", {})
            shortfall = response.get("shortfall_by_good", {})
            for good in HOUSEHOLD_GOODS:
                required = max(0.0, float(demand.get(good, 0.0)))
                owned = max(0.0, float(account.get(
                    "holdings", {}).get(good, 0.0)))
                expected_ratio = (1.0 if required <= 0.0 else
                                  max(0.0, min(1.0, owned / required)))
                if abs(float(coverage.get(good, -1.0))
                       - expected_ratio * 100.0) > 2e-5:
                    return False
                if abs(float(shortfall.get(good, -1.0))
                       - (1.0 - expected_ratio)) > 2e-5:
                    return False
                community_demand[good] += required
                community_unmet[good] += required * (1.0 - expected_ratio)
                world_demand[good] += required
                world_unmet[good] += required * (1.0 - expected_ratio)
        anonymous = community.get("anonymous", {})
        anonymous_account = household_goods_state.get(
            "anonymous_pools", {}).get(community_id, {})
        anonymous_demand = needs.get(
            "anonymous_demand_quantity_by_good", {})
        for good in HOUSEHOLD_GOODS:
            required = max(0.0, float(anonymous_demand.get(good, 0.0)))
            owned = max(0.0, float(anonymous_account.get(
                "holdings", {}).get(good, 0.0)))
            expected_ratio = (1.0 if required <= 0.0 else
                              max(0.0, min(1.0, owned / required)))
            if abs(float(anonymous.get(
                    "coverage_by_good", {}).get(good, -1.0))
                   - expected_ratio * 100.0) > 2e-5:
                return False
            if abs(float(anonymous.get(
                    "shortfall_by_good", {}).get(good, -1.0))
                   - (1.0 - expected_ratio)) > 2e-5:
                return False
            community_demand[good] += required
            community_unmet[good] += required * (1.0 - expected_ratio)
            world_demand[good] += required
            world_unmet[good] += required * (1.0 - expected_ratio)
        if community.get("priority_household_counts_by_good") != (
                expected_counts):
            return False
        for good in HOUSEHOLD_GOODS:
            expected_pressure = (
                community_unmet[good] / community_demand[good]
                if community_demand[good] > 0.0 else 0.0)
            if abs(float(community.get(
                    "priority_pressure_by_good", {}).get(good, -1.0))
                   - expected_pressure) > 2e-5:
                return False
    if state.get("world_priority_household_counts_by_good") != (
            expected_world_counts):
        return False
    for good in HOUSEHOLD_GOODS:
        expected_pressure = (
            world_unmet[good] / world_demand[good]
            if world_demand[good] > 0.0 else 0.0)
        if abs(float(state.get(
                "world_priority_pressure_by_good", {}).get(good, -1.0))
               - expected_pressure) > 2e-5:
            return False
    return True
