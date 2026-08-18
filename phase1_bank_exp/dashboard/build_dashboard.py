# -*- coding: utf-8 -*-
"""可視化ダッシュボード(life_ledger.html)のビルドスクリプト。

`python game.py --visualize-run --seed <N> --policy <policy>` が書き出す
visualize_trace.json(phase1_bank_expの直下、既定パス)を読み、ダッシュボード
埋め込み用に軽量化してから template.html の "__DATA_JSON__" プレースホルダへ
差し込み、life_ledger.html として書き出す。

更新経路(2コマンド):
    python game.py --visualize-run --seed 1 --policy cautious
    python dashboard/build_dashboard.py

外部通信・CDN依存は一切無い(データはHTML内に埋め込み、チャートはcanvasへ
自前描画)。HTML上部では、同じturnトレースを月ごとに再生する観察モードも
提供する。docs/visualize-trace-design.md「F. ローカル配信への切り替え・
再現可能なビルド」参照。
"""
import argparse
import json
from pathlib import Path

try:
    from .event_density import (
        build_event_density_history, observer_events_for_population)
    from .particle_cohorts import build_particle_cohorts
    from .particle_packet import build_particle_packet
    from .spatial_history import build_spatial_history
except ImportError:  # `python dashboard/build_dashboard.py`での直接実行
    from event_density import (
        build_event_density_history, observer_events_for_population)
    from particle_cohorts import build_particle_cohorts
    from particle_packet import build_particle_packet
    from spatial_history import build_spatial_history

DASHBOARD_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = DASHBOARD_DIR.parent / "visualize_trace.json"
DEFAULT_TEMPLATE = DASHBOARD_DIR / "template.html"
DEFAULT_OUTPUT = DASHBOARD_DIR / "life_ledger.html"
DEFAULT_BIN_COUNT = 60
SUPPORTED_TRACE_SCHEMA_VERSION = 1
EMBEDDED_DATA_PREFIX = "const DATA = "
OBSERVER_STAGE_KEYS = (
    ("bank_stage", "bank"),
    ("currency_stage", "currency"),
    ("local_credit_stage", "local_credit"),
    ("enforcement_stage", "contract_enforcement"),
    ("barter_stage", "barter"),
    ("population_stage", "population"),
)
ACTIVITY_GOODS = ("food", "medicine", "shelter", "tools")
WORKFORCE_ORGANIZATION_KINDS = frozenset((
    "company", "guild", "family_workshop"))
HOUSEHOLD_GOODS = ("food", "medicine", "shelter", "tools")


def _household_goods_coverage(state: dict, needs_state: dict,
                              household_id: str) -> dict:
    """表示用の世帯保有/需要比。モデル側の物理量やstateは変更しない。"""
    account = state.get("households", {}).get(str(household_id), {})
    settlement_id = str(account.get("settlement_id", ""))
    demand = needs_state.get("communities", {}).get(
        settlement_id, {}).get("household_demands", {}).get(
            str(household_id), {}).get("demand_quantity_by_good", {})
    holdings = account.get("holdings", {})
    return {
        good: round3(
            100.0 if float(demand.get(good, 0.0)) <= 0.0 else
            max(0.0, min(100.0, float(holdings.get(good, 0.0))
                         / float(demand[good]) * 100.0)))
        for good in HOUSEHOLD_GOODS}


def activity_labor_summary(communities: list[dict]) -> dict:
    """活動経済の財別労働配賦を、総数を崩さず表示用に集約する。"""
    required = {good: 0 for good in ACTIVITY_GOODS}
    active = {good: 0 for good in ACTIVITY_GOODS}
    constrained = {good: 0 for good in ACTIVITY_GOODS}
    factors = {good: [] for good in ACTIVITY_GOODS}
    for row in communities:
        required_row = row.get("required_worker_count_by_good", {})
        active_row = row.get("active_worker_count_by_good", {})
        factor_row = row.get(
            "background_production_labor_factor_by_good", {})
        for good in ACTIVITY_GOODS:
            need = max(0, int(required_row.get(good, 0)))
            workers = max(0, int(active_row.get(good, 0)))
            required[good] += need
            active[good] += workers
            if workers < need:
                constrained[good] += 1
            if need:
                factors[good].append(float(factor_row.get(
                    good, workers / need)))
    return {
        "required": required,
        "active": active,
        "constrained_communities": constrained,
        "min_factor": {
            good: round3(min(factors[good]) if factors[good] else 0.0)
            for good in ACTIVITY_GOODS},
    }


def organization_labor_summary(rows: list[dict]) -> dict:
    """所属可能人口と当月実働を混ぜずに表示用へ集約する。"""
    workforce_rows = [
        row for row in rows
        if row.get("kind") in WORKFORCE_ORGANIZATION_KINDS]
    eligible = sum(max(0, int(row.get(
        "eligible_workforce_count",
        row.get("productive_member_count", 0)))) for row in workforce_rows)
    active = sum(max(0, int(row.get("workforce_count", 0)))
                 for row in workforce_rows)
    named = sum(len(row.get("named_worker_ids", ()))
                for row in workforce_rows)
    unrepresented = sum(max(0, int(row.get(
        "unrepresented_workforce_count",
        max(0, int(row.get("workforce_count", 0))
            - len(row.get("named_worker_ids", ()))))))
        for row in workforce_rows)
    return {
        "eligible": eligible,
        "active": active,
        "named": named,
        "unrepresented": unrepresented,
        "active_to_eligible_ratio": round3(
            active / eligible if eligible else 0.0),
    }


def round3(x):
    return round(x, 3) if isinstance(x, float) else x


