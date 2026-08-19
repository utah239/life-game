# -*- coding: utf-8 -*-
"""永続デジタル水槽worker・保存時計・UI境界。"""
import copy
import json
from pathlib import Path
import pickle
import random
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import game
from dashboard import aquarium_runtime
from dashboard import aquarium_stream
from dashboard import aquarium_worker
from dashboard import build_dashboard
from dashboard import particle_packet
from dashboard import server as dashboard_server
from dashboard import spatial_history
from institutions import spatial
from institutions.residents import (
    NAMED_RESIDENT_LIMIT,
    RESIDENT_REGISTRY_VERSION,
    anonymous_population_count,
    living_population_count,
    living_residents,
    registry_matches_settlements,
)


WORKER = Path(__file__).resolve().parent / "dashboard" / "aquarium_worker.py"


def run_worker(payload: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, str(WORKER)],
        cwd=Path(__file__).resolve().parent,
        input=json.dumps(payload), text=True, capture_output=True,
        encoding="utf-8", check=False)
    if proc.returncode:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout)


def run_worker_pickle(payload: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, str(WORKER), "--pickle"],
        cwd=Path(__file__).resolve().parent,
        input=pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL),
        capture_output=True, check=False)
    if proc.returncode:
        raise AssertionError(proc.stderr.decode("utf-8", errors="replace"))
    return pickle.loads(proc.stdout)


