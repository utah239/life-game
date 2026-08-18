# -*- coding: utf-8 -*-
"""生産年齢人口・活動密度から成立する組織層。"""
import copy
import json
import random
import unittest
from unittest import mock

import game
import offline_simulation
from dashboard import aquarium_stream, build_dashboard
from institutions import (
    activity_communities, activity_economy, organizations, residents,
    settlement_network, spatial,
)


HOME_ECONOMY = {
    "food": 90.0, "medicine": 92.0, "shelter": 70.0, "tools": 65.0,
    "production_capacity": 60.0, "barter_stage": 0,
    "community_trust": 50.0, "local_credit_stage": 0,
}


class OrganizationRulesTest(unittest.TestCase):
    def setUp(self):
        self.settlements = settlement_network.initial_settlement_network(
            HOME_ECONOMY)
        self.registry = residents.initial_resident_registry(
            self.settlements, world_seed=31)
        self.spatial = spatial.initial_spatial_state(self.registry, turn=1)
        activity = activity_economy.plan_activity_economy(
            None, self.registry, self.spatial, self.settlements,
            {settlement_id: {
                good: 1.0 for good in activity_economy.ACTIVITY_GOODS}
             for settlement_id in self.settlements}, 1)
        self.registry = activity["registry"]
        self.spatial = spatial.synchronize_spatial_state(
            self.spatial, self.registry, 1,
            activity_economy_state=activity["state"])
        self.ledger = activity_communities.build_activity_community_ledger(
            self.spatial, self.registry, self.settlements, 1)

    def plan(self, state=None, **stages):
        return organizations.plan_organization_turn(
            state, self.ledger, self.spatial, 1,
            bank_stage=stages.get("bank_stage", 0),
            currency_stage=stages.get("currency_stage", 0),
            enforcement_stage=stages.get("enforcement_stage", 0),
            resident_registry=self.registry)

    def test_formal_company_requires_functioning_credit_and_institutions(self):
        formal = self.plan()
        degraded = self.plan(enforcement_stage=2)
        formal_kinds = {
            row["kind"] for row in organizations.active_organizations(formal)}
        degraded_kinds = {
            row["kind"] for row in organizations.active_organizations(degraded)}
        self.assertIn(organizations.ORGANIZATION_KIND_COMPANY, formal_kinds)
        self.assertNotIn(organizations.ORGANIZATION_KIND_COMPANY, degraded_kinds)
        self.assertTrue(degraded_kinds & {
            organizations.ORGANIZATION_KIND_GUILD,
            organizations.ORGANIZATION_KIND_FAMILY_WORKSHOP})

    def test_company_reforms_without_losing_identity_or_site_lineage(self):
        formal = self.plan()
        degraded = organizations.plan_organization_turn(
            formal, self.ledger, self.spatial, 2,
            bank_stage=0, currency_stage=0, enforcement_stage=2,
            resident_registry=self.registry)
        prior_companies = {
            row["id"]: row for row in organizations.active_organizations(formal)
            if row["kind"] == organizations.ORGANIZATION_KIND_COMPANY}
        reformed = [
            row for row in degraded["events"]
            if row["kind"] == "organization_reformed"]
        self.assertTrue(reformed)
        for event in reformed:
            self.assertIn(event["organization_id"], prior_companies)
            after = degraded["organizations"][event["organization_id"]]
            self.assertEqual(
                after["site_ids"],
                prior_companies[event["organization_id"]]["site_ids"])
            self.assertEqual(
                after["founded_turn"],
                prior_companies[event["organization_id"]]["founded_turn"])

    def test_company_reformation_requires_sustained_stability(self):
        formal = self.plan()
        target = next(
            row for row in organizations.active_organizations(formal)
            if row["kind"] == organizations.ORGANIZATION_KIND_COMPANY)
        state = organizations.plan_organization_turn(
            formal, self.ledger, self.spatial, 2,
            bank_stage=0, currency_stage=0, enforcement_stage=2,
            resident_registry=self.registry)
        self.assertNotEqual(
            state["organizations"][target["id"]]["kind"],
            organizations.ORGANIZATION_KIND_COMPANY)
        reform_turn = 2 + organizations.COMPANY_REFORM_STABILITY_TURNS
        for turn in range(3, reform_turn):
            state = organizations.plan_organization_turn(
                state, self.ledger, self.spatial, turn,
                bank_stage=0, currency_stage=0, enforcement_stage=0,
                resident_registry=self.registry)
            self.assertNotEqual(
                state["organizations"][target["id"]]["kind"],
                organizations.ORGANIZATION_KIND_COMPANY)
        state = organizations.plan_organization_turn(
            state, self.ledger, self.spatial, reform_turn,
            bank_stage=0, currency_stage=0, enforcement_stage=0,
            resident_registry=self.registry)
        self.assertEqual(
            state["organizations"][target["id"]]["kind"],
            organizations.ORGANIZATION_KIND_COMPANY)

    def test_assembly_coexists_and_congregation_requires_social_stress(self):
        normal = self.plan()
        kinds = [row["kind"] for row in organizations.active_organizations(normal)]
        self.assertIn(organizations.ORGANIZATION_KIND_ASSEMBLY, kinds)
        self.assertNotIn(organizations.ORGANIZATION_KIND_CONGREGATION, kinds)

        stressed_ledger = copy.deepcopy(self.ledger)
        first = next(iter(stressed_ledger["communities"].values()))
        first["local_economy"]["community_health"] = 40.0
        stressed = organizations.plan_organization_turn(
            None, stressed_ledger, self.spatial, 1,
            bank_stage=0, currency_stage=0, enforcement_stage=0,
            resident_registry=self.registry)
        self.assertIn(
            organizations.ORGANIZATION_KIND_CONGREGATION,
            {row["kind"] for row in organizations.active_organizations(stressed)})

    def test_size_and_trust_stage_are_independent_fields(self):
        state = self.plan()
        rows = organizations.active_organizations(state)
        self.assertTrue(rows)
        self.assertTrue(all(row["size"] in {
            "micro", "small", "medium", "large"} for row in rows))
        self.assertTrue(all(row["trust_stage"] in range(4) for row in rows))
        self.assertTrue(all(
            row["productive_member_count"] <= row["member_count"]
            for row in rows))
        self.assertTrue(all(
            0.0 <= row["operating_reserve"]
            <= organizations.ORGANIZATION_OPERATING_RESERVE_CAP
            for row in rows))
        self.assertTrue(all(row["operating_status"] ==
                            organizations.ORGANIZATION_OPERATING_STABLE
                            for row in rows))
        self.assertTrue(all(row["operating_history"] for row in rows))

    def test_planning_is_deterministic_rng_free_and_non_mutating(self):
        before = copy.deepcopy((self.ledger, self.spatial, self.registry))
        rng = random.getstate()
        first = self.plan()
        second = self.plan()
        self.assertEqual(first, second)
        self.assertEqual((self.ledger, self.spatial, self.registry), before)
        self.assertEqual(random.getstate(), rng)

    def test_company_uses_distinct_entry_and_retention_thresholds(self):
        formal = self.plan()
        company = next(
            row for row in organizations.active_organizations(formal)
            if row["kind"] == organizations.ORGANIZATION_KIND_COMPANY)
        ledger = copy.deepcopy(self.ledger)
        community = ledger["communities"][
            company["home_activity_cluster_id"]]
        community["productive_population"] = 20
        community["site_count"] = 3
        community["local_economy"]["community_trust"] = 40.0

        retained = organizations.plan_organization_turn(
            formal, ledger, self.spatial, 2, bank_stage=0,
            currency_stage=0, enforcement_stage=0,
            resident_registry=self.registry)
        cold_start = organizations.plan_organization_turn(
            None, ledger, self.spatial, 2, bank_stage=0,
            currency_stage=0, enforcement_stage=0,
            resident_registry=self.registry)
        self.assertEqual(
            retained["organizations"][company["id"]]["kind"],
            organizations.ORGANIZATION_KIND_COMPANY)
        self.assertNotEqual(
            cold_start["organizations"][company["id"]]["kind"],
            organizations.ORGANIZATION_KIND_COMPANY)

    def test_guild_assembly_and_congregation_have_retention_bands(self):
        formal = self.plan()
        degraded = organizations.plan_organization_turn(
            formal, self.ledger, self.spatial, 2, bank_stage=0,
            currency_stage=0, enforcement_stage=2,
            resident_registry=self.registry)
        guild = next(
            row for row in organizations.active_organizations(degraded)
            if row["kind"] == organizations.ORGANIZATION_KIND_GUILD)
        guild_ledger = copy.deepcopy(self.ledger)
        guild_community = guild_ledger["communities"][
            guild["home_activity_cluster_id"]]
        guild_community["productive_population"] = 19
        retained_guild = organizations.plan_organization_turn(
            degraded, guild_ledger, self.spatial, 3, bank_stage=0,
            currency_stage=0, enforcement_stage=2,
            resident_registry=self.registry)
        cold_guild = organizations.plan_organization_turn(
            None, guild_ledger, self.spatial, 3, bank_stage=0,
            currency_stage=0, enforcement_stage=2,
            resident_registry=self.registry)
        self.assertEqual(
            retained_guild["organizations"][guild["id"]]["kind"],
            organizations.ORGANIZATION_KIND_GUILD)
        self.assertEqual(
            cold_guild["organizations"][guild["id"]]["kind"],
            organizations.ORGANIZATION_KIND_FAMILY_WORKSHOP)

        civic = next(
            row for row in organizations.active_organizations(formal)
            if row["kind"] == organizations.ORGANIZATION_KIND_ASSEMBLY)
        civic_ledger = copy.deepcopy(self.ledger)
        civic_ledger["communities"][
            civic["home_activity_cluster_id"]]["productive_population"] = 7
        retained_civic = organizations.plan_organization_turn(
            formal, civic_ledger, self.spatial, 2, bank_stage=0,
            currency_stage=0, enforcement_stage=0,
            resident_registry=self.registry)
        cold_civic = organizations.plan_organization_turn(
            None, civic_ledger, self.spatial, 2, bank_stage=0,
            currency_stage=0, enforcement_stage=0,
            resident_registry=self.registry)
        self.assertIn(civic["id"], retained_civic["organizations"])
        self.assertNotIn(civic["id"], {
            row["id"] for row in organizations.active_organizations(
                cold_civic)})

        stressed_ledger = copy.deepcopy(self.ledger)
        community_id = next(iter(stressed_ledger["communities"]))
        stressed_ledger["communities"][community_id][
            "local_economy"]["community_health"] = 40.0
        stressed = organizations.plan_organization_turn(
            None, stressed_ledger, self.spatial, 1, bank_stage=0,
            currency_stage=0, enforcement_stage=0,
            resident_registry=self.registry)
        care_id = f"org:{community_id}:care"
        recovering_ledger = copy.deepcopy(stressed_ledger)
        recovering_ledger["communities"][community_id][
            "local_economy"]["community_health"] = 58.0
        retained_care = organizations.plan_organization_turn(
            stressed, recovering_ledger, self.spatial, 2, bank_stage=0,
            currency_stage=0, enforcement_stage=0,
            resident_registry=self.registry)
        cold_care = organizations.plan_organization_turn(
            None, recovering_ledger, self.spatial, 2, bank_stage=0,
            currency_stage=0, enforcement_stage=0,
            resident_registry=self.registry)
        self.assertTrue(retained_care["organizations"][care_id]["active"])
        self.assertNotIn(care_id, {
            row["id"] for row in organizations.active_organizations(cold_care)})

    def test_sector_capacity_is_actual_activity_share(self):
        state = self.plan()
        row = next(
            row for row in organizations.active_organizations(state)
            if row["purpose"] in {"food", "medicine", "shelter", "tools"})
        community = self.ledger["communities"][
            row["home_activity_cluster_id"]]
        production = community["local_economy"]["production_capacity"]
        community_workers = sum(
            self.spatial["sites"][site_id]["activity_worker_count"]
            for site_id in community["site_ids"])
        expected = round(
            production * row["workforce_count"] / community_workers, 6)
        old_eligible_formula = round(
            production * row["productive_member_count"]
            / community["productive_population"], 6)
        self.assertEqual(row["capacity"], expected)
        self.assertEqual(
            row["workforce_community_activity_share"],
            round(row["workforce_count"] / community_workers, 6))
        self.assertNotEqual(row["capacity"], old_eligible_formula)

    def test_sector_capacity_includes_community_production_practice(self):
        ledger = copy.deepcopy(self.ledger)
        for community in ledger["communities"].values():
            community["local_economy"]["production_practice_by_good"] = {
                good: 100.0 for good in
                ("food", "medicine", "shelter", "tools")}
        state = organizations.plan_organization_turn(
            None, ledger, self.spatial, 1,
            bank_stage=0, currency_stage=0, enforcement_stage=0,
            resident_registry=self.registry)
        row = next(
            row for row in organizations.active_organizations(state)
            if row["purpose"] in {"food", "medicine", "shelter", "tools"})
        community = ledger["communities"][
            row["home_activity_cluster_id"]]
        community_workers = sum(
            self.spatial["sites"][site_id]["activity_worker_count"]
            for site_id in community["site_ids"])
        expected = round(
            community["local_economy"]["production_capacity"]
            * row["workforce_count"] / community_workers * 1.2, 6)
        self.assertEqual(row["production_practice_score"], 100.0)
        self.assertEqual(row["production_practice_factor"], 1.2)
        self.assertEqual(row["capacity"], expected)
        self.assertTrue(all(
            civic["production_practice_factor"] == 1.0
            for civic in organizations.active_organizations(state)
            if civic["kind"] not in organizations.ORGANIZATION_WORKFORCE_KINDS))

    def test_named_members_follow_household_activity_sites_without_recounting_population(self):
        state = self.plan()
        sector_rows = [
            row for row in organizations.active_organizations(state)
            if row["kind"] in organizations.ORGANIZATION_WORKFORCE_KINDS]
        self.assertTrue(sector_rows)
        seen_workers = set()
        for row in sector_rows:
            household_ids = {
                self.spatial["sites"][site_id]["household_id"]
                for site_id in row["site_ids"]}
            expected_members = sorted(
                resident["id"]
                for resident in self.registry["residents"].values()
                if resident.get("alive", True)
                and resident["household_id"] in household_ids)
            self.assertEqual(
                row["named_affiliated_resident_ids"], expected_members)
            self.assertEqual(row["named_member_ids"],
                             row["named_productive_member_ids"])
            self.assertEqual(row["participant_count"],
                             row["productive_member_count"])
            self.assertEqual(
                row["productive_member_count"],
                len(row["named_productive_member_ids"])
                + row["unrepresented_productive_member_count"])
            self.assertEqual(row["eligible_workforce_count"],
                             row["productive_member_count"])
            self.assertEqual(row["workforce_count"],
                             len(row["named_worker_ids"])
                             + row["unrepresented_workforce_count"])
            self.assertTrue(set(row["named_worker_ids"]).issubset(
                row["named_productive_member_ids"]))
            self.assertTrue(all(
                15 <= residents.resident_age_years(
                    self.registry["residents"][resident_id], 1) <= 64
                for resident_id in row["named_worker_ids"]))
            self.assertTrue(all(
                self.registry["residents"][resident_id][
                    "last_activity_turn"] == 1
                and self.registry["residents"][resident_id][
                    "last_activity"] == row["purpose"]
                for resident_id in row["named_worker_ids"]))
            self.assertFalse(seen_workers & set(row["named_worker_ids"]))
            seen_workers.update(row["named_worker_ids"])
        for community_id, community in self.ledger["communities"].items():
            organized = sum(
                row["workforce_count"] for row in sector_rows
                if row["home_activity_cluster_id"] == community_id)
            available = sum(
                self.spatial["sites"][site_id]["activity_worker_count"]
                for site_id in community["site_ids"])
            self.assertLessEqual(organized, available)

    def test_company_reform_preserves_people_but_changes_work_relationship(self):
        formal = self.plan()
        company = next(
            row for row in organizations.active_organizations(formal)
            if row["kind"] == organizations.ORGANIZATION_KIND_COMPANY)
        degraded = organizations.plan_organization_turn(
            formal, self.ledger, self.spatial, 2,
            bank_stage=0, currency_stage=0, enforcement_stage=2,
            resident_registry=self.registry)
        after = degraded["organizations"][company["id"]]
        self.assertEqual(after["named_affiliated_resident_ids"],
                         company["named_affiliated_resident_ids"])
        self.assertEqual(company["work_relationship"], "employee")
        self.assertIn(after["work_relationship"], {
            "guild_member", "household_worker"})

    def test_cohort_mode_keeps_unrepresented_workers_as_aggregate_count(self):
        registry = residents.initial_resident_registry(
            self.settlements, world_seed=31, named_resident_limit=10)
        spatial_state = spatial.initial_spatial_state(registry, turn=1)
        activity = activity_economy.plan_activity_economy(
            None, registry, spatial_state, self.settlements,
            {settlement_id: {
                good: 1.0 for good in activity_economy.ACTIVITY_GOODS}
             for settlement_id in self.settlements}, 1)
        registry = activity["registry"]
        spatial_state = spatial.synchronize_spatial_state(
            spatial_state, registry, 1,
            activity_economy_state=activity["state"])
        ledger = activity_communities.build_activity_community_ledger(
            spatial_state, registry, self.settlements, 1)
        state = organizations.plan_organization_turn(
            None, ledger, spatial_state, 1, bank_stage=0,
            currency_stage=0, enforcement_stage=0,
            resident_registry=registry)
        rows = organizations.active_organizations(state)
        self.assertTrue(registry["cohort_mode"])
        self.assertTrue(any(
            row["unrepresented_productive_member_count"] > 0
            for row in rows))
        self.assertTrue(all(
            row["productive_member_count"]
            == len(row["named_productive_member_ids"])
            + row["unrepresented_productive_member_count"]
            for row in rows))
        workforce_rows = [
            row for row in rows
            if row["kind"] in organizations.ORGANIZATION_WORKFORCE_KINDS]
        self.assertTrue(any(
            row["unrepresented_workforce_count"] > 0
            for row in workforce_rows))
        self.assertTrue(all(
            row["workforce_count"]
            == len(row["named_worker_ids"])
            + row["unrepresented_workforce_count"]
            for row in workforce_rows))

    def test_zero_activity_keeps_affiliation_but_stops_sector_workforce(self):
        spatial_state = copy.deepcopy(self.spatial)
        for site in spatial_state["sites"].values():
            site["activity_worker_count"] = 0
            site["activity_named_worker_count"] = 0
            site["activity_anonymous_worker_count"] = 0
            site["activity_worker_count_by_good"] = {}
        ledger = activity_communities.build_activity_community_ledger(
            spatial_state, self.registry, self.settlements, 1)
        state = organizations.plan_organization_turn(
            None, ledger, spatial_state, 1, bank_stage=0,
            currency_stage=0, enforcement_stage=0,
            resident_registry=self.registry)
        workforce_rows = [
            row for row in organizations.active_organizations(state)
            if row["kind"] in organizations.ORGANIZATION_WORKFORCE_KINDS]
        self.assertTrue(any(
            row["productive_member_count"] > 0 for row in workforce_rows))
        self.assertTrue(all(
            row["workforce_count"] == 0
            and row["named_worker_ids"] == []
            and row["capacity"] == 0.0
            for row in workforce_rows))
        effects = organizations.plan_organization_effects(state)
        self.assertFalse(any(
            any(field.endswith("_delta") and field.split("_", 1)[0]
                in activity_economy.ACTIVITY_GOODS
                for field in deltas)
            for deltas in effects["settlements"].values()))

    def test_legacy_workforce_state_upgrades_without_mutating_input(self):
        current = self.plan()
        legacy = copy.deepcopy(current)
        legacy["version"] = organizations.LEGACY_ORGANIZATION_STATE_VERSION
        for row in legacy["organizations"].values():
            for key in (
                    "eligible_workforce_count",
                    "unrepresented_workforce_count",
                    "workforce_affiliation_ratio",
                    "workforce_community_activity_share",
                    "production_practice_score",
                    "production_practice_factor"):
                row.pop(key, None)
        before = copy.deepcopy(legacy)
        upgraded = organizations.upgrade_organization_state(legacy)
        self.assertEqual(legacy, before)
        self.assertEqual(
            upgraded["version"], organizations.ORGANIZATION_STATE_VERSION)
        self.assertTrue(all(
            "eligible_workforce_count" in row
            and "unrepresented_workforce_count" in row
            and row["production_practice_score"] == 0.0
            and row["production_practice_factor"] == 1.0
            for row in upgraded["organizations"].values()))

    def test_legacy_state_without_operating_fields_upgrades_in_place(self):
        current = self.plan()
        legacy = copy.deepcopy(current)
        for row in legacy["organizations"].values():
            row.pop("operating_reserve", None)
            row.pop("operating_reserve_delta", None)
            row.pop("operating_status", None)
            row.pop("operating_history", None)

        legacy_effects = organizations.plan_organization_effects(legacy)
        full_runway = copy.deepcopy(legacy)
        for row in full_runway["organizations"].values():
            row["operating_reserve"] = (
                organizations.ORGANIZATION_OPERATING_RESERVE_INITIAL)
            row["operating_status"] = (
                organizations.ORGANIZATION_OPERATING_STABLE)
        self.assertEqual(
            legacy_effects,
            organizations.plan_organization_effects(full_runway))

        before_upgrade = copy.deepcopy(legacy)
        upgraded = organizations.plan_organization_turn(
            legacy, self.ledger, self.spatial, 2,
            bank_stage=0, currency_stage=0, enforcement_stage=0,
            resident_registry=self.registry)
        self.assertEqual(legacy, before_upgrade)
        for row in organizations.active_organizations(upgraded):
            self.assertIn("operating_reserve", row)
            self.assertIn("operating_status", row)
            self.assertTrue(row["operating_history"])

    def test_legacy_state_without_asset_claims_upgrades_without_seizing_goods(self):
        current = self.plan()
        legacy = copy.deepcopy(current)
        for key in (
                "asset_claim_acquisition_totals",
                "community_distribution_totals",
                "last_asset_allocation_turn", "last_asset_allocations"):
            legacy.pop(key, None)
        for row in legacy["organizations"].values():
            for key in (
                    "asset_claims", "asset_claim_total",
                    "asset_claim_coverage", "last_surplus_allocation"):
                row.pop(key, None)
        before = copy.deepcopy(legacy)
        upgraded = organizations.plan_organization_turn(
            legacy, self.ledger, self.spatial, 2,
            bank_stage=0, currency_stage=0, enforcement_stage=0,
            resident_registry=self.registry)
        self.assertEqual(legacy, before)
        self.assertTrue(all(
            row["asset_claims"]
            == organizations.initial_organization_asset_claims()
            for row in organizations.active_organizations(upgraded)))
        self.assertEqual(
            upgraded["asset_claim_acquisition_totals"],
            organizations.initial_organization_asset_claims())
        self.assertTrue(
            organizations.organization_asset_claims_match_communities(
                upgraded, self.ledger))


