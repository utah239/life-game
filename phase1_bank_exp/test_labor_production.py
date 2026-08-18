# -*- coding: utf-8 -*-
"""背景生産が実在する生産年齢人口へ依存することの縦断テスト。"""
import copy
import random
import unittest

import game
from dashboard import build_dashboard
from dashboard.aquarium_worker import _summary
from institutions.activity_economy import (
    ESSENTIAL_PRODUCTION_WORKER_SHARE,
    background_production_labor_factor,
    background_production_labor_plan,
)
from institutions.population import (
    AGE_COHORT_CHILDREN,
    AGE_COHORT_ELDERLY,
    AGE_COHORT_PRODUCTIVE,
)


class BackgroundProductionLaborFactorTest(unittest.TestCase):
    def test_required_essential_workers_are_not_all_productive_people(self):
        for population in (1, 52, 120, 1_000_000):
            productive = round(population * 0.62)
            plan = background_production_labor_plan(population, productive)
            self.assertEqual(
                background_production_labor_factor(population, productive),
                1.0)
            self.assertEqual(
                plan["required_worker_count"],
                max(1, round(
                    population * ESSENTIAL_PRODUCTION_WORKER_SHARE)))
            self.assertEqual(
                plan["active_worker_count"]
                + plan["unassigned_productive_population"], productive)

    def test_labor_shortage_scales_and_zero_labor_stops_production(self):
        self.assertEqual(background_production_labor_factor(100, 15), 0.5)
        self.assertEqual(background_production_labor_factor(100, 0), 0.0)
        self.assertEqual(background_production_labor_factor(0, 0), 0.0)
        self.assertEqual(background_production_labor_factor(100, 100), 1.0)

    def test_factor_does_not_consume_rng(self):
        before = random.getstate()
        background_production_labor_factor(100, 15)
        self.assertEqual(random.getstate(), before)

    def test_per_good_worker_counts_preserve_totals_and_caps(self):
        plan = background_production_labor_plan(
            100, 15,
            {"food": 0.0, "medicine": 100.0,
             "shelter": 100.0, "tools": 100.0})
        self.assertEqual(
            sum(plan["required_worker_count_by_good"].values()),
            plan["required_worker_count"])
        self.assertEqual(
            sum(plan["active_worker_count_by_good"].values()),
            plan["active_worker_count"])
        for good in ("food", "medicine", "shelter", "tools"):
            self.assertLessEqual(
                plan["active_worker_count_by_good"][good],
                plan["required_worker_count_by_good"][good])

    def test_short_good_receives_more_scarce_labor_without_creating_workers(self):
        neutral = background_production_labor_plan(100, 15)
        food_short = background_production_labor_plan(
            100, 15,
            {"food": 0.0, "medicine": 100.0,
             "shelter": 100.0, "tools": 100.0})
        self.assertGreater(
            food_short["active_worker_count_by_good"]["food"],
            neutral["active_worker_count_by_good"]["food"])
        self.assertEqual(
            sum(food_short["active_worker_count_by_good"].values()), 15)


