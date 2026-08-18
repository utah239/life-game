# -*- coding: utf-8 -*-
"""活動密度から生まれる組織インスタンスと、その限定的な実効果。

組織は集落の別名でも、集落を先に作る原因でもない。生産年齢人口・活動場所・
実際の生業が集まった結果として、同じ活動共同体に複数成立する。近代的な会社は
信用・契約執行・決済・銀行が一定水準で機能する場合だけ成立し、その条件が崩れ
れば同じID・構成員・活動場所の系譜を保ったままギルドや家族工房へ縮退する。

組織がなくても従来の世帯生産は成立する。組織はその上に生じる協調余剰だけを
翌月へ加え、形態・信用・生産年齢構成が崩れれば余剰も失われる。名前付き住民は
活動点を介して所属するが、所属可能な生産年齢人口と当月実働は混同しない。
実働人数は活動経済台帳が活動点へ保存した財別人数だけを正本とし、匿名層も人数の
まま保持する。人員と活動点の継続性は運営余力へ作用する。形成・効果計画はともに
決定論的で、入力を変更せず、ゲーム本体のRNGを消費しない。
"""
from __future__ import annotations

import copy
import math

from institutions.population import PRODUCTIVE_AGE_RANGE
from institutions.production_practice import (
    normalize_production_practice,
    practice_productivity_factor,
)
from institutions.residents import resident_age_years


ORGANIZATION_STATE_VERSION = 3
LEGACY_ORGANIZATION_STATE_VERSION = 1
WORKFORCE_ORGANIZATION_STATE_VERSION = 2

ORGANIZATION_KIND_COMPANY = "company"
ORGANIZATION_KIND_GUILD = "guild"
ORGANIZATION_KIND_ASSEMBLY = "assembly"
ORGANIZATION_KIND_CONGREGATION = "congregation"
ORGANIZATION_KIND_FAMILY_WORKSHOP = "family_workshop"

ORGANIZATION_TRUST_HEALTHY = 0
ORGANIZATION_TRUST_STRAINED = 1
ORGANIZATION_TRUST_FRAGILE = 2
ORGANIZATION_TRUST_BROKEN = 3
ORGANIZATION_TRUST_STAGE_NAMES = {
    0: "信用良好", 1: "信用低下", 2: "相互保証", 3: "組織信用喪失"}

ORGANIZATION_OPERATING_STABLE = "stable"
ORGANIZATION_OPERATING_STRAINED = "strained"
ORGANIZATION_OPERATING_DORMANT = "dormant"
ORGANIZATION_OPERATING_RESERVE_INITIAL = 6.0
ORGANIZATION_OPERATING_RESERVE_CAP = 12.0
ORGANIZATION_OPERATING_STRAINED_ENTER = 3.0
ORGANIZATION_OPERATING_STRAINED_EXIT = 4.0
ORGANIZATION_OPERATING_DORMANT_ENTER = 1.0
ORGANIZATION_OPERATING_DORMANT_EXIT = 2.0
ORGANIZATION_OPERATING_STRESS_COST = 0.005
ORGANIZATION_OPERATING_TURNOVER_COST = 0.01
ORGANIZATION_OPERATING_CONTINUITY_FLOOR = 0.5
ORGANIZATION_OPERATING_ACTIVITY_FACTORS = {
    ORGANIZATION_OPERATING_STABLE: 1.0,
    ORGANIZATION_OPERATING_STRAINED: 0.75,
    ORGANIZATION_OPERATING_DORMANT: 0.5,
}
ORGANIZATION_OPERATING_EFFECT_FACTORS = {
    ORGANIZATION_OPERATING_STABLE: 1.0,
    ORGANIZATION_OPERATING_STRAINED: 0.5,
    ORGANIZATION_OPERATING_DORMANT: 0.0,
}
ORGANIZATION_OPERATING_REPLENISHMENT = {
    ORGANIZATION_KIND_COMPANY: 0.09,
    ORGANIZATION_KIND_GUILD: 0.065,
    ORGANIZATION_KIND_FAMILY_WORKSHOP: 0.045,
    ORGANIZATION_KIND_ASSEMBLY: 0.06,
    ORGANIZATION_KIND_CONGREGATION: 0.05,
}
ORGANIZATION_OPERATING_UPKEEP = {
    ORGANIZATION_KIND_COMPANY: 0.035,
    ORGANIZATION_KIND_GUILD: 0.025,
    ORGANIZATION_KIND_FAMILY_WORKSHOP: 0.012,
    ORGANIZATION_KIND_ASSEMBLY: 0.02,
    ORGANIZATION_KIND_CONGREGATION: 0.015,
}
ORGANIZATION_OPERATING_CAPACITY_REFERENCE = {
    ORGANIZATION_KIND_COMPANY: 10.0,
    ORGANIZATION_KIND_GUILD: 6.0,
    ORGANIZATION_KIND_FAMILY_WORKSHOP: 3.0,
    ORGANIZATION_KIND_ASSEMBLY: 15.0,
    ORGANIZATION_KIND_CONGREGATION: 10.0,
}

TRUST_STAGE1_ENTER = 45.0
TRUST_STAGE1_EXIT = 52.0
TRUST_STAGE2_ENTER = 30.0
TRUST_STAGE2_EXIT = 38.0
TRUST_STAGE3_ENTER = 15.0
TRUST_STAGE3_EXIT = 22.0

COMPANY_MIN_PRODUCTIVE_POPULATION = 24
COMPANY_MIN_ACTIVITY_SITES = 4
COMPANY_MIN_SECTOR_SITES = 2
COMPANY_MIN_COMMUNITY_TRUST = 45.0
COMPANY_RETAIN_MIN_PRODUCTIVE_POPULATION = 18
COMPANY_RETAIN_MIN_ACTIVITY_SITES = 3
COMPANY_RETAIN_MIN_COMMUNITY_TRUST = 38.0
COMPANY_REFORM_STABILITY_TURNS = 36
GUILD_MIN_PRODUCTIVE_MEMBERS = 6
GUILD_RETAIN_MIN_PRODUCTIVE_MEMBERS = 4
ASSEMBLY_MIN_PRODUCTIVE_POPULATION = 8
ASSEMBLY_MIN_ACTIVITY_SITES = 3
ASSEMBLY_RETAIN_MIN_PRODUCTIVE_POPULATION = 6
ASSEMBLY_RETAIN_MIN_ACTIVITY_SITES = 2
CONGREGATION_HEALTH_TRIGGER = 55.0
CONGREGATION_HEALTH_EXIT = 62.0
CONGREGATION_RETAIN_MIN_PRODUCTIVE_POPULATION = 3
CONGREGATION_MIN_ACTIVITY_SITES = 2