class OrganizationOperationTest(unittest.TestCase):
    def test_status_boundaries_are_hysteretic_and_recover_one_step(self):
        cases = (
            (organizations.ORGANIZATION_OPERATING_STABLE, 3.0,
             organizations.ORGANIZATION_OPERATING_STABLE),
            (organizations.ORGANIZATION_OPERATING_STABLE, 2.999,
             organizations.ORGANIZATION_OPERATING_STRAINED),
            (organizations.ORGANIZATION_OPERATING_STABLE, 0.999,
             organizations.ORGANIZATION_OPERATING_DORMANT),
            (organizations.ORGANIZATION_OPERATING_STRAINED, 3.999,
             organizations.ORGANIZATION_OPERATING_STRAINED),
            (organizations.ORGANIZATION_OPERATING_STRAINED, 4.0,
             organizations.ORGANIZATION_OPERATING_STABLE),
            (organizations.ORGANIZATION_OPERATING_DORMANT, 1.999,
             organizations.ORGANIZATION_OPERATING_DORMANT),
            (organizations.ORGANIZATION_OPERATING_DORMANT, 12.0,
             organizations.ORGANIZATION_OPERATING_STRAINED),
        )
        for current, reserve, expected in cases:
            with self.subTest(current=current, reserve=reserve):
                self.assertEqual(
                    organizations.organization_operating_status_next(
                        current, reserve), expected)

    def test_new_operation_starts_with_six_months_of_runway(self):
        operation = organizations.plan_organization_operation(
            None, organizations.ORGANIZATION_KIND_COMPANY,
            10.0, 80.0, organizations.ORGANIZATION_TRUST_HEALTHY, 0.0)
        self.assertEqual(operation, {
            "operating_reserve_before": 6.0,
            "operating_reserve_delta": 0.0,
            "operating_reserve": 6.0,
            "operating_status_before": "stable",
            "operating_status": "stable",
            "operating_transitioned": False,
            "member_continuity": 1.0,
            "turnover_cost": 0.0,
        })

    def test_member_and_site_turnover_reduce_operating_replenishment(self):
        continuity = organizations.organization_member_continuity(
            {"named_productive_member_ids": ["r1", "r2"],
             "site_ids": ["s1", "s2"]},
            ["r2", "r3"], ["s2", "s3"])
        self.assertEqual(continuity, 0.5)
        self.assertEqual(organizations.organization_member_continuity(
            {"site_ids": ["s1"]}, [], []), 1.0)
        operation = organizations.plan_organization_operation(
            {"operating_reserve": 6.0, "operating_status": "stable"},
            organizations.ORGANIZATION_KIND_COMPANY,
            10.0, 80.0, organizations.ORGANIZATION_TRUST_HEALTHY, 0.0,
            member_continuity=0.0)
        self.assertEqual(operation["member_continuity"], 0.0)
        self.assertEqual(operation["turnover_cost"], 0.01)
        self.assertEqual(operation["operating_reserve_delta"], -0.009)
        self.assertEqual(operation["operating_reserve"], 5.991)

    def test_effective_company_replenishes_and_stressed_company_goes_dormant(self):
        healthy = organizations.plan_organization_operation(
            {"operating_reserve": 6.0, "operating_status": "stable"},
            organizations.ORGANIZATION_KIND_COMPANY,
            10.0, 80.0, organizations.ORGANIZATION_TRUST_HEALTHY, 0.0)
        stressed = organizations.plan_organization_operation(
            {"operating_reserve": 1.02, "operating_status": "stable"},
            organizations.ORGANIZATION_KIND_COMPANY,
            10.0, 20.0, organizations.ORGANIZATION_TRUST_FRAGILE, 100.0)
        self.assertEqual(healthy["operating_reserve_delta"], 0.037)
        self.assertEqual(healthy["operating_reserve"], 6.037)
        self.assertEqual(stressed["operating_reserve_delta"], -0.0328)
        self.assertEqual(stressed["operating_reserve"], 0.9872)
        self.assertEqual(
            stressed["operating_status"],
            organizations.ORGANIZATION_OPERATING_DORMANT)
        self.assertTrue(stressed["operating_transitioned"])

    def test_dormant_operation_can_recover_but_only_to_strained(self):
        recovered = organizations.plan_organization_operation(
            {"operating_reserve": 1.99, "operating_status": "dormant"},
            organizations.ORGANIZATION_KIND_COMPANY,
            10.0, 100.0, organizations.ORGANIZATION_TRUST_HEALTHY, 0.0)
        self.assertEqual(recovered["operating_reserve_delta"], 0.0275)
        self.assertEqual(recovered["operating_reserve"], 2.0175)
        self.assertEqual(recovered["operating_status"], "strained")

    def test_operation_is_bounded_rng_free_and_non_mutating(self):
        previous = {"operating_reserve": 11.99,
                    "operating_status": "stable"}
        before = copy.deepcopy(previous)
        rng = random.getstate()
        operation = organizations.plan_organization_operation(
            previous, organizations.ORGANIZATION_KIND_FAMILY_WORKSHOP,
            3.0, 100.0, organizations.ORGANIZATION_TRUST_HEALTHY, 0.0)
        self.assertEqual(operation["operating_reserve"], 12.0)
        self.assertEqual(operation["operating_reserve_delta"], 0.01)
        self.assertEqual(previous, before)
        self.assertEqual(random.getstate(), rng)

    def test_operating_factor_is_backward_compatible_and_status_sensitive(self):
        self.assertEqual(organizations.organization_operating_factor({}), 1.0)
        self.assertEqual(organizations.organization_operating_factor({
            "operating_reserve": 3.0, "operating_status": "stable"}), 0.5)
        self.assertEqual(organizations.organization_operating_factor({
            "operating_reserve": 3.0, "operating_status": "strained"}), 0.25)
        self.assertEqual(organizations.organization_operating_factor({
            "operating_reserve": 6.0, "operating_status": "dormant"}), 0.0)


