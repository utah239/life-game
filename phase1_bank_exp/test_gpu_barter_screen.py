# -*- coding: utf-8 -*-
"""GPU近似スクリーニングの標準ライブラリ側契約テスト。"""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from dashboard.experiment_parameters import default_values
from dashboard.gpu_barter_model import (
    MODEL_VERSION,
    PARAMETER_KEYS,
    ScreenRun,
    aggregate_screen_runs,
    hash32,
    parameter_row,
    screen_matrix_size,
    simulate_screen_run,
)
from dashboard import gpu_barter_screen
from dashboard import gpu_validate
from dashboard import robustness_validate
from dashboard import batch_sweep
from dashboard import production_disruption_validate


class GpuBarterModelTest(unittest.TestCase):
    def test_full_matrix_raw_output_size(self):
        size = screen_matrix_size(100_000, 6, 10, 3)
        self.assertEqual(size["runs"], 18_000_000)
        self.assertEqual(size["output_bytes"], 1_152_000_000)
        self.assertEqual(size["parameter_bytes"], 10_800_000)

    def test_parameter_row_has_stable_width_and_order(self):
        values = default_values()
        row = parameter_row(values)
        self.assertEqual(len(row), 27)
        self.assertEqual(row[0], values[PARAMETER_KEYS[0]])
        with self.assertRaisesRegex(ValueError, "missing GPU screen parameters"):
            parameter_row({})

    def test_hash_is_deterministic_and_lane_sensitive(self):
        self.assertEqual(hash32(1, 2, 3, 0, 0), hash32(1, 2, 3, 0, 0))
        self.assertNotEqual(hash32(1, 2, 3, 0, 0), hash32(1, 2, 4, 0, 0))

    def test_reference_run_is_deterministic_and_scenario_sensitive(self):
        values = default_values()
        natural = simulate_screen_run(values, 240, 1, 0, 0)
        forced = simulate_screen_run(values, 240, 1, 0, 1)
        self.assertEqual(natural, simulate_screen_run(values, 240, 1, 0, 0))
        self.assertGreater(natural.activated_turn, 1.0)
        self.assertEqual(forced.activated_turn, 1.0)
        self.assertGreater(forced.alternative_turns, natural.alternative_turns)

    def test_aggregate_uses_requested_turns_for_death_ratio(self):
        survivor = ScreenRun(1, 0, 0, 0, 0, 1, 0, 10, 20, 40, 2, 1, 1, 80, 60, 60)
        death = ScreenRun(0, 30, 3, 3, 10, 0, 1, 90, 0, 0, 4, 0, 1, 0, 30, 30)
        result = aggregate_screen_runs([survivor, death], requested_turns=60)
        self.assertEqual(result["run_count"], 2)
        self.assertEqual(result["survival_proxy_rate"], 0.5)
        self.assertAlmostEqual(result["mean_lifespan_ratio"], 0.75)