class LaborAwareBarterUpkeepTest(unittest.TestCase):
    def test_default_factor_preserves_existing_full_production(self):
        implicit = game.plan_barter_upkeep(
            game.FOOD_STOCK_INITIAL, game.MEDICINE_STOCK_INITIAL,
            game.SHELTER_DURABILITY_INITIAL, game.TOOLS_DURABILITY_INITIAL,
            game.PRODUCTION_CAPACITY_INITIAL)
        explicit = game.plan_barter_upkeep(
            game.FOOD_STOCK_INITIAL, game.MEDICINE_STOCK_INITIAL,
            game.SHELTER_DURABILITY_INITIAL, game.TOOLS_DURABILITY_INITIAL,
            game.PRODUCTION_CAPACITY_INITIAL, labor_factor=1.0)
        self.assertEqual(implicit, explicit)
        self.assertEqual(implicit["background_production_labor_factor"], 1.0)

    def test_half_labor_halves_gross_and_zero_labor_stops_it(self):
        full = game.plan_barter_upkeep(
            90.0, 92.0, 21.0, 21.0,
            game.PRODUCTION_CAPACITY_INITIAL, labor_factor=1.0)
        half = game.plan_barter_upkeep(
            90.0, 92.0, 21.0, 21.0,
            game.PRODUCTION_CAPACITY_INITIAL, labor_factor=0.5)
        none = game.plan_barter_upkeep(
            90.0, 92.0, 21.0, 21.0,
            game.PRODUCTION_CAPACITY_INITIAL, labor_factor=0.0)
        for good in ("food", "medicine", "shelter", "tools"):
            self.assertAlmostEqual(
                half["gross_production"][good],
                full["gross_production"][good] / 2, places=6)
            self.assertEqual(none["gross_production"][good], 0.0)
            self.assertEqual(
                none[f"{good}_delta"],
                -full["gross_consumption"][good])

    def test_factor_is_bounded_without_mutating_capacity_or_rng(self):
        before = random.getstate()
        above = game.plan_barter_upkeep(
            90.0, 92.0, 21.0, 21.0,
            game.PRODUCTION_CAPACITY_INITIAL, labor_factor=4.0)
        below = game.plan_barter_upkeep(
            90.0, 92.0, 21.0, 21.0,
            game.PRODUCTION_CAPACITY_INITIAL, labor_factor=-2.0)
        self.assertEqual(above["background_production_labor_factor"], 1.0)
        self.assertEqual(below["background_production_labor_factor"], 0.0)
        self.assertEqual(random.getstate(), before)

    def test_per_good_factors_scale_only_the_corresponding_production(self):
        result = game.plan_barter_upkeep(
            90.0, 92.0, 21.0, 21.0,
            game.PRODUCTION_CAPACITY_INITIAL,
            labor_factor=0.5,
            labor_factors_by_good={
                "food": 1.0, "medicine": 0.5,
                "shelter": 0.25, "tools": 0.0})
        raw = game.background_goods_production(
            game.PRODUCTION_CAPACITY_INITIAL)
        self.assertEqual(result["gross_production"]["food"],
                         raw["food_delta"])
        self.assertEqual(result["gross_production"]["medicine"],
                         round(raw["medicine_delta"] * 0.5, 6))
        self.assertEqual(result["gross_production"]["shelter"],
                         round(raw["shelter_delta"] * 0.25, 6))
        self.assertEqual(result["gross_production"]["tools"], 0.0)
        self.assertEqual(
            result["background_production_labor_factors_by_good"], {
                "food": 1.0, "medicine": 0.5,
                "shelter": 0.25, "tools": 0.0})

    def test_practice_productivity_multiplies_after_labor_by_good(self):
        result = game.plan_barter_upkeep(
            90.0, 92.0, 21.0, 21.0,
            game.PRODUCTION_CAPACITY_INITIAL,
            labor_factor=0.5,
            labor_factors_by_good={
                "food": 1.0, "medicine": 0.5,
                "shelter": 0.25, "tools": 0.0},
            productivity_factors_by_good={
                "food": 1.2, "medicine": 1.1,
                "shelter": 1.05, "tools": 1.2})
        raw = game.background_goods_production(
            game.PRODUCTION_CAPACITY_INITIAL)
        self.assertEqual(
            result["gross_production"]["food"],
            round(raw["food_delta"] * 1.0 * 1.2, 6))
        self.assertEqual(
            result["gross_production"]["medicine"],
            round(raw["medicine_delta"] * 0.5 * 1.1, 6))
        self.assertEqual(
            result["gross_production"]["shelter"],
            round(raw["shelter_delta"] * 0.25 * 1.05, 6))
        self.assertEqual(result["gross_production"]["tools"], 0.0)
        self.assertEqual(
            result[
                "background_production_productivity_factors_by_good"], {
                    "food": 1.2, "medicine": 1.1,
                    "shelter": 1.05, "tools": 1.2})

    def test_missing_productivity_factor_is_neutral(self):
        result = game.plan_barter_upkeep(
            90.0, 92.0, 21.0, 21.0,
            game.PRODUCTION_CAPACITY_INITIAL,
            productivity_factors_by_good={"food": 1.1})
        self.assertEqual(
            result[
                "background_production_productivity_factors_by_good"], {
                    "food": 1.1, "medicine": 1.0,
                    "shelter": 1.0, "tools": 1.0})