class OrganizationEffectsTest(unittest.TestCase):
    @staticmethod
    def state(*rows, updated_turn=7):
        state = organizations.initial_organization_state()
        state["updated_turn"] = updated_turn
        state["organizations"] = {row["id"]: row for row in rows}
        return state

    @staticmethod
    def row(identifier, kind, purpose, *, capacity=10.0, trust=80.0,
            trust_stage=0, community_id="cluster:a", active=True):
        return {
            "id": identifier, "kind": kind, "purpose": purpose,
            "capacity": capacity, "trust": trust,
            "trust_stage": trust_stage, "active": active,
            "productive_member_count": 8,
            "home_activity_cluster_id": community_id,
        }

    def test_each_organization_form_has_a_bounded_distinct_effect(self):
        cases = [
            (self.row("company", "company", "food"),
             {"food_delta": 0.024}),
            (self.row("guild", "guild", "tools"),
             {"tools_delta": 0.014}),
            (self.row("family", "family_workshop", "shelter"),
             {"shelter_delta": 0.005}),
            (self.row("assembly", "assembly", "shared_governance"),
             {"community_trust_delta": 0.0012,
              "production_capacity_delta": 0.0006}),
            (self.row("care", "congregation", "care_and_ritual"),
             {"community_health_delta": 0.0024,
              "medicine_delta": 0.002}),
        ]
        for row, expected in cases:
            with self.subTest(kind=row["kind"]):
                plan = organizations.plan_organization_effects(self.state(row))
                self.assertEqual(plan["settlements"]["cluster:a"], expected)
                self.assertEqual(plan["source_turn"], 7)
                self.assertEqual(plan["organization_count"], 1)

    def test_trust_stage_and_continuous_trust_scale_effects(self):
        healthy = self.row("healthy", "company", "food")
        strained = self.row(
            "strained", "company", "food", trust_stage=1)
        low_trust = self.row(
            "low", "company", "food", trust=40.0, community_id="cluster:b")
        plan = organizations.plan_organization_effects(
            self.state(healthy, strained, low_trust))
        self.assertEqual(plan["settlements"]["cluster:a"]["food_delta"], 0.042)
        self.assertEqual(plan["settlements"]["cluster:b"]["food_delta"], 0.012)

    def test_operating_runway_scales_and_dormancy_stops_surplus(self):
        stable = self.row("stable", "company", "food")
        stable.update({"operating_reserve": 3.0,
                       "operating_status": "stable"})
        strained = self.row("strained", "company", "food",
                            community_id="cluster:b")
        strained.update({"operating_reserve": 3.0,
                         "operating_status": "strained"})
        dormant = self.row("dormant", "company", "food",
                           community_id="cluster:c")
        dormant.update({"operating_reserve": 6.0,
                        "operating_status": "dormant"})
        plan = organizations.plan_organization_effects(
            self.state(stable, strained, dormant))
        self.assertEqual(plan["settlements"]["cluster:a"]["food_delta"], 0.012)
        self.assertEqual(plan["settlements"]["cluster:b"]["food_delta"], 0.006)
        self.assertNotIn("cluster:c", plan["settlements"])
        contributor = next(row for row in plan["contributors"]
                           if row["organization_id"] == "strained")
        self.assertEqual(contributor["operating_factor"], 0.25)

    def test_effects_are_capped_per_community(self):
        rows = (
            self.row("food", "company", "food", capacity=20000, trust=100),
            self.row("assembly", "assembly", "shared_governance",
                     capacity=20000, trust=100),
            self.row("care", "congregation", "care_and_ritual",
                     capacity=20000, trust=100),
        )
        deltas = organizations.plan_organization_effects(
            self.state(*rows))["settlements"]["cluster:a"]
        self.assertEqual(deltas, {
            "food_delta": 3.0,
            "community_trust_delta": 0.25,
            "production_capacity_delta": 0.12,
            "community_health_delta": 0.4,
            "medicine_delta": 3.0,
        })

    def test_effect_planning_and_recording_are_rng_free_and_non_mutating(self):
        state = self.state(self.row("company", "company", "food"))
        before = copy.deepcopy(state)
        rng = random.getstate()
        effects = organizations.plan_organization_effects(state)
        recorded = organizations.record_organization_effects(state, effects, 8)
        self.assertEqual(state, before)
        self.assertEqual(random.getstate(), rng)
        self.assertEqual(recorded["effect_totals"]["food_delta"], 0.024)
        self.assertEqual(recorded["effect_application_count"], 1)
        self.assertEqual(recorded["last_effect_turn"], 8)
        self.assertEqual(recorded["last_effects"], effects)

    def test_inactive_or_empty_state_has_no_effect(self):
        inactive = self.row(
            "ended", "company", "food", active=False)
        for state in (self.state(), self.state(inactive)):
            with self.subTest(state=state):
                plan = organizations.plan_organization_effects(state)
                self.assertEqual(plan["settlements"], {})
                self.assertEqual(plan["contributors"], [])


