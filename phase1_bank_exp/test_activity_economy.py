# -*- coding: utf-8 -*-
"""背景生産を活動主体へ配賦する台帳の保存則。"""
import copy
import json
import random
import unittest

from institutions import activity_economy, spatial as spatial_module
from institutions.residents import initial_resident_registry
from institutions.settlement_network import initial_settlement_network
from institutions.spatial import (
    build_spatial_keyframe,
    initial_spatial_state,
    synchronize_spatial_state,
)


def _economy():
    return {
        "food": 90.0, "medicine": 92.0, "shelter": 1.0,
        "tools": 21.0, "production_capacity": 21.0,
        "barter_stage": 0, "community_trust": 50.0,
        "local_credit_stage": 0,
    }


def _gross(settlements):
    return {
        settlement_id: {
            "food": 9.4, "medicine": 9.3,
            "shelter": 3.95, "tools": 2.45,
        }
        for settlement_id in settlements}


class ActivityEconomyTest(unittest.TestCase):
    def setUp(self):
        self.settlements = initial_settlement_network(_economy())
        self.registry = initial_resident_registry(
            self.settlements, world_seed=41)
        self.spatial = initial_spatial_state(self.registry, turn=1)

    def test_gross_output_and_productive_workers_are_conserved(self):
        before_registry = copy.deepcopy(self.registry)
        before_spatial = copy.deepcopy(self.spatial)
        rng_before = random.getstate()

        result = activity_economy.plan_activity_economy(
            None, self.registry, self.spatial, self.settlements,
            _gross(self.settlements), turn=2)

        self.assertEqual(
            result["state"]["version"],
            activity_economy.ACTIVITY_ECONOMY_VERSION)
        self.assertEqual(len(result["events"]), len(self.settlements) * 4)
        named_ids = []
        for community_id, row in result["state"]["communities"].items():
            self.assertEqual(
                sum(activity["worker_count"]
                    for activity in row["activities"].values()),
                row["active_worker_count"])
            self.assertEqual(
                row["active_worker_count"]
                + row["unassigned_productive_population"],
                self.settlements[community_id]["productive_population"])
            for good, activity in row["activities"].items():
                self.assertAlmostEqual(
                    sum(site["gross_output"]
                        for site in activity["site_allocations"]),
                    _gross(self.settlements)[community_id][good], places=6)
                self.assertEqual(
                    sum(site["worker_count"]
                        for site in activity["site_allocations"]),
                    activity["worker_count"])
                self.assertEqual(
                    len(activity["named_worker_ids"])
                    + activity["anonymous_worker_count"],
                    activity["worker_count"])
                named_ids.extend(activity["named_worker_ids"])
        self.assertEqual(len(named_ids), len(set(named_ids)))
        self.assertEqual(self.registry, before_registry)
        self.assertEqual(self.spatial, before_spatial)
        self.assertEqual(random.getstate(), rng_before)

    def test_cumulative_output_survives_json_and_accumulates(self):
        first = activity_economy.plan_activity_economy(
            None, self.registry, self.spatial, self.settlements,
            _gross(self.settlements), turn=2)
        restored = json.loads(json.dumps(first["state"]))
        second = activity_economy.plan_activity_economy(
            restored, first["registry"], self.spatial, self.settlements,
            _gross(self.settlements), turn=3)
        community_count = len(self.settlements)
        for good, amount in {
                "food": 9.4, "medicine": 9.3,
                "shelter": 3.95, "tools": 2.45}.items():
            self.assertAlmostEqual(
                second["state"]["cumulative_output_by_good"][good],
                amount * community_count * 2, places=6)

    def test_explicit_per_good_labor_plan_is_preserved_to_activity_sites(self):
        scarce = copy.deepcopy(self.settlements)
        labor_plans = {}
        for community_id, settlement in scarce.items():
            settlement["productive_population"] = 4
            plan = activity_economy.background_production_labor_plan(
                settlement["population"], 4,
                {"food": 0.0, "medicine": 100.0,
                 "shelter": 100.0, "tools": 100.0})
            labor_plans[community_id] = plan
        result = activity_economy.plan_activity_economy(
            None, self.registry, self.spatial, scarce,
            _gross(scarce), turn=2,
            labor_plans_by_community=labor_plans)
        for community_id, row in result["state"]["communities"].items():
            plan = labor_plans[community_id]
            self.assertEqual(
                row["required_worker_count_by_good"],
                plan["required_worker_count_by_good"])
            self.assertEqual(
                row["active_worker_count_by_good"],
                plan["active_worker_count_by_good"])
            self.assertEqual(
                {good: activity["worker_count"]
                 for good, activity in row["activities"].items()},
                plan["active_worker_count_by_good"])
            self.assertEqual(
                sum(activity["worker_count"]
                    for activity in row["activities"].values()), 4)

    def test_activity_ledger_is_projected_to_persistent_sites(self):
        planned = activity_economy.plan_activity_economy(
            None, self.registry, self.spatial, self.settlements,
            _gross(self.settlements), turn=2)

        synchronized = synchronize_spatial_state(
            self.spatial, planned["registry"], 2,
            activity_economy_state=planned["state"])
        active_sites = [
            row for row in synchronized["sites"].values()
            if row.get("active", True)]
        self.assertEqual(
            sum(row["activity_worker_count"] for row in active_sites),
            sum(row["active_worker_count"] for row in
                planned["state"]["communities"].values()))
        for good in activity_economy.ACTIVITY_GOODS:
            self.assertEqual(
                sum(row["activity_worker_count_by_good"].get(good, 0)
                    for row in active_sites),
                sum(row["active_worker_count_by_good"][good]
                    for row in planned["state"]["communities"].values()))
            self.assertAlmostEqual(
                sum(row["activity_output_by_good"].get(good, 0.0)
                    for row in active_sites),
                sum(row[good] for row in _gross(self.settlements).values()),
                places=6)
        frame = build_spatial_keyframe(synchronized)
        self.assertTrue(any(row[11] == 2 and row[12] > 0
                            for row in frame["sites"]))
        self.assertTrue(any(row[16] for row in frame["sites"]))

    def test_large_population_keeps_anonymous_workers_aggregated(self):
        settlements = initial_settlement_network(
            _economy(), total_population=1_000_000)
        registry = initial_resident_registry(
            settlements, world_seed=42)
        spatial = initial_spatial_state(registry, turn=1)

        result = activity_economy.plan_activity_economy(
            None, registry, spatial, settlements,
            _gross(settlements), turn=2)

        named = sum(
            len(activity["named_worker_ids"])
            for community in result["state"]["communities"].values()
            for activity in community["activities"].values())
        anonymous = sum(
            activity["anonymous_worker_count"]
            for community in result["state"]["communities"].values()
            for activity in community["activities"].values())
        self.assertLessEqual(named, registry["named_resident_limit"])
        self.assertGreater(anonymous, 250_000)
        self.assertEqual(
            named + anonymous,
            sum(row["active_worker_count"] for row in
                result["state"]["communities"].values()))
        self.assertEqual(
            len(result["registry"]["residents"]),
            len(registry["residents"]))

    def test_zero_population_output_is_preserved_as_unlocated_not_fake_labor(self):
        settlements = copy.deepcopy(self.settlements)
        for row in settlements.values():
            row["population"] = 0
            row["productive_population"] = 0

        result = activity_economy.plan_activity_economy(
            None, self.registry, self.spatial, settlements,
            _gross(settlements), turn=2)

        for community in result["state"]["communities"].values():
            self.assertEqual(
                community["background_production_labor_factor"], 0.0)
            for activity in community["activities"].values():
                self.assertEqual(activity["worker_count"], 0)
                self.assertEqual(activity["site_allocations"], [])
                self.assertEqual(
                    activity["unlocated_gross_output"],
                    activity["gross_output"])

    def test_stale_activity_is_not_replayed_and_current_closed_site_is_retained(self):
        planned = activity_economy.plan_activity_economy(
            None, self.registry, self.spatial, self.settlements,
            _gross(self.settlements), turn=2)
        stale = copy.deepcopy(self.spatial)
        spatial_module._sync_site_activity(stale, planned["state"], turn=3)
        self.assertTrue(all(
            row["activity_worker_count"] == 0
            for row in stale["sites"].values()))

        current = copy.deepcopy(self.spatial)
        allocated_site = next(
            allocation["site_id"]
            for community in planned["state"]["communities"].values()
            for activity in community["activities"].values()
            for allocation in activity["site_allocations"]
            if allocation["worker_count"] > 0)
        current["sites"][allocated_site]["active"] = False
        spatial_module._sync_site_activity(current, planned["state"], turn=2)
        self.assertGreater(
            current["sites"][allocated_site]["activity_worker_count"], 0)
        frame = build_spatial_keyframe(current, turn=2)
        self.assertIn(allocated_site, [row[0] for row in frame["sites"]])

    def test_unknown_state_version_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported activity"):
            activity_economy.plan_activity_economy(
                {"version": 99}, self.registry, self.spatial,
                self.settlements, _gross(self.settlements), turn=2)

    def test_version_one_state_upgrades_to_per_good_labor_version(self):
        legacy = activity_economy.initial_activity_economy_state()
        legacy["version"] = activity_economy.LEGACY_ACTIVITY_ECONOMY_VERSION
        result = activity_economy.plan_activity_economy(
            legacy, self.registry, self.spatial, self.settlements,
            _gross(self.settlements), turn=2)
        self.assertEqual(
            result["state"]["version"],
            activity_economy.ACTIVITY_ECONOMY_VERSION)
        self.assertTrue(all(
            "required_worker_count_by_good" in row
            for row in result["state"]["communities"].values()))


if __name__ == "__main__":
    unittest.main()
