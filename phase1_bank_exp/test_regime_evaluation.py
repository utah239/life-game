# -*- coding: utf-8 -*-
"""生存を目的化しない制度レジーム評価の回帰テスト。"""
import copy
import unittest

from dashboard import experiment_parameters
from dashboard import regime_candidate_search, regime_evaluation
from institutions import barter


INTERIM_DEFAULT_81851 = {
    "barter_food_gain": 8.0,
    "barter_medicine_gain": 1.5,
    "food_initial": 90.0,
    "food_upkeep": 9.4,
    "medicine_initial": 92.0,
    "medicine_upkeep": 9.3,
    "production_disruption": 0.3,
    "production_initial": 21.0,
    "production_reversion": 0.15,
    "shelter_initial": 1.0,
    "shelter_wear": 3.95,
    "shortage_energy_penalty": -48.0,
    "shortage_health_penalty": -17.75,
    "stage1_enter": 40.0,
    "stage1_exit": 13.0,
    "stage2_enter": 48.0,
    "stage2_exit": 17.0,
    "stage3_enter": 85.0,
    "stage3_exit": 63.0,
    "subsistence_food_gain": 18.5,
    "subsistence_medicine_gain": 17.0,
    "subsistence_production_gain": 4.0,
    "subsistence_shelter_repair": 15.5,
    "subsistence_tools_repair": 12.5,
    "survival_value_scale": 18.75,
    "tools_initial": 21.0,
    "tools_wear": 2.45,
}


def evaluation(*, survived=True, health=80.0, energy=20.0,
               scenario="natural", policy="cautious", final=1,
               worst=2, transitions=2, recoveries=1, recross=0,
               alt=2, shortage=0):
    return {
        "turns_requested": 100, "observed_turns": 80,
        "survived": survived, "scenario": scenario, "policy": policy,
        "barter_active": True,
        "normal_counts": {"labor": 8, "barter": alt, "subsistence": 0},
        "barter_shortage_penalty_applied_count": shortage,
        "traits": {"health": health}, "min_seen": {"energy": energy},
        "institutions": {
            "barter": {
                "final_stage": final, "worst_stage": worst,
                "transition_count": transitions, "recovery_count": recoveries,
                "rapid_same_boundary_recross_count": recross,
            },
            "bank": {"final_stage": 0},
        },
        "criteria": [
            {"name": "生存(T960)", "passed": survived, "category": "core"},
            {"name": "health(T960)", "passed": health > 20, "category": "core"},
            {"name": "制度遷移", "passed": True, "category": "barter"},
            {"name": "未適用制度", "passed": None, "category": "currency"},
        ],
    }


class RegimeAggregationTest(unittest.TestCase):
    def test_survival_health_and_energy_are_reference_only(self):
        left = evaluation(survived=True, health=99, energy=50)
        right = evaluation(survived=False, health=-500, energy=-50)
        a = regime_evaluation.aggregate_regime_evaluations([left])
        b = regime_evaluation.aggregate_regime_evaluations([right])
        ref_a = a.pop("reference_outcomes")
        ref_b = b.pop("reference_outcomes")
        guard_a = a.pop("numerical_guard")
        guard_b = b.pop("numerical_guard")
        self.assertEqual(a, b)
        self.assertNotEqual(ref_a, ref_b)
        self.assertTrue(guard_a["passed"])
        self.assertTrue(guard_b["passed"])

    def test_survival_and_health_criteria_are_excluded_and_na_is_neutral(self):
        aggregate = regime_evaluation.aggregate_regime_evaluations([evaluation()])
        self.assertEqual(aggregate["structural_criteria_tally"], {
            "pass": 1, "fail": 0, "na": 1})
        self.assertEqual(aggregate["structural_criteria_score"], 0.75)

    def test_scenario_paths_create_entropy_and_separation(self):
        rows = [
            evaluation(scenario="natural", final=0, worst=0),
            evaluation(scenario="compound_collapse", final=3, worst=3,
                       shortage=3),
        ]
        aggregate = regime_evaluation.aggregate_regime_evaluations(rows)
        self.assertGreater(aggregate["barter_final_stage_entropy"], 0.0)
        self.assertEqual(aggregate["scenario_regime_separation"], 1.0)
        self.assertEqual(aggregate["stage3_penalty_consistency"], 1.0)

    def test_profile_scores_do_not_change_when_reference_outcomes_change(self):
        rows = []
        for config_id, stage in ((1, 0), (2, 2), (3, 3)):
            aggregate = regime_evaluation.aggregate_regime_evaluations([
                evaluation(final=stage, worst=stage)])
            aggregate["config_id"] = config_id
            rows.append(aggregate)
        regime_evaluation.score_regime_rows(rows)
        before = [copy.deepcopy(row["profile_scores"]) for row in rows]
        for row in rows:
            row["reference_outcomes"] = {
                "survival_rate": 1.0 - row["config_id"] / 3,
                "mean_lifespan_ratio": 0.0,
            }
        regime_evaluation.score_regime_rows(rows)
        self.assertEqual(before, [row["profile_scores"] for row in rows])

    def test_catastrophic_energy_is_a_hard_guard_not_a_profile_score(self):
        safe = regime_evaluation.aggregate_regime_evaluations([
            evaluation(energy=0)])
        bad = regime_evaluation.aggregate_regime_evaluations([
            evaluation(energy=-2000)])
        safe["config_id"], bad["config_id"] = 1, 2
        regime_evaluation.score_regime_rows([safe, bad])
        self.assertTrue(safe["numerical_guard"]["passed"])
        self.assertFalse(bad["numerical_guard"]["passed"])
        self.assertEqual(safe["profile_scores"], bad["profile_scores"])