class OrganizationAssetClaimsTest(unittest.TestCase):
    @staticmethod
    def row(identifier, *, community_id="cluster:a", sites=None,
            predecessors=None, claims=None, kind="company"):
        return {
            "id": identifier, "kind": kind, "purpose": "food",
            "home_activity_cluster_id": community_id,
            "site_ids": list(sites or ()),
            "predecessor_ids": list(predecessors or ()),
            "active": True,
            "asset_claims": {
                **organizations.initial_organization_asset_claims(),
                **(claims or {}),
            },
        }

    @staticmethod
    def communities(**stocks):
        return {
            community_id: {"local_economy": {
                field: float(values.get(field, 100.0))
                for field in organizations.ORGANIZATION_ASSET_CLAIM_FIELDS}}
            for community_id, values in stocks.items()}

    def test_claims_are_an_interior_ownership_layer_and_bound_effect_bonus(self):
        self.assertEqual(organizations.organization_asset_factor({
            "kind": "company"}), 1.0)
        row = self.row("company", claims={"food": 6.0})
        self.assertEqual(organizations.organization_asset_factor(row), 1.15)
        effect_row = OrganizationEffectsTest.row(
            "company", "company", "food")
        effect_row["asset_claims"] = row["asset_claims"]
        plan = organizations.plan_organization_effects(
            OrganizationEffectsTest.state(effect_row))
        self.assertEqual(
            plan["settlements"]["cluster:a"]["food_delta"], 0.0276)
        self.assertEqual(plan["contributors"][0]["asset_factor"], 1.15)

    def test_realized_surplus_is_exactly_split_between_claim_and_community(self):
        row = OrganizationEffectsTest.row(
            "company", "company", "food")
        state = OrganizationEffectsTest.state(row)
        effects = organizations.plan_organization_effects(state)
        before = copy.deepcopy(state)
        rng = random.getstate()
        recorded = organizations.record_organization_effects(
            state, effects, 8)
        allocation = recorded["last_asset_allocations"][0]
        self.assertEqual(state, before)
        self.assertEqual(random.getstate(), rng)
        self.assertEqual(
            recorded["organizations"]["company"]["asset_claims"]["food"],
            0.006)
        self.assertEqual(
            allocation["asset_claims_acquired"]["food"], 0.006)
        self.assertEqual(
            allocation["community_distributed"]["food"], 0.018)
        self.assertEqual(
            allocation["realized_surplus"]["food"],
            allocation["asset_claims_acquired"]["food"]
            + allocation["community_distributed"]["food"])
        self.assertEqual(
            recorded["asset_claim_acquisition_totals"]["food"], 0.006)
        self.assertEqual(
            recorded["community_distribution_totals"]["food"], 0.018)

    def test_split_and_merge_preserve_claims_by_activity_site_lineage(self):
        previous = {
            "old": self.row(
                "old", sites=["s1", "s2"], claims={"tools": 10.0})}
        split = {
            "left": self.row(
                "left", community_id="left", sites=["s1"],
                predecessors=["old"]),
            "right": self.row(
                "right", community_id="right", sites=["s2"],
                predecessors=["old"]),
        }
        communities = self.communities(left={}, right={})
        before_previous = copy.deepcopy(previous)
        before_split = copy.deepcopy(split)
        divided = organizations.reconcile_organization_asset_claims(
            previous, split, communities)
        self.assertEqual(previous, before_previous)
        self.assertEqual(split, before_split)
        self.assertEqual(divided["left"]["asset_claims"]["tools"], 5.0)
        self.assertEqual(divided["right"]["asset_claims"]["tools"], 5.0)

        merged = {
            "merged": self.row(
                "merged", community_id="merged", sites=["s1", "s2"],
                predecessors=["left", "right"])}
        merged_rows = organizations.reconcile_organization_asset_claims(
            divided, merged, self.communities(merged={}))
        self.assertEqual(
            merged_rows["merged"]["asset_claims"]["tools"], 10.0)

    def test_split_including_retained_id_still_divides_claims(self):
        previous = {
            "old": self.row(
                "old", sites=["s1", "s2"], claims={"tools": 10.0})}
        split = {
            "old": self.row("old", community_id="left", sites=["s1"]),
            "new": self.row(
                "new", community_id="right", sites=["s2"],
                predecessors=["old"]),
        }

        divided = organizations.reconcile_organization_asset_claims(
            previous, split, self.communities(left={}, right={}))

        self.assertEqual(divided["old"]["asset_claims"]["tools"], 5.0)
        self.assertEqual(divided["new"]["asset_claims"]["tools"], 5.0)
        unchanged_next_turn = (
            organizations.reconcile_organization_asset_claims(
                divided, divided, self.communities(left={}, right={})))
        self.assertEqual(
            unchanged_next_turn["old"]["asset_claims"]["tools"], 5.0)
        self.assertEqual(
            unchanged_next_turn["new"]["asset_claims"]["tools"], 5.0)

    def test_claims_are_proportionally_clamped_to_real_community_stock(self):
        previous = {
            "a": self.row("a", sites=["s1"], claims={"food": 8.0}),
            "b": self.row("b", sites=["s2"], claims={"food": 8.0}),
        }
        active = copy.deepcopy(previous)
        reconciled = organizations.reconcile_organization_asset_claims(
            previous, active, self.communities(**{"cluster:a": {"food": 10.0}}))
        self.assertEqual(reconciled["a"]["asset_claims"]["food"], 5.0)
        self.assertEqual(reconciled["b"]["asset_claims"]["food"], 5.0)
        state = {"organizations": reconciled}
        ledger = {"communities": self.communities(
            **{"cluster:a": {"food": 10.0}})}
        self.assertTrue(
            organizations.organization_asset_claims_match_communities(
                state, ledger))
        reconciled["a"]["asset_claims"]["food"] = -1.0
        self.assertFalse(
            organizations.organization_asset_claims_match_communities(
                {"organizations": reconciled}, ledger))

    def test_current_claims_shrink_before_effects_when_underlying_stock_is_used(self):
        state = organizations.initial_organization_state()
        state["updated_turn"] = 9
        state["organizations"] = {
            "company": self.row(
                "company", sites=["s1"], claims={"food": 10.0})}
        before = copy.deepcopy(state)
        reconciled = organizations.reconcile_organization_state_asset_claims(
            state, self.communities(**{"cluster:a": {"food": 4.0}}))
        self.assertEqual(state, before)
        self.assertEqual(reconciled["updated_turn"], 9)
        self.assertEqual(
            reconciled["organizations"]["company"]["asset_claims"]["food"],
            4.0)
        self.assertEqual(
            reconciled["organizations"]["company"]["asset_claim_total"], 4.0)

    def test_ended_organization_releases_claim_without_removing_goods(self):
        state = organizations.initial_organization_state()
        state["organizations"] = {
            "company": self.row(
                "company", sites=["s1"], claims={"food": 7.0})}
        ledger = {"communities": self.communities(
            **{"cluster:a": {"food": 11.0}})}
        before_ledger = copy.deepcopy(ledger)

        planned = organizations.plan_organization_turn(
            state, ledger, {"sites": {}}, 10,
            bank_stage=0, currency_stage=0, enforcement_stage=0)

        ended = planned["organizations"]["company"]
        self.assertFalse(ended["active"])
        self.assertEqual(
            ended["asset_claims"],
            organizations.initial_organization_asset_claims())
        self.assertEqual(ended["asset_claim_total"], 0.0)
        self.assertEqual(ledger, before_ledger)
        self.assertEqual(
            ledger["communities"]["cluster:a"]["local_economy"]["food"],
            11.0)


