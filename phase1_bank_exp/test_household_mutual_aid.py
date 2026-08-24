# -*- coding: utf-8 -*-
"""強い実在関係による有限の世帯間相互扶助。"""
import copy
import json
import random
import unittest

import game
from dashboard import build_dashboard
from institutions import (
    barter,
    household_agency,
    household_goods,
    household_mutual_aid,
    resident_relationships,
)


GOODS = household_goods.HOUSEHOLD_GOODS
REFERENCES = {good: barter.goods_reference(good) for good in GOODS}


def _demand(scale=0.01):
    return {good: REFERENCES[good] * scale for good in GOODS}


def _fixture(*, strength=80.0):
    demand = _demand()
    state = household_goods.initial_household_goods_state()
    state["households"] = {
        "h1": {
            "household_id": "h1", "settlement_id": "a",
            "holdings": {**demand, "food": demand["food"] * 2.0},
        },
        "h2": {
            "household_id": "h2", "settlement_id": "a",
            "holdings": {**demand, "food": 0.0},
        },
    }
    state = household_goods.upgrade_household_goods_state(state)
    needs = {"version": 1, "updated_turn": 1, "communities": {"a": {
        "settlement_id": "a", "population": 2,
        "named_population": 2, "anonymous_population": 0,
        "household_demands": {
            household_id: {
                "household_id": household_id, "population": 1,
                "demand_quantity_by_good": dict(demand),
            }
            for household_id in ("h1", "h2")
        },
        "anonymous_demand_quantity_by_good": {
            good: 0.0 for good in GOODS},
    }}}
    registry = {
        "residents": {
            "r1": {"id": "r1", "household_id": "h1",
                   "settlement_id": "a", "alive": True},
            "r2": {"id": "r2", "household_id": "h2",
                   "settlement_id": "a", "alive": True},
        },
        "households": {
            "h1": {"id": "h1", "settlement_id": "a", "active": True,
                   "livelihood": "food"},
            "h2": {"id": "h2", "settlement_id": "a", "active": True,
                   "livelihood": "medicine"},
        },
        "anonymous_population_by_settlement": {"a": 0},
    }
    relationship_state = _relationship_state(
        registry, [("r1", "r2", strength)])
    return state, needs, registry, relationship_state


def _relationship_state(registry, edges):
    state = resident_relationships.initial_resident_relationship_state(1)
    for left_id, right_id, strength in edges:
        edge_id = resident_relationships.relationship_id(left_id, right_id)
        state["relationships"][edge_id] = {
            "id": edge_id,
            "resident_a_id": min(left_id, right_id),
            "resident_b_id": max(left_id, right_id),
            "formed_turn": 1,
            "updated_turn": 1,
            "last_interaction_turn": 1,
            "settlement_id": "a",
            "strength": float(strength),
            "kinds": [resident_relationships.RELATIONSHIP_KIN],
            "interaction_count": 1,
            "interaction_counts_by_kind": {
                kind: int(kind == resident_relationships.RELATIONSHIP_KIN)
                for kind in resident_relationships.RELATIONSHIP_KINDS},
        }
    living = resident_relationships._living_residents(registry)
    state["communities"], cross = (
        resident_relationships._community_summaries(
            state["relationships"], living))
    state["world_relationship_count"] = len(state["relationships"])
    state["world_cross_community_relationship_count"] = cross
    state["world_relationships_formed_total"] = len(state["relationships"])
    state["world_interactions_by_kind"][
        resident_relationships.RELATIONSHIP_KIN] = len(
            state["relationships"])
    return state


def _claim_totals(state):
    return {
        good: round(sum(
            row["holdings"][good]
            for row in state["households"].values()), 6)
        for good in GOODS}


