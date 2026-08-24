# -*- coding: utf-8 -*-
"""強い住民関係を通じ、世帯の余剰claimを不足世帯へ有限に移す純粋ルール。

相互扶助は物々交換と異なり、欲求の二重一致や反対給付を要求しない。ただし財を
生成する制度ではない。移すのは同一活動共同体にある名前付き世帯間の既存claim
だけで、提供世帯自身の需要を上回る余剰を、不足世帯の需要上限まで一方向に移す。

対象関係は前月までに ``resident_relationship_state`` へ保存された強い実在辺に
限る。同じ世帯ペアを複数の住民辺が結んでいても最強の1辺だけを使い、各世帯・
各辺に月間容量を置く。したがって処理量は人口や全世帯ペアではなく、最大16,384本
の保存関係辺に比例し、匿名人口を個体化しない。通常のplannerは入力を変更せず、
乱数も使わない。月次pipeline向けの明示的なowned-state入口だけは、既に呼出側が
所有する現行schemaの財状態をその場で更新し、巨大な台帳の重複copyを避ける。
"""
from __future__ import annotations

import math

from institutions import barter
from institutions.household_goods import (
    HOUSEHOLD_GOODS,
    HOUSEHOLD_GOODS_VERSION,
    ROUND_DIGITS,
    upgrade_household_goods_state,
)
from institutions.resident_relationships import (
    LEGACY_RESIDENT_RELATIONSHIP_VERSION,
    RELATIONSHIP_KINDS,
    RESIDENT_RELATIONSHIP_VERSION,
)


MUTUAL_AID_MIN_RELATIONSHIP_STRENGTH = 60.0
MUTUAL_AID_DONOR_RESERVE_RATIO = 1.10
MUTUAL_AID_RECIPIENT_TARGET_RATIO = 1.0
# 4財の平均月間需要を1.0としたとき、1世帯が1か月に贈与・受領できる上限。
MUTUAL_AID_MONTHLY_COVERAGE = 0.25
MUTUAL_AID_ROUTE_SAMPLE_LIMIT = 24
EPSILON = 1e-9


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


def _claim_totals_by_community(state: dict) -> dict[str, dict[str, float]]:
    totals: dict[str, dict[str, float]] = {}
    for row in state.get("households", {}).values():
        community_id = str(row.get("settlement_id"))
        community = totals.setdefault(community_id, _zero_goods())
        for good in HOUSEHOLD_GOODS:
            community[good] += float(
                row.get("holdings", {}).get(good, 0.0))
    return {
        community_id: {
            good: round(value, ROUND_DIGITS)
            for good, value in community.items()}
        for community_id, community in totals.items()}


def _party_rows(state: dict, household_needs_state: dict,
                references: dict[str, float]) -> dict[str, dict]:
    parties: dict[str, dict] = {}
    for community_id, community in sorted(
            household_needs_state.get("communities", {}).items()):
        community_id = str(community_id)
        for household_id, demand_row in sorted(
                community.get("household_demands", {}).items()):
            household_id = str(household_id)
            account = state.get("households", {}).get(household_id)
            if (account is None
                    or str(account.get("settlement_id")) != community_id):
                continue
            demand = {
                good: max(0.0, float(demand_row.get(
                    "demand_quantity_by_good", {}).get(good, 0.0)))
                for good in HOUSEHOLD_GOODS}
            demand_units = sum(
                demand[good] / references[good]
                for good in HOUSEHOLD_GOODS) / len(HOUSEHOLD_GOODS)
            parties[household_id] = {
                "household_id": household_id,
                "settlement_id": community_id,
                "account": account,
                "demand": demand,
                "demand_units": demand_units,
                "capacity_units": (
                    demand_units * MUTUAL_AID_MONTHLY_COVERAGE),
                "used_units": 0.0,
            }
    return parties


