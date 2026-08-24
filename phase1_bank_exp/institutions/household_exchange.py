# -*- coding: utf-8 -*-
"""世帯の余剰と不足を、欲求の二重一致で即時交換する純粋ルール。

この層が動かすのは ``household_goods_state`` 内の所有claimだけであり、
共同体の物理在庫は増減させない。同じ共同体で、一方が余らせている財を他方が
必要とし、かつ逆向きにも同じ関係が成立するときだけ、基準需要で正規化した
同量の交換単位を同時に移す。後払い・信用残高・贈与はここへ混ぜない。

名前付き世帯は最大4096件、残りは共同体別匿名pool 1件として扱う。全組合せを
走査せず、財ペアごとの売り手列を決定的に突き合わせるため、処理量は人口では
なく保存中の世帯行数に比例する。入力は変更せず、乱数も使わない。
"""
from __future__ import annotations

import copy
import math

from institutions import barter
from institutions.household_goods import (
    HOUSEHOLD_GOODS,
    ROUND_DIGITS,
    upgrade_household_goods_state,
)


BARTER_CAPACITY_BY_STAGE = {
    barter.BARTER_STAGE_FUNCTIONING: 1.0,
    barter.BARTER_STAGE_THINNED: 0.50,
    barter.BARTER_STAGE_SUBSISTENCE_ONLY: 0.0,
    barter.BARTER_STAGE_SHORTAGE: 0.0,
}
HOUSEHOLD_BARTER_RESERVE_RATIO = 1.0
HOUSEHOLD_BARTER_TARGET_RATIO = 1.0
# 1世帯が1か月に交換へ回せる量。4財の平均月間需要を1.0とした正規化値。
HOUSEHOLD_BARTER_MONTHLY_COVERAGE = 0.50
# 既存の別世帯関係は、縮小市場で相手を探せる月間容量だけを支える。Stage 0の
# 上限は越えず、財・需要・欲求の二重一致を生成することもない。
RELATIONSHIP_BARTER_CAPACITY_BONUS = 0.50
HOUSEHOLD_BARTER_ROUTE_SAMPLE_LIMIT = 24
EPSILON = 1e-9


def barter_capacity(barter_stage: int) -> float:
    """物々交換Stageから世帯交換能力(0〜1)を返す。"""
    return float(BARTER_CAPACITY_BY_STAGE.get(int(barter_stage), 0.0))


def _zero_goods() -> dict[str, float]:
    return {good: 0.0 for good in HOUSEHOLD_GOODS}


def _references() -> dict[str, float]:
    return {
        good: max(EPSILON, float(barter.goods_reference(good)))
        for good in HOUSEHOLD_GOODS}


def _add_total(account: dict, field: str, good: str, amount: float) -> None:
    totals = account.setdefault(field, _zero_goods())
    totals[good] = round(
        max(0.0, float(totals.get(good, 0.0)) + float(amount)),
        ROUND_DIGITS)


def _community_claim_totals(state: dict, community_id: str) -> dict[str, float]:
    rows = [
        row for row in state.get("households", {}).values()
        if str(row.get("settlement_id")) == community_id]
    anonymous = state.get("anonymous_pools", {}).get(community_id, {})
    return {
        good: round(
            sum(float(row.get("holdings", {}).get(good, 0.0))
                for row in rows)
            + float(anonymous.get("holdings", {}).get(good, 0.0)),
            ROUND_DIGITS)
        for good in HOUSEHOLD_GOODS}


def _relationship_support(
        support_by_household: dict[str, float] | None,
        household_id: str) -> float:
    try:
        value = float((support_by_household or {}).get(household_id, 0.0))
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, value))


