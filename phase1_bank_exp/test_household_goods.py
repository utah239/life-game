# -*- coding: utf-8 -*-
import copy
import unittest

import game
from dashboard.build_dashboard import build_dashboard_data, build_observer_events
from institutions import household_goods


GOODS = household_goods.HOUSEHOLD_GOODS


def economy(value=100.0):
    return {
        **{good: float(value) for good in GOODS},
        "provisioning_scale": 1.0,
    }


def settlements(**populations):
    return {
        settlement_id: {
            "id": settlement_id,
            "population": population,
            "local_economy": economy(),
        }
        for settlement_id, population in populations.items()}


def registry(rows, households, anonymous=None):
    return {
        "residents": {row["id"]: row for row in rows},
        "households": {
            household_id: {
                "id": household_id,
                "family_name": family_name,
                "settlement_id": settlement_id,
                "active": active,
            }
            for household_id, family_name, settlement_id, active in households},
        "anonymous_population_by_settlement": dict(anonymous or {}),
    }


def resident(resident_id, household_id, settlement_id, *, alive=True,
             parent_ids=(), generation=0):
    return {
        "id": resident_id,
        "household_id": household_id,
        "settlement_id": settlement_id,
        "alive": alive,
        "parent_ids": list(parent_ids),
        "generation": generation,
    }


def needs(communities):
    rows = {}
    for settlement_id, spec in communities.items():
        household_rows = {
            household_id: {
                "household_id": household_id,
                "population": population,
                "demand_quantity_by_good": {
                    good: float(population) for good in GOODS},
            }
            for household_id, population in spec.get("households", {}).items()}
        anonymous_population = int(spec.get("anonymous", 0))
        rows[settlement_id] = {
            "settlement_id": settlement_id,
            "population": sum(spec.get("households", {}).values())
                + anonymous_population,
            "named_population": sum(spec.get("households", {}).values()),
            "anonymous_population": anonymous_population,
            "household_demands": household_rows,
            "anonymous_demand_quantity_by_good": {
                good: float(anonymous_population) for good in GOODS},
        }
    return {"version": 1, "updated_turn": 1, "communities": rows}


def organization_state(community_id=None, amount=0.0):
    organizations = {}
    if community_id is not None:
        organizations["org"] = {
            "id": "org", "active": True,
            "home_activity_cluster_id": community_id,
            "asset_claims": {good: float(amount) for good in GOODS},
        }
    return {"organizations": organizations}


