# -*- coding: utf-8 -*-
"""財の物量・生活基盤規模・強度値を混同しないための保存則テスト。"""
import copy
import random
import unittest

import game
from institutions import (
    activity_communities, activity_economy, barter, residents,
    settlement_network, spatial)
from institutions.intersettlement_trade import (
    PORTABLE_GOODS, plan_intersettlement_trade)


HOME_ECONOMY = {
    "food": barter.FOOD_STOCK_INITIAL,
    "medicine": barter.MEDICINE_STOCK_INITIAL,
    "shelter": barter.SHELTER_DURABILITY_INITIAL,
    "tools": barter.TOOLS_DURABILITY_INITIAL,
    "production_capacity": 60.0,
    "barter_stage": 0,
    "community_trust": 50.0,
    "local_credit_stage": 0,
}


class GoodsScaleRulesTest(unittest.TestCase):
    def test_scale_changes_quantity_not_coverage_or_shortfall(self):
        one = barter.goods_coverage(45.0, 46.0, 0.5, 10.5, 0.5)
        two = barter.goods_coverage(90.0, 92.0, 1.0, 21.0, 1.0)
        self.assertEqual(one, two)
        self.assertEqual(
            barter.worst_shortfall(
                45.0, 46.0, 0.5, 10.5, 60.0,
                provisioning_scale=0.5),
            barter.worst_shortfall(90.0, 92.0, 1.0, 21.0, 60.0))

    def test_upkeep_total_is_invariant_under_accounting_split(self):
        parent = game.plan_barter_upkeep(
            90.0, 92.0, 1.0, 21.0, 60.0,
            provisioning_scale=1.0)
        left = game.plan_barter_upkeep(
            36.0, 36.8, 0.4, 8.4, 60.0,
            provisioning_scale=0.4)
        right = game.plan_barter_upkeep(
            54.0, 55.2, 0.6, 12.6, 60.0,
            provisioning_scale=0.6)
        for good in ("food", "medicine", "shelter", "tools"):
            self.assertAlmostEqual(
                left[f"{good}_delta"] + right[f"{good}_delta"],
                parent[f"{good}_delta"], 6)
            self.assertAlmostEqual(
                left["gross_consumption"][good]
                + right["gross_consumption"][good],
                parent["gross_consumption"][good], 6)
            self.assertAlmostEqual(
                left["gross_production"][good]
                + right["gross_production"][good],
                parent["gross_production"][good], 6)
        self.assertEqual(left["production_capacity_after"],
                         parent["production_capacity_after"])
        self.assertEqual(right["production_capacity_after"],
                         parent["production_capacity_after"])

    def test_labor_urgency_uses_scaled_reference(self):
        parent = activity_economy.background_production_labor_plan(
            100, 15,
            {"food": 45.0, "medicine": 92.0,
             "shelter": 1.0, "tools": 21.0},
            provisioning_scale=1.0)
        half = activity_economy.background_production_labor_plan(
            100, 15,
            {"food": 22.5, "medicine": 46.0,
             "shelter": 0.5, "tools": 10.5},
            provisioning_scale=0.5)
        self.assertEqual(parent["active_worker_count_by_good"],
                         half["active_worker_count_by_good"])

    def test_negative_scale_is_rejected_and_zero_scale_is_well_defined(self):
        with self.assertRaises(ValueError):
            barter.normalize_provisioning_scale(-0.01)
        self.assertEqual(
            barter.goods_coverage(0.0, 0.0, 0.0, 0.0, 0.0),
            {"food": 100.0, "medicine": 100.0,
             "shelter": 100.0, "tools": 100.0})

    def test_million_person_split_keeps_one_person_scale_nonzero(self):
        allocation = activity_communities._allocate_float(
            1.0, {"one": 1, "rest": 999_999}, digits=12)
        self.assertEqual(allocation["one"], 0.000001)
        self.assertEqual(sum(allocation.values()), 1.0)