class GpuBarterScreenDatabaseTest(unittest.TestCase):
    def _database(self, path: Path):
        connection = sqlite3.connect(path)
        connection.execute("""
            CREATE TABLE configs (
                id INTEGER PRIMARY KEY, parameters_json TEXT NOT NULL)
        """)
        payload = json.dumps({key: default_values()[key] for key in PARAMETER_KEYS})
        connection.executemany(
            "INSERT INTO configs(id,parameters_json) VALUES(?,?)",
            [(1, payload), (2, payload)])
        connection.commit()
        return connection

    def test_result_tables_are_separate_and_resume_pending_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            connection = self._database(Path(temp) / "screen.sqlite3")
            try:
                gpu_barter_screen.ensure_tables(connection)
                key = "matrix"
                self.assertEqual(
                    gpu_barter_screen.pending_config_ids(connection, key), [1, 2])
                aggregate = {
                    "run_count": 1, "screen_score": 1.0,
                    "survival_proxy_rate": 1.0, "mean_lifespan_ratio": 1.0,
                    "shortage_rate": 0.0, "alternative_use_rate": 1.0,
                    "mean_final_health": 50.0, "mean_goods_safety": 50.0,
                    "transition_count": 1, "recovery_count": 0,
                    "recovery_ratio": 0.0,
                }
                gpu_barter_screen.save_results(connection, key, [1], [aggregate])
                self.assertEqual(
                    gpu_barter_screen.pending_config_ids(connection, key), [2])
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM gpu_screen_results").fetchone()[0], 1)
            finally:
                connection.close()

    def test_matrix_key_includes_model_and_settings(self):
        left = gpu_barter_screen.matrix_key(
            1920, ("natural",), (1,), ("cautious",))
        right = gpu_barter_screen.matrix_key(
            1921, ("natural",), (1,), ("cautious",))
        self.assertNotEqual(left, right)
        self.assertEqual(len(left), 20)
        self.assertEqual(MODEL_VERSION, "barter_gpu_screen_v2")

    def test_dry_run_does_not_require_numpy_or_cuda(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "screen.sqlite3"
            self._database(path).close()
            result = subprocess.run([
                sys.executable, str(Path(gpu_barter_screen.__file__)),
                "--db", str(path), "--limit", "2", "--turns", "10",
                "--seeds", "1", "--scenarios", "natural",
                "--policies", "cautious", "--dry-run",
            ], cwd=Path(gpu_barter_screen.__file__).resolve().parent.parent,
               capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("configs=2", result.stdout)
            self.assertIn("approximate GPU pre-screen", result.stdout)


class GpuCpuValidationPlanningTest(unittest.TestCase):
    def test_selects_gpu_top_stratified_controls_and_baseline(self):
        connection = sqlite3.connect(":memory:")
        connection.execute("""
            CREATE TABLE gpu_screen_results(
                matrix_key TEXT, config_id INTEGER, screen_score REAL)
        """)
        connection.executemany(
            "INSERT INTO gpu_screen_results VALUES('m',?,?)",
            [(index, 101 - index) for index in range(1, 101)])
        selected, ranks = gpu_validate.select_screen_candidates(
            connection, "m", top_count=5, exploration_count=4,
            baseline_id=100)
        self.assertEqual(selected[:5], [1, 2, 3, 4, 5])
        self.assertEqual(selected[5:9], [13, 38, 63, 88])
        self.assertEqual(selected[-1], 100)
        self.assertEqual(ranks[1], 1)
        self.assertEqual(ranks[100], 100)

    def test_validation_key_changes_with_plan(self):
        left = gpu_validate.validation_key("gpu", {"turns": 240})
        right = gpu_validate.validation_key("gpu", {"turns": 960})
        self.assertNotEqual(left, right)
        self.assertEqual(left, gpu_validate.validation_key("gpu", {"turns": 240}))

    def test_load_manifest_validates_shape(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "top.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing"):
                gpu_validate.load_gpu_manifest(path)

    def test_scenario_matrix_uses_all_scenarios_then_combines(self):
        database = mock.Mock()
        with (mock.patch("dashboard.gpu_validate.batch_sweep.run_stage") as run,
              mock.patch("dashboard.gpu_validate.exhaustive_sweep.combine_matrix_stages",
                         return_value=([{"config_id": 1}], [1])) as combine,
              mock.patch("dashboard.gpu_validate.batch_sweep.write_reports",
                         return_value=(Path("a"), Path("b"), Path("c")))):
            scored, selected, name = gpu_validate.run_scenario_matrix(
                database, "prefix", 10, (1,), [1], ("cautious",), 30,
                "random", 2, 60, 1, Path("reports"), 1)
        self.assertEqual(run.call_count, len(gpu_validate.SCENARIOS))
        self.assertEqual(name, "prefix_all")
        self.assertEqual(scored, [{"config_id": 1}])
        self.assertEqual(selected, [1])
        source_names = combine.call_args.args[2]
        self.assertEqual(len(source_names), len(gpu_validate.SCENARIOS))


class RobustnessValidationTest(unittest.TestCase):
    def _evaluation(self, *, survived=True, observed=100, energy=10.0,
                    policy="cautious", scenario="natural"):
        return {
            "survived": survived,
            "observed_turns": observed,
            "turns_requested": 100,
            "normal_counts": {"rest": 1},
            "barter_active": False,
            "barter_shortage_penalty_applied_count": 0,
            "institutions": {},
            "criteria_tally": {"pass": 3, "fail": 1, "na": 0},
            "traits": {"health": 50.0},
            "min_seen": {"energy": energy},
            "policy": policy,
            "scenario": scenario,
        }

    def test_local_neighbors_are_deterministic_unique_and_valid(self):
        defaults = default_values()
        center = {key: defaults[key] for key in robustness_validate.TUNABLE_KEYS}
        left = robustness_validate.generate_local_neighbors(
            center, 8, 123, 0.05)
        right = robustness_validate.generate_local_neighbors(
            center, 8, 123, 0.05)
        self.assertEqual(left, right)
        self.assertEqual(len({batch_sweep.config_hash(row) for row in left}), 8)
        for row in left:
            robustness_validate._validated_tunable(row)

    def test_wilson_interval_contains_observed_rate(self):
        low, high = robustness_validate.wilson_interval(30, 100)
        self.assertLess(low, 0.30)
        self.assertGreater(high, 0.30)
        self.assertEqual(robustness_validate.wilson_interval(0, 0), (0.0, 0.0))

    def test_energy_guard_rejects_catastrophic_negative_energy(self):
        healthy = robustness_validate.summarize_exact(
            [self._evaluation(energy=0.0) for _ in range(10)], -100, -1000)
        pathological = robustness_validate.summarize_exact(
            [self._evaluation(energy=-2000.0) for _ in range(10)], -100, -1000)
        self.assertTrue(healthy["energy_guard"]["guard_pass"])
        self.assertFalse(pathological["energy_guard"]["guard_pass"])

    def test_confirmation_requires_all_preregistered_checks(self):
        primary_rows = [self._evaluation() for _ in range(80)]
        baseline_rows = [self._evaluation(survived=index < 20, observed=60)
                         for index in range(80)]
        primary = robustness_validate.summarize_exact(
            primary_rows, -100, -1000)
        baseline = robustness_validate.summarize_exact(
            baseline_rows, -100, -1000)
        primary_scenarios = {name: primary for name in robustness_validate.SCENARIOS}
        baseline_scenarios = {name: baseline for name in robustness_validate.SCENARIOS}
        result = robustness_validate.build_confirmation(
            primary, baseline, primary_scenarios, baseline_scenarios)
        self.assertTrue(result["passed"])
        self.assertTrue(all(result["checks"].values()))


class ProductionDisruptionValidationTest(unittest.TestCase):
    def test_parse_values_deduplicates_and_requires_step_alignment(self):
        self.assertEqual(
            production_disruption_validate.parse_values("0,0.1,0.1,0.6"),
            (0.0, 0.1, 0.6))
        with self.assertRaisesRegex(ValueError, "step"):
            production_disruption_validate.parse_values("0.15")

    def test_build_candidates_changes_only_production_disruption(self):
        defaults = default_values()
        source = {key: defaults[key] for key in batch_sweep.TUNABLE_KEYS}
        candidates = production_disruption_validate.build_candidates(
            source, (0.0, 0.1, 0.2))
        self.assertEqual(
            [row["production_disruption"] for row in candidates],
            [0.0, 0.1, 0.2])
        for row in candidates:
            for key in batch_sweep.TUNABLE_KEYS:
                if key != "production_disruption":
                    self.assertEqual(row[key], source[key])

    def test_acceptance_uses_reference_relative_limits(self):
        reference = {
            "survival_rate": 0.30, "mean_lifespan_ratio": 0.75,
            "pass_rate": 0.76, "shortage_rate": 0.001,
            "energy_guard": {"guard_pass": True},
        }
        accepted = production_disruption_validate._acceptance(
            dict(reference), reference)
        rejected_row = dict(reference, survival_rate=0.19)
        rejected = production_disruption_validate._acceptance(
            rejected_row, reference)
        self.assertTrue(accepted["passed"])
        self.assertFalse(rejected["passed"])
        self.assertFalse(rejected["checks"]["survival_within_10pp"])


if __name__ == "__main__":
    unittest.main()