def build_turns(trace_turns: list) -> list:
    """毎ターンのスナップショットを、チャート描画に必要な項目だけへ平坦化する
    (resources/traitsのネストを外し、数値は3桁へ丸める)。"""
    out = []
    for row in trace_turns:
        res, tr = row["resources"], row["traits"]
        flat = {
            "t": row["turn"],
            "energy": round3(res.get("energy", 0)), "money": round3(res.get("money", 0)),
            "peace": round3(res.get("peace", 0)), "real_money": round3(row["real_money"]),
            "dexterity": round3(tr.get("dexterity", 0)), "intellect": round3(tr.get("intellect", 0)),
            "skill": round3(tr.get("skill", 0)), "health": round3(tr.get("health", 0)),
            "bank_trust": round3(row["bank_trust"]), "bank_stage": row["bank_stage"],
            "currency_confidence": round3(row["currency_confidence"]),
            "currency_stage": row["currency_stage"],
            "community_trust": round3(row["community_trust"]),
            "local_credit_stage": row["local_credit_stage"],
            "enforcement_capacity": round3(row["enforcement_capacity"]),
            "enforcement_stage": row["enforcement_stage"],
            "production_capacity": round3(row["production_capacity"]),
            "barter_stage": row["barter_stage"],
            "food": round3(row["food"]), "medicine": round3(row["medicine"]),
            "shelter": round3(row["shelter"]), "tools": round3(row["tools"]),
        }
        if "population" in row:
            flat.update({
                "population": row["population"],
                "reproductive_population": row["reproductive_population"],
                "productive_population": row.get(
                    "productive_population", row["reproductive_population"]),
                "age_cohorts": dict(row.get("age_cohorts", {})),
                "population_stage": row["population_stage"],
                "total_population": row.get("total_population", row["population"]),
                "total_productive_population": row.get(
                    "total_productive_population",
                    row.get("productive_population",
                            row["reproductive_population"])),
                "settlement_id": row.get("settlement_id", "home"),
                "settlement_populations": dict(row.get(
                    "settlement_populations", {"home": row["population"]})),
                "settlement_reproductive_populations": dict(row.get(
                    "settlement_reproductive_populations",
                    {"home": row["reproductive_population"]})),
                "settlement_productive_populations": dict(row.get(
                    "settlement_productive_populations", {})),
                "settlement_age_cohorts": {
                    key: dict(value) for key, value in row.get(
                        "settlement_age_cohorts", {}).items()},
                "settlement_stages": dict(row.get(
                    "settlement_stages", {"home": row["population_stage"]})),
                "settlement_community_trusts": dict(row.get(
                    "settlement_community_trusts", {})),
                "settlement_local_credit_stages": dict(row.get(
                    "settlement_local_credit_stages", {})),
                "settlement_barter_stages": dict(row.get(
                    "settlement_barter_stages", {})),
                "settlement_worst_shortfalls": {
                    key: round3(value) for key, value in row.get(
                        "settlement_worst_shortfalls", {}).items()},
                "provisioning_scale": round3(row.get(
                    "provisioning_scale", 1.0)),
                "demand_scales_by_good": {
                    key: round3(value) for key, value in row.get(
                        "demand_scales_by_good", {}).items()},
                "goods_coverage_by_good": {
                    key: round3(value) for key, value in row.get(
                        "goods_coverage_by_good", {}).items()},
                "world_provisioning_scale": round3(row.get(
                    "world_provisioning_scale", 0.0)),
                "world_demand_scales_by_good": {
                    key: round3(value) for key, value in row.get(
                        "world_demand_scales_by_good", {}).items()},
                "world_demand_quantities_by_good": {
                    key: round3(value) for key, value in row.get(
                        "world_demand_quantities_by_good", {}).items()},
                "world_goods_totals": {
                    key: round3(value) for key, value in row.get(
                        "world_goods_totals", {}).items()},
                "world_goods_coverage_by_good": {
                    key: round3(value) for key, value in row.get(
                        "world_goods_coverage_by_good", {}).items()},
                "settlement_provisioning_scales": {
                    key: round3(value) for key, value in row.get(
                        "settlement_provisioning_scales", {}).items()},
                "settlement_demand_scales_by_good": {
                    settlement_id: {
                        key: round3(value)
                        for key, value in scales.items()}
                    for settlement_id, scales in row.get(
                        "settlement_demand_scales_by_good", {}).items()},
                "settlement_goods_coverage_by_good": {
                    settlement_id: {
                        key: round3(value)
                        for key, value in coverage.items()}
                    for settlement_id, coverage in row.get(
                        "settlement_goods_coverage_by_good", {}).items()},
                "settlement_trade_sent_totals": {
                    key: round3(value) for key, value in row.get(
                        "settlement_trade_sent_totals", {}).items()},
                "settlement_trade_received_totals": {
                    key: round3(value) for key, value in row.get(
                        "settlement_trade_received_totals", {}).items()},
                "production_practice_by_good": {
                    key: round3(value) for key, value in row.get(
                        "production_practice_by_good", {}).items()},
                "production_productivity_factors_by_good": {
                    key: round3(value) for key, value in row.get(
                        "production_productivity_factors_by_good", {}).items()},
                "world_production_practice_by_good": {
                    key: round3(value) for key, value in row.get(
                        "world_production_practice_by_good", {}).items()},
                "world_production_productivity_factors_by_good": {
                    key: round3(value) for key, value in row.get(
                        "world_production_productivity_factors_by_good", {}).items()},
                "settlement_production_practice_by_good": {
                    settlement_id: {
                        key: round3(value)
                        for key, value in practice.items()}
                    for settlement_id, practice in row.get(
                        "settlement_production_practice_by_good", {}).items()},
                "migration_total": row.get("migration_total", 0),
                "trade_volume_total": round3(row.get("trade_volume_total", 0.0)),
                "trade_event_count": row.get("trade_event_count", 0),
                "generation": row.get("generation", 1),
                "character_alive": row.get("character_alive", True),
                "character_age": round3(row.get("character_age")),
                "focus_resident_id": row.get("focus_resident_id"),
                "focus_resident_name": row.get("focus_resident_name"),
                "focus_resident_age": round3(row.get("focus_resident_age")),
                "focus_household_id": row.get("focus_household_id"),
                "focus_household_name": row.get("focus_household_name"),
                "focus_household_goods": {
                    key: round3(value) for key, value in row.get(
                        "focus_household_goods", {}).items()},
                "focus_household_goods_coverage_by_good": {
                    key: round3(value) for key, value in row.get(
                        "focus_household_goods_coverage_by_good", {}).items()},
                "world_household_goods_holdings": {
                    key: round3(value) for key, value in row.get(
                        "world_household_goods_holdings", {}).items()},
                "world_anonymous_goods_holdings": {
                    key: round3(value) for key, value in row.get(
                        "world_anonymous_goods_holdings", {}).items()},
                "world_common_goods_pool": {
                    key: round3(value) for key, value in row.get(
                        "world_common_goods_pool", {}).items()},
                "world_organization_goods_claims": {
                    key: round3(value) for key, value in row.get(
                        "world_organization_goods_claims", {}).items()},
                "living_resident_count": row.get(
                    "living_resident_count", row.get("total_population")),
                "active_household_count": row.get("active_household_count"),
                "focus_activity_community_id": row.get(
                    "focus_activity_community_id"),
                "activity_community_populations": dict(row.get(
                    "activity_community_populations", {})),
                "activity_community_reproductive_populations": dict(row.get(
                    "activity_community_reproductive_populations", {})),
                "activity_community_productive_populations": dict(row.get(
                    "activity_community_productive_populations", {})),
                "organization_count": row.get("organization_count", 0),
                "organization_kind_counts": dict(row.get(
                    "organization_kind_counts", {})),
                "organization_trust_stage_counts": dict(row.get(
                    "organization_trust_stage_counts", {})),
                "organization_operating_status_counts": dict(row.get(
                    "organization_operating_status_counts", {})),
                "organization_operating_reserve_total": round3(row.get(
                    "organization_operating_reserve_total", 0.0)),
                "organization_workforce_total": row.get(
                    "organization_workforce_total", 0),
                "organization_eligible_workforce_total": row.get(
                    "organization_eligible_workforce_total", 0),
                "organization_named_worker_total": row.get(
                    "organization_named_worker_total", 0),
                "organization_unrepresented_workforce_total": row.get(
                    "organization_unrepresented_workforce_total", 0),
                "organization_mean_member_continuity": round3(row.get(
                    "organization_mean_member_continuity", 1.0)),
                "organization_asset_claim_totals": {
                    key: round3(value) for key, value in row.get(
                        "organization_asset_claim_totals", {}).items()},
                "organization_asset_claim_acquisition_totals": {
                    key: round3(value) for key, value in row.get(
                        "organization_asset_claim_acquisition_totals", {}).items()},
                "organization_community_distribution_totals": {
                    key: round3(value) for key, value in row.get(
                        "organization_community_distribution_totals", {}).items()},
                "activity_community_stages": dict(row.get(
                    "activity_community_stages", {})),
                "activity_community_trusts": {
                    key: round3(value) for key, value in row.get(
                        "activity_community_trusts", {}).items()},
                "activity_community_local_credit_stages": dict(row.get(
                    "activity_community_local_credit_stages", {})),
                "activity_community_barter_stages": dict(row.get(
                    "activity_community_barter_stages", {})),
                "activity_community_worst_shortfalls": {
                    key: round3(value) for key, value in row.get(
                        "activity_community_worst_shortfalls", {}).items()},
                "activity_community_provisioning_scales": {
                    key: round3(value) for key, value in row.get(
                        "activity_community_provisioning_scales", {}).items()},
                "activity_community_demand_scales_by_good": {
                    community_id: {
                        key: round3(value)
                        for key, value in scales.items()}
                    for community_id, scales in row.get(
                        "activity_community_demand_scales_by_good",
                        {}).items()},
                "activity_community_goods_coverage_by_good": {
                    community_id: {
                        key: round3(value)
                        for key, value in coverage.items()}
                    for community_id, coverage in row.get(
                        "activity_community_goods_coverage_by_good",
                        {}).items()},
                "activity_community_trade_sent_totals": {
                    key: round3(value) for key, value in row.get(
                        "activity_community_trade_sent_totals", {}).items()},
                "activity_community_trade_received_totals": {
                    key: round3(value) for key, value in row.get(
                        "activity_community_trade_received_totals", {}).items()},
                "activity_community_production_practice_by_good": {
                    community_id: {
                        key: round3(value)
                        for key, value in practice.items()}
                    for community_id, practice in row.get(
                        "activity_community_production_practice_by_good",
                        {}).items()},
                "world_extinct": row.get("world_extinct", False),
            })
            if "activity_community_populations" not in row:
                # 空間共同体導入前のtraceへ空mapの長いキーを毎月追加しない。
                for key in (
                        "focus_activity_community_id",
                        "activity_community_populations",
                        "activity_community_reproductive_populations",
                        "activity_community_productive_populations",
                        "activity_community_stages",
                        "activity_community_trusts",
                        "activity_community_local_credit_stages",
                        "activity_community_barter_stages",
                        "activity_community_worst_shortfalls",
                        "activity_community_provisioning_scales",
                        "activity_community_goods_coverage_by_good",
                        "activity_community_trade_sent_totals",
                        "activity_community_trade_received_totals",
                        "activity_community_production_practice_by_good"):
                    flat.pop(key, None)
        out.append(flat)
    return out


