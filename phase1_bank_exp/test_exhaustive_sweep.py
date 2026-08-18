# -*- coding: utf-8 -*-
"""途中選抜なしの全scenario総当たり走査テスト。"""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from dashboard import batch_sweep
from dashboard import batch_worker
from dashboard import exhaustive_sweep


class ScenarioTest(unittest.TestCase):
    def _fake_game(self):
        return SimpleNamespace(
            engine=SimpleNamespace(
                alternative_economy_triggered=lambda *_args: False),
            alternative_economy_triggered=lambda *_args: False,
            currency_stage_next=lambda *_args: 0,
            local_credit_stage_next=lambda *_args: 0,
            enforcement_stage_next=lambda *_args: 0,
            bank_stage_next=lambda *_args: 0,
            CURRENCY_STAGE_ABANDONED=3,
            LOCAL_CREDIT_STAGE_ISOLATED=3,
            ENFORCEMENT_STAGE_NONE=4,
            BANK_STAGE_COLLAPSED=3,
        )

    def test_all_scenarios_are_accepted_and_unknown_is_rejected(self):
        for scenario in batch_worker.SCENARIOS:
            batch_worker.apply_world_scenario(self._fake_game(), scenario)
        with self.assertRaises(ValueError):
            batch_worker.apply_world_scenario(self._fake_game(), "unknown")

    def test_forced_alternative_changes_game_and_engine_gate(self):
        game = self._fake_game()
        batch_worker.apply_world_scenario(game, "forced_alternative")
        self.assertTrue(game.alternative_economy_triggered(0, 0, 0, 0))
        self.assertTrue(game.engine.alternative_economy_triggered(0, 0, 0, 0))

    def test_compound_collapse_forces_all_four_institutions(self):
        game = self._fake_game()
        batch_worker.apply_world_scenario(game, "compound_collapse")
        self.assertEqual(game.bank_stage_next(), 3)
        self.assertEqual(game.currency_stage_next(), 3)
        self.assertEqual(game.local_credit_stage_next(), 3)
        self.assertEqual(game.enforcement_stage_next(), 4)


class ExhaustiveMatrixPlanningTest(unittest.TestCase):
    def test_matrix_size_matches_full_cross_product(self):
        size = exhaustive_sweep.matrix_size(100000, 6, 10, 3, 1920, 2500)
        self.assertEqual(size["evaluations"], 18_000_000)
        self.assertEqual(size["simulated_turns_upper_bound"], 34_560_000_000)
        self.assertEqual(size["estimated_database_bytes"], 45_000_000_000)

    def test_scenario_parser_deduplicates_and_rejects_unknown(self):
        self.assertEqual(
            exhaustive_sweep.parse_scenarios("natural,natural,enforcement_none"),
            ("natural", "enforcement_none"))
        with self.assertRaises(ValueError):
            exhaustive_sweep.parse_scenarios("natural,unknown")

    def test_worker_payload_contains_stage_scenario(self):
        stage = batch_sweep.StageSpec(
            "matrix_forced", 10, (1,), None, "forced_alternative")
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"evaluations":[]}', stderr="")
        with mock.patch("dashboard.batch_sweep.subprocess.run",
                        return_value=completed) as run:
            batch_sweep.run_worker({}, stage, ("cautious",), 30, "random", 60)
        payload = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(payload["scenario"], "forced_alternative")
        self.assertEqual(payload["shortfall_reference_mode"], "coupled")

    def test_worker_payload_can_fix_shortfall_reference(self):
        stage = batch_sweep.StageSpec("fixed", 10, (1,), None, "natural")
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"evaluations":[]}', stderr="")
        with mock.patch("dashboard.batch_sweep.subprocess.run",
                        return_value=completed) as run:
            batch_sweep.run_worker(
                {}, stage, ("cautious",), 30, "random", 60, "fixed")
        payload = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(payload["shortfall_reference_mode"], "fixed")

    def test_large_scoring_is_bounded_and_keeps_baseline(self):
        rows = []
        for config_id in range(1, 101):
            score = config_id / 100
            rows.append({
                "config_id": config_id,
                "survival_rate": score,
                "mean_lifespan_ratio": score,
                "pass_rate": score,
                "policy_diversity": score,
                "regime_diversity": score,
                "alternative_observation_score": score,
                "shortage_rate": 1 - score,
                "rapid_recross_rate": 1 - score,
                "recovery_ratio": score,
                "profile_scores": {
                    name: score for name in batch_sweep.PROFILE_NAMES},
            })
        _, selected = exhaustive_sweep.score_large_matrix(rows, 10, {1})
        self.assertEqual(len(selected), 10)
        self.assertIn(1, selected)


class ExhaustiveMatrixEndToEndTest(unittest.TestCase):
    def test_tiny_matrix_runs_every_candidate_scenario_policy_seed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_path = root / "matrix.sqlite3"
            report_dir = root / "reports"
            command = [
                sys.executable, str(exhaustive_sweep.__file__),
                "--db", str(db_path), "--report-dir", str(report_dir),
                "--candidates", "2", "--workers", "2", "--turns", "12",
                "--seeds", "1", "--policies", "cautious",
                "--scenarios", "natural,forced_alternative",
                "--recommendations", "2", "--progress-every", "1",
            ]
            result = subprocess.run(
                command, cwd=batch_sweep.PROJECT_DIR, capture_output=True,
                text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            with sqlite3.connect(db_path) as connection:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM configs").fetchone()[0], 2)
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM evaluations").fetchone()[0], 4)
                stages = {row[0] for row in connection.execute(
                    "SELECT DISTINCT stage_name FROM stage_results")}
            self.assertEqual(stages, {
                "matrix_natural", "matrix_forced_alternative", "matrix_all"})
            self.assertTrue(
                (report_dir / "barter_sweep_matrix_all.html").is_file())


if __name__ == "__main__":
    unittest.main()
