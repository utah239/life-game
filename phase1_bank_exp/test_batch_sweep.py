# -*- coding: utf-8 -*-
"""大量パラメータ走査の決定性・隔離・再開・保存境界テスト。"""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zlib

from dashboard import batch_sweep
from dashboard import server as experiment_server
from dashboard.experiment_parameters import default_values, validate_request


class CandidateGenerationTest(unittest.TestCase):
    def test_seed_spec_supports_ranges_and_deduplicates(self):
        self.assertEqual(batch_sweep.parse_seed_spec("1-3,2,8"), (1, 2, 3, 8))
        with self.assertRaises(ValueError):
            batch_sweep.parse_seed_spec("3-1")

    def test_candidates_are_deterministic_unique_and_valid(self):
        first = batch_sweep.generate_candidates(80, 1234, 0.25)
        second = batch_sweep.generate_candidates(80, 1234, 0.25)
        self.assertEqual(first, second)
        self.assertEqual(len({batch_sweep.config_hash(row) for row in first}), 80)
        expected_default = {key: default_values()[key]
                            for key in batch_sweep.TUNABLE_KEYS}
        self.assertEqual(first[0], expected_default)
        for row in first:
            values = default_values()
            values.update(row)
            validate_request({"values": values})

    def test_config_hash_ignores_dict_insertion_order(self):
        row = batch_sweep.generate_candidates(1, 1)[0]
        self.assertEqual(
            batch_sweep.config_hash(row),
            batch_sweep.config_hash(dict(reversed(list(row.items())))))


def sample_evaluation(policy="cautious", survived=True, alt=2, shortage=0,
                      final_stage=1):
    return {
        "seed": 1, "policy": policy, "turns_requested": 100,
        "observed_turns": 100 if survived else 60, "survived": survived,
        "death_turn": None if survived else 60, "talent": "health",
        "blocked": 0, "overridden": 0, "fires": 1, "bank_crisis_count": 0,
        "barter_active": True, "barter_activated_turn": 20,
        "barter_shortage_penalty_applied_count": shortage,
        "normal_counts": {"labor": 8, "barter": alt, "subsistence": 0},
        "settlement_counts": {"money": 1}, "default_rate": 0.0,
        "resources": {"energy": 50, "peace": 60, "money": 10},
        "traits": {"health": 70}, "min_seen": {"energy": 30},
        "goods_min_seen": {"food": 20, "medicine": 15, "shelter": 50, "tools": 40},
        "finals": {"barter_stage": final_stage, "enforcement_stage": 1,
                   "local_credit_stage": 1, "currency_stage": 0, "bank_stage": 0},
        "institutions": {
            "barter": {"final_stage": final_stage, "transition_count": 2,
                       "recovery_count": 1, "rapid_same_boundary_recross_count": 0},
            "bank": {"final_stage": 0, "transition_count": 0,
                     "recovery_count": 0, "rapid_same_boundary_recross_count": 0},
        },
        "criteria": [{"name": "x", "passed": True, "category": "core"}],
        "criteria_tally": {"pass": 1, "fail": 0, "na": 0},
    }