def build_settlements(trace_settlements: list) -> list:
    return [{
        "t": e["turn"], "cp": e["counterparty"], "bank": e["is_bank_debt"],
        "key": e["choice_key"], "settle": e["settle"], "amt": e["repay_money"],
        "enf": e["enforcement_stage"],
        "settlement_id": e.get("settlement_id", "home"),
    } for e in trace_settlements]


def build_npcs(npcs: dict, settlements: list) -> list:
    """NPCごとの履行/不履行件数・総額は、settlementsトレースから実測して
    付与する(npcs辞書の最終trustだけでは経緯が分からないため)。"""
    stats = {}
    for s in settlements:
        if s["bank"]:
            continue
        row = stats.setdefault(s["cp"], {"fulfilled": 0, "defaulted": 0, "total_amt": 0})
        row[s["settle"]] += 1
        row["total_amt"] += abs(s["amt"])

    npc_introductions = {}  # name -> 初登場ターン(settlements/該当NPCから逆算しない、npcs側の情報が無いのでNoneのまま)
    out = []
    for name, n in npcs.items():
        if n.get("role") != "acquaintance":
            continue
        st = stats.get(name, {"fulfilled": 0, "defaulted": 0, "total_amt": 0})
        out.append({
            "name": name, "trust": round3(n["trust"]), "ethics": round3(n.get("ethics")),
            "introduced": npc_introductions.get(name), "retire_turn": n.get("retire_turn"),
            "birth_turn": n.get("birth_turn"), "death_turn": n.get("death_turn"),
            "died_turn": n.get("died_turn"), "alive": n.get("alive", True),
            "fulfilled": st["fulfilled"], "defaulted": st["defaulted"], "total_amt": st["total_amt"],
        })
    out.sort(key=lambda x: -(x["fulfilled"] + x["defaulted"]))
    return out