# 世帯生産を正本としたまま、組織が生む協調余剰だけを足す初期較正値。
# trustそのものとtrust_stageの両方を掛けるため、信用喪失時にも組織の看板だけで
# 大きな効果が残ることはない。各月・各共同体の上限で重複組織も抑制する。
ORGANIZATION_TRUST_STAGE_FACTORS = {
    ORGANIZATION_TRUST_HEALTHY: 1.0,
    ORGANIZATION_TRUST_STRAINED: 0.75,
    ORGANIZATION_TRUST_FRAGILE: 0.4,
    ORGANIZATION_TRUST_BROKEN: 0.1,
}
ORGANIZATION_SECTOR_OUTPUT_RATES = {
    ORGANIZATION_KIND_COMPANY: 0.003,
    ORGANIZATION_KIND_GUILD: 0.00175,
    ORGANIZATION_KIND_FAMILY_WORKSHOP: 0.000625,
}
ASSEMBLY_TRUST_RATE = 0.00015
ASSEMBLY_PRODUCTION_CAPACITY_RATE = 0.000075
CONGREGATION_HEALTH_RATE = 0.0003
CONGREGATION_MEDICINE_RATE = 0.00025
ORGANIZATION_EFFECT_LIMITS = {
    "food_delta": 3.0,
    "medicine_delta": 3.0,
    "shelter_delta": 3.0,
    "tools_delta": 3.0,
    "production_capacity_delta": 0.12,
    "community_trust_delta": 0.25,
    "community_health_delta": 0.4,
}
ORGANIZATION_EFFECT_PLAN_VERSION = 1

# 組織の物的資産は共同体在庫とは別の財ではなく、その内側にある所有・
# 優先利用分(asset claim)として持つ。したがって世界の財総量へ加算しない。
# 協調余剰の一部だけをclaimへ留保し、残りは共同体へ分配済みと記録する。
ORGANIZATION_ASSET_CLAIM_FIELDS = (
    "food", "medicine", "shelter", "tools")
ORGANIZATION_ASSET_RETENTION_RATES = {
    ORGANIZATION_KIND_COMPANY: 0.25,
    ORGANIZATION_KIND_GUILD: 0.20,
    ORGANIZATION_KIND_FAMILY_WORKSHOP: 0.15,
    ORGANIZATION_KIND_ASSEMBLY: 0.0,
    ORGANIZATION_KIND_CONGREGATION: 0.20,
}
ORGANIZATION_ASSET_EFFECT_REFERENCE = {
    ORGANIZATION_KIND_COMPANY: 6.0,
    ORGANIZATION_KIND_GUILD: 4.0,
    ORGANIZATION_KIND_FAMILY_WORKSHOP: 2.0,
    ORGANIZATION_KIND_ASSEMBLY: 6.0,
    ORGANIZATION_KIND_CONGREGATION: 3.0,
}
ORGANIZATION_ASSET_EFFECT_BONUS_MAX = 0.15

ORGANIZATION_WORK_RELATIONSHIPS = {
    ORGANIZATION_KIND_COMPANY: "employee",
    ORGANIZATION_KIND_GUILD: "guild_member",
    ORGANIZATION_KIND_FAMILY_WORKSHOP: "household_worker",
    ORGANIZATION_KIND_ASSEMBLY: "civic_participant",
    ORGANIZATION_KIND_CONGREGATION: "mutual_aid_participant",
}
ORGANIZATION_WORKFORCE_KINDS = {
    ORGANIZATION_KIND_COMPANY,
    ORGANIZATION_KIND_GUILD,
    ORGANIZATION_KIND_FAMILY_WORKSHOP,
}


def initial_organization_state() -> dict:
    return {
        "version": ORGANIZATION_STATE_VERSION,
        "updated_turn": 0,
        "organizations": {},
        "events": [],
        "effect_totals": {
            field: 0.0 for field in ORGANIZATION_EFFECT_LIMITS},
        "effect_application_count": 0,
        "last_effect_turn": None,
        "last_effects": None,
        "asset_claim_acquisition_totals": {
            field: 0.0 for field in ORGANIZATION_ASSET_CLAIM_FIELDS},
        "community_distribution_totals": {
            field: 0.0 for field in ORGANIZATION_ASSET_CLAIM_FIELDS},
        "last_asset_allocation_turn": None,
        "last_asset_allocations": [],
    }


def upgrade_organization_state(state: dict) -> dict:
    """旧組織台帳を、所属可能人数と実働人数を分けた現行形式へ昇格する。

    v1の``workforce_count``は生産年齢構成員数と同義だったため、保存済み値を
    捨てずに初回だけ実働人数として引き継ぐ。次の月次計画で活動経済台帳から
    実測値へ置き換わる。入力は変更しない。
    """
    version = int(state.get("version", 0))
    if version not in (
            LEGACY_ORGANIZATION_STATE_VERSION,
            WORKFORCE_ORGANIZATION_STATE_VERSION,
            ORGANIZATION_STATE_VERSION):
        raise ValueError("unsupported organization state version")
    after = copy.deepcopy(state)
    if version == LEGACY_ORGANIZATION_STATE_VERSION:
        for row in after.get("organizations", {}).values():
            workforce_organization = row.get(
                "kind") in ORGANIZATION_WORKFORCE_KINDS
            eligible = (max(0, int(row.get(
                "productive_member_count", 0)))
                if workforce_organization else 0)
            workforce = (max(0, int(row.get("workforce_count", eligible)))
                         if workforce_organization else 0)
            named_workers = list(row.get("named_worker_ids", ()))
            row.setdefault("eligible_workforce_count", eligible)
            row["workforce_count"] = workforce
            row.setdefault("unrepresented_workforce_count", max(
                0, workforce - len(named_workers)))
            row.setdefault("workforce_affiliation_ratio", round(
                workforce / eligible, 6) if eligible else 0.0)
            row.setdefault("workforce_community_activity_share", None)
    if version < ORGANIZATION_STATE_VERSION:
        for row in after.get("organizations", {}).values():
            row.setdefault("production_practice_score", 0.0)
            row.setdefault("production_practice_factor", 1.0)
        after["version"] = ORGANIZATION_STATE_VERSION
    return after


def initial_organization_asset_claims() -> dict[str, float]:
    return {field: 0.0 for field in ORGANIZATION_ASSET_CLAIM_FIELDS}


def _asset_claims(row: dict | None) -> dict[str, float]:
    raw = (row or {}).get("asset_claims", {})
    return {
        field: round(max(0.0, float(raw.get(field, 0.0))), 6)
        for field in ORGANIZATION_ASSET_CLAIM_FIELDS}


def _allocate_float_proportionally(
        total: float, weights: dict[str, float]) -> dict[str, float]:
    """丸め後も合計を保存する決定論的な比例配賦。"""
    total = round(max(0.0, float(total)), 6)
    keys = sorted(key for key, value in weights.items()
                  if float(value) > 0.0)
    if not keys or total <= 0.0:
        return {}
    denominator = sum(float(weights[key]) for key in keys)
    allocations = {}
    used = 0.0
    for key in keys[:-1]:
        value = round(total * float(weights[key]) / denominator, 6)
        allocations[key] = value
        used = round(used + value, 6)
    allocations[keys[-1]] = round(max(0.0, total - used), 6)
    return allocations


