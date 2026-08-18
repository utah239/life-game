# -*- coding: utf-8 -*-
"""複数集落の所有境界・移住・人口保存則。"""
import copy
import random
import unittest

import game
from dashboard import build_dashboard
from institutions import activity_communities, settlement_network, spatial
from institutions import household_needs


HOME_ECONOMY = {
    "food": 90.0, "medicine": 92.0, "shelter": 1.0, "tools": 21.0,
    "production_capacity": 21.0, "barter_stage": 0,
    "community_trust": 50.0, "local_credit_stage": 0,
}


class SettlementNetworkRulesTest(unittest.TestCase):
    def test_initial_network_has_three_distinct_local_economies(self):
        before = copy.deepcopy(HOME_ECONOMY)
        network = settlement_network.initial_settlement_network(HOME_ECONOMY)
        self.assertEqual(set(network), {"home", "riverside", "upland"})
        self.assertEqual(sum(s["population"] for s in network.values()), 250)
        self.assertEqual(network["home"]["local_economy"]["food"], 129.6)
        self.assertEqual(sum(
            row["local_economy"]["provisioning_scale"]
            for row in network.values()), 3.0)
        self.assertEqual(
            household_needs.build_household_needs_state(
                network, None, 1)["world_demand_scale_by_good"],
            {good: 3.0 for good in household_needs.NEED_GOODS})
        self.assertNotEqual(
            network["riverside"]["local_economy"],
            network["upland"]["local_economy"])
        self.assertEqual(
            [network[sid]["local_economy"]["community_trust"]
             for sid in ("home", "riverside", "upland")],
            [50.0, 56.0, 44.0])
        self.assertTrue(all(
            "local_credit_stage" in row["local_economy"]
            and "trade_sent_total" in row["local_economy"]
            for row in network.values()))
        self.assertEqual(HOME_ECONOMY, before)

    def test_initial_network_scales_to_million_without_rounding_loss(self):
        before = copy.deepcopy(HOME_ECONOMY)
        rng_before = random.getstate()
        default = settlement_network.initial_settlement_network(HOME_ECONOMY)
        explicit_default = settlement_network.initial_settlement_network(
            HOME_ECONOMY, total_population=250)
        network = settlement_network.initial_settlement_network(
            HOME_ECONOMY, total_population=1_000_000)

        self.assertEqual(explicit_default, default)
        self.assertEqual(
            [row["population"] for row in network.values()],
            [480_000, 312_000, 208_000])
        self.assertEqual(
            [row["reproductive_population"] for row in network.values()],
            [168_000, 112_000, 72_000])
        self.assertEqual(sum(
            row["population"] for row in network.values()), 1_000_000)
        self.assertEqual(sum(
            row["productive_population"] for row in network.values()), 620_000)
        self.assertEqual(sum(
            row["local_economy"]["provisioning_scale"]
            for row in network.values()), 12_000.0)
        million_demand = household_needs.build_household_needs_state(
            network, None, 1)["world_demand_scale_by_good"]
        self.assertEqual(million_demand["shelter"], 12_000.0)
        self.assertTrue(all(
            11_900.0 <= million_demand[good] <= 12_100.0
            for good in household_needs.NEED_GOODS))
        self.assertTrue(all(
            sum(row["age_cohorts"].values()) == row["population"]
            for row in network.values()))
        self.assertEqual(HOME_ECONOMY, before)
        self.assertEqual(random.getstate(), rng_before)

    def test_initial_population_must_be_positive_integer(self):
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                settlement_network.initial_settlement_network(
                    HOME_ECONOMY, total_population=value)

    def test_old_single_settlement_is_upgraded_without_inventing_population(self):
        old = {"home": {
            "id": "home", "name": "old", "population": 17,
            "reproductive_population": 3, "stage": 2,
        }}
        upgraded = settlement_network.upgrade_single_settlement(old, HOME_ECONOMY)
        self.assertEqual(set(upgraded), {"home"})
        self.assertEqual(upgraded["home"]["population"], 17)
        self.assertIn("local_economy", upgraded["home"])
        self.assertEqual(
            sum(upgraded["home"]["age_cohorts"].values()), 17)
        self.assertIn("productive_population", upgraded["home"])
        self.assertNotIn("local_economy", old["home"])

    def test_migration_preserves_total_and_moves_toward_safer_settlement(self):
        network = settlement_network.initial_settlement_network(HOME_ECONOMY)
        network["home"]["stage"] = 3
        network["home"]["local_economy"]["worst_shortfall"] = 95
        network["riverside"]["stage"] = 0
        network["riverside"]["local_economy"]["worst_shortfall"] = 0
        before = copy.deepcopy(network)
        population_before = sum(s["population"] for s in network.values())
        reproductive_before = sum(
            s["reproductive_population"] for s in network.values())
        productive_before = sum(
            s["productive_population"] for s in network.values())
        cohorts_before = {
            key: sum(s["age_cohorts"][key] for s in network.values())
            for key in settlement_network.AGE_COHORT_KEYS}
        plan = settlement_network.plan_migration(network, 10)
        after = plan["settlements"]
        self.assertGreater(plan["migrants"], 0)
        self.assertEqual(
            sum(s["population"] for s in after.values()), population_before)
        self.assertEqual(
            sum(s["reproductive_population"] for s in after.values()),
            reproductive_before)
        self.assertEqual(
            sum(s["productive_population"] for s in after.values()),
            productive_before)
        self.assertEqual({
            key: sum(s["age_cohorts"][key] for s in after.values())
            for key in settlement_network.AGE_COHORT_KEYS}, cohorts_before)
        self.assertTrue(all(
            event["productive_migrants"]
            == event["age_cohort_migrants"]["productive"]
            for event in plan["events"]))
        self.assertLess(after["home"]["population"], before["home"]["population"])
        self.assertEqual(network, before)
        self.assertTrue(all(
            e["source_pressure"] > e["destination_pressure"]
            for e in plan["events"]))

    def test_no_pressure_gap_means_no_migration_and_no_rng(self):
        network = settlement_network.initial_settlement_network(HOME_ECONOMY)
        state = random.getstate()
        plan = settlement_network.plan_migration(network, 1)
        self.assertEqual(plan["migrants"], 0)
        self.assertEqual(plan["events"], [])
        self.assertEqual(random.getstate(), state)

    def test_focus_selection_uses_largest_surviving_settlement(self):
        network = settlement_network.initial_settlement_network(HOME_ECONOMY)
        network["home"]["population"] = 0
        network["home"]["stage"] = 4
        self.assertEqual(
            settlement_network.select_focus_settlement(network), "riverside")
        for row in network.values():
            row["population"] = 0
            row["stage"] = 4
        self.assertIsNone(settlement_network.select_focus_settlement(network))