def _eligible_household_edges(
        relationship_state: dict | None, registry: dict,
        parties: dict[str, dict]) -> list[dict]:
    """同じ世帯ペアの多重辺を、最強・同強度ならID最小の1辺へ畳む。"""
    if relationship_state is None:
        relationships = {}
    else:
        if not isinstance(relationship_state, dict):
            raise TypeError("resident relationship state must be a dict")
        version = int(relationship_state.get("version", 0))
        if version not in (
                LEGACY_RESIDENT_RELATIONSHIP_VERSION,
                RESIDENT_RELATIONSHIP_VERSION):
            raise ValueError(
                f"unsupported resident relationship version: {version}")
        relationships = relationship_state.get("relationships", {})
    residents = registry.get("residents", {})
    selected: dict[tuple[str, str], dict] = {}
    for edge_id, edge in sorted(relationships.items()):
        try:
            strength = float(edge.get("strength", 0.0))
        except (TypeError, ValueError):
            continue
        if (not math.isfinite(strength)
                or strength < MUTUAL_AID_MIN_RELATIONSHIP_STRENGTH):
            continue
        left_id = str(edge.get("resident_a_id"))
        right_id = str(edge.get("resident_b_id"))
        left = residents.get(left_id)
        right = residents.get(right_id)
        if (left is None or right is None
                or not left.get("alive", True)
                or not right.get("alive", True)):
            continue
        left_household = str(left.get("household_id"))
        right_household = str(right.get("household_id"))
        if (left_household == right_household
                or left_household not in parties
                or right_household not in parties):
            continue
        community_id = str(left.get("settlement_id"))
        if (community_id != str(right.get("settlement_id"))
                or community_id != parties[left_household]["settlement_id"]
                or community_id != parties[right_household]["settlement_id"]):
            continue
        pair = tuple(sorted((left_household, right_household)))
        candidate = {
            "relationship_id": str(edge_id),
            "resident_by_household": {
                left_household: left_id,
                right_household: right_id,
            },
            "household_a_id": pair[0],
            "household_b_id": pair[1],
            "settlement_id": community_id,
            "strength": max(0.0, min(100.0, strength)),
            # v1を読む場合もfull upgraderと同じ意味へ正規化する。ただし援助は
            # 読取専用なので、最大16,384辺のdeepcopyは行わない。
            "kinds": [
                kind for kind in RELATIONSHIP_KINDS
                if kind in edge.get("kinds", ())],
        }
        previous = selected.get(pair)
        candidate_key = (
            -candidate["strength"], candidate["relationship_id"])
        previous_key = (
            (-previous["strength"], previous["relationship_id"])
            if previous is not None else None)
        if previous_key is None or candidate_key < previous_key:
            selected[pair] = candidate
    return sorted(selected.values(), key=lambda row: (
        row["settlement_id"], row["relationship_id"]))


def _remaining_capacity(party: dict) -> float:
    return max(0.0, float(party["capacity_units"])
               - float(party["used_units"]))


def _surplus_units(party: dict, good: str,
                   references: dict[str, float]) -> float:
    owned = max(0.0, float(party["account"].get(
        "holdings", {}).get(good, 0.0)))
    reserve = float(party["demand"].get(good, 0.0)) * (
        MUTUAL_AID_DONOR_RESERVE_RATIO)
    return max(0.0, owned - reserve) / references[good]


def _deficit_units(party: dict, good: str,
                   references: dict[str, float]) -> float:
    owned = max(0.0, float(party["account"].get(
        "holdings", {}).get(good, 0.0)))
    target = float(party["demand"].get(good, 0.0)) * (
        MUTUAL_AID_RECIPIENT_TARGET_RATIO)
    return max(0.0, target - owned) / references[good]


def _edge_capacity(edge: dict, parties: dict[str, dict]) -> float:
    smaller_demand = min(
        float(parties[edge["household_a_id"]]["demand_units"]),
        float(parties[edge["household_b_id"]]["demand_units"]),
    )
    return (smaller_demand * MUTUAL_AID_MONTHLY_COVERAGE
            * float(edge["strength"]) / 100.0)


def _ordered_goods(turn: int) -> list[str]:
    if not HOUSEHOLD_GOODS:
        return []
    offset = int(turn) % len(HOUSEHOLD_GOODS)
    return list(HOUSEHOLD_GOODS[offset:] + HOUSEHOLD_GOODS[:offset])


def _initial_route_metrics(
        parties: dict[str, dict],
        references: dict[str, float]) -> dict[str, dict[str, dict]]:
    """候補順位に使う月初の余剰・不足を、世帯×財ごとに一度だけ計算する。"""
    metrics = {}
    for household_id, party in parties.items():
        by_good = {}
        for good in HOUSEHOLD_GOODS:
            deficit = _deficit_units(party, good, references)
            demand_units = max(
                EPSILON,
                float(party["demand"].get(good, 0.0)) / references[good])
            by_good[good] = {
                "surplus": _surplus_units(party, good, references),
                "deficit": deficit,
                "shortfall_ratio": deficit / demand_units,
            }
        metrics[household_id] = by_good
    return metrics


