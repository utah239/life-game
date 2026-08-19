# -*- coding: utf-8 -*-
"""共同体の物理在庫を、世帯・匿名人口・共用分へ分ける階層台帳。

``settlement["local_economy"]`` の4財は、引き続き共同体に存在する物理総量の
正本である。このモジュールの世帯保有・匿名人口保有・共用分と、既存の組織
``asset_claims`` はその**内訳**であり、世界総量へ二重加算しない。

大人口世界では全住民を個体化しない。名前付き観察標本は世帯ID単位、残りは
共同体別の匿名poolとして保持するため、状態量は人口ではなく
``名前付き世帯数 + 活動共同体数`` に比例する。

財の移動規則:

* food / medicine / tools は携行可能。世帯移住とともに共同体総量も移す。
* shelter は活動場所に固定し、移住時は元共同体の共用分へ戻す。
* 生産・消費は既存の共同体会計が確定したgross flowを内訳へ写すだけで、
  財を追加で生産・消費しない。
* 死亡後も同じ世帯に生存者がいれば保有はそのまま。世帯が閉じた場合は
  子孫、同姓世帯、共用分の順で継承・返還する。

全関数は入力を変更せず、乱数を使わない。
"""
from __future__ import annotations

import copy

from institutions.barter import goods_capacity


HOUSEHOLD_GOODS_VERSION = 1
HOUSEHOLD_GOODS = ("food", "medicine", "shelter", "tools")
PORTABLE_HOUSEHOLD_GOODS = ("food", "medicine", "tools")
SITE_BOUND_HOUSEHOLD_GOODS = ("shelter",)
ROUND_DIGITS = 6
EPSILON = 1e-6


def _zero_goods() -> dict[str, float]:
    return {good: 0.0 for good in HOUSEHOLD_GOODS}


def _goods(raw: dict | None) -> dict[str, float]:
    raw = raw or {}
    return {
        good: round(max(0.0, float(raw.get(good, 0.0))), ROUND_DIGITS)
        for good in HOUSEHOLD_GOODS}


def _account_record(account_id: str, settlement_id: str, *,
                    population: int | None = None) -> dict:
    row = {
        "household_id": str(account_id),
        "settlement_id": str(settlement_id),
        "holdings": _zero_goods(),
        "acquired_totals": _zero_goods(),
        "common_access_totals": _zero_goods(),
        "barter_sent_totals": _zero_goods(),
        "barter_received_totals": _zero_goods(),
        "barter_exchange_count": 0,
        "consumed_totals": _zero_goods(),
        "inherited_totals": _zero_goods(),
        "migrated_sent_totals": _zero_goods(),
        "migrated_received_totals": _zero_goods(),
    }
    if population is not None:
        row.pop("household_id")
        row["population"] = max(0, int(population))
    return row


def initial_household_goods_state(turn: int = 0) -> dict:
    """空の内訳台帳。共同体総量は最初のreconcileで共用分として現れる。"""
    return {
        "version": HOUSEHOLD_GOODS_VERSION,
        "updated_turn": int(turn),
        "households": {},
        "anonymous_pools": {},
        "common_pool_by_community": {},
        "organization_claims_by_community": {},
        "world_physical_goods": _zero_goods(),
        "world_household_holdings": _zero_goods(),
        "world_anonymous_holdings": _zero_goods(),
        "world_organization_claims": _zero_goods(),
        "world_common_pool": _zero_goods(),
        "world_household_barter_volume_by_good": _zero_goods(),
        "world_household_barter_exchange_count": 0,
        "household_barter_applied_turn": None,
    }


def upgrade_household_goods_state(state: dict | None) -> dict:
    if state is None:
        return initial_household_goods_state()
    if not isinstance(state, dict):
        raise TypeError("household goods state must be a dict")
    version = int(state.get("version", 0))
    if version != HOUSEHOLD_GOODS_VERSION:
        raise ValueError(f"unsupported household goods version: {version}")
    after = copy.deepcopy(state)
    after.setdefault("updated_turn", 0)
    after.setdefault("households", {})
    after.setdefault("anonymous_pools", {})
    for household_id, row in after["households"].items():
        row["household_id"] = str(household_id)
        row["settlement_id"] = str(row.get("settlement_id", ""))
        for field in (
                "holdings", "acquired_totals", "common_access_totals",
                "barter_sent_totals", "barter_received_totals",
                "consumed_totals",
                "inherited_totals", "migrated_sent_totals",
                "migrated_received_totals"):
            row[field] = _goods(row.get(field))
        row["barter_exchange_count"] = max(
            0, int(row.get("barter_exchange_count", 0)))
    for settlement_id, row in after["anonymous_pools"].items():
        row["settlement_id"] = str(settlement_id)
        row["population"] = max(0, int(row.get("population", 0)))
        for field in (
                "holdings", "acquired_totals", "common_access_totals",
                "barter_sent_totals", "barter_received_totals",
                "consumed_totals",
                "migrated_sent_totals", "migrated_received_totals"):
            row[field] = _goods(row.get(field))
        row["barter_exchange_count"] = max(
            0, int(row.get("barter_exchange_count", 0)))
    for field in (
            "common_pool_by_community", "organization_claims_by_community"):
        after.setdefault(field, {})
        after[field] = {
            str(key): _goods(value) for key, value in after[field].items()}
    for field in (
            "world_physical_goods", "world_household_holdings",
            "world_anonymous_holdings", "world_organization_claims",
            "world_common_pool", "world_household_barter_volume_by_good"):
        after[field] = _goods(after.get(field))
    after["world_household_barter_exchange_count"] = max(
        0, int(after.get("world_household_barter_exchange_count", 0)))
    applied_turn = after.get("household_barter_applied_turn")
    after["household_barter_applied_turn"] = (
        None if applied_turn is None else int(applied_turn))
    return after


