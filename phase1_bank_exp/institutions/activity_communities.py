# -*- coding: utf-8 -*-
"""活動クラスタを数値上の共同体会計へ昇格する、保存則付きの境界。

前月のsettlement会計にある人口・財・信用を、住民が実際に属する活動クラスタへ
決定論的に配賦する。加算可能な値は世界合計を保存し、Stageや不足は構成元の
最悪値、信用・健康は人口加重平均とする。その台帳を直後に``settlements``と
住民所属へ昇格することで、次月の出生・交易・移住はcluster lineage IDを持つ
活動共同体そのものを主体として計算される。

このモジュールは座標を生成せず、入力を変更せず、乱数を使わない。分裂時は
在庫・生活基盤規模・累計・端数carryを人口比で保存配賦し、合流時は
加算する。生産能力と経験は強度値なので、分裂で継承する。合流時は生産能力を
生活基盤規模で、人に宿る生産経験を人口で加重平均する。住民を失った
土地在庫は人口0のreserveとして残す。
"""
from __future__ import annotations

import copy

from institutions.population import (
    AGE_COHORT_KEYS,
    AGE_COHORT_PRODUCTIVE,
    DEMOGRAPHY_VERSION,
    upgrade_settlement_demography,
)
from institutions.production_practice import (
    PRACTICE_GOODS,
    initial_production_practice,
    normalize_production_practice,
)
from institutions.barter import (
    GOODS_ACCOUNTING_VERSION,
    PROVISIONING_SCALE_INITIAL,
    normalize_provisioning_scale,
)
from institutions.household_needs import demand_scales_by_good


ACTIVITY_COMMUNITY_LEDGER_VERSION = 4
ACTIVITY_COMMUNITY_ACCOUNTING_VERSION = 4
FLOAT_ACCOUNT_FIELDS = (
    "food", "medicine", "shelter", "tools", "provisioning_scale",
    "trade_sent_total", "trade_received_total",
)
INTENSIVE_ACCOUNT_FIELDS = ("production_capacity",)
INTEGER_ACCOUNT_FIELDS = ("trade_events_total",)
FLOAT_SETTLEMENT_FIELDS = (
    "birth_carry", "death_carry", "migration_carry",
    "last_expected_net_change",
)
INTEGER_SETTLEMENT_FIELDS = (
    "births_total", "deaths_total", "last_births", "last_deaths",
    "immigrants_total", "emigrants_total",
)
AGE_TRANSITION_CARRY_KEYS = (
    "children_to_productive", "productive_to_elderly")


def _account_digits(field: str) -> int:
    # 100万人級でも1人分の生活基盤を0へ丸めない。財本体と
    # 累計値は従来の6桁を維持する。
    return 12 if field == "provisioning_scale" else 6


def _allocate_float(total: float, weights: dict[str, int],
                    digits: int = 6) -> dict[str, float]:
    keys = sorted(key for key, weight in weights.items() if weight > 0)
    if not keys:
        return {}
    denominator = sum(weights[key] for key in keys)
    allocations = {}
    used = 0.0
    for key in keys[:-1]:
        value = round(float(total) * weights[key] / denominator, digits)
        allocations[key] = value
        used = round(used + value, digits)
    allocations[keys[-1]] = round(float(total) - used, digits)
    return allocations


def _allocate_integer(total: int, weights: dict[str, int]) -> dict[str, int]:
    keys = sorted(key for key, weight in weights.items() if weight > 0)
    if not keys:
        return {}
    denominator = sum(weights[key] for key in keys)
    exact = {key: int(total) * weights[key] / denominator for key in keys}
    allocations = {key: int(exact[key]) for key in keys}
    remainder = int(total) - sum(allocations.values())
    order = sorted(keys, key=lambda key: (-(exact[key] - allocations[key]), key))
    for key in order[:remainder]:
        allocations[key] += 1
    return allocations


