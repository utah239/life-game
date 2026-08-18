# -*- coding: utf-8 -*-
"""活動クラスタ共同体台帳の保存則と系譜接続。"""
import copy
import json
import random
import unittest

from institutions import (
    activity_communities, population, residents, settlement_network, spatial)


HOME_ECONOMY = {
    "food": 90.0, "medicine": 92.0, "shelter": 70.0, "tools": 65.0,
    "production_capacity": 60.0, "barter_stage": 0,
    "community_trust": 50.0, "local_credit_stage": 0,
}


class ActivityCommunityLedgerTest(unittest.TestCase):
    def setUp(self):
        self.settlements = settlement_network.initial_settlement_network(
            HOME_ECONOMY)
        self.registry = residents.initial_resident_registry(
            self.settlements, world_seed=23)
        self.spatial = spatial.initial_spatial_state(self.registry, turn=1)

    def build(self, spatial_state=None, registry=None, settlements=None, turn=1):
        return activity_communities.build_activity_community_ledger(
            spatial_state or self.spatial, registry or self.registry,
            settlements or self.settlements, turn)

    def test_is_deterministic_conservative_json_safe_and_rng_free(self):
        spatial_before = copy.deepcopy(self.spatial)
        registry_before = copy.deepcopy(self.registry)
        settlements_before = copy.deepcopy(self.settlements)
        rng_before = random.getstate()

        first = self.build()
        second = self.build()

        self.assertEqual(first, second)
        self.assertTrue(
            activity_communities.activity_community_ledger_matches_sources(
                first, self.spatial, self.registry, self.settlements))
        self.assertEqual(
            sum(row["population"] for row in first["communities"].values()),
            sum(row["population"] for row in self.settlements.values()))
        self.assertEqual(
            sum(row["productive_population"]
                for row in first["communities"].values()),
            sum(row["productive_population"]
                for row in self.settlements.values()))
        self.assertTrue(all(
            sum(row["age_cohorts"].values()) == row["population"]
            for row in first["communities"].values()))
        for field in activity_communities.FLOAT_ACCOUNT_FIELDS:
            projected = round(sum(
                row["local_economy"][field]
                for row in first["communities"].values()), 6)
            self.assertAlmostEqual(projected, first["world_totals"][field], 6)
        self.assertEqual(random.getstate(), rng_before)
        self.assertEqual(self.spatial, spatial_before)
        self.assertEqual(self.registry, registry_before)
        self.assertEqual(self.settlements, settlements_before)
        json.dumps(first)

    def test_split_creates_two_ledgers_and_preserves_parent_lineage(self):
        together = copy.deepcopy(self.spatial)
        for site in together["sites"].values():
            site["x"] = site["target_x"] = 0.5
            site["y"] = site["target_y"] = 0.5
        one = spatial.synchronize_spatial_state(
            together, self.registry, turn=2)
        parent_id = next(iter(one["clusters"]))

        separated = copy.deepcopy(one)
        for index, site_id in enumerate(sorted(separated["sites"])):
            site = separated["sites"][site_id]
            site["x"] = site["target_x"] = 0.2 if index % 2 else 0.8
            site["y"] = site["target_y"] = 0.5
        split = spatial.synchronize_spatial_state(
            separated, self.registry, turn=3)
        ledger = self.build(split, turn=3)

        self.assertEqual(set(ledger["communities"]), set(split["clusters"]))
        self.assertEqual(len(ledger["communities"]), 2)
        self.assertIn(parent_id, ledger["communities"])
        child_id = next(key for key in ledger["communities"] if key != parent_id)
        self.assertEqual(ledger["communities"][child_id]["parent_ids"], [parent_id])
        self.assertEqual(
            sum(row["population"] for row in ledger["communities"].values()),
            ledger["world_totals"]["population"])

    def test_mixed_legacy_accounts_become_one_activity_community(self):
        together = copy.deepcopy(self.spatial)
        for site in together["sites"].values():
            site["x"] = site["target_x"] = 0.5
            site["y"] = site["target_y"] = 0.5
        together = spatial.synchronize_spatial_state(
            together, self.registry, turn=2)
        ledger = self.build(together, turn=2)
        community = next(iter(ledger["communities"].values()))

        self.assertEqual(
            set(community["account_populations"]), set(self.settlements))
        expected_trust = round(sum(
            row["local_economy"]["community_trust"] * row["population"]
            for row in self.settlements.values())
            / sum(row["population"] for row in self.settlements.values()), 6)
        self.assertEqual(
            community["local_economy"]["community_trust"], expected_trust)

    def test_uninhabited_land_inventory_is_preserved_outside_communities(self):
        settlements = copy.deepcopy(self.settlements)
        registry = copy.deepcopy(self.registry)
        for resident in registry["residents"].values():
            if resident["settlement_id"] == "upland":
                resident["alive"] = False
        settlements["upland"]["population"] = 0
        settlements["upland"]["reproductive_population"] = 0
        settlements["upland"]["productive_population"] = 0
        settlements["upland"]["age_cohorts"] = {
            key: 0 for key in population.AGE_COHORT_KEYS}
        settlements["upland"]["age_transition_carry"] = {
            "children_to_productive": 0.0,
            "productive_to_elderly": 0.0,
        }
        state = spatial.initial_spatial_state(registry, turn=2)

        ledger = self.build(state, registry, settlements, turn=2)

        self.assertIn("upland", ledger["unassigned_accounts"])
        self.assertEqual(
            ledger["unassigned_accounts"]["upland"]["local_economy"]["shelter"],
            settlements["upland"]["local_economy"]["shelter"])
        self.assertTrue(
            activity_communities.activity_community_ledger_matches_sources(
                ledger, state, registry, settlements))

    def test_promotion_rekeys_accounting_to_clusters_without_loss(self):
        ledger = self.build()

        promoted = activity_communities.promote_activity_communities(
            ledger, self.registry)
        relabeled = spatial.relabel_spatial_accounts(
            self.spatial, promoted["registry"])
        rebuilt = activity_communities.build_activity_community_ledger(
            relabeled, promoted["registry"], promoted["settlements"], 1)

        self.assertEqual(
            {key for key, row in promoted["settlements"].items()
             if row["population"] > 0}, set(self.spatial["clusters"]))
        self.assertTrue(residents.registry_matches_settlements(
            promoted["registry"], promoted["settlements"]))
        self.assertEqual(rebuilt["world_totals"], ledger["world_totals"])
        self.assertEqual(
            rebuilt["world_totals"]["productive_population"],
            ledger["world_totals"]["productive_population"])
        self.assertEqual(
            {row["account_id"] for row in relabeled["sites"].values()
             if row.get("active", True)}, set(self.spatial["clusters"]))
        self.assertEqual(relabeled["accounting_mode"], "activity_communities")

    def test_promoted_split_and_merge_preserve_every_additive_account(self):
        together = copy.deepcopy(self.spatial)
        for site in together["sites"].values():
            site["x"] = site["target_x"] = 0.5
            site["y"] = site["target_y"] = 0.5
        together = spatial.synchronize_spatial_state(
            together, self.registry, turn=2)
        parent_ledger = self.build(together, turn=2)
        parent = activity_communities.promote_activity_communities(
            parent_ledger, self.registry)
        together = spatial.relabel_spatial_accounts(
            together, parent["registry"])

        separated = copy.deepcopy(together)
        for index, site_id in enumerate(sorted(separated["sites"])):
            site = separated["sites"][site_id]
            site["x"] = site["target_x"] = 0.2 if index % 2 else 0.8
            site["y"] = site["target_y"] = 0.5
        split_space = spatial.synchronize_spatial_state(
            separated, parent["registry"], turn=3)
        split_ledger = activity_communities.build_activity_community_ledger(
            split_space, parent["registry"], parent["settlements"], 3)
        split = activity_communities.promote_activity_communities(
            split_ledger, parent["registry"])
        self.assertEqual(len(split_ledger["communities"]), 2)
        self.assertEqual(
            split_ledger["world_totals"], parent_ledger["world_totals"])
        self.assertTrue(residents.registry_matches_settlements(
            split["registry"], split["settlements"]))

        split_space = spatial.relabel_spatial_accounts(
            split_space, split["registry"])
        reunited = copy.deepcopy(split_space)
        for site in reunited["sites"].values():
            site["x"] = site["target_x"] = 0.5
            site["y"] = site["target_y"] = 0.5
        merged_space = spatial.synchronize_spatial_state(
            reunited, split["registry"], turn=4)
        merged_ledger = activity_communities.build_activity_community_ledger(
            merged_space, split["registry"], split["settlements"], 4)
        merged = activity_communities.promote_activity_communities(
            merged_ledger, split["registry"])

        self.assertEqual(len(merged_ledger["communities"]), 1)
        self.assertEqual(
            merged_ledger["world_totals"], parent_ledger["world_totals"])
        self.assertTrue(residents.registry_matches_settlements(
            merged["registry"], merged["settlements"]))

    def test_practice_is_inherited_on_split_and_weighted_on_merge(self):
        together = copy.deepcopy(self.spatial)
        for site in together["sites"].values():
            site["x"] = site["target_x"] = 0.5
            site["y"] = site["target_y"] = 0.5
        together = spatial.synchronize_spatial_state(
            together, self.registry, turn=2)
        parent_ledger = self.build(together, turn=2)
        parent = activity_communities.promote_activity_communities(
            parent_ledger, self.registry)
        parent_id = next(iter(parent["settlements"]))
        parent["settlements"][parent_id]["local_economy"][
            "production_practice_by_good"] = {
                "food": 80.0, "medicine": 60.0,
                "shelter": 40.0, "tools": 20.0}
        together = spatial.relabel_spatial_accounts(
            together, parent["registry"])

        separated = copy.deepcopy(together)
        for index, site_id in enumerate(sorted(separated["sites"])):
            site = separated["sites"][site_id]
            site["x"] = site["target_x"] = 0.2 if index % 2 else 0.8
            site["y"] = site["target_y"] = 0.5
        split_space = spatial.synchronize_spatial_state(
            separated, parent["registry"], turn=3)
        split_ledger = activity_communities.build_activity_community_ledger(
            split_space, parent["registry"], parent["settlements"], 3)
        self.assertEqual(len(split_ledger["communities"]), 2)
        self.assertTrue(all(
            row["local_economy"]["production_practice_by_good"]
            == {"food": 80.0, "medicine": 60.0,
                "shelter": 40.0, "tools": 20.0}
            for row in split_ledger["communities"].values()))

        split = activity_communities.promote_activity_communities(
            split_ledger, parent["registry"])
        split_ids = sorted(split["settlements"])
        values = (10.0, 90.0)
        for settlement_id, value in zip(split_ids, values):
            split["settlements"][settlement_id]["local_economy"][
                "production_practice_by_good"] = {
                    good: value for good in
                    ("food", "medicine", "shelter", "tools")}
        total_population = sum(
            split["settlements"][sid]["population"] for sid in split_ids)
        expected = round(sum(
            split["settlements"][sid]["population"] * value
            for sid, value in zip(split_ids, values)) / total_population, 6)

        split_space = spatial.relabel_spatial_accounts(
            split_space, split["registry"])
        reunited = copy.deepcopy(split_space)
        for site in reunited["sites"].values():
            site["x"] = site["target_x"] = 0.5
            site["y"] = site["target_y"] = 0.5
        merged_space = spatial.synchronize_spatial_state(
            reunited, split["registry"], turn=4)
        merged_ledger = activity_communities.build_activity_community_ledger(
            merged_space, split["registry"], split["settlements"], 4)
        merged_practice = next(iter(merged_ledger["communities"].values()))[
            "local_economy"]["production_practice_by_good"]
        self.assertEqual(merged_practice, {
            good: expected for good in
            ("food", "medicine", "shelter", "tools")})


