# -*- coding: utf-8 -*-
"""大人口向け匿名particle cohortの会計境界。"""
import copy
import json
import random
import unittest

from dashboard import particle_cohorts


class ParticleCohortTest(unittest.TestCase):
    def setUp(self):
        self.turns = [{
            "t": 12, "total_population": 9,
            "activity_community_populations": {"a": 6, "b": 3},
        }]
        self.residents = [
            {"id": "r1", "settlement_id": "a", "birth_turn": 1,
             "died_turn": None},
            {"id": "r2", "settlement_id": "a", "birth_turn": 1,
             "died_turn": None},
            {"id": "r3", "settlement_id": "b", "birth_turn": 1,
             "died_turn": None},
            {"id": "dead", "settlement_id": "b", "birth_turn": 1,
             "died_turn": 12},
            {"id": "future", "settlement_id": "b", "birth_turn": 13,
             "died_turn": None},
        ]
        self.spatial = {
            "sites": {
                "sa": {"id": "sa", "account_id": "a", "active": True},
                "sb": {"id": "sb", "account_id": "b", "active": True},
            },
            "residents": {
                "r1": {"site_id": "sa"},
                "r2": {"site_id": "sa"},
                "r3": {"site_id": "sb"},
                "dead": {"site_id": "sb"},
                "future": {"site_id": "sb"},
            },
        }

    def test_deficit_becomes_compact_per_settlement_cohorts(self):
        result = particle_cohorts.build_particle_cohorts(
            self.turns, self.residents, self.spatial, {"count": 3})
        self.assertEqual(result, {
            "version": 2, "turn": 12,
            "settlement_ids": ["a", "b"],
            "cohorts": [[0, 4], [1, 2]],
            "total_population": 9,
            "named_particle_count": 3,
            "anonymous_particle_count": 6,
            "source_turn_count": 1,
            "frames": [[12, 9, 3, 6, [[0, 4], [1, 2]]]],
        })

    def test_exact_named_population_produces_no_anonymous_rows(self):
        turns = [{
            "t": 12,
            "activity_community_populations": {"a": 2, "b": 1},
        }]
        result = particle_cohorts.build_particle_cohorts(
            turns, self.residents, self.spatial, {"count": 3})
        self.assertEqual(result["cohorts"], [])
        self.assertEqual(result["anonymous_particle_count"], 0)
        self.assertEqual(
            result["named_particle_count"], result["total_population"])

    def test_only_packet_eligible_named_people_are_subtracted(self):
        spatial = copy.deepcopy(self.spatial)
        spatial["sites"]["sb"]["active"] = False
        result = particle_cohorts.build_particle_cohorts(
            self.turns, self.residents, spatial, {"count": 2})
        self.assertEqual(result["cohorts"], [[0, 4], [1, 3]])
        self.assertEqual(result["anonymous_particle_count"], 7)

    def test_legacy_total_population_fallback(self):
        result = particle_cohorts.build_particle_cohorts(
            [{"t": 1, "settlement_id": "home", "total_population": 4}],
            [{"id": "r1", "settlement_id": "home",
              "birth_turn": 1, "died_turn": None}], {}, None)
        self.assertEqual(result["settlement_ids"], ["home"])
        self.assertEqual(result["cohorts"], [[0, 3]])

    def test_empty_history(self):
        self.assertEqual(
            particle_cohorts.build_particle_cohorts([], [], {}, None), {
                "version": 2, "turn": None, "settlement_ids": [],
                "cohorts": [], "total_population": 0,
                "named_particle_count": 0, "anonymous_particle_count": 0,
                "source_turn_count": 0, "frames": [],
            })

    def test_packet_count_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "particle packet count"):
            particle_cohorts.build_particle_cohorts(
                self.turns, self.residents, self.spatial, {"count": 2})

    def test_named_overcount_and_unknown_account_are_rejected(self):
        turns = [{"t": 12, "activity_community_populations": {"a": 1}}]
        residents = self.residents[:2]
        spatial = copy.deepcopy(self.spatial)
        with self.assertRaisesRegex(ValueError, "exceeds population"):
            particle_cohorts.build_particle_cohorts(
                turns, residents, spatial, {"count": 2})

        spatial["sites"]["sa"]["account_id"] = "missing"
        with self.assertRaisesRegex(ValueError, "absent from population"):
            particle_cohorts.build_particle_cohorts(
                turns, residents[:1], spatial, {"count": 1})

    def test_history_tracks_birth_death_and_migration_by_compact_frames(self):
        turns = [
            {"t": 1, "activity_community_populations": {"a": 100, "b": 50}},
            {"t": 2, "activity_community_populations": {"a": 100, "b": 50}},
            {"t": 3, "activity_community_populations": {"a": 100, "b": 50}},
        ]
        residents = [
            {"id": "r1", "settlement_id": "b", "birth_turn": 1,
             "died_turn": None},
            {"id": "r2", "settlement_id": "a", "birth_turn": 1,
             "died_turn": 3},
        ]
        resident_events = [{
            "turn": 2, "kind": "residents_migrated",
            "from_settlement": "a", "to_settlement": "b",
            "residents": [{"resident_id": "r1"}],
        }]
        result = particle_cohorts.build_particle_cohorts(
            turns, residents, {}, None, resident_events)
        self.assertEqual(result["settlement_ids"], ["a", "b"])
        self.assertEqual(result["frames"], [
            [1, 150, 2, 148, [[0, 98], [1, 50]]],
            [2, 150, 2, 148, [[0, 99], [1, 49]]],
            [3, 150, 1, 149, [[0, 100], [1, 49]]],
        ])
        self.assertEqual(result["cohorts"], [[0, 100], [1, 49]])

    def test_sparse_observation_applies_intermediate_resident_events(self):
        turns = [
            {"t": 1, "settlement_populations": {"a": 2, "b": 0}},
            {"t": 4, "settlement_populations": {"a": 0, "b": 1}},
        ]
        residents = [
            {"id": "r1", "settlement_id": "b", "birth_turn": 1,
             "died_turn": None},
            {"id": "r2", "settlement_id": "a", "birth_turn": 1,
             "died_turn": 3},
        ]
        events = [{
            "turn": 2, "kind": "residents_migrated",
            "from_settlement": "a", "to_settlement": "b",
            "residents": [{"resident_id": "r1"}],
        }]
        result = particle_cohorts.build_particle_cohorts(
            turns, residents, {}, None, events)
        self.assertEqual(result["frames"], [
            [1, 2, 2, 0, []], [4, 1, 1, 0, []],
        ])

    def test_materialized_resident_is_named_only_from_registration_turn(self):
        result = particle_cohorts.build_particle_cohorts(
            [{"t": 1, "total_population": 10},
             {"t": 2, "total_population": 10}],
            [{"id": "r1", "settlement_id": "home", "birth_turn": -154,
              "registered_turn": 2, "died_turn": None}], {}, None)
        self.assertEqual(result["frames"], [
            [1, 10, 0, 10, [[0, 10]]],
            [2, 10, 1, 9, [[0, 9]]],
        ])

    def test_million_population_long_history_is_run_length_compressed(self):
        turns = [
            {"t": turn, "settlement_id": "home",
             "total_population": 1_000_000}
            for turn in range(1, 1921)]
        result = particle_cohorts.build_particle_cohorts(
            turns, [], {}, None)
        self.assertEqual(result["frames"], [
            [1, 1_000_000, 0, 1_000_000, [[0, 1_000_000]]],
        ])
        self.assertEqual(result["source_turn_count"], 1920)
        self.assertLess(len(json.dumps(result, separators=(",", ":"))), 400)

    def test_turns_must_be_strictly_increasing(self):
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            particle_cohorts.build_particle_cohorts(
                [{"t": 2, "total_population": 0},
                 {"t": 2, "total_population": 0}], [], {}, None)

    def test_inputs_and_rng_are_unchanged(self):
        before = copy.deepcopy((self.turns, self.residents, self.spatial))
        rng_before = random.getstate()
        particle_cohorts.build_particle_cohorts(
            self.turns, self.residents, self.spatial, {"count": 3})
        self.assertEqual((self.turns, self.residents, self.spatial), before)
        self.assertEqual(random.getstate(), rng_before)


if __name__ == "__main__":
    unittest.main()
