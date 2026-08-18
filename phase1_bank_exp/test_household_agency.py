# -*- coding: utf-8 -*-
import copy
import random
import unittest

import game
from dashboard.build_dashboard import build_dashboard_data
from institutions import activity_economy, household_agency, household_goods
from institutions.residents import initial_resident_registry
from institutions.settlement_network import initial_settlement_network
from institutions.spatial import initial_spatial_state


GOODS = household_goods.HOUSEHOLD_GOODS


def _settlements(stage=0, quantity=100.0, population=4):
    return {"a": {
        "id": "a", "population": population,
        "local_economy": {
            **{good: float(quantity) for good in GOODS},
            "provisioning_scale": 1.0,
            "local_credit_stage": int(stage),
        },
    }}


def _registry():
    return {
        "residents": {
            "r1": {"id": "r1", "household_id": "h1",
                   "settlement_id": "a", "alive": True},
            "r2": {"id": "r2", "household_id": "h2",
                   "settlement_id": "a", "alive": True},
        },
        "households": {
            "h1": {"id": "h1", "settlement_id": "a",
                   "livelihood": "food", "active": True},
            "h2": {"id": "h2", "settlement_id": "a",
                   "livelihood": "medicine", "active": True},
        },
    }


def _needs():
    demands = {
        household_id: {
            "household_id": household_id, "population": 1,
            "demand_quantity_by_good": {good: 10.0 for good in GOODS},
        }
        for household_id in ("h1", "h2")}
    return {
        "version": 1, "updated_turn": 1,
        "communities": {"a": {
            "settlement_id": "a", "population": 4,
            "named_population": 2, "anonymous_population": 2,
            "household_demands": demands,
            "anonymous_demand_quantity_by_good": {
                good: 20.0 for good in GOODS},
        }},
    }


def _organizations():
    return {"organizations": {}}


class HouseholdAgencyTest(unittest.TestCase):
    def plan(self, *, stage=0, quantity=100.0, state=None, turn=1):
        settlements = _settlements(stage, quantity)
        registry = _registry()
        needs = _needs()
        organizations = _organizations()
        goods = household_goods.reconcile_household_goods_state(
            None, settlements, registry, needs, organizations, 0)["state"]
        result = household_agency.plan_household_agency(
            state, goods, settlements, registry, needs, organizations, turn)
        return result, settlements, registry, needs, organizations

    def test_healthy_shared_ledger_grants_one_month_need_without_new_goods(self):
        result, settlements, registry, needs, organizations = self.plan()
        goods = result["household_goods_state"]
        for household_id in ("h1", "h2"):
            self.assertEqual(goods["households"][household_id]["holdings"], {
                good: 10.0 for good in GOODS})
            self.assertIsNone(
                result["state"]["households"][household_id][
                    "priority_good"])
        self.assertEqual(goods["anonymous_pools"]["a"]["holdings"], {
            good: 20.0 for good in GOODS})
        self.assertEqual(goods["common_pool_by_community"]["a"], {
            good: 60.0 for good in GOODS})
        self.assertTrue(household_goods.verify_household_goods_state(
            goods, settlements, registry, needs, organizations))
        self.assertEqual(goods["world_physical_goods"], {
            good: 100.0 for good in GOODS})
        access = next(
            row for row in result["household_goods_events"]
            if row.get("kind") == "household_common_goods_accessed")
        self.assertNotIn("named_household_allocations", access)
        self.assertEqual(access["named_household_count"], 2)
        self.assertLessEqual(
            len(access["named_household_allocations_sample"]), 32)

    def test_credit_contraction_limits_access_and_households_choose_own_trade(self):
        result, *_ = self.plan(stage=1)
        rows = result["state"]["households"]
        self.assertEqual(rows["h1"]["coverage_by_good"], {
            good: 70.0 for good in GOODS})
        self.assertEqual(rows["h1"]["priority_good"], "food")
        self.assertEqual(rows["h2"]["priority_good"], "medicine")
        self.assertEqual(
            result["state"]["communities"]["a"][
                "priority_pressure_by_good"],
            {good: 0.3 for good in GOODS})

    def test_isolation_stops_common_access_but_does_not_delete_common_goods(self):
        result, *_ = self.plan(stage=3)
        goods = result["household_goods_state"]
        self.assertEqual(goods["common_pool_by_community"]["a"], {
            good: 100.0 for good in GOODS})
        self.assertEqual(goods["households"]["h1"]["holdings"], {
            good: 0.0 for good in GOODS})
        self.assertEqual(
            result["state"]["communities"]["a"][
                "priority_pressure_by_good"],
            {good: 1.0 for good in GOODS})

    def test_scarce_common_goods_are_shared_proportionally(self):
        result, *_ = self.plan(quantity=20.0)
        goods = result["household_goods_state"]
        self.assertEqual(goods["households"]["h1"]["holdings"], {
            good: 5.0 for good in GOODS})
        self.assertEqual(goods["households"]["h2"]["holdings"], {
            good: 5.0 for good in GOODS})
        self.assertEqual(goods["anonymous_pools"]["a"]["holdings"], {
            good: 10.0 for good in GOODS})
        self.assertEqual(goods["common_pool_by_community"]["a"], {
            good: 0.0 for good in GOODS})

    def test_same_turn_replan_is_idempotent_and_next_turn_ages_shortage(self):
        first, settlements, registry, needs, organizations = self.plan(stage=3)
        same = household_agency.plan_household_agency(
            first["state"], first["household_goods_state"], settlements,
            registry, needs, organizations, 1)
        following = household_agency.plan_household_agency(
            same["state"], same["household_goods_state"], settlements,
            registry, needs, organizations, 2)
        self.assertEqual(
            first["state"]["households"]["h1"][
                "consecutive_shortage_months"], 1)
        self.assertEqual(same["state"], first["state"])
        self.assertEqual(
            following["state"]["households"]["h1"][
                "consecutive_shortage_months"], 2)

    def test_inputs_and_rng_are_unchanged(self):
        settlements = _settlements(stage=1)
        registry = _registry()
        needs = _needs()
        organizations = _organizations()
        goods = household_goods.reconcile_household_goods_state(
            None, settlements, registry, needs, organizations, 0)["state"]
        inputs = copy.deepcopy(
            (goods, settlements, registry, needs, organizations))
        random.seed(91)
        before_rng = random.getstate()
        household_agency.plan_household_agency(
            None, goods, settlements, registry, needs, organizations, 1)
        self.assertEqual(random.getstate(), before_rng)
        self.assertEqual(
            (goods, settlements, registry, needs, organizations), inputs)

    def test_verifier_rejects_stale_pressure_cache(self):
        result, _, _, needs, _ = self.plan(stage=1)
        state = copy.deepcopy(result["state"])
        state["world_priority_pressure_by_good"]["food"] = 0.0
        self.assertFalse(household_agency.verify_household_agency_state(
            state, result["household_goods_state"], needs))

    def test_household_pressure_reallocates_workers_without_creating_labor(self):
        neutral = activity_economy.background_production_labor_plan(
            100, 15)
        pressured = activity_economy.background_production_labor_plan(
            100, 15, household_pressure_by_good={"medicine": 1.0})
        self.assertGreater(
            pressured["active_worker_count_by_good"]["medicine"],
            neutral["active_worker_count_by_good"]["medicine"])
        self.assertEqual(
            sum(pressured["active_worker_count_by_good"].values()), 15)
        self.assertEqual(
            pressured["required_worker_count_by_good"],
            neutral["required_worker_count_by_good"])

    def test_named_worker_selection_honors_household_priority(self):
        economy = {
            "food": 90.0, "medicine": 92.0, "shelter": 80.0,
            "tools": 80.0, "production_capacity": 50.0,
            "barter_stage": 0, "community_trust": 50.0,
            "local_credit_stage": 0,
        }
        settlements = initial_settlement_network(economy)
        registry = initial_resident_registry(settlements, world_seed=41)
        spatial = initial_spatial_state(registry, turn=1)
        community_id = next(iter(settlements))
        candidate = next(
            row for row in registry["residents"].values()
            if row.get("alive", True)
            and row.get("settlement_id") == community_id)
        household_id = candidate["household_id"]
        productive = settlements[community_id]["productive_population"]
        labor_plan = {
            "productive_population": productive,
            "required_worker_count": 1,
            "active_worker_count": 1,
            "required_worker_count_by_good": {
                "food": 0, "medicine": 1, "shelter": 0, "tools": 0},
            "active_worker_count_by_good": {
                "food": 0, "medicine": 1, "shelter": 0, "tools": 0},
            "unassigned_productive_population": productive - 1,
            "labor_factor": 1.0,
            "provisioning_scale": 1.0,
        }
        result = activity_economy.plan_activity_economy(
            None, registry, spatial, settlements,
            {community_id: {good: (1.0 if good == "medicine" else 0.0)
                            for good in GOODS}}, 2,
            labor_plans_by_community={community_id: labor_plan},
            household_priorities={household_id: "medicine"})
        activity = result["state"]["communities"][community_id][
            "activities"]["medicine"]
        self.assertEqual(activity["named_household_ids"], [household_id])
        self.assertEqual(activity["responding_household_ids"], [household_id])