def organization_asset_factor(row: dict) -> float:
    """共同体在庫内の組織所有分が協調余剰を支える限定倍率。"""
    claims = _asset_claims(row)
    reference = max(0.000001, float(
        ORGANIZATION_ASSET_EFFECT_REFERENCE.get(row.get("kind"), 6.0)))
    coverage = min(1.0, sum(claims.values()) / reference)
    return round(1.0 + ORGANIZATION_ASSET_EFFECT_BONUS_MAX * coverage, 6)


def organization_trust_stage_next(current_stage: int, trust: float) -> int:
    """組織信用の可逆な4段階。回復は1段階ずつ。"""
    trust = max(0.0, min(100.0, float(trust)))
    if trust < TRUST_STAGE3_ENTER:
        return ORGANIZATION_TRUST_BROKEN
    if trust < TRUST_STAGE2_ENTER and current_stage < ORGANIZATION_TRUST_FRAGILE:
        return ORGANIZATION_TRUST_FRAGILE
    if trust < TRUST_STAGE1_ENTER and current_stage < ORGANIZATION_TRUST_STRAINED:
        return ORGANIZATION_TRUST_STRAINED
    if current_stage == ORGANIZATION_TRUST_BROKEN:
        return (ORGANIZATION_TRUST_FRAGILE
                if trust >= TRUST_STAGE3_EXIT else current_stage)
    if current_stage == ORGANIZATION_TRUST_FRAGILE:
        return (ORGANIZATION_TRUST_STRAINED
                if trust >= TRUST_STAGE2_EXIT else current_stage)
    if current_stage == ORGANIZATION_TRUST_STRAINED:
        return (ORGANIZATION_TRUST_HEALTHY
                if trust >= TRUST_STAGE1_EXIT else current_stage)
    return current_stage


def organization_size(productive_members: int) -> str:
    productive_members = max(0, int(productive_members))
    if productive_members < 5:
        return "micro"
    if productive_members < 20:
        return "small"
    if productive_members < 100:
        return "medium"
    return "large"


def _organization_effectiveness(row: dict) -> float:
    trust = max(0.0, min(100.0, float(row.get("trust", 0.0)))) / 100.0
    stage_factor = ORGANIZATION_TRUST_STAGE_FACTORS.get(
        int(row.get("trust_stage", ORGANIZATION_TRUST_BROKEN)), 0.0)
    return trust * stage_factor


def organization_operating_status_next(current_status: str,
                                       reserve: float) -> str:
    """運営余力の可逆な3段階。回復は隣接段階へ1段ずつ進む。"""
    reserve = max(0.0, min(
        ORGANIZATION_OPERATING_RESERVE_CAP, float(reserve)))
    if current_status == ORGANIZATION_OPERATING_DORMANT:
        return (ORGANIZATION_OPERATING_STRAINED
                if reserve >= ORGANIZATION_OPERATING_DORMANT_EXIT
                else current_status)
    if current_status == ORGANIZATION_OPERATING_STRAINED:
        if reserve < ORGANIZATION_OPERATING_DORMANT_ENTER:
            return ORGANIZATION_OPERATING_DORMANT
        return (ORGANIZATION_OPERATING_STABLE
                if reserve >= ORGANIZATION_OPERATING_STRAINED_EXIT
                else current_status)
    if reserve < ORGANIZATION_OPERATING_DORMANT_ENTER:
        return ORGANIZATION_OPERATING_DORMANT
    if reserve < ORGANIZATION_OPERATING_STRAINED_ENTER:
        return ORGANIZATION_OPERATING_STRAINED
    return ORGANIZATION_OPERATING_STABLE


def plan_organization_operation(previous: dict | None, kind: str,
                                capacity: float, trust: float,
                                trust_stage: int,
                                worst_shortfall: float,
                                member_continuity: float = 1.0) -> dict:
    """契約関係・手順・物流網の運営余力を1か月進める純粋なstock-flow。"""
    if previous is None:
        return {
            "operating_reserve_before": ORGANIZATION_OPERATING_RESERVE_INITIAL,
            "operating_reserve_delta": 0.0,
            "operating_reserve": ORGANIZATION_OPERATING_RESERVE_INITIAL,
            "operating_status_before": ORGANIZATION_OPERATING_STABLE,
            "operating_status": ORGANIZATION_OPERATING_STABLE,
            "operating_transitioned": False,
            "member_continuity": 1.0,
            "turnover_cost": 0.0,
        }
    reserve_before = max(0.0, min(
        ORGANIZATION_OPERATING_RESERVE_CAP,
        float(previous.get(
            "operating_reserve", ORGANIZATION_OPERATING_RESERVE_INITIAL))))
    status_before = str(previous.get(
        "operating_status", ORGANIZATION_OPERATING_STABLE))
    activity_factor = ORGANIZATION_OPERATING_ACTIVITY_FACTORS.get(
        status_before, 1.0)
    effectiveness = _organization_effectiveness({
        "trust": trust, "trust_stage": trust_stage})
    capacity_reference = ORGANIZATION_OPERATING_CAPACITY_REFERENCE[kind]
    capacity_factor = max(0.0, min(
        1.0, float(capacity) / capacity_reference))
    member_continuity = max(0.0, min(1.0, float(member_continuity)))
    continuity_factor = (
        ORGANIZATION_OPERATING_CONTINUITY_FLOOR
        + (1.0 - ORGANIZATION_OPERATING_CONTINUITY_FLOOR)
        * member_continuity)
    replenishment = (
        ORGANIZATION_OPERATING_REPLENISHMENT[kind]
        * effectiveness * capacity_factor * continuity_factor)
    upkeep = ORGANIZATION_OPERATING_UPKEEP[kind]
    stress_cost = (ORGANIZATION_OPERATING_STRESS_COST
                   * max(0.0, min(100.0, float(worst_shortfall))) / 100.0)
    turnover_cost = (
        ORGANIZATION_OPERATING_TURNOVER_COST * (1.0 - member_continuity))
    delta = round(
        activity_factor * (replenishment - upkeep)
        - stress_cost - turnover_cost, 6)
    reserve = round(max(0.0, min(
        ORGANIZATION_OPERATING_RESERVE_CAP, reserve_before + delta)), 6)
    realized_delta = round(reserve - reserve_before, 6)
    status = organization_operating_status_next(status_before, reserve)
    return {
        "operating_reserve_before": round(reserve_before, 6),
        "operating_reserve_delta": realized_delta,
        "operating_reserve": reserve,
        "operating_status_before": status_before,
        "operating_status": status,
        "operating_transitioned": status != status_before,
        "member_continuity": round(member_continuity, 6),
        "turnover_cost": round(turnover_cost, 6),
    }


def organization_operating_factor(row: dict) -> float:
    """運営余力が今月の協調余剰へ寄与できる割合。旧stateは1.0。"""
    if "operating_reserve" not in row and "operating_status" not in row:
        return 1.0
    reserve = max(0.0, min(
        ORGANIZATION_OPERATING_RESERVE_CAP,
        float(row.get("operating_reserve", 0.0))))
    runway_factor = min(
        1.0, reserve / ORGANIZATION_OPERATING_RESERVE_INITIAL)
    status_factor = ORGANIZATION_OPERATING_EFFECT_FACTORS.get(
        str(row.get("operating_status", ORGANIZATION_OPERATING_STABLE)), 0.0)
    return runway_factor * status_factor


