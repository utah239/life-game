# -*- coding: utf-8 -*-
"""共同体の生産経験が財在庫と異なる強度値であることのテスト。"""
import copy
import json
import random
import unittest

from institutions import production_practice as practice


class ProductionPracticeTest(unittest.TestCase):
    def test_initial_and_partial_state_are_normalized_without_mutation(self):
        raw = {"food": 25.0, "medicine": -1.0, "unknown": 90.0}
        before = copy.deepcopy(raw)
        self.assertEqual(practice.initial_production_practice(), {
            good: 0.0 for good in practice.PRACTICE_GOODS})
        self.assertEqual(practice.normalize_production_practice(raw), {
            "food": 25.0, "medicine": 0.0,
            "shelter": 0.0, "tools": 0.0})
        self.assertEqual(raw, before)

    def test_invalid_or_nonfinite_state_is_rejected(self):
        with self.assertRaises(TypeError):
            practice.normalize_production_practice([])
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                practice.normalize_production_practice({"food": value})

    def test_productivity_factor_is_bounded_from_one_to_one_point_two(self):
        self.assertEqual(practice.practice_productivity_factor(-20), 1.0)
        self.assertEqual(practice.practice_productivity_factor(0), 1.0)
        self.assertEqual(practice.practice_productivity_factor(50), 1.1)
        self.assertEqual(practice.practice_productivity_factor(100), 1.2)
        self.assertEqual(practice.practice_productivity_factor(140), 1.2)

    def test_full_staff_learning_halves_remaining_gap_in_120_months(self):
        state = practice.initial_production_practice()
        required = {good: 10 for good in practice.PRACTICE_GOODS}
        active = dict(required)
        for _ in range(practice.PRACTICE_LEARNING_HALF_LIFE_MONTHS):
            state = practice.plan_production_practice(
                state, required, active)["practice_after_by_good"]
        for value in state.values():
            self.assertAlmostEqual(value, 50.0, delta=0.0001)

    def test_idle_decay_halves_practice_in_sixty_months(self):
        state = {good: 80.0 for good in practice.PRACTICE_GOODS}
        empty = {good: 0 for good in practice.PRACTICE_GOODS}
        for _ in range(practice.PRACTICE_IDLE_DECAY_HALF_LIFE_MONTHS):
            state = practice.plan_production_practice(
                state, empty, empty)["practice_after_by_good"]
        for value in state.values():
            self.assertAlmostEqual(value, 40.0, delta=0.0001)

    def test_partial_staff_learns_slower_and_idle_good_decays(self):
        result = practice.plan_production_practice(
            {"food": 20.0, "medicine": 20.0,
             "shelter": 20.0, "tools": 20.0},
            {"food": 10, "medicine": 10, "shelter": 10, "tools": 10},
            {"food": 10, "medicine": 5, "shelter": 1, "tools": 0})
        after = result["practice_after_by_good"]
        self.assertGreater(after["food"], after["medicine"])
        self.assertGreater(after["medicine"], after["shelter"])
        self.assertGreater(after["shelter"], 20.0)
        self.assertLess(after["tools"], 20.0)
        self.assertEqual(result["labor_coverage_by_good"], {
            "food": 1.0, "medicine": 0.5,
            "shelter": 0.1, "tools": 0.0})

    def test_current_factor_precedes_learning_and_after_factor_is_next_month(self):
        result = practice.plan_production_practice(
            None,
            {good: 1 for good in practice.PRACTICE_GOODS},
            {good: 1 for good in practice.PRACTICE_GOODS})
        self.assertEqual(result["productivity_factors_by_good"], {
            good: 1.0 for good in practice.PRACTICE_GOODS})
        self.assertTrue(all(
            value > 1.0
            for value in result["productivity_factors_after_by_good"].values()))

    def test_population_weighted_world_value_is_intensive(self):
        weighted = practice.population_weighted_production_practice([
            (25, {"food": 100.0}),
            (75, {"food": 20.0}),
            (0, {"food": 100.0}),
        ])
        self.assertEqual(weighted, {
            "food": 40.0, "medicine": 0.0,
            "shelter": 0.0, "tools": 0.0,
        })
        self.assertEqual(
            practice.population_weighted_production_practice([]),
            practice.initial_production_practice())

    def test_population_weight_rejects_nonfinite_values(self):
        with self.assertRaisesRegex(ValueError, "population must be finite"):
            practice.population_weighted_production_practice([
                (float("nan"), {"food": 10.0})])

    def test_active_workers_may_not_exceed_required_workers(self):
        with self.assertRaisesRegex(ValueError, "food"):
            practice.plan_production_practice(
                None,
                {"food": 1},
                {"food": 2})

    def test_plan_is_json_stable_rng_free_and_non_mutating(self):
        state = {good: index * 10.0 for index, good in enumerate(
            practice.PRACTICE_GOODS, start=1)}
        required = {good: 4 for good in practice.PRACTICE_GOODS}
        active = {good: 3 for good in practice.PRACTICE_GOODS}
        before = copy.deepcopy((state, required, active))
        rng_before = random.getstate()
        first = practice.plan_production_practice(
            state, required, active)
        restored = json.loads(json.dumps(first))
        self.assertEqual(restored, first)
        self.assertEqual((state, required, active), before)
        self.assertEqual(random.getstate(), rng_before)


if __name__ == "__main__":
    unittest.main()
