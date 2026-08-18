# -*- coding: utf-8 -*-
import copy
import random
import unittest

from institutions import barter
from institutions.intersettlement_trade import (
    PORTABLE_GOODS,
    plan_intersettlement_trade,
    route_trade_capacity,
    settlement_logistics_scale,
    trade_capacity,
)
from institutions.local_credit import LOCAL_CREDIT_STAGE_ISOLATED
from institutions.settlement_network import initial_settlement_network


def network():
    return initial_settlement_network({
        "food": barter.FOOD_STOCK_INITIAL,
        "medicine": barter.MEDICINE_STOCK_INITIAL,
        "shelter": barter.SHELTER_DURABILITY_INITIAL,
        "tools": barter.TOOLS_DURABILITY_INITIAL,
        "production_capacity": barter.PRODUCTION_CAPACITY_INITIAL,
        "barter_stage": barter.BARTER_STAGE_FUNCTIONING,
        "community_trust": 50.0,
        "local_credit_stage": 0,
    })


class IntersettlementTradeTest(unittest.TestCase):
    def test_portable_goods_are_conserved_and_trade_occurs(self):
        before = network()
        totals_before = {
            good: sum(row["local_economy"][good] for row in before.values())
            for good in PORTABLE_GOODS}
        result = plan_intersettlement_trade(before, 7)
        totals_after = {
            good: sum(row["local_economy"][good]
                      for row in result["settlements"].values())
            for good in PORTABLE_GOODS}
        self.assertTrue(result["events"])
        self.assertGreater(result["volume"], 0.0)
        for good in PORTABLE_GOODS:
            self.assertAlmostEqual(totals_before[good], totals_after[good], 6)
        self.assertTrue(all(event["turn"] == 7 for event in result["events"]))

    def test_shelter_and_production_capacity_never_move(self):
        before = network()
        result = plan_intersettlement_trade(before, 1)["settlements"]
        for sid in before:
            self.assertEqual(
                before[sid]["local_economy"]["shelter"],
                result[sid]["local_economy"]["shelter"])
            self.assertEqual(
                before[sid]["local_economy"]["production_capacity"],
                result[sid]["local_economy"]["production_capacity"])

    def test_isolated_settlement_cannot_send_or_receive(self):
        before = network()
        before["upland"]["local_economy"]["local_credit_stage"] = (
            LOCAL_CREDIT_STAGE_ISOLATED)
        result = plan_intersettlement_trade(before, 1)
        self.assertFalse(any(
            event["from_settlement"] == "upland"
            or event["to_settlement"] == "upland"
            for event in result["events"]))
        self.assertEqual(trade_capacity(LOCAL_CREDIT_STAGE_ISOLATED), 0.0)

    def test_successful_trade_builds_both_local_ledgers(self):
        before = network()
        result = plan_intersettlement_trade(before, 1)
        first = result["events"][0]
        source = result["settlements"][first["from_settlement"]]["local_economy"]
        destination = result["settlements"][first["to_settlement"]]["local_economy"]
        self.assertGreater(
            source["community_trust"],
            before[first["from_settlement"]]["local_economy"]["community_trust"])
        self.assertGreater(
            destination["community_trust"],
            before[first["to_settlement"]]["local_economy"]["community_trust"])
        self.assertGreater(source["trade_sent_total"], 0.0)
        self.assertGreater(destination["trade_received_total"], 0.0)

    def test_personal_credit_stage_reduces_route_capacity(self):
        before = network()
        before["riverside"]["population"] = 0
        before["home"]["local_economy"]["food"] = 100.0
        before["upland"]["local_economy"]["food"] = 0.0
        before["upland"]["local_economy"]["local_credit_stage"] = 2
        result = plan_intersettlement_trade(before, 1)
        event = next(
            row for row in result["events"]
            if row["good"] == "food" and row["to_settlement"] == "upland")
        self.assertEqual(event["credit_capacity"], 0.25)
        self.assertEqual(event["route_logistics_scale"], round(
            settlement_logistics_scale(before["upland"]), 12))
        self.assertEqual(event["amount"], event["route_capacity"])

    def test_logistics_scale_is_bottlenecked_by_people_and_infrastructure(self):
        rows = network()
        home = rows["home"]
        baseline = settlement_logistics_scale(home)
        self.assertGreater(baseline, 1.0)

        few_workers = copy.deepcopy(home)
        few_workers["age_cohorts"] = {
            "children": 100, "productive": 10, "elderly": 10}
        few_workers["productive_population"] = 10
        worker_limited = settlement_logistics_scale(few_workers)
        self.assertLess(worker_limited, baseline)

        little_infrastructure = copy.deepcopy(home)
        little_infrastructure["local_economy"]["provisioning_scale"] = 0.1
        self.assertEqual(
            settlement_logistics_scale(little_infrastructure), 0.1)

        no_workers = copy.deepcopy(home)
        no_workers["age_cohorts"] = {
            "children": 100, "productive": 0, "elderly": 20}
        no_workers["productive_population"] = 0
        self.assertEqual(settlement_logistics_scale(no_workers), 0.0)

    def test_route_capacity_scales_with_an_equivalent_larger_world(self):
        small = network()
        small["riverside"]["population"] = 0
        small["home"]["local_economy"]["food"] = 200.0
        small["upland"]["local_economy"]["food"] = 0.0
        large = copy.deepcopy(small)
        factor = 4000
        for row in large.values():
            row["population"] *= factor
            row["reproductive_population"] *= factor
            row["productive_population"] *= factor
            row["age_cohorts"] = {
                key: value * factor
                for key, value in row["age_cohorts"].items()}
            economy = row["local_economy"]
            economy["provisioning_scale"] *= factor
            economy["demand_scales_by_good"] = {
                key: value * factor
                for key, value in economy[
                    "demand_scales_by_good"].items()}
            for good in ("food", "medicine", "shelter", "tools"):
                economy[good] *= factor

        small_event = next(row for row in plan_intersettlement_trade(
            small, 1)["events"] if row["good"] == "food")
        large_event = next(row for row in plan_intersettlement_trade(
            large, 1)["events"] if row["good"] == "food")
        self.assertAlmostEqual(
            large_event["route_logistics_scale"],
            small_event["route_logistics_scale"] * factor, 6)
        self.assertAlmostEqual(
            large_event["route_capacity"],
            small_event["route_capacity"] * factor, delta=0.01)

    def test_route_capacity_observes_both_endpoint_and_credit_limits(self):
        rows = network()
        source = rows["home"]
        destination = rows["upland"]
        details = route_trade_capacity(source, destination, 0.25)
        self.assertEqual(details["source_logistics_scale"],
                         settlement_logistics_scale(source))
        self.assertEqual(details["destination_logistics_scale"],
                         settlement_logistics_scale(destination))
        self.assertEqual(details["route_logistics_scale"], min(
            details["source_logistics_scale"],
            details["destination_logistics_scale"]))
        self.assertEqual(details["route_capacity"], round(
            4.0 * details["route_logistics_scale"] * 0.25, 6))

    def test_input_and_rng_are_unchanged(self):
        before = network()
        original = copy.deepcopy(before)
        random.seed(991)
        rng_before = random.getstate()
        plan_intersettlement_trade(before, 1)
        self.assertEqual(before, original)
        self.assertEqual(random.getstate(), rng_before)

    def test_no_trade_with_one_living_settlement(self):
        before = network()
        before["riverside"]["population"] = 0
        before["upland"]["population"] = 0
        result = plan_intersettlement_trade(before, 1)
        self.assertEqual(result["events"], [])
        self.assertEqual(result["volume"], 0.0)


if __name__ == "__main__":
    unittest.main()