def attach_npc_introductions(npc_rows: list, trace_npc_introductions: list) -> None:
    by_name = {e["name"]: e["turn"] for e in trace_npc_introductions}
    for row in npc_rows:
        row["introduced"] = by_name.get(row["name"])


def build_residents(registry: dict, current_turn: int,
                    focus_resident_id: str | None,
                    household_goods_state: dict | None = None,
                    household_needs_state: dict | None = None) -> tuple[list, list]:
    """永続住民台帳を、現在の住民・世帯テーブル用に整形する。"""
    household_records = registry.get("households", {}) if registry else {}
    residents = []
    members_by_household = {}
    for resident_id, resident in (registry.get("residents", {}).items()
                                  if registry else ()):
        household = household_records.get(resident.get("household_id"), {})
        died_turn = resident.get("died_turn")
        age_turn = died_turn if died_turn is not None else current_turn
        row = {
            "id": resident_id,
            "name": resident.get("name", resident_id),
            "settlement_id": resident.get("settlement_id"),
            "household_id": resident.get("household_id"),
            "household_name": household.get("name", resident.get("household_id")),
            "birth_turn": resident.get("birth_turn"),
            "registered_turn": resident.get("registered_turn"),
            "age": round3(max(
                0, age_turn - int(resident.get("birth_turn", age_turn))) / 12),
            "alive": resident.get("alive", True),
            "died_turn": died_turn,
            "death_cause": resident.get("death_cause"),
            "origin": resident.get("origin"),
            "parent_ids": list(resident.get("parent_ids", ())),
            "generation": resident.get("generation", 0),
            "talent": resident.get("talent"),
            "focus": resident_id == focus_resident_id,
            "focus_count": resident.get("focus_count", 0),
            "activity_count": resident.get("activity_count", 0),
            "activity_counts_by_good": dict(resident.get(
                "activity_counts_by_good", {})),
            "unclassified_activity_count": resident.get(
                "unclassified_activity_count", 0),
            "last_activity_turn": resident.get("last_activity_turn"),
            "last_activity": resident.get("last_activity"),
        }
        residents.append(row)
        members_by_household.setdefault(row["household_id"], []).append(row)
    residents.sort(key=lambda row: (
        not row["focus"], not row["alive"], row["settlement_id"] or "",
        row["household_id"] or "", row["id"]))

    households = []
    goods_accounts = (household_goods_state or {}).get("households", {})
    needs_state = household_needs_state or {}
    for household_id, household in household_records.items():
        members = members_by_household.get(household_id, [])
        living = [row for row in members if row["alive"]]
        goods_account = goods_accounts.get(household_id, {})
        households.append({
            "id": household_id,
            "name": household.get("name", household_id),
            "settlement_id": household.get("settlement_id"),
            "founded_turn": household.get("founded_turn"),
            "closed_turn": household.get("closed_turn"),
            "active": bool(living),
            "living_members": len(living),
            "recorded_members": len(members),
            "member_names": [row["name"] for row in living[:6]],
            "livelihood": household.get("livelihood"),
            "activity_count": household.get("activity_count", 0),
            "activity_counts_by_good": dict(household.get(
                "activity_counts_by_good", {})),
            "unclassified_activity_count": household.get(
                "unclassified_activity_count", 0),
            "last_activity_turn": household.get("last_activity_turn"),
            "last_activity": household.get("last_activity"),
            "last_actor_id": household.get("last_actor_id"),
            "goods_holdings": {
                key: round3(value) for key, value in goods_account.get(
                    "holdings", {}).items()},
            "goods_coverage_by_good": {
                key: round3(value) for key, value in (
                    _household_goods_coverage(
                        household_goods_state or {}, needs_state,
                        household_id).items()
                    if goods_account else ())},
            "goods_acquired_totals": {
                key: round3(value) for key, value in goods_account.get(
                    "acquired_totals", {}).items()},
            "goods_consumed_totals": {
                key: round3(value) for key, value in goods_account.get(
                    "consumed_totals", {}).items()},
        })
    households.sort(key=lambda row: (
        not row["active"], row["settlement_id"] or "", row["id"]))
    return residents, households