def _organization_claims_by_community(
        organization_state: dict | None) -> dict[str, dict[str, float]]:
    claims: dict[str, dict[str, float]] = {}
    for row in (organization_state or {}).get("organizations", {}).values():
        if not row.get("active", True):
            continue
        community_id = str(row.get("home_activity_cluster_id") or "")
        if not community_id:
            continue
        aggregate = claims.setdefault(community_id, _zero_goods())
        raw = row.get("asset_claims", {})
        for good in HOUSEHOLD_GOODS:
            aggregate[good] = round(
                aggregate[good] + max(0.0, float(raw.get(good, 0.0))),
                ROUND_DIGITS)
    return claims


def _active_household_locations(registry: dict,
                                household_needs_state: dict) -> dict[str, str]:
    locations = {}
    for settlement_id, community in household_needs_state.get(
            "communities", {}).items():
        for household_id in community.get("household_demands", {}):
            locations[str(household_id)] = str(settlement_id)
    # 最小fixtureや移行中stateでneedsに名前付き内訳が無い場合だけregistryへ
    # fallbackする。死亡済みだけの世帯はここへ入れない。
    if locations:
        return locations
    living_households = {
        str(row.get("household_id"))
        for row in registry.get("residents", {}).values()
        if row.get("alive", True)}
    for household_id in living_households:
        household = registry.get("households", {}).get(household_id, {})
        locations[household_id] = str(household.get("settlement_id", ""))
    return locations


def _anonymous_populations(household_needs_state: dict) -> dict[str, int]:
    return {
        str(settlement_id): max(0, int(row.get("anonymous_population", 0)))
        for settlement_id, row in household_needs_state.get(
            "communities", {}).items()}


def _allocate_float(total: float, raw_weights: dict[str, float]) -> dict[str, float]:
    total = round(max(0.0, float(total)), ROUND_DIGITS)
    weights = {
        str(key): max(0.0, float(value))
        for key, value in raw_weights.items() if float(value) > 0.0}
    if total <= 0.0 or not weights:
        return {key: 0.0 for key in weights}
    denominator = sum(weights.values())
    keys = sorted(weights)
    result = {}
    used = 0.0
    for key in keys[:-1]:
        value = round(total * weights[key] / denominator, ROUND_DIGITS)
        result[key] = value
        used = round(used + value, ROUND_DIGITS)
    result[keys[-1]] = round(total - used, ROUND_DIGITS)
    return result


def _add_total(row: dict, field: str, good: str, amount: float) -> None:
    row.setdefault(field, _zero_goods())
    row[field][good] = round(
        max(0.0, float(row[field].get(good, 0.0)) + float(amount)),
        ROUND_DIGITS)


