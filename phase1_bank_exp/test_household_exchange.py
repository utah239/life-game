# -*- coding: utf-8 -*-
import copy
import json
import random
import unittest

import game
from dashboard.build_dashboard import build_dashboard_data
from institutions import (
    barter, household_agency, household_exchange, household_goods,
)


GOODS = household_goods.HOUSEHOLD_GOODS
REFERENCES = {good: barter.goods_reference(good) for good in GOODS}


def _demand(scale=0.01):
    return {good: REFERENCES[good] * scale for good in GOODS}


def _holdings(*, surplus=None, deficit=None, scale=0.01):
    result = _demand(scale)
    if surplus:
        result[surplus] *= 2.0
    if deficit:
        result[deficit] = 0.0
    return result


def _fixture(*, stage=0, pairs=1, anonymous=False):
    state = household_goods.initial_household_goods_state()
    demands = {}
    for index in range(pairs * 2):
        household_id = f"h{index:03d}"
        left = index % 2 == 0
        state["households"][household_id] = {
            "household_id": household_id,
            "settlement_id": "a",
            "holdings": _holdings(
                surplus="food" if left else "medicine",
                deficit="medicine" if left else "food"),
        }
        demands[household_id] = {
            "household_id": household_id,
            "population": 1,
            "demand_quantity_by_good": _demand(),
        }
    anonymous_population = 1 if anonymous else 0
    anonymous_demand = _demand() if anonymous else {
        good: 0.0 for good in GOODS}
    if anonymous:
        state["anonymous_pools"]["a"] = {
            "settlement_id": "a", "population": 1,
            "holdings": _holdings(
                surplus="medicine", deficit="food"),
        }
    state = household_goods.upgrade_household_goods_state(state)
    settlements = {"a": {"id": "a", "local_economy": {
        "barter_stage": stage}}}
    needs = {"version": 1, "updated_turn": 1, "communities": {"a": {
        "settlement_id": "a", "population": pairs * 2 + anonymous_population,
        "named_population": pairs * 2,
        "anonymous_population": anonymous_population,
        "household_demands": demands,
        "anonymous_demand_quantity_by_good": anonymous_demand,
    }}}
    return state, settlements, needs


def _claim_totals(state):
    return {
        good: round(
            sum(row["holdings"][good]
                for row in state["households"].values())
            + sum(row["holdings"][good]
                  for row in state["anonymous_pools"].values()), 6)
        for good in GOODS}


def _agency_fixture():
    """共用分が無く、2世帯の欲求だけが相互に一致する小さな共同体。"""
    state, settlements, needs = _fixture()
    settlements["a"].update({"population": 2})
    settlements["a"]["local_economy"].update({
        "local_credit_stage": 3,
        **_claim_totals(state),
    })
    registry = {
        "residents": {
            "r0": {"id": "r0", "household_id": "h000",
                   "settlement_id": "a", "alive": True},
            "r1": {"id": "r1", "household_id": "h001",
                   "settlement_id": "a", "alive": True},
        },
        "households": {
            "h000": {"id": "h000", "settlement_id": "a",
                     "livelihood": "food", "active": True},
            "h001": {"id": "h001", "settlement_id": "a",
                     "livelihood": "medicine", "active": True},
        },
        "anonymous_population_by_settlement": {"a": 0},
    }
    organizations = {"organizations": {}}
    reconciled = household_goods.reconcile_household_goods_state(
        state, settlements, registry, needs, organizations, 0)["state"]
    return reconciled, settlements, registry, needs, organizations


def _forced_exchange_checkpoint():
    """短いresumeテストで、初回再開時に必ず交換が起きるcheckpointを作る。"""
    checkpoint = game.simulate_policy(
        "cautious", 1, 7, 30, "health", continue_world=True,
    )["resume_state"]
    state = copy.deepcopy(checkpoint)
    for organization in state["organization_state"]["organizations"].values():
        organization["asset_claims"] = {good: 0.0 for good in GOODS}
        organization["asset_claim_total"] = 0.0
    for row in state["household_goods_state"]["households"].values():
        row["holdings"] = {good: 0.0 for good in GOODS}
    for row in state["household_goods_state"]["anonymous_pools"].values():
        row["holdings"] = {good: 0.0 for good in GOODS}

    community = next(iter(
        state["household_needs_state"]["communities"].values()))
    household_ids = list(community["household_demands"])[:2]
    for index, household_id in enumerate(household_ids):
        demand = community["household_demands"][household_id][
            "demand_quantity_by_good"]
        holdings = {good: float(demand[good]) for good in GOODS}
        if index == 0:
            holdings["food"] *= 2.0
            holdings["medicine"] = 0.0
        else:
            holdings["medicine"] *= 2.0
            holdings["food"] = 0.0
        state["household_goods_state"]["households"][household_id][
            "holdings"] = holdings

    for community_id, settlement in state["settlements"].items():
        # 物理総量は元の健全域に保ち、物々交換Stageが不足だけで即停止するのを
        # 避ける。共有分は残るが、地域信用Stage3ではこの時点のアクセスは0。
        settlement["local_economy"]["local_credit_stage"] = 3
        settlement["local_economy"]["barter_stage"] = 0
    state["household_goods_state"] = (
        household_goods.reconcile_household_goods_state(
            state["household_goods_state"], state["settlements"],
            state["resident_registry"], state["household_needs_state"],
            state["organization_state"], 1)["state"])
    # 再開前時点のagencyは有効にしておく。これにより初期昇格ではなく、次の
    # 実ターン末尾に交換が起き、traceにもその月の出来事として記録される。
    pre_exchange = household_agency.plan_household_agency(
        None, state["household_goods_state"], state["settlements"],
        state["resident_registry"], state["household_needs_state"],
        state["organization_state"], 1, barter_active=False)
    state["household_goods_state"] = pre_exchange["household_goods_state"]
    state["household_goods_state"]["household_barter_applied_turn"] = None
    state["household_agency_state"] = pre_exchange["state"]
    state["barter_active"] = True
    state["barter_activated_turn"] = 1
    return json.loads(json.dumps(state))