class WorldLaborProductionIntegrationTest(unittest.TestCase):
    def test_world_practice_is_learned_after_actual_work_and_published(self):
        data = game.collect_visualize_trace(
            23, "cautious", 3, game.SAFETY_FLOOR, "health",
            include_resume_state=True)
        checkpoint = data["resume_state"]
        practices = [
            row["local_economy"]["production_practice_by_good"]
            for row in checkpoint["settlements"].values()]
        self.assertTrue(any(
            value > 0.0 for row in practices for value in row.values()))

        latest = data["trace"]["turns"][-1]
        self.assertEqual(
            latest["settlement_production_practice_by_good"],
            {sid: row["local_economy"]["production_practice_by_good"]
             for sid, row in checkpoint["settlements"].items()})
        for key in (
                "production_practice_by_good",
                "production_productivity_factors_by_good",
                "world_production_practice_by_good",
                "world_production_productivity_factors_by_good",
                "activity_community_production_practice_by_good"):
            self.assertIn(key, latest)

        summary = _summary(checkpoint, 2400)
        self.assertEqual(
            summary["production_practice_by_good"],
            checkpoint["settlements"][checkpoint["focus_settlement_id"]]
            ["local_economy"]["production_practice_by_good"])
        self.assertGreater(
            max(summary["world_production_practice_by_good"].values()), 0.0)
        self.assertGreater(
            max(summary[
                "world_production_productivity_factors_by_good"].values()),
            1.0)
        built = build_dashboard.build_dashboard_data(data)
        self.assertEqual(
            built["meta"]["world_production_practice_by_good"],
            built["turns"][-1]["world_production_practice_by_good"])
        self.assertEqual(
            built["residents"][0]["activity_count"],
            built["residents"][0]["unclassified_activity_count"]
            + sum(built["residents"][0][
                "activity_counts_by_good"].values()))

    def test_children_only_community_has_no_background_production(self):
        first = game.simulate_policy(
            "cautious", 1, 17, game.SAFETY_FLOOR, "health",
            continue_world=True)
        checkpoint = copy.deepcopy(first["resume_state"])
        community_id = next(
            key for key in sorted(checkpoint["settlements"])
            if key != checkpoint["focus_settlement_id"])
        settlement = checkpoint["settlements"][community_id]
        population = settlement["population"]
        settlement["age_cohorts"] = {
            AGE_COHORT_CHILDREN: population,
            AGE_COHORT_PRODUCTIVE: 0,
            AGE_COHORT_ELDERLY: 0,
        }
        settlement["productive_population"] = 0
        settlement["age_transition_carry"] = {
            "children_to_productive": 0.0,
            "productive_to_elderly": 0.0,
        }

        resumed = game.simulate_policy(
            "cautious", 2, 17, game.SAFETY_FLOOR, "health",
            continue_world=True, resume_state=checkpoint)
        activity = resumed["activity_economy_state"]["communities"][
            community_id]
        self.assertEqual(
            activity["background_production_labor_factor"], 0.0)
        self.assertEqual(activity["productive_population"], 0)
        for row in activity["activities"].values():
            self.assertEqual(row["gross_output"], 0.0)
            self.assertEqual(row["worker_count"], 0)
            self.assertEqual(row["site_allocations"], [])
            self.assertEqual(row["unlocated_gross_output"], 0.0)
        summary = _summary(resumed["resume_state"], 2400)
        self.assertGreater(summary["activity_required_worker_total"], 0)
        self.assertGreaterEqual(
            summary["activity_labor_constrained_community_count"], 1)
        self.assertLess(summary["activity_min_labor_factor"], 1.0)
        self.assertEqual(
            summary["activity_worker_total"]
            + summary["activity_unassigned_productive_total"],
            summary["total_productive_population"])
        self.assertEqual(
            sum(summary["activity_workers_by_good"].values()),
            summary["activity_worker_total"])
        self.assertEqual(
            sum(summary["activity_required_workers_by_good"].values()),
            summary["activity_required_worker_total"])


if __name__ == "__main__":
    unittest.main()
