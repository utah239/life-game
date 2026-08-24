# -*- coding: utf-8 -*-
"""名前付き住民のboundedな関係台帳と因果接続。"""
import copy
import json
import random
import unittest
from unittest import mock

import game
from dashboard import build_dashboard
from institutions import resident_relationships


def _registry():
    households = {
        f"h{index}": {
            "id": f"h{index}", "settlement_id": "a", "active": True}
        for index in range(1, 4)}
    residents = {}
    for index in range(1, 7):
        household_id = f"h{(index + 1) // 2}"
        residents[f"r{index}"] = {
            "id": f"r{index}", "household_id": household_id,
            "settlement_id": "a", "alive": True}
    return {"households": households, "residents": residents}


def _organization():
    return {"organizations": {"org:a": {
        "id": "org:a", "kind": "company", "active": True,
        "named_worker_ids": ["r1", "r3"],
        "named_member_ids": ["r1", "r2", "r3", "r4"],
    }}}


def _barter_routes():
    return [{
        "left_household_id": "h1", "right_household_id": "h2",
        "left_anonymous": False, "right_anonymous": False,
    }]


class ResidentRelationshipRulesTest(unittest.TestCase):
    def test_real_kin_work_and_barter_contacts_form_bounded_edges(self):
        registry = _registry()
        organization = _organization()
        routes = _barter_routes()
        before = copy.deepcopy((registry, organization, routes))
        random.seed(730)
        rng_before = random.getstate()

        result = resident_relationships.plan_resident_relationships(
            None, registry, organization, routes, 1)
        state = result["state"]

        self.assertEqual(state["world_relationship_count"], 5)
        self.assertEqual(
            state["communities"]["a"]["relationship_counts_by_kind"],
            {"kin": 3, "organization": 1, "barter": 1})
        self.assertEqual(
            state["world_interactions_by_kind"],
            {"kin": 3, "organization": 1, "barter": 1})
        self.assertEqual(result["events"][0]["formed_count"], 5)
        self.assertTrue(
            resident_relationships.verify_resident_relationship_state(
                state, registry))
        self.assertEqual((registry, organization, routes), before)
        self.assertEqual(random.getstate(), rng_before)

    def test_same_turn_is_idempotent_and_next_turn_decays_idle_nonkin(self):
        first = resident_relationships.plan_resident_relationships(
            None, _registry(), _organization(), _barter_routes(), 1)
        repeated = resident_relationships.plan_resident_relationships(
            first["state"], _registry(), _organization(), _barter_routes(), 1)
        following = resident_relationships.plan_resident_relationships(
            repeated["state"], _registry(), {"organizations": {}}, [], 2)
        self.assertEqual(repeated["state"], first["state"])
        self.assertEqual(repeated["events"], [])
        nonkin = [
            row for row in following["state"]["relationships"].values()
            if "kin" not in row["kinds"]]
        self.assertEqual(
            sorted(row["strength"] for row in nonkin), [24.5, 34.5])
        kin = [
            row for row in following["state"]["relationships"].values()
            if "kin" in row["kinds"]]
        self.assertTrue(all(row["strength"] >= 80.0 for row in kin))

    def test_kin_survives_household_split_and_migration_death_ends_edges(self):
        first = resident_relationships.plan_resident_relationships(
            None, _registry(), {"organizations": {}}, [], 1)["state"]
        registry = _registry()
        registry["households"]["h4"] = {
            "id": "h4", "settlement_id": "b", "active": True}
        registry["residents"]["r2"]["household_id"] = "h4"
        registry["residents"]["r2"]["settlement_id"] = "b"
        moved = resident_relationships.plan_resident_relationships(
            first, registry, {"organizations": {}}, [], 2)
        edge = moved["state"]["relationships"][
            resident_relationships.relationship_id("r1", "r2")]
        self.assertIn("kin", edge["kinds"])
        self.assertIsNone(edge["settlement_id"])
        self.assertEqual(
            moved["state"]["world_cross_community_relationship_count"], 1)

        registry["residents"]["r1"]["alive"] = False
        ended = resident_relationships.plan_resident_relationships(
            moved["state"], registry, {"organizations": {}}, [], 3)
        self.assertNotIn(edge["id"], ended["state"]["relationships"])
        self.assertTrue(any(
            row["reason"] == "resident_died"
            for row in ended["events"][0]["ended_relationships"]))

    def test_idle_weak_relationship_fades(self):
        registry = _registry()
        state = resident_relationships.initial_resident_relationship_state()
        edge = {
            "id": resident_relationships.relationship_id("r1", "r3"),
            "resident_a_id": "r1", "resident_b_id": "r3",
            "formed_turn": 0, "updated_turn": 0,
            "last_interaction_turn": 0, "settlement_id": "a",
            "strength": resident_relationships.RELATIONSHIP_FADE_THRESHOLD,
            "kinds": ["organization"], "interaction_count": 1,
            "interaction_counts_by_kind": {
                "kin": 0, "organization": 1, "barter": 0},
        }
        state["relationships"][edge["id"]] = edge
        state["communities"] = {"a": {
            "settlement_id": "a", "relationship_count": 1,
            "cross_household_relationship_count": 1,
            "relationship_counts_by_kind": {
                "kin": 0, "organization": 1, "barter": 0},
            "average_strength": resident_relationships.RELATIONSHIP_FADE_THRESHOLD,
        }}
        state["world_relationship_count"] = 1
        result = resident_relationships.plan_resident_relationships(
            state, registry, {"organizations": {}}, [], 1)
        self.assertEqual(result["state"]["world_relationship_count"], 3)
        self.assertNotIn(edge["id"], result["state"]["relationships"])
        self.assertEqual(result["events"][0]["ended_count"], 1)

    def test_nonkin_degree_and_world_size_are_bounded(self):
        registry = {"households": {}, "residents": {}}
        registry["households"]["center"] = {
            "id": "center", "settlement_id": "a", "active": True}
        registry["residents"]["center"] = {
            "id": "center", "household_id": "center",
            "settlement_id": "a", "alive": True}
        routes = []
        for index in range(20):
            household_id = f"h{index:02d}"
            resident_id = f"r{index:02d}"
            registry["households"][household_id] = {
                "id": household_id, "settlement_id": "a", "active": True}
            registry["residents"][resident_id] = {
                "id": resident_id, "household_id": household_id,
                "settlement_id": "a", "alive": True}
            routes.append({
                "left_household_id": "center",
                "right_household_id": household_id})
        result = resident_relationships.plan_resident_relationships(
            None, registry, {"organizations": {}}, routes, 1)
        degree = sum(
            "center" in (row["resident_a_id"], row["resident_b_id"])
            for row in result["state"]["relationships"].values())
        self.assertEqual(
            degree,
            resident_relationships.MAX_NON_KIN_RELATIONSHIPS_PER_RESIDENT)
        self.assertEqual(result["events"][0]["suppressed_count"], 12)
        self.assertLessEqual(
            result["state"]["world_relationship_count"],
            resident_relationships.MAX_RESIDENT_RELATIONSHIPS)

    def test_support_requires_a_living_cross_household_local_edge(self):
        state = resident_relationships.plan_resident_relationships(
            None, _registry(), _organization(), _barter_routes(), 1)["state"]
        support = resident_relationships.household_relationship_support(
            state, _registry())
        self.assertEqual(support, {"h1": 0.35, "h2": 0.35})
        moved = _registry()
        moved["residents"]["r4"]["settlement_id"] = "b"
        moved_support = resident_relationships.household_relationship_support(
            state, moved)
        self.assertEqual(moved_support["h1"], 0.25)
        self.assertEqual(moved_support["h2"], 0.25)

    def test_new_kin_evicts_weak_nonkin_at_world_cap(self):
        registry = _registry()
        state = resident_relationships.plan_resident_relationships(
            None, registry, _organization(), [], 1)[
                "state"]
        nonkin = next(row for row in state["relationships"].values()
                      if "kin" not in row["kinds"])
        state["relationships"] = {nonkin["id"]: nonkin}
        state["communities"], cross = (
            resident_relationships._community_summaries(
                state["relationships"], resident_relationships._living_residents(
                    registry)))
        state["world_relationship_count"] = 1
        state["world_cross_community_relationship_count"] = cross
        with mock.patch.object(
                resident_relationships, "MAX_RESIDENT_RELATIONSHIPS", 1):
            result = resident_relationships.plan_resident_relationships(
                state, registry, {"organizations": {}}, [], 2)

        edge = next(iter(result["state"]["relationships"].values()))
        self.assertEqual(edge["kinds"], ["kin"])
        self.assertEqual(result["events"][0]["ended_relationships"][0][
            "reason"], "capacity_for_kin")

    def test_json_round_trip_preserves_state(self):
        state = resident_relationships.plan_resident_relationships(
            None, _registry(), _organization(), _barter_routes(), 1)["state"]
        self.assertEqual(json.loads(json.dumps(state)), state)


class ResidentRelationshipIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = game.collect_visualize_trace(
            41, "cautious", 3, 30, "health", world_mode=True,
            include_resume_state=True, initial_population=12)
        cls.dashboard = build_dashboard.build_dashboard_data(cls.data, 3)

    def test_state_enters_turn_trace_checkpoint_and_dashboard(self):
        state = self.data["resident_relationship_state"]
        registry = self.data["resident_registry"]
        self.assertGreater(state["world_relationship_count"], 0)
        self.assertTrue(
            resident_relationships.verify_resident_relationship_state(
                state, registry))
        self.assertEqual(
            self.data["resume_state"]["resident_relationship_state"], state)
        self.assertEqual(
            self.data["trace"]["turns"][-1][
                "world_resident_relationship_count"],
            state["world_relationship_count"])
        self.assertTrue(self.data["trace"][
            "resident_relationship_events"])
        self.assertEqual(
            self.dashboard["meta"]["resident_relationship_count"],
            state["world_relationship_count"])
        resident_ids = {row["id"] for row in self.dashboard["residents"]}
        self.assertTrue(self.dashboard["resident_relationships"])
        self.assertTrue(all(
            edge["a"] in resident_ids and edge["b"] in resident_ids
            for edge in self.dashboard["resident_relationships"]))
        self.assertTrue(any(
            event["kind"] == "resident_relationship_summary"
            for event in self.dashboard["observer_events"]))

    def test_json_checkpoint_resume_matches_one_shot(self):
        checkpoint = self.data["resume_state"]
        one_shot = game.simulate_policy(
            "cautious", 5, 41, 30, "health", continue_world=True,
            resume_state=checkpoint)
        first = game.simulate_policy(
            "cautious", 4, 41, 30, "health", continue_world=True,
            resume_state=checkpoint)
        resumed = game.simulate_policy(
            "cautious", 5, 41, 30, "health", continue_world=True,
            resume_state=json.loads(json.dumps(first["resume_state"])))
        self.assertEqual(
            resumed["resident_relationship_state"],
            one_shot["resident_relationship_state"])
        self.assertEqual(resumed["resume_state"], one_shot["resume_state"])


if __name__ == "__main__":
    unittest.main()