def _route_candidates(
        edges: list[dict], parties: dict[str, dict],
        references: dict[str, float], turn: int,
        metrics: dict[str, dict[str, dict]]) -> list[dict]:
    candidates = []
    good_order = {
        good: index for index, good in enumerate(_ordered_goods(turn))}
    for edge in edges:
        edge_capacity_units = _edge_capacity(edge, parties)
        for donor_id, recipient_id in (
                (edge["household_a_id"], edge["household_b_id"]),
                (edge["household_b_id"], edge["household_a_id"])):
            for good in HOUSEHOLD_GOODS:
                donor_metric = metrics[donor_id][good]
                recipient_metric = metrics[recipient_id][good]
                if (donor_metric["surplus"] <= EPSILON
                        or recipient_metric["deficit"] <= EPSILON):
                    continue
                candidates.append({
                    "edge": edge,
                    "edge_capacity_units": edge_capacity_units,
                    "donor_id": donor_id,
                    "recipient_id": recipient_id,
                    "good": good,
                    "shortfall_ratio": recipient_metric["shortfall_ratio"],
                    "good_order": good_order[good],
                })
    candidates.sort(key=lambda row: (
        -row["shortfall_ratio"],
        -float(row["edge"]["strength"]),
        row["edge"]["relationship_id"],
        row["good_order"],
        row["donor_id"],
        row["recipient_id"],
    ))
    return candidates


def _record_transfer(candidate: dict, parties: dict[str, dict],
                     references: dict[str, float],
                     edge_used_units: dict[str, float]) -> dict | None:
    edge = candidate["edge"]
    donor = parties[candidate["donor_id"]]
    recipient = parties[candidate["recipient_id"]]
    good = candidate["good"]
    edge_id = edge["relationship_id"]
    units = min(
        _surplus_units(donor, good, references),
        _deficit_units(recipient, good, references),
        _remaining_capacity(donor),
        _remaining_capacity(recipient),
        max(0.0, float(candidate["edge_capacity_units"])
            - edge_used_units.get(edge_id, 0.0)),
    )
    if units <= EPSILON:
        return None
    rounding_scale = 10 ** ROUND_DIGITS
    # 最近傍丸めで容量・余剰を微小に越えないよう、実移動量は下方へ丸める。
    amount = round(
        math.floor(units * references[good] * rounding_scale + EPSILON)
        / rounding_scale,
        ROUND_DIGITS)
    if amount <= 0.0:
        return None
    actual_units = amount / references[good]

    donor_holdings = donor["account"]["holdings"]
    recipient_holdings = recipient["account"]["holdings"]
    donor_holdings[good] = round(
        float(donor_holdings.get(good, 0.0)) - amount, ROUND_DIGITS)
    recipient_holdings[good] = round(
        float(recipient_holdings.get(good, 0.0)) + amount, ROUND_DIGITS)
    if donor_holdings[good] < -1e-6:
        raise RuntimeError("household mutual aid overdrew a holding")
    donor_holdings[good] = max(0.0, donor_holdings[good])

    _add_total(
        donor["account"], "mutual_aid_given_totals", good, amount)
    _add_total(
        recipient["account"], "mutual_aid_received_totals", good, amount)
    donor["account"]["mutual_aid_transfer_count"] = int(
        donor["account"].get("mutual_aid_transfer_count", 0)) + 1
    recipient["account"]["mutual_aid_transfer_count"] = int(
        recipient["account"].get("mutual_aid_transfer_count", 0)) + 1
    donor["used_units"] += actual_units
    recipient["used_units"] += actual_units
    edge_used_units[edge_id] = (
        edge_used_units.get(edge_id, 0.0) + actual_units)

    return {
        "relationship_id": edge_id,
        "donor_resident_id": edge["resident_by_household"][
            donor["household_id"]],
        "recipient_resident_id": edge["resident_by_household"][
            recipient["household_id"]],
        "donor_household_id": donor["household_id"],
        "recipient_household_id": recipient["household_id"],
        "good": good,
        "amount": amount,
        "normalized_units": round(actual_units, 12),
        "relationship_strength": round(
            float(edge["strength"]), ROUND_DIGITS),
        "relationship_kinds": list(edge["kinds"]),
    }