def _formal_company_available(community: dict, *, bank_stage: int,
                              currency_stage: int,
                              enforcement_stage: int,
                              retaining: bool = False) -> bool:
    economy = community.get("local_economy", {})
    productive_min = (COMPANY_RETAIN_MIN_PRODUCTIVE_POPULATION
                      if retaining else COMPANY_MIN_PRODUCTIVE_POPULATION)
    site_min = (COMPANY_RETAIN_MIN_ACTIVITY_SITES
                if retaining else COMPANY_MIN_ACTIVITY_SITES)
    trust_min = (COMPANY_RETAIN_MIN_COMMUNITY_TRUST
                 if retaining else COMPANY_MIN_COMMUNITY_TRUST)
    return (
        int(community.get("productive_population", 0))
        >= productive_min
        and int(community.get("site_count", 0)) >= site_min
        and float(economy.get("community_trust", 0.0)) >= trust_min
        and int(economy.get("local_credit_stage", 3)) <= 1
        and int(bank_stage) <= 1
        and int(currency_stage) <= 1
        and int(enforcement_stage) <= 1
    )


def _trust_target(community: dict, *, bank_stage: int,
                  currency_stage: int, enforcement_stage: int) -> float:
    economy = community.get("local_economy", {})
    penalty = (2.0 * max(0, int(bank_stage))
               + 2.0 * max(0, int(currency_stage))
               + 3.0 * max(0, int(enforcement_stage)))
    return max(0.0, min(
        100.0, float(economy.get("community_trust", 50.0)) - penalty))


def _member_counts(community: dict, site_rows: list[dict]) -> tuple[int, int]:
    member_count = sum(max(0, int(row.get("population_weight", 0)))
                       for row in site_rows)
    population = max(1, int(community.get("population", 0)))
    productive = max(0, int(community.get("productive_population", 0)))
    productive_members = min(
        member_count, round(productive * member_count / population))
    return member_count, productive_members


def _named_residents_by_household(
        resident_registry: dict | None) -> dict[str, list[dict]]:
    rows: dict[str, list[dict]] = {}
    if resident_registry is None:
        return rows
    for resident in sorted(
            resident_registry.get("residents", {}).values(),
            key=lambda row: row["id"]):
        if not resident.get("alive", True):
            continue
        rows.setdefault(str(resident.get("household_id")), []).append(resident)
    return rows


def _named_members(site_rows: list[dict],
                   residents_by_household: dict[str, list[dict]],
                   turn: int, productive_member_count: int,
                   purpose: str, workforce_count: int) -> dict:
    """活動点に属する観察可能な構成員IDを、人口正本と混同せず返す。"""
    household_ids = {
        str(row.get("household_id")) for row in site_rows
        if row.get("household_id") is not None}
    residents = sorted((
        resident for household_id in household_ids
        for resident in residents_by_household.get(household_id, ())
    ), key=lambda row: row["id"])
    named_member_ids = [row["id"] for row in residents]
    productive_lo, productive_hi = PRODUCTIVE_AGE_RANGE
    productive_candidates = [
        row for row in residents
        if productive_lo <= resident_age_years(row, turn) <= productive_hi]
    # 数値正本はsettlement cohortである。観察標本の年齢構成がその人数を
    # 上回る場合も、組織の労働力を表示標本から水増ししない。
    actual_candidates = [
        row for row in productive_candidates
        if row.get("last_activity_turn") is not None
        and int(row["last_activity_turn"]) == int(turn)
        and row.get("last_activity") == purpose]
    actual_ids = {row["id"] for row in actual_candidates}
    # cohortの数値正本を越えない範囲では、当月実際に働いた観察個体を優先して
    # 生産年齢構成員へ含める。これによりnamed_workerは必ずnamed memberの部分集合
    # となり、残りの実働は匿名cohortとして保持できる。
    ordered_productive = actual_candidates + [
        row for row in productive_candidates if row["id"] not in actual_ids]
    named_productive_ids = [
        row["id"] for row in ordered_productive[
            :max(0, int(productive_member_count))]]
    named_worker_ids = [
        resident_id for resident_id in named_productive_ids
        if resident_id in actual_ids
    ][:max(0, int(workforce_count))]
    return {
        "named_affiliated_resident_ids": named_member_ids,
        "named_productive_member_ids": named_productive_ids,
        "unrepresented_productive_member_count": max(
            0, int(productive_member_count) - len(named_productive_ids)),
        "named_worker_ids": named_worker_ids,
        "unrepresented_workforce_count": max(
            0, int(workforce_count) - len(named_worker_ids)),
    }


def organization_member_continuity(previous: dict | None,
                                   named_productive_member_ids: list[str],
                                   site_ids: list[str]) -> float:
    """前月の人と活動点が残った割合。旧stateは完全継続として読む。"""
    if previous is None:
        return 1.0
    if "named_productive_member_ids" not in previous:
        return 1.0
    components = []
    previous_members = set(previous.get("named_productive_member_ids", ()))
    if previous_members:
        components.append(
            len(previous_members & set(named_productive_member_ids))
            / len(previous_members))
    previous_sites = set(previous.get("site_ids", ()))
    if previous_sites:
        components.append(len(previous_sites & set(site_ids))
                          / len(previous_sites))
    return round(sum(components) / len(components), 6) if components else 1.0


def _predecessors(previous: dict, site_ids: set[str], purpose: str) -> list[str]:
    rows = []
    for organization_id, row in previous.items():
        if not row.get("active", True) or row.get("purpose") != purpose:
            continue
        overlap = len(site_ids & set(row.get("site_ids", ())))
        if overlap:
            rows.append((-overlap, organization_id))
    return [organization_id for _, organization_id in sorted(rows)]