class SettlementNetworkWorldTest(unittest.TestCase):
    def test_visualized_world_has_independent_activity_community_accounts(self):
        data = game.collect_visualize_trace(
            1, "cautious", 240, game.SAFETY_FLOOR, "health")
        built = build_dashboard.build_dashboard_data(data)
        physical_ids = set(data["spatial_state"]["clusters"])
        active_ids = spatial.accounting_activity_cluster_ids(
            data["spatial_state"])
        positive_account_ids = {
            settlement_id for settlement_id, row
            in built["settlement_states"].items()
            if row["population"] > 0}
        self.assertEqual(
            data["activity_community_accounting_version"],
            activity_communities.ACTIVITY_COMMUNITY_ACCOUNTING_VERSION)
        self.assertEqual(
            active_ids,
            set(data["activity_community_ledger"]["communities"]))
        self.assertEqual(active_ids, positive_account_ids)
        self.assertTrue(active_ids)
        self.assertTrue(active_ids.issubset(physical_ids))
        self.assertTrue(all(
            settlement_id.startswith("cluster:")
            for settlement_id in built["settlement_states"]))
        self.assertEqual(
            set(built["turns"][-1]["activity_community_populations"]),
            active_ids)
        self.assertEqual(
            set(built["turns"][-1]["activity_community_trusts"]),
            active_ids)
        # 移住と交易の成立自体は各pure ruleのテストが固定する。
        # 統合runで「早期に必ず発生」を要求すると、空間分割が作った
        # 架空の不足を正した際に誤って回帰とみなす。ここでは、活動
        # 共同体ごとの会計規模と充足率が公開されることを統合境界とする。
        latest = built["turns"][-1]
        self.assertEqual(
            set(latest["activity_community_provisioning_scales"]),
            active_ids)
        self.assertEqual(
            set(latest["activity_community_goods_coverage_by_good"]),
            active_ids)
        self.assertEqual(
            set(latest["activity_community_demand_scales_by_good"]),
            active_ids)
        self.assertAlmostEqual(
            sum(row["local_economy"]["provisioning_scale"]
                for row in data["settlement_states"].values()),
            latest["world_provisioning_scale"], 6)
        self.assertAlmostEqual(latest["world_provisioning_scale"], 3.0, 9)
        economies = [
            row["local_economy"] for row in data["settlement_states"].values()]
        self.assertGreater(len({
            (e["food"], e["medicine"], e["production_capacity"])
            for e in economies}), 1)

    def test_schema1_single_settlement_checkpoint_upgrades_without_neighbors(self):
        first = game.simulate_policy(
            "cautious", 12, 3, game.SAFETY_FLOOR, "health",
            continue_world=True)
        old = copy.deepcopy(first["resume_state"])
        home = copy.deepcopy(
            old["settlements"][old["focus_settlement_id"]])
        home["id"] = "home"
        home["name"] = "旧単一集落"
        home.pop("local_economy", None)
        for key in (
                "settlement_network_version", "focus_settlement_id",
                "migration_total", "resident_registry", "focus_resident_id",
                "spatial_state", "activity_community_ledger",
                "activity_community_accounting_version"):
            old.pop(key, None)
        old["settlements"] = {"home": home}
        resumed = game.simulate_policy(
            "cautious", 13, 3, game.SAFETY_FLOOR, "health",
            continue_world=True, resume_state=old)
        active_ids = spatial.accounting_activity_cluster_ids(
            resumed["spatial_state"])
        positive_ids = {
            settlement_id for settlement_id, row
            in resumed["settlements"].items() if row["population"] > 0}
        self.assertEqual(active_ids, positive_ids)
        self.assertEqual(
            active_ids,
            set(resumed["activity_community_ledger"]["communities"]))
        self.assertNotIn("riverside", resumed["settlements"])
        self.assertNotIn("upland", resumed["settlements"])
        for settlement_id in active_ids:
            economy = resumed["settlements"][settlement_id]["local_economy"]
            self.assertIn("community_trust", economy)
            self.assertIn("trade_sent_total", economy)
        self.assertEqual(
            resumed["settlement_network_version"],
            settlement_network.SETTLEMENT_NETWORK_VERSION)
        self.assertEqual(
            resumed["activity_community_accounting_version"],
            activity_communities.ACTIVITY_COMMUNITY_ACCOUNTING_VERSION)
        self.assertTrue(resumed["resident_registry"]["legacy_snapshot"])
        self.assertEqual(
            sum(row.get("alive", True)
                for row in resumed["resident_registry"]["residents"].values()),
            sum(row["population"] for row in resumed["settlements"].values()))
        self.assertTrue(resumed["focus_resident_id"])


if __name__ == "__main__":
    unittest.main()