class RegimeSelectionTest(unittest.TestCase):
    def _rows(self):
        rows = []
        for config_id, stage in ((1, 0), (2, 1), (3, 2), (4, 3)):
            aggregate = regime_evaluation.aggregate_regime_evaluations([
                evaluation(final=stage, worst=stage,
                           recoveries=config_id % 2)])
            aggregate["config_id"] = config_id
            rows.append(aggregate)
        return rows

    def test_portfolio_pins_legacy_without_calling_it_best(self):
        rows = self._rows()
        selected = regime_evaluation.select_portfolio(rows, 3, {1})
        self.assertIn(1, selected)
        self.assertEqual(len(selected), 3)

    def test_excluded_legacy_is_scored_but_not_recommended(self):
        rows = self._rows()
        selected = regime_evaluation.select_portfolio(
            rows, 3, excluded_ids={1})
        legacy = next(row for row in rows if row["config_id"] == 1)
        self.assertNotIn(1, selected)
        self.assertFalse(legacy["selection_eligible"])
        self.assertFalse(legacy["selected_for_next"])
        self.assertIn("structural_balance", legacy["profile_scores"])

    def test_semantically_disabled_candidate_is_not_recommended(self):
        rows = self._rows()
        rows[0]["semantic_guard"] = {"passed": False}
        selected = regime_evaluation.select_portfolio(rows, 3)
        self.assertNotIn(1, selected)
        self.assertEqual(
            rows[0]["selection_exclusion_reason"], "semantic_guard_failed")

    def test_semantic_guard_requires_every_mechanism_to_remain_enabled(self):
        enabled = {
            "production_disruption": 0.1,
            "shortage_energy_penalty": -1.0,
            "shortage_health_penalty": 0.0,
            "barter_food_gain": 1.0,
            "barter_medicine_gain": 0.0,
            "subsistence_food_gain": 1.0,
            "subsistence_tools_repair": 1.0,
        }
        self.assertTrue(regime_candidate_search.semantic_parameter_guard(
            enabled)["passed"])
        disabled = dict(enabled, shortage_energy_penalty=0.0)
        self.assertFalse(regime_candidate_search.semantic_parameter_guard(
            disabled)["passed"])
        no_food = dict(enabled, subsistence_food_gain=0.0)
        self.assertFalse(regime_candidate_search.semantic_parameter_guard(
            no_food)["passed"])

    def test_gpu_structural_selection_ignores_survival_columns(self):
        rows = [{
            "config_id": config_id,
            "alternative_use_rate": config_id / 10,
            "shortage_rate": (5 - config_id) / 10,
            "mean_goods_safety": config_id * 10,
            "transition_per_run": config_id,
            "recovery_ratio": config_id / 5,
            "survival_proxy_rate": 1.0,
            "mean_lifespan_ratio": 1.0,
        } for config_id in range(1, 6)]
        first = regime_candidate_search.select_gpu_structural_portfolio(
            copy.deepcopy(rows), 3, {1})
        for row in rows:
            row["survival_proxy_rate"] = row["config_id"] / 100
            row["mean_lifespan_ratio"] = 0.0
        second = regime_candidate_search.select_gpu_structural_portfolio(
            copy.deepcopy(rows), 3, {1})
        self.assertEqual(first, second)


class InterimRegimeDefaultTest(unittest.TestCase):
    def test_dashboard_defaults_match_selected_candidate_81851(self):
        defaults = experiment_parameters.default_values()
        self.assertEqual(
            {key: defaults[key] for key in INTERIM_DEFAULT_81851},
            INTERIM_DEFAULT_81851)

    def test_shortfall_references_follow_selected_initial_values(self):
        self.assertEqual(barter.FOOD_SHORTFALL_REFERENCE, 90.0)
        self.assertEqual(barter.MEDICINE_SHORTFALL_REFERENCE, 92.0)
        self.assertEqual(barter.SHELTER_SHORTFALL_REFERENCE, 1.0)
        self.assertEqual(barter.TOOLS_SHORTFALL_REFERENCE, 21.0)


if __name__ == "__main__":
    unittest.main()