class HouseholdMutualAidRulesTest(unittest.TestCase):
    def test_strong_real_edge_moves_one_way_and_preserves_goods(self):
        state, needs, registry, relationships = _fixture()
        before = copy.deepcopy((state, needs, registry, relationships))
        before_totals = _claim_totals(state)
        random.seed(817)
        rng_before = random.getstate()

        result = household_mutual_aid.plan_household_mutual_aid(
            state, needs, registry, relationships, 2)
        after = result["state"]
        route = result["events"][0]["routes_sample"][0]

        self.assertEqual(result["transfer_count"], 1)
        self.assertEqual(route["donor_household_id"], "h1")
        self.assertEqual(route["recipient_household_id"], "h2")
        self.assertEqual(route["good"], "food")
        self.assertGreater(route["amount"], 0.0)
        self.assertEqual(_claim_totals(after), before_totals)
        self.assertGreater(after["households"]["h2"]["holdings"]["food"], 0)
        self.assertGreaterEqual(
            after["households"]["h1"]["holdings"]["food"],
            _demand()["food"]
            * household_mutual_aid.MUTUAL_AID_DONOR_RESERVE_RATIO)
        self.assertEqual(
            after["world_household_mutual_aid_transfer_count"], 1)
        self.assertEqual((state, needs, registry, relationships), before)
        self.assertEqual(random.getstate(), rng_before)

    def test_weak_or_cross_community_edge_cannot_aid(self):
        state, needs, registry, weak = _fixture(strength=59.999)
        weak_result = household_mutual_aid.plan_household_mutual_aid(
            state, needs, registry, weak, 2)
        moved_registry = copy.deepcopy(registry)
        moved_registry["residents"]["r2"]["settlement_id"] = "b"
        moved = household_mutual_aid.plan_household_mutual_aid(
            state, needs, moved_registry,
            _relationship_state(moved_registry, [("r1", "r2", 100.0)]), 2)

        self.assertEqual(weak_result["transfer_count"], 0)
        self.assertEqual(moved["transfer_count"], 0)
        self.assertEqual(weak_result["state"][
            "household_mutual_aid_applied_turn"], 2)

    def test_same_turn_is_idempotent_and_next_month_is_newly_bounded(self):
        state, needs, registry, relationships = _fixture(strength=100.0)
        first = household_mutual_aid.plan_household_mutual_aid(
            state, needs, registry, relationships, 2)
        repeated = household_mutual_aid.plan_household_mutual_aid(
            first["state"], needs, registry, relationships, 2)
        following = household_mutual_aid.plan_household_mutual_aid(
            repeated["state"], needs, registry, relationships, 3)

        self.assertEqual(repeated["state"], first["state"])
        self.assertEqual(repeated["transfer_count"], 0)
        self.assertGreater(following["transfer_count"], 0)
        self.assertEqual(following["state"][
            "world_household_mutual_aid_transfer_count"], 2)

    def test_multiple_resident_edges_do_not_multiply_one_household_pair(self):
        state, needs, registry, _ = _fixture()
        registry["residents"].update({
            "r1b": {"id": "r1b", "household_id": "h1",
                    "settlement_id": "a", "alive": True},
            "r2b": {"id": "r2b", "household_id": "h2",
                    "settlement_id": "a", "alive": True},
        })
        relationships = _relationship_state(registry, [
            ("r1", "r2", 80.0), ("r1b", "r2b", 100.0)])
        result = household_mutual_aid.plan_household_mutual_aid(
            state, needs, registry, relationships, 2)
        event = result["events"][0]

        self.assertEqual(event["relationship_count"], 1)
        self.assertEqual(
            event["routes_sample"][0]["relationship_id"],
            resident_relationships.relationship_id("r1b", "r2b"))
        self.assertLessEqual(
            result["normalized_units"],
            0.01 * household_mutual_aid.MUTUAL_AID_MONTHLY_COVERAGE)

    def test_all_relationship_edges_apply_but_visual_route_sample_is_bounded(self):
        demand = _demand()
        state = household_goods.initial_household_goods_state()
        registry = {"residents": {}, "households": {}}
        demand_rows = {}
        edges = []
        for index in range(40):
            donor_id = f"d{index:02d}"
            recipient_id = f"p{index:02d}"
            donor_resident = f"rd{index:02d}"
            recipient_resident = f"rp{index:02d}"
            state["households"][donor_id] = {
                "household_id": donor_id, "settlement_id": "a",
                "holdings": {**demand, "food": demand["food"] * 2.0},
            }
            state["households"][recipient_id] = {
                "household_id": recipient_id, "settlement_id": "a",
                "holdings": {**demand, "food": 0.0},
            }
            for household_id, resident_id in (
                    (donor_id, donor_resident),
                    (recipient_id, recipient_resident)):
                registry["households"][household_id] = {
                    "id": household_id, "settlement_id": "a",
                    "active": True}
                registry["residents"][resident_id] = {
                    "id": resident_id, "household_id": household_id,
                    "settlement_id": "a", "alive": True}
                demand_rows[household_id] = {
                    "household_id": household_id, "population": 1,
                    "demand_quantity_by_good": dict(demand),
                }
            edges.append((donor_resident, recipient_resident, 80.0))
        state = household_goods.upgrade_household_goods_state(state)
        needs = {"version": 1, "updated_turn": 1, "communities": {"a": {
            "settlement_id": "a", "population": 80,
            "named_population": 80, "anonymous_population": 0,
            "household_demands": demand_rows,
            "anonymous_demand_quantity_by_good": {
                good: 0.0 for good in GOODS},
        }}}
        relationships = _relationship_state(registry, edges)

        result = household_mutual_aid.plan_household_mutual_aid(
            state, needs, registry, relationships, 2)

        self.assertEqual(result["transfer_count"], 40)
        self.assertEqual(len(result["relationship_routes"]), 40)
        self.assertEqual(
            len(result["events"][0]["routes_sample"]),
            household_mutual_aid.MUTUAL_AID_ROUTE_SAMPLE_LIMIT)

    def test_anonymous_pool_is_not_individualized_into_relationship_aid(self):
        state, needs, registry, relationships = _fixture()
        state["households"]["h1"]["holdings"]["food"] = _demand()["food"]
        state["anonymous_pools"]["a"] = {
            "settlement_id": "a", "population": 100,
            "holdings": {**_demand(), "food": _demand()["food"] * 100},
        }
        before_anonymous_holdings = copy.deepcopy(
            state["anonymous_pools"]["a"]["holdings"])
        result = household_mutual_aid.plan_household_mutual_aid(
            state, needs, registry, relationships, 2)

        self.assertEqual(result["transfer_count"], 0)
        self.assertEqual(
            result["state"]["anonymous_pools"]["a"]["holdings"],
            before_anonymous_holdings)
        self.assertEqual(len(registry["residents"]), 2)

    def test_v1_goods_and_relationship_checkpoints_upgrade_without_reset(self):
        state, _, registry, relationships = _fixture()
        legacy_goods = copy.deepcopy(state)
        legacy_goods["version"] = 1
        for row in legacy_goods["households"].values():
            row.pop("mutual_aid_given_totals")
            row.pop("mutual_aid_received_totals")
            row.pop("mutual_aid_transfer_count")
        legacy_goods.pop("world_household_mutual_aid_volume_by_good")
        legacy_goods.pop("world_household_mutual_aid_transfer_count")
        legacy_goods.pop("household_mutual_aid_applied_turn")
        legacy_relationships = copy.deepcopy(relationships)
        legacy_relationships["version"] = 1
        legacy_relationships["world_interactions_by_kind"].pop("mutual_aid")
        for row in legacy_relationships["relationships"].values():
            row["interaction_counts_by_kind"].pop("mutual_aid")
        for row in legacy_relationships["communities"].values():
            row["relationship_counts_by_kind"].pop("mutual_aid")

        goods = household_goods.upgrade_household_goods_state(legacy_goods)
        rels = resident_relationships.upgrade_resident_relationship_state(
            legacy_relationships)

        self.assertEqual(goods["version"], 2)
        self.assertEqual(rels["version"], 2)
        self.assertEqual(goods["households"]["h1"][
            "mutual_aid_given_totals"], {good: 0.0 for good in GOODS})
        self.assertEqual(rels["world_interactions_by_kind"]["mutual_aid"], 0)
        self.assertTrue(
            resident_relationships.verify_resident_relationship_state(
                rels, registry))
        self.assertEqual(json.loads(json.dumps(goods)), goods)

    def test_actual_aid_reinforces_only_its_existing_edge_next_month(self):
        state, needs, registry, relationships = _fixture()
        aid = household_mutual_aid.plan_household_mutual_aid(
            state, needs, registry, relationships, 2)
        planned = resident_relationships.plan_resident_relationships(
            relationships, registry, {"organizations": {}}, [], 2,
            mutual_aid_routes=aid["relationship_routes"])
        edge = next(iter(planned["state"]["relationships"].values()))

        self.assertEqual(edge["strength"], 84.0)
        self.assertIn("mutual_aid", edge["kinds"])
        self.assertEqual(
            edge["interaction_counts_by_kind"]["mutual_aid"], 1)
        self.assertEqual(
            planned["events"][0]["interactions_by_kind"]["mutual_aid"], 1)

        forged = resident_relationships.plan_resident_relationships(
            relationships, registry, {"organizations": {}}, [], 2,
            mutual_aid_routes=[{
                "relationship_id": "relationship:unknown|pair",
                "donor_resident_id": "r1",
                "recipient_resident_id": "r2",
            }])
        forged_edge = next(iter(forged["state"]["relationships"].values()))
        self.assertNotIn("mutual_aid", forged_edge["kinds"])

    def test_agency_applies_aid_without_barter_or_local_credit(self):
        state, needs, registry, relationships = _fixture()
        totals = _claim_totals(state)
        settlements = {"a": {
            "id": "a", "population": 2,
            "local_economy": {
                **totals, "local_credit_stage": 3, "barter_stage": 3,
            },
        }}
        organizations = {"organizations": {}}
        state = household_goods.reconcile_household_goods_state(
            state, settlements, registry, needs, organizations, 1)["state"]

        result = household_agency.plan_household_agency(
            None, state, settlements, registry, needs, organizations, 2,
            barter_active=False,
            resident_relationship_state=relationships)

        self.assertTrue(any(
            event["kind"] == "household_mutual_aid_summary"
            for event in result["household_goods_events"]))
        self.assertTrue(result["mutual_aid_routes"])
        self.assertLess(
            result["state"]["households"]["h2"][
                "shortfall_by_good"]["food"], 1.0)
        self.assertTrue(household_goods.verify_household_goods_state(
            result["household_goods_state"], settlements, registry, needs,
            organizations))


class HouseholdMutualAidIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = game.collect_visualize_trace(
            41, "cautious", 24, 30, "health", world_mode=True,
            include_resume_state=True, initial_population=12)
        cls.dashboard = build_dashboard.build_dashboard_data(cls.data, 6)

    def test_aid_enters_checkpoint_trace_relationship_and_dashboard(self):
        goods = self.data["household_goods_state"]
        relationships = self.data["resident_relationship_state"]
        aid_events = [
            row for row in self.data["trace"]["household_goods_events"]
            if row.get("kind") == "household_mutual_aid_summary"]

        self.assertGreater(
            goods["world_household_mutual_aid_transfer_count"], 0)
        self.assertTrue(aid_events)
        self.assertTrue(all(
            len(row["routes_sample"])
            <= household_mutual_aid.MUTUAL_AID_ROUTE_SAMPLE_LIMIT
            for row in aid_events))
        self.assertTrue(all(
            route["donor_household_id"] != route["recipient_household_id"]
            for row in aid_events for route in row["routes_sample"]))
        self.assertEqual(
            self.data["resume_state"]["household_goods_state"], goods)
        self.assertEqual(
            self.data["trace"]["turns"][-1][
                "world_household_mutual_aid_transfer_count"],
            goods["world_household_mutual_aid_transfer_count"])
        self.assertGreater(
            relationships["world_interactions_by_kind"]["mutual_aid"], 0)
        self.assertTrue(any(
            "mutual_aid" in row["kinds"]
            for row in relationships["relationships"].values()))
        self.assertEqual(
            self.dashboard["meta"][
                "world_household_mutual_aid_transfer_count"],
            goods["world_household_mutual_aid_transfer_count"])
        self.assertTrue(any(
            row["kind"] == "household_mutual_aid_summary"
            for row in self.dashboard["observer_events"]))
        self.assertTrue(all(
            "mutual_aid_transfer_count" in row
            and "goods_mutual_aid_given_totals" in row
            and "goods_mutual_aid_received_totals" in row
            for row in self.dashboard["households"]))

    def test_json_checkpoint_resume_matches_one_shot(self):
        checkpoint = self.data["resume_state"]
        one_shot = game.simulate_policy(
            "cautious", 26, 41, 30, "health", continue_world=True,
            resume_state=checkpoint)
        first = game.simulate_policy(
            "cautious", 25, 41, 30, "health", continue_world=True,
            resume_state=checkpoint)
        resumed = game.simulate_policy(
            "cautious", 26, 41, 30, "health", continue_world=True,
            resume_state=json.loads(json.dumps(first["resume_state"])))

        self.assertEqual(resumed["resume_state"], one_shot["resume_state"])
        self.assertEqual(
            resumed["household_goods_state"],
            one_shot["household_goods_state"])
        self.assertEqual(
            resumed["resident_relationship_state"],
            one_shot["resident_relationship_state"])


if __name__ == "__main__":
    unittest.main()