def _site_to_cluster(spatial_state: dict) -> dict[str, str]:
    mapping = {}
    for cluster_id, cluster in spatial_state.get("clusters", {}).items():
        accounting_cluster_id = (
            cluster.get("accounting_parent_id")
            if cluster.get("provisional", False) else cluster_id)
        if accounting_cluster_id not in spatial_state.get("clusters", {}):
            raise ValueError(
                f"provisional cluster has no accounting parent: {cluster_id}")
        for site_id in cluster.get("site_ids", ()):
            if site_id in mapping:
                raise ValueError(f"site belongs to multiple clusters: {site_id}")
            mapping[site_id] = accounting_cluster_id
    return mapping


def _weighted_average(rows: list[tuple[float, float]], default: float) -> float:
    total_weight = sum(weight for _, weight in rows)
    if total_weight <= 0:
        return float(default)
    return round(sum(value * weight for value, weight in rows) / total_weight, 6)


def build_activity_community_ledger(spatial_state: dict, registry: dict,
                                    settlements: dict, turn: int) -> dict:
    """活動クラスタへ人口・財・信用を保存則付きで投影する。"""
    site_to_cluster = _site_to_cluster(spatial_state)
    clusters = spatial_state.get("clusters", {})
    accounting_site_ids = {
        cluster_id: sorted(
            site_id for site_id, target_id in site_to_cluster.items()
            if target_id == cluster_id)
        for cluster_id, cluster in clusters.items()
        if not cluster.get("provisional", False)}
    communities = {}
    for cluster_id, cluster in sorted(clusters.items()):
        if cluster.get("provisional", False):
            continue
        communities[cluster_id] = {
            "id": cluster_id,
            "name": f"活動集落 {cluster_id.rsplit(':', 1)[-1]}",
            "formed_turn": int(cluster.get("formed_turn", turn)),
            "parent_ids": list(cluster.get("parent_ids", ())),
            "centroid_x": cluster["centroid_x"],
            "centroid_y": cluster["centroid_y"],
            "site_ids": accounting_site_ids.get(cluster_id, []),
            "site_count": len(accounting_site_ids.get(cluster_id, [])),
            "resident_count": 0, "named_resident_count": 0,
            "population": 0,
            "reproductive_population": 0,
            "productive_population": 0,
            "age_cohorts": {key: 0 for key in AGE_COHORT_KEYS},
            "age_transition_carry": {
                key: 0.0 for key in AGE_TRANSITION_CARRY_KEYS},
            "demography_version": DEMOGRAPHY_VERSION,
            "household_count": 0,
            **{key: 0.0 for key in FLOAT_SETTLEMENT_FIELDS},
            **{key: 0 for key in INTEGER_SETTLEMENT_FIELDS},
            "account_populations": {},
            "account_provisioning_scales": {},
            "dominant_account_id": None,
            "population_stage": 0,
            "local_economy": {
                **{key: 0.0 for key in FLOAT_ACCOUNT_FIELDS},
                **{key: 0 for key in INTEGER_ACCOUNT_FIELDS},
                "goods_accounting_version": GOODS_ACCOUNTING_VERSION,
                "production_capacity": 0.0,
                "community_trust": 50.0, "community_health": 80.0,
                "local_credit_stage": 0, "barter_stage": 0,
                "worst_shortfall": 0.0, "worst_good": None,
                "production_practice_by_good": (
                    initial_production_practice()),
            },
        }

    living = {
        resident_id: resident
        for resident_id, resident in registry.get("residents", {}).items()
        if resident.get("alive", True)}
    resident_communities = {}
    household_sets = {cluster_id: set() for cluster_id in communities}
    account_cluster_weights: dict[str, dict[str, int]] = {}
    household_communities = {}
    for physical_cluster_id, cluster in sorted(clusters.items()):
        for site_id in cluster.get("site_ids", ()):
            site = spatial_state.get("sites", {}).get(site_id)
            if site is None or not site.get("active", True):
                continue
            cluster_id = site_to_cluster.get(site_id)
            if cluster_id not in communities:
                raise ValueError(
                    f"site has no accounting activity community: {site_id}")
            household_id = site.get("household_id")
            if household_id is not None:
                prior = household_communities.setdefault(
                    household_id, cluster_id)
                if prior != cluster_id:
                    raise ValueError(
                        f"household spans activity communities: {household_id}")
                household_sets[cluster_id].add(household_id)
            weight = int(site.get("population_weight", 0))
            if weight <= 0:
                continue
            account_id = str(site.get("account_id"))
            weights = account_cluster_weights.setdefault(account_id, {})
            weights[cluster_id] = weights.get(cluster_id, 0) + weight
    for resident_id, resident in sorted(living.items()):
        position = spatial_state.get("residents", {}).get(resident_id)
        if position is None:
            raise ValueError(f"living resident has no spatial position: {resident_id}")
        cluster_id = site_to_cluster.get(position.get("site_id"))
        if cluster_id not in communities:
            raise ValueError(f"resident has no activity community: {resident_id}")
        account_id = str(resident.get("settlement_id"))
        community = communities[cluster_id]
        community["named_resident_count"] += 1
        resident_communities[resident_id] = cluster_id

    unassigned_accounts = {}
    for account_id, raw_settlement in sorted(settlements.items()):
        account_id = str(account_id)
        settlement = upgrade_settlement_demography(raw_settlement)
        economy = settlement.get("local_economy", {})
        weights = account_cluster_weights.get(account_id, {})
        if not weights:
            unassigned_accounts[account_id] = {
                "id": account_id,
                "name": settlement.get("name", account_id),
                "founded_turn": int(settlement.get("founded_turn", turn)),
                "population": int(settlement.get("population", 0)),
                "reproductive_population": int(settlement.get(
                    "reproductive_population", 0)),
                "productive_population": int(settlement.get(
                    "productive_population", 0)),
                "age_cohorts": copy.deepcopy(settlement["age_cohorts"]),
                "age_transition_carry": copy.deepcopy(
                    settlement["age_transition_carry"]),
                "demography_version": DEMOGRAPHY_VERSION,
                "population_stage": int(settlement.get("stage", 4)),
                **{key: copy.deepcopy(settlement.get(key, 0.0))
                   for key in FLOAT_SETTLEMENT_FIELDS},
                **{key: int(settlement.get(key, 0))
                   for key in INTEGER_SETTLEMENT_FIELDS},
                "local_economy": {
                    key: copy.deepcopy(economy.get(key, 0))
                    for key in (*FLOAT_ACCOUNT_FIELDS, *INTEGER_ACCOUNT_FIELDS)},
            }
            unassigned_accounts[account_id]["local_economy"].update({
                "goods_accounting_version": GOODS_ACCOUNTING_VERSION,
                "provisioning_scale": normalize_provisioning_scale(
                    economy.get("provisioning_scale")),
                "production_capacity": float(
                    economy.get("production_capacity", 0.0)),
                "community_trust": float(economy.get("community_trust", 50.0)),
                "community_health": float(economy.get("community_health", 80.0)),
                "local_credit_stage": int(economy.get("local_credit_stage", 0)),
                "barter_stage": int(economy.get("barter_stage", 0)),
                "worst_shortfall": float(economy.get("worst_shortfall", 0.0)),
                "worst_good": economy.get("worst_good"),
                "production_practice_by_good": (
                    normalize_production_practice(economy.get(
                        "production_practice_by_good"))),
            })
            continue
        population = _allocate_integer(
            int(settlement.get("population", 0)), weights)
        for cluster_id, value in population.items():
            community = communities[cluster_id]
            community["population"] += value
            community["resident_count"] += value
            community["account_populations"][account_id] = (
                community["account_populations"].get(account_id, 0)
                + value)
        reproductive = _allocate_integer(
            int(settlement.get("reproductive_population", 0)), weights)
        for cluster_id, value in reproductive.items():
            communities[cluster_id]["reproductive_population"] += value
        # cohortを互いに独立に丸めると、clusterごとの合計がpopulation配賦と
        # 1人ずれる場合がある。残容量を次区分のweightにすることで、世界の
        # 区分別合計と各cluster人口の双方を同時に保存する。
        remaining_population = dict(population)
        for cohort_key in AGE_COHORT_KEYS[:-1]:
            allocation = _allocate_integer(
                int(settlement["age_cohorts"][cohort_key]),
                remaining_population)
            for cluster_id, value in allocation.items():
                communities[cluster_id]["age_cohorts"][cohort_key] += value
                remaining_population[cluster_id] -= value
        final_cohort = AGE_COHORT_KEYS[-1]
        if sum(remaining_population.values()) != int(
                settlement["age_cohorts"][final_cohort]):
            raise RuntimeError("activity community age cohort allocation drift")
        for cluster_id, value in remaining_population.items():
            communities[cluster_id]["age_cohorts"][final_cohort] += value
        for carry_key in AGE_TRANSITION_CARRY_KEYS:
            allocation = _allocate_float(
                float(settlement["age_transition_carry"][carry_key]),
                weights, digits=12)
            for cluster_id, value in allocation.items():
                communities[cluster_id]["age_transition_carry"][carry_key] = round(
                    communities[cluster_id]["age_transition_carry"][carry_key]
                    + value, 12)
        for field in FLOAT_ACCOUNT_FIELDS:
            for cluster_id, value in _allocate_float(
                    (normalize_provisioning_scale(
                        economy.get("provisioning_scale"))
                     if field == "provisioning_scale" else
                     float(economy.get(field, 0.0))),
                    weights, digits=_account_digits(field)).items():
                communities[cluster_id]["local_economy"][field] = round(
                    communities[cluster_id]["local_economy"][field] + value,
                    _account_digits(field))
                if field == "provisioning_scale":
                    communities[cluster_id][
                        "account_provisioning_scales"][account_id] = value
        for field in INTEGER_ACCOUNT_FIELDS:
            for cluster_id, value in _allocate_integer(
                    int(economy.get(field, 0)), weights).items():
                communities[cluster_id]["local_economy"][field] += value
        for field in FLOAT_SETTLEMENT_FIELDS:
            for cluster_id, value in _allocate_float(
                    float(settlement.get(field, 0.0)), weights,
                    digits=12).items():
                communities[cluster_id][field] = round(
                    communities[cluster_id][field] + value, 12)
        for field in INTEGER_SETTLEMENT_FIELDS:
            for cluster_id, value in _allocate_integer(
                    int(settlement.get(field, 0)), weights).items():
                communities[cluster_id][field] += value

    for cluster_id, community in communities.items():
        community["productive_population"] = community["age_cohorts"][
            AGE_COHORT_PRODUCTIVE]
        community["household_count"] = len(household_sets[cluster_id])
        account_rows = community["account_populations"]
        if account_rows:
            community["dominant_account_id"] = min(
                account_rows, key=lambda key: (-account_rows[key], key))
        sources = [
            (settlements[account_id], weight)
            for account_id, weight in account_rows.items()
            if account_id in settlements]
        economy = community["local_economy"]
        economy["goods_accounting_version"] = GOODS_ACCOUNTING_VERSION
        economy["demand_scales_by_good"] = demand_scales_by_good({
            "population": community["population"],
            "reproductive_population": community[
                "reproductive_population"],
            "age_cohorts": community["age_cohorts"],
            "age_transition_carry": community["age_transition_carry"],
        })
        # production_capacityは在庫量ではなく0〜100の生産強度。
        # 分裂では親の強度を継承し、合流では生活基盤規模で加重平均する。
        capacity_rows = [
            (float(settlements[account_id].get(
                "local_economy", {}).get("production_capacity", 0.0)),
             float(scale))
            for account_id, scale in community[
                "account_provisioning_scales"].items()
            if account_id in settlements]
        # capacityは「人の能力」ではなく生活基盤の生産強度。
        # scale加重により、合流前のΣ(scale×capacity)と合流後の
        # scale_total×capacity_afterを一致させる。scale=0の旧状態
        # だけは人口加重を後方互換fallbackとする。
        economy["production_capacity"] = _weighted_average(
            capacity_rows,
            _weighted_average([
                (float(row.get("local_economy", {}).get(
                    "production_capacity", 0.0)), weight)
                for row, weight in sources], 0.0))
        economy["community_trust"] = _weighted_average([
            (float(row.get("local_economy", {}).get("community_trust", 50.0)), weight)
            for row, weight in sources], 50.0)
        economy["community_health"] = _weighted_average([
            (float(row.get("local_economy", {}).get("community_health", 80.0)), weight)
            for row, weight in sources], 80.0)
        # 生産経験は在庫のような外延量ではない。分裂元が一つなら同じ強度を
        # 両方へ継承し、複数共同体が合流したときだけ人口加重平均する。
        economy["production_practice_by_good"] = {
            good: _weighted_average([
                (normalize_production_practice(
                    row.get("local_economy", {}).get(
                        "production_practice_by_good"))[good], weight)
                for row, weight in sources], 0.0)
            for good in PRACTICE_GOODS
        }
        community["population_stage"] = max((
            int(row.get("stage", 0)) for row, _ in sources), default=0)
        economy["local_credit_stage"] = max((
            int(row.get("local_economy", {}).get("local_credit_stage", 0))
            for row, _ in sources), default=0)
        economy["barter_stage"] = max((
            int(row.get("local_economy", {}).get("barter_stage", 0))
            for row, _ in sources), default=0)
        worst_source = max(sources, key=lambda item: float(
            item[0].get("local_economy", {}).get("worst_shortfall", 0.0)),
            default=None)
        if worst_source:
            source_economy = worst_source[0].get("local_economy", {})
            economy["worst_shortfall"] = float(
                source_economy.get("worst_shortfall", 0.0))
            economy["worst_good"] = source_economy.get("worst_good")

    world_totals = {
        "population": sum(int(row.get("population", 0))
                          for row in settlements.values()),
        "reproductive_population": sum(int(row.get(
            "reproductive_population", 0)) for row in settlements.values()),
        "productive_population": sum(int(
            upgrade_settlement_demography(row)["productive_population"])
            for row in settlements.values()),
        "age_cohorts": {
            key: sum(int(upgrade_settlement_demography(row)[
                "age_cohorts"][key]) for row in settlements.values())
            for key in AGE_COHORT_KEYS},
        "age_transition_carry": {
            key: round(sum(float(upgrade_settlement_demography(row)[
                "age_transition_carry"][key])
                for row in settlements.values()), 12)
            for key in AGE_TRANSITION_CARRY_KEYS},
        **{field: round(sum(
            (normalize_provisioning_scale(
                row.get("local_economy", {}).get("provisioning_scale"))
             if field == "provisioning_scale" else
             float(row.get("local_economy", {}).get(field, 0.0)))
            for row in settlements.values()), _account_digits(field))
           for field in FLOAT_ACCOUNT_FIELDS},
        **{field: sum(int(row.get("local_economy", {}).get(field, 0))
                      for row in settlements.values())
           for field in INTEGER_ACCOUNT_FIELDS},
        **{field: round(sum(float(row.get(field, 0.0))
                            for row in settlements.values()), 12)
           for field in FLOAT_SETTLEMENT_FIELDS},
        **{field: sum(int(row.get(field, 0)) for row in settlements.values())
           for field in INTEGER_SETTLEMENT_FIELDS},
    }
    ledger = {
        "version": ACTIVITY_COMMUNITY_LEDGER_VERSION,
        "updated_turn": int(turn), "communities": communities,
        "provisional_clusters": {
            cluster_id: {
                "id": cluster_id,
                "accounting_parent_id": cluster.get(
                    "accounting_parent_id"),
                "site_count": int(cluster.get("site_count", 0)),
                "resident_count": int(cluster.get("resident_count", 0)),
                "centroid_x": cluster.get("centroid_x"),
                "centroid_y": cluster.get("centroid_y"),
            }
            for cluster_id, cluster in sorted(clusters.items())
            if cluster.get("provisional", False)
        },
        "resident_communities": resident_communities,
        "household_communities": household_communities,
        "unassigned_accounts": unassigned_accounts,
        "world_totals": world_totals,
    }
    if not activity_community_ledger_matches_sources(
            ledger, spatial_state, registry, settlements):
        raise RuntimeError("activity community ledger violates source conservation")
    return ledger