class HouseholdExchangeTest(unittest.TestCase):
    def test_double_coincidence_moves_equal_normalized_units(self):
        state, settlements, needs = _fixture()
        before = _claim_totals(state)
        result = household_exchange.plan_household_barter_exchange(
            state, settlements, needs, 1, enabled=True)
        after = result["state"]
        route = result["events"][0]["routes_sample"][0]
        self.assertEqual(result["exchange_count"], 1)
        self.assertAlmostEqual(
            route["left_gives_amount"] / REFERENCES["food"],
            route["right_gives_amount"] / REFERENCES["medicine"],
            places=6)
        self.assertEqual(_claim_totals(after), before)
        self.assertGreater(
            after["households"]["h000"]["holdings"]["medicine"], 0.0)
        self.assertGreater(
            after["households"]["h001"]["holdings"]["food"], 0.0)
        self.assertEqual(
            after["world_household_barter_exchange_count"], 1)

    def test_same_direction_surplus_does_not_satisfy_double_coincidence(self):
        state, settlements, needs = _fixture()
        state["households"]["h001"]["holdings"] = _holdings(
            surplus="food", deficit="medicine")
        result = household_exchange.plan_household_barter_exchange(
            state, settlements, needs, 1, enabled=True)
        self.assertEqual(result["exchange_count"], 0)
        self.assertEqual(result["events"], [])

    def test_inactive_and_subsistence_stage_stop_exchange(self):
        state, settlements, needs = _fixture()
        inactive = household_exchange.plan_household_barter_exchange(
            state, settlements, needs, 1, enabled=False)
        settlements["a"]["local_economy"]["barter_stage"] = (
            barter.BARTER_STAGE_SUBSISTENCE_ONLY)
        collapsed = household_exchange.plan_household_barter_exchange(
            state, settlements, needs, 1, enabled=True)
        self.assertEqual(inactive["exchange_count"], 0)
        self.assertEqual(collapsed["exchange_count"], 0)

    def test_thinned_market_has_half_the_monthly_capacity(self):
        functioning = household_exchange.plan_household_barter_exchange(
            *_fixture(stage=barter.BARTER_STAGE_FUNCTIONING), 1,
            enabled=True)
        thinned = household_exchange.plan_household_barter_exchange(
            *_fixture(stage=barter.BARTER_STAGE_THINNED), 1,
            enabled=True)
        self.assertAlmostEqual(
            thinned["normalized_units"],
            functioning["normalized_units"] * 0.5, places=9)

    def test_same_turn_replan_is_idempotent_next_turn_can_continue(self):
        state, settlements, needs = _fixture()
        first = household_exchange.plan_household_barter_exchange(
            state, settlements, needs, 5, enabled=True)
        repeated = household_exchange.plan_household_barter_exchange(
            first["state"], settlements, needs, 5, enabled=True)
        following = household_exchange.plan_household_barter_exchange(
            repeated["state"], settlements, needs, 6, enabled=True)
        self.assertEqual(repeated["state"], first["state"])
        self.assertEqual(repeated["exchange_count"], 0)
        self.assertGreater(following["exchange_count"], 0)
        self.assertEqual(
            following["state"]["world_household_barter_exchange_count"], 2)

    def test_anonymous_pool_can_exchange_without_becoming_individuals(self):
        state, settlements, needs = _fixture(pairs=0, anonymous=True)
        # 匿名側と対になる名前付き世帯を1件だけ追加する。
        state["households"]["h000"] = household_goods.upgrade_household_goods_state({
            **household_goods.initial_household_goods_state(),
            "households": {"h000": {
                "settlement_id": "a",
                "holdings": _holdings(surplus="food", deficit="medicine"),
            }},
        })["households"]["h000"]
        needs["communities"]["a"]["population"] = 2
        needs["communities"]["a"]["named_population"] = 1
        needs["communities"]["a"]["household_demands"]["h000"] = {
            "household_id": "h000", "population": 1,
            "demand_quantity_by_good": _demand(),
        }
        result = household_exchange.plan_household_barter_exchange(
            state, settlements, needs, 1, enabled=True)
        event = result["events"][0]
        self.assertEqual(event["anonymous_exchange_count"], 1)
        self.assertEqual(event["named_participant_count"], 1)
        self.assertTrue(event["routes_sample"][0]["right_anonymous"])
        self.assertEqual(len(result["state"]["anonymous_pools"]), 1)

    def test_large_named_set_is_fully_applied_but_event_sample_is_bounded(self):
        state, settlements, needs = _fixture(pairs=2048)
        result = household_exchange.plan_household_barter_exchange(
            state, settlements, needs, 1, enabled=True)
        event = result["events"][0]
        self.assertEqual(result["exchange_count"], 2048)
        self.assertEqual(event["exchange_count"], 2048)
        self.assertEqual(
            len(event["routes_sample"]),
            household_exchange.HOUSEHOLD_BARTER_ROUTE_SAMPLE_LIMIT)
        self.assertEqual(event["named_participant_count"], 4096)
        self.assertEqual(len(result["state"]["households"]), 4096)
        self.assertEqual(len(result["state"]["anonymous_pools"]), 0)

    def test_inputs_and_rng_are_unchanged(self):
        state, settlements, needs = _fixture(pairs=4)
        inputs = copy.deepcopy((state, settlements, needs))
        random.seed(911)
        rng_before = random.getstate()
        household_exchange.plan_household_barter_exchange(
            state, settlements, needs, 7, enabled=True)
        self.assertEqual((state, settlements, needs), inputs)
        self.assertEqual(random.getstate(), rng_before)

    def test_agency_applies_exchange_before_measuring_household_shortfall(self):
        goods, settlements, registry, needs, organizations = _agency_fixture()
        without_exchange = household_agency.plan_household_agency(
            None, goods, settlements, registry, needs, organizations, 1,
            barter_active=False)
        with_exchange = household_agency.plan_household_agency(
            None, goods, settlements, registry, needs, organizations, 1,
            barter_active=True)

        self.assertLess(
            with_exchange["state"]["world_priority_pressure_by_good"][
                "food"],
            without_exchange["state"]["world_priority_pressure_by_good"][
                "food"])
        self.assertLess(
            with_exchange["state"]["world_priority_pressure_by_good"][
                "medicine"],
            without_exchange["state"]["world_priority_pressure_by_good"][
                "medicine"])
        self.assertEqual(
            _claim_totals(with_exchange["household_goods_state"]),
            _claim_totals(goods))
        self.assertTrue(any(
            event["kind"] == "household_barter_exchange_summary"
            for event in with_exchange["household_goods_events"]))
        self.assertTrue(household_agency.verify_household_agency_state(
            with_exchange["state"], with_exchange["household_goods_state"],
            needs))

    def test_exchange_checkpoint_json_resume_matches_one_shot(self):
        checkpoint = _forced_exchange_checkpoint()
        one_shot = game.simulate_policy(
            "cautious", 3, 7, 30, "health", continue_world=True,
            resume_state=checkpoint)
        first = game.simulate_policy(
            "cautious", 2, 7, 30, "health", continue_world=True,
            resume_state=checkpoint)
        resumed = game.simulate_policy(
            "cautious", 3, 7, 30, "health", continue_world=True,
            resume_state=json.loads(json.dumps(first["resume_state"])))

        self.assertGreater(
            one_shot["household_goods_state"][
                "world_household_barter_exchange_count"], 0)
        self.assertEqual(resumed, one_shot)

    def test_exchange_enters_trace_checkpoint_and_dashboard_projection(self):
        checkpoint = _forced_exchange_checkpoint()
        data = game.collect_visualize_trace(
            7, "cautious", 3, 30, "health", world_mode=True,
            resume_state=checkpoint, include_resume_state=True)
        dashboard = build_dashboard_data(data, 3)
        state = data["household_goods_state"]

        self.assertEqual(
            data["resume_state"]["household_goods_state"], state)
        self.assertTrue(any(
            event["kind"] == "household_barter_exchange_summary"
            for event in data["trace"]["household_goods_events"]))
        self.assertGreater(
            state["world_household_barter_exchange_count"], 0)
        self.assertEqual(
            dashboard["meta"]["world_household_barter_exchange_count"],
            state["world_household_barter_exchange_count"])
        self.assertEqual(
            dashboard["turns"][-1][
                "world_household_barter_volume_by_good"],
            dashboard["meta"]["world_household_barter_volume_by_good"])
        self.assertTrue(any(
            row["kind"] == "household_barter_exchange_summary"
            for row in dashboard["observer_events"]))
        self.assertTrue(all(
            "barter_exchange_count" in row
            and "goods_barter_sent_totals" in row
            and "goods_barter_received_totals" in row
            for row in dashboard["households"]))


if __name__ == "__main__":
    unittest.main()