def _synchronize_accounts(state: dict, registry: dict,
                          household_needs_state: dict) -> list[dict]:
    """現在の名前付き世帯と匿名poolへIDを同期し、孤児claimを共用へ返す。"""
    events = []
    locations = _active_household_locations(registry, household_needs_state)
    households = state["households"]
    for household_id, settlement_id in sorted(locations.items()):
        if household_id not in households:
            households[household_id] = _account_record(
                household_id, settlement_id)
            events.append({
                "kind": "household_goods_account_opened",
                "household_id": household_id,
                "settlement_id": settlement_id,
            })
        else:
            households[household_id]["settlement_id"] = settlement_id

    # needsから消えた世帯のclaimは物理総量から消さず、共同体共用分へ戻す。
    # closed処理が先に継承済みならholdingは既に0である。
    for household_id, row in households.items():
        if household_id in locations:
            continue
        released = _goods(row.get("holdings"))
        if any(released.values()):
            events.append({
                "kind": "household_goods_released",
                "household_id": household_id,
                "settlement_id": row.get("settlement_id"),
                "goods": released,
                "reason": "household_no_longer_active",
            })
        row["holdings"] = _zero_goods()

    # 閉鎖世帯の履歴はresident/trace側が正本であり、財台帳へ空口座を累積させない。
    # これにより長期世界でも状態量は「現在の名前付き世帯 + 共同体数」に収まる。
    for household_id in list(households):
        if household_id not in locations and not any(
                households[household_id]["holdings"].values()):
            del households[household_id]

    populations = _anonymous_populations(household_needs_state)
    pools = state["anonymous_pools"]
    orphan_goods = _zero_goods()
    for settlement_id in list(pools):
        if settlement_id in populations:
            continue
        for good, value in pools[settlement_id]["holdings"].items():
            orphan_goods[good] = round(
                orphan_goods[good] + value, ROUND_DIGITS)
        del pools[settlement_id]
    for settlement_id, population in sorted(populations.items()):
        if settlement_id not in pools:
            pools[settlement_id] = _account_record(
                settlement_id, settlement_id, population=population)
        pools[settlement_id]["population"] = population
        pools[settlement_id]["settlement_id"] = settlement_id
    if any(orphan_goods.values()) and pools:
        weights = {
            settlement_id: max(0, row["population"])
            for settlement_id, row in pools.items()}
        if not any(weights.values()):
            weights = {settlement_id: 1 for settlement_id in pools}
        for good, total in orphan_goods.items():
            for settlement_id, amount in _allocate_float(total, weights).items():
                pools[settlement_id]["holdings"][good] = round(
                    pools[settlement_id]["holdings"][good] + amount,
                    ROUND_DIGITS)
        events.append({
            "kind": "anonymous_goods_reallocated",
            "goods": orphan_goods,
            "reason": "activity_community_rekey",
        })
    return events


def _physical_goods(settlements: dict) -> dict[str, dict[str, float]]:
    return {
        str(settlement_id): _goods(row.get("local_economy", {}))
        for settlement_id, row in settlements.items()}


def reconcile_household_goods_state(
        state: dict | None, settlements: dict, registry: dict,
        household_needs_state: dict, organization_state: dict | None,
        turn: int) -> dict:
    """現在の物理総量へ内訳を収め、共用分と世界合計を再計算する。"""
    after = upgrade_household_goods_state(state)
    events = _synchronize_accounts(after, registry, household_needs_state)
    physical = _physical_goods(settlements)
    organization = _organization_claims_by_community(organization_state)
    community_ids = sorted(physical)

    for community_id in community_ids:
        organization.setdefault(community_id, _zero_goods())
    unknown_claims = set(organization) - set(physical)
    if any(any(organization[key].values()) for key in unknown_claims):
        raise ValueError("organization claims refer to a missing community")

    common = {community_id: _zero_goods() for community_id in community_ids}
    for community_id in community_ids:
        named_rows = [
            row for row in after["households"].values()
            if str(row.get("settlement_id")) == community_id]
        anonymous = after["anonymous_pools"].get(community_id)
        for good in HOUSEHOLD_GOODS:
            total = physical[community_id][good]
            org = organization[community_id][good]
            if org > total + EPSILON:
                raise ValueError(
                    f"organization {good} claims exceed physical goods in "
                    f"{community_id}")
            budget = round(max(0.0, total - org), ROUND_DIGITS)
            accounts = {
                f"h:{row['household_id']}": row["holdings"][good]
                for row in named_rows if row["holdings"][good] > 0.0}
            if anonymous is not None and anonymous["holdings"][good] > 0.0:
                accounts[f"a:{community_id}"] = anonymous["holdings"][good]
            claimed = round(sum(accounts.values()), ROUND_DIGITS)
            if claimed > budget + EPSILON:
                scaled = _allocate_float(budget, accounts)
                released = round(claimed - budget, ROUND_DIGITS)
                for key in accounts:
                    if key.startswith("h:"):
                        after["households"][key[2:]]["holdings"][good] = (
                            scaled.get(key, 0.0))
                    else:
                        after["anonymous_pools"][community_id][
                            "holdings"][good] = scaled.get(key, 0.0)
                events.append({
                    "kind": "household_goods_claims_reconciled",
                    "settlement_id": community_id,
                    "good": good,
                    "released": released,
                    "reason": "physical_inventory_or_organization_claim_limit",
                })
                claimed = budget
            common[community_id][good] = round(
                max(0.0, budget - claimed), ROUND_DIGITS)

    after["common_pool_by_community"] = common
    after["organization_claims_by_community"] = {
        key: value for key, value in organization.items() if key in physical}
    after["updated_turn"] = int(turn)
    after["world_physical_goods"] = {
        good: round(sum(row[good] for row in physical.values()), ROUND_DIGITS)
        for good in HOUSEHOLD_GOODS}
    after["world_household_holdings"] = {
        good: round(sum(row["holdings"][good]
                        for row in after["households"].values()), ROUND_DIGITS)
        for good in HOUSEHOLD_GOODS}
    after["world_anonymous_holdings"] = {
        good: round(sum(row["holdings"][good]
                        for row in after["anonymous_pools"].values()),
                    ROUND_DIGITS)
        for good in HOUSEHOLD_GOODS}
    after["world_organization_claims"] = {
        good: round(sum(row[good]
                        for row in after[
                            "organization_claims_by_community"].values()),
                    ROUND_DIGITS)
        for good in HOUSEHOLD_GOODS}
    after["world_common_pool"] = {
        good: round(sum(row[good] for row in common.values()), ROUND_DIGITS)
        for good in HOUSEHOLD_GOODS}
    for event in events:
        event.setdefault("turn", int(turn))
    if not verify_household_goods_state(
            after, settlements, registry, household_needs_state,
            organization_state):
        raise RuntimeError("household goods reconciliation failed")
    return {"state": after, "events": events}


