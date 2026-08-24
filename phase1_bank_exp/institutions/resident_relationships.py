# -*- coding: utf-8 -*-
"""名前付き住民の実接触を、boundedな関係グラフへ保存する純粋ルール。

関係は表示用に捏造せず、同一世帯、同一組織の当月参加、成立済みの世帯間
物々交換からだけ形成する。既存の強い辺を通った相互扶助は新しい辺を作らず、
その実在辺を強化する。最大4096人の名前付き標本だけを扱い、匿名人口を
個体化しない。非親族の次数と世界の辺数に上限を設けるため、処理量・checkpoint
サイズは世界人口ではなく名前付き標本数に比例する。

関係強度は次月の縮小した物々交換市場で相手探索容量を支え、十分に強い辺だけが
有限の相互扶助を可能にする。財そのものを生成せず、欲求の二重一致と世帯別交換
上限は ``household_exchange``、援助の保存則は ``household_mutual_aid`` が正本である。
"""
from __future__ import annotations

import copy
import math


RESIDENT_RELATIONSHIP_VERSION = 2
LEGACY_RESIDENT_RELATIONSHIP_VERSION = 1
MAX_RESIDENT_RELATIONSHIPS = 16_384
MAX_NON_KIN_RELATIONSHIPS_PER_RESIDENT = 8
RELATIONSHIP_EVENT_SAMPLE_LIMIT = 32

KIN_STRENGTH_FLOOR = 80.0
ORGANIZATION_INITIAL_STRENGTH = 25.0
BARTER_INITIAL_STRENGTH = 35.0
ORGANIZATION_STRENGTH_GAIN = 2.0
BARTER_STRENGTH_GAIN = 6.0
MUTUAL_AID_STRENGTH_GAIN = 4.0
PASSIVE_STRENGTH_DECAY = 0.5
RELATIONSHIP_FADE_THRESHOLD = 5.0
ROUND_DIGITS = 6

RELATIONSHIP_KIN = "kin"
RELATIONSHIP_ORGANIZATION = "organization"
RELATIONSHIP_BARTER = "barter"
RELATIONSHIP_MUTUAL_AID = "mutual_aid"
RELATIONSHIP_KINDS = (
    RELATIONSHIP_KIN,
    RELATIONSHIP_ORGANIZATION,
    RELATIONSHIP_BARTER,
    RELATIONSHIP_MUTUAL_AID,
)
WORKFORCE_ORGANIZATION_KINDS = frozenset((
    "company", "guild", "family_workshop"))


def _zero_kind_counts() -> dict[str, int]:
    return {kind: 0 for kind in RELATIONSHIP_KINDS}


def initial_resident_relationship_state(turn: int = 0) -> dict:
    return {
        "version": RESIDENT_RELATIONSHIP_VERSION,
        "updated_turn": int(turn),
        "relationships": {},
        "communities": {},
        "world_relationship_count": 0,
        "world_cross_community_relationship_count": 0,
        "world_relationships_formed_total": 0,
        "world_relationships_ended_total": 0,
        "world_interactions_by_kind": _zero_kind_counts(),
        "suppressed_relationship_count": 0,
    }


def upgrade_resident_relationship_state(state: dict | None) -> dict:
    if state is None:
        return initial_resident_relationship_state()
    if not isinstance(state, dict):
        raise TypeError("resident relationship state must be a dict")
    version = int(state.get("version", 0))
    if version not in (
            LEGACY_RESIDENT_RELATIONSHIP_VERSION,
            RESIDENT_RELATIONSHIP_VERSION):
        raise ValueError(
            f"unsupported resident relationship version: {version}")
    after = copy.deepcopy(state)
    after["version"] = RESIDENT_RELATIONSHIP_VERSION
    after.setdefault("updated_turn", 0)
    after.setdefault("relationships", {})
    after.setdefault("communities", {})
    after.setdefault("world_relationship_count", 0)
    after.setdefault("world_cross_community_relationship_count", 0)
    after.setdefault("world_relationships_formed_total", 0)
    after.setdefault("world_relationships_ended_total", 0)
    after["world_interactions_by_kind"] = {
        kind: max(0, int(after.get(
            "world_interactions_by_kind", {}).get(kind, 0)))
        for kind in RELATIONSHIP_KINDS}
    after.setdefault("suppressed_relationship_count", 0)
    for row in after["relationships"].values():
        row["kinds"] = [
            kind for kind in RELATIONSHIP_KINDS
            if kind in row.get("kinds", ())]
        row["interaction_counts_by_kind"] = {
            kind: max(0, int(row.get(
                "interaction_counts_by_kind", {}).get(kind, 0)))
            for kind in RELATIONSHIP_KINDS}
        row["interaction_count"] = sum(
            row["interaction_counts_by_kind"].values())
    for row in after["communities"].values():
        row["relationship_counts_by_kind"] = {
            kind: max(0, int(row.get(
                "relationship_counts_by_kind", {}).get(kind, 0)))
            for kind in RELATIONSHIP_KINDS}
    return after


