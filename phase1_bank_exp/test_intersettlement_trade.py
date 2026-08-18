# -*- coding: utf-8 -*-
import copy
import random
import unittest

from institutions import barter
from institutions.intersettlement_trade import (
    PORTABLE_GOODS,
    plan_intersettlement_trade,
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
        self.assertEqual(event["amount"], 1.0)

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