def _flow_allocations(household_needs_state: dict, settlement_id: str,
                      good: str, total: float) -> tuple[dict, float]:
    community = household_needs_state.get("communities", {}).get(
        str(settlement_id), {})
    named_weights = {
        str(household_id): max(0.0, float(row.get(
            "demand_quantity_by_good", {}).get(good, 0.0)))
        for household_id, row in community.get(
            "household_demands", {}).items()}
    anonymous_weight = max(0.0, float(community.get(
        "anonymous_demand_quantity_by_good", {}).get(good, 0.0)))
    weights = {f"h:{key}": value for key, value in named_weights.items()}
    if anonymous_weight > 0.0:
        weights[f"a:{settlement_id}"] = anonymous_weight
    allocation = _allocate_float(total, weights)
    named = {
        key[2:]: value for key, value in allocation.items()
        if key.startswith("h:")}
    anonymous = allocation.get(f"a:{settlement_id}", 0.0)
    return named, anonymous


def _actual_upkeep_flows(before: float, plan: dict, good: str) -> tuple[float, float, float]:
    gross_production = max(0.0, float(
        plan.get("gross_production", {}).get(good, 0.0)))
    gross_consumption = max(0.0, float(
        plan.get("gross_consumption", {}).get(good, 0.0)))
    after = max(0.0, float(plan.get(f"{good}_after", before)))
    actual_consumption = min(
        gross_consumption, max(0.0, float(before) + gross_production))
    actual_production = max(
        0.0, after - float(before) + actual_consumption)
    return (
        round(actual_production, ROUND_DIGITS),
        round(actual_consumption, ROUND_DIGITS),
        round(max(0.0, gross_consumption - actual_consumption), ROUND_DIGITS),
    )