def _apply_household_mutual_aid(
        after: dict, household_needs_state: dict, registry: dict,
        resident_relationship_state: dict | None, turn: int) -> dict:
    """現行schemaで呼出側所有の財状態へ相互扶助を適用する内部実装。"""
    world_volume = _zero_goods()
    world_count = 0
    world_units = 0.0
    events = []
    if after.get("household_mutual_aid_applied_turn") == int(turn):
        return {
            "state": after,
            "events": events,
            "transfer_count": 0,
            "volume_by_good": world_volume,
            "normalized_units": 0.0,
            "relationship_routes": [],
        }

    references = _references()
    parties = _party_rows(after, household_needs_state, references)
    edges = _eligible_household_edges(
        resident_relationship_state, registry, parties)
    route_metrics = (
        _initial_route_metrics(parties, references) if edges else {})
    edges_by_community: dict[str, list[dict]] = {}
    for edge in edges:
        edges_by_community.setdefault(edge["settlement_id"], []).append(edge)
    edge_used_units: dict[str, float] = {}
    relationship_routes: dict[str, dict] = {}
    before_totals = _claim_totals_by_community(after)

    for community_id, community_edges in sorted(edges_by_community.items()):
        volume = _zero_goods()
        routes_sample = []
        transfer_count = 0
        normalized_units = 0.0
        donor_ids = set()
        recipient_ids = set()
        community_relationship_ids = set()
        for candidate in _route_candidates(
                community_edges, parties, references, turn, route_metrics):
            route = _record_transfer(
                candidate, parties, references, edge_used_units)
            if route is None:
                continue
            transfer_count += 1
            normalized_units += route["normalized_units"]
            donor_ids.add(route["donor_household_id"])
            recipient_ids.add(route["recipient_household_id"])
            community_relationship_ids.add(route["relationship_id"])
            volume[route["good"]] = round(
                volume[route["good"]] + route["amount"], ROUND_DIGITS)
            relationship_routes.setdefault(route["relationship_id"], route)
            if len(routes_sample) < MUTUAL_AID_ROUTE_SAMPLE_LIMIT:
                routes_sample.append(route)
        if not transfer_count:
            continue
        for good in HOUSEHOLD_GOODS:
            world_volume[good] = round(
                world_volume[good] + volume[good], ROUND_DIGITS)
        world_count += transfer_count
        world_units += normalized_units
        events.append({
            "turn": int(turn),
            "kind": "household_mutual_aid_summary",
            "settlement_id": community_id,
            "transfer_count": transfer_count,
            "donor_household_count": len(donor_ids),
            "recipient_household_count": len(recipient_ids),
            "relationship_count": len(community_relationship_ids),
            "normalized_units": round(normalized_units, 12),
            "volume_by_good": volume,
            "routes_sample": routes_sample,
        })

    after_totals = _claim_totals_by_community(after)
    if set(before_totals) != set(after_totals) or any(
            abs(before_totals[community_id][good]
                - after_totals[community_id][good]) > 1e-6
            for community_id in before_totals
            for good in HOUSEHOLD_GOODS):
        raise RuntimeError("household mutual aid broke goods conservation")

    totals = after.setdefault(
        "world_household_mutual_aid_volume_by_good", _zero_goods())
    for good in HOUSEHOLD_GOODS:
        totals[good] = round(
            float(totals.get(good, 0.0)) + world_volume[good],
            ROUND_DIGITS)
    after["world_household_mutual_aid_transfer_count"] = int(
        after.get("world_household_mutual_aid_transfer_count", 0)
        ) + world_count
    after["household_mutual_aid_applied_turn"] = int(turn)
    after["updated_turn"] = int(turn)
    return {
        "state": after,
        "events": events,
        "transfer_count": world_count,
        "volume_by_good": world_volume,
        "normalized_units": round(world_units, 12),
        "relationship_routes": [
            relationship_routes[key] for key in sorted(relationship_routes)],
    }


def plan_household_mutual_aid(
        household_goods_state: dict | None, household_needs_state: dict,
        registry: dict, resident_relationship_state: dict | None,
        turn: int) -> dict:
    """入力を変更せず、前月までの強い関係から有限の援助を計画する。"""
    after = upgrade_household_goods_state(household_goods_state)
    return _apply_household_mutual_aid(
        after, household_needs_state, registry,
        resident_relationship_state, turn)


def apply_household_mutual_aid_to_owned_state(
        household_goods_state: dict, household_needs_state: dict,
        registry: dict, resident_relationship_state: dict | None,
        turn: int) -> dict:
    """呼出側から所有権を移した現行schemaの財状態へ援助を直接適用する。

    月次pipeline内で既にcopy・reconcile済みの台帳だけに使う入口である。通常の
    外部呼出では、入力不変を保証する ``plan_household_mutual_aid`` を使う。
    """
    if not isinstance(household_goods_state, dict):
        raise TypeError("owned household goods state must be a dict")
    version = int(household_goods_state.get("version", 0))
    if version != HOUSEHOLD_GOODS_VERSION:
        raise ValueError(
            f"owned household goods state must use current version: {version}")
    return _apply_household_mutual_aid(
        household_goods_state, household_needs_state, registry,
        resident_relationship_state, turn)