def activity_community_ledger_matches_sources(
        ledger: dict, spatial_state: dict, registry: dict,
        settlements: dict) -> bool:
    communities = ledger.get("communities", {})
    unassigned = ledger.get("unassigned_accounts", {})
    living_count = sum(
        1 for row in registry.get("residents", {}).values()
        if row.get("alive", True))
    registry_population = living_count + sum(
        max(0, int(value)) for value in registry.get(
            "anonymous_population_by_settlement", {}).values())
    expected_communities = {
        cluster_id for cluster_id, cluster
        in spatial_state.get("clusters", {}).items()
        if not cluster.get("provisional", False)}
    if set(communities) != expected_communities:
        return False
    site_to_cluster = _site_to_cluster(spatial_state)
    expected_populations = {cluster_id: 0 for cluster_id in communities}
    for site_id, cluster_id in site_to_cluster.items():
        site = spatial_state.get("sites", {}).get(site_id, {})
        expected_populations[cluster_id] += int(
            site.get("population_weight", 0))
    if any(expected_populations.get(cluster_id, 0)
           != int(community.get("population", 0))
           for cluster_id, community in communities.items()):
        return False
    source_totals = ledger.get("world_totals", {})
    expected_population = sum(
        int(row.get("population", 0)) for row in settlements.values())
    expected_reproductive = sum(
        int(row.get("reproductive_population", 0))
        for row in settlements.values())
    upgraded_settlements = {
        settlement_id: upgrade_settlement_demography(row)
        for settlement_id, row in settlements.items()}
    expected_productive = sum(
        int(row["productive_population"])
        for row in upgraded_settlements.values())
    if source_totals.get("population") != expected_population:
        return False
    if registry_population != expected_population:
        return False
    if source_totals.get("reproductive_population") != expected_reproductive:
        return False
    if source_totals.get("productive_population") != expected_productive:
        return False
    population_total = (
        sum(int(row.get("population", 0)) for row in communities.values())
        + sum(int(row.get("population", 0)) for row in unassigned.values()))
    reproductive_total = (
        sum(int(row.get("reproductive_population", 0))
            for row in communities.values())
        + sum(int(row.get("reproductive_population", 0))
              for row in unassigned.values()))
    if population_total != source_totals.get("population"):
        return False
    if reproductive_total != source_totals.get("reproductive_population"):
        return False
    productive_total = (
        sum(int(row.get("productive_population", 0))
            for row in communities.values())
        + sum(int(row.get("productive_population", 0))
              for row in unassigned.values()))
    if productive_total != source_totals.get("productive_population"):
        return False
    for cohort_key in AGE_COHORT_KEYS:
        expected = sum(int(row["age_cohorts"][cohort_key])
                       for row in upgraded_settlements.values())
        projected = (
            sum(int(row.get("age_cohorts", {}).get(cohort_key, 0))
                for row in communities.values())
            + sum(int(row.get("age_cohorts", {}).get(cohort_key, 0))
                  for row in unassigned.values()))
        if (expected != projected
                or int(source_totals.get("age_cohorts", {}).get(
                    cohort_key, -1)) != expected):
            return False
    for carry_key in AGE_TRANSITION_CARRY_KEYS:
        expected = round(sum(float(
            row["age_transition_carry"][carry_key])
            for row in upgraded_settlements.values()), 12)
        projected = round(
            sum(float(row.get("age_transition_carry", {}).get(carry_key, 0.0))
                for row in communities.values())
            + sum(float(row.get("age_transition_carry", {}).get(
                carry_key, 0.0)) for row in unassigned.values()), 12)
        if (abs(projected - expected) > 1e-9
                or abs(float(source_totals.get(
                    "age_transition_carry", {}).get(carry_key, 0.0))
                       - expected) > 1e-9):
            return False
    for field in FLOAT_ACCOUNT_FIELDS:
        expected = round(sum(
            (normalize_provisioning_scale(
                row.get("local_economy", {}).get("provisioning_scale"))
             if field == "provisioning_scale" else
             float(row.get("local_economy", {}).get(field, 0.0)))
            for row in settlements.values()), _account_digits(field))
        tolerance = 1e-9 if field == "provisioning_scale" else 1e-5
        if abs(float(source_totals.get(field, 0.0)) - expected) > tolerance:
            return False
        projected = round(
            sum(float(row.get("local_economy", {}).get(field, 0.0))
                for row in communities.values())
            + sum(float(row.get("local_economy", {}).get(field, 0.0))
                  for row in unassigned.values()), _account_digits(field))
        if abs(projected - float(
                source_totals.get(field, 0.0))) > tolerance:
            return False
    for field in INTEGER_ACCOUNT_FIELDS:
        expected = sum(int(row.get("local_economy", {}).get(field, 0))
                       for row in settlements.values())
        if int(source_totals.get(field, 0)) != expected:
            return False
        projected = (
            sum(int(row.get("local_economy", {}).get(field, 0))
                for row in communities.values())
            + sum(int(row.get("local_economy", {}).get(field, 0))
                  for row in unassigned.values()))
        if projected != int(source_totals.get(field, 0)):
            return False
    for field in FLOAT_SETTLEMENT_FIELDS:
        expected = round(sum(float(row.get(field, 0.0))
                             for row in settlements.values()), 12)
        if abs(float(source_totals.get(field, 0.0)) - expected) > 1e-9:
            return False
        projected = round(
            sum(float(row.get(field, 0.0)) for row in communities.values())
            + sum(float(row.get(field, 0.0)) for row in unassigned.values()), 12)
        if abs(projected - expected) > 1e-9:
            return False
    for field in INTEGER_SETTLEMENT_FIELDS:
        expected = sum(int(row.get(field, 0)) for row in settlements.values())
        projected = (
            sum(int(row.get(field, 0)) for row in communities.values())
            + sum(int(row.get(field, 0)) for row in unassigned.values()))
        if projected != expected or int(source_totals.get(field, 0)) != expected:
            return False
    # 強度値は世界合計を保存する対象ではなく、共同体ごとの由来に対して
    # 人口加重平均されていることを検証する。
    for community in communities.values():
        sources = [
            (settlements[account_id], int(weight))
            for account_id, weight in community.get(
                "account_populations", {}).items()
            if account_id in settlements]
        actual = normalize_production_practice(
            community.get("local_economy", {}).get(
                "production_practice_by_good"))
        capacity_rows = [
            (float(settlements[account_id].get(
                "local_economy", {}).get("production_capacity", 0.0)),
             float(scale))
            for account_id, scale in community.get(
                "account_provisioning_scales", {}).items()
            if account_id in settlements]
        expected_capacity = _weighted_average(
            capacity_rows,
            _weighted_average([
                (float(row.get("local_economy", {}).get(
                    "production_capacity", 0.0)), weight)
                for row, weight in sources], 0.0))
        actual_capacity = float(community.get(
            "local_economy", {}).get("production_capacity", 0.0))
        if abs(actual_capacity - expected_capacity) > 1e-6:
            return False
        for good in PRACTICE_GOODS:
            expected = _weighted_average([
                (normalize_production_practice(
                    row.get("local_economy", {}).get(
                        "production_practice_by_good"))[good], weight)
                for row, weight in sources], 0.0)
            if abs(actual[good] - expected) > 1e-6:
                return False
    for account_id, reserve in unassigned.items():
        if account_id not in settlements:
            return False
        expected = normalize_production_practice(
            settlements[account_id].get("local_economy", {}).get(
                "production_practice_by_good"))
        actual = normalize_production_practice(
            reserve.get("local_economy", {}).get(
                "production_practice_by_good"))
        if actual != expected:
            return False
        expected_capacity = float(settlements[account_id].get(
            "local_economy", {}).get("production_capacity", 0.0))
        actual_capacity = float(reserve.get(
            "local_economy", {}).get("production_capacity", 0.0))
        if abs(actual_capacity - expected_capacity) > 1e-6:
            return False
    resident_communities = ledger.get("resident_communities", {})
    return (set(resident_communities) == {
        resident_id for resident_id, row in registry.get("residents", {}).items()
        if row.get("alive", True)}
        and set(resident_communities.values()).issubset(communities))