def plan_household_goods_provisioning(
        state: dict | None, settlements: dict, registry: dict,
        household_needs_state: dict, organization_state: dict | None,
        activity_economy_state: dict, spatial_state: dict,
        goods_before_by_community: dict,
        upkeep_plans_by_community: dict, turn: int, *,
        finalize: bool = True) -> dict:
    """既存upkeepのgross生産・消費を世帯内訳へ写す。

    ``settlements``は既に共同体会計を適用済みであり、この関数は変更しない。
    生産のうち名前付き/匿名労働者へ位置付けられた量だけを保有へ加え、残りは
    共用分となる。消費は需要比で世帯保有から先に差し引き、不足分は共同体
    共用分が負担したものとして集計する。
    """
    # 所有内訳はupkeep適用前の物理在庫へ一度合わせてからgross flowを写す。
    # 適用後在庫へ先にreconcileすると、在庫減少分でholdingsを縮小したうえに
    # consumptionをもう一度差し引くことになり、物理消費は1回でも世帯claimだけが
    # 二重に減るため、適用後物理量への照合はflow反映後の1回だけにする。
    # 呼び出し境界では前月末にreconcile済みであり、goods_beforeはその物理総量
    # そのもの。照合用settlementsとstateをもう一組deepcopyせず、upgradeが返す
    # 1つの作業copyへgross flowを適用する。最終reconcileが適用後物理量を検証する。
    after = upgrade_household_goods_state(state)
    events = []
    sites = {
        str(site_id): row for site_id, row in spatial_state.get(
            "sites", {}).items()}

    for settlement_id, plan in sorted(upkeep_plans_by_community.items()):
        settlement_id = str(settlement_id)
        before_goods = _goods(goods_before_by_community.get(settlement_id))
        activity = activity_economy_state.get(
            "communities", {}).get(settlement_id, {}).get("activities", {})
        for good in HOUSEHOLD_GOODS:
            produced, consumed, unmet = _actual_upkeep_flows(
                before_goods[good], plan, good)
            raw_gross = max(0.0, float(
                plan.get("gross_production", {}).get(good, 0.0)))
            named_acquired: dict[str, float] = {}
            anonymous_acquired = 0.0
            located = 0.0
            if produced > 0.0 and raw_gross > 0.0:
                scale = min(1.0, produced / raw_gross)
                for allocation in activity.get(good, {}).get(
                        "site_allocations", ()):
                    output = max(0.0, float(
                        allocation.get("gross_output", 0.0))) * scale
                    worker_count = max(0, int(allocation.get("worker_count", 0)))
                    named_workers = max(0, int(allocation.get(
                        "named_worker_count", 0)))
                    anonymous_workers = max(0, int(allocation.get(
                        "anonymous_worker_count", 0)))
                    if worker_count <= 0:
                        continue
                    named_amount = round(
                        output * named_workers / worker_count, ROUND_DIGITS)
                    anonymous_amount = round(
                        output * anonymous_workers / worker_count,
                        ROUND_DIGITS)
                    site = sites.get(str(allocation.get("site_id")), {})
                    household_id = str(site.get("household_id") or "")
                    if named_amount > 0.0 and household_id in after["households"]:
                        row = after["households"][household_id]
                        row["holdings"][good] = round(
                            row["holdings"][good] + named_amount,
                            ROUND_DIGITS)
                        _add_total(row, "acquired_totals", good, named_amount)
                        named_acquired[household_id] = round(
                            named_acquired.get(household_id, 0.0)
                            + named_amount, ROUND_DIGITS)
                        located += named_amount
                    pool = after["anonymous_pools"].get(settlement_id)
                    if anonymous_amount > 0.0 and pool is not None:
                        pool["holdings"][good] = round(
                            pool["holdings"][good] + anonymous_amount,
                            ROUND_DIGITS)
                        _add_total(
                            pool, "acquired_totals", good, anonymous_amount)
                        anonymous_acquired = round(
                            anonymous_acquired + anonymous_amount,
                            ROUND_DIGITS)
                        located += anonymous_amount
            located = round(min(produced, located), ROUND_DIGITS)

            named_targets, anonymous_target = _flow_allocations(
                household_needs_state, settlement_id, good, consumed)
            named_from_holdings = 0.0
            for household_id, target in named_targets.items():
                row = after["households"].get(household_id)
                if row is None:
                    continue
                debit = round(min(row["holdings"][good], target), ROUND_DIGITS)
                row["holdings"][good] = round(
                    row["holdings"][good] - debit, ROUND_DIGITS)
                _add_total(row, "consumed_totals", good, debit)
                named_from_holdings += debit
            anonymous_from_holdings = 0.0
            pool = after["anonymous_pools"].get(settlement_id)
            if pool is not None:
                anonymous_from_holdings = round(min(
                    pool["holdings"][good], anonymous_target), ROUND_DIGITS)
                pool["holdings"][good] = round(
                    pool["holdings"][good] - anonymous_from_holdings,
                    ROUND_DIGITS)
                _add_total(
                    pool, "consumed_totals", good,
                    anonymous_from_holdings)
            from_holdings = round(
                named_from_holdings + anonymous_from_holdings,
                ROUND_DIGITS)
            events.append({
                "turn": int(turn),
                "kind": "household_goods_flow",
                "settlement_id": settlement_id,
                "good": good,
                "produced": produced,
                "producer_household_allocations": named_acquired,
                "anonymous_producer_allocation": anonymous_acquired,
                "production_to_common": round(
                    max(0.0, produced - located), ROUND_DIGITS),
                "consumed": consumed,
                "consumed_from_household_holdings": round(
                    named_from_holdings, ROUND_DIGITS),
                "consumed_from_anonymous_holdings": anonymous_from_holdings,
                "consumed_from_common": round(
                    max(0.0, consumed - from_holdings), ROUND_DIGITS),
                "unmet": unmet,
            })

    if not finalize:
        # 呼び出し側が同月の交易・移住を続けてから一度だけ確定する高速経路。
        # 中間stateは外部へ公開せず、最終reconcileを必ず行うことが契約である。
        after["updated_turn"] = int(turn)
        return {"state": after, "events": events}
    reconciled = reconcile_household_goods_state(
        after, settlements, registry, household_needs_state,
        organization_state, turn)
    events.extend(reconciled["events"])
    return {"state": reconciled["state"], "events": events}