def build_observer_events(turns: list, settlements: list, npcs: list,
                          death_turn: int | None,
                          character_events: list | None = None,
                          population_events: list | None = None,
                          npc_events: list | None = None,
                          trade_events: list | None = None,
                          resident_events: list | None = None,
                          spatial_events: list | None = None,
                          organization_events: list | None = None,
                          household_goods_events: list | None = None) -> list:
    """観察再生でその月までに起きたことだけを表示するための構造化イベント。

    トレースの最終状態をブラウザ側で逆算させず、Stage差分・NPC初登場・契約清算・
    死亡をturn付きの事実へ変換する。表示文言はtemplate.html側の責務とし、ここでは
    ユーザー由来になり得るNPC名を文字列のまま保持する(build_htmlで安全に埋め込む)。
    """
    events = []
    sequence = 0

    for previous, current in zip(turns, turns[1:]):
        for key, institution in OBSERVER_STAGE_KEYS:
            if key not in current or key not in previous:
                continue
            if current[key] == previous[key]:
                continue
            events.append({
                "t": current["t"], "kind": "institution_transition",
                "institution": institution, "from": previous[key], "to": current[key],
                "_sequence": sequence,
            })
            sequence += 1

    for npc in npcs:
        if npc["introduced"] is None:
            continue
        events.append({
            "t": npc["introduced"], "kind": "npc_introduced", "name": npc["name"],
            "_sequence": sequence,
        })
        sequence += 1

    for event in npc_events or ():
        events.append({
            "t": event["turn"], "kind": event["kind"],
            **{key: value for key, value in event.items()
               if key not in ("turn", "kind")},
            "_sequence": sequence,
        })
        sequence += 1

    for settlement in settlements:
        events.append({
            "t": settlement["t"], "kind": "contract_settled",
            "counterparty": settlement["cp"], "bank": settlement["bank"],
            "settle": settlement["settle"], "amount": settlement["amt"],
            "_sequence": sequence,
        })
        sequence += 1

    for event in character_events or ():
        events.append({
            "t": event["turn"], "kind": event["kind"],
            **{key: value for key, value in event.items()
               if key not in ("turn", "kind")},
            "_sequence": sequence,
        })
        sequence += 1

    # シミュレーションでは交易後の在庫で人口変化を決めるため、同じ月の表示も
    # trade→populationの順にする。
    for event in trade_events or ():
        events.append({
            "t": event["turn"], "kind": event["kind"],
            **{key: value for key, value in event.items()
               if key not in ("turn", "kind")},
            "_sequence": sequence,
        })
        sequence += 1

    for event in population_events or ():
        # 複数集落ではtop-levelのpopulation_stage差分だけから、各集落の遷移・
        # 局所絶滅・移住を復元できない。population_eventsをそのまま観察層へ渡す。
        events.append({
            "t": event["turn"], "kind": event["kind"],
            **{key: value for key, value in event.items()
               if key not in ("turn", "kind")},
            "_sequence": sequence,
        })
        sequence += 1

    for event in resident_events or ():
        events.append({
            "t": event["turn"], "kind": event["kind"],
            **{key: value for key, value in event.items()
               if key not in ("turn", "kind")},
            "_sequence": sequence,
        })
        sequence += 1

    for event in spatial_events or ():
        events.append({
            "t": event["turn"], "kind": event["kind"],
            **{key: value for key, value in event.items()
               if key not in ("turn", "kind")},
            "_sequence": sequence,
        })
        sequence += 1

    for event in organization_events or ():
        events.append({
            "t": event["turn"], "kind": event["kind"],
            **{key: value for key, value in event.items()
               if key not in ("turn", "kind")},
            "_sequence": sequence,
        })
        sequence += 1

    # 毎月・財別のhousehold_goods_flowは活動密度側ですでに観察できるため、
    # ここでは空間移動・行動取得・相続という離散的な出来事だけを流す。
    observable_goods_kinds = {
        "household_goods_migrated", "anonymous_goods_migrated",
        "household_goods_acquired", "household_goods_inherited",
        "household_goods_released",
    }
    for event in household_goods_events or ():
        if event.get("kind") not in observable_goods_kinds:
            continue
        events.append({
            "t": event["turn"], "kind": event["kind"],
            **{key: value for key, value in event.items()
               if key not in ("turn", "kind")},
            "_sequence": sequence,
        })
        sequence += 1

    if death_turn is not None and not character_events:
        events.append({
            "t": death_turn, "kind": "character_died", "_sequence": sequence,
        })

    events.sort(key=lambda row: (row["t"], row["_sequence"]))
    for row in events:
        row.pop("_sequence")
    return events