def _party_rows(state: dict, needs: dict, references: dict,
                capacity_factor: float,
                relationship_support_by_household: (
                    dict[str, float] | None) = None) -> dict[str, dict]:
    parties = {}
    for household_id, demand_row in sorted(
            needs.get("household_demands", {}).items()):
        household_id = str(household_id)
        account = state.get("households", {}).get(household_id)
        if account is None:
            continue
        demand = {
            good: max(0.0, float(demand_row.get(
                "demand_quantity_by_good", {}).get(good, 0.0)))
            for good in HOUSEHOLD_GOODS}
        demand_units = sum(
            demand[good] / references[good]
            for good in HOUSEHOLD_GOODS) / len(HOUSEHOLD_GOODS)
        relationship_support = _relationship_support(
            relationship_support_by_household, household_id)
        effective_capacity_factor = min(
            1.0,
            capacity_factor * (1.0 + RELATIONSHIP_BARTER_CAPACITY_BONUS
                               * relationship_support))
        base_capacity_units = (
            demand_units * HOUSEHOLD_BARTER_MONTHLY_COVERAGE
            * capacity_factor)
        capacity_units = (
            demand_units * HOUSEHOLD_BARTER_MONTHLY_COVERAGE
            * effective_capacity_factor)
        parties[f"h:{household_id}"] = {
            "party_id": f"h:{household_id}",
            "household_id": household_id,
            "anonymous": False,
            "account": account,
            "demand": demand,
            "base_capacity_units": base_capacity_units,
            "capacity_units": capacity_units,
            "relationship_support": relationship_support,
            "relationship_capacity_bonus_units": max(
                0.0, capacity_units - base_capacity_units),
            "used_units": 0.0,
        }
    anonymous = state.get("anonymous_pools", {}).get(
        str(needs.get("settlement_id")))
    if anonymous is not None and int(needs.get("anonymous_population", 0)) > 0:
        demand = {
            good: max(0.0, float(needs.get(
                "anonymous_demand_quantity_by_good", {}).get(good, 0.0)))
            for good in HOUSEHOLD_GOODS}
        demand_units = sum(
            demand[good] / references[good]
            for good in HOUSEHOLD_GOODS) / len(HOUSEHOLD_GOODS)
        community_id = str(needs.get("settlement_id"))
        parties[f"a:{community_id}"] = {
            "party_id": f"a:{community_id}",
            "household_id": None,
            "anonymous": True,
            "account": anonymous,
            "demand": demand,
            "capacity_units": (
                demand_units * HOUSEHOLD_BARTER_MONTHLY_COVERAGE
                * capacity_factor),
            "base_capacity_units": (
                demand_units * HOUSEHOLD_BARTER_MONTHLY_COVERAGE
                * capacity_factor),
            "relationship_support": 0.0,
            "relationship_capacity_bonus_units": 0.0,
            "used_units": 0.0,
        }
    return parties


def _remaining_capacity(party: dict) -> float:
    return max(
        0.0, float(party["capacity_units"])
        - float(party["used_units"]))


def _surplus_units(party: dict, good: str, references: dict) -> float:
    owned = max(0.0, float(party["account"]["holdings"].get(good, 0.0)))
    reserve = float(party["demand"].get(good, 0.0)) * (
        HOUSEHOLD_BARTER_RESERVE_RATIO)
    return max(0.0, owned - reserve) / references[good]


def _deficit_units(party: dict, good: str, references: dict) -> float:
    owned = max(0.0, float(party["account"]["holdings"].get(good, 0.0)))
    target = float(party["demand"].get(good, 0.0)) * (
        HOUSEHOLD_BARTER_TARGET_RATIO)
    return max(0.0, target - owned) / references[good]


def _ordered_good_pairs(turn: int) -> list[tuple[str, str]]:
    pairs = [
        (HOUSEHOLD_GOODS[left], HOUSEHOLD_GOODS[right])
        for left in range(len(HOUSEHOLD_GOODS))
        for right in range(left + 1, len(HOUSEHOLD_GOODS))]
    if not pairs:
        return []
    offset = int(turn) % len(pairs)
    return pairs[offset:] + pairs[:offset]


def _eligible_side(parties: dict, give_good: str, receive_good: str,
                   references: dict) -> list[dict]:
    rows = [
        party for party in parties.values()
        if _remaining_capacity(party) > EPSILON
        and _surplus_units(party, give_good, references) > EPSILON
        and _deficit_units(party, receive_good, references) > EPSILON]
    rows.sort(key=lambda party: (
        -_deficit_units(party, receive_good, references),
        party["party_id"]))
    return rows


