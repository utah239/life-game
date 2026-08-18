# -*- coding: utf-8 -*-
"""名前付きNPCの寿命・死亡・水槽表示の境界。"""
import copy
import random
import unittest

import game
from dashboard import build_dashboard
from institutions import npc_population


def empty_trace() -> dict:
    return {
        "turns": [], "settlements": [], "npc_introductions": [],
        "npc_events": [], "character_events": [], "population_events": [],
    }


class NpcPopulationRulesTest(unittest.TestCase):
    def test_schedule_is_stable_bounded_and_does_not_use_global_rng(self):
        before = random.getstate()
        first = npc_population.npc_life_schedule(7, "npc42", 120)
        second = npc_population.npc_life_schedule(7, "npc42", 120)
        self.assertEqual(first, second)
        self.assertEqual(random.getstate(), before)
        self.assertTrue(first["alive"])
        self.assertGreater(first["death_turn"], 120)
        self.assertGreaterEqual(
            first["introduction_age_years"],
            npc_population.NPC_INTRODUCTION_AGE_RANGE[0])
        self.assertLess(
            first["introduction_age_years"],
            npc_population.NPC_INTRODUCTION_AGE_RANGE[1] + 1)
        self.assertGreater(first["death_age_years"], first["introduction_age_years"])
        self.assertLess(
            first["death_age_years"], npc_population.NPC_LIFESPAN_RANGE[1] + 1)

    def test_identity_changes_schedule_without_python_hash_randomization(self):
        a = npc_population.npc_life_schedule(7, "npc1", 20)
        b = npc_population.npc_life_schedule(7, "npc2", 20)
        self.assertNotEqual(
            (a["birth_turn"], a["death_turn"]),
            (b["birth_turn"], b["death_turn"]))

    def test_due_and_mark_died_are_non_mutating(self):
        npc = npc_population.npc_life_schedule(1, "npc1", 1)
        before = copy.deepcopy(npc)
        self.assertFalse(npc_population.npc_due_to_die(npc, npc["death_turn"] - 1))
        self.assertTrue(npc_population.npc_due_to_die(npc, npc["death_turn"]))
        dead = npc_population.mark_npc_died(npc, npc["death_turn"])
        self.assertEqual(npc, before)
        self.assertFalse(dead["alive"])
        self.assertEqual(dead["died_turn"], npc["death_turn"])
        self.assertFalse(npc_population.npc_due_to_die(dead, npc["death_turn"] + 1))


class NpcLifecycleWorldTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.trace = empty_trace()
        cls.result = game.simulate_policy(
            "cautious", 1720, 1, game.SAFETY_FLOOR, "health",
            trace=cls.trace, continue_world=True)

    def test_long_world_has_named_npc_deaths(self):
        deaths = [n for n in self.result["npcs"].values()
                  if not n.get("alive", True)]
        self.assertGreater(len(deaths), 0)
        self.assertEqual(self.result["npc_death_count"], len(deaths))
        self.assertEqual(len(self.trace["npc_events"]), len(deaths))
        self.assertTrue(all(e["kind"] == "npc_died" for e in self.trace["npc_events"]))
        self.assertTrue(all(e["age"] >= 1 for e in self.trace["npc_events"]))

    def test_npc_death_orphans_debt_without_population_double_count(self):
        first = game.simulate_policy(
            "cautious", 12, 9, game.SAFETY_FLOOR, "health",
            continue_world=True)
        self.assertTrue(first["resume_state"]["npcs"])
        dying = copy.deepcopy(first["resume_state"])
        control = copy.deepcopy(first["resume_state"])
        npc_id = next(iter(dying["npcs"]))
        dying["npcs"][npc_id]["death_turn"] = 13
        control["npcs"][npc_id]["death_turn"] = 9999
        for state in (dying, control):
            state["contract_sequence"] += 1
            state["contracts"].append({
                "id": f"c{state['contract_sequence']}", "status": "open",
                "due_turn": 1000, "repay_money": -10,
                "counterparty": npc_id,
            })

        death_trace, control_trace = empty_trace(), empty_trace()
        dead_run = game.simulate_policy(
            "cautious", 13, 9, game.SAFETY_FLOOR, "health",
            continue_world=True, resume_state=dying, trace=death_trace)
        control_run = game.simulate_policy(
            "cautious", 13, 9, game.SAFETY_FLOOR, "health",
            continue_world=True, resume_state=control, trace=control_trace)
        contract = next(c for c in dead_run["contracts"]
                        if c["id"] == f"c{dying['contract_sequence']}")
        self.assertEqual(contract["status"], "orphaned")
        self.assertEqual(contract["orphaned_reason"], "counterparty_died")
        self.assertEqual(len(death_trace["npc_events"]), 1)
        self.assertEqual(
            {key: row["population"]
             for key, row in dead_run["settlements"].items()},
            {key: row["population"]
             for key, row in control_run["settlements"].items()})
        self.assertEqual(
            sum(row.get("alive", True) for row in dead_run[
                "resident_registry"]["residents"].values()),
            sum(row.get("alive", True) for row in control_run[
                "resident_registry"]["residents"].values()))

    def test_dead_npc_is_not_a_relationship_candidate(self):
        state = {"npcs": {
            "alive": {"role": "acquaintance", "alive": True},
            "dead": {"role": "acquaintance", "alive": False},
            "bank": {"role": "money_issuer", "alive": True},
        }}
        self.assertEqual(set(game.acquaintance_npcs(state)), {"alive"})


class NpcLifecycleDashboardTest(unittest.TestCase):
    def test_dashboard_contains_npc_death_state_and_event(self):
        data = {
            "schema_version": 1, "policy": "cautious", "seed": 1,
            "talent": "health", "turns": 2, "death_turn": None,
            "final": {
                "resources": {}, "traits": {}, "bank_stage": 0,
                "currency_stage": 0, "local_credit_stage": 0,
                "enforcement_stage": 0, "barter_stage": 0,
            },
            "counts": {}, "institution_trajectories": {},
            "npcs": {"npc1": {
                "name": "npc1", "role": "acquaintance", "trust": 50,
                "ethics": 50, "retire_turn": 20, "birth_turn": -800,
                "death_turn": 2, "died_turn": 2, "alive": False,
            }},
            "contracts": [], "agree_log": [], "npc_death_count": 1,
            "trace": {
                "turns": [], "settlements": [],
                "npc_introductions": [{"turn": 1, "name": "npc1"}],
                "npc_events": [{
                    "turn": 2, "kind": "npc_died", "name": "npc1",
                    "age": 66.8, "orphaned_contract_ids": [],
                }],
                "character_events": [], "population_events": [],
            },
        }
        built = build_dashboard.build_dashboard_data(data)
        self.assertFalse(built["npcs"][0]["alive"])
        self.assertEqual(built["npcs"][0]["died_turn"], 2)
        self.assertEqual(built["meta"]["npc_death_count"], 1)
        event = next(e for e in built["observer_events"] if e["kind"] == "npc_died")
        self.assertEqual(event["name"], "npc1")


if __name__ == "__main__":
    unittest.main()