def _pair(left_id: str, right_id: str) -> tuple[str, str]:
    left_id, right_id = str(left_id), str(right_id)
    if left_id == right_id:
        raise ValueError("a resident relationship requires two residents")
    return tuple(sorted((left_id, right_id)))


def relationship_id(left_id: str, right_id: str) -> str:
    left_id, right_id = _pair(left_id, right_id)
    return f"relationship:{left_id}|{right_id}"


def _living_residents(registry: dict) -> dict[str, dict]:
    return {
        str(resident_id): resident
        for resident_id, resident in registry.get("residents", {}).items()
        if resident.get("alive", True)
    }


def _members_by_household(living: dict[str, dict]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for resident_id, resident in living.items():
        grouped.setdefault(str(resident.get("household_id")), []).append(
            resident_id)
    return {
        household_id: sorted(member_ids)
        for household_id, member_ids in grouped.items()
    }


def _observe(observations: dict, left_id: str, right_id: str,
             kind: str, count: int = 1) -> None:
    if kind not in RELATIONSHIP_KINDS or str(left_id) == str(right_id):
        return
    pair = _pair(left_id, right_id)
    row = observations.setdefault(pair, _zero_kind_counts())
    row[kind] += max(0, int(count))


def _kin_observations(observations: dict, living: dict[str, dict],
                      existing: dict[str, dict]) -> None:
    # 同一世帯をcliqueにせず、ID順のchainで全員を連結する。世帯人数-1本なので
    # 大世帯でも辺数は線形。いったん形成された親族関係は世帯分割・移住後も残る。
    for member_ids in _members_by_household(living).values():
        for left_id, right_id in zip(member_ids, member_ids[1:]):
            _observe(observations, left_id, right_id, RELATIONSHIP_KIN)
    for row in existing.values():
        if (RELATIONSHIP_KIN in row.get("kinds", ())
                and str(row.get("resident_a_id")) in living
                and str(row.get("resident_b_id")) in living):
            _observe(
                observations, row["resident_a_id"], row["resident_b_id"],
                RELATIONSHIP_KIN, count=0)


def _organization_observations(observations: dict, living: dict[str, dict],
                               organization_state: dict | None) -> None:
    for organization in sorted(
            (organization_state or {}).get("organizations", {}).values(),
            key=lambda row: str(row.get("id", ""))):
        if not organization.get("active", True):
            continue
        member_ids = (
            organization.get("named_worker_ids", ())
            if organization.get("kind") in WORKFORCE_ORGANIZATION_KINDS
            else organization.get("named_member_ids", ()))
        member_ids = sorted({
            str(resident_id) for resident_id in member_ids
            if str(resident_id) in living})
        for left_id, right_id in zip(member_ids, member_ids[1:]):
            _observe(
                observations, left_id, right_id,
                RELATIONSHIP_ORGANIZATION)


def _barter_observations(observations: dict, living: dict[str, dict],
                         barter_routes: list[dict] | None,
                         turn: int) -> None:
    members = _members_by_household(living)
    for index, route in enumerate(barter_routes or ()):
        left_household = route.get("left_household_id")
        right_household = route.get("right_household_id")
        if left_household is None or right_household is None:
            continue
        left = members.get(str(left_household), ())
        right = members.get(str(right_household), ())
        if not left or not right:
            continue
        left_id = left[(int(turn) + index) % len(left)]
        right_id = right[(int(turn) * 3 + index) % len(right)]
        _observe(observations, left_id, right_id, RELATIONSHIP_BARTER)


def _mutual_aid_observations(
        observations: dict, living: dict[str, dict], existing: dict[str, dict],
        mutual_aid_routes: list[dict] | None) -> None:
    """援助は既存辺だけを月1回強化し、route入力から新しい辺を捏造しない。"""
    observed_edge_ids = set()
    for route in mutual_aid_routes or ():
        edge_id = str(route.get("relationship_id") or "")
        if not edge_id or edge_id in observed_edge_ids:
            continue
        edge = existing.get(edge_id)
        if edge is None:
            continue
        donor_id = str(route.get("donor_resident_id") or "")
        recipient_id = str(route.get("recipient_resident_id") or "")
        edge_left_id = str(edge.get("resident_a_id") or "")
        edge_right_id = str(edge.get("resident_b_id") or "")
        if (donor_id not in living or recipient_id not in living
                or donor_id == recipient_id
                or not edge_left_id or not edge_right_id
                or _pair(donor_id, recipient_id) != _pair(
                    edge_left_id, edge_right_id)):
            continue
        _observe(
            observations, donor_id, recipient_id,
            RELATIONSHIP_MUTUAL_AID)
        observed_edge_ids.add(edge_id)


def _initial_strength(kind_counts: dict[str, int]) -> float:
    strengths = []
    if int(kind_counts.get(RELATIONSHIP_KIN, 0)) > 0:
        strengths.append(KIN_STRENGTH_FLOOR)
    if kind_counts.get(RELATIONSHIP_ORGANIZATION, 0):
        strengths.append(ORGANIZATION_INITIAL_STRENGTH)
    if kind_counts.get(RELATIONSHIP_BARTER, 0):
        strengths.append(BARTER_INITIAL_STRENGTH)
    return max(strengths, default=RELATIONSHIP_FADE_THRESHOLD)


def _strength_gain(kind_counts: dict[str, int]) -> float:
    return (
        int(kind_counts.get(RELATIONSHIP_ORGANIZATION, 0))
        * ORGANIZATION_STRENGTH_GAIN
        + int(kind_counts.get(RELATIONSHIP_BARTER, 0))
        * BARTER_STRENGTH_GAIN
        + int(kind_counts.get(RELATIONSHIP_MUTUAL_AID, 0))
        * MUTUAL_AID_STRENGTH_GAIN)


def _edge_record(pair: tuple[str, str], kind_counts: dict[str, int],
                 living: dict[str, dict], turn: int) -> dict:
    left_id, right_id = pair
    kinds = [kind for kind in RELATIONSHIP_KINDS
             if kind_counts.get(kind, 0) > 0]
    settlement_id = (
        str(living[left_id].get("settlement_id"))
        if str(living[left_id].get("settlement_id"))
        == str(living[right_id].get("settlement_id")) else None)
    counts = {
        kind: int(kind_counts.get(kind, 0)) for kind in RELATIONSHIP_KINDS}
    return {
        "id": relationship_id(left_id, right_id),
        "resident_a_id": left_id,
        "resident_b_id": right_id,
        "formed_turn": int(turn),
        "updated_turn": int(turn),
        "last_interaction_turn": int(turn),
        "settlement_id": settlement_id,
        "strength": round(_initial_strength(kind_counts), ROUND_DIGITS),
        "kinds": kinds,
        "interaction_count": sum(counts.values()),
        "interaction_counts_by_kind": counts,
    }


def _update_edge(row: dict, kind_counts: dict[str, int],
                 living: dict[str, dict], turn: int) -> dict:
    after = copy.deepcopy(row)
    kinds = set(after.get("kinds", ()))
    kinds.update(kind for kind, count in kind_counts.items() if count > 0)
    counts = after.setdefault("interaction_counts_by_kind", _zero_kind_counts())
    interaction_increment = 0
    for kind in RELATIONSHIP_KINDS:
        increment = int(kind_counts.get(kind, 0))
        counts[kind] = int(counts.get(kind, 0)) + increment
        interaction_increment += increment
    after["interaction_count"] = int(
        after.get("interaction_count", 0)) + interaction_increment
    strength = float(after.get("strength", 0.0)) + _strength_gain(kind_counts)
    if RELATIONSHIP_KIN in kinds:
        strength = max(KIN_STRENGTH_FLOOR, strength)
    after["strength"] = round(min(100.0, strength), ROUND_DIGITS)
    after["kinds"] = [kind for kind in RELATIONSHIP_KINDS if kind in kinds]
    after["updated_turn"] = int(turn)
    if interaction_increment:
        after["last_interaction_turn"] = int(turn)
    left = living[str(after["resident_a_id"])]
    right = living[str(after["resident_b_id"])]
    after["settlement_id"] = (
        str(left.get("settlement_id"))
        if str(left.get("settlement_id"))
        == str(right.get("settlement_id")) else None)
    return after


def _community_summaries(relationships: dict[str, dict],
                         living: dict[str, dict]) -> tuple[dict, int]:
    communities: dict[str, dict] = {}
    cross_community = 0
    for row in relationships.values():
        left = living[str(row["resident_a_id"])]
        right = living[str(row["resident_b_id"])]
        left_settlement = str(left.get("settlement_id"))
        right_settlement = str(right.get("settlement_id"))
        if left_settlement != right_settlement:
            cross_community += 1
            continue
        summary = communities.setdefault(left_settlement, {
            "settlement_id": left_settlement,
            "relationship_count": 0,
            "cross_household_relationship_count": 0,
            "relationship_counts_by_kind": _zero_kind_counts(),
            "total_strength": 0.0,
        })
        summary["relationship_count"] += 1
        if str(left.get("household_id")) != str(right.get("household_id")):
            summary["cross_household_relationship_count"] += 1
        for kind in row.get("kinds", ()):
            if kind in RELATIONSHIP_KINDS:
                summary["relationship_counts_by_kind"][kind] += 1
        summary["total_strength"] += float(row.get("strength", 0.0))
    for summary in communities.values():
        count = summary["relationship_count"]
        summary["average_strength"] = round(
            summary.pop("total_strength") / count if count else 0.0,
            ROUND_DIGITS)
    return communities, cross_community


def verify_resident_relationship_state(state: dict, registry: dict) -> bool:
    if int(state.get("version", 0)) != RESIDENT_RELATIONSHIP_VERSION:
        return False
    living = _living_residents(registry)
    relationships = state.get("relationships", {})
    if len(relationships) > MAX_RESIDENT_RELATIONSHIPS:
        return False
    world_interactions = state.get("world_interactions_by_kind", {})
    if (set(world_interactions) != set(RELATIONSHIP_KINDS)
            or any(int(world_interactions[kind]) < 0
                   for kind in RELATIONSHIP_KINDS)):
        return False
    non_kin_degree: dict[str, int] = {}
    for edge_id, row in relationships.items():
        left_id = str(row.get("resident_a_id"))
        right_id = str(row.get("resident_b_id"))
        if (left_id not in living or right_id not in living
                or left_id >= right_id
                or edge_id != relationship_id(left_id, right_id)):
            return False
        strength = float(row.get("strength", -1.0))
        if not math.isfinite(strength) or not 0.0 <= strength <= 100.0:
            return False
        kinds = row.get("kinds", ())
        if not kinds or any(kind not in RELATIONSHIP_KINDS for kind in kinds):
            return False
        counts = row.get("interaction_counts_by_kind", {})
        if (set(counts) != set(RELATIONSHIP_KINDS)
                or any(int(counts[kind]) < 0 for kind in RELATIONSHIP_KINDS)
                or int(row.get("interaction_count", -1))
                != sum(int(counts[kind]) for kind in RELATIONSHIP_KINDS)
                or any(int(counts[kind]) > 0 and kind not in kinds
                       for kind in RELATIONSHIP_KINDS)):
            return False
        if RELATIONSHIP_KIN not in kinds:
            non_kin_degree[left_id] = non_kin_degree.get(left_id, 0) + 1
            non_kin_degree[right_id] = non_kin_degree.get(right_id, 0) + 1
    if any(degree > MAX_NON_KIN_RELATIONSHIPS_PER_RESIDENT
           for degree in non_kin_degree.values()):
        return False
    communities, cross_community = _community_summaries(
        relationships, living)
    return (
        state.get("communities", {}) == communities
        and int(state.get("world_relationship_count", -1))
        == len(relationships)
        and int(state.get("world_cross_community_relationship_count", -1))
        == cross_community)


def household_relationship_support(state: dict | None,
                                   registry: dict) -> dict[str, float]:
    """同一共同体の別世帯へ届く最強の既存関係を0〜1で返す。"""
    if state is None:
        return {}
    current = upgrade_resident_relationship_state(state)
    living = _living_residents(registry)
    support: dict[str, float] = {}
    for row in current.get("relationships", {}).values():
        left = living.get(str(row.get("resident_a_id")))
        right = living.get(str(row.get("resident_b_id")))
        if left is None or right is None:
            continue
        left_household = str(left.get("household_id"))
        right_household = str(right.get("household_id"))
        if (left_household == right_household
                or str(left.get("settlement_id"))
                != str(right.get("settlement_id"))):
            continue
        value = max(0.0, min(1.0, float(row.get("strength", 0.0)) / 100.0))
        support[left_household] = max(support.get(left_household, 0.0), value)
        support[right_household] = max(support.get(right_household, 0.0), value)
    return {key: round(value, ROUND_DIGITS)
            for key, value in support.items()}


def plan_resident_relationships(
        state: dict | None, registry: dict,
        organization_state: dict | None, barter_routes: list[dict] | None,
        turn: int, *, mutual_aid_routes: list[dict] | None = None) -> dict:
    """当月の実接触を適用し、次月の意思決定に使う関係台帳を返す。"""
    before = upgrade_resident_relationship_state(state)
    if int(before.get("updated_turn", -1)) == int(turn):
        return {"state": before, "events": []}
    living = _living_residents(registry)
    existing = before.get("relationships", {})
    observations: dict[tuple[str, str], dict[str, int]] = {}
    _kin_observations(observations, living, existing)
    _organization_observations(observations, living, organization_state)
    _barter_observations(observations, living, barter_routes, turn)
    _mutual_aid_observations(
        observations, living, existing, mutual_aid_routes)

    relationships: dict[str, dict] = {}
    ended = []
    reinforced = []
    for edge_id, row in sorted(existing.items()):
        left_id = str(row.get("resident_a_id"))
        right_id = str(row.get("resident_b_id"))
        if left_id not in living or right_id not in living:
            ended.append({"id": edge_id, "reason": "resident_died"})
            continue
        pair = _pair(left_id, right_id)
        kind_counts = observations.pop(pair, None)
        if kind_counts is None:
            after = copy.deepcopy(row)
            after["strength"] = round(
                max(0.0, float(after.get("strength", 0.0))
                    - PASSIVE_STRENGTH_DECAY), ROUND_DIGITS)
            after["updated_turn"] = int(turn)
            left, right = living[left_id], living[right_id]
            after["settlement_id"] = (
                str(left.get("settlement_id"))
                if str(left.get("settlement_id"))
                == str(right.get("settlement_id")) else None)
            if (RELATIONSHIP_KIN not in after.get("kinds", ())
                    and after["strength"] < RELATIONSHIP_FADE_THRESHOLD):
                ended.append({"id": edge_id, "reason": "faded"})
                continue
        else:
            after = _update_edge(row, kind_counts, living, turn)
            if sum(kind_counts.values()):
                reinforced.append(edge_id)
        relationships[edge_id] = after

    non_kin_degree: dict[str, int] = {}
    for row in relationships.values():
        if RELATIONSHIP_KIN in row.get("kinds", ()):
            continue
        for resident_id in (row["resident_a_id"], row["resident_b_id"]):
            non_kin_degree[str(resident_id)] = (
                non_kin_degree.get(str(resident_id), 0) + 1)

    formed = []
    suppressed = 0
    candidates = sorted(observations.items(), key=lambda item: (
        0 if item[1].get(RELATIONSHIP_KIN, 0) > 0
        else 1 if item[1].get(RELATIONSHIP_BARTER, 0) else 2,
        item[0]))
    for pair, kind_counts in candidates:
        left_id, right_id = pair
        kin = int(kind_counts.get(RELATIONSHIP_KIN, 0)) > 0
        if not kin and (
                non_kin_degree.get(left_id, 0)
                >= MAX_NON_KIN_RELATIONSHIPS_PER_RESIDENT
                or non_kin_degree.get(right_id, 0)
                >= MAX_NON_KIN_RELATIONSHIPS_PER_RESIDENT):
            suppressed += 1
            continue
        if len(relationships) >= MAX_RESIDENT_RELATIONSHIPS:
            # 非親族辺が世界上限を使い切っても、新たに実在した親族関係を
            # 見失わない。最弱・最古の非親族辺だけを退避し、総辺数は保つ。
            evictable = [
                row for row in relationships.values()
                if RELATIONSHIP_KIN not in row.get("kinds", ())]
            if not kin or not evictable:
                suppressed += 1
                continue
            evicted = min(evictable, key=lambda row: (
                float(row.get("strength", 0.0)),
                int(row.get("last_interaction_turn", -1)),
                str(row.get("id", "")),
            ))
            relationships.pop(str(evicted["id"]), None)
            for resident_id in (
                    evicted["resident_a_id"], evicted["resident_b_id"]):
                resident_id = str(resident_id)
                non_kin_degree[resident_id] = max(
                    0, non_kin_degree.get(resident_id, 0) - 1)
            ended.append({
                "id": evicted["id"], "reason": "capacity_for_kin"})
        row = _edge_record(pair, kind_counts, living, turn)
        relationships[row["id"]] = row
        formed.append(row["id"])
        if not kin:
            non_kin_degree[left_id] = non_kin_degree.get(left_id, 0) + 1
            non_kin_degree[right_id] = non_kin_degree.get(right_id, 0) + 1

    communities, cross_community = _community_summaries(
        relationships, living)
    interactions = dict(before.get(
        "world_interactions_by_kind", _zero_kind_counts()))
    # observationsは既存辺をpop済みなので、形成・強化後の累計はstate差から
    # 取りこぼさないよう、当月更新済み辺のlast_interactionを集計する。
    observed_counts = _zero_kind_counts()
    for edge_id in formed + reinforced:
        row = relationships.get(edge_id, {})
        if int(row.get("last_interaction_turn", -1)) != int(turn):
            continue
        before_counts = existing.get(edge_id, {}).get(
            "interaction_counts_by_kind", {})
        after_counts = row.get("interaction_counts_by_kind", {})
        for kind in RELATIONSHIP_KINDS:
            observed_counts[kind] += max(
                0, int(after_counts.get(kind, 0))
                - int(before_counts.get(kind, 0)))
    for kind in RELATIONSHIP_KINDS:
        interactions[kind] = int(interactions.get(kind, 0)) + observed_counts[kind]

    after = {
        "version": RESIDENT_RELATIONSHIP_VERSION,
        "updated_turn": int(turn),
        "relationships": relationships,
        "communities": communities,
        "world_relationship_count": len(relationships),
        "world_cross_community_relationship_count": cross_community,
        "world_relationships_formed_total": int(before.get(
            "world_relationships_formed_total", 0)) + len(formed),
        "world_relationships_ended_total": int(before.get(
            "world_relationships_ended_total", 0)) + len(ended),
        "world_interactions_by_kind": interactions,
        "suppressed_relationship_count": int(before.get(
            "suppressed_relationship_count", 0)) + suppressed,
    }
    if not verify_resident_relationship_state(after, registry):
        raise RuntimeError("resident relationship state verification failed")
    events = []
    if formed or reinforced or ended:
        events.append({
            "turn": int(turn),
            "kind": "resident_relationship_summary",
            "formed_count": len(formed),
            "reinforced_count": len(reinforced),
            "ended_count": len(ended),
            "active_relationship_count": len(relationships),
            "cross_community_relationship_count": cross_community,
            "interactions_by_kind": observed_counts,
            "formed_relationship_ids": formed[:RELATIONSHIP_EVENT_SAMPLE_LIMIT],
            "reinforced_relationship_ids": reinforced[
                :RELATIONSHIP_EVENT_SAMPLE_LIMIT],
            "ended_relationships": ended[:RELATIONSHIP_EVENT_SAMPLE_LIMIT],
            "suppressed_count": suppressed,
        })
    return {"state": after, "events": events}