def _record_swap(left: dict, right: dict,
                 left_good: str, right_good: str,
                 units: float, references: dict) -> dict | None:
    left_amount = round(units * references[left_good], ROUND_DIGITS)
    right_amount = round(units * references[right_good], ROUND_DIGITS)
    if left_amount <= 0.0 or right_amount <= 0.0:
        return None
    # 丸め後のどちらかが制約を越えないよう、実際に動かせる正規化量へ揃える。
    actual_units = min(
        left_amount / references[left_good],
        right_amount / references[right_good], units)
    left_amount = round(actual_units * references[left_good], ROUND_DIGITS)
    right_amount = round(actual_units * references[right_good], ROUND_DIGITS)
    if left_amount <= 0.0 or right_amount <= 0.0:
        return None

    left_holdings = left["account"]["holdings"]
    right_holdings = right["account"]["holdings"]
    left_holdings[left_good] = round(
        left_holdings[left_good] - left_amount, ROUND_DIGITS)
    right_holdings[left_good] = round(
        right_holdings[left_good] + left_amount, ROUND_DIGITS)
    right_holdings[right_good] = round(
        right_holdings[right_good] - right_amount, ROUND_DIGITS)
    left_holdings[right_good] = round(
        left_holdings[right_good] + right_amount, ROUND_DIGITS)
    if min(left_holdings[left_good], right_holdings[right_good]) < -1e-6:
        raise RuntimeError("household barter overdrew a holding")
    left_holdings[left_good] = max(0.0, left_holdings[left_good])
    right_holdings[right_good] = max(0.0, right_holdings[right_good])

    _add_total(left["account"], "barter_sent_totals", left_good, left_amount)
    _add_total(left["account"], "barter_received_totals", right_good, right_amount)
    _add_total(right["account"], "barter_sent_totals", right_good, right_amount)
    _add_total(right["account"], "barter_received_totals", left_good, left_amount)
    left["account"]["barter_exchange_count"] = int(
        left["account"].get("barter_exchange_count", 0)) + 1
    right["account"]["barter_exchange_count"] = int(
        right["account"].get("barter_exchange_count", 0)) + 1
    left["used_units"] += actual_units
    right["used_units"] += actual_units
    return {
        "left_household_id": left["household_id"],
        "right_household_id": right["household_id"],
        "left_anonymous": left["anonymous"],
        "right_anonymous": right["anonymous"],
        "left_gives_good": left_good,
        "left_gives_amount": left_amount,
        "right_gives_good": right_good,
        "right_gives_amount": right_amount,
        "normalized_units": round(actual_units, 12),
    }