class HybridActivityCommunityLedgerTest(unittest.TestCase):
    def test_million_population_is_carried_by_bounded_sites_without_loss(self):
        settlements = {"mega": population.initial_settlement(
            "mega", population=1_000_000,
            reproductive_population=350_000)}
        registry = residents.initial_resident_registry(
            settlements, world_seed=17)
        before = copy.deepcopy((settlements, registry))
        rng_before = random.getstate()

        state = spatial.initial_spatial_state(registry, turn=1)
        ledger = activity_communities.build_activity_community_ledger(
            state, registry, settlements, 1)

        self.assertLessEqual(
            len(state["sites"]),
            residents.NAMED_RESIDENT_LIMIT //
            residents.INITIAL_HOUSEHOLD_TARGET_SIZE)
        self.assertEqual(len(state["residents"]), 4096)
        self.assertEqual(sum(
            row["population_weight"] for row in state["sites"].values()
            if row["active"]), 1_000_000)
        self.assertEqual(sum(
            row["resident_count"] for row in state["clusters"].values()),
            1_000_000)
        self.assertEqual(sum(
            row["named_resident_count"]
            for row in state["clusters"].values()), 4096)
        self.assertEqual(sum(
            row["population"] for row in ledger["communities"].values()),
            1_000_000)
        self.assertTrue(spatial.spatial_state_matches_registry(
            state, registry))
        self.assertTrue(
            activity_communities.activity_community_ledger_matches_sources(
                ledger, state, registry, settlements))

        promoted = activity_communities.promote_activity_communities(
            ledger, registry)
        relabeled = spatial.relabel_spatial_accounts(
            state, promoted["registry"])
        rebuilt = activity_communities.build_activity_community_ledger(
            relabeled, promoted["registry"], promoted["settlements"], 1)
        self.assertTrue(residents.registry_matches_settlements(
            promoted["registry"], promoted["settlements"]))
        self.assertTrue(spatial.spatial_state_matches_registry(
            relabeled, promoted["registry"]))
        self.assertEqual(rebuilt["world_totals"]["population"], 1_000_000)
        self.assertEqual(
            rebuilt["world_totals"]["productive_population"], 620_000)
        self.assertEqual((settlements, registry), before)
        self.assertEqual(random.getstate(), rng_before)


if __name__ == "__main__":
    unittest.main()