class AquariumWorkerTest(unittest.TestCase):
    def test_trusted_pickle_transport_matches_portable_json_transport(self):
        payload = {
            "action": "start", "initial_months": 2,
            "values": {"turns": 2, "seed": 3, "talent": "health"},
        }
        portable = run_worker(payload)
        binary = run_worker_pickle(payload)
        # JSON正規化後の数値world・dashboard・timing field構造が一致する。
        # 実時間値だけは呼び出しごとに異なるため比較対象から外す。
        portable.pop("timings")
        binary.pop("timings")
        self.assertEqual(
            json.loads(json.dumps(binary, ensure_ascii=False)), portable)

    def test_stateful_pickle_loop_matches_two_one_shot_advances(self):
        start_payload = {
            "action": "start", "initial_months": 2,
            "values": {"turns": 2, "seed": 13, "talent": "health"},
        }
        expected_start = run_worker_pickle(start_payload)
        persisted_start_world = json.loads(json.dumps(
            expected_start["world"], ensure_ascii=False))
        expected_first = run_worker_pickle({
            "action": "advance", "world": persisted_start_world,
            "months": 1,
        })
        persisted_first_world = json.loads(json.dumps(
            expected_first["world"], ensure_ascii=False))
        expected_second = run_worker_pickle({
            "action": "advance", "world": persisted_first_world,
            "months": 1,
        })

        proc = subprocess.Popen(
            [sys.executable, str(WORKER), "--pickle-loop"],
            cwd=Path(__file__).resolve().parent,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        try:
            pickle.dump(start_payload, proc.stdin)
            proc.stdin.flush()
            actual_start = pickle.load(proc.stdout)
            pickle.dump({"action": "advance", "months": 1}, proc.stdin)
            proc.stdin.flush()
            actual_first = pickle.load(proc.stdout)
            pickle.dump({"action": "advance", "months": 1}, proc.stdin)
            proc.stdin.flush()
            actual_second = pickle.load(proc.stdout)
        finally:
            proc.terminate()
            proc.wait(timeout=3)
            proc.stdin.close()
            proc.stdout.close()
            proc.stderr.close()

        for expected, actual in (
                (expected_start, actual_start),
                (expected_first, actual_first),
                (expected_second, actual_second)):
            expected = copy.deepcopy(expected)
            actual = copy.deepcopy(actual)
            expected.pop("timings")
            actual.pop("timings")
            self.assertEqual(actual, expected)

    def test_million_person_world_uses_named_sample_and_anonymous_cohort(self):
        started = run_worker({
            "action": "start", "initial_months": 1,
            "values": {
                "turns": 1, "seed": 1, "talent": "health",
                "initial_population": 1_000_000,
            },
        })
        world = started["world"]
        registry = world["checkpoint"]["resident_registry"]
        total = sum(
            row["population"]
            for row in world["checkpoint"]["settlements"].values())

        self.assertEqual(
            world["config"]["values"]["initial_population"], 1_000_000)
        self.assertTrue(registry["cohort_mode"])
        self.assertEqual(len(living_residents(registry)), NAMED_RESIDENT_LIMIT)
        self.assertEqual(living_population_count(registry), total)
        self.assertEqual(
            anonymous_population_count(registry),
            total - NAMED_RESIDENT_LIMIT)
        self.assertEqual(world["summary"]["total_population"], total)
        self.assertEqual(
            started["dashboard_data"]["meta"]["particle_population_total"],
            total)
        organizations = [
            row for row in world["checkpoint"]["organization_state"][
                "organizations"].values()
            if row.get("active", True)]
        worker_ids = [
            resident_id for row in organizations
            for resident_id in row.get("named_worker_ids", ())]
        workforce = sum(row.get("workforce_count", 0)
                        for row in organizations)
        unrepresented = sum(
            row.get("unrepresented_workforce_count", 0)
            for row in organizations if row.get("workforce_count", 0))
        eligible = sum(
            row.get("eligible_workforce_count", 0)
            for row in organizations if row.get("workforce_count", 0)
            or row.get("eligible_workforce_count", 0))
        self.assertGreater(workforce, len(worker_ids))
        self.assertEqual(len(worker_ids), len(set(worker_ids)))
        self.assertEqual(workforce, len(worker_ids) + unrepresented)
        self.assertEqual(
            world["summary"]["organization_workforce_total"], workforce)
        self.assertEqual(
            world["summary"]["organization_named_worker_total"],
            len(worker_ids))
        self.assertEqual(
            world["summary"]["organization_eligible_workforce_total"],
            eligible)
        self.assertEqual(
            started["dashboard_data"]["meta"][
                "organization_workforce_total"], workforce)
        self.assertEqual(
            started["dashboard_data"]["meta"][
                "organization_eligible_workforce_total"], eligible)
        self.assertIn(
            "organization_mean_member_continuity", world["summary"])

    def test_start_then_json_advance_matches_one_shot_checkpoint(self):
        first = run_worker({
            "action": "start",
            "values": {"turns": 12, "seed": 7, "talent": "health"},
        })
        # subprocess間の受け渡し自体がJSON往復なので、tuple/int key復元を含む。
        resumed = run_worker({
            "action": "advance", "world": first["world"], "months": 12,
        })
        one_shot = game.simulate_policy(
            "cautious", 24, 7, 30, "health", continue_world=True)
        expected = json.loads(json.dumps(one_shot["resume_state"]))
        self.assertEqual(resumed["world"]["checkpoint"], expected)
        self.assertEqual(resumed["world"]["summary"]["completed_turn"], 24)
        self.assertEqual(len(resumed["world"]["trace"]["turns"]), 24)
        self.assertEqual(
            [row["turn"] for row in resumed["world"]["trace"]["spatial_keyframes"]],
            [1, 12, 24])
        self.assertTrue(resumed["dashboard_data"]["meta"]["aquarium_live"])
        dashboard_spatial = resumed["dashboard_data"]["spatial_state"]
        checkpoint_spatial = resumed["world"]["checkpoint"]["spatial_state"]
        self.assertEqual(dashboard_spatial["residents"], {})
        self.assertEqual(
            dashboard_spatial["packed_resident_count"],
            len(checkpoint_spatial["residents"]))
        self.assertEqual(
            resumed["dashboard_data"]["particle_frame"]["count"],
            len(checkpoint_spatial["residents"]))
        self.assertEqual(
            {key: value for key, value in dashboard_spatial.items()
             if key not in {"residents", "packed_resident_count"}},
            {key: value for key, value in checkpoint_spatial.items()
             if key != "residents"})
        self.assertEqual(
            resumed["dashboard_data"]["activity_community_ledger"],
            resumed["world"]["checkpoint"]["activity_community_ledger"])
        self.assertEqual(
            resumed["world"]["summary"]["activity_cluster_count"],
            len(resumed["world"]["checkpoint"]["spatial_state"]["clusters"]))
        clusters = resumed["world"]["checkpoint"]["spatial_state"]["clusters"]
        self.assertEqual(
            resumed["world"]["summary"][
                "activity_provisional_cluster_count"],
            sum(1 for row in clusters.values()
                if row.get("provisional", False)))
        self.assertEqual(
            resumed["world"]["summary"][
                "activity_accounting_cluster_count"],
            sum(1 for row in clusters.values()
                if not row.get("provisional", False)))
        self.assertEqual(
            resumed["world"]["summary"][
                "activity_community_accounting_version"],
            resumed["world"]["checkpoint"][
                "activity_community_accounting_version"])
        for name in (
                "simulation_seconds", "compact_observation_seconds",
                "build_dashboard_seconds", "summary_seconds",
                "prepare_world_seconds", "total_execute_seconds"):
            self.assertIn(name, resumed["timings"])
            self.assertGreaterEqual(resumed["timings"][name], 0.0)

    def test_owned_world_fast_path_does_not_mutate_execute_input(self):
        started = run_worker({
            "action": "start", "initial_months": 2,
            "values": {"turns": 2, "seed": 3, "talent": "health"},
        })
        payload = {
            "action": "advance", "world": started["world"], "months": 1}
        before = copy.deepcopy(payload)
        aquarium_worker.execute(payload)
        self.assertEqual(payload, before)

    def test_version_two_nas_world_upgrades_without_reset_and_keeps_old_keyframes(self):
        started = run_worker({
            "action": "start", "initial_months": 24,
            "values": {"turns": 24, "seed": 1, "talent": "health"},
        })
        current_world = copy.deepcopy(started["world"])
        legacy_world = copy.deepcopy(started["world"])
        state = legacy_world["checkpoint"]["spatial_state"]
        state["version"] = spatial.CLUSTERED_SPATIAL_STATE_VERSION
        state.pop("pioneering_sequence")
        state.pop("last_pioneering_turn")
        for cluster in state["clusters"].values():
            for key in ("provisional", "accounting_parent_id",
                        "pioneering_site_count"):
                cluster.pop(key, None)
        for frame in legacy_world["trace"]["spatial_keyframes"]:
            frame["version"] = 3
            frame["clusters"] = [row[:8] for row in frame["clusters"]]

        expected = run_worker({
            "action": "advance", "world": current_world, "months": 12})
        upgraded = run_worker({
            "action": "advance", "world": legacy_world, "months": 12})

        self.assertEqual(
            upgraded["world"]["checkpoint"], expected["world"]["checkpoint"])
        self.assertEqual(
            upgraded["world"]["checkpoint"]["spatial_state"]["version"],
            spatial.SPATIAL_STATE_VERSION)
        self.assertEqual(
            upgraded["world"]["checkpoint"]["spatial_state"][
                "pioneering_sequence"], 1)
        cluster_row_lengths = {
            len(row)
            for frame in upgraded["dashboard_data"]["spatial_history"]["frames"]
            for row in frame.get("clusters", {}).get("upsert", [])}
        self.assertEqual(cluster_row_lengths, {8, 10})

    def test_rejects_unknown_action_and_extinct_world_advance(self):
        proc = subprocess.run(
            [sys.executable, str(WORKER)], input=json.dumps({"action": "bad"}),
            text=True, capture_output=True, encoding="utf-8", check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("action must be start or advance", proc.stderr)

        started = run_worker({"action": "start", "values": {"turns": 1}})
        started["world"]["checkpoint"]["world_extinct_turn"] = 1
        started["world"]["summary"]["world_extinct"] = True
        proc = subprocess.run(
            [sys.executable, str(WORKER)], input=json.dumps({
                "action": "advance", "world": started["world"], "months": 1}),
            text=True, capture_output=True, encoding="utf-8", check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("extinct world", proc.stderr)

    def test_observation_window_is_bounded_without_reusing_ids(self):
        first = run_worker({
            "action": "start", "history_months": 120,
            "values": {"turns": 130, "seed": 9, "talent": "health"},
        })
        world = first["world"]
        self.assertEqual(world["summary"]["history_start_turn"], 11)
        self.assertEqual(len(world["trace"]["turns"]), 120)
        keyframe_turns = [
            row["turn"] for row in world["trace"]["spatial_keyframes"]]
        self.assertEqual(keyframe_turns[0], 1)
        self.assertTrue(all(turn >= 11 for turn in keyframe_turns[1:]))
        self.assertEqual(world["trace"]["turns"][0]["turn"], 11)
        self.assertLessEqual(len(world["checkpoint"]["used_places"]), 5)
        self.assertGreaterEqual(
            world["checkpoint"]["contract_sequence"],
            len(world["checkpoint"]["contracts"]))

        second = run_worker({
            "action": "advance", "world": world, "months": 10,
        })["world"]
        self.assertEqual(second["summary"]["history_start_turn"], 21)
        self.assertEqual(len(second["trace"]["turns"]), 120)
        contract_ids = [
            int(row["id"][1:]) for row in second["checkpoint"]["contracts"]]
        self.assertTrue(all(
            value <= second["checkpoint"]["contract_sequence"]
            for value in contract_ids))

    def test_compacted_checkpoint_preserves_future_dynamics_and_rng(self):
        compacted = run_worker({
            "action": "start", "history_months": 120,
            "values": {"turns": 300, "seed": 9, "talent": "health"},
        })["world"]["checkpoint"]
        uncompact = game.simulate_policy(
            "cautious", 300, 9, 30, "health", continue_world=True)["resume_state"]

        # The comparison must exercise real contract compaction. NPCs are retained:
        # even retired NPCs remain in the current price-modifier sampling pool.
        self.assertEqual(compacted["npcs"], uncompact["npcs"])
        self.assertTrue(any(
            n.get("retire_turn") and n["retire_turn"] < 181
            for n in compacted["npcs"].values()))
        self.assertGreater(
            compacted["contract_sequence"], len(compacted["contracts"]))
        for organization in compacted["organization_state"][
                "organizations"].values():
            for history_name in ("form_history", "operating_history"):
                self.assertLessEqual(sum(
                    int(row["turn"]) < 181
                    for row in organization.get(history_name, ())), 1)

        def trace():
            return {"turns": [], "settlements": [], "npc_introductions": [],
                    "character_events": [], "population_events": [],
                    "npc_events": [], "trade_events": [],
                    "resident_events": []}

        full_trace, compact_trace = trace(), trace()
        full = game.simulate_policy(
            "cautious", 330, 9, 30, "health", continue_world=True,
            resume_state=uncompact, trace=full_trace)
        compact = game.simulate_policy(
            "cautious", 330, 9, 30, "health", continue_world=True,
            resume_state=compacted, trace=compact_trace)
        self.assertEqual(compact_trace, full_trace)
        for key in (
                "resources", "traits", "counts", "bank_trust", "bank_stage",
                "currency_confidence", "currency_stage", "community_trust",
                "local_credit_stage", "enforcement_capacity", "enforcement_stage",
                "food", "medicine", "shelter", "tools", "production_capacity",
                "barter_stage", "settlements", "generation", "character_death_count"):
            with self.subTest(key=key):
                self.assertEqual(compact[key], full[key])
        self.assertEqual(
            compact["resume_state"]["random_state"],
            full["resume_state"]["random_state"])
        self.assertEqual(
            [row for row in compact["contracts"] if row["status"] == "open"],
            [row for row in full["contracts"] if row["status"] == "open"])
        self.assertEqual(
            living_residents(compact["resident_registry"]),
            living_residents(full["resident_registry"]))
        self.assertTrue(registry_matches_settlements(
            compact["resident_registry"], compact["settlements"]))
        self.assertEqual(
            compact["spatial_state"]["residents"],
            full["spatial_state"]["residents"])
        self.assertEqual(
            {key: row for key, row in compact["spatial_state"]["sites"].items()
             if row.get("active", True)},
            {key: row for key, row in full["spatial_state"]["sites"].items()
             if row.get("active", True)})


class AquariumManagerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.now = [1000.0]
        self.monotonic = [0.0]
        self.manager = aquarium_runtime.AquariumManager(
            root / "world.json", root / "aquarium.html",
            time_fn=lambda: self.now[0],
            monotonic_fn=lambda: self.monotonic[0],
            unmeasured_safe_tick_seconds=1)

    def tearDown(self):
        self.manager.stop_background()
        self.temp.cleanup()

    def test_persists_atomic_world_and_latest_html(self):
        status = self.manager.start_world(
            {"turns": 6, "seed": 3, "talent": "health"},
            tick_seconds=2, months_per_tick=3)
        self.assertEqual(status["summary"]["completed_turn"], 6)
        self.assertEqual(status["summary"]["settlement_count"], 3)
        self.assertEqual(
            len(status["summary"]["settlements"]), 3)
        self.assertGreater(status["summary"]["total_population"], 0)
        # 交易の発生条件は不足差であり、永続化smoke testの6か月以内に
        # 必ず起こるものではない。件数と量の整合だけをここで固定し、
        # 実際の交易成立はintersettlement_tradeの圧力fixtureで検証する。
        self.assertEqual(
            status["summary"]["trade_event_count"] == 0,
            status["summary"]["trade_volume_total"] == 0.0)
        self.assertAlmostEqual(
            status["summary"]["world_provisioning_scale"], 3.0, 9)
        self.assertEqual(
            set(status["summary"]["world_demand_scales_by_good"]),
            {"food", "medicine", "shelter", "tools"})
        self.assertEqual(
            status["summary"]["living_resident_count"],
            status["summary"]["total_population"])
        self.assertGreater(status["summary"]["household_count"], 0)
        self.assertGreater(status["summary"]["household_activity_total"], 0)
        self.assertTrue(status["summary"]["focus_resident_name"])
        self.assertTrue(all(
            "community_trust" in row
            and "local_credit_stage" in row
            and row["provisioning_scale"] > 0.0
            and set(row["demand_scales_by_good"])
            == {"food", "medicine", "shelter", "tools"}
            and set(row["goods_coverage_by_good"])
            == {"food", "medicine", "shelter", "tools"}
            for row in status["summary"]["settlements"]))
        self.assertTrue(self.manager.state_path.is_file())
        self.assertTrue(self.manager.html_path.is_file())
        self.assertTrue(self.manager.stream_path.is_file())
        self.assertTrue(status["stream_available"])
        self.assertNotIn("__DATA_JSON__", self.manager.html_path.read_text(encoding="utf-8"))
        stored = aquarium_runtime.read_world(self.manager.state_path)
        self.assertEqual(stored["clock"], {"tick_seconds": 2, "months_per_tick": 3})
        self.assertEqual(list(self.manager.state_path.parent.glob("*.tmp")), [])

    def test_default_live_clock_leaves_headroom_for_nas(self):
        self.manager.start_world({"turns": 1, "seed": 1})
        stored = aquarium_runtime.read_world(self.manager.state_path)
        self.assertEqual(
            stored["clock"], {"tick_seconds": 30, "months_per_tick": 1})

    def test_streaming_advance_keeps_initial_html_and_updates_atomic_stream(self):
        started = self.manager.start_world(
            {"turns": 2, "seed": 5}, tick_seconds=5,
            months_per_tick=1)
        html_before = self.manager.html_path.read_bytes()
        stream_before = aquarium_runtime.read_stream(self.manager.stream_path)
        advanced = self.manager.advance(1)
        stream_after = aquarium_runtime.read_stream(self.manager.stream_path)

        self.assertEqual(self.manager.html_path.read_bytes(), html_before)
        self.assertEqual(advanced["revision"], started["revision"] + 1)
        self.assertEqual(
            stream_after["base_revision"], advanced["revision"])
        self.assertGreater(
            stream_after["last_sequence"], stream_before["last_sequence"])

    def test_same_world_reuses_worker_and_sends_only_advance_command(self):
        self.manager.start_world(
            {"turns": 2, "seed": 5}, tick_seconds=5,
            months_per_tick=1)
        worker = self.manager._worker_process
        requests = []
        original_exchange = self.manager._exchange_with_worker

        def record_request(proc, request):
            requests.append(copy.deepcopy(request))
            return original_exchange(proc, request)

        self.manager._exchange_with_worker = record_request
        first = self.manager.advance(1)
        second = self.manager.advance(1)

        self.assertIs(self.manager._worker_process, worker)
        self.assertIsNone(worker.poll())
        self.assertEqual(requests, [
            {"action": "advance", "months": 1},
            {"action": "advance", "months": 1},
        ])
        self.assertEqual(first["summary"]["completed_turn"], 3)
        self.assertEqual(second["summary"]["completed_turn"], 4)
        self.assertEqual(
            first["compute_performance"]["sample_count"], 1)
        self.assertEqual(
            second["compute_performance"]["sample_count"], 2)

    def test_dead_worker_recovers_from_last_committed_world(self):
        self.manager.start_world(
            {"turns": 2, "seed": 5}, tick_seconds=5,
            months_per_tick=1)
        dead_worker = self.manager._worker_process
        dead_worker.terminate()
        dead_worker.wait(timeout=3)

        advanced = self.manager.advance(1)

        self.assertEqual(advanced["summary"]["completed_turn"], 3)
        self.assertIsNot(self.manager._worker_process, dead_worker)
        self.assertIsNone(self.manager._worker_process.poll())

    def test_new_world_replaces_dedicated_worker(self):
        self.manager.start_world(
            {"turns": 1, "seed": 5}, tick_seconds=5,
            months_per_tick=1)
        prior_worker = self.manager._worker_process

        replaced = self.manager.start_world(
            {"turns": 1, "seed": 9}, tick_seconds=5,
            months_per_tick=1)

        self.assertIsNot(self.manager._worker_process, prior_worker)
        self.assertIsNotNone(prior_worker.poll())
        self.assertEqual(replaced["summary"]["completed_turn"], 1)
        stored = aquarium_runtime.read_world(self.manager.state_path)
        self.assertEqual(stored["config"]["values"]["seed"], 9)

    def test_unmeasured_capacity_exposes_and_enforces_conservative_minimum(self):
        root = Path(self.temp.name)
        manager = aquarium_runtime.AquariumManager(
            root / "unmeasured.json", root / "unmeasured.html",
            time_fn=lambda: self.now[0],
            monotonic_fn=lambda: self.monotonic[0])
        self.assertEqual(manager.status()["minimum_tick_seconds"], 30)
        with self.assertRaisesRegex(ValueError, "at least 30 seconds"):
            manager.start_world({"turns": 1, "seed": 1}, tick_seconds=29)

    def test_measured_compute_capacity_raises_clock_with_fifty_percent_headroom(self):
        self.manager.start_world(
            {"turns": 2, "seed": 5}, tick_seconds=5,
            months_per_tick=1)
        original_invoke = self.manager._invoke

        def measured_invoke(payload):
            result = original_invoke(payload)
            if payload.get("action") == "advance":
                self.monotonic[0] += 8.0
            return result

        self.manager._invoke = measured_invoke
        advanced = self.manager.advance(1)
        self.assertEqual(advanced["minimum_tick_seconds"], 12)
        self.assertEqual(advanced["clock"]["tick_seconds"], 12)
        performance = advanced["compute_performance"]
        self.assertEqual(performance["last_compute_seconds"], 8.0)
        self.assertEqual(performance["ema_compute_seconds"], 8.0)
        self.assertEqual(performance["safe_tick_seconds"], 12)
        self.assertEqual(performance["headroom_factor"], 1.5)
        self.assertEqual(performance["sample_count"], 1)
        self.assertIn("simulation_seconds", performance["breakdown_seconds"])
        self.assertIn("build_dashboard_seconds", performance["breakdown_seconds"])
        self.assertIn("worker_boundary_seconds", performance["breakdown_seconds"])
        with self.assertRaisesRegex(ValueError, "at least 12 seconds"):
            self.manager.start_world(
                {"turns": 1, "seed": 1}, tick_seconds=11)

    def test_atomic_commit_time_is_measured_and_survives_manager_restart(self):
        self.manager.start_world(
            {"turns": 2, "seed": 5}, tick_seconds=5,
            months_per_tick=1)
        original_invoke = self.manager._invoke
        original_atomic_write = aquarium_runtime._atomic_write

        def measured_invoke(payload):
            result = original_invoke(payload)
            if payload.get("action") == "advance":
                self.monotonic[0] += 8.0
            return result

        def measured_atomic_write(path, text):
            # recurring commitのstreamとworldを各2秒とし、計測結果を保存する
            # 小さなsidecar自体はpipeline時間に含めない。
            if Path(path) in (self.manager.stream_path, self.manager.state_path):
                self.monotonic[0] += 2.0
            return original_atomic_write(path, text)

        self.manager._invoke = measured_invoke
        aquarium_runtime._atomic_write = measured_atomic_write
        try:
            advanced = self.manager.advance(1)
        finally:
            aquarium_runtime._atomic_write = original_atomic_write

        performance = advanced["compute_performance"]
        self.assertEqual(performance["last_worker_seconds"], 8.0)
        self.assertEqual(performance["last_commit_seconds"], 4.0)
        self.assertEqual(performance["last_pipeline_seconds"], 12.0)
        self.assertEqual(performance["breakdown_seconds"]["commit_seconds"], 4.0)
        self.assertEqual(performance["safe_tick_seconds"], 18)
        self.assertEqual(advanced["clock"]["tick_seconds"], 18)
        self.assertTrue(self.manager.performance_path.is_file())

        restarted = aquarium_runtime.AquariumManager(
            self.manager.state_path, self.manager.html_path,
            stream_path=self.manager.stream_path,
            performance_path=self.manager.performance_path,
            time_fn=lambda: self.now[0],
            monotonic_fn=lambda: self.monotonic[0],
            unmeasured_safe_tick_seconds=1)
        restored = restarted.status()
        self.assertEqual(restored["compute_performance"], performance)
        self.assertEqual(restored["minimum_tick_seconds"], 18)
        self.assertEqual(restored["clock"]["tick_seconds"], 18)

    def test_unsafe_existing_clock_measures_one_tick_instead_of_old_catchup(self):
        self.manager.start_world(
            {"turns": 2, "seed": 5}, tick_seconds=5,
            months_per_tick=1)
        world = aquarium_runtime.read_world(self.manager.state_path)
        world["compute_performance"]["safe_tick_seconds"] = 12
        aquarium_runtime.atomic_write_json(self.manager.state_path, world)
        original_invoke = self.manager._invoke

        def measured_invoke(payload):
            result = original_invoke(payload)
            if payload.get("action") == "advance":
                self.monotonic[0] += 8.0
            return result

        self.manager._invoke = measured_invoke
        # 旧5秒時計なら10回catch-upする遅れだが、安全下限への昇格時は
        # 1か月だけ進め、その処理時間を次の下限計算へ使う。
        self.now[0] = 1050.0
        advanced = self.manager.advance_if_due()
        self.assertEqual(advanced["summary"]["completed_turn"], 3)
        self.assertEqual(advanced["clock"]["tick_seconds"], 12)
        self.assertEqual(advanced["compute_performance"]["sample_count"], 1)
        self.assertEqual(advanced["next_tick_at"], 1062.0)

    def test_version_one_named_registry_is_upgraded_without_replacing_people(self):
        started = run_worker({
            "action": "start", "values": {
                "turns": 2, "seed": 23, "talent": "health"},
        })
        world = started["world"]
        registry = world["checkpoint"]["resident_registry"]
        names_before = {
            key: row["name"] for key, row in registry["residents"].items()}
        registry["version"] = 1
        for household in registry["households"].values():
            for key in ("livelihood", "activity_count", "last_activity_turn",
                        "last_activity", "last_actor_id"):
                household.pop(key, None)
        for resident in registry["residents"].values():
            for key in ("activity_count", "last_activity_turn", "last_activity"):
                resident.pop(key, None)

        advanced = run_worker({
            "action": "advance", "world": world, "months": 1,
        })["world"]
        upgraded = advanced["checkpoint"]["resident_registry"]
        self.assertEqual(upgraded["version"], RESIDENT_REGISTRY_VERSION)
        self.assertEqual(
            {key: upgraded["residents"][key]["name"] for key in names_before},
            names_before)
        self.assertTrue(all(
            "livelihood" in row for row in upgraded["households"].values()))
        self.assertTrue(any(
            row["kind"] == "production_activity"
            for row in advanced["trace"]["resident_events"]))

    def test_wall_clock_pause_resume_and_manual_advance(self):
        self.manager.start_world(
            {"turns": 6, "seed": 3, "talent": "health"},
            tick_seconds=2, months_per_tick=3)
        self.now[0] = 1001.9
        self.assertIsNone(self.manager.advance_if_due())
        self.now[0] = 1002.0
        due = self.manager.advance_if_due()
        self.assertEqual(due["summary"]["completed_turn"], 9)
        self.assertEqual(due["next_tick_at"], 1004.0)

        self.assertTrue(self.manager.pause()["paused"])
        self.now[0] = 2000.0
        self.assertIsNone(self.manager.advance_if_due())
        self.assertTrue(self.manager.resume()["running"])
        advanced = self.manager.advance(12)
        self.assertEqual(advanced["summary"]["completed_turn"], 21)

    def test_new_manager_reads_existing_world_after_restart(self):
        before = self.manager.start_world(
            {"turns": 4, "seed": 5}, tick_seconds=5, months_per_tick=1)
        replacement = aquarium_runtime.AquariumManager(
            self.manager.state_path, self.manager.html_path,
            time_fn=lambda: self.now[0])
        self.assertEqual(replacement.status()["summary"], before["summary"])

    def test_status_polling_reuses_small_published_snapshot(self):
        started = self.manager.start_world(
            {"turns": 4, "seed": 5}, tick_seconds=5, months_per_tick=1)
        with mock.patch.object(
                aquarium_runtime, "read_world",
                side_effect=AssertionError("full world was parsed by status poll")):
            first = self.manager.status()
            second = self.manager.status()
        self.assertEqual(first, started)
        self.assertEqual(second, started)
        self.assertIsNot(first, second)
        self.assertIsNot(first["summary"], second["summary"])

    def test_startup_refreshes_stale_dashboard_without_changing_world(self):
        self.manager.start_world(
            {"turns": 4, "seed": 5}, tick_seconds=5, months_per_tick=1)
        self.manager.pause()
        dashboard_data = build_dashboard.extract_dashboard_data(
            self.manager.html_path.read_text(encoding="utf-8"))
        stale_html = (
            "<!doctype html><script>const DATA = "
            + json.dumps(dashboard_data, ensure_ascii=False, separators=(",", ":"))
            + ";</script><p>STALE_TEMPLATE</p>")
        self.manager.html_path.write_text(stale_html, encoding="utf-8")
        world_before = self.manager.state_path.read_bytes()

        replacement = aquarium_runtime.AquariumManager(
            self.manager.state_path, self.manager.html_path,
            time_fn=lambda: self.now[0])
        replacement.start_background()
        replacement.stop_background()

        refreshed = self.manager.html_path.read_text(encoding="utf-8")
        self.assertEqual(self.manager.state_path.read_bytes(), world_before)
        self.assertNotIn("STALE_TEMPLATE", refreshed)
        self.assertIn("function zoomAquariumAt", refreshed)
        self.assertIn("pioneering_started", refreshed)
        self.assertEqual(
            build_dashboard.extract_dashboard_data(refreshed), dashboard_data)
        self.assertEqual(list(self.manager.state_path.parent.glob("*.tmp")), [])

    def test_legacy_world_builds_stream_without_rewriting_checkpoint(self):
        self.manager.start_world(
            {"turns": 4, "seed": 5}, tick_seconds=5, months_per_tick=1)
        world = aquarium_runtime.read_world(self.manager.state_path)
        world.pop("stream_id")
        world.pop("frame_sequence_end")
        aquarium_runtime.atomic_write_json(self.manager.state_path, world)
        self.manager.stream_path.unlink()
        world_before = self.manager.state_path.read_bytes()

        replacement = aquarium_runtime.AquariumManager(
            self.manager.state_path, self.manager.html_path,
            time_fn=lambda: self.now[0])
        replacement.start_background()
        replacement.stop_background()

        self.assertEqual(self.manager.state_path.read_bytes(), world_before)
        chunk = replacement.frame_chunk()
        self.assertTrue(chunk["exists"])
        self.assertEqual(len(chunk["frames"]), 100)
        self.assertIsNotNone(chunk["keyframe"])

    def test_resident_detail_reads_one_current_registry_record(self):
        self.manager.start_world(
            {"turns": 2, "seed": 5}, tick_seconds=5, months_per_tick=1)
        world = aquarium_runtime.read_world(self.manager.state_path)
        resident_id = world["checkpoint"]["focus_resident_id"]
        detail = self.manager.resident_detail(resident_id)
        self.assertEqual(detail["resident"]["id"], resident_id)
        self.assertEqual(
            detail["household"]["id"],
            detail["resident"]["household_id"])
        self.assertEqual(detail["turn"], 2)
        self.assertEqual(detail["revision"], 1)
        self.assertEqual(
            detail["household_goods"],
            world["checkpoint"]["household_goods_state"]["households"][
                detail["resident"]["household_id"]])
        self.assertEqual(
            detail["household_response"],
            world["checkpoint"]["household_agency_state"]["households"][
                detail["resident"]["household_id"]])
        self.assertIsNotNone(detail["position"])
        self.assertIsNotNone(detail["site"])
        self.assertIn(
            detail["activity_community_id"],
            world["checkpoint"]["spatial_state"]["clusters"])
        self.assertTrue(detail["organizations"])
        self.assertTrue(all(
            resident_id in world["checkpoint"]["organization_state"]
            ["organizations"][row["id"]]["named_member_ids"]
            for row in detail["organizations"]))
        self.assertTrue(all("work_relationship" in row
                            and "is_worker" in row
                            for row in detail["organizations"]))
        self.assertTrue(all("asset_claims" in row
                            and "asset_claim_total" in row
                            for row in detail["organizations"]))
        detail["resident"]["parent_ids"].append("changed-in-response")
        reread = self.manager.resident_detail(resident_id)
        self.assertNotIn(
            "changed-in-response", reread["resident"]["parent_ids"])
        with self.assertRaises(KeyError):
            self.manager.resident_detail("r999999999")
        with self.assertRaisesRegex(ValueError, "too long"):
            self.manager.resident_detail("r" * 129)

    def test_observation_reads_do_not_wait_for_world_update_lock(self):
        self.manager.start_world(
            {"turns": 2, "seed": 5}, tick_seconds=5, months_per_tick=1)
        world = aquarium_runtime.read_world(self.manager.state_path)
        resident_id = world["checkpoint"]["focus_resident_id"]
        result = {}
        finished = threading.Event()

        def observe():
            try:
                result["status"] = self.manager.status()
                result["detail"] = self.manager.resident_detail(resident_id)
                result["frames"] = self.manager.frame_chunk()
            except Exception as exc:  # pragma: no cover - asserted below
                result["error"] = exc
            finally:
                finished.set()

        # 実際のworker更新はこのlockを数秒〜数分保持しうる。観察APIは
        # atomicに保存済みの直前snapshotを読み、更新完了を待ってはならない。
        with self.manager.lock:
            observer = threading.Thread(target=observe)
            observer.start()
            self.assertTrue(
                finished.wait(1.0),
                "status/resident detail waited for the update lock")
        observer.join(timeout=1.0)

        self.assertNotIn("error", result)
        self.assertEqual(result["status"]["revision"], 1)
        self.assertEqual(result["detail"]["resident"]["id"], resident_id)
        self.assertEqual(len(result["frames"]["frames"]), 100)

    def test_stream_sequence_continues_and_keyframe_is_cursor_aware(self):
        self.manager.start_world(
            {"turns": 2, "seed": 5}, tick_seconds=5, months_per_tick=1)
        first = self.manager.frame_chunk()
        first_snapshot = aquarium_runtime.read_stream(self.manager.stream_path)
        self.assertEqual(first["first_sequence"], 1)
        self.assertEqual(first["last_sequence"], 100)
        self.assertIsNotNone(first["keyframe"])
        self.assertEqual(first["frames"][0]["month_phase"], 0.0)
        self.assertEqual(first["frames"][-1]["month_phase"], 1.0)

        paused = self.manager.pause()
        resumed = self.manager.resume()
        self.assertEqual(paused["revision"], first["base_revision"])
        self.assertEqual(resumed["revision"], first["base_revision"])
        clock_only = self.manager.frame_chunk()
        self.assertEqual(clock_only["base_revision"], first["base_revision"])
        self.assertEqual(clock_only["first_sequence"], 1)
        self.assertEqual(clock_only["last_sequence"], 100)

        exhausted = self.manager.frame_chunk(
            client_stream_id=first["stream_id"],
            client_revision=first["base_revision"], after_sequence=100)
        self.assertIsNone(exhausted["keyframe"])
        self.assertEqual(exhausted["frames"], [])

        self.manager.advance(1)
        second = self.manager.frame_chunk(
            client_stream_id=first["stream_id"],
            client_revision=first["base_revision"], after_sequence=100)
        self.assertEqual(second["first_sequence"], 101)
        self.assertEqual(second["last_sequence"], 200)
        self.assertIsNone(second["keyframe"])
        self.assertIsNotNone(second["keyframe_delta"])
        second_snapshot = aquarium_runtime.read_stream(self.manager.stream_path)
        self.assertEqual(
            aquarium_stream.apply_keyframe_delta(
                first_snapshot["keyframe"], second["keyframe_delta"]),
            second_snapshot["keyframe"])
        stale = self.manager.frame_chunk(
            client_stream_id=first["stream_id"],
            client_revision=first["base_revision"] - 1,
            after_sequence=100)
        self.assertIsNotNone(stale["keyframe"])
        self.assertIsNone(stale["keyframe_delta"])
        self.assertEqual(stale["keyframe"]["turns"][-1]["t"], 3)

    def test_corrupt_stream_cache_recovers_with_full_keyframe(self):
        self.manager.start_world(
            {"turns": 2, "seed": 5}, tick_seconds=5, months_per_tick=1)
        first = self.manager.frame_chunk()
        self.manager.stream_path.write_text("{broken", encoding="utf-8")

        advanced = self.manager.advance(1)
        self.assertEqual(advanced["summary"]["completed_turn"], 3)
        recovered = self.manager.frame_chunk(
            client_stream_id=first["stream_id"],
            client_revision=first["base_revision"], after_sequence=100)
        self.assertIsNotNone(recovered["keyframe"])
        self.assertIsNone(recovered["keyframe_delta"])
        self.assertEqual(recovered["keyframe"]["turns"][-1]["t"], 3)

    def test_multi_month_advance_is_buffered_in_turn_order(self):
        self.manager.start_world(
            {"turns": 2, "seed": 5}, tick_seconds=5, months_per_tick=1)
        self.manager.advance(3)
        world = aquarium_runtime.read_world(self.manager.state_path)
        chunk = self.manager.frame_chunk()

        self.assertEqual(world["frame_turn_start"], 3)
        self.assertEqual(world["frame_turn_end"], 5)
        self.assertEqual(chunk["frame_schema_version"], 2)
        self.assertEqual(chunk["frame_turn_start"], 3)
        self.assertEqual(chunk["frame_turn_end"], 5)
        self.assertEqual(
            sorted({row["turn"] for row in chunk["frames"]}), [3, 4, 5])
        self.assertEqual(chunk["frames"][0]["turn"], 3)
        self.assertEqual(chunk["frames"][-1]["turn"], 5)

    def test_http_frame_endpoint_serves_chunk_and_validates_cursor(self):
        self.manager.start_world(
            {"turns": 2, "seed": 5}, tick_seconds=5, months_per_tick=1)
        handler = object.__new__(dashboard_server.Handler)
        handler.server = SimpleNamespace(
            aquarium_manager=self.manager, report_dir=Path(self.temp.name))
        sent = []
        handler._send_json = lambda status, body: sent.append((status, body))

        handler.path = "/api/aquarium/frames?limit=30"
        handler.do_GET()
        self.assertEqual(sent[-1][0], 200)
        self.assertEqual(len(sent[-1][1]["frames"]), 30)
        self.assertIsNotNone(sent[-1][1]["keyframe"])

        handler.path = "/api/aquarium/frames?limit=151"
        handler.do_GET()
        self.assertEqual(sent[-1][0], 400)
        self.assertIn("limit", sent[-1][1]["error"])

    def test_clock_validation_and_missing_world_errors(self):
        with self.assertRaises(ValueError):
            self.manager.start_world({"turns": 1}, tick_seconds=0)
        with self.assertRaises(ValueError):
            self.manager.start_world({"turns": 1}, history_months=12)
        with self.assertRaises(ValueError):
            self.manager.advance(1)
        with self.assertRaises(ValueError):
            self.manager.pause()


class AquariumStreamContractTest(unittest.TestCase):
    def test_keyframe_is_bounded_and_does_not_mutate_dashboard_data(self):
        data = {
            "meta": {"aquarium_live": True},
            "final": {"population": 3},
            "turns": [{"t": turn, "population": turn} for turn in range(1, 131)],
            "observer_events": [
                {"t": turn, "kind": "resident_born"}
                for turn in (1, 10, 11, 130)],
            "settlements": [
                {"t": turn, "cp": "x"} for turn in (1, 10, 11, 130)],
            "event_density_history": {
                "version": 1, "settlement_ids": ["home"],
                "activity_keys": ["food"],
                "population": [[1, 0, 0, 0, 3, 1], [11, 0, 1, 0, 4, 2]],
                "migrations": [[130, 0, 0, 1, 1]],
                "activities": [[10, 0, 0, 1, 0], [130, 0, 0, 2, 0]],
            },
        }
        before = copy.deepcopy(data)
        keyframe = aquarium_stream.build_live_keyframe(data)

        self.assertEqual(data, before)
        self.assertEqual(len(keyframe["turns"]), 120)
        self.assertEqual(keyframe["turns"][0]["t"], 11)
        self.assertEqual(
            [row["t"] for row in keyframe["observer_events"]], [11, 130])
        self.assertEqual(
            [row[0] for row in keyframe["event_density_history"]["population"]],
            [11])
        self.assertEqual(
            [row[0] for row in keyframe["event_density_history"]["activities"]],
            [130])

    def test_snapshot_and_chunk_have_contiguous_hundred_frame_contract(self):
        data = {"turns": [{"t": 24}], "meta": {}, "observer_events": [],
                "settlements": []}
        world = {
            "stream_id": "world-a", "frame_sequence_end": 300,
            "revision": 7, "updated_at": 12.5,
            "clock": {"tick_seconds": 30},
            "summary": {"completed_turn": 24},
        }
        snapshot = aquarium_stream.build_stream_snapshot(data, world)
        aquarium_stream.validate_stream_snapshot(snapshot)
        self.assertEqual(snapshot["first_sequence"], 201)
        self.assertEqual(snapshot["last_sequence"], 300)
        self.assertEqual(len(snapshot["frames"]), 100)
        self.assertEqual(
            [row["sequence"] for row in snapshot["frames"]],
            list(range(201, 301)))

        initial = aquarium_stream.stream_chunk(snapshot, limit=30)
        self.assertIsNotNone(initial["keyframe"])
        self.assertEqual(len(initial["frames"]), 30)
        continued = aquarium_stream.stream_chunk(
            snapshot, client_stream_id="world-a", client_revision=7,
            after_sequence=230, limit=150)
        self.assertIsNone(continued["keyframe"])
        self.assertEqual(continued["frames"][0]["sequence"], 231)
        mismatch = aquarium_stream.stream_chunk(
            snapshot, client_stream_id="world-a", client_revision=6,
            after_sequence=299)
        self.assertIsNotNone(mismatch["keyframe"])
        self.assertEqual(mismatch["frames"][0]["sequence"], 201)

    def test_revision_delta_reconstructs_exact_keyframe_and_falls_back_to_full(self):
        static_spatial = {
            "updated_turn": 1, "blob": "unchanged" * 20_000}
        before_data = {
            "meta": {"aquarium_live": True},
            "turns": [{"t": 1, "population": 2}],
            "observer_events": [], "settlements": [],
            "npcs": [{"name": "A", "trust": 50}],
            "residents": [
                {"id": "r2", "age": 20, "alive": True},
                {"id": "r1", "age": 10, "alive": True}],
            "households": [{"id": "h1", "living_members": 2}],
            "spatial_state": static_spatial,
            "spatial_history": {
                "version": 1, "source_frame_count": 1,
                "frames": [{"turn": 1, "value": "old"}]},
        }
        after_data = copy.deepcopy(before_data)
        after_data["turns"].append({"t": 2, "population": 3})
        after_data["observer_events"] = [
            {"t": 2, "kind": "resident_born"}]
        after_data["npcs"][0]["trust"] = 51
        after_data["residents"] = [
            {"id": "r1", "age": 10.1, "alive": True},
            {"id": "r3", "age": 0, "alive": True},
            {"id": "r2", "age": 20.1, "alive": True}]
        after_data["households"][0]["living_members"] = 3
        after_data["spatial_history"] = {
            "version": 1, "source_frame_count": 2,
            "frames": [{"turn": 2, "value": "new"}]}
        before_copy, after_copy = copy.deepcopy(before_data), copy.deepcopy(after_data)
        random_state = random.getstate()
        first = aquarium_stream.build_stream_snapshot(before_data, {
            "stream_id": "delta-world", "frame_sequence_end": 100,
            "revision": 1, "summary": {"completed_turn": 1},
            "clock": {"tick_seconds": 30}})
        second = aquarium_stream.build_stream_snapshot(after_data, {
            "stream_id": "delta-world", "frame_sequence_end": 200,
            "revision": 2, "summary": {"completed_turn": 2},
            "frame_turn_start": 2, "frame_turn_end": 2,
            "clock": {"tick_seconds": 30}}, previous_snapshot=first)

        self.assertEqual(before_data, before_copy)
        self.assertEqual(after_data, after_copy)
        self.assertEqual(random.getstate(), random_state)
        aquarium_stream.validate_stream_snapshot(second)
        self.assertEqual(
            aquarium_stream.apply_keyframe_delta(
                first["keyframe"], second["keyframe_delta"]),
            second["keyframe"])
        delta_chunk = aquarium_stream.stream_chunk(
            second, client_stream_id="delta-world", client_revision=1,
            after_sequence=100)
        self.assertIsNone(delta_chunk["keyframe"])
        self.assertIsNotNone(delta_chunk["keyframe_delta"])
        same = aquarium_stream.stream_chunk(
            second, client_stream_id="delta-world", client_revision=2,
            after_sequence=200)
        self.assertIsNone(same["keyframe"])
        self.assertIsNone(same["keyframe_delta"])
        stale = aquarium_stream.stream_chunk(
            second, client_stream_id="delta-world", client_revision=0,
            after_sequence=100)
        self.assertIsNotNone(stale["keyframe"])
        self.assertIsNone(stale["keyframe_delta"])
        full_bytes = len(json.dumps(
            second["keyframe"], separators=(",", ":")).encode())
        delta_bytes = len(json.dumps(
            second["keyframe_delta"], separators=(",", ":")).encode())
        self.assertLess(delta_bytes, full_bytes // 4)
        bad_schema = copy.deepcopy(second)
        bad_schema["keyframe_delta"]["schema_version"] = 99
        with self.assertRaisesRegex(ValueError, "delta schema"):
            aquarium_stream.validate_stream_snapshot(bad_schema)
        bad_source = copy.deepcopy(second)
        bad_source["keyframe_delta"]["from_revision"] = 0
        with self.assertRaisesRegex(ValueError, "source"):
            aquarium_stream.validate_stream_snapshot(bad_source)

    def test_semantic_frames_cover_confirmed_turns_and_reference_signals(self):
        data = {
            "turns": [{"t": turn} for turn in range(20, 26)],
            "meta": {}, "settlements": [],
            "observer_events": [
                {"t": 21, "kind": "production_activity"},
                {"t": 22, "kind": "intersettlement_trade"},
                {"t": 23, "kind": "residents_migrated"},
                {"t": 24, "kind": "organization_reformed"},
                {"t": 25, "kind": "household_barter_exchange_summary"},
            ],
            "spatial_keyframes": [{"turn": 24}],
            "spatial_history": {"version": 1, "frames": []},
        }
        before = copy.deepcopy(data)
        random_state = random.getstate()
        world = {
            "stream_id": "world-semantic", "frame_sequence_end": 100,
            "revision": 1, "summary": {"completed_turn": 24},
            "frame_turn_start": 21, "frame_turn_end": 25,
            "clock": {"tick_seconds": 30},
        }
        snapshot = aquarium_stream.build_stream_snapshot(data, world)

        self.assertEqual(data, before)
        self.assertEqual(random.getstate(), random_state)
        self.assertEqual(snapshot["frame_schema_version"], 2)
        self.assertEqual(snapshot["frame_turn_start"], 21)
        self.assertEqual(snapshot["frame_turn_end"], 25)
        self.assertEqual(snapshot["keyframe"]["spatial_keyframes"], [
            {"turn": 24}])
        self.assertEqual(snapshot["keyframe"]["spatial_history"], {
            "version": 1, "frames": []})
        by_turn = {}
        for frame in snapshot["frames"]:
            by_turn.setdefault(frame["turn"], []).append(frame)
        self.assertEqual({turn: len(rows) for turn, rows in by_turn.items()}, {
            21: 20, 22: 20, 23: 20, 24: 20, 25: 20})
        self.assertTrue(all(rows[0]["month_phase"] == 0.0
                            and rows[-1]["month_phase"] == 1.0
                            for rows in by_turn.values()))
        self.assertTrue(all(
            row["signal_mask"] & aquarium_stream.FRAME_SIGNAL_ACTIVITY
            for row in by_turn[21]))
        self.assertTrue(all(
            row["signal_mask"] & aquarium_stream.FRAME_SIGNAL_TRADE
            for row in by_turn[22]))
        self.assertTrue(all(
            row["signal_mask"] & aquarium_stream.FRAME_SIGNAL_MIGRATION
            for row in by_turn[23]))
        self.assertTrue(all(
            row["signal_mask"] & aquarium_stream.FRAME_SIGNAL_ORGANIZATION
            for row in by_turn[24]))
        self.assertTrue(all(
            row["signal_mask"] & aquarium_stream.FRAME_SIGNAL_TRADE
            for row in by_turn[25]))

    def test_event_payload_is_not_duplicated_into_hundred_frames(self):
        data = {
            "turns": [{"t": 8}], "meta": {}, "settlements": [],
            "observer_events": [
                {"t": 8, "kind": "intersettlement_trade",
                 "payload": "x" * 1000, "event_index": index}
                for index in range(200)
            ],
        }
        world = {
            "stream_id": "bounded", "frame_sequence_end": 100,
            "revision": 1, "summary": {"completed_turn": 8},
            "clock": {"tick_seconds": 30},
        }
        snapshot = aquarium_stream.build_stream_snapshot(data, world)
        encoded_frames = json.dumps(
            snapshot["frames"], separators=(",", ":")).encode()

        self.assertLess(len(encoded_frames), 10_000)
        self.assertGreater(
            len(json.dumps(snapshot["keyframe"]).encode()), 200_000)
        self.assertTrue(all(
            row["signal_mask"] & aquarium_stream.FRAME_SIGNAL_TRADE
            for row in snapshot["frames"]))

    def test_version_one_logical_frames_remain_readable(self):
        data = {"turns": [{"t": 1}], "observer_events": [],
                "settlements": []}
        world = {
            "stream_id": "legacy", "frame_sequence_end": 100,
            "revision": 1, "summary": {"completed_turn": 1},
            "clock": {"tick_seconds": 1},
        }
        legacy = aquarium_stream.build_stream_snapshot(data, world)
        legacy.pop("frame_schema_version")
        legacy.pop("frame_turn_start")
        legacy.pop("frame_turn_end")
        for frame in legacy["frames"]:
            frame.pop("signal_mask")

        aquarium_stream.validate_stream_snapshot(legacy)
        chunk = aquarium_stream.stream_chunk(legacy)
        self.assertEqual(chunk["frame_schema_version"], 1)
        self.assertEqual(chunk["frame_turn_start"], 1)
        self.assertEqual(chunk["frame_turn_end"], 1)

    def test_stream_validation_rejects_bad_cursor_limit_and_sequence(self):
        data = {"turns": [], "observer_events": [], "settlements": []}
        world = {
            "stream_id": "world-a", "frame_sequence_end": 100,
            "revision": 1, "summary": {"completed_turn": 0},
            "clock": {"tick_seconds": 1},
        }
        snapshot = aquarium_stream.build_stream_snapshot(data, world)
        with self.assertRaisesRegex(ValueError, "limit"):
            aquarium_stream.stream_chunk(snapshot, limit=151)
        with self.assertRaisesRegex(ValueError, "after_sequence"):
            aquarium_stream.stream_chunk(snapshot, after_sequence="bad")
        broken = copy.deepcopy(snapshot)
        broken["frames"][4]["sequence"] = 999
        with self.assertRaisesRegex(ValueError, "contiguous"):
            aquarium_stream.validate_stream_snapshot(broken)

class AquariumUiContractTest(unittest.TestCase):
    def test_embedded_dashboard_data_round_trips_through_template(self):
        data = {
            "meta": {"aquarium_live": True},
            "label": "粉体</script>\u2028水槽",
            "values": [1, 2.5, None],
        }
        html = build_dashboard.build_html(data, build_dashboard.DEFAULT_TEMPLATE)
        self.assertEqual(build_dashboard.extract_dashboard_data(html), data)
        self.assertNotIn("</script>\u2028水槽", html)

    def test_embedded_dashboard_data_rejects_invalid_documents(self):
        with self.assertRaisesRegex(ValueError, "no embedded DATA"):
            build_dashboard.extract_dashboard_data("<html></html>")
        with self.assertRaisesRegex(ValueError, "not terminated"):
            build_dashboard.extract_dashboard_data("const DATA = {}")
        with self.assertRaisesRegex(ValueError, "must be an object"):
            build_dashboard.extract_dashboard_data("const DATA = []; ")

    def test_console_has_live_controls_and_polling(self):
        app = (Path(__file__).resolve().parent / "dashboard" / "app.html").read_text(
            encoding="utf-8")
        for marker in (
                'id="aquarium-start"', 'id="aquarium-toggle"',
                'id="aquarium-year"', 'id="aquarium-initial"', "/api/aquarium/status",
                'id="aquarium-history"', "/api/aquarium/advance", "setInterval",
                "migration_total", "settlement_count", "life-ledger-scroll",
                "life-ledger-restore-scroll", "life-ledger-restore-view",
                "life-ledger-restore-aquarium-selection",
                "life-ledger-aquarium-camera",
                "life-ledger-restore-aquarium-camera",
                "life-ledger-live-clock",
                "life-ledger-stream-chunk", "life-ledger-stream-status",
                "life-ledger-stream-refill", "/api/aquarium/frames",
                "aquariumStreamCursor", "pullAquariumFrames",
                "life-ledger-resident-detail-request",
                "life-ledger-resident-detail", "/api/aquarium/resident",
                'id="fullscreen-toggle"', 'id="console-toggle"',
                "requestFullscreen", "100dvh"):
            self.assertIn(marker, app)
        self.assertNotIn("(forceView || changed)", app)
        self.assertIn("trade_volume_total", app)
        self.assertIn('id="aquarium-seconds" type="number"', app)
        self.assertIn('min="30" max="86400" step="1" value="30"', app)
        self.assertIn('id="aquarium-seconds-hint"', app)
        self.assertIn("minimum_tick_seconds", app)
        self.assertIn("50%の余裕", app)
        self.assertIn("commit_seconds: 'stream/checkpoint保存'", app)
        self.assertIn("orchestration_seconds: '制御処理'", app)

    def test_console_drawer_stays_above_its_click_to_close_backdrop(self):
        app = (Path(__file__).resolve().parent / "dashboard" / "app.html").read_text(
            encoding="utf-8")
        self.assertIn(".shell { position:absolute;", app)
        self.assertIn("aside { position:fixed; z-index:60;", app)
        self.assertIn(".console-backdrop { position:fixed; z-index:55;", app)
        self.assertNotIn(".shell { position:fixed;", app)

    def test_nas_installer_backs_up_state_and_waits_for_health(self):
        root = Path(__file__).resolve().parent
        installer = root / "deploy" / "nas" / "install-staged.sh"
        deployer = root / "deploy" / "nas" / "deploy-sweep-reports.ps1"
        shell = installer.read_text(encoding="utf-8")
        powershell = deployer.read_text(encoding="utf-8")
        proc = subprocess.run(
            ["sh", "-n", str(installer)], text=True,
            capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        for marker in (
                "compose stop", "backups/${timestamp}",
                "aquarium_world.json", "aquarium_stream.json",
                "aquarium_performance.json",
                "institutions/spatial.py",
                "compose config", "compose up -d --force-recreate",
                ".State.Health",
                "health check timed out"):
            self.assertIn(marker, shell)
        self.assertIn("install-staged.sh", powershell)
        self.assertIn('"aquarium_stream.py"', powershell)
        self.assertIn("sh '$RemoteStagingDir/install-staged.sh'", powershell)
        self.assertIn("[switch]$StageOnly", powershell)
        self.assertIn("if ($StageOnly)", powershell)
        self.assertIn("visible PowerShell terminal", powershell)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_console_javascript_is_syntactically_valid(self):
        app = (Path(__file__).resolve().parent / "dashboard" /
               "app.html").read_text(encoding="utf-8")
        script = app.split("<script>", 1)[1].split("</script>", 1)[0]
        proc = subprocess.run(
            [shutil.which("node"), "--check", "-"], input=script,
            text=True, capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_console_retries_transient_frame_failure_without_iframe_reload(self):
        app = (Path(__file__).resolve().parent / "dashboard" /
               "app.html").read_text(encoding="utf-8")
        source = "async function pullAquariumFrames" + app.split(
            "async function pullAquariumFrames", 1)[1]
        source = source.split(
            "async function forwardResidentDetail", 1)[0]
        script = r"""
let aquariumFrameReady=true;
let aquariumState={exists:true,revision:2,dashboard_available:true,
  summary:{completed_turn:2}};
let aquariumStreamCursor={streamId:'stream',revision:1,after:100,turn:1,pending:false};
let aquariumStreamUnavailable=false;
let aquariumStreamLastFetch=0;
let responseStatus=500;
let retryCount=0,showCount=0,statusMessages=[];
globalThis.performance={now:()=>1000};
globalThis.fetch=async()=>({ok:false,status:responseStatus,statusText:'failed'});
globalThis.document={getElementById(){return {contentWindow:{postMessage(){}}};}};
globalThis.window={setTimeout(){retryCount++;}};
function showAquarium(){showCount++;}
function setStatus(message,isError){statusMessages.push([message,isError]);}
""" + source + r"""
(async()=>{
  await pullAquariumFrames(true);
  const transient={unavailable:aquariumStreamUnavailable,
    pending:aquariumStreamCursor.pending,retryCount,showCount};
  responseStatus=404;
  await pullAquariumFrames(true);
  console.log(JSON.stringify({transient,unsupported:{
    unavailable:aquariumStreamUnavailable,
    pending:aquariumStreamCursor.pending,retryCount,showCount},
    statusMessages}));
})().catch(error=>{console.error(error);process.exit(1);});
"""
        proc = subprocess.run(
            [shutil.which("node")], input=script, text=True,
            capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["transient"], {
            "unavailable": False, "pending": False,
            "retryCount": 1, "showCount": 0,
        })
        self.assertEqual(result["unsupported"], {
            "unavailable": True, "pending": False,
            "retryCount": 1, "showCount": 1,
        })
        self.assertEqual(len(result["statusMessages"]), 2)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_dashboard_javascript_is_syntactically_valid(self):
        template = (Path(__file__).resolve().parent / "dashboard" /
                    "template.html").read_text(encoding="utf-8")
        script = template.split("<script>", 1)[1].split("</script>", 1)[0]
        script = script.replace("__DATA_JSON__", "{}")
        proc = subprocess.run(
            [shutil.which("node"), "--check", "-"], input=script,
            text=True, capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

    def test_live_dashboard_opens_at_latest_month(self):
        template = (Path(__file__).resolve().parent / "dashboard" / "template.html").read_text(
            encoding="utf-8")
        self.assertIn("DATA.meta.aquarium_live", template)
        self.assertIn("DATA.turns.length - 1", template)
        self.assertIn("population_migrated", template)
        self.assertIn("settlement_populations", template)
        self.assertIn("intersettlement_trade", template)
        self.assertIn("household_barter_exchange_summary", template)
        self.assertIn("routes_sample", template)
        self.assertIn("world_household_barter_exchange_count", template)
        self.assertIn("world_household_barter_volume_by_good", template)
        self.assertIn("世帯間交換", template)
        self.assertIn('id="settlement-ledger-body"', template)
        self.assertIn('id="resident-table-body"', template)
        self.assertIn("function renderResidentHouseholds()", template)
        self.assertIn('class="provisional-community"', template)
        self.assertIn("独立会計前", template)
        self.assertIn("resident_born", template)
        self.assertIn("life-ledger-restore-scroll", template)
        self.assertIn('id="pixel-aquarium"', template)
        self.assertIn('id="aquarium-camera-reset"', template)
        self.assertIn("function zoomAquariumAt", template)
        self.assertIn("function panAquariumBy", template)
        self.assertIn("function aquariumWorldAt", template)
        self.assertIn("aquariumCameraKey()", template)
        self.assertIn("life-ledger-aquarium-camera", template)
        self.assertIn("life-ledger-restore-aquarium-camera", template)
        self.assertIn('data-view-target="statistics"', template)
        self.assertIn("1人 = 1粒子", template)
        self.assertIn("重なり = 1pxへ色/人数集約", template)
        self.assertIn("function buildActivityField", template)
        self.assertIn("function buildPersistentActivityField", template)
        self.assertIn("function decodeSpatialKeyframe", template)
        self.assertIn("function interpolateSpatialRows", template)
        self.assertIn("DATA.spatial_state", template)
        self.assertIn("DATA.particle_frame", template)
        self.assertIn("DATA.particle_cohorts", template)
        self.assertIn("function particleFrame()", template)
        self.assertIn("activityModeKeys", template)
        self.assertIn("function aquariumMonthPhase", template)
        self.assertIn("function episodeCommuteAmount", template)
        self.assertIn("今月の担い手", template)
        self.assertIn("月次正史のanchorは常に住居側", template)
        self.assertIn("function populationCohortsAt", template)
        self.assertIn("function addPopulationCohorts", template)
        self.assertIn("function populationSitePatches", template)
        self.assertIn("function populationPatchVisibility", template)
        self.assertIn("maxAnonymousPerPixel", template)
        self.assertIn("typedArrayFromBase64", template)
        self.assertIn("residentRows", template)
        self.assertIn("DATA.spatial_keyframes", template)
        self.assertIn("DATA.spatial_history", template)
        self.assertIn("function decodeSpatialHistoryFrame", template)
        self.assertIn("SPATIAL_HISTORY_CACHE_LIMIT = 4", template)
        self.assertIn("function requestResidentDetail", template)
        self.assertIn("residentDetailCache", template)
        self.assertIn("life-ledger-resident-detail-request", template)
        self.assertIn("current.updated_turn", template)
        self.assertIn("const ParticleRaster", template)
        self.assertIn("new Uint32Array(pixels)", template)
        self.assertIn("new Float64Array(pixels)", template)
        self.assertIn("function buildAquariumParticleScene", template)
        self.assertIn("particleLayer", template)
        self.assertIn("dynamicIndices", template)
        self.assertIn("aquariumAnimationInterval", template)
        self.assertNotIn("pixelBuckets", template)
        self.assertIn("同じpixelに", template)
        self.assertIn("function spatialClusterEventsAt", template)
        self.assertIn("function drawClusterTransitionSignals", template)
        self.assertIn("活動集落", template)
        self.assertIn("疎な十字点 = 母共同体に属する前集落", template)
        self.assertIn("accountingClusterId", template)
        self.assertIn("DATA.activity_community_ledger", template)
        self.assertIn("function activityCommunityForResidentAt", template)
        self.assertIn("function inferActivityClusters", template)
        self.assertIn("function residentParticlePosition", template)
        self.assertIn("NAMED_ACTIVITY_MOTION_LIMIT = 4096", template)
        self.assertIn("ANONYMOUS_ACTIVITY_DRIFT_LIMIT = 384", template)
        self.assertIn("function renderAnonymousActivityDrifts", template)
        self.assertIn("function renderGenerationFlows", template)
        self.assertIn("GENERATION_FLOW_LIMIT = 128", template)
        self.assertIn("function chooseAquariumAnimationInterval", template)
        self.assertIn("aquariumRenderMilliseconds", template)
        self.assertIn(
            "ctx.fillRect(Math.round(x), Math.round(y), 1, 1)", template)
        self.assertIn("function drawAquariumPixel", template)
        self.assertIn("必需財労働", template)
        self.assertIn("activity_required_worker_total", template)
        self.assertIn("activity_labor_constrained_community_count", template)
        self.assertIn("財別担当", template)
        self.assertIn("activity_workers_by_good", template)
        self.assertIn("activity_required_workers_by_good", template)
        self.assertNotIn("peoplePerCell", template)
        self.assertNotIn("zoneRects", template)
        self.assertNotIn("aquariumZoneHitMap", template)
        self.assertIn("household_activity", template)
        self.assertIn("DATA.event_density_history", template)
        self.assertIn("EVENT_DENSITY_BY_TURN", template)
        self.assertIn("OBSERVER_EVENTS_BY_TURN", template)
        self.assertIn("function densityEventsAt", template)
        self.assertIn("function drawPopulationDensitySignals", template)
        self.assertIn("population_density_migration", template)
        self.assertIn("MIGRATIONS_BY_RESIDENT", template)
        self.assertIn("function residentSettlementAt", template)
        self.assertIn("function aquariumAnimationTick", template)
        self.assertIn("function receiveAquariumStreamChunk", template)
        self.assertIn("function applyAquariumLiveKeyframe", template)
        self.assertIn("function applyAquariumLiveDelta", template)
        self.assertIn("function aquariumStreamPhase", template)
        self.assertIn("function setAquariumStreamObservationTurn", template)
        self.assertIn("function aquariumPresentationSeconds", template)
        self.assertIn("AQUARIUM_STREAM_TARGET = 100", template)
        self.assertIn("AQUARIUM_STREAM_LOW = 30", template)
        self.assertIn("AQUARIUM_STREAM_START = 100", template)
        self.assertIn("AQUARIUM_STREAM_HIGH = 300", template)
        self.assertIn("function aquariumReservedFrameCount", template)
        self.assertIn("selected.resident.parent_ids", template)
        self.assertIn("life-ledger-aquarium-selection", template)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_live_stream_keyframe_and_hundred_frame_buffer_apply_without_reload(self):
        template = (Path(__file__).resolve().parent / "dashboard" /
                    "template.html").read_text(encoding="utf-8")
        data = {
            "meta": {"aquarium_live": True, "world_mode": True},
            "turns": [{"t": 1}], "final": {}, "observer_events": [],
            "event_density_history": {
                "version": 1, "settlement_ids": [], "activity_keys": [],
                "population": [], "migrations": [], "activities": []},
            "settlements": [], "npcs": [], "residents": [],
            "households": [], "spatial_state": {
                "updated_turn": 1, "sites": {}, "residents": {},
                "clusters": {}, "cluster_lineages": {}, "cluster_events": []},
            "spatial_keyframes": [],
            "spatial_history": {"version": 1, "frames": []},
            "particle_frame": None, "particle_cohorts": None,
            "activity_community_ledger": {"communities": {}},
            "settlement_states": {}, "choice_bins": [], "choice_keys": [],
        }
        script = template.split("<script>", 1)[1].split("</script>", 1)[0]
        script = script.replace(
            "__DATA_JSON__", json.dumps(data, separators=(",", ":")))
        script = script.split(
            "/* ---------------------------------------------------------------------\n"
            "   汎用チャート基盤", 1)[0]
        prefix = r"""
const messages=[];
const elements=new Map();
globalThis.document={querySelectorAll(){return[];},body:{dataset:{}},documentElement:{},getElementById(id){if(!elements.has(id))elements.set(id,{textContent:'',value:0,max:0});return elements.get(id);},createElement(){return{};}};
globalThis.window={addEventListener(){},parent:{postMessage(message){messages.push(message);}},requestAnimationFrame(){return 1;},cancelAnimationFrame(){}};
globalThis.getComputedStyle=()=>({getPropertyValue(){return '';}});
let renderAllCalls=0,renderObservationCalls=0;
function renderAll(){renderAllCalls++;}
function renderObservation(){renderObservationCalls++;}
"""
        suffix = r"""
const frames=Array.from({length:100},(_,index)=>({
  sequence:index+1,turn:index<50?2:3,
  month_phase:index<50?index/49:(index-50)/49,
  signal_mask:index<50?2:4,
}));
receiveAquariumStreamChunk({
  schema_version:1,frame_schema_version:2,exists:true,
  stream_id:'stream-a',base_revision:2,fps:30,
  first_sequence:1,last_sequence:100,frames,
  keyframe:{schema_version:1,turns:[{t:2},{t:3}],observer_events:[],settlements:[],
    event_density_history:{version:1,settlement_ids:[],activity_keys:[],population:[],migrations:[],activities:[]},
    meta:{aquarium_live:true,world_mode:true},final:{population:4},npcs:[],residents:[],households:[],
    spatial_state:{updated_turn:2,sites:{},residents:{},clusters:{},cluster_lineages:{},cluster_events:[]},
    activity_community_ledger:{communities:{}},settlement_states:{},choice_bins:[],choice_keys:[]}
});
const middle=aquariumStreamPhase(.25);
const middleTurn=currentObservation().t;
const late=aquariumStreamPhase(.75);
const lateTurn=currentObservation().t;
const currentBeforeTurnChange=aquariumStreamBuffer.current.sequence;
const lateSignal=aquariumStreamSignalMask();
aquariumLiveClock={turn:4};
const held=aquariumStreamPhase(.1);
console.log(JSON.stringify({
  turns:DATA.turns.map(row=>row.t),frames:aquariumStreamBuffer.frames.length,
  middle,late,held,current:currentBeforeTurnChange,
  observedTurns:[middleTurn,lateTurn,currentObservation().t],lateSignal,
  rendered:[renderAllCalls,renderObservationCalls],
  messageTypes:messages.map(row=>row.type),
  resync:messages.some(row=>row.type==='life-ledger-stream-resync'),
}));
"""
        proc = subprocess.run(
            [shutil.which("node")], input=prefix + script + suffix,
            text=True, capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["turns"], [1, 2, 3])
        self.assertEqual(result["frames"], 100)
        self.assertAlmostEqual(result["middle"], .5, delta=.03)
        self.assertAlmostEqual(result["late"], .5, delta=.03)
        self.assertAlmostEqual(result["held"], 1.0)
        self.assertEqual(result["current"], 75)
        self.assertEqual(result["observedTurns"], [2, 3, 3])
        self.assertEqual(result["lateSignal"], 4)
        self.assertEqual(result["rendered"], [1, 1])
        self.assertIn("life-ledger-stream-status", result["messageTypes"])
        self.assertIn("life-ledger-stream-refill", result["messageTypes"])
        self.assertFalse(result["resync"])

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_live_stream_playback_uses_its_queue_clock_not_expired_server_phase(self):
        template = (Path(__file__).resolve().parent / "dashboard" /
                    "template.html").read_text(encoding="utf-8")
        data = {
            "meta": {"aquarium_live": True, "world_mode": True},
            "turns": [{"t": 1}], "final": {}, "observer_events": [],
            "event_density_history": {
                "version": 1, "settlement_ids": [], "activity_keys": [],
                "population": [], "migrations": [], "activities": []},
            "settlements": [], "npcs": [], "residents": [],
            "households": [], "spatial_state": {
                "updated_turn": 1, "sites": {}, "residents": {},
                "clusters": {}, "cluster_lineages": {}, "cluster_events": []},
            "spatial_keyframes": [],
            "spatial_history": {"version": 1, "frames": []},
            "particle_frame": None, "particle_cohorts": None,
            "activity_community_ledger": {"communities": {}},
            "settlement_states": {}, "choice_bins": [], "choice_keys": [],
        }
        script = template.split("<script>", 1)[1].split("</script>", 1)[0]
        script = script.replace(
            "__DATA_JSON__", json.dumps(data, separators=(",", ":")))
        script = script.split(
            "/* ---------------------------------------------------------------------\n"
            "   汎用チャート基盤", 1)[0]
        prefix = r"""
const messages=[];
const elements=new Map();
globalThis.document={querySelectorAll(){return[];},body:{dataset:{}},documentElement:{},getElementById(id){if(!elements.has(id))elements.set(id,{textContent:'',value:0,max:0});return elements.get(id);},createElement(){return{};}};
globalThis.window={addEventListener(){},parent:{postMessage(message){messages.push(message);}},requestAnimationFrame(){return 1;},cancelAnimationFrame(){}};
globalThis.getComputedStyle=()=>({getPropertyValue(){return '';}});
let wallMs=0;
Object.defineProperty(globalThis,'performance',{value:{now:()=>wallMs},configurable:true});
function renderAll(){}
function renderObservation(){}
"""
        suffix = r"""
aquariumLiveClock={paused:false,tick_seconds:2,turn:2};
const frames=Array.from({length:100},(_,index)=>({
  sequence:index+1,turn:2,month_phase:index/99,signal_mask:0,
}));
receiveAquariumStreamChunk({
  schema_version:1,frame_schema_version:2,exists:true,
  stream_id:'queue-clock',base_revision:1,fps:30,tick_seconds:2,
  first_sequence:1,last_sequence:100,frames,
  keyframe:{schema_version:1,turns:[{t:2}],observer_events:[],settlements:[],
    event_density_history:{version:1,settlement_ids:[],activity_keys:[],population:[],migrations:[],activities:[]},
    meta:{aquarium_live:true,world_mode:true},final:{},npcs:[],residents:[],households:[],
    spatial_state:{updated_turn:2,sites:{},residents:{},clusters:{},cluster_lineages:{},cluster_events:[]},
    activity_community_ledger:{communities:{}},settlement_states:{},choice_bins:[],choice_keys:[]}
});
const start=aquariumStreamPhase(1);
const startStarted=aquariumStreamBuffer.playoutStarted;
const startBuffered=aquariumBufferedFrameCount();
wallMs=1000;
const firstMiddle=aquariumStreamPhase(1);
const firstMiddleStarted=aquariumStreamBuffer.playoutStarted;
function receiveNextRevision(revision,turn,firstSequence) {
  const nextFrames=Array.from({length:100},(_,index)=>({
    sequence:firstSequence+index,turn,month_phase:index/99,signal_mask:0,
  }));
  receiveAquariumStreamChunk({
    schema_version:1,frame_schema_version:2,exists:true,
    stream_id:'queue-clock',base_revision:revision,fps:30,tick_seconds:2,
    first_sequence:firstSequence,last_sequence:firstSequence+99,
    frames:nextFrames,keyframe:null,
    keyframe_delta:{schema_version:1,from_revision:revision-1,
      to_revision:revision,history_start_turn:1,
      turns:{order:DATA.turns.map(row=>row.t).concat([turn]),
        upsert:[{t:turn}],patch:[],remove:[]},entities:{},
      tails:{observer_events:[],settlements:[]},
      event_density_history:{version:1,settlement_ids:[],activity_keys:[],population:[],migrations:[],activities:[]},
      spatial_history:{version:1,source_frame_count:0,upsert:[],remove:[]},
      current:{final:{}}},
  });
}
receiveNextRevision(2,3,101);
const rollingStart=aquariumStreamPhase(1);
const reserveAtStart=aquariumReservedFrameCount();
wallMs=2000;
const middle=aquariumStreamPhase(1);
const middleBuffered=aquariumBufferedFrameCount();
const middleReserve=aquariumReservedFrameCount();
receiveNextRevision(3,4,201);
wallMs=3000;
const nextMonth=aquariumStreamPhase(1);
const rollingReserve=aquariumReservedFrameCount();
wallMs=6000;
const exhausted=aquariumStreamPhase(1);
const exhaustedStarted=aquariumStreamBuffer.playoutStarted;
wallMs=8101;
const stalled=aquariumStreamPhase(1);
const stalledStarted=aquariumStreamBuffer.playoutStarted;
receiveNextRevision(4,5,301);
const restarted=aquariumStreamPhase(1);
const restartedStarted=aquariumStreamBuffer.playoutStarted;
const restartBuffered=aquariumBufferedFrameCount();
aquariumLiveClock.paused=true;
wallMs=9000;
const held=aquariumStreamPhase(0);
console.log(JSON.stringify({
  start,startStarted,firstMiddle,firstMiddleStarted,rollingStart,reserveAtStart,
  middle,nextMonth,rollingReserve,held,startBuffered,middleBuffered,middleReserve,
  exhausted,exhaustedStarted,stalled,stalledStarted,
  restarted,restartedStarted,restartBuffered,
  presentationTime:aquariumStreamBuffer.presentationTime,
  revision:aquariumStreamBuffer.revision,
  resync:messages.some(row=>row.type==='life-ledger-stream-resync'),
}));
"""
        proc = subprocess.run(
            [shutil.which("node")], input=prefix + script + suffix,
            text=True, capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        result = json.loads(proc.stdout)
        self.assertAlmostEqual(result["start"], 0.0, delta=.001)
        self.assertTrue(result["startStarted"])
        self.assertAlmostEqual(result["firstMiddle"], 0.5, delta=.02)
        self.assertTrue(result["firstMiddleStarted"])
        self.assertAlmostEqual(result["rollingStart"], 0.5, delta=.02)
        self.assertEqual(result["reserveAtStart"], 100)
        self.assertAlmostEqual(result["middle"], 0.0, delta=.02)
        self.assertAlmostEqual(result["nextMonth"], 0.5, delta=.02)
        self.assertEqual(result["startBuffered"], 100)
        self.assertEqual(result["middleBuffered"], 100)
        self.assertEqual(result["middleReserve"], 0)
        self.assertEqual(result["rollingReserve"], 100)
        self.assertAlmostEqual(result["exhausted"], 1.0, delta=.02)
        self.assertTrue(result["exhaustedStarted"])
        self.assertAlmostEqual(result["stalled"], 1.0, delta=.02)
        self.assertFalse(result["stalledStarted"])
        self.assertAlmostEqual(result["restarted"], 0.0, delta=.02)
        self.assertTrue(result["restartedStarted"])
        self.assertEqual(result["restartBuffered"], 100)
        self.assertAlmostEqual(result["held"], 0.0, delta=.02)
        self.assertAlmostEqual(result["presentationTime"], 5.0)
        self.assertEqual(result["revision"], 4)
        self.assertFalse(result["resync"])

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_live_stream_applies_revision_delta_and_preserves_long_history(self):
        template = (Path(__file__).resolve().parent / "dashboard" /
                    "template.html").read_text(encoding="utf-8")
        data = {
            "meta": {"aquarium_live": True, "world_mode": True},
            "turns": [{"t": 1}], "final": {"population": 1},
            "observer_events": [{"t": 1, "kind": "resident_born"}],
            "event_density_history": {
                "version": 1, "settlement_ids": [], "activity_keys": [],
                "population": [], "migrations": [], "activities": []},
            "settlements": [], "npcs": [], "residents": [],
            "households": [], "spatial_state": {
                "updated_turn": 1, "sites": {}, "residents": {},
                "clusters": {}, "cluster_lineages": {}, "cluster_events": []},
            "spatial_keyframes": [],
            "spatial_history": {"version": 1, "source_frame_count": 1,
                                "frames": [{"turn": 1, "value": "one"}]},
            "particle_frame": None, "particle_cohorts": None,
            "activity_community_ledger": {"communities": {}},
            "settlement_states": {}, "choice_bins": [], "choice_keys": [],
        }
        script = template.split("<script>", 1)[1].split("</script>", 1)[0]
        script = script.replace(
            "__DATA_JSON__", json.dumps(data, separators=(",", ":")))
        script = script.split(
            "/* ---------------------------------------------------------------------\n"
            "   汎用チャート基盤", 1)[0]
        prefix = r"""
const messages=[];
const elements=new Map();
globalThis.document={querySelectorAll(){return[];},body:{dataset:{}},documentElement:{},getElementById(id){if(!elements.has(id))elements.set(id,{textContent:'',value:0,max:0});return elements.get(id);},createElement(){return{};}};
globalThis.window={addEventListener(){},parent:{postMessage(message){messages.push(message);}},requestAnimationFrame(){return 1;},cancelAnimationFrame(){}};
globalThis.getComputedStyle=()=>({getPropertyValue(){return '';}});
let renderAllCalls=0,renderObservationCalls=0;
function renderAll(){renderAllCalls++;}
function renderObservation(){renderObservationCalls++;}
"""
        suffix = r"""
receiveAquariumStreamChunk({
  schema_version:1,frame_schema_version:2,exists:true,
  stream_id:'stream-delta',base_revision:1,fps:30,
  first_sequence:1,last_sequence:1,
  frames:[{sequence:1,turn:2,month_phase:1,signal_mask:0}],
  keyframe:{schema_version:1,turns:[{t:2}],
    observer_events:[{t:2,kind:'organization_founded'}],settlements:[],
    event_density_history:{version:1,settlement_ids:[],activity_keys:[],population:[],migrations:[],activities:[]},
    meta:{aquarium_live:true,world_mode:true},final:{population:2},
    npcs:[{name:'A',trust:50}],residents:[{id:'r1',age:10}],
    households:[{id:'h1',living_members:1}],
    spatial_state:{updated_turn:2,sites:{},residents:{},clusters:{},cluster_lineages:{},cluster_events:[]},
    spatial_history:{version:1,source_frame_count:2,frames:[{turn:1,value:'one'},{turn:2,value:'two'}]},
    activity_community_ledger:{communities:{}},settlement_states:{},choice_bins:[],choice_keys:[]}
});
receiveAquariumStreamChunk({
  schema_version:1,frame_schema_version:2,exists:true,
  stream_id:'stream-delta',base_revision:2,fps:30,
  first_sequence:2,last_sequence:2,
  frames:[{sequence:2,turn:3,month_phase:1,signal_mask:16}],keyframe:null,
  keyframe_delta:{schema_version:1,from_revision:1,to_revision:2,
    history_start_turn:3,
    turns:{order:[3],upsert:[{t:3}],patch:[],remove:[2]},
    entities:{
      npcs:{order:['A'],upsert:[],patch:[{key:'A',set:{trust:52},remove:[]}],remove:[]},
      residents:{order:['r2','r1'],upsert:[{id:'r2',age:0}],patch:[{key:'r1',set:{age:10.1},remove:[]}],remove:[]},
      households:{order:['h1'],upsert:[],patch:[{key:'h1',set:{living_members:2},remove:[]}],remove:[]}},
    tails:{observer_events:[{t:3,kind:'resident_born'}],settlements:[]},
    event_density_history:{version:1,settlement_ids:[],activity_keys:[],population:[],migrations:[],activities:[]},
    spatial_history:{version:1,source_frame_count:3,upsert:[{turn:3,value:'three'}],remove:[1]},
    current:{final:{population:3}}}
});
console.log(JSON.stringify({
  turns:DATA.turns.map(row=>row.t),events:DATA.observer_events.map(row=>row.t),
  residents:DATA.residents.map(row=>[row.id,row.age]),npcTrust:DATA.npcs[0].trust,
  household:DATA.households[0].living_members,final:DATA.final.population,
  spatial:DATA.spatial_history.frames.map(row=>row.turn),
  revision:aquariumStreamBuffer.revision,frames:aquariumStreamBuffer.frames.length,
  observerTurn:DATA.turns[observerIndex].t,
  rendered:[renderAllCalls,renderObservationCalls],
  resync:messages.some(row=>row.type==='life-ledger-stream-resync'),
}));
"""
        proc = subprocess.run(
            [shutil.which("node")], input=prefix + script + suffix,
            text=True, capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result, {
            "turns": [1, 2, 3], "events": [1, 2, 3],
            "residents": [["r2", 0], ["r1", 10.1]],
            "npcTrust": 52, "household": 2, "final": 3,
            "spatial": [2, 3], "revision": 2, "frames": 2,
            "observerTurn": 2,
            "rendered": [2, 2], "resync": False,
        })

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_spatial_cluster_decoder_preserves_v4_provisional_and_reads_v3(self):
        template = (Path(__file__).resolve().parent / "dashboard" /
                    "template.html").read_text(encoding="utf-8")
        source = "function decodedClusterRow" + template.split(
            "function decodedClusterRow", 1)[1]
        source = source.split("\n}\n", 1)[0] + "\n}\n"
        script = source + r"""
const oldRow=decodedClusterRow(
  ['cluster:old',.2,.3,1,4,12,[],['site:old']],12);
const provisional=decodedClusterRow(
  ['cluster:new',.7,.8,1,5,24,['cluster:old'],['site:new'],true,'cluster:old'],24);
console.log(JSON.stringify({oldRow,provisional}));
"""
        proc = subprocess.run(
            [shutil.which("node")], input=script, text=True,
            capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        decoded = json.loads(proc.stdout)
        self.assertFalse(decoded["oldRow"]["provisional"])
        self.assertEqual(
            decoded["oldRow"]["accounting_parent_id"], "cluster:old")
        self.assertTrue(decoded["provisional"]["provisional"])
        self.assertEqual(
            decoded["provisional"]["accounting_parent_id"], "cluster:old")

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_provisional_outpost_keeps_physical_and_accounting_identity_in_ui(self):
        template = (Path(__file__).resolve().parent / "dashboard" /
                    "template.html").read_text(encoding="utf-8")
        data = {
            "meta": {"aquarium_live": False, "world_mode": True},
            "turns": [{"t": 24}], "final": {}, "observer_events": [],
            "residents": [
                {"id": "r0", "household_id": "h0"},
                {"id": "r1", "household_id": "h1"},
            ],
            "households": [
                {"id": "h0", "name": "母世帯", "settlement_id": "c0",
                 "livelihood": "food"},
                {"id": "h1", "name": "開拓世帯", "settlement_id": "c0",
                 "livelihood": "tools"},
            ],
            "spatial_state": {
                "updated_turn": 24,
                "sites": {
                    "s0": {"id": "s0", "household_id": "h0",
                           "livelihood": "food", "account_id": "c0",
                           "x": .35, "y": .5, "home_x": .34, "home_y": .5,
                           "founded_turn": 1, "active": True},
                    "s1": {"id": "s1", "household_id": "h1",
                           "livelihood": "tools", "account_id": "c0",
                           "x": .7, "y": .5, "home_x": .69, "home_y": .5,
                           "founded_turn": 24, "active": True},
                },
                "residents": {
                    "r0": {"resident_id": "r0", "site_id": "s0",
                           "household_id": "h0", "x": .35, "y": .5},
                    "r1": {"resident_id": "r1", "site_id": "s1",
                           "household_id": "h1", "x": .7, "y": .5},
                },
                "clusters": {
                    "c0": {"id": "c0", "centroid_x": .35,
                           "centroid_y": .5, "site_count": 1,
                           "resident_count": 1, "formed_turn": 1,
                           "site_ids": ["s0"], "provisional": False,
                           "accounting_parent_id": "c0"},
                    "c1": {"id": "c1", "centroid_x": .7,
                           "centroid_y": .5, "site_count": 1,
                           "resident_count": 1, "formed_turn": 24,
                           "site_ids": ["s1"], "provisional": True,
                           "accounting_parent_id": "c0"},
                },
                "cluster_lineages": {
                    "c0": {"born_turn": 1, "ended_turn": None},
                    "c1": {"born_turn": 24, "ended_turn": None},
                },
            },
            "spatial_keyframes": [], "spatial_history": {"frames": []},
            "activity_community_ledger": {
                "communities": {"c0": {"name": "母共同体"}}},
            "settlement_states": {},
        }
        script = template.split("<script>", 1)[1].split("</script>", 1)[0]
        script = script.replace(
            "__DATA_JSON__", json.dumps(data, separators=(",", ":")))
        script = script.split(
            "/* ---------------------------------------------------------------------\n"
            "   汎用チャート基盤", 1)[0]
        prefix = r"""
globalThis.document={querySelectorAll(){return[];},body:{dataset:{}},documentElement:{},createElement(){return{width:0,height:0,getContext(){return{fillRect(){},putImageData(){}};}};}};
globalThis.window={addEventListener(){},parent:{postMessage(){}},requestAnimationFrame(){return 1;},cancelAnimationFrame(){}};
globalThis.getComputedStyle=()=>({getPropertyValue(){return '';}});
"""
        suffix = r"""
const field=buildPersistentActivityField(320,470,DATA.residents,24);
const outpost=field.sites.find(site=>site.id==='h1');
const context={fillStyle:'',globalAlpha:1,draws:0,fillRect(){this.draws++;}};
drawActivitySites(context,field.sites,320,470);
console.log(JSON.stringify({
  physical:outpost.clusterId,accounting:outpost.accountingClusterId,
  provisional:outpost.provisional,residentAccounting:activityCommunityForResidentAt('r1',24),
  physicalCount:field.clusterCount,accountingCount:field.accountingClusterCount,
  provisionalCount:field.provisionalClusterCount,siteDraws:context.draws,
}));
"""
        proc = subprocess.run(
            [shutil.which("node")], input=prefix + script + suffix,
            text=True, capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {
            "physical": "c1", "accounting": "c0", "provisional": True,
            "residentAccounting": "c0", "physicalCount": 2,
            "accountingCount": 1, "provisionalCount": 1,
            # 通常点5回 + 前集落点9回。すべて1pxのfillRectである。
            "siteDraws": 14,
        })

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_typed_particle_raster_composites_and_scales_linearly(self):
        template = (Path(__file__).resolve().parent / "dashboard" /
                    "template.html").read_text(encoding="utf-8")
        block = template.split("/* PARTICLE_RASTER_BEGIN", 1)[1]
        block = block.split("*/", 1)[1]
        block = block.split("/* PARTICLE_RASTER_END */", 1)[0]
        script = block + r"""
const width = 640, height = 360, n = 250000;
const start = performance.now();
const raster = ParticleRaster.create(width, height, n);
const colors = [[121,184,106],[101,185,189],[212,163,91],[165,144,203]];
for (let index = 0; index < n; index++) {
  const x = (Math.imul(index, 2654435761) >>> 0) % width;
  const y = (Math.imul(index, 2246822519) >>> 0) % height;
  ParticleRaster.add(raster, index, x, y, colors[index & 3], index === 1);
}
ParticleRaster.finalize(raster);
let total = 0;
for (let index = 0; index < raster.occupiedCount; index++) {
  total += raster.counts[raster.occupied[index]];
}
const exact = ParticleRaster.create(4, 4, 2);
ParticleRaster.add(exact, 0, 1, 1, [100,20,0], false);
ParticleRaster.add(exact, 1, 1, 1, [0,100,20], true);
ParticleRaster.finalize(exact);
const pixel = 5;
console.log(JSON.stringify({
  particles: raster.particleCount,
  total,
  occupied: raster.occupiedCount,
  elapsed_ms: performance.now() - start,
  exact_count: exact.counts[pixel],
  exact_rgb: Array.from(exact.rgba.slice(pixel * 4, pixel * 4 + 3)),
  exact_alpha: exact.rgba[pixel * 4 + 3],
  exact_indices: ParticleRaster.indicesAt(exact, pixel),
  exact_hit: ParticleRaster.closestPixel(exact, 1.2, 1.1, 2),
}));
"""
        proc = subprocess.run(
            [shutil.which("node")], input=script, text=True,
            capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["particles"], 250000)
        self.assertEqual(result["total"], 250000)
        self.assertGreater(result["occupied"], 1)
        # 固い速度競争ではなく、誤って全住民同士を比較するO(N^2)へ戻って
        # いないことを検出する十分に広い上限。
        self.assertLess(result["elapsed_ms"], 5000)
        self.assertEqual(result["exact_count"], 2)
        self.assertEqual(result["exact_rgb"], [56, 67, 11])
        self.assertEqual(result["exact_alpha"], 255)
        self.assertEqual(result["exact_indices"], [1, 0])
        self.assertEqual(result["exact_hit"], 5)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_persistent_named_residents_flow_and_keep_individual_hits(self):
        template = (Path(__file__).resolve().parent / "dashboard" /
                    "template.html").read_text(encoding="utf-8")
        script = template.split("<script>", 1)[1].split("</script>", 1)[0]
        data = {
            "meta": {"aquarium_live": False, "focus_resident_id": "r1",
                     "world_mode": True},
            "turns": [{"t": 1, "focus_resident_id": "r1"}], "final": {},
            "observer_events": [{
                "t": 1, "kind": "resident_born", "resident_id": "r2",
                "parent_ids": ["r1"], "settlement_id": "cluster:000001",
                "household_id": "h1", "household_name": "H", "name": "B",
            }],
            "residents": [
                {"id": "r1", "name": "A", "household_id": "h1",
                 "settlement_id": "cluster:000001", "birth_turn": -20,
                 "died_turn": None, "last_activity_turn": 1,
                 "parent_ids": []},
                {"id": "r2", "name": "B", "household_id": "h1",
                 "settlement_id": "cluster:000001", "birth_turn": 1,
                 "died_turn": None, "last_activity_turn": None,
                 "parent_ids": ["r1"]},
            ],
            "households": [{"id": "h1", "name": "H",
                            "settlement_id": "cluster:000001",
                            "livelihood": "food", "founded_turn": 1}],
            "spatial_state": {
                "updated_turn": 1,
                "sites": {"s1": {
                    "id": "s1", "household_id": "h1",
                    "livelihood": "food", "account_id": "cluster:000001",
                    "x": .62, "y": .5, "home_x": .38, "home_y": .5,
                    "founded_turn": 1, "active": True}},
                "residents": {
                    "r1": {"resident_id": "r1", "site_id": "s1",
                           "household_id": "h1", "x": .38, "y": .5,
                           "target_x": .38, "target_y": .5,
                           "activity": "food", "activity_mode": "primary",
                           "departure_phase": .1, "return_phase": .82},
                    "r2": {"resident_id": "r2", "site_id": "s1",
                           "household_id": "h1", "x": .38, "y": .5,
                           "target_x": .38, "target_y": .5,
                           "activity": "food", "activity_mode": "home",
                           "departure_phase": 0, "return_phase": 1}},
                "clusters": {"cluster:000001": {
                    "id": "cluster:000001", "centroid_x": .5,
                    "centroid_y": .5, "site_count": 1,
                    "resident_count": 2, "formed_turn": 1,
                    "site_ids": ["s1"]}},
                "cluster_lineages": {"cluster:000001": {
                    "born_turn": 1, "ended_turn": None}},
                "cluster_events": [],
            },
            "spatial_keyframes": [],
            "activity_community_ledger": {"communities": {}},
            "settlement_states": {},
        }
        script = script.replace(
            "__DATA_JSON__", json.dumps(data, separators=(",", ":")))
        script = script.split(
            "/* ---------------------------------------------------------------------\n"
            "   汎用チャート基盤", 1)[0]
        prefix = r"""
const fakeContext={fillStyle:'',globalAlpha:1,drawCalls:0,fillRect(){this.drawCalls++;},putImageData(v){this.lastImage=v;},drawImage(){},setTransform(){},strokeRect(){},beginPath(){},moveTo(){},lineTo(){},stroke(){}};
const fakeCanvas=()=>({width:0,height:0,getContext(){return Object.create(fakeContext);}});
globalThis.document={querySelectorAll(){return[];},body:{dataset:{}},documentElement:{},createElement(tag){return tag==='canvas'?fakeCanvas():{};}};
globalThis.window={addEventListener(){},parent:{postMessage(){}},requestAnimationFrame(){return 1;},cancelAnimationFrame(){}};
globalThis.ImageData=class {constructor(data,width,height){this.data=data;this.width=width;this.height=height;}};
globalThis.getComputedStyle=()=>({getPropertyValue(){return '';}});
"""
        suffix = r"""
const scene=buildAquariumParticleScene(320,470,1,'r1');
const dynamicContext=Object.create(fakeContext);
renderDynamicResidents(dynamicContext,scene,0);
const first=particlePoint(scene,0);
const hit=findAquariumHit(first.x,first.y,1);
const beforeGeneration=dynamicContext.drawCalls;
renderGenerationFlows(dynamicContext,scene,0);
const generationDraws=dynamicContext.drawCalls-beforeGeneration;
OBSERVER_EVENTS_BY_TURN.set(2,Array.from({length:200},(_,index)=>({
  t:2,kind:'resident_born',resident_id:'r2',parent_ids:['r1'],sequence:index,
})));
const boundedGenerationFlows=buildGenerationFlows(scene,2).length;
const homeFirst=dynamicParticlePosition(scene,1,0);
renderDynamicResidents(dynamicContext,scene,9);
const second=particlePoint(scene,0);
const homeSecond=dynamicParticlePosition(scene,1,9);
const site=scene.field.sites[0];
console.log(JSON.stringify({particles:scene.particleCount,static_particles:scene.raster.particleCount,occupied:scene.raster.occupiedCount,dynamic:scene.dynamicIndices.length,visible:scene.visibleParticleCount,hit:hit.resident.id,moved:first.x!==second.x||first.y!==second.y,actor_reached_work:Math.abs(second.x-site.workX)<Math.abs(first.x-site.workX),home_stays_home:Math.min(Math.abs(homeFirst.x-site.workX),Math.abs(homeSecond.x-site.workX))>20,generation_flows:scene.generationFlows.length,generation_draws:generationDraws,bounded_generation_flows:boundedGenerationFlows,cached:scene===buildAquariumParticleScene(320,470,1,'r1'),resident_identity:scene.residents[0]===DATA.residents[0],direct_spatial:scene.field.sites.every(site=>site.spatialResidents===DATA.spatial_state.residents&&!('residentPositions' in site))}));
"""
        proc = subprocess.run(
            [shutil.which("node")], input=prefix + script + suffix,
            text=True, capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result, {
            "particles": 2, "static_particles": 0, "occupied": 0,
            "dynamic": 2, "visible": 2, "hit": "r1", "moved": True,
            "actor_reached_work": True, "home_stays_home": True,
            "generation_flows": 1, "generation_draws": 11,
            "bounded_generation_flows": 128,
            "cached": True,
            "resident_identity": True, "direct_spatial": True,
        })

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_spatial_camera_zoom_preserves_anchor_and_culls_offscreen_people(self):
        template = (Path(__file__).resolve().parent / "dashboard" /
                    "template.html").read_text(encoding="utf-8")
        script = template.split("<script>", 1)[1].split("</script>", 1)[0]
        residents = [{
            "id": resident_id, "name": resident_id,
            "household_id": "h1", "settlement_id": "c1",
            "birth_turn": 1, "died_turn": None,
            "last_activity_turn": None, "parent_ids": [],
        } for resident_id in ("left", "center", "right")]
        spatial = {
            "updated_turn": 1,
            "sites": {"s1": {
                "id": "s1", "household_id": "h1", "livelihood": "food",
                "account_id": "c1", "x": .5, "y": .5,
                "home_x": .5, "home_y": .5,
                "founded_turn": 1, "active": True,
            }},
            "residents": {
                "left": {"resident_id": "left", "site_id": "s1",
                         "household_id": "h1", "x": .1, "y": .5},
                "center": {"resident_id": "center", "site_id": "s1",
                           "household_id": "h1", "x": .5, "y": .5},
                "right": {"resident_id": "right", "site_id": "s1",
                          "household_id": "h1", "x": .9, "y": .5},
            },
            "clusters": {"c1": {
                "id": "c1", "centroid_x": .5, "centroid_y": .5,
                "site_count": 1, "resident_count": 3,
                "formed_turn": 1, "site_ids": ["s1"],
            }},
            "cluster_lineages": {"c1": {
                "born_turn": 1, "ended_turn": None}},
            "cluster_events": [],
        }
        packet = particle_packet.build_particle_packet(residents, spatial, 1)
        compact_spatial = copy.deepcopy(spatial)
        compact_spatial["residents"] = {}
        compact_spatial["packed_resident_count"] = 3
        data = {
            "meta": {"aquarium_live": False, "world_mode": True,
                     "focus_resident_id": "center"},
            "turns": [{"t": 1, "total_population": 3}], "final": {},
            "observer_events": [], "residents": residents,
            "households": [{"id": "h1", "name": "H",
                            "settlement_id": "c1", "livelihood": "food",
                            "founded_turn": 1}],
            "particle_frame": packet,
            "particle_cohorts": {
                "version": 1, "turn": 1, "settlement_ids": ["c1"],
                "cohorts": [], "total_population": 3,
                "named_particle_count": 3, "anonymous_particle_count": 0,
            },
            "spatial_state": compact_spatial,
            "spatial_keyframes": [], "spatial_history": {"frames": []},
            "activity_community_ledger": {"communities": {}},
            "settlement_states": {},
        }
        script = script.replace(
            "__DATA_JSON__", json.dumps(data, separators=(",", ":")))
        script = script.split(
            "/* ---------------------------------------------------------------------\n"
            "   汎用チャート基盤", 1)[0]
        prefix = r"""
const posted=[];
const fakeContext={fillStyle:'',globalAlpha:1,fillRect(){},putImageData(){},drawImage(){},setTransform(){},strokeRect(){},beginPath(){},moveTo(){},lineTo(){},stroke(){}};
const fakeCanvas=()=>({width:0,height:0,getContext(){return Object.create(fakeContext);}});
globalThis.document={querySelectorAll(){return[];},body:{dataset:{}},documentElement:{},createElement(tag){return tag==='canvas'?fakeCanvas():{};}};
globalThis.window={addEventListener(){},parent:{postMessage(value){posted.push(value);}},requestAnimationFrame(){return 1;},cancelAnimationFrame(){}};
globalThis.ImageData=class {constructor(data,width,height){this.data=data;this.width=width;this.height=height;}};
globalThis.getComputedStyle=()=>({getPropertyValue(){return '';}});
globalThis.atob ||= value=>Buffer.from(value,'base64').toString('binary');
"""
        suffix = r"""
const width=320,height=470;
const defaultScene=buildAquariumParticleScene(width,height,1,'center');
renderDynamicResidents(Object.create(fakeContext),defaultScene,0);
const defaultCenter=spatialPixelX(.5,width),defaultDelta=spatialPixelX(.6,width)-defaultCenter;
setAquariumCamera({x:.5,y:.5,zoom:4},false);
const zoomScene=buildAquariumParticleScene(width,height,1,'center');
renderDynamicResidents(Object.create(fakeContext),zoomScene,0);
const zoomCenter=spatialPixelX(.5,width),zoomDelta=spatialPixelX(.6,width)-zoomCenter;
const leftIndex=particleIndexByResidentId(zoomScene,'left');
const centerIndex=particleIndexByResidentId(zoomScene,'center');
const before=(()=>{setAquariumCamera({x:.5,y:.5,zoom:1},false);return aquariumWorldAt(80,257,width,height);})();
zoomAquariumAt(80,257,2,width,height,false);
const after=aquariumWorldAt(80,257,width,height);
const panBefore=aquariumCamera.x;
panAquariumBy(20,0,width,height,false);
const panAfter=aquariumCamera.x;
setAquariumCamera({x:.5,y:.5,zoom:3},true);
console.log(JSON.stringify({default_visible:defaultScene.visibleParticleCount,zoom_visible:zoomScene.visibleParticleCount,world_total:zoomScene.particleCount,left_point:particlePoint(zoomScene,leftIndex),center_point:particlePoint(zoomScene,centerIndex),default_center:defaultCenter,zoom_center:zoomCenter,delta_ratio:zoomDelta/defaultDelta,anchor_dx:after.x-before.x,anchor_dy:after.y-before.y,pan_moved:panAfter<panBefore,posted:posted.map(row=>row.type),zoom:aquariumCamera.zoom}));
"""
        proc = subprocess.run(
            [shutil.which("node")], input=prefix + script + suffix,
            text=True, capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["default_visible"], 3)
        self.assertEqual(result["zoom_visible"], 1)
        self.assertEqual(result["world_total"], 3)
        self.assertIsNone(result["left_point"])
        self.assertIsNotNone(result["center_point"])
        self.assertAlmostEqual(result["default_center"], result["zoom_center"])
        self.assertAlmostEqual(result["delta_ratio"], 4.0)
        self.assertAlmostEqual(result["anchor_dx"], 0.0)
        self.assertAlmostEqual(result["anchor_dy"], 0.0)
        self.assertTrue(result["pan_moved"])
        self.assertEqual(result["posted"], ["life-ledger-aquarium-camera"])
        self.assertEqual(result["zoom"], 3)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_compact_particle_packet_drives_scene_without_position_objects(self):
        template = (Path(__file__).resolve().parent / "dashboard" /
                    "template.html").read_text(encoding="utf-8")
        script = template.split("<script>", 1)[1].split("</script>", 1)[0]
        residents = [
            {"id": "r1", "name": "A", "household_id": "h1",
             "settlement_id": "cluster:000001", "birth_turn": -20,
             "died_turn": None, "last_activity_turn": 1,
             "parent_ids": []},
            {"id": "r2", "name": "B", "household_id": "h1",
             "settlement_id": "cluster:000001", "birth_turn": -10,
             "died_turn": None, "last_activity_turn": None,
             "parent_ids": []},
        ]
        full_spatial = {
            "updated_turn": 1,
            "sites": {"s1": {
                "id": "s1", "household_id": "h1",
                "livelihood": "food", "account_id": "cluster:000001",
                "x": .5, "y": .5, "home_x": .5, "home_y": .5,
                "founded_turn": 1, "active": True}},
            "residents": {
                resident_id: {"resident_id": resident_id,
                              "site_id": "s1", "household_id": "h1",
                              "x": .5, "y": .5,
                              "target_x": .5, "target_y": .5}
                for resident_id in ("r1", "r2")},
            "clusters": {"cluster:000001": {
                "id": "cluster:000001", "centroid_x": .5,
                "centroid_y": .5, "site_count": 1,
                "resident_count": 2, "formed_turn": 1,
                "site_ids": ["s1"]}},
            "cluster_lineages": {"cluster:000001": {
                "born_turn": 1, "ended_turn": None}},
            "cluster_events": [],
        }
        packet = particle_packet.build_particle_packet(
            residents, full_spatial, 1)
        compact_spatial = dict(full_spatial)
        compact_spatial["residents"] = {}
        compact_spatial["packed_resident_count"] = packet["count"]
        data = {
            "meta": {"aquarium_live": False, "focus_resident_id": "r1",
                     "world_mode": True},
            "turns": [{"t": 1, "focus_resident_id": "r1"}], "final": {},
            "observer_events": [], "residents": residents,
            "households": [{"id": "h1", "name": "H",
                            "settlement_id": "cluster:000001",
                            "livelihood": "food", "founded_turn": 1}],
            "spatial_state": compact_spatial,
            "particle_frame": packet,
            "spatial_keyframes": [],
            "activity_community_ledger": {"communities": {}},
            "settlement_states": {},
        }
        script = script.replace(
            "__DATA_JSON__", json.dumps(data, separators=(",", ":")))
        script = script.split(
            "/* ---------------------------------------------------------------------\n"
            "   汎用チャート基盤", 1)[0]
        prefix = r"""
const fakeContext={fillStyle:'',globalAlpha:1,fillRect(){},putImageData(v){this.lastImage=v;},drawImage(){},setTransform(){},strokeRect(){},beginPath(){},moveTo(){},lineTo(){},stroke(){}};
const fakeCanvas=()=>({width:0,height:0,getContext(){return Object.create(fakeContext);}});
globalThis.document={querySelectorAll(){return[];},body:{dataset:{}},documentElement:{},createElement(tag){return tag==='canvas'?fakeCanvas():{};}};
globalThis.window={addEventListener(){},parent:{postMessage(){}},requestAnimationFrame(){return 1;},cancelAnimationFrame(){}};
globalThis.ImageData=class {constructor(data,width,height){this.data=data;this.width=width;this.height=height;}};
globalThis.getComputedStyle=()=>({getPropertyValue(){return '';}});
globalThis.atob ||= value=>Buffer.from(value,'base64').toString('binary');
"""
        suffix = r"""
const frame=particleFrame();
const scene=buildAquariumParticleScene(320,470,1,'r1');
renderDynamicResidents(Object.create(fakeContext),scene,0);
const point=particlePoint(scene,0);
const hit=findAquariumHit(point.x,point.y,1);
console.log(JSON.stringify({frame_count:frame.count,particles:scene.particleCount,occupied:scene.raster.occupiedCount,dynamic:scene.dynamicIndices.length,visible:scene.visibleParticleCount,hit:hit.resident.id,packet_scene:scene.packedFrame===frame,no_particle_objects:scene.particleResidents===null,no_position_objects:Object.keys(DATA.spatial_state.residents).length===0,community:activityCommunityForResidentAt('r1',1,0)}));
"""
        proc = subprocess.run(
            [shutil.which("node")], input=prefix + script + suffix,
            text=True, capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {
            "frame_count": 2, "particles": 2, "occupied": 0,
            "dynamic": 2, "visible": 2, "hit": "r1",
            "packet_scene": True,
            "no_particle_objects": True, "no_position_objects": True,
            "community": "cluster:000001",
        })

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_population_cohort_v2_history_uses_last_frame_at_or_before_turn(self):
        template = (Path(__file__).resolve().parent / "dashboard" /
                    "template.html").read_text(encoding="utf-8")
        script = template.split("<script>", 1)[1].split("</script>", 1)[0]
        start = script.index("function populationCohortsAt(turn)")
        end = script.index("function residentSettlementAt", start)
        function_source = script[start:end]
        data = {"particle_cohorts": {
            "version": 2, "turn": 3,
            "settlement_ids": ["a", "b"],
            "cohorts": [[0, 5], [1, 4]],
            "total_population": 12, "named_particle_count": 3,
            "anonymous_particle_count": 9, "source_turn_count": 3,
            "frames": [
                [1, 10, 2, 8, [[0, 8]]],
                [3, 12, 3, 9, [[0, 5], [1, 4]]],
            ],
        }}
        node_source = f"const DATA={json.dumps(data)};\n{function_source}\n" + r"""
const summarize = frame => frame && ({
  turn: frame.turn, total: frame.totalPopulation,
  named: frame.namedPopulation, anonymous: frame.anonymousPopulation,
  cohorts: frame.cohorts.map(row => [row.settlementId, row.count]),
});
const history = [
  summarize(populationCohortsAt(0)), summarize(populationCohortsAt(1)),
  summarize(populationCohortsAt(2)), summarize(populationCohortsAt(3)),
];
DATA.particle_cohorts = {
  version: 1, turn: 4, settlement_ids: ['legacy'],
  cohorts: [[0, 6]], total_population: 8,
  named_particle_count: 2, anonymous_particle_count: 6,
};
console.log(JSON.stringify({history, legacy: summarize(populationCohortsAt(4))}));
"""
        proc = subprocess.run(
            [shutil.which("node")], input=node_source,
            text=True, capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {
            "history": [
                None,
                {"turn": 1, "total": 10, "named": 2, "anonymous": 8,
                 "cohorts": [["a", 8]]},
                {"turn": 1, "total": 10, "named": 2, "anonymous": 8,
                 "cohorts": [["a", 8]]},
                {"turn": 3, "total": 12, "named": 3, "anonymous": 9,
                 "cohorts": [["a", 5], ["b", 4]]},
            ],
            "legacy": {
                "turn": 4, "total": 8, "named": 2, "anonymous": 6,
                "cohorts": [["legacy", 6]],
            },
        })

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_million_anonymous_people_use_bounded_pixels_and_density_flows(self):
        template = (Path(__file__).resolve().parent / "dashboard" /
                    "template.html").read_text(encoding="utf-8")
        script = template.split("<script>", 1)[1].split("</script>", 1)[0]
        data = {
            "meta": {"aquarium_live": False, "world_mode": True,
                     "focus_resident_id": None},
            "turns": [{"t": 1, "total_population": 1_000_000}],
            "final": {}, "observer_events": [], "residents": [],
            "households": [],
            "particle_frame": {
                "version": 1, "encoding": "base64-le", "turn": 1,
                "count": 0, "site_ids": ["sa", "sa2", "sb"],
                "activity_keys": [
                    "food", "medicine", "shelter", "tools", "unknown"],
                "coordinate_scale": 65535, "resident_rows": "",
                "site_indices": "", "coordinates": "",
                "activities": "", "flags": "",
            },
            "particle_cohorts": {
                "version": 2, "turn": 1,
                "settlement_ids": ["a", "b"],
                "cohorts": [[0, 600_000], [1, 400_000]],
                "total_population": 1_000_000,
                "named_particle_count": 0,
                "anonymous_particle_count": 1_000_000,
                "source_turn_count": 1,
                "frames": [[
                    1, 1_000_000, 0, 1_000_000,
                    [[0, 600_000], [1, 400_000]],
                ]],
            },
            "event_density_history": {
                "version": 1, "settlement_ids": ["a", "b"],
                "activity_keys": [
                    "food", "medicine", "shelter", "tools", "unknown"],
                "population": [[1, 0, 2000, 500, 600000, 200000]],
                "migrations": [[1, 0, 1, 9000, 3000]],
                "activities": [[1, 0, 0, 3000, 100]],
            },
            "spatial_state": {
                "updated_turn": 1,
                "sites": {
                    "sa": {"id": "sa", "household_id": "ha",
                           "livelihood": "food", "account_id": "a",
                           "x": .35, "y": .5, "home_x": .32, "home_y": .52,
                           "founded_turn": 1, "active": True,
                           "named_resident_count": 0,
                           "population_weight": 450_000},
                    "sa2": {"id": "sa2", "household_id": "ha2",
                            "livelihood": "medicine", "account_id": "a",
                            "x": .40, "y": .55, "home_x": .38, "home_y": .57,
                            "founded_turn": 1, "active": True,
                            "named_resident_count": 0,
                            "population_weight": 150_000},
                    "sb": {"id": "sb", "household_id": "hb",
                           "livelihood": "tools", "account_id": "b",
                           "x": .65, "y": .5, "home_x": .68, "home_y": .52,
                           "founded_turn": 1, "active": True,
                           "named_resident_count": 0,
                           "population_weight": 400_000},
                },
                "residents": {}, "packed_resident_count": 0,
                "clusters": {
                    "a": {"id": "a", "centroid_x": .35,
                          "centroid_y": .5, "site_count": 2,
                          "resident_count": 600000, "formed_turn": 1,
                          "site_ids": ["sa", "sa2"]},
                    "b": {"id": "b", "centroid_x": .65,
                          "centroid_y": .5, "site_count": 1,
                          "resident_count": 400000, "formed_turn": 1,
                          "site_ids": ["sb"]},
                },
                "cluster_lineages": {
                    "a": {"born_turn": 1, "ended_turn": None},
                    "b": {"born_turn": 1, "ended_turn": None},
                },
                "cluster_events": [],
            },
            "spatial_keyframes": [], "spatial_history": {"frames": []},
            "activity_community_ledger": {"communities": {}},
            "settlement_states": {"a": {"name": "A"}, "b": {"name": "B"}},
        }
        script = script.replace(
            "__DATA_JSON__", json.dumps(data, separators=(",", ":")))
        script = script.split(
            "/* ---------------------------------------------------------------------\n"
            "   汎用チャート基盤", 1)[0]
        prefix = r"""
const fakeContext={fillStyle:'',globalAlpha:1,drawCalls:0,fillRect(){this.drawCalls++;},putImageData(v){this.lastImage=v;},drawImage(){},setTransform(){},strokeRect(){},beginPath(){},moveTo(){},lineTo(){},stroke(){}};
const fakeCanvas=()=>({width:0,height:0,getContext(){return Object.create(fakeContext);}});
globalThis.document={querySelectorAll(){return[];},body:{dataset:{}},documentElement:{},createElement(tag){return tag==='canvas'?fakeCanvas():{};}};
globalThis.window={addEventListener(){},parent:{postMessage(){}},requestAnimationFrame(){return 1;},cancelAnimationFrame(){}};
globalThis.ImageData=class {constructor(data,width,height){this.data=data;this.width=width;this.height=height;}};
globalThis.getComputedStyle=()=>({getPropertyValue(){return '';}});
globalThis.atob ||= value=>Buffer.from(value,'base64').toString('binary');
"""
        suffix = r"""
let randomCalls=0;
Math.random=()=>{randomCalls++;return .5;};
const start=performance.now();
const scene=buildAquariumParticleScene(320,470,1,null);
const sitePatchTotals={};
for(const patch of populationSitePatches(populationCohortsAt(1),scene.field,320,470)) sitePatchTotals[patch.siteIndex]=(sitePatchTotals[patch.siteIndex]||0)+patch.count;
let total=0;
for(let index=0;index<scene.raster.occupiedCount;index++) total+=scene.raster.counts[scene.raster.occupied[index]];
const pixel=scene.raster.occupied[0],x=pixel%320,y=Math.floor(pixel/320);
const hit=findAquariumHit(x,y,1);
const signalContext=Object.create(fakeContext);signalContext.drawCalls=0;
drawFlowParticles(signalContext,scene.flows,scene.field,1.25);
const flowDraws=signalContext.drawCalls;
const boundedContext=Object.create(fakeContext);boundedContext.points=[];
boundedContext.fillRect=function(x,y){this.points.push([x,y]);this.drawCalls++;};
const stressFoodFlows=Array.from({length:64},()=>({
  kind:'intersettlement_trade',from_settlement:'a',to_settlement:'b',
  good:'food',amount:100,
}));
drawFlowParticles(boundedContext,stressFoodFlows,scene.field,1.25);
const foodFlowsBounded=boundedContext.points.length>0&&boundedContext.points.every(
  ([x,y])=>x>=1&&x<=318&&y>=72&&y<=444);
drawPopulationDensitySignals(signalContext,scene.densityEvents,scene.field,1.25);
const densityDraws=signalContext.drawCalls-flowDraws;
const driftBefore=signalContext.drawCalls;
renderAnonymousActivityDrifts(signalContext,scene,1.25);
const driftDraws=signalContext.drawCalls-driftBefore;
const driftFirst=anonymousActivityDriftPoint(
  scene,scene.anonymousActivityDrifts[0],0);
const driftSecond=anonymousActivityDriftPoint(
  scene,scene.anonymousActivityDrifts[0],9);
setAquariumCamera({x:.35,y:.5,zoom:4},false);
const zoomScene=buildAquariumParticleScene(320,470,1,null);
let zoomTotal=0;
for(let index=0;index<zoomScene.raster.occupiedCount;index++) zoomTotal+=zoomScene.raster.counts[zoomScene.raster.occupied[index]];
setAquariumCamera({x:.35,y:.5,zoom:32},false);
const maxZoomScene=buildAquariumParticleScene(320,470,1,null);
let maxZoomTotal=0;
for(let index=0;index<maxZoomScene.raster.occupiedCount;index++) maxZoomTotal+=maxZoomScene.raster.counts[maxZoomScene.raster.occupied[index]];
const maxZoomInitialVisible=maxZoomScene.visibleParticleCount;
const maxMotionRow=maxZoomScene.anonymousDynamicIndividuals[0];
const maxMotionFirst=anonymousIndividualPosition(maxZoomScene,maxMotionRow,0);
const maxMotionSecond=anonymousIndividualPosition(maxZoomScene,maxMotionRow,9);
const maxMotionContext=Object.create(fakeContext);maxMotionContext.drawCalls=0;
renderAnonymousIndividuals(maxMotionContext,maxZoomScene,0);
renderDynamicResidents(maxMotionContext,maxZoomScene,0);
const maxZoomRenderedVisible=maxZoomScene.visibleParticleCount;
aquariumParticleScene=maxZoomScene;
const maxDynamicOnly=maxZoomScene.anonymousDynamicHits.find(
  row=>!maxZoomScene.raster.counts[row.pixel]);
const maxDynamicHit=findAquariumHit(maxDynamicOnly.x,maxDynamicOnly.y,1);
const stablePatchA=populationSitePatches(
  populationCohortsAt(1),maxZoomScene.field,320,470).find(
    row=>row.siteIndex===0&&row.anchor==='work');
const stablePointsA=new Map();
stablePopulationPatchPoints(stablePatchA,populationPatchVisibility(
  stablePatchA,320,470),320,470,point=>stablePointsA.set(
    point.key,[point.x,point.y]));
setAquariumCamera({x:.352,y:.5,zoom:32},false);
const panScene=buildAquariumParticleScene(320,470,1,null);
const stablePatchB=populationSitePatches(
  populationCohortsAt(1),panScene.field,320,470).find(
    row=>row.siteIndex===0&&row.anchor==='work');
let stableShared=0,stableCoordinates=true;
stablePopulationPatchPoints(stablePatchB,populationPatchVisibility(
  stablePatchB,320,470),320,470,point=>{
    const before=stablePointsA.get(point.key);
    if(!before)return;
    stableShared++;
    stableCoordinates&&=before[0]===point.x&&before[1]===point.y;
  });
const exact=ParticleRaster.create(2,2,0,true);
ParticleRaster.addPopulation(exact,0,0,1,1,[100,0,0],600000);
ParticleRaster.addPopulation(exact,1,3,1,1,[0,0,200],400000);
ParticleRaster.finalize(exact);
let legacyMotionCount=0;
for(let index=0;index<100000;index++) {
  if(namedParticleMoves(
      index,100000,{id:`legacy-${index}`},new Map(),null)) {
    legacyMotionCount++;
  }
}
const offset=3*4;
console.log(JSON.stringify({particles:scene.particleCount,named:scene.namedParticleCount,anonymous:scene.anonymousParticleCount,raster_particles:scene.raster.particleCount,total,occupied:scene.raster.occupiedCount,anonymous_pixels:scene.raster.anonymousOccupiedCount,max_anonymous_per_pixel:scene.raster.maxAnonymousPerPixel,patch_count:scene.anonymousPopulationPatchCount,site_patch_totals:sitePatchTotals,max_slots:(320-24)*(470-108),resident_links:scene.raster.next.length,hit_anonymous:hit.anonymous,hit_count:hit.anonymousCount,flow_kind:scene.flows[0].kind,flow_draws:flowDraws,food_flows_bounded:foodFlowsBounded,density_draws:densityDraws,drift_count:scene.anonymousActivityDrifts.length,drift_draws:driftDraws,drift_moved:driftFirst.x!==driftSecond.x||driftFirst.y!==driftSecond.y,legacy_motion_count:legacyMotionCount,frame_intervals:[chooseAquariumAnimationInterval(true,5,false),chooseAquariumAnimationInterval(true,15,false),chooseAquariumAnimationInterval(true,29,false),chooseAquariumAnimationInterval(true,71,false),chooseAquariumAnimationInterval(true,181,false),chooseAquariumAnimationInterval(false,5,false),chooseAquariumAnimationInterval(true,5,true)],zoom_world:zoomScene.particleCount,zoom_visible:zoomScene.visibleParticleCount,zoom_total:zoomTotal,zoom_max_anonymous_per_pixel:zoomScene.raster.maxAnonymousPerPixel,max_zoom_world:maxZoomScene.particleCount,max_zoom_visible:maxZoomInitialVisible,max_zoom_total:maxZoomTotal,max_zoom_anonymous_pixels:maxZoomScene.raster.anonymousOccupiedCount,max_zoom_max_anonymous_per_pixel:maxZoomScene.raster.maxAnonymousPerPixel,max_zoom_stable_mode:maxZoomScene.anonymousStableIndividualMode,max_zoom_dynamic_count:maxZoomScene.anonymousDynamicIndividuals.length,max_zoom_drift_count:maxZoomScene.anonymousActivityDrifts.length,max_zoom_motion_moved:maxMotionFirst.x!==maxMotionSecond.x||maxMotionFirst.y!==maxMotionSecond.y,max_zoom_rendered_visible:maxZoomRenderedVisible,max_zoom_rendered_max_anonymous_per_pixel:maxZoomScene.maxVisibleAnonymousPerPixel,max_zoom_dynamic_hit_anonymous:maxDynamicHit.anonymous,max_zoom_dynamic_hit_moving:maxDynamicHit.moving,pan_stable_mode:panScene.anonymousStableIndividualMode,stable_shared:stableShared,stable_coordinates:stableCoordinates,pan_max_anonymous_per_pixel:panScene.raster.maxAnonymousPerPixel,random_calls:randomCalls,elapsed_ms:performance.now()-start,exact_count:exact.counts[3],exact_anonymous:exact.anonymousCounts[3],exact_rgb:Array.from(exact.rgba.slice(offset,offset+3))}));
"""
        proc = subprocess.run(
            [shutil.which("node")], input=prefix + script + suffix,
            text=True, capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["particles"], 1_000_000)
        self.assertEqual(result["named"], 0)
        self.assertEqual(result["anonymous"], 1_000_000)
        self.assertEqual(result["raster_particles"], 1_000_000)
        self.assertEqual(result["total"], 1_000_000)
        self.assertGreater(result["occupied"], 1)
        self.assertLessEqual(result["occupied"], result["max_slots"])
        self.assertEqual(result["anonymous_pixels"], result["occupied"])
        self.assertGreater(result["max_anonymous_per_pixel"], 1)
        self.assertEqual(result["patch_count"], 6)
        self.assertEqual(result["site_patch_totals"], {
            "0": 450_000, "1": 150_000, "2": 400_000,
        })
        self.assertEqual(result["resident_links"], 0)
        self.assertTrue(result["hit_anonymous"])
        self.assertGreater(result["hit_count"], 0)
        self.assertEqual(result["flow_kind"], "population_density_migration")
        self.assertGreater(result["flow_draws"], 0)
        self.assertTrue(result["food_flows_bounded"])
        self.assertGreater(result["density_draws"], 0)
        self.assertEqual(result["drift_count"], 384)
        self.assertLessEqual(result["drift_draws"], 384)
        self.assertTrue(result["drift_moved"])
        self.assertEqual(result["legacy_motion_count"], 4096)
        self.assertEqual(
            result["frame_intervals"], [33, 50, 100, 250, 500, 250, 250])
        self.assertEqual(result["zoom_world"], 1_000_000)
        self.assertGreater(result["zoom_visible"], 0)
        self.assertLess(result["zoom_visible"], 1_000_000)
        self.assertEqual(result["zoom_total"], result["zoom_visible"])
        self.assertLess(
            result["zoom_max_anonymous_per_pixel"],
            result["max_anonymous_per_pixel"])
        self.assertEqual(result["max_zoom_world"], 1_000_000)
        self.assertGreater(result["max_zoom_visible"], 0)
        self.assertLess(result["max_zoom_visible"], result["zoom_visible"])
        self.assertEqual(
            result["max_zoom_total"] + result["max_zoom_dynamic_count"],
            result["max_zoom_visible"])
        self.assertEqual(
            result["max_zoom_anonymous_pixels"],
            result["max_zoom_total"])
        self.assertEqual(result["max_zoom_max_anonymous_per_pixel"], 1)
        self.assertTrue(result["max_zoom_stable_mode"])
        self.assertEqual(result["max_zoom_dynamic_count"], 4096)
        self.assertEqual(result["max_zoom_drift_count"], 0)
        self.assertTrue(result["max_zoom_motion_moved"])
        self.assertGreater(result["max_zoom_rendered_visible"], 0)
        self.assertTrue(result["max_zoom_dynamic_hit_anonymous"])
        self.assertTrue(result["max_zoom_dynamic_hit_moving"])
        self.assertLessEqual(
            result["max_zoom_rendered_max_anonymous_per_pixel"], 8)
        self.assertTrue(result["pan_stable_mode"])
        self.assertGreater(result["stable_shared"], 100)
        self.assertTrue(result["stable_coordinates"])
        self.assertEqual(result["pan_max_anonymous_per_pixel"], 1)
        self.assertEqual(result["random_calls"], 0)
        self.assertLess(result["elapsed_ms"], 5000)
        self.assertEqual(result["exact_count"], 1_000_000)
        self.assertEqual(result["exact_anonymous"], 1_000_000)
        self.assertEqual(result["exact_rgb"], [96, 0, 128])

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_delta_spatial_history_reconstructs_and_interpolates(self):
        template = (Path(__file__).resolve().parent / "dashboard" /
                    "template.html").read_text(encoding="utf-8")
        script = template.split("<script>", 1)[1].split("</script>", 1)[0]
        frames = [
            {"turn": 1,
             "sites": [["s1", "h1", "food", "c1", .1, .2, .1, .2, 1]],
             "residents": [["r1", "s1", "h1", .1, .2]],
             "clusters": [["c1", .1, .2, 1, 1, 1, [], ["s1"]]]},
            {"turn": 12,
             "sites": [
                 ["s1", "h1", "food", "c1", .3, .4, .1, .2, 1],
                 ["s2", "h2", "tools", "c1", .8, .7, .8, .7, 12]],
             "residents": [
                 ["r1", "s1", "h1", .3, .4],
                 ["r2", "s2", "h2", .8, .7]],
             "clusters": [["c1", .55, .55, 2, 2, 1, [], ["s1", "s2"]]]},
            {"turn": 24,
             "sites": [["s2", "h2", "tools", "c2", .8, .7, .8, .7, 12]],
             "residents": [["r2", "s2", "h2", .8, .7]],
             "clusters": [["c2", .8, .7, 1, 1, 24, ["c1"], ["s2"]]]},
        ]
        data = {
            "meta": {"world_mode": True}, "turns": [{"t": 24}],
            "final": {}, "observer_events": [], "residents": [
                {"id": "r1"}, {"id": "r2"}], "households": [],
            "particle_frame": None,
            "spatial_state": {
                "updated_turn": 24, "sites": {}, "residents": {},
                "clusters": {}, "cluster_lineages": {
                    "c1": {"born_turn": 1, "ended_turn": 24},
                    "c2": {"born_turn": 24, "ended_turn": None}},
                "cluster_events": []},
            "spatial_keyframes": [],
            "spatial_history": spatial_history.build_spatial_history(frames),
            "activity_community_ledger": {"communities": {}},
            "settlement_states": {},
        }
        script = script.replace(
            "__DATA_JSON__", json.dumps(data, separators=(",", ":")))
        script = script.split(
            "/* ---------------------------------------------------------------------\n"
            "   汎用チャート基盤", 1)[0]
        prefix = r"""
globalThis.document={querySelectorAll(){return[];},body:{dataset:{}},documentElement:{}};
globalThis.window={addEventListener(){},parent:{postMessage(){}},requestAnimationFrame(){return 1;},cancelAnimationFrame(){}};
globalThis.getComputedStyle=()=>({getPropertyValue(){return '';}});
"""
        suffix = r"""
const first=decodeSpatialHistoryFrame(0),last=decodeSpatialHistoryFrame(2),middle=persistentSpatialStateAt(6);
for(let index=0;index<6;index++)rememberSpatialHistoryFrame(100+index,{sites:{},residents:{},clusters:{}});
console.log(JSON.stringify({first_residents:Object.keys(first.residents),last_residents:Object.keys(last.residents),last_sites:Object.keys(last.sites),last_clusters:Object.keys(last.clusters),middle_x:middle.residents.r1.x,middle_site_x:middle.sites.s1.x,middle_clusters:Object.keys(middle.clusters),cache_size:SPATIAL_HISTORY_FRAME_CACHE.size,cache_keys:[...SPATIAL_HISTORY_FRAME_CACHE.keys()]}));
"""
        proc = subprocess.run(
            [shutil.which("node")], input=prefix + script + suffix,
            text=True, capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["first_residents"], ["r1"])
        self.assertEqual(result["last_residents"], ["r2"])
        self.assertEqual(result["last_sites"], ["s2"])
        self.assertEqual(result["last_clusters"], ["c2"])
        self.assertAlmostEqual(result["middle_x"], .1 + (.3 - .1) * 5 / 11)
        self.assertAlmostEqual(
            result["middle_site_x"], .1 + (.3 - .1) * 5 / 11)
        self.assertEqual(result["middle_clusters"], ["c1"])
        self.assertEqual(result["cache_size"], 4)
        self.assertEqual(result["cache_keys"], [102, 103, 104, 105])


if __name__ == "__main__":
    unittest.main()