def plan_household_barter_exchange(
        household_goods_state: dict | None, settlements: dict,
        household_needs_state: dict, turn: int, *, enabled: bool,
        relationship_support_by_household: (
            dict[str, float] | None) = None) -> dict:
    """同一共同体の1か月分の即時物々交換を計画する。

    ``enabled=False``、または共同体のbarter Stageが2以上なら交換しない。
    戻り値の``events``は共同体ごとのsummaryで、個別経路は先頭24件だけを
    ``routes_sample``へ保持する。交換計算自体は全保存世帯へ適用される。
    """
    after = upgrade_household_goods_state(household_goods_state)
    world_volume = _zero_goods()
    world_count = 0
    world_units = 0.0
    events = []
    relationship_routes = []
    if (not enabled
            or after.get("household_barter_applied_turn") == int(turn)):
        return {
            "state": after, "events": events, "exchange_count": 0,
            "volume_by_good": world_volume, "normalized_units": 0.0,
            "relationship_routes": relationship_routes}

    references = _references()
    for community_id, needs in sorted(household_needs_state.get(
            "communities", {}).items()):
        community_id = str(community_id)
        economy = settlements.get(community_id, {}).get("local_economy", {})
        stage = int(economy.get(
            "barter_stage", barter.BARTER_STAGE_FUNCTIONING))
        capacity_factor = barter_capacity(stage)
        if capacity_factor <= 0.0:
            continue
        before_totals = _community_claim_totals(after, community_id)
        parties = _party_rows(
            after, needs, references, capacity_factor,
            relationship_support_by_household)
        if len(parties) < 2:
            continue
        routes = []
        volume = _zero_goods()
        normalized_units = 0.0
        anonymous_exchange_count = 0
        named_participants = set()

        for left_good, right_good in _ordered_good_pairs(turn):
            left_rows = _eligible_side(
                parties, left_good, right_good, references)
            right_rows = _eligible_side(
                parties, right_good, left_good, references)
            left_index = right_index = 0
            while (left_index < len(left_rows)
                   and right_index < len(right_rows)):
                left = left_rows[left_index]
                right = right_rows[right_index]
                units = min(
                    _surplus_units(left, left_good, references),
                    _deficit_units(left, right_good, references),
                    _surplus_units(right, right_good, references),
                    _deficit_units(right, left_good, references),
                    _remaining_capacity(left),
                    _remaining_capacity(right),
                )
                if units <= EPSILON:
                    if (_remaining_capacity(left) <= EPSILON
                            or _surplus_units(
                                left, left_good, references) <= EPSILON
                            or _deficit_units(
                                left, right_good, references) <= EPSILON):
                        left_index += 1
                    if (_remaining_capacity(right) <= EPSILON
                            or _surplus_units(
                                right, right_good, references) <= EPSILON
                            or _deficit_units(
                                right, left_good, references) <= EPSILON):
                        right_index += 1
                    continue
                route = _record_swap(
                    left, right, left_good, right_good, units, references)
                if route is None:
                    left_index += 1
                    right_index += 1
                    continue
                routes.append(route)
                if (route["left_household_id"] is not None
                        and route["right_household_id"] is not None):
                    relationship_routes.append(route)
                normalized_units += route["normalized_units"]
                volume[left_good] = round(
                    volume[left_good] + route["left_gives_amount"],
                    ROUND_DIGITS)
                volume[right_good] = round(
                    volume[right_good] + route["right_gives_amount"],
                    ROUND_DIGITS)
                if left["anonymous"] or right["anonymous"]:
                    anonymous_exchange_count += 1
                if left["household_id"]:
                    named_participants.add(left["household_id"])
                if right["household_id"]:
                    named_participants.add(right["household_id"])

        if not routes:
            continue
        after_totals = _community_claim_totals(after, community_id)
        if any(abs(before_totals[good] - after_totals[good]) > 1e-6
               for good in HOUSEHOLD_GOODS):
            raise RuntimeError("household barter broke goods conservation")
        for good in HOUSEHOLD_GOODS:
            world_volume[good] = round(
                world_volume[good] + volume[good], ROUND_DIGITS)
        world_count += len(routes)
        world_units += normalized_units
        supported_participants = {
            household_id for household_id in named_participants
            if parties[f"h:{household_id}"][
                "relationship_capacity_bonus_units"] > EPSILON}
        relationship_capacity_bonus_units = sum(
            parties[f"h:{household_id}"][
                "relationship_capacity_bonus_units"]
            for household_id in supported_participants)
        events.append({
            "turn": int(turn),
            "kind": "household_barter_exchange_summary",
            "settlement_id": community_id,
            "barter_stage": stage,
            "capacity_factor": capacity_factor,
            "exchange_count": len(routes),
            "named_participant_count": len(named_participants),
            "relationship_supported_participant_count": len(
                supported_participants),
            "relationship_capacity_bonus_units": round(
                relationship_capacity_bonus_units, 12),
            "anonymous_exchange_count": anonymous_exchange_count,
            "normalized_units": round(normalized_units, 12),
            "volume_by_good": volume,
            "routes_sample": routes[:HOUSEHOLD_BARTER_ROUTE_SAMPLE_LIMIT],
        })

    totals = after.setdefault(
        "world_household_barter_volume_by_good", _zero_goods())
    for good in HOUSEHOLD_GOODS:
        totals[good] = round(
            float(totals.get(good, 0.0)) + world_volume[good],
            ROUND_DIGITS)
    after["world_household_barter_exchange_count"] = int(
        after.get("world_household_barter_exchange_count", 0)) + world_count
    after["household_barter_applied_turn"] = int(turn)
    after["updated_turn"] = int(turn)
    return {
        "state": after,
        "events": events,
        "exchange_count": world_count,
        "volume_by_good": world_volume,
        "normalized_units": round(world_units, 12),
        "relationship_routes": relationship_routes,
    }