def reconcile_organization_asset_claims(
        previous: dict[str, dict], active: dict[str, dict],
        communities: dict[str, dict]) -> dict[str, dict]:
    """組織の所有分を同一IDへ継承し、分裂・合流時は活動点比で保存配賦する。

    claimは共同体在庫の内数なので、最後に共同体別・財別の実在庫を上限として
    比例縮小する。後継の無い組織のclaimは共同体へ解放され、財自体は消えない。
    """
    after = copy.deepcopy(active)
    for row in after.values():
        row["asset_claims"] = initial_organization_asset_claims()

    for previous_id, prior in sorted(previous.items()):
        if not prior.get("active", True):
            continue
        prior_sites = set(prior.get("site_ids", ()))
        successor_ids = [
            organization_id
            for organization_id, row in after.items()
            if (organization_id == previous_id
                or (organization_id not in previous
                    and previous_id in row.get("predecessor_ids", ())))]
        successors = {
            organization_id: float(len(
                prior_sites & set(after[organization_id].get(
                    "site_ids", ()))))
            for organization_id in successor_ids}
        if successor_ids and not any(successors.values()):
            successors = {
                organization_id: 1.0
                for organization_id in successor_ids}
        if not successors:
            continue
        for field, value in _asset_claims(prior).items():
            for organization_id, allocation in (
                    _allocate_float_proportionally(
                        value, successors).items()):
                claims = after[organization_id]["asset_claims"]
                claims[field] = round(claims[field] + allocation, 6)

    by_community: dict[str, list[str]] = {}
    for organization_id, row in after.items():
        if row.get("active", True):
            by_community.setdefault(str(row.get(
                "home_activity_cluster_id") or ""), []).append(
                    organization_id)
    for community_id, organization_ids in sorted(by_community.items()):
        economy = communities.get(community_id, {}).get("local_economy", {})
        for field in ORGANIZATION_ASSET_CLAIM_FIELDS:
            weights = {
                organization_id: after[organization_id][
                    "asset_claims"][field]
                for organization_id in organization_ids
                if after[organization_id]["asset_claims"][field] > 0.0}
            claimed = round(sum(weights.values()), 6)
            available = round(max(0.0, float(economy.get(field, 0.0))), 6)
            if claimed <= available:
                continue
            allocations = _allocate_float_proportionally(available, weights)
            for organization_id in weights:
                after[organization_id]["asset_claims"][field] = (
                    allocations.get(organization_id, 0.0))

    for row in after.values():
        claims = row["asset_claims"]
        row["asset_claim_total"] = round(sum(claims.values()), 6)
        reference = max(0.000001, float(
            ORGANIZATION_ASSET_EFFECT_REFERENCE.get(row.get("kind"), 6.0)))
        row["asset_claim_coverage"] = round(min(
            1.0, row["asset_claim_total"] / reference), 6)
    return after


def organization_asset_claims_match_communities(
        state: dict, activity_community_ledger: dict) -> bool:
    """claimが非負で、共同体の物理在庫を超えないことを検証する。"""
    totals: dict[str, dict[str, float]] = {}
    for row in state.get("organizations", {}).values():
        if not row.get("active", True):
            continue
        community_id = str(row.get("home_activity_cluster_id") or "")
        aggregate = totals.setdefault(
            community_id, initial_organization_asset_claims())
        raw_claims = row.get("asset_claims", {})
        if any(not math.isfinite(float(raw_claims.get(field, 0.0)))
               or float(raw_claims.get(field, 0.0)) < 0.0
               for field in ORGANIZATION_ASSET_CLAIM_FIELDS):
            return False
        claims = _asset_claims(row)
        for field, value in claims.items():
            aggregate[field] = round(aggregate[field] + value, 6)
    communities = activity_community_ledger.get("communities", {})
    for community_id, claims in totals.items():
        economy = communities.get(community_id, {}).get("local_economy")
        if economy is None:
            return False
        if any(value > float(economy.get(field, 0.0)) + 1e-6
               for field, value in claims.items()):
            return False
    return True


def reconcile_organization_state_asset_claims(
        state: dict, communities: dict[str, dict]) -> dict:
    """同一組織のclaimを現在在庫へ合わせ、他の組織状態は保持する。"""
    after = upgrade_organization_state(state)
    organizations = after.get("organizations", {})
    after["organizations"] = reconcile_organization_asset_claims(
        organizations, organizations, communities)
    return after


def _organization_record(*, organization_id: str, purpose: str, kind: str,
                         community_id: str, community: dict,
                         sites: list[dict], turn: int, previous: dict | None,
                         residents_by_household: dict[str, list[dict]],
                         community_activity_worker_count: int,
                         bank_stage: int, currency_stage: int,
                         enforcement_stage: int) -> dict:
    member_count, productive_members = _member_counts(community, sites)
    workforce_organization = kind in ORGANIZATION_WORKFORCE_KINDS
    workforce_count = (sum(max(0, int(row.get(
        "activity_worker_count_by_good", {}).get(purpose, 0)))
        for row in sites) if workforce_organization else 0)
    community_activity_worker_count = max(
        0, int(community_activity_worker_count))
    if workforce_count > community_activity_worker_count:
        raise RuntimeError("organization workforce exceeds community activity")
    target = _trust_target(
        community, bank_stage=bank_stage, currency_stage=currency_stage,
        enforcement_stage=enforcement_stage)
    old_trust = float(previous.get("trust", target)) if previous else target
    trust = round(old_trust + (target - old_trust) * 0.2, 6)
    old_stage = int(previous.get(
        "trust_stage", ORGANIZATION_TRUST_HEALTHY)) if previous else 0
    trust_stage = organization_trust_stage_next(old_stage, trust)
    site_ids = sorted(row["id"] for row in sites)
    membership = _named_members(
        sites, residents_by_household, turn, productive_members,
        purpose, workforce_count)
    prior_asset_claims = _asset_claims(previous)
    member_continuity = organization_member_continuity(
        previous, membership["named_productive_member_ids"], site_ids)
    named_member_ids = (
        membership["named_productive_member_ids"]
        if workforce_organization
        else membership["named_affiliated_resident_ids"])
    participant_count = (
        productive_members if workforce_organization else member_count)
    productive_population = max(
        0, int(community.get("productive_population", 0)))
    production = float(community.get(
        "local_economy", {}).get("production_capacity", 0.0))
    practice = normalize_production_practice(
        community.get("local_economy", {}).get(
            "production_practice_by_good"))
    practice_score = (practice.get(purpose, 0.0)
                      if workforce_organization else 0.0)
    practice_factor = (practice_productivity_factor(practice_score)
                       if workforce_organization else 1.0)
    if workforce_organization:
        capacity = round(
            production * workforce_count / community_activity_worker_count
            * practice_factor,
            6) if community_activity_worker_count else 0.0
    else:
        capacity = round(
            production * productive_members / productive_population, 6
        ) if productive_population else 0.0
    operation = plan_organization_operation(
        previous, kind, capacity, trust, trust_stage,
        float(community.get("local_economy", {}).get(
            "worst_shortfall", 0.0)), member_continuity)
    operating_history = list(previous.get(
        "operating_history", ())) if previous else []
    if (not operating_history
            or operating_history[-1].get("status")
            != operation["operating_status"]):
        operating_history.append({
            "turn": int(turn),
            "status": operation["operating_status"],
            "reserve": operation["operating_reserve"],
        })
    histories = list(previous.get("form_history", ())) if previous else []
    if not histories or histories[-1].get("kind") != kind:
        histories.append({"turn": int(turn), "kind": kind})
    return {
        "id": organization_id,
        "kind": kind,
        "legal_form": {
            ORGANIZATION_KIND_COMPANY: "registered_company",
            ORGANIZATION_KIND_GUILD: "mutual_guild",
            ORGANIZATION_KIND_ASSEMBLY: "community_council",
            ORGANIZATION_KIND_CONGREGATION: "mutual_aid_congregation",
            ORGANIZATION_KIND_FAMILY_WORKSHOP: "household_workshop",
        }[kind],
        "purpose": purpose,
        "scope": [community_id],
        "member_count": member_count,
        "productive_member_count": productive_members,
        "affiliated_population_count": member_count,
        "named_affiliated_resident_ids": membership[
            "named_affiliated_resident_ids"],
        "participant_count": participant_count,
        "named_member_ids": named_member_ids,
        "unrepresented_member_count": max(
            0, participant_count - len(named_member_ids)),
        "named_productive_member_ids": membership[
            "named_productive_member_ids"],
        "unrepresented_productive_member_count": membership[
            "unrepresented_productive_member_count"],
        "work_relationship": ORGANIZATION_WORK_RELATIONSHIPS[kind],
        "eligible_workforce_count": (
            productive_members if workforce_organization else 0),
        "workforce_count": workforce_count,
        "named_worker_ids": (
            membership["named_worker_ids"] if workforce_organization else []),
        "unrepresented_workforce_count": (
            membership["unrepresented_workforce_count"]
            if workforce_organization else 0),
        "workforce_affiliation_ratio": round(
            workforce_count / productive_members, 6
        ) if workforce_organization and productive_members else 0.0,
        "workforce_community_activity_share": round(
            workforce_count / community_activity_worker_count, 6
        ) if workforce_organization and community_activity_worker_count else 0.0,
        "member_continuity": member_continuity,
        "asset_claims": prior_asset_claims,
        "asset_claim_total": round(sum(prior_asset_claims.values()), 6),
        "asset_claim_coverage": round(min(
            1.0, sum(prior_asset_claims.values()) / max(
                0.000001, ORGANIZATION_ASSET_EFFECT_REFERENCE[kind])), 6),
        "last_surplus_allocation": copy.deepcopy(
            previous.get("last_surplus_allocation")
            if previous else None),
        "trust": trust,
        "trust_stage": trust_stage,
        "production_practice_score": practice_score,
        "production_practice_factor": practice_factor,
        "capacity": capacity,
        "size": organization_size(productive_members),
        "operating_reserve": operation["operating_reserve"],
        "operating_reserve_delta": operation["operating_reserve_delta"],
        "operating_status": operation["operating_status"],
        "operating_history": operating_history,
        "home_activity_cluster_id": community_id,
        "site_ids": site_ids,
        "founded_turn": int(previous.get("founded_turn", turn)) if previous else int(turn),
        "ended_turn": None,
        "active": True,
        "predecessor_ids": (list(previous.get("predecessor_ids", ()))
                            if previous else []),
        "form_history": histories,
    }