def credit_household_action_goods(
        state: dict | None, settlements: dict, registry: dict,
        household_needs_state: dict, organization_state: dict | None,
        household_id: str, settlement_id: str, goods: dict,
        turn: int, *, reason: str) -> dict:
    """共同体総量へ既に加算済みの行動効果を、実行世帯の内訳へ帰属させる。"""
    base = reconcile_household_goods_state(
        state, settlements, registry, household_needs_state,
        organization_state, turn)["state"]
    after = copy.deepcopy(base)
    household_id = str(household_id)
    if household_id not in after["households"]:
        return {"state": after, "events": []}
    row = after["households"][household_id]
    credited = _zero_goods()
    for good in HOUSEHOLD_GOODS:
        amount = round(max(0.0, float(goods.get(good, 0.0))), ROUND_DIGITS)
        if amount <= 0.0:
            continue
        row["holdings"][good] = round(
            row["holdings"][good] + amount, ROUND_DIGITS)
        _add_total(row, "acquired_totals", good, amount)
        credited[good] = amount
    reconciled = reconcile_household_goods_state(
        after, settlements, registry, household_needs_state,
        organization_state, turn)
    events = []
    if any(credited.values()):
        events.append({
            "turn": int(turn),
            "kind": "household_goods_acquired",
            "settlement_id": str(settlement_id),
            "household_id": household_id,
            "reason": str(reason),
            "goods": credited,
        })
    events.extend(reconciled["events"])
    return {"state": reconciled["state"], "events": events}


def _move_physical_good(settlements: dict, source_id: str,
                        destination_id: str, good: str,
                        desired: float) -> float:
    if source_id == destination_id:
        return round(max(0.0, float(desired)), ROUND_DIGITS)
    source = settlements.get(source_id)
    destination = settlements.get(destination_id)
    if source is None or destination is None:
        return 0.0
    source_economy = source["local_economy"]
    destination_economy = destination["local_economy"]
    room = max(0.0, goods_capacity(
        destination_economy.get("provisioning_scale", 1.0))
        - float(destination_economy.get(good, 0.0)))
    amount = round(min(
        max(0.0, float(desired)),
        max(0.0, float(source_economy.get(good, 0.0))), room),
        ROUND_DIGITS)
    source_economy[good] = round(
        float(source_economy.get(good, 0.0)) - amount, ROUND_DIGITS)
    destination_economy[good] = round(
        float(destination_economy.get(good, 0.0)) + amount, ROUND_DIGITS)
    return amount


def _find_heir_household(registry: dict, closed_household_id: str,
                         settlement_id: str) -> str | None:
    residents = registry.get("residents", {})
    closed_members = {
        resident_id for resident_id, row in residents.items()
        if str(row.get("household_id")) == str(closed_household_id)}
    descendants = [
        row for row in residents.values()
        if row.get("alive", True)
        and set(row.get("parent_ids", ())) & closed_members]
    if descendants:
        descendants.sort(key=lambda row: (
            int(row.get("generation", 0)), row["id"]))
        return str(descendants[0]["household_id"])
    households = registry.get("households", {})
    closed = households.get(str(closed_household_id), {})
    family_name = closed.get("family_name")
    living_households = {
        str(row.get("household_id"))
        for row in residents.values() if row.get("alive", True)}
    candidates = sorted(
        household_id for household_id in living_households
        if household_id != str(closed_household_id)
        and households.get(household_id, {}).get("family_name") == family_name
        and str(households.get(household_id, {}).get(
            "settlement_id")) == str(settlement_id))
    return candidates[0] if candidates else None


