# -*- coding: utf-8 -*-
"""デジタル水槽の永続チェックポイントと決定論的再開境界。"""
import copy
import json
import random
import unittest
from unittest import mock

import game
from institutions import activity_communities, spatial


def empty_trace() -> dict:
    return {
        "turns": [],
        "settlements": [],
        "npc_introductions": [],
        "npc_events": [],
        "character_events": [],
        "population_events": [],
        "trade_events": [],
        "resident_events": [],
    }


class WorldResumeEquivalenceTest(unittest.TestCase):
    """長期一括走行とJSON経由の分割走行を同一に保つ。

    数値モデルの較正によって固定turn内に世代交代があるとは限らないため、
    世代イベントが起きた場合の対応数を検証する。世代交代そのものの強制経路は
    test_population.pyの決定論的fixtureが担う。
    """

    @classmethod
    def setUpClass(cls):
        cls.kwargs = {
            "policy_name": "cautious",
            "seed": 1,
            "safety_floor": game.SAFETY_FLOOR,
            "talent": "health",
            "continue_world": True,
        }

        # 世代境界の既存goldenは、空間開拓による新しい分岐と分離する。
        # 開拓を含む再開同値性は下のPioneeringResumeEquivalenceTestで固定する。
        with mock.patch.object(
                spatial, "PIONEERING_FIRST_TURN", 10_000):
            cls.one_trace = empty_trace()
            cls.one_shot = game.simulate_policy(
                turns=1720, trace=cls.one_trace, **cls.kwargs)
            cls.one_shot_rng = random.getstate()

            cls.first_trace = empty_trace()
            cls.first = game.simulate_policy(
                turns=450, trace=cls.first_trace, **cls.kwargs)
            # 実際の永続化と同じJSON往復を必ず通す。random.getstate()のtupleや
            # 整数dict keyがJSONでlist/stringへ変わる境界もここで固定する。
            cls.checkpoint_wire = json.loads(json.dumps(
                cls.first["resume_state"]))
            cls.checkpoint_before_resume = copy.deepcopy(cls.checkpoint_wire)

            cls.second_trace = empty_trace()
            cls.resumed = game.simulate_policy(
                turns=1720, trace=cls.second_trace,
                resume_state=cls.checkpoint_wire, **cls.kwargs)
            cls.resumed_rng = random.getstate()

    def test_result_and_random_state_match_one_shot_exactly(self):
        self.assertEqual(self.resumed, self.one_shot)
        self.assertEqual(self.resumed_rng, self.one_shot_rng)

    def test_every_trace_stream_matches_concatenated_segments(self):
        for name in self.one_trace:
            with self.subTest(stream=name):
                self.assertEqual(
                    self.first_trace[name] + self.second_trace[name],
                    self.one_trace[name])

    def test_resume_preserves_generation_events_when_they_occur(self):
        deaths = [
            row for row in self.second_trace["character_events"]
            if row["kind"] == "character_died"
        ]
        successors = [
            row for row in self.second_trace["character_events"]
            if row["kind"] == "successor_selected"
        ]
        # 最後の焦点人物の死亡と同時に世界が絶滅した場合、選べる後継者は
        # 存在しない。絶滅を失敗扱いせず、その終端1件だけを許す。
        expected_unreplaced = 1 if self.resumed["world_extinct"] else 0
        self.assertEqual(len(deaths), len(successors) + expected_unreplaced)
        self.assertEqual(
            self.resumed["generation"] - self.first["generation"],
            len(successors))

    def test_resume_crosses_lineage_dissolution_and_focus_migration(self):
        active_ids = spatial.accounting_activity_cluster_ids(
            self.resumed["spatial_state"])
        self.assertEqual(
            active_ids,
            set(self.resumed["activity_community_ledger"]["communities"]))
        if self.resumed["world_extinct"]:
            self.assertEqual(active_ids, set())
            self.assertTrue(all(
                row["population"] == 0
                for row in self.resumed["settlements"].values()))
        else:
            self.assertIn(self.resumed["focus_settlement_id"], active_ids)
        self.assertEqual(
            self.resumed["activity_community_accounting_version"],
            activity_communities.ACTIVITY_COMMUNITY_ACCOUNTING_VERSION)
        self.assertTrue(any(
            event["kind"] == "cluster_dissolved"
            for event in self.resumed["spatial_state"]["cluster_events"]))
        self.assertGreater(self.resumed["migration_total"], 0)
        self.assertTrue(any(
            row["kind"] == "population_migrated"
            for row in self.second_trace["population_events"]))

    def test_checkpoint_is_json_serializable_and_not_mutated_by_resume(self):
        json.dumps(self.first["resume_state"])
        self.assertEqual(self.checkpoint_wire, self.checkpoint_before_resume)
        self.assertEqual(self.first["resume_state"]["completed_turn"], 450)
        self.assertEqual(
            self.resumed["resume_state"]["completed_turn"],
            self.one_shot["resume_state"]["completed_turn"])
        self.assertTrue(
            self.resumed["resume_state"]["completed_turn"] == 1720
            or self.resumed["world_extinct"])

    def test_checkpoint_is_detached_from_public_result(self):
        result = game.simulate_policy(
            turns=12, **self.kwargs)
        checkpoint_money = result["resume_state"]["resources"]["money"]
        result["resources"]["money"] += 999
        self.assertEqual(
            result["resume_state"]["resources"]["money"], checkpoint_money)

    def test_resume_rejects_incompatible_or_terminal_worlds(self):
        checkpoint = self.first["resume_state"]
        calls = [
            dict(turns=451, continue_world=False),
            dict(turns=451, continue_world=True, policy_name="family"),
            dict(turns=450, continue_world=True),
        ]
        for overrides in calls:
            kwargs = dict(self.kwargs)
            kwargs.update(overrides)
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                game.simulate_policy(resume_state=checkpoint, **kwargs)

        extinct = copy.deepcopy(checkpoint)
        extinct["world_extinct_turn"] = extinct["completed_turn"]
        with self.assertRaises(ValueError):
            game.simulate_policy(
                turns=451, resume_state=extinct, **self.kwargs)


class PioneeringResumeEquivalenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        kwargs = {
            "policy_name": "cautious", "seed": 1,
            "safety_floor": game.SAFETY_FLOOR, "talent": "health",
            "continue_world": True,
        }
        cls.one_trace = empty_trace()
        cls.one = game.simulate_policy(
            turns=300, trace=cls.one_trace, **kwargs)
        cls.one_rng = random.getstate()
        first_trace = empty_trace()
        first = game.simulate_policy(
            turns=150, trace=first_trace, **kwargs)
        wire = json.loads(json.dumps(first["resume_state"]))
        cls.second_trace = empty_trace()
        cls.resumed = game.simulate_policy(
            turns=300, trace=cls.second_trace,
            resume_state=wire, **kwargs)
        cls.resumed_rng = random.getstate()
        cls.combined_trace = {
            key: first_trace[key] + cls.second_trace[key]
            for key in first_trace}

    def test_pioneering_checkpoint_matches_one_shot_result_rng_and_trace(self):
        self.assertEqual(self.resumed, self.one)
        self.assertEqual(self.resumed_rng, self.one_rng)
        self.assertEqual(self.combined_trace, self.one_trace)

    def test_split_crosses_pioneering_outpost_lifecycle(self):
        events = self.one["spatial_state"]["cluster_events"]
        started = [event for event in events
                   if event["kind"] == "pioneering_started"]
        settled = [event for event in events
                   if event["kind"] == "pioneering_settled"]
        self.assertTrue(started)
        self.assertEqual(len(started), len(settled))
        self.assertEqual(
            set(self.one["activity_community_ledger"]["communities"]),
            spatial.accounting_activity_cluster_ids(
                self.one["spatial_state"]))


if __name__ == "__main__":
    unittest.main()