def plan_organization_turn(state: dict | None, activity_community_ledger: dict,
                           spatial_state: dict, turn: int, *, bank_stage: int,
                           currency_stage: int,
                           enforcement_stage: int,
                           resident_registry: dict | None = None) -> dict:
    """当月の活動共同体から組織台帳を更新する。入力は変更しない。"""
    before = upgrade_organization_state(
        state or initial_organization_state())
    previous = before.get("organizations", {})
    sites_by_id = spatial_state.get("sites", {})
    residents_by_household = _named_residents_by_household(resident_registry)
    active: dict[str, dict] = {}
    events = []

    for community_id, community in sorted(
            activity_community_ledger.get("communities", {}).items()):
        if int(community.get("population", 0)) <= 0:
            continue
        community_sites = [
            sites_by_id[site_id]
            for site_id in community.get("site_ids", ())
            if site_id in sites_by_id
            and sites_by_id[site_id].get("active", True)]
        community_activity_worker_count = sum(
            max(0, int(site.get("activity_worker_count", 0)))
            for site in community_sites)
        sector_sites: dict[str, list[dict]] = {}
        for site in community_sites:
            sector_sites.setdefault(
                str(site.get("livelihood") or "shared"), []).append(site)
        for purpose, rows in sorted(sector_sites.items()):
            member_count, productive_members = _member_counts(community, rows)
            if member_count <= 0:
                continue
            organization_id = f"org:{community_id}:{purpose}"
            prior = previous.get(organization_id)
            prior_active = bool(prior and prior.get("active", True))
            retaining_company = bool(
                prior_active
                and prior.get("kind") == ORGANIZATION_KIND_COMPANY)
            formal_entry = _formal_company_available(
                community, bank_stage=bank_stage,
                currency_stage=currency_stage,
                enforcement_stage=enforcement_stage,
                retaining=False)
            formal_retained = (
                retaining_company and _formal_company_available(
                    community, bank_stage=bank_stage,
                    currency_stage=currency_stage,
                    enforcement_stage=enforcement_stage,
                    retaining=True))
            prior_eligible_turns = (
                int(prior.get("company_eligible_turns", 0))
                if prior_active and not retaining_company else 0)
            company_eligible_turns = (
                prior_eligible_turns + 1
                if formal_entry and len(rows) >= COMPANY_MIN_SECTOR_SITES
                else 0)
            company_can_form = (
                prior is None
                or company_eligible_turns >= COMPANY_REFORM_STABILITY_TURNS)
            if (formal_retained
                    or (formal_entry and company_can_form
                        and len(rows) >= COMPANY_MIN_SECTOR_SITES)):
                kind = ORGANIZATION_KIND_COMPANY
            elif (len(rows) >= 2
                  and productive_members >= (
                      GUILD_RETAIN_MIN_PRODUCTIVE_MEMBERS
                      if prior_active and prior.get("kind") in {
                          ORGANIZATION_KIND_COMPANY,
                          ORGANIZATION_KIND_GUILD}
                      else GUILD_MIN_PRODUCTIVE_MEMBERS)):
                kind = ORGANIZATION_KIND_GUILD
            else:
                kind = ORGANIZATION_KIND_FAMILY_WORKSHOP
            record = _organization_record(
                organization_id=organization_id, purpose=purpose, kind=kind,
                community_id=community_id, community=community, sites=rows,
                turn=turn, previous=prior,
                residents_by_household=residents_by_household,
                community_activity_worker_count=(
                    community_activity_worker_count),
                bank_stage=bank_stage,
                currency_stage=currency_stage,
                enforcement_stage=enforcement_stage)
            record["company_eligible_turns"] = (
                COMPANY_REFORM_STABILITY_TURNS
                if kind == ORGANIZATION_KIND_COMPANY
                else company_eligible_turns)
            if prior is None:
                record["predecessor_ids"] = _predecessors(
                    previous, set(record["site_ids"]), purpose)
                events.append({
                    "turn": int(turn), "kind": "organization_founded",
                    "organization_id": organization_id,
                    "organization_kind": kind,
                    "community_id": community_id, "purpose": purpose})
            elif prior.get("kind") != kind:
                events.append({
                    "turn": int(turn), "kind": "organization_reformed",
                    "organization_id": organization_id,
                    "from_kind": prior.get("kind"), "to_kind": kind,
                    "community_id": community_id, "purpose": purpose})
            active[organization_id] = record

        productive = int(community.get("productive_population", 0))
        organization_id = f"org:{community_id}:civic"
        prior = previous.get(organization_id)
        retaining_assembly = bool(
            prior and prior.get("active", True)
            and prior.get("kind") == ORGANIZATION_KIND_ASSEMBLY)
        if (productive >= (
                ASSEMBLY_RETAIN_MIN_PRODUCTIVE_POPULATION
                if retaining_assembly
                else ASSEMBLY_MIN_PRODUCTIVE_POPULATION)
                and len(community_sites) >= (
                    ASSEMBLY_RETAIN_MIN_ACTIVITY_SITES
                    if retaining_assembly
                    else ASSEMBLY_MIN_ACTIVITY_SITES)):
            record = _organization_record(
                organization_id=organization_id, purpose="shared_governance",
                kind=ORGANIZATION_KIND_ASSEMBLY, community_id=community_id,
                community=community, sites=community_sites, turn=turn,
                previous=prior,
                residents_by_household=residents_by_household,
                community_activity_worker_count=(
                    community_activity_worker_count),
                bank_stage=bank_stage,
                currency_stage=currency_stage,
                enforcement_stage=enforcement_stage)
            if prior is None:
                record["predecessor_ids"] = _predecessors(
                    previous, set(record["site_ids"]),
                    "shared_governance")
                events.append({
                    "turn": int(turn), "kind": "organization_founded",
                    "organization_id": organization_id,
                    "organization_kind": ORGANIZATION_KIND_ASSEMBLY,
                    "community_id": community_id,
                    "purpose": "shared_governance"})
            active[organization_id] = record

        economy = community.get("local_economy", {})
        organization_id = f"org:{community_id}:care"
        prior = previous.get(organization_id)
        retaining_congregation = bool(
            prior and prior.get("active", True)
            and prior.get("kind") == ORGANIZATION_KIND_CONGREGATION)
        social_stress = (
            float(economy.get("community_health", 80.0))
            < (CONGREGATION_HEALTH_EXIT if retaining_congregation
               else CONGREGATION_HEALTH_TRIGGER)
            or int(community.get("population_stage", 0))
            >= (1 if retaining_congregation else 2))
        if (social_stress
                and len(community_sites) >= CONGREGATION_MIN_ACTIVITY_SITES
                and productive >= (
                    CONGREGATION_RETAIN_MIN_PRODUCTIVE_POPULATION
                    if retaining_congregation else 4)):
            record = _organization_record(
                organization_id=organization_id, purpose="care_and_ritual",
                kind=ORGANIZATION_KIND_CONGREGATION,
                community_id=community_id, community=community,
                sites=community_sites, turn=turn, previous=prior,
                residents_by_household=residents_by_household,
                community_activity_worker_count=(
                    community_activity_worker_count),
                bank_stage=bank_stage, currency_stage=currency_stage,
                enforcement_stage=enforcement_stage)
            if prior is None:
                record["predecessor_ids"] = _predecessors(
                    previous, set(record["site_ids"]),
                    "care_and_ritual")
                events.append({
                    "turn": int(turn), "kind": "organization_founded",
                    "organization_id": organization_id,
                    "organization_kind": ORGANIZATION_KIND_CONGREGATION,
                    "community_id": community_id,
                    "purpose": "care_and_ritual"})
            active[organization_id] = record

    active = reconcile_organization_asset_claims(
        previous, active,
        activity_community_ledger.get("communities", {}))
    seen_named_workers: set[str] = set()
    for community_id, community in activity_community_ledger.get(
            "communities", {}).items():
        active_site_ids = {
            site_id for site_id in community.get("site_ids", ())
            if site_id in sites_by_id
            and sites_by_id[site_id].get("active", True)}
        available_workers = sum(max(0, int(
            sites_by_id[site_id].get("activity_worker_count", 0)))
            for site_id in active_site_ids)
        rows = [
            row for row in active.values()
            if row.get("home_activity_cluster_id") == community_id
            and row.get("kind") in ORGANIZATION_WORKFORCE_KINDS]
        if sum(int(row.get("workforce_count", 0)) for row in rows) > (
                available_workers):
            raise RuntimeError(
                "organization workforce exceeds activity ledger")
        for row in rows:
            worker_ids = set(row.get("named_worker_ids", ()))
            if seen_named_workers & worker_ids:
                raise RuntimeError("named worker belongs to multiple organizations")
            seen_named_workers.update(worker_ids)
    candidate_state = {"organizations": active}
    if not organization_asset_claims_match_communities(
            candidate_state, activity_community_ledger):
        raise RuntimeError(
            "organization asset claims exceed community inventory")

    all_rows = copy.deepcopy(active)
    for organization_id, row in previous.items():
        if organization_id in active:
            continue
        ended = copy.deepcopy(row)
        if ended.get("active", True):
            ended["active"] = False
            ended["ended_turn"] = int(turn)
            events.append({
                "turn": int(turn), "kind": "organization_ended",
                "organization_id": organization_id,
                "organization_kind": ended.get("kind"),
                "community_id": ended.get("home_activity_cluster_id"),
                "purpose": ended.get("purpose")})
        # 後継へ移らなかった所有分は共同体在庫へ解放する。財そのものは
        # communities側に残っているため、ここではclaimだけを0にする。
        ended["asset_claims"] = initial_organization_asset_claims()
        ended["asset_claim_total"] = 0.0
        ended["asset_claim_coverage"] = 0.0
        all_rows[organization_id] = ended
    history = list(before.get("events", ())) + events
    return {
        "version": ORGANIZATION_STATE_VERSION,
        "updated_turn": int(turn),
        "organizations": all_rows,
        "events": history,
        "effect_totals": copy.deepcopy(before.get(
            "effect_totals", initial_organization_state()["effect_totals"])),
        "effect_application_count": int(before.get(
            "effect_application_count", 0)),
        "last_effect_turn": before.get("last_effect_turn"),
        "last_effects": copy.deepcopy(before.get("last_effects")),
        "asset_claim_acquisition_totals": copy.deepcopy(before.get(
            "asset_claim_acquisition_totals",
            initial_organization_state()["asset_claim_acquisition_totals"])),
        "community_distribution_totals": copy.deepcopy(before.get(
            "community_distribution_totals",
            initial_organization_state()["community_distribution_totals"])),
        "last_asset_allocation_turn": before.get(
            "last_asset_allocation_turn"),
        "last_asset_allocations": copy.deepcopy(before.get(
            "last_asset_allocations", [])),
    }