def plan_household_goods_lifecycle(
        state: dict | None, settlements: dict, registry: dict,
        household_needs_state: dict, organization_state: dict | None,
        resident_events: list[dict], turn: int, *,
        reconcile_organization_state_fn=None) -> dict:
    """死亡・世帯分割・移住を保有財と共同体総量へ反映する。"""
    after = upgrade_household_goods_state(state)
    after_settlements = copy.deepcopy(settlements)
    events = []

    split_sources = {
        str(event["household_id"]): str(event["from_household_id"])
        for event in resident_events
        if event.get("kind") == "household_split"}

    for event in resident_events:
        if event.get("kind") != "residents_migrated":
            continue
        source_id = str(event["from_settlement"])
        destination_id = str(event["to_settlement"])
        moved_rows = list(event.get("residents", ()))
        by_household: dict[str, list[dict]] = {}
        for moved in moved_rows:
            by_household.setdefault(
                str(moved.get("household_id")), []).append(moved)
        for destination_household_id, members in sorted(by_household.items()):
            source_household_id = split_sources.get(
                destination_household_id, destination_household_id)
            source_row = after["households"].get(source_household_id)
            if source_row is None:
                continue
            if destination_household_id == source_household_id:
                destination_row = source_row
                share = 1.0
            else:
                destination_row = after["households"].setdefault(
                    destination_household_id,
                    _account_record(destination_household_id, destination_id))
                remaining = sum(
                    1 for row in registry.get("residents", {}).values()
                    if row.get("alive", True)
                    and str(row.get("household_id")) == source_household_id)
                share = len(members) / max(1, len(members) + remaining)
            moved_goods = _zero_goods()
            for good in PORTABLE_HOUSEHOLD_GOODS:
                desired = round(
                    source_row["holdings"][good] * share, ROUND_DIGITS)
                amount = _move_physical_good(
                    after_settlements, source_id, destination_id,
                    good, desired)
                if destination_row is source_row:
                    source_row["holdings"][good] = amount
                else:
                    source_row["holdings"][good] = round(
                        source_row["holdings"][good] - amount,
                        ROUND_DIGITS)
                    destination_row["holdings"][good] = round(
                        destination_row["holdings"][good] + amount,
                        ROUND_DIGITS)
                _add_total(
                    source_row, "migrated_sent_totals", good, amount)
                _add_total(
                    destination_row, "migrated_received_totals", good, amount)
                moved_goods[good] = amount
            if destination_row is source_row:
                # 住居は場所に固定される。全世帯移住でもclaimを持ち出さない。
                source_row["holdings"]["shelter"] = 0.0
            destination_row["settlement_id"] = destination_id
            events.append({
                "turn": int(turn),
                "kind": "household_goods_migrated",
                "from_settlement": source_id,
                "to_settlement": destination_id,
                "from_household_id": source_household_id,
                "household_id": destination_household_id,
                "goods": moved_goods,
                "shelter_carried": 0.0,
            })

        anonymous_migrants = max(
            0, int(event.get("migrants", 0)) - len(moved_rows))
        source_pool = after["anonymous_pools"].get(source_id)
        if anonymous_migrants and source_pool is not None:
            destination_pool = after["anonymous_pools"].setdefault(
                destination_id,
                _account_record(destination_id, destination_id, population=0))
            source_population = max(
                anonymous_migrants, int(source_pool.get("population", 0)))
            share = anonymous_migrants / max(1, source_population)
            moved_goods = _zero_goods()
            for good in PORTABLE_HOUSEHOLD_GOODS:
                desired = round(
                    source_pool["holdings"][good] * share, ROUND_DIGITS)
                amount = _move_physical_good(
                    after_settlements, source_id, destination_id,
                    good, desired)
                source_pool["holdings"][good] = round(
                    source_pool["holdings"][good] - amount,
                    ROUND_DIGITS)
                destination_pool["holdings"][good] = round(
                    destination_pool["holdings"][good] + amount,
                    ROUND_DIGITS)
                _add_total(source_pool, "migrated_sent_totals", good, amount)
                _add_total(
                    destination_pool, "migrated_received_totals", good, amount)
                moved_goods[good] = amount
            events.append({
                "turn": int(turn),
                "kind": "anonymous_goods_migrated",
                "from_settlement": source_id,
                "to_settlement": destination_id,
                "population": anonymous_migrants,
                "goods": moved_goods,
                "shelter_carried": 0.0,
            })

    for event in resident_events:
        if event.get("kind") != "household_closed":
            continue
        household_id = str(event["household_id"])
        source_row = after["households"].get(household_id)
        if source_row is None or not any(source_row["holdings"].values()):
            continue
        settlement_id = str(source_row.get("settlement_id") or
                            event.get("settlement_id"))
        heir_id = _find_heir_household(
            registry, household_id, settlement_id)
        inherited = _goods(source_row["holdings"])
        if heir_id is not None and heir_id in after["households"]:
            heir = after["households"][heir_id]
            # 同一共同体の相続は物理移動を伴わない。遠隔相続では携行財だけを
            # 実際に移し、住居は元共同体へ残す。
            if str(heir.get("settlement_id")) == settlement_id:
                for good in HOUSEHOLD_GOODS:
                    heir["holdings"][good] = round(
                        heir["holdings"][good] + inherited[good],
                        ROUND_DIGITS)
                    _add_total(
                        heir, "inherited_totals", good, inherited[good])
            else:
                destination_id = str(heir.get("settlement_id"))
                for good in PORTABLE_HOUSEHOLD_GOODS:
                    amount = _move_physical_good(
                        after_settlements, settlement_id, destination_id,
                        good, inherited[good])
                    heir["holdings"][good] = round(
                        heir["holdings"][good] + amount, ROUND_DIGITS)
                    _add_total(heir, "inherited_totals", good, amount)
                    inherited[good] = amount
                inherited["shelter"] = 0.0
            reason = "inherited"
        else:
            heir_id = None
            reason = "returned_to_common"
        source_row["holdings"] = _zero_goods()
        events.append({
            "turn": int(turn),
            "kind": "household_goods_inherited",
            "from_household_id": household_id,
            "household_id": heir_id,
            "settlement_id": settlement_id,
            "goods": inherited,
            "reason": reason,
        })

    adjusted_organization_state = (
        reconcile_organization_state_fn(
            organization_state, after_settlements)
        if reconcile_organization_state_fn is not None
        else organization_state)
    reconciled = reconcile_household_goods_state(
        after, after_settlements, registry, household_needs_state,
        adjusted_organization_state, turn)
    events.extend(reconciled["events"])
    return {
        "state": reconciled["state"],
        "settlements": after_settlements,
        "organization_state": adjusted_organization_state,
        "events": events,
    }