def promote_activity_communities(ledger: dict, registry: dict) -> dict:
    """共同体投影を実際のsettlement会計と住民所属へ昇格する。

    現存クラスタは同じIDのsettlementとなる。住民を失った会計区分は土地在庫を
    消さないため人口0のreserveとして残す。入力は変更しない。
    """
    communities = ledger.get("communities", {})
    settlements = {}
    for cluster_id, community in sorted(communities.items()):
        settlements[cluster_id] = {
            "id": cluster_id, "name": community["name"],
            "population": int(community["population"]),
            "reproductive_population": int(
                community["reproductive_population"]),
            "productive_population": int(
                community["productive_population"]),
            "age_cohorts": copy.deepcopy(community["age_cohorts"]),
            "age_transition_carry": copy.deepcopy(
                community["age_transition_carry"]),
            "demography_version": DEMOGRAPHY_VERSION,
            "stage": int(community.get("population_stage", 0)),
            "founded_turn": int(community.get("formed_turn", 1)),
            **{key: copy.deepcopy(community.get(key, 0.0))
               for key in FLOAT_SETTLEMENT_FIELDS},
            **{key: int(community.get(key, 0))
               for key in INTEGER_SETTLEMENT_FIELDS},
            "local_economy": copy.deepcopy(community["local_economy"]),
        }
    for account_id, reserve in sorted(
            ledger.get("unassigned_accounts", {}).items()):
        if account_id in settlements:
            raise ValueError(f"active community collides with reserve: {account_id}")
        settlements[account_id] = {
            "id": account_id,
            "name": reserve.get("name", account_id),
            "population": int(reserve.get("population", 0)),
            "reproductive_population": int(
                reserve.get("reproductive_population", 0)),
            "productive_population": int(
                reserve.get("productive_population", 0)),
            "age_cohorts": copy.deepcopy(reserve["age_cohorts"]),
            "age_transition_carry": copy.deepcopy(
                reserve["age_transition_carry"]),
            "demography_version": DEMOGRAPHY_VERSION,
            "stage": int(reserve.get("population_stage", 4)),
            "founded_turn": int(reserve.get("founded_turn", 1)),
            **{key: copy.deepcopy(reserve.get(key, 0.0))
               for key in FLOAT_SETTLEMENT_FIELDS},
            **{key: int(reserve.get(key, 0))
               for key in INTEGER_SETTLEMENT_FIELDS},
            "local_economy": copy.deepcopy(reserve["local_economy"]),
        }

    after_registry = copy.deepcopy(registry)
    resident_communities = ledger.get("resident_communities", {})
    household_communities = dict(ledger.get("household_communities", {}))
    for resident_id, cluster_id in resident_communities.items():
        resident = after_registry["residents"].get(resident_id)
        if resident is None or not resident.get("alive", True):
            continue
        resident["settlement_id"] = cluster_id
        household_id = resident["household_id"]
        previous = household_communities.get(household_id, cluster_id)
        if previous != cluster_id:
            raise ValueError(
                f"one household spans activity communities: {household_id}")
    for household_id, cluster_id in household_communities.items():
        household = after_registry["households"].get(household_id)
        if household is not None:
            household["settlement_id"] = cluster_id

    named_counts = {}
    for resident in after_registry.get("residents", {}).values():
        if not resident.get("alive", True):
            continue
        account_id = str(resident.get("settlement_id"))
        named_counts[account_id] = named_counts.get(account_id, 0) + 1
    anonymous = {}
    for account_id, settlement in settlements.items():
        remainder = int(settlement.get("population", 0)) - named_counts.get(
            str(account_id), 0)
        if remainder < 0:
            raise ValueError(
                f"named residents exceed promoted population: {account_id}")
        if remainder:
            anonymous[str(account_id)] = remainder
    after_registry["anonymous_population_by_settlement"] = anonymous
    return {"settlements": settlements, "registry": after_registry}