class OrganizationWorldIntegrationTest(unittest.TestCase):
    def test_short_json_checkpoint_resume_matches_one_shot(self):
        kwargs = {
            "policy_name": "cautious", "seed": 1,
            "safety_floor": game.SAFETY_FLOOR, "talent": "health",
            "continue_world": True,
        }
        one_shot = game.simulate_policy(turns=18, **kwargs)
        first = game.simulate_policy(turns=9, **kwargs)
        checkpoint = json.loads(json.dumps(first["resume_state"]))
        resumed = game.simulate_policy(
            turns=18, resume_state=checkpoint, **kwargs)
        self.assertEqual(resumed, one_shot)
        active = organizations.active_organizations(
            resumed["organization_state"])
        self.assertTrue(active)
        self.assertTrue(all("operating_reserve" in row for row in active))

    def test_checkpoint_dashboard_and_stream_publish_organizations(self):
        data = game.collect_visualize_trace(
            1, "cautious", 3, game.SAFETY_FLOOR, "health",
            include_resume_state=True)
        state = data["organization_state"]
        self.assertGreater(len(organizations.active_organizations(state)), 0)
        self.assertEqual(
            data["resume_state"]["organization_state"], state)
        self.assertTrue(data["trace"]["organization_events"])
        self.assertEqual(
            [(row["turn"], row["source_turn"])
             for row in data["trace"]["organization_effects"]],
            [(2, 1), (3, 2)])
        self.assertEqual(state["effect_application_count"], 2)
        self.assertGreater(sum(state["effect_totals"].values()), 0.0)
        self.assertGreater(sum(
            state["asset_claim_acquisition_totals"].values()), 0.0)
        self.assertTrue(
            organizations.organization_asset_claims_match_communities(
                state, data["activity_community_ledger"]))
        self.assertIn(
            "organization_operating_status_counts", data["trace"]["turns"][-1])
        self.assertIn(
            "organization_operating_reserve_total", data["trace"]["turns"][-1])
        self.assertIn(
            "organization_workforce_total", data["trace"]["turns"][-1])
        self.assertIn(
            "organization_named_worker_total", data["trace"]["turns"][-1])
        self.assertIn(
            "organization_mean_member_continuity", data["trace"]["turns"][-1])
        self.assertIn(
            "organization_asset_claim_totals", data["trace"]["turns"][-1])
        self.assertIn("productive_population", data["final"])

        built = build_dashboard.build_dashboard_data(data)
        self.assertEqual(built["organization_state"], state)
        self.assertGreater(built["meta"]["organization_count"], 0)
        self.assertEqual(
            built["meta"]["organization_workforce_total"],
            data["trace"]["turns"][-1]["organization_workforce_total"])
        self.assertEqual(
            built["meta"]["organization_named_worker_total"],
            data["trace"]["turns"][-1]["organization_named_worker_total"])
        self.assertEqual(
            built["turns"][-1]["organization_workforce_total"],
            data["trace"]["turns"][-1]["organization_workforce_total"])
        self.assertIn(
            "organization_mean_member_continuity", built["turns"][-1])
        self.assertEqual(
            built["meta"]["organization_asset_claim_totals"],
            built["turns"][-1]["organization_asset_claim_totals"])
        self.assertIn("total_productive_population", built["turns"][-1])
        world = {
            "stream_id": "organization-test", "frame_sequence_end": 100,
            "revision": 1, "updated_at": 1.0,
            "summary": {"completed_turn": 3},
            "clock": {"tick_seconds": 1},
        }
        stream = aquarium_stream.build_stream_snapshot(built, world)
        self.assertEqual(
            stream["keyframe"]["organization_state"], state)

    def test_new_organizations_do_not_affect_their_foundation_turn(self):
        data = game.collect_visualize_trace(
            1, "cautious", 1, game.SAFETY_FLOOR, "health",
            include_resume_state=True)
        self.assertTrue(data["trace"]["organization_events"])
        self.assertEqual(data["trace"]["organization_effects"], [])
        self.assertEqual(
            data["organization_state"]["effect_application_count"], 0)
        self.assertEqual(data["organization_state"]["last_effect_turn"], 1)

    def test_zero_effect_control_preserves_organizations_without_surplus(self):
        def no_effects(state):
            return {
                "version": organizations.ORGANIZATION_EFFECT_PLAN_VERSION,
                "source_turn": int(state.get("updated_turn", 0)),
                "settlements": {}, "contributors": [],
                "organization_count": 0,
            }

        trace = {"turns": [], "settlements": [], "npc_introductions": []}
        with mock.patch.object(
                offline_simulation, "plan_organization_effects",
                side_effect=no_effects):
            result = game.simulate_policy(
                "cautious", 3, 1, game.SAFETY_FLOOR, "health",
                continue_world=True, trace=trace)
        self.assertTrue(result["organization_state"]["organizations"])
        self.assertEqual(trace["organization_effects"], [])
        self.assertEqual(
            result["organization_state"]["effect_application_count"], 0)
        self.assertEqual(
            sum(result["organization_state"]["effect_totals"].values()), 0.0)

    def test_effect_accounting_records_realized_clamped_delta(self):
        def excessive_effects(state):
            return {
                "version": organizations.ORGANIZATION_EFFECT_PLAN_VERSION,
                "source_turn": int(state.get("updated_turn", 0)),
                "settlements": {"cluster:000001": {
                    field: 1000.0
                    for field in organizations.ORGANIZATION_EFFECT_LIMITS}},
                "contributors": [{
                    "organization_id": "synthetic", "community_id":
                    "cluster:000001"}],
                "organization_count": 1,
            }

        trace = {"turns": [], "settlements": [], "npc_introductions": []}
        with mock.patch.object(
                offline_simulation, "plan_organization_effects",
                side_effect=excessive_effects):
            result = game.simulate_policy(
                "cautious", 1, 1, game.SAFETY_FLOOR, "health",
                continue_world=True, trace=trace)
        totals = result["organization_state"]["effect_totals"]
        self.assertTrue(trace["organization_effects"])
        self.assertEqual(
            trace["organization_effects"][0]["settlements"]
            ["cluster:000001"],
            {field: value for field, value in totals.items() if value})
        self.assertTrue(all(0.0 < value < 1000.0 for value in totals.values()))
        self.assertEqual(
            result["organization_state"]["effect_application_count"], 1)


if __name__ == "__main__":
    unittest.main()