def household_goods_coverage(state: dict, household_needs_state: dict,
                             household_id: str) -> dict[str, float]:
    """世帯の現在保有を、その世帯の需要基準に対する0〜100%で返す。"""
    household_id = str(household_id)
    row = state.get("households", {}).get(household_id, {})
    settlement_id = str(row.get("settlement_id", ""))
    demand = household_needs_state.get("communities", {}).get(
        settlement_id, {}).get("household_demands", {}).get(
            household_id, {}).get("demand_quantity_by_good", {})
    holdings = _goods(row.get("holdings"))
    return {
        good: round(
            100.0 if float(demand.get(good, 0.0)) <= 0.0 else
            max(0.0, min(
                100.0, holdings[good] / float(demand[good]) * 100.0)),
            ROUND_DIGITS)
        for good in HOUSEHOLD_GOODS}


def verify_household_goods_state(
        state: dict, settlements: dict, registry: dict,
        household_needs_state: dict, organization_state: dict | None) -> bool:
    """共同体ごとに household+anonymous+organization+common=physical を検証。"""
    if int(state.get("version", 0)) != HOUSEHOLD_GOODS_VERSION:
        return False
    physical = _physical_goods(settlements)
    organization = _organization_claims_by_community(organization_state)
    locations = _active_household_locations(registry, household_needs_state)
    populations = _anonymous_populations(household_needs_state)
    households = state.get("households", {})
    anonymous_pools = state.get("anonymous_pools", {})
    common = state.get("common_pool_by_community", {})
    stored_organization = state.get(
        "organization_claims_by_community", {})
    if set(locations) != set(households):
        return False
    if any(str(households[key].get("settlement_id")) != settlement_id
           for key, settlement_id in locations.items()):
        return False
    if set(populations) != set(anonymous_pools):
        return False
    if set(common) != set(physical):
        return False
    if set(stored_organization) != set(physical):
        return False
    if any(any(row.values()) for key, row in organization.items()
           if key not in physical):
        return False
    if any(int(anonymous_pools[key].get("population", -1)) != value
           for key, value in populations.items()):
        return False
    for community_id, totals in physical.items():
        named = [
            row for household_id, row in state.get("households", {}).items()
            if household_id in locations
            and str(row.get("settlement_id")) == community_id]
        anonymous = anonymous_pools.get(community_id, {})
        community_common = common.get(community_id, {})
        org = organization.get(community_id, _zero_goods())
        for good in HOUSEHOLD_GOODS:
            if abs(float(stored_organization[community_id].get(
                    good, 0.0)) - org[good]) > EPSILON:
                return False
            parts = (
                sum(float(row.get("holdings", {}).get(good, 0.0))
                    for row in named)
                + float(anonymous.get("holdings", {}).get(good, 0.0))
                + float(community_common.get(good, 0.0))
                + float(org.get(good, 0.0)))
            if abs(parts - totals[good]) > 2e-5:
                return False
    aggregates = {
        "world_physical_goods": {
            good: sum(row[good] for row in physical.values())
            for good in HOUSEHOLD_GOODS},
        "world_household_holdings": {
            good: sum(float(row.get("holdings", {}).get(good, 0.0))
                      for row in households.values())
            for good in HOUSEHOLD_GOODS},
        "world_anonymous_holdings": {
            good: sum(float(row.get("holdings", {}).get(good, 0.0))
                      for row in anonymous_pools.values())
            for good in HOUSEHOLD_GOODS},
        "world_organization_claims": {
            good: sum(row.get(good, 0.0)
                      for row in stored_organization.values())
            for good in HOUSEHOLD_GOODS},
        "world_common_pool": {
            good: sum(row.get(good, 0.0) for row in common.values())
            for good in HOUSEHOLD_GOODS},
    }
    for field, expected in aggregates.items():
        actual = state.get(field, {})
        if any(abs(float(actual.get(good, 0.0)) - expected[good]) > 2e-5
               for good in HOUSEHOLD_GOODS):
            return False
    return True