def active_organizations(state: dict) -> list[dict]:
    state = upgrade_organization_state(state)
    return sorted((
        copy.deepcopy(row) for row in state.get("organizations", {}).values()
        if row.get("active", True)
    ), key=lambda row: row["id"])


def plan_organization_effects(state: dict) -> dict:
    """前月末の組織台帳から、共同体別の今月の協調余剰を計画する。"""
    settlements: dict[str, dict[str, float]] = {}
    contributors = []
    for row in active_organizations(state):
        community_id = str(row.get("home_activity_cluster_id") or "")
        if not community_id:
            continue
        capacity = max(0.0, float(row.get("capacity", 0.0)))
        trust_effectiveness = _organization_effectiveness(row)
        operating_factor = organization_operating_factor(row)
        asset_factor = organization_asset_factor(row)
        effectiveness = (
            trust_effectiveness * operating_factor * asset_factor)
        kind = row.get("kind")
        purpose = row.get("purpose")
        deltas = {}
        if (kind in ORGANIZATION_SECTOR_OUTPUT_RATES
                and purpose in {"food", "medicine", "shelter", "tools"}):
            deltas[f"{purpose}_delta"] = (
                capacity * ORGANIZATION_SECTOR_OUTPUT_RATES[kind]
                * effectiveness)
        elif kind == ORGANIZATION_KIND_ASSEMBLY:
            deltas = {
                "community_trust_delta": (
                    capacity * ASSEMBLY_TRUST_RATE * effectiveness),
                "production_capacity_delta": (
                    capacity * ASSEMBLY_PRODUCTION_CAPACITY_RATE
                    * effectiveness),
            }
        elif kind == ORGANIZATION_KIND_CONGREGATION:
            deltas = {
                "community_health_delta": (
                    capacity * CONGREGATION_HEALTH_RATE * effectiveness),
                "medicine_delta": (
                    capacity * CONGREGATION_MEDICINE_RATE * effectiveness),
            }
        deltas = {
            field: round(value, 6) for field, value in deltas.items()
            if value > 0.0}
        if not deltas:
            continue
        aggregate = settlements.setdefault(community_id, {})
        for field, value in deltas.items():
            aggregate[field] = aggregate.get(field, 0.0) + value
        contributors.append({
            "organization_id": row.get("id"),
            "community_id": community_id,
            "kind": kind,
            "purpose": purpose,
            "productive_member_count": int(row.get(
                "productive_member_count", 0)),
            "eligible_workforce_count": int(row.get(
                "eligible_workforce_count",
                row.get("productive_member_count", 0))),
            "workforce_count": int(row.get("workforce_count", 0)),
            "capacity": round(capacity, 6),
            "trust": round(float(row.get("trust", 0.0)), 6),
            "trust_stage": int(row.get(
                "trust_stage", ORGANIZATION_TRUST_BROKEN)),
            "trust_effectiveness": round(trust_effectiveness, 6),
            "operating_reserve": round(float(row.get(
                "operating_reserve",
                ORGANIZATION_OPERATING_RESERVE_INITIAL)), 6),
            "operating_status": str(row.get(
                "operating_status", ORGANIZATION_OPERATING_STABLE)),
            "operating_factor": round(operating_factor, 6),
            "asset_claims": _asset_claims(row),
            "asset_factor": asset_factor,
            "effectiveness": round(effectiveness, 6),
            "deltas": deltas,
        })
    for deltas in settlements.values():
        for field, value in tuple(deltas.items()):
            deltas[field] = round(min(
                ORGANIZATION_EFFECT_LIMITS[field], value), 6)
    return {
        "version": ORGANIZATION_EFFECT_PLAN_VERSION,
        "source_turn": int(state.get("updated_turn", 0)),
        "settlements": settlements,
        "contributors": contributors,
        "organization_count": len(contributors),
    }


