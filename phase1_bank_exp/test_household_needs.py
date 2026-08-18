# -*- coding: utf-8 -*-
"""人口需要・世帯配賦・財の需要/供給分離の保存則。"""
import copy
import random
import unittest

import game
from institutions import barter, household_needs, settlement_network
from institutions.intersettlement_trade import plan_intersettlement_trade
from institutions.residents import initial_resident_registry


HOME_ECONOMY = {
    "food": barter.FOOD_STOCK_INITIAL,
    "medicine": barter.MEDICINE_STOCK_INITIAL,
    "shelter": barter.SHELTER_DURABILITY_INITIAL,
    "tools": barter.TOOLS_DURABILITY_INITIAL,
    "production_capacity": barter.PRODUCTION_CAPACITY_INITIAL,
    "barter_stage": barter.BARTER_STAGE_FUNCTIONING,
    "community_trust": 50.0,
    "local_credit_stage": 0,
}


class HouseholdNeedsConservationTest(unittest.TestCase):
    def _world(self, population=None):
        settlements = settlement_network.initial_settlement_network(
            HOME_ECONOMY, total_population=population)
        registry = initial_resident_registry(settlements, 7, 1)
        return settlements, registry

    def test_default_world_demand_and_provisioning_both_total_three(self):
        settlements, registry = self._world()
        state = household_needs.build_household_needs_state(
            settlements, registry, 1)
        self.assertEqual(
            state["world_demand_scale_by_good"],
            {good: 3.0 for good in household_needs.NEED_GOODS})
        self.assertEqual(round(sum(
            row["local_economy"]["provisioning_scale"]
            for row in settlements.values()), 12), 3.0)
        self.assertTrue(household_needs.verify_household_needs_state(
            state, settlements))

    def test_named_households_plus_anonymous_exactly_equal_community(self):
        settlements, registry = self._world(10_000)
        state = household_needs.build_household_needs_state(
            settlements, registry, 15)
        self.assertLessEqual(len(registry["residents"]), 4096)
        for row in state["communities"].values():
            self.assertEqual(
                row["named_population"] + row["anonymous_population"],
                row["population"])
            for good in household_needs.NEED_GOODS:
                named = sum(
                    h["demand_quantity_by_good"][good]
                    for h in row["household_demands"].values())
                self.assertAlmostEqual(
                    named + row["anonymous_demand_quantity_by_good"][good],
                    row["demand_quantity_by_good"][good], places=8)

    def test_million_population_is_bounded_and_per_capita_equivalent(self):
        settlements, registry = self._world(1_000_000)
        state = household_needs.build_household_needs_state(
            settlements, registry, 1)
        self.assertEqual(len(registry["residents"]), 4096)
        self.assertEqual(sum(
            row["anonymous_population"]
            for row in state["communities"].values()), 1_000_000 - 4096)
        self.assertEqual(
            state["world_demand_scale_by_good"]["shelter"], 12_000.0)
        self.assertTrue(all(
            11_900.0 <= state["world_demand_scale_by_good"][good] <= 12_100.0
            for good in household_needs.NEED_GOODS))
        self.assertLessEqual(sum(
            len(row["household_demands"])
            for row in state["communities"].values()), 1025)

    def test_migration_moves_demand_but_preserves_world_total(self):
        settlements, registry = self._world()
        settlements["home"]["stage"] = 3
        settlements["home"]["local_economy"]["worst_shortfall"] = 95.0
        before = household_needs.build_household_needs_state(
            settlements, registry, 10)
        plan = settlement_network.plan_migration(settlements, 10)
        after = household_needs.build_household_needs_state(
            plan["settlements"], None, 10)
        self.assertGreater(plan["migrants"], 0)
        self.assertEqual(
            before["world_demand_scale_by_good"],
            after["world_demand_scale_by_good"])
        self.assertNotEqual(
            before["communities"]["home"]["demand_scale_by_good"],
            after["communities"]["home"]["demand_scale_by_good"])

    def test_birth_and_death_change_demand_from_integer_cohorts(self):
        settlements, _ = self._world()
        before = household_needs.demand_scales_by_good(settlements["home"])
        changed = copy.deepcopy(settlements["home"])
        changed["population"] += 1
        changed["age_cohorts"]["children"] += 1
        after_birth = household_needs.demand_scales_by_good(changed)
        self.assertTrue(all(
            after_birth[good] > before[good]
            for good in household_needs.NEED_GOODS))
        changed["population"] -= 1
        changed["age_cohorts"]["children"] -= 1
        self.assertEqual(
            household_needs.demand_scales_by_good(changed), before)

    def test_upkeep_consumes_by_people_but_produces_by_infrastructure(self):
        deps = game._barter_upkeep_dependencies()
        neutral = game.plan_barter_upkeep(
            90.0, 92.0, 1.0, 21.0, 21.0,
            provisioning_scale=1.0,
            demand_scales_by_good={good: 1.0 for good in household_needs.NEED_GOODS})
        crowded = game.plan_barter_upkeep(
            90.0, 92.0, 1.0, 21.0, 21.0,
            provisioning_scale=1.0,
            demand_scales_by_good={good: 2.0 for good in household_needs.NEED_GOODS})
        self.assertEqual(neutral["food_delta"], 0.0)
        self.assertEqual(crowded["gross_production"], neutral["gross_production"])
        self.assertEqual(
            crowded["gross_consumption"]["food"],
            neutral["gross_consumption"]["food"] * 2)
        self.assertLess(crowded["food_after"], neutral["food_after"])
        self.assertTrue(deps)

    def test_observation_does_not_mutate_or_consume_rng(self):
        settlements, registry = self._world()
        before_settlements = copy.deepcopy(settlements)
        before_registry = copy.deepcopy(registry)
        rng = random.getstate()
        household_needs.build_household_needs_state(
            settlements, registry, 50)
        self.assertEqual(settlements, before_settlements)
        self.assertEqual(registry, before_registry)
        self.assertEqual(random.getstate(), rng)

    def test_trade_target_follows_people_demand_and_preserves_goods(self):
        settlements, _ = self._world()
        rows = {
            "source": copy.deepcopy(settlements["home"]),
            "destination": copy.deepcopy(settlements["riverside"]),
        }
        for settlement_id, row in rows.items():
            row["id"] = settlement_id
            row["population"] = 10
            row["stage"] = 0
            economy = row["local_economy"]
            economy["provisioning_scale"] = 1.0
            economy["local_credit_stage"] = 0
            economy["demand_scales_by_good"] = {
                good: 1.0 for good in household_needs.NEED_GOODS}
            for good in household_needs.NEED_GOODS:
                economy[good] = barter.goods_reference(good)
        rows["source"]["local_economy"]["demand_scales_by_good"]["food"] = 0.5
        rows["destination"]["local_economy"][
            "demand_scales_by_good"]["food"] = 2.0
        before = sum(row["local_economy"]["food"] for row in rows.values())
        plan = plan_intersettlement_trade(rows, 7)
        food_events = [
            row for row in plan["events"] if row["good"] == "food"]
        self.assertTrue(food_events)
        self.assertEqual(food_events[0]["from_settlement"], "source")
        self.assertEqual(food_events[0]["to_settlement"], "destination")
        self.assertEqual(round(sum(
            row["local_economy"]["food"]
            for row in plan["settlements"].values()), 6), before)


if __name__ == "__main__":
    unittest.main()