class HouseholdGoodsAccountingTest(unittest.TestCase):
    def setUp(self):
        self.settlements = settlements(a=2)
        self.registry = registry(
            [resident("r1", "h1", "a"), resident("r2", "h1", "a")],
            [("h1", "青木", "a", True)])
        self.needs = needs({"a": {"households": {"h1": 2}}})
        self.organizations = organization_state()

    def reconcile(self, state=None, *, turn=1):
        return household_goods.reconcile_household_goods_state(
            state, self.settlements, self.registry, self.needs,
            self.organizations, turn)

    def test_empty_ledger_treats_all_unclaimed_goods_as_common(self):
        result = self.reconcile()
        state = result["state"]
        self.assertEqual(state["households"]["h1"]["holdings"], {
            good: 0.0 for good in GOODS})
        self.assertEqual(state["common_pool_by_community"]["a"], {
            good: 100.0 for good in GOODS})
        self.assertTrue(household_goods.verify_household_goods_state(
            state, self.settlements, self.registry, self.needs,
            self.organizations))

    def test_organization_and_household_claims_never_double_count(self):
        self.organizations = organization_state("a", 20.0)
        state = self.reconcile()["state"]
        state["households"]["h1"]["holdings"] = {
            good: 90.0 for good in GOODS}
        reconciled = self.reconcile(state)["state"]
        self.assertEqual(reconciled["households"]["h1"]["holdings"], {
            good: 80.0 for good in GOODS})
        self.assertEqual(reconciled["common_pool_by_community"]["a"], {
            good: 0.0 for good in GOODS})
        self.assertEqual(reconciled["world_physical_goods"], {
            good: 100.0 for good in GOODS})

    def test_production_is_acquired_and_consumption_debits_same_holding(self):
        state = self.reconcile()["state"]
        after_settlements = copy.deepcopy(self.settlements)
        after_settlements["a"]["local_economy"]["food"] = 105.0
        plan = {
            "gross_production": {good: (10.0 if good == "food" else 0.0)
                                 for good in GOODS},
            "gross_consumption": {good: (5.0 if good == "food" else 0.0)
                                  for good in GOODS},
            **{f"{good}_after": (105.0 if good == "food" else 100.0)
               for good in GOODS},
        }
        activity = {"communities": {"a": {"activities": {
            "food": {"site_allocations": [{
                "site_id": "site:h1", "gross_output": 10.0,
                "worker_count": 1, "named_worker_count": 1,
                "anonymous_worker_count": 0,
            }]},
        }}}}
        spatial = {"sites": {"site:h1": {
            "id": "site:h1", "household_id": "h1"}}}
        result = household_goods.plan_household_goods_provisioning(
            state, after_settlements, self.registry, self.needs,
            self.organizations, activity, spatial,
            {"a": {good: 100.0 for good in GOODS}}, {"a": plan}, 2)
        final = result["state"]
        self.assertEqual(final["households"]["h1"]["holdings"]["food"], 5.0)
        self.assertEqual(final["common_pool_by_community"]["a"]["food"], 100.0)
        flow = next(row for row in result["events"]
                    if row.get("kind") == "household_goods_flow"
                    and row.get("good") == "food")
        self.assertEqual(flow["produced"], 10.0)
        self.assertEqual(flow["consumed_from_household_holdings"], 5.0)

    def test_consumption_is_not_double_debited_when_all_stock_is_owned(self):
        state = self.reconcile()["state"]
        state["households"]["h1"]["holdings"]["food"] = 10.0
        self.settlements["a"]["local_economy"]["food"] = 10.0
        state = self.reconcile(state, turn=1)["state"]
        after_settlements = copy.deepcopy(self.settlements)
        after_settlements["a"]["local_economy"]["food"] = 7.0
        upkeep = {"a": {
            "food_after": 7.0,
            "gross_production": {"food": 0.0},
            "gross_consumption": {"food": 3.0},
        }}
        result = household_goods.plan_household_goods_provisioning(
            state, after_settlements, self.registry, self.needs,
            self.organizations, {"communities": {}}, {"sites": {}},
            {"a": {"food": 10.0}}, upkeep, 2)
        self.assertEqual(
            result["state"]["households"]["h1"]["holdings"]["food"],
            7.0)
        self.assertEqual(
            result["state"]["common_pool_by_community"]["a"]["food"],
            0.0)

    def test_fast_provisioning_path_matches_finalized_path_after_reconcile(self):
        state = self.reconcile()["state"]
        after_settlements = copy.deepcopy(self.settlements)
        after_settlements["a"]["local_economy"]["food"] = 105.0
        upkeep = {"a": {
            "food_after": 105.0,
            "gross_production": {"food": 10.0},
            "gross_consumption": {"food": 5.0},
        }}
        activity = {"communities": {"a": {"activities": {
            "food": {"site_allocations": [{
                "site_id": "site:h1", "gross_output": 10.0,
                "worker_count": 1, "named_worker_count": 1,
                "anonymous_worker_count": 0,
            }]},
        }}}}
        spatial = {"sites": {"site:h1": {
            "id": "site:h1", "household_id": "h1"}}}
        arguments = (
            state, after_settlements, self.registry, self.needs,
            self.organizations, activity, spatial,
            {"a": {good: 100.0 for good in GOODS}}, upkeep, 2)
        finalized = household_goods.plan_household_goods_provisioning(
            *arguments)
        intermediate = household_goods.plan_household_goods_provisioning(
            *arguments, finalize=False)
        self.assertFalse(household_goods.verify_household_goods_state(
            intermediate["state"], after_settlements, self.registry,
            self.needs, self.organizations))
        reconciled = household_goods.reconcile_household_goods_state(
            intermediate["state"], after_settlements, self.registry,
            self.needs, self.organizations, 2)
        self.assertEqual(reconciled["state"], finalized["state"])

    def test_verifier_rejects_stale_world_aggregate(self):
        state = self.reconcile()["state"]
        state["world_common_pool"]["food"] += 1.0
        self.assertFalse(household_goods.verify_household_goods_state(
            state, self.settlements, self.registry, self.needs,
            self.organizations))

    def test_named_and_anonymous_production_share_one_bounded_ledger(self):
        self.settlements = settlements(a=100)
        self.registry = registry(
            [resident("r1", "h1", "a")],
            [("h1", "青木", "a", True)], {"a": 99})
        self.needs = needs({
            "a": {"households": {"h1": 1}, "anonymous": 99}})
        state = self.reconcile()["state"]
        after_settlements = copy.deepcopy(self.settlements)
        after_settlements["a"]["local_economy"]["food"] = 105.0
        plan = {
            "gross_production": {"food": 10.0},
            "gross_consumption": {"food": 5.0},
            "food_after": 105.0,
        }
        activity = {"communities": {"a": {"activities": {
            "food": {"site_allocations": [{
                "site_id": "site:h1", "gross_output": 10.0,
                "worker_count": 10, "named_worker_count": 1,
                "anonymous_worker_count": 9,
            }]},
        }}}}
        spatial = {"sites": {"site:h1": {
            "id": "site:h1", "household_id": "h1"}}}
        result = household_goods.plan_household_goods_provisioning(
            state, after_settlements, self.registry, self.needs,
            self.organizations, activity, spatial,
            {"a": {good: 100.0 for good in GOODS}}, {"a": plan}, 2)
        final = result["state"]
        self.assertEqual(final["households"]["h1"]["holdings"]["food"], 0.95)
        self.assertEqual(final["anonymous_pools"]["a"]["holdings"]["food"], 4.05)
        self.assertTrue(household_goods.verify_household_goods_state(
            final, after_settlements, self.registry, self.needs,
            self.organizations))

    def test_whole_household_migration_carries_portable_goods_not_shelter(self):
        self.settlements = settlements(a=2, b=1)
        self.settlements["b"]["local_economy"].update(
            {good: 20.0 for good in GOODS})
        self.registry = registry(
            [resident("r1", "h1", "b"), resident("r2", "h1", "b")],
            [("h1", "青木", "b", True)])
        self.needs = needs({
            "a": {"households": {}},
            "b": {"households": {"h1": 2}},
        })
        state = household_goods.initial_household_goods_state()
        state["households"]["h1"] = household_goods._account_record(
            "h1", "a")
        state["households"]["h1"]["holdings"].update({
            "food": 10.0, "medicine": 4.0,
            "shelter": 20.0, "tools": 6.0,
        })
        event = {
            "turn": 2, "kind": "residents_migrated",
            "from_settlement": "a", "to_settlement": "b", "migrants": 2,
            "residents": [
                {"resident_id": "r1", "household_id": "h1"},
                {"resident_id": "r2", "household_id": "h1"},
            ],
        }
        result = household_goods.plan_household_goods_lifecycle(
            state, self.settlements, self.registry, self.needs,
            self.organizations, [event], 2)
        row = result["state"]["households"]["h1"]
        self.assertEqual(row["settlement_id"], "b")
        self.assertEqual(row["holdings"], {
            "food": 10.0, "medicine": 4.0,
            "shelter": 0.0, "tools": 6.0})
        self.assertEqual(
            result["settlements"]["a"]["local_economy"]["food"], 90.0)
        self.assertEqual(
            result["settlements"]["b"]["local_economy"]["food"], 30.0)
        self.assertEqual(
            result["settlements"]["a"]["local_economy"]["shelter"], 100.0)

    def test_partial_household_split_divides_portable_holdings(self):
        self.settlements = settlements(a=1, b=1)
        self.settlements["b"]["local_economy"].update(
            {good: 20.0 for good in GOODS})
        self.registry = registry(
            [resident("r1", "h1", "a"), resident("r2", "h2", "b")],
            [("h1", "青木", "a", True), ("h2", "青木", "b", True)])
        self.needs = needs({
            "a": {"households": {"h1": 1}},
            "b": {"households": {"h2": 1}},
        })
        state = household_goods.initial_household_goods_state()
        state["households"]["h1"] = household_goods._account_record(
            "h1", "a")
        state["households"]["h1"]["holdings"].update({
            "food": 12.0, "shelter": 8.0, "tools": 6.0})
        events = [{
            "turn": 2, "kind": "household_split",
            "from_household_id": "h1", "household_id": "h2",
            "from_settlement": "a", "settlement_id": "b",
        }, {
            "turn": 2, "kind": "residents_migrated",
            "from_settlement": "a", "to_settlement": "b", "migrants": 1,
            "residents": [{"resident_id": "r2", "household_id": "h2"}],
        }]
        result = household_goods.plan_household_goods_lifecycle(
            state, self.settlements, self.registry, self.needs,
            self.organizations, events, 2)
        rows = result["state"]["households"]
        self.assertEqual(rows["h1"]["holdings"]["food"], 6.0)
        self.assertEqual(rows["h2"]["holdings"]["food"], 6.0)
        self.assertEqual(rows["h1"]["holdings"]["tools"], 3.0)
        self.assertEqual(rows["h2"]["holdings"]["tools"], 3.0)
        self.assertEqual(rows["h1"]["holdings"]["shelter"], 8.0)
        self.assertEqual(rows["h2"]["holdings"]["shelter"], 0.0)

    def test_closed_household_is_inherited_by_living_descendant(self):
        self.settlements = settlements(a=1)
        self.registry = registry([
            resident("r1", "h1", "a", alive=False),
            resident("r2", "h2", "a", parent_ids=("r1",), generation=1),
        ], [
            ("h1", "青木", "a", False),
            ("h2", "別姓", "a", True),
        ])
        self.needs = needs({"a": {"households": {"h2": 1}}})
        state = household_goods.initial_household_goods_state()
        state["households"]["h1"] = household_goods._account_record(
            "h1", "a")
        state["households"]["h1"]["holdings"].update({
            "food": 8.0, "medicine": 2.0,
            "shelter": 5.0, "tools": 3.0})
        state["households"]["h2"] = household_goods._account_record(
            "h2", "a")
        result = household_goods.plan_household_goods_lifecycle(
            state, self.settlements, self.registry, self.needs,
            self.organizations, [{
                "turn": 2, "kind": "household_closed",
                "household_id": "h1", "settlement_id": "a",
            }], 2)
        self.assertNotIn("h1", result["state"]["households"])
        self.assertEqual(result["state"]["households"]["h2"]["holdings"], {
            "food": 8.0, "medicine": 2.0,
            "shelter": 5.0, "tools": 3.0})
        event = next(row for row in result["events"]
                     if row.get("kind") == "household_goods_inherited")
        self.assertEqual(event["household_id"], "h2")
        self.assertEqual(event["reason"], "inherited")

    def test_closed_zero_balance_accounts_are_pruned(self):
        state = self.reconcile()["state"]
        for row in self.registry["residents"].values():
            row["alive"] = False
        self.registry["households"]["h1"]["active"] = False
        self.needs = needs({"a": {"anonymous": 1}})
        result = self.reconcile(state, turn=2)
        self.assertNotIn("h1", result["state"]["households"])

    def test_action_gain_is_inner_allocation_not_second_world_stock(self):
        state = self.reconcile()["state"]
        self.settlements["a"]["local_economy"]["food"] = 108.0
        result = household_goods.credit_household_action_goods(
            state, self.settlements, self.registry, self.needs,
            self.organizations, "h1", "a", {"food": 8.0}, 2,
            reason="barter")
        final = result["state"]
        self.assertEqual(final["households"]["h1"]["holdings"]["food"], 8.0)
        self.assertEqual(final["common_pool_by_community"]["a"]["food"], 100.0)
        self.assertEqual(final["world_physical_goods"]["food"], 108.0)
        self.assertTrue(household_goods.verify_household_goods_state(
            final, self.settlements, self.registry, self.needs,
            self.organizations))

    def test_coverage_uses_household_specific_demand(self):
        state = self.reconcile()["state"]
        state["households"]["h1"]["holdings"] = {
            good: 1.0 for good in GOODS}
        coverage = household_goods.household_goods_coverage(
            state, self.needs, "h1")
        self.assertEqual(coverage, {good: 50.0 for good in GOODS})

    def test_inputs_and_random_state_are_not_mutated_or_consumed(self):
        import random
        state = self.reconcile()["state"]
        inputs = copy.deepcopy((
            state, self.settlements, self.registry,
            self.needs, self.organizations))
        random.seed(7)
        before_rng = random.getstate()
        self.reconcile(state, turn=2)
        self.assertEqual(random.getstate(), before_rng)
        self.assertEqual((
            state, self.settlements, self.registry,
            self.needs, self.organizations), inputs)


class HouseholdGoodsWorldIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = game.collect_visualize_trace(
            7, "cautious", 24, 30, "health", world_mode=True,
            include_resume_state=True)

    def test_world_result_checkpoint_trace_and_dashboard_share_ledger(self):
        data = self.data
        state = data["household_goods_state"]
        self.assertEqual(
            data["resume_state"]["household_goods_state"], state)
        self.assertTrue(household_goods.verify_household_goods_state(
            state, data["settlement_states"], data["resident_registry"],
            data["household_needs_state"], data["organization_state"]))
        self.assertTrue(any(
            row["kind"] == "household_goods_flow"
            for row in data["trace"]["household_goods_events"]))
        self.assertTrue(all(
            "turn" in row
            for row in data["trace"]["household_goods_events"]))
        dashboard = build_dashboard_data(data, 12)
        self.assertIn("focus_household_goods", dashboard["turns"][-1])
        self.assertIn("world_common_goods_pool", dashboard["turns"][-1])
        self.assertTrue(dashboard["households"])
        self.assertTrue(all(
            "goods_holdings" in row and "goods_coverage_by_good" in row
            for row in dashboard["households"]))

    def test_world_allocation_parts_equal_physical_goods(self):
        state = self.data["household_goods_state"]
        for good in GOODS:
            parts = sum(float(state[field][good]) for field in (
                "world_household_holdings", "world_anonymous_holdings",
                "world_organization_claims", "world_common_pool"))
            self.assertAlmostEqual(
                parts, float(state["world_physical_goods"][good]), places=5)

    def test_million_person_world_uses_bounded_accounts(self):
        result = game.simulate_policy(
            "cautious", 1, 7, 30, "health", continue_world=True,
            initial_population=1_000_000)
        state = result["household_goods_state"]
        registry = result["resident_registry"]
        self.assertLessEqual(
            len(state["households"]), len(registry["households"]))
        self.assertLessEqual(len(state["households"]), 4096)
        self.assertLessEqual(
            len(state["anonymous_pools"]), len(result["settlements"]))
        represented = sum(
            row["population"] for row in state["anonymous_pools"].values())
        self.assertGreater(represented, 990_000)

    def test_discrete_household_goods_events_enter_observer_stream(self):
        events = build_observer_events(
            [], [], [], None, household_goods_events=[{
                "turn": 3, "kind": "household_goods_migrated",
                "from_settlement": "a", "to_settlement": "b",
                "household_id": "h1", "goods": {"food": 2.0},
            }, {
                "turn": 3, "kind": "household_goods_flow",
                "settlement_id": "a", "good": "food",
            }])
        self.assertEqual([row["kind"] for row in events], [
            "household_goods_migrated"])


if __name__ == "__main__":
    unittest.main()