class ActivityCommunityGoodsConservationTest(unittest.TestCase):
    def setUp(self):
        self.settlements = settlement_network.initial_settlement_network(
            HOME_ECONOMY)
        self.registry = residents.initial_resident_registry(
            self.settlements, world_seed=23)
        self.spatial = spatial.initial_spatial_state(self.registry, turn=1)

    def _one_parent(self):
        together = copy.deepcopy(self.spatial)
        for site in together["sites"].values():
            site["x"] = site["target_x"] = 0.5
            site["y"] = site["target_y"] = 0.5
        together = spatial.synchronize_spatial_state(
            together, self.registry, turn=2)
        ledger = activity_communities.build_activity_community_ledger(
            together, self.registry, self.settlements, 2)
        promoted = activity_communities.promote_activity_communities(
            ledger, self.registry)
        together = spatial.relabel_spatial_accounts(
            together, promoted["registry"])
        return together, promoted, ledger

    def test_split_preserves_goods_scale_coverage_and_capacity_intensity(self):
        together, parent, parent_ledger = self._one_parent()
        parent_economy = next(iter(parent["settlements"].values()))[
            "local_economy"]
        parent_coverage = barter.goods_coverage(
            parent_economy["food"], parent_economy["medicine"],
            parent_economy["shelter"], parent_economy["tools"],
            parent_economy["provisioning_scale"])
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
        self.assertAlmostEqual(sum(
            row["local_economy"]["provisioning_scale"]
            for row in split_ledger["communities"].values()),
            parent_economy["provisioning_scale"], 10)
        for row in split_ledger["communities"].values():
            economy = row["local_economy"]
            self.assertEqual(barter.goods_coverage(
                economy["food"], economy["medicine"], economy["shelter"],
                economy["tools"], economy["provisioning_scale"]),
                parent_coverage)
            self.assertEqual(economy["production_capacity"],
                             parent_economy["production_capacity"])
        self.assertEqual(
            split_ledger["world_totals"], parent_ledger["world_totals"])

    def test_merge_provisioning_scale_weights_capacity_instead_of_adding_it(self):
        settlements = copy.deepcopy(self.settlements)
        for settlement_id, capacity in zip(
                sorted(settlements), (10.0, 40.0, 90.0)):
            settlements[settlement_id]["local_economy"][
                "production_capacity"] = capacity
        together = copy.deepcopy(self.spatial)
        for site in together["sites"].values():
            site["x"] = site["target_x"] = 0.5
            site["y"] = site["target_y"] = 0.5
        together = spatial.synchronize_spatial_state(
            together, self.registry, turn=2)
        ledger = activity_communities.build_activity_community_ledger(
            together, self.registry, settlements, 2)
        community = next(iter(ledger["communities"].values()))
        weights = community["account_provisioning_scales"]
        expected = round(sum(
            settlements[sid]["local_economy"]["production_capacity"]
            * weights[sid] for sid in weights) / sum(weights.values()), 6)
        self.assertEqual(
            community["local_economy"]["production_capacity"], expected)
        self.assertLessEqual(
            community["local_economy"]["production_capacity"], 90.0)
        self.assertAlmostEqual(
            community["local_economy"]["provisioning_scale"]
            * community["local_economy"]["production_capacity"],
            sum(weights[sid] * settlements[sid]["local_economy"][
                "production_capacity"] for sid in weights), 5)


class ScaledTradeConservationTest(unittest.TestCase):
    def test_trade_uses_each_community_scale_and_preserves_quantity(self):
        settlements = settlement_network.initial_settlement_network(
            HOME_ECONOMY)
        source = settlements["home"]["local_economy"]
        destination = settlements["upland"]["local_economy"]
        source["provisioning_scale"] = 2.0
        source["food"] = 180.0
        destination["provisioning_scale"] = 0.5
        destination["food"] = 0.0
        settlements["riverside"]["population"] = 0
        before = copy.deepcopy(settlements)
        rng_before = random.getstate()

        result = plan_intersettlement_trade(settlements, 1)

        event = next(row for row in result["events"]
                     if row["good"] == "food")
        self.assertEqual(event["from_settlement"], "home")
        self.assertEqual(event["to_settlement"], "upland")
        for good in PORTABLE_GOODS:
            self.assertAlmostEqual(
                sum(row["local_economy"][good]
                    for row in before.values()),
                sum(row["local_economy"][good]
                    for row in result["settlements"].values()), 6)
        self.assertEqual(settlements, before)
        self.assertEqual(random.getstate(), rng_before)


if __name__ == "__main__":
    unittest.main()