def build_choice_bins(agree_log: list, policy: str, max_turn: int, bin_count: int) -> tuple:
    """agree_logから実行方針の選択だけを取り出し、bin_count区間へ集計する。
    1920ターンをそのまま描画すると密すぎるための粗視化(既存の選択ロジック・
    agree_log自体は変更しない、表示専用の後処理)。"""
    if bin_count < 1:
        raise ValueError("bin_count must be at least 1")
    if max_turn < 1:
        return [], []
    bin_size = max(1, -(-max_turn // bin_count))
    bins = {}
    for row in agree_log:
        key = row["picks"].get(policy)
        if key is None:
            continue
        b = (row["turn"] - 1) // bin_size
        bucket = bins.setdefault(b, {})
        bucket[key] = bucket.get(key, 0) + 1

    choice_keys = sorted({k for bucket in bins.values() for k in bucket})
    choice_bins = []
    # 最後に選択が無かった期間もrun全体の横軸として残す。
    total_bins = -(-max_turn // bin_size)
    for b in range(total_bins):
        bucket = bins.get(b, {})
        total = sum(bucket.values())
        choice_bins.append({
            "t0": b * bin_size + 1, "t1": min((b + 1) * bin_size, max_turn), "total": total,
            **{k: bucket.get(k, 0) for k in choice_keys},
        })
    return choice_bins, choice_keys


def build_institution_trajectories(raw: dict) -> dict:
    out = {}
    for inst, tr in raw.items():
        out[inst] = {
            "stage_turns": {str(k): v for k, v in tr["stage_turns"].items()},
            "first_reached_turn": {str(k): v for k, v in tr["first_reached_turn"].items()},
            "transition_history": tr["transition_history"],
            "worst_stage": tr["worst_stage"], "final_stage": tr["final_stage"],
            "transition_count": tr["transition_count"], "recovery_count": tr["recovery_count"],
        }
    return out


def build_dashboard_data(trace_data: dict, bin_count: int = DEFAULT_BIN_COUNT) -> dict:
    """visualize_trace.json(run_visualize_trace()の出力)から、ダッシュボード
    埋め込み用の軽量なdictを組み立てる。数値を丸め、ネストを平坦化し、
    agree_logをbinへ集計する以外の意味のある変換は行わない(元データの
    再解釈はしない)。"""
    version = trace_data.get("schema_version")
    if version != SUPPORTED_TRACE_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported visualize trace schema: {version!r}; "
            "run `python game.py --visualize-run ...` again")
    turns = build_turns(trace_data["trace"]["turns"])
    settlements = build_settlements(trace_data["trace"]["settlements"])
    npcs = build_npcs(trace_data["npcs"], settlements)
    attach_npc_introductions(npcs, trace_data["trace"]["npc_introductions"])
    max_turn = max((row["t"] for row in turns), default=0)
    residents, households = build_residents(
        trace_data.get("resident_registry", {}), max_turn,
        trace_data.get("focus_resident_id"),
        trace_data.get("household_goods_state"),
        trace_data.get("household_needs_state"))
    spatial_state = trace_data.get("spatial_state", {})
    particle_frame = build_particle_packet(residents, spatial_state, max_turn)
    particle_cohorts = build_particle_cohorts(
        turns, residents, spatial_state, particle_frame,
        trace_data["trace"].get("resident_events"))
    spatial_history = build_spatial_history(
        trace_data["trace"].get("spatial_keyframes", []))
    dashboard_spatial_state = spatial_state
    if particle_frame is not None:
        # 最新月の個体座標はparticle_frameが正本。site/cluster/lineageは従来の
        # object形を保つが、同じ住民座標をJSON objectでも二重送信しない。
        dashboard_spatial_state = dict(spatial_state)
        dashboard_spatial_state["residents"] = {}
        dashboard_spatial_state["packed_resident_count"] = particle_frame["count"]
    activity_community_ledger = trace_data.get(
        "activity_community_ledger", {})
    activity_economy_state = trace_data.get("activity_economy_state", {
        "version": 1, "updated_turn": max_turn, "communities": {},
        "cumulative_output_by_good": {},
    })
    activity_economy_communities = list(
        activity_economy_state.get("communities", {}).values())
    activity_labor_by_good = activity_labor_summary(
        activity_economy_communities)
    activity_required_worker_total = sum(
        int(row.get("required_worker_count", 0))
        for row in activity_economy_communities)
    activity_unassigned_productive_total = sum(
        int(row.get("unassigned_productive_population", 0))
        for row in activity_economy_communities)
    activity_labor_constrained_community_count = sum(
        int(row.get("active_worker_count", 0))
        < int(row.get("required_worker_count", 0))
        for row in activity_economy_communities)
    activity_labor_factors = [
        float(row.get("background_production_labor_factor", 1.0))
        for row in activity_economy_communities
        if int(row.get("required_worker_count", 0)) > 0]
    observer_events = build_observer_events(
        turns, settlements, npcs, trace_data["death_turn"],
        trace_data["trace"].get("character_events"),
        trace_data["trace"].get("population_events"),
        trace_data["trace"].get("npc_events"),
        trace_data["trace"].get("trade_events"),
        trace_data["trace"].get("resident_events"),
        spatial_state.get("cluster_events"),
        trace_data["trace"].get("organization_events"),
        trace_data["trace"].get("household_goods_events"))
    resident_total = len(residents) + int(trace_data.get(
        "resident_registry", {}).get("archived_resident_count", 0))
    named_living_resident_count = sum(
        1 for resident in residents if resident["alive"])
    latest_population = int(
        turns[-1].get("total_population", named_living_resident_count)
        if turns else named_living_resident_count)
    latest_world_practice = dict(turns[-1].get(
        "world_production_practice_by_good", {}) if turns else {})
    latest_world_productivity = dict(turns[-1].get(
        "world_production_productivity_factors_by_good", {})
        if turns else {})
    latest_world_provisioning_scale = (
        turns[-1].get("world_provisioning_scale", 0.0) if turns else 0.0)
    latest_world_demand_scales = dict(
        turns[-1].get("world_demand_scales_by_good", {}) if turns else {})
    latest_world_demand_quantities = dict(
        turns[-1].get("world_demand_quantities_by_good", {})
        if turns else {})
    latest_world_goods_totals = dict(
        turns[-1].get("world_goods_totals", {}) if turns else {})
    latest_world_goods_coverage = dict(
        turns[-1].get("world_goods_coverage_by_good", {}) if turns else {})
    latest_world_household_holdings = dict(
        turns[-1].get("world_household_goods_holdings", {}) if turns else {})
    latest_world_anonymous_holdings = dict(
        turns[-1].get("world_anonymous_goods_holdings", {}) if turns else {})
    latest_world_common_pool = dict(
        turns[-1].get("world_common_goods_pool", {}) if turns else {})
    latest_world_organization_claims = dict(
        turns[-1].get("world_organization_goods_claims", {}) if turns else {})
    peak_population = max((int(row.get(
        "total_population", named_living_resident_count))
        for row in turns), default=latest_population)
    observer_events, observer_detail_mode = observer_events_for_population(
        observer_events, peak_population)
    event_density_history = build_event_density_history(
        trace_data["trace"].get("population_events"),
        trace_data["trace"].get("resident_events"))
    choice_bins, choice_keys = build_choice_bins(
        trace_data["agree_log"], trace_data["policy"], max_turn, bin_count)
    organization_state = trace_data.get("organization_state", {})
    active_organization_rows = [
        row for row in organization_state.get("organizations", {}).values()
        if row.get("active", True)]
    organization_labor = organization_labor_summary(
        active_organization_rows)
    organization_mean_member_continuity = (
        sum(float(row.get("member_continuity", 1.0))
            for row in active_organization_rows)
        / len(active_organization_rows)
        if active_organization_rows else 1.0)
    organization_asset_claim_totals = {
        field: round(sum(float(row.get(
            "asset_claims", {}).get(field, 0.0))
            for row in active_organization_rows), 6)
        for field in ("food", "medicine", "shelter", "tools")}

    return {
        "meta": {
            "schema_version": version,
            "policy": trace_data["policy"], "seed": trace_data["seed"],
            "talent": trace_data["talent"], "turns_requested": trace_data["turns"],
            "death_turn": trace_data["death_turn"],
            "npc_total": trace_data.get("npc_total", len(npcs)),
            "npc_death_count": trace_data.get("npc_death_count", 0),
            "settlement_total": len(settlements),
            "contract_total": trace_data.get("contract_total", len(trace_data["contracts"])),
            "history_start_turn": trace_data.get("history_start_turn", 1),
            "world_mode": trace_data.get("world_mode", False),
            "world_extinct": trace_data.get("world_extinct", False),
            "world_extinct_turn": trace_data.get("world_extinct_turn"),
            "character_death_count": trace_data.get(
                "character_death_count", 1 if trace_data["death_turn"] is not None else 0),
            "generation": trace_data.get("generation", 1),
            "focus_settlement_id": trace_data.get(
                "focus_settlement_id", "home"),
            "settlement_count": len(trace_data.get("settlement_states", {})),
            "migration_total": trace_data.get("migration_total", 0),
            "trade_volume_total": round3(trace_data.get(
                "trade_volume_total", 0.0)),
            "trade_event_count": trace_data.get("trade_event_count", 0),
            "resident_total": resident_total,
            "observer_detail_mode": observer_detail_mode,
            "event_density_version": event_density_history.get("version"),
            "living_resident_count": latest_population,
            "named_living_resident_count": named_living_resident_count,
            "anonymous_resident_count": max(
                0, latest_population - named_living_resident_count),
            "household_count": sum(
                1 for household in households if household["active"]),
            "focus_resident_id": trace_data.get("focus_resident_id"),
            "particle_packet_version": (
                particle_frame.get("version") if particle_frame else None),
            "particle_packet_count": (
                particle_frame.get("count") if particle_frame else 0),
            "particle_packet_raw_bytes": (
                particle_frame.get("raw_bytes") if particle_frame else 0),
            "anonymous_particle_count": particle_cohorts[
                "anonymous_particle_count"],
            "particle_population_total": particle_cohorts[
                "total_population"],
            "particle_cohort_version": particle_cohorts.get("version"),
            "particle_cohort_frame_count": len(
                particle_cohorts.get("frames", ())),
            "spatial_history_version": spatial_history.get("version"),
            "spatial_history_frame_count": spatial_history.get(
                "source_frame_count", 0),
            "spatial_state_version": spatial_state.get("version"),
            "activity_site_count": len([
                row for row in spatial_state.get("sites", {}).values()
                if row.get("active", True)]),
            "activity_cluster_count": len(
                spatial_state.get("clusters", {})),
            "activity_provisional_cluster_count": sum(
                1 for row in spatial_state.get("clusters", {}).values()
                if row.get("provisional", False)),
            "activity_accounting_cluster_count": sum(
                1 for row in spatial_state.get("clusters", {}).values()
                if not row.get("provisional", False)),
            "activity_cluster_lineage_count": len(
                spatial_state.get("cluster_lineages", {})),
            "activity_cluster_event_count": len(
                spatial_state.get("cluster_events", [])),
            "activity_community_count": len(
                activity_community_ledger.get("communities", {})),
            "activity_community_accounting_version": trace_data.get(
                "activity_community_accounting_version", 0),
            "activity_economy_version": activity_economy_state.get("version"),
            "activity_economy_updated_turn": activity_economy_state.get(
                "updated_turn"),
            "activity_worker_total": sum(
                int(activity.get("worker_count", 0))
                for community in activity_economy_state.get(
                    "communities", {}).values()
                for activity in community.get("activities", {}).values()),
            "activity_anonymous_worker_total": sum(
                int(activity.get("anonymous_worker_count", 0))
                for community in activity_economy_state.get(
                    "communities", {}).values()
                for activity in community.get("activities", {}).values()),
            "activity_required_worker_total": (
                activity_required_worker_total),
            "activity_unassigned_productive_total": (
                activity_unassigned_productive_total),
            "activity_labor_constrained_community_count": (
                activity_labor_constrained_community_count),
            "activity_min_labor_factor": round3(
                min(activity_labor_factors)
                if activity_labor_factors else 0.0),
            "activity_required_workers_by_good": (
                activity_labor_by_good["required"]),
            "activity_workers_by_good": activity_labor_by_good["active"],
            "activity_labor_constrained_communities_by_good": (
                activity_labor_by_good["constrained_communities"]),
            "activity_min_labor_factor_by_good": (
                activity_labor_by_good["min_factor"]),
            "world_production_practice_by_good": latest_world_practice,
            "world_production_productivity_factors_by_good": (
                latest_world_productivity),
            "world_provisioning_scale": round3(
                latest_world_provisioning_scale),
            "world_demand_scales_by_good": {
                key: round3(value) for key, value in
                latest_world_demand_scales.items()},
            "world_demand_quantities_by_good": {
                key: round3(value) for key, value in
                latest_world_demand_quantities.items()},
            "world_goods_totals": {
                key: round3(value) for key, value in
                latest_world_goods_totals.items()},
            "world_goods_coverage_by_good": {
                key: round3(value) for key, value in
                latest_world_goods_coverage.items()},
            "world_household_goods_holdings": {
                key: round3(value) for key, value in
                latest_world_household_holdings.items()},
            "world_anonymous_goods_holdings": {
                key: round3(value) for key, value in
                latest_world_anonymous_holdings.items()},
            "world_common_goods_pool": {
                key: round3(value) for key, value in
                latest_world_common_pool.items()},
            "world_organization_goods_claims": {
                key: round3(value) for key, value in
                latest_world_organization_claims.items()},
            "organization_count": len(active_organization_rows),
            "organization_eligible_workforce_total": (
                organization_labor["eligible"]),
            "organization_workforce_total": organization_labor["active"],
            "organization_named_worker_total": organization_labor["named"],
            "organization_unrepresented_workforce_total": (
                organization_labor["unrepresented"]),
            "organization_workforce_affiliation_ratio": (
                organization_labor["active_to_eligible_ratio"]),
            "organization_mean_member_continuity": round3(
                organization_mean_member_continuity),
            "organization_asset_claim_totals": {
                key: round3(value) for key, value in
                organization_asset_claim_totals.items()},
            "organization_asset_claim_acquisition_totals": {
                key: round3(value) for key, value in organization_state.get(
                    "asset_claim_acquisition_totals", {}).items()},
            "organization_community_distribution_totals": {
                key: round3(value) for key, value in organization_state.get(
                    "community_distribution_totals", {}).items()},
            "unassigned_activity_account_count": len(
                activity_community_ledger.get("unassigned_accounts", {})),
        },
        "final": {k: round3(v) for k, v in trace_data["final"].items() if not isinstance(v, dict)},
        "final_resources": {k: round3(v) for k, v in trace_data["final"]["resources"].items()},
        "final_traits": {k: round3(v) for k, v in trace_data["final"]["traits"].items()},
        "turns": turns,
        "settlements": settlements,
        "npcs": npcs,
        "residents": residents,
        "households": households,
        "spatial_state": dashboard_spatial_state,
        "particle_frame": particle_frame,
        "particle_cohorts": particle_cohorts,
        "activity_community_ledger": activity_community_ledger,
        "activity_economy_state": activity_economy_state,
        "organization_state": trace_data.get("organization_state", {
            "version": 1, "updated_turn": max_turn,
            "organizations": {}, "events": []}),
        # 新規buildは差分historyだけを送る。空の旧fieldは、packet/historyを
        # 持たない過去の自己完結HTMLをtemplateが読める互換境界として残す。
        "spatial_keyframes": [],
        "spatial_history": spatial_history,
        "observer_events": observer_events,
        "event_density_history": event_density_history,
        "institution_trajectories": build_institution_trajectories(trace_data["institution_trajectories"]),
        "settlement_states": trace_data.get("settlement_states", {}),
        "choice_bins": choice_bins,
        "choice_keys": choice_keys,
    }


def build_html(dashboard_data: dict, template_path: Path) -> str:
    template = template_path.read_text(encoding="utf-8")
    if "__DATA_JSON__" not in template:
        raise ValueError(f"{template_path} に __DATA_JSON__ プレースホルダが見つからない")
    payload = json.dumps(dashboard_data, ensure_ascii=False, separators=(",", ":"))
    # HTML parserがJSON文字列内の`</script>`を終了タグとして解釈しないようにする。
    # NPC名は現在合成値だが、将来LLM由来の名前を可視化しても安全な境界にしておく。
    payload = (payload.replace("&", "\\u0026").replace("<", "\\u003c")
               .replace(">", "\\u003e").replace("\u2028", "\\u2028")
               .replace("\u2029", "\\u2029"))
    return template.replace("__DATA_JSON__", payload)


def extract_dashboard_data(html_text: str) -> dict:
    """自分で生成した観察HTMLから埋め込みDATAだけを安全に復元する。

    数値checkpointを進めずに、新しいテンプレートへ表示だけ載せ替えるための
    migration境界。正規表現で巨大JSONを切らず、JSONDecoderが返す終端の直後に
    JavaScriptのセミコロンがあることまで確認する。
    """
    if not isinstance(html_text, str):
        raise TypeError("dashboard HTML must be text")
    start = html_text.find(EMBEDDED_DATA_PREFIX)
    if start < 0:
        raise ValueError("dashboard HTML has no embedded DATA")
    source = html_text[start + len(EMBEDDED_DATA_PREFIX):]
    try:
        data, end = json.JSONDecoder().raw_decode(source)
    except json.JSONDecodeError as exc:
        raise ValueError(f"dashboard HTML has invalid embedded DATA: {exc}") from exc
    if not source[end:].lstrip().startswith(";"):
        raise ValueError("dashboard embedded DATA is not terminated")
    if not isinstance(data, dict):
        raise ValueError("dashboard embedded DATA must be an object")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT,
                        help="run_visualize_trace()が書き出したJSON(既定: ../visualize_trace.json)")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE,
                        help="__DATA_JSON__プレースホルダ入りのHTMLテンプレート")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help="書き出すHTMLダッシュボードのパス")
    parser.add_argument("--bins", type=int, default=DEFAULT_BIN_COUNT,
                        help="選択タイムラインの区間数(既定60)")
    args = parser.parse_args()

    if args.bins < 1:
        parser.error("--bins must be at least 1")

    if not args.input.exists():
        raise SystemExit(
            f"{args.input} が見つかりません。先に "
            f"`python game.py --visualize-run --seed <N> --policy <policy>` を実行してください。")

    with open(args.input, encoding="utf-8") as f:
        trace_data = json.load(f)

    dashboard_data = build_dashboard_data(trace_data, args.bins)
    html = build_html(dashboard_data, args.template)
    args.output.write_text(html, encoding="utf-8")

    print(f"=== ダッシュボードを書き出しました: {args.output} "
          f"({args.output.stat().st_size:,} bytes) ===")
    print(f"    turns={len(dashboard_data['turns'])}件 "
          f"settlements={len(dashboard_data['settlements'])}件 "
          f"npcs={len(dashboard_data['npcs'])}人 "
          f"choice_bins={len(dashboard_data['choice_bins'])}区間")


if __name__ == "__main__":
    main()