class AggregationAndSelectionTest(unittest.TestCase):
    def test_aggregate_keeps_multiple_objectives(self):
        aggregate = batch_sweep.aggregate_evaluations([
            sample_evaluation("cautious", True, 2, 0, 1),
            sample_evaluation("family", False, 0, 6, 2),
        ])
        self.assertEqual(aggregate["survival_rate"], 0.5)
        self.assertGreater(aggregate["policy_diversity"], 0.0)
        self.assertGreater(aggregate["regime_diversity"], 0.0)
        self.assertEqual(set(aggregate["profile_scores"]),
                         set(batch_sweep.PROFILE_NAMES))

    def test_pareto_front_does_not_collapse_to_one_scalar(self):
        base = {
            "pass_rate": 0.5, "policy_diversity": 0.5,
            "alternative_observation_score": 0.5, "shortage_rate": 0.1,
            "rapid_recross_rate": 0.1,
        }
        rows = [
            dict(base, config_id=1, survival_rate=1.0),
            dict(base, config_id=2, survival_rate=0.8, pass_rate=0.9),
            dict(base, config_id=3, survival_rate=0.2, pass_rate=0.1),
        ]
        self.assertEqual(batch_sweep.pareto_front_ids(rows), {1, 2})

    def test_default_candidate_can_be_pinned_as_cross_stage_control(self):
        rows = []
        for config_id, score in ((1, 0.0), (2, 0.9), (3, 0.8)):
            row = {
                "config_id": config_id, "survival_rate": score, "pass_rate": score,
                "policy_diversity": score, "alternative_observation_score": score,
                "shortage_rate": 1.0 - score, "rapid_recross_rate": 1.0 - score,
                "profile_scores": {name: score for name in batch_sweep.PROFILE_NAMES},
            }
            rows.append(row)
        _, selected = batch_sweep.score_and_select(rows, 2, {1})
        self.assertEqual(len(selected), 2)
        self.assertIn(1, selected)


class SweepEndToEndTest(unittest.TestCase):
    def test_tiny_sweep_persists_compressed_summary_and_resumes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_path = root / "tiny.sqlite3"
            report_dir = root / "reports"
            command = [
                sys.executable, str(batch_sweep.__file__),
                "--db", str(db_path), "--report-dir", str(report_dir),
                "--candidates", "3", "--workers", "2", "--policies", "cautious",
                "--screen-turns", "12", "--screen-seeds", "1",
                "--screen-keep", "2", "--stop-after", "screen",
                "--progress-every", "1",
            ]
            first = subprocess.run(command, cwd=batch_sweep.PROJECT_DIR,
                                   capture_output=True, text=True, timeout=60)
            self.assertEqual(first.returncode, 0, first.stderr)
            second = subprocess.run(command, cwd=batch_sweep.PROJECT_DIR,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("pending=0", second.stdout)
            with sqlite3.connect(db_path) as connection:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM configs").fetchone()[0], 3)
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM evaluations").fetchone()[0], 3)
                packed = connection.execute(
                    "SELECT summary_zlib FROM evaluations LIMIT 1").fetchone()[0]
            summary = json.loads(zlib.decompress(packed))
            self.assertNotIn("agree_log", summary)
            self.assertNotIn("contracts", summary)
            self.assertIn("institutions", summary)
            db = batch_sweep.SweepDatabase(db_path)
            try:
                scored, selected = batch_sweep.build_validated_stage(
                    db, db.all_config_ids(), source_stages=("screen",),
                    recommendation_count=2)
                self.assertEqual(len(scored), 3)
                self.assertEqual(len(selected), 2)
            finally:
                db.close()
            self.assertTrue((report_dir / "barter_sweep_screen.html").exists())
            self.assertTrue((report_dir / "barter_sweep_screen.csv").exists())
            self.assertTrue((report_dir / "barter_sweep_screen.json").exists())

    def test_existing_database_rejects_changed_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sweep.sqlite3"
            candidates = batch_sweep.generate_candidates(2, 1)
            db = batch_sweep.SweepDatabase(path)
            try:
                db.initialize({"candidate_count": 2}, "fingerprint", candidates)
                with self.assertRaises(ValueError):
                    db.initialize({"candidate_count": 3}, "fingerprint", candidates)
            finally:
                db.close()


class SweepReportServerTest(unittest.TestCase):
    def test_reports_are_read_only_and_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            report_dir = Path(temp)
            report = report_dir / "barter_sweep_screen.html"
            report.write_text(
                "<h1>sweep ok</h1>", encoding="utf-8")
            self.assertEqual(experiment_server.resolve_report_path(
                report_dir, report.name), report.resolve())
            self.assertIsNone(experiment_server.resolve_report_path(
                report_dir, "%2e%2e%2fREADME.md"))
            self.assertIsNone(experiment_server.resolve_report_path(
                report_dir, "not-html.json"))


if __name__ == "__main__":
    unittest.main()