def record_organization_effects(state: dict, effects: dict, turn: int) -> dict:
    """適用済み効果と、その内数である組織留保・共同体分配を記録する。"""
    after = upgrade_organization_state(state)
    totals = dict(initial_organization_state()["effect_totals"])
    totals.update(after.get("effect_totals", {}))
    for deltas in effects.get("settlements", {}).values():
        for field, value in deltas.items():
            if field in totals:
                totals[field] = round(totals[field] + float(value), 6)
    after["effect_totals"] = totals
    if effects.get("settlements"):
        after["effect_application_count"] = int(after.get(
            "effect_application_count", 0)) + 1
    else:
        after["effect_application_count"] = int(after.get(
            "effect_application_count", 0))
    after["last_effect_turn"] = int(turn)
    after["last_effects"] = copy.deepcopy(effects)

    acquired_totals = initial_organization_asset_claims()
    acquired_totals.update(after.get("asset_claim_acquisition_totals", {}))
    distributed_totals = initial_organization_asset_claims()
    distributed_totals.update(after.get("community_distribution_totals", {}))
    organizations = after.get("organizations", {})
    contributors = effects.get("contributors", ())
    allocations_by_organization: dict[str, dict] = {}
    for community_id, realized_deltas in sorted(
            effects.get("settlements", {}).items()):
        for asset_field in ORGANIZATION_ASSET_CLAIM_FIELDS:
            effect_field = f"{asset_field}_delta"
            realized_total = round(max(
                0.0, float(realized_deltas.get(effect_field, 0.0))), 6)
            if realized_total <= 0.0:
                continue
            weights: dict[str, float] = {}
            for contributor in contributors:
                organization_id = contributor.get("organization_id")
                if (str(contributor.get("community_id")) != str(community_id)
                        or organization_id not in organizations
                        or not organizations[organization_id].get(
                            "active", True)):
                    continue
                planned = max(0.0, float(contributor.get(
                    "deltas", {}).get(effect_field, 0.0)))
                if planned > 0.0:
                    weights[organization_id] = (
                        weights.get(organization_id, 0.0) + planned)
            for organization_id, realized in (
                    _allocate_float_proportionally(
                        realized_total, weights).items()):
                row = organizations[organization_id]
                retention_rate = float(
                    ORGANIZATION_ASSET_RETENTION_RATES.get(
                        row.get("kind"), 0.0))
                retained = round(realized * retention_rate, 6)
                distributed = round(realized - retained, 6)
                claims = _asset_claims(row)
                claims[asset_field] = round(
                    claims[asset_field] + retained, 6)
                row["asset_claims"] = claims
                row["asset_claim_total"] = round(sum(claims.values()), 6)
                reference = max(0.000001, float(
                    ORGANIZATION_ASSET_EFFECT_REFERENCE.get(
                        row.get("kind"), 6.0)))
                row["asset_claim_coverage"] = round(min(
                    1.0, row["asset_claim_total"] / reference), 6)
                allocation = allocations_by_organization.setdefault(
                    organization_id, {
                        "turn": int(turn),
                        "organization_id": organization_id,
                        "community_id": str(community_id),
                        "realized_surplus": initial_organization_asset_claims(),
                        "asset_claims_acquired": (
                            initial_organization_asset_claims()),
                        "community_distributed": (
                            initial_organization_asset_claims()),
                    })
                allocation["realized_surplus"][asset_field] = round(
                    allocation["realized_surplus"][asset_field]
                    + realized, 6)
                allocation["asset_claims_acquired"][asset_field] = round(
                    allocation["asset_claims_acquired"][asset_field]
                    + retained, 6)
                allocation["community_distributed"][asset_field] = round(
                    allocation["community_distributed"][asset_field]
                    + distributed, 6)
                acquired_totals[asset_field] = round(
                    float(acquired_totals.get(asset_field, 0.0))
                    + retained, 6)
                distributed_totals[asset_field] = round(
                    float(distributed_totals.get(asset_field, 0.0))
                    + distributed, 6)

    allocations = [
        allocations_by_organization[organization_id]
        for organization_id in sorted(allocations_by_organization)]
    for allocation in allocations:
        organizations[allocation["organization_id"]][
            "last_surplus_allocation"] = copy.deepcopy(allocation)
    after["asset_claim_acquisition_totals"] = acquired_totals
    after["community_distribution_totals"] = distributed_totals
    after["last_asset_allocation_turn"] = int(turn)
    after["last_asset_allocations"] = allocations
    return after