class HouseholdAgencyWorldIntegrationTest(unittest.TestCase):
    def test_world_checkpoint_trace_and_result_share_agency_state(self):
        data = game.collect_visualize_trace(
            7, "cautious", 24, 30, "health", world_mode=True,
            include_resume_state=True)
        state = data["household_agency_state"]
        self.assertEqual(
            data["resume_state"]["household_agency_state"], state)
        self.assertTrue(household_agency.verify_household_agency_state(
            state, data["household_goods_state"],
            data["household_needs_state"]))
        self.assertTrue(data["trace"]["household_agency_events"])
        self.assertTrue(all(
            row["kind"] == "household_response_summary"
            for row in data["trace"]["household_agency_events"]))
        self.assertTrue(any(
            row.get("responding_household_ids")
            for community in data["activity_economy_state"][
                "communities"].values()
            for row in community["activities"].values()))
        dashboard = build_dashboard_data(data, 12)
        self.assertIn("focus_household_response", dashboard["turns"][-1])
        self.assertIn(
            "world_household_priority_pressure_by_good",
            dashboard["meta"])
        self.assertTrue(all(
            "priority_good" in row and "response_shortfall_by_good" in row
            for row in dashboard["households"]))
        self.assertTrue(any(
            row["kind"] == "household_response_summary"
            for row in dashboard["observer_events"]))

    def test_million_person_world_keeps_agency_aggregated(self):
        result = game.simulate_policy(
            "cautious", 1, 7, 30, "health", continue_world=True,
            initial_population=1_000_000)
        state = result["household_agency_state"]
        self.assertLessEqual(len(state["households"]), 4096)
        self.assertLessEqual(
            len(state["communities"]), len(result["settlements"]))
        self.assertGreater(sum(
            row["anonymous_population"]
            for row in state["communities"].values()), 990_000)


if __name__ == "__main__":
    unittest.main()
