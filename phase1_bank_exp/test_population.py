# -*- coding: utf-8 -*-
"""人口維持制度とデジタル水槽の世代継続境界。"""
import copy
import random
import unittest
from unittest import mock

from institutions import (
    activity_communities, population, production_practice, spatial)
import game
from dashboard import build_dashboard


class PopulationRulesTest(unittest.TestCase):
    def test_initial_state_is_settlement_scoped(self):
        row = population.initial_settlement()
        self.assertEqual(row["id"], population.HOME_SETTLEMENT_ID)
        self.assertEqual(row["population"], population.POPULATION_INITIAL)
        self.assertLessEqual(row["reproductive_population"], row["population"])
        self.assertEqual(row["stage"], population.POPULATION_STAGE_MAINTAINED)
        self.assertEqual(sum(row["age_cohorts"].values()), row["population"])
        self.assertEqual(
            row["productive_population"],
            row["age_cohorts"][population.AGE_COHORT_PRODUCTIVE])

    def test_age_cohorts_are_integer_source_of_truth_and_scale_to_million(self):
        row = population.initial_settlement(
            "mega", population=1_000_000,
            reproductive_population=350_000)
        self.assertEqual(row["demography_version"], population.DEMOGRAPHY_VERSION)
        self.assertEqual(sum(row["age_cohorts"].values()), 1_000_000)
        self.assertEqual(row["productive_population"], 620_000)
        self.assertGreaterEqual(
            row["productive_population"], row["reproductive_population"])

    def test_monthly_aging_birth_and_death_preserve_population_exactly(self):
        row = population.initial_settlement()
        for _ in range(24):
            plan = population.plan_population_turn(row, 0.0, 80.0)
            row = plan["settlement"]
            self.assertEqual(sum(row["age_cohorts"].values()), row["population"])
            self.assertEqual(
                row["productive_population"],
                row["age_cohorts"][population.AGE_COHORT_PRODUCTIVE])
        forced = population.initial_settlement(
            "young", population=10, reproductive_population=0)
        forced["age_cohorts"] = {
            population.AGE_COHORT_CHILDREN: 10,
            population.AGE_COHORT_PRODUCTIVE: 0,
            population.AGE_COHORT_ELDERLY: 0,
        }
        forced["productive_population"] = 0
        forced["age_transition_carry"]["children_to_productive"] = 0.99
        aged = population.plan_population_turn(
            forced, 0.0, 80.0)["settlement"]
        self.assertEqual(aged["productive_population"], 1)

    def test_character_death_removes_the_matching_age_cohort(self):
        row = population.initial_settlement()
        productive = population.apply_character_death(row, 40)
        elderly = population.apply_character_death(row, 80)
        self.assertEqual(
            productive["age_cohorts"][population.AGE_COHORT_PRODUCTIVE],
            row["age_cohorts"][population.AGE_COHORT_PRODUCTIVE] - 1)
        self.assertEqual(
            elderly["age_cohorts"][population.AGE_COHORT_ELDERLY],
            row["age_cohorts"][population.AGE_COHORT_ELDERLY] - 1)

    def test_legacy_demography_upgrade_is_rng_free_and_population_preserving(self):
        legacy = {
            "id": "legacy", "population": 17,
            "reproductive_population": 3, "stage": 2}
        rng = random.getstate()
        upgraded = population.upgrade_settlement_demography(legacy)
        self.assertEqual(sum(upgraded["age_cohorts"].values()), 17)
        self.assertEqual(legacy, {
            "id": "legacy", "population": 17,
            "reproductive_population": 3, "stage": 2})
        self.assertEqual(random.getstate(), rng)

    def test_healthy_conditions_maintain_population(self):
        row = population.initial_settlement()
        for _ in range(192):
            row = population.plan_population_turn(row, 0.0, 80.0)["settlement"]
        self.assertGreaterEqual(row["population"], population.POPULATION_INITIAL)
        self.assertEqual(row["stage"], population.POPULATION_STAGE_MAINTAINED)
        self.assertGreater(row["births_total"], 0)
        self.assertGreater(row["deaths_total"], 0)

    def test_shortage_exposes_all_nonhealthy_stages_before_extinction(self):
        row = population.initial_settlement()
        seen = set()
        for _ in range(240):
            row = population.plan_population_turn(row, 100.0, 0.0)["settlement"]
            seen.add(row["stage"])
            if row["stage"] == population.POPULATION_STAGE_EXTINCT:
                break
        self.assertEqual(seen, {1, 2, 3, 4})
        self.assertEqual(row["population"], 0)
        self.assertEqual(row["reproductive_population"], 0)

    def test_stage2_is_nonzero_and_distinct_from_extinction(self):
        stage = population.population_stage_next(
            population.POPULATION_STAGE_DECLINING, 40, 10, -1.0)
        self.assertEqual(stage, population.POPULATION_STAGE_NON_REPRODUCTIVE)
        self.assertNotEqual(stage, population.POPULATION_STAGE_EXTINCT)

    def test_stage4_is_irreversible_for_same_settlement(self):
        self.assertEqual(
            population.population_stage_next(
                population.POPULATION_STAGE_EXTINCT, 100, 40, 2.0),
            population.POPULATION_STAGE_EXTINCT)

    def test_recovery_is_one_stage_at_a_time(self):
        self.assertEqual(
            population.population_stage_next(
                population.POPULATION_STAGE_ABANDONED,
                population.ABANDONED_POPULATION_EXIT, 30, 1.0),
            population.POPULATION_STAGE_NON_REPRODUCTIVE)
        self.assertEqual(
            population.population_stage_next(
                population.POPULATION_STAGE_NON_REPRODUCTIVE,
                40, population.REPRODUCTIVE_POPULATION_EXIT, 1.0),
            population.POPULATION_STAGE_DECLINING)

    def test_fractional_flows_accumulate_without_rng(self):
        state = random.getstate()
        row = population.initial_settlement()
        first = population.plan_population_turn(row, 0.0, 80.0)
        self.assertEqual(first["births"], 0)
        self.assertGreater(first["settlement"]["birth_carry"], 0.0)
        self.assertEqual(random.getstate(), state)

    def test_input_is_not_mutated(self):
        row = population.initial_settlement()
        before = copy.deepcopy(row)
        population.plan_population_turn(row, 50.0, 40.0)
        population.apply_character_death(row)
        self.assertEqual(row, before)

    def test_character_death_changes_population_not_credit(self):
        row = population.initial_settlement()
        after = population.apply_character_death(row)
        self.assertEqual(after["population"], row["population"] - 1)
        self.assertEqual(after["deaths_total"], row["deaths_total"] + 1)
        self.assertNotIn("trust", after)
        self.assertNotIn("default", after)

    def test_local_and_global_extinction_are_distinct(self):
        settlements = {
            "a": population.initial_settlement("a", population=0,
                                                 reproductive_population=0),
            "b": population.initial_settlement("b", population=3,
                                                 reproductive_population=0),
        }
        self.assertFalse(population.world_is_extinct(settlements))
        settlements["b"] = population.initial_settlement(
            "b", population=0, reproductive_population=0)
        self.assertTrue(population.world_is_extinct(settlements))

    def test_transition_event_names_follow_spec(self):
        self.assertEqual(
            population.population_transition_event(1),
            "settlement_population_declining")
        self.assertEqual(
            population.population_transition_event(2),
            "settlement_non_reproductive")
        self.assertEqual(
            population.population_transition_event(3),
            "settlement_abandoned")
        self.assertEqual(
            population.population_transition_event(4),
            "population_extinct")
        self.assertIsNone(population.population_transition_event(0))


class WorldContinuationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.trace = {"turns": [], "settlements": [], "npc_introductions": [],
                     "character_events": [], "population_events": []}
        # 世代継続そのものの既存goldenは、開拓による新しい分岐とは別に固定する。
        # 開拓の実世界統合はPioneeringWorldIntegrationTestと
        # WorldResumeEquivalenceTestで検証する。
        with (mock.patch.object(
                spatial, "PIONEERING_FIRST_TURN", 10_000),
              mock.patch.object(
                production_practice,
                "PRACTICE_MAX_PRODUCTIVITY_BONUS", 0.0)):
            cls.result = game.simulate_policy(
                "cautious", 1720, seed=1, safety_floor=game.SAFETY_FLOOR,
                talent="health", trace=cls.trace, continue_world=True)

    def test_individual_death_does_not_stop_world_clock(self):
        self.assertIsNotNone(self.result["death_turn"])
        self.assertLess(
            self.result["death_turn"],
            self.result["resume_state"]["completed_turn"])
        self.assertEqual(
            len(self.trace["turns"]),
            self.result["resume_state"]["completed_turn"])
        self.assertEqual(
            self.trace["turns"][-1]["turn"],
            self.result["resume_state"]["completed_turn"])
        self.assertEqual(
            self.result["world_extinct"],
            self.result["world_extinct_turn"] is not None)

    def test_focus_moves_to_next_generation_and_age_resets(self):
        deaths = [row for row in self.trace["character_events"]
                  if row["kind"] == "character_died"]
        successors = [row for row in self.trace["character_events"]
                      if row["kind"] == "successor_selected"]
        self.assertGreaterEqual(len(successors), 1)
        # 個人死亡ごとに後継へ移る。ただし最後の焦点人物と同時に世界人口も
        # 尽きた場合は後継が存在しない。絶滅を不正な終端として扱わない。
        self.assertEqual(
            len(deaths), len(successors)
            + (1 if self.result["world_extinct"] else 0))
        self.assertEqual(successors[0]["generation"], 2)
        death_turn = deaths[0]["turn"]
        death_snapshot = self.trace["turns"][death_turn - 1]
        successor_snapshot = self.trace["turns"][death_turn]
        self.assertFalse(death_snapshot["character_alive"])
        self.assertTrue(successor_snapshot["character_alive"])
        self.assertGreater(death_snapshot["character_age"], 14)
        self.assertLess(successor_snapshot["character_age"], 14)
        self.assertLess(
            successor_snapshot["character_age"],
            death_snapshot["character_age"])

    def test_death_orphans_open_contracts_without_defaulting_them(self):
        # 長期の選択軌跡に「死亡時点でたまたまopen契約がある」ことを依存させず、
        # 1ターンcheckpointへ未清算契約を置いて次月の個人死亡を決定論的に作る。
        base = game.simulate_policy(
            "cautious", 1, seed=71, safety_floor=game.SAFETY_FLOOR,
            talent="health", continue_world=True)
        checkpoint = copy.deepcopy(base["resume_state"])
        checkpoint["traits"]["health"] = 0.0
        checkpoint["contract_sequence"] += 1
        contract_id = f"c{checkpoint['contract_sequence']}"
        checkpoint["contracts"].append({
            "id": contract_id, "status": "open", "due_turn": 999,
            "repay_money": -10, "counterparty": "forced-lender",
        })
        trace = {"turns": [], "settlements": [], "npc_introductions": [],
                 "character_events": [], "population_events": []}
        resumed = game.simulate_policy(
            "cautious", 2, seed=71, safety_floor=game.SAFETY_FLOOR,
            talent="health", continue_world=True,
            resume_state=checkpoint, trace=trace)
        death = next(row for row in trace["character_events"]
                     if row["kind"] == "character_died")
        self.assertIn(contract_id, death["orphaned_contract_ids"])
        by_id = {row["id"]: row for row in resumed["contracts"]}
        self.assertEqual(by_id[contract_id]["status"], "orphaned")

    def test_population_map_and_sixth_institution_match_world_status(self):
        active_ids = set(self.result["spatial_state"]["clusters"])
        positive_ids = {
            settlement_id for settlement_id, row
            in self.result["settlements"].items() if row["population"] > 0}
        self.assertEqual(active_ids, positive_ids)
        self.assertEqual(
            active_ids,
            set(self.result["activity_community_ledger"]["communities"]))
        self.assertEqual(
            self.result["activity_community_accounting_version"],
            activity_communities.ACTIVITY_COMMUNITY_ACCOUNTING_VERSION)
        self.assertTrue(all(
            settlement_id.startswith("cluster:")
            for settlement_id in self.result["settlements"]))
        total_population = sum(
            row["population"] for row in self.result["settlements"].values())
        self.assertEqual(bool(positive_ids), total_population > 0)
        self.assertEqual(self.result["world_extinct"], total_population == 0)
        self.assertIn("population", self.result["institution_trajectories"])
        self.assertEqual(
            self.result["institution_trajectories"]["population"]["final_stage"],
            self.result["population_stage"])

    def test_migration_preserves_world_population_accounting(self):
        settlements = self.result["settlements"]
        initial_total = 120 + 78 + 52
        births = sum(row["births_total"] for row in settlements.values())
        deaths = sum(row["deaths_total"] for row in settlements.values())
        population = sum(row["population"] for row in settlements.values())
        immigrants = sum(row["immigrants_total"] for row in settlements.values())
        emigrants = sum(row["emigrants_total"] for row in settlements.values())
        self.assertEqual(population, initial_total + births - deaths)
        self.assertEqual(immigrants, emigrants)
        self.assertEqual(immigrants, self.result["migration_total"])
        self.assertGreater(immigrants, 0)

    def test_named_resident_ledger_matches_population_and_successor(self):
        registry = self.result["resident_registry"]
        living = [row for row in registry["residents"].values()
                  if row.get("alive", True)]
        self.assertEqual(
            len(living),
            sum(row["population"] for row in self.result["settlements"].values()))
        successor = next(
            row for row in reversed(self.trace["character_events"])
            if row["kind"] == "successor_selected")
        self.assertTrue(successor["resident_name"])
        self.assertTrue(successor["household_name"])
        self.assertEqual(
            successor["resident_id"], self.result["focus_resident_id"])

    def test_focus_lineage_remains_consistent_with_world_status(self):
        focus_id = self.result["focus_settlement_id"]
        focus_resident = self.result["resident_registry"]["residents"][
            self.result["focus_resident_id"]]
        self.assertEqual(focus_resident["settlement_id"], focus_id)
        if self.result["world_extinct"]:
            self.assertNotIn(
                focus_id, self.result["spatial_state"]["clusters"])
            self.assertEqual(
                self.result["settlements"][focus_id]["population"], 0)
            self.assertFalse(focus_resident["alive"])
        else:
            self.assertIn(focus_id, self.result["spatial_state"]["clusters"])
            self.assertGreater(
                self.result["settlements"][focus_id]["population"], 0)
            self.assertTrue(focus_resident["alive"])
        self.assertTrue(any(
            event["kind"] == "cluster_dissolved"
            for event in self.result["spatial_state"]["cluster_events"]))
        successor = next(
            event for event in reversed(self.trace["character_events"])
            if event["kind"] == "successor_selected")
        self.assertEqual(
            successor["resident_id"], self.result["focus_resident_id"])

    def test_legacy_mode_still_ends_at_first_character_death(self):
        legacy = game.simulate_policy(
            "cautious", 1720, seed=1, safety_floor=game.SAFETY_FLOOR,
            talent="health")
        self.assertIsNotNone(legacy["death_turn"])
        self.assertNotIn("world_mode", legacy)
        self.assertNotIn("settlements", legacy)

    def test_visualize_collection_enables_world_mode_by_default(self):
        data = game.collect_visualize_trace(
            1, "cautious", 12, game.SAFETY_FLOOR, "health")
        self.assertTrue(data["world_mode"])
        self.assertIn("settlement_states", data)
        self.assertIn("resident_registry", data)
        self.assertIn("spatial_state", data)
        self.assertIn("activity_community_ledger", data)
        self.assertIn("focus_resident_id", data)
        self.assertEqual(
            [row["turn"] for row in data["trace"]["spatial_keyframes"]],
            [1, 12])
        self.assertEqual(len(data["trace"]["turns"]), 12)
        self.assertIn("population", data["trace"]["turns"][0])
        built = build_dashboard.build_dashboard_data(data)
        self.assertEqual(
            built["meta"]["living_resident_count"],
            built["turns"][-1]["total_population"])
        self.assertEqual(
            len(built["residents"]), built["meta"]["resident_total"])
        self.assertGreaterEqual(
            len(built["residents"]), built["meta"]["living_resident_count"])
        self.assertGreater(len(built["households"]), 0)
        self.assertTrue(any(row["focus"] for row in built["residents"]))
        self.assertEqual(built["spatial_state"]["residents"], {})
        self.assertGreater(len(data["spatial_state"]["residents"]), 0)
        self.assertEqual(
            built["spatial_state"]["packed_resident_count"],
            built["particle_frame"]["count"])
        self.assertEqual(
            built["particle_frame"]["count"],
            built["meta"]["living_resident_count"])
        self.assertEqual(built["particle_cohorts"]["version"], 2)
        self.assertEqual(built["particle_cohorts"]["cohorts"], [])
        self.assertEqual(
            built["particle_cohorts"]["source_turn_count"],
            len(built["turns"]))
        self.assertGreater(len(built["particle_cohorts"]["frames"]), 0)
        self.assertEqual(
            built["particle_cohorts"]["total_population"],
            built["meta"]["living_resident_count"])
        self.assertEqual(built["meta"]["anonymous_particle_count"], 0)
        self.assertEqual(
            built["meta"]["particle_population_total"],
            built["meta"]["living_resident_count"])
        self.assertEqual(built["meta"]["particle_cohort_version"], 2)
        self.assertEqual(
            built["meta"]["particle_cohort_frame_count"],
            len(built["particle_cohorts"]["frames"]))
        self.assertEqual(
            {key: value for key, value in built["spatial_state"].items()
             if key not in {"residents", "packed_resident_count"}},
            {key: value for key, value in data["spatial_state"].items()
             if key != "residents"})
        self.assertEqual(
            built["activity_community_ledger"],
            data["activity_community_ledger"])
        self.assertEqual(built["spatial_keyframes"], [])
        self.assertEqual(built["spatial_history"]["version"], 1)
        self.assertEqual(
            built["spatial_history"]["source_frame_count"],
            len(data["trace"]["spatial_keyframes"]))
        self.assertEqual(
            [row["turn"] for row in built["spatial_history"]["frames"]],
            [row["turn"] for row in data["trace"]["spatial_keyframes"]])
        self.assertEqual(
            built["meta"]["activity_site_count"],
            len([row for row in data["spatial_state"]["sites"].values()
                 if row["active"]]))
        self.assertEqual(
            built["meta"]["activity_cluster_count"],
            len(data["spatial_state"]["clusters"]))
        self.assertEqual(
            built["meta"]["activity_provisional_cluster_count"],
            sum(1 for row in data["spatial_state"]["clusters"].values()
                if row.get("provisional", False)))
        self.assertEqual(
            built["meta"]["activity_accounting_cluster_count"],
            sum(1 for row in data["spatial_state"]["clusters"].values()
                if not row.get("provisional", False)))
        self.assertEqual(
            built["meta"]["activity_cluster_lineage_count"],
            len(data["spatial_state"]["cluster_lineages"]))
        self.assertEqual(
            built["meta"]["activity_cluster_event_count"],
            len(data["spatial_state"]["cluster_events"]))
        self.assertEqual(
            built["meta"]["activity_community_count"],
            len(data["activity_community_ledger"]["communities"]))
        self.assertEqual(
            built["meta"]["activity_community_accounting_version"],
            data["activity_community_accounting_version"])
        self.assertEqual(built["event_density_history"]["version"], 1)
        self.assertEqual(built["meta"]["observer_detail_mode"], "full")
        raw_population = data["trace"]["population_events"]
        self.assertEqual(
            sum(row[2] for row in built["event_density_history"]["population"]),
            sum(row.get("births", 0) for row in raw_population
                if row["kind"] == "population_changed"))
        self.assertEqual(
            sum(row[3] for row in built["event_density_history"]["population"]),
            sum(row.get("deaths", 0) for row in raw_population
                if row["kind"] == "population_changed"))
        self.assertEqual(
            sum(row[3] for row in built["event_density_history"]["migrations"]),
            sum(row.get("migrants", 0) for row in raw_population
                if row["kind"] == "population_migrated"))


class PioneeringWorldIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = game.simulate_policy(
            "cautious", 300, seed=1, safety_floor=game.SAFETY_FLOOR,
            talent="health", continue_world=True)

    def test_real_world_starts_and_settles_pioneering_activity(self):
        events = self.result["spatial_state"]["cluster_events"]
        self.assertTrue(any(
            event["kind"] == "pioneering_started" for event in events))
        self.assertTrue(any(
            event["kind"] == "pioneering_settled" for event in events))
        self.assertEqual(
            self.result["resume_state"]["completed_turn"], 300)
        self.assertFalse(self.result["world_extinct"])

    def test_provisional_activity_clusters_do_not_become_extra_accounts(self):
        state = self.result["spatial_state"]
        ledger = self.result["activity_community_ledger"]
        accounting_ids = spatial.accounting_activity_cluster_ids(state)
        self.assertTrue(ledger["provisional_clusters"])
        self.assertEqual(set(ledger["communities"]), accounting_ids)
        self.assertGreater(len(state["clusters"]), len(accounting_ids))
        self.assertEqual(
            sum(row["population"] for row in ledger["communities"].values()),
            sum(row["population"] for row in self.result["settlements"].values()))


class PopulationDashboardTest(unittest.TestCase):
    def test_observer_combines_population_and_generation_events(self):
        turns = [
            {"t": 1, "population_stage": 0},
            {"t": 2, "population_stage": 1},
        ]
        events = build_dashboard.build_observer_events(
            turns, [], [], 2,
            character_events=[
                {"turn": 2, "kind": "character_died", "generation": 1,
                 "population_after": 10},
                {"turn": 2, "kind": "successor_selected", "generation": 2,
                 "population": 10},
            ],
            population_events=[
                {"turn": 2, "kind": "population_changed", "births": 0,
                 "deaths": 1, "population": 10},
            ])
        self.assertEqual([row["kind"] for row in events], [
            "institution_transition", "character_died", "successor_selected",
            "population_changed",
        ])
        self.assertEqual(events[0]["institution"], "population")

    def test_template_renders_population_generation_and_world_time(self):
        template = build_dashboard.DEFAULT_TEMPLATE.read_text(encoding="utf-8")
        for marker in (
                'id="chart-population"', "function renderPopulation()",
                "reproductive_population", "successor_selected",
                "DATA.meta.world_mode", "pioneering_started",
                "pioneering_settled", "pioneering_community_formed",
                "外へ伸びる点 = 新しい活動地の開拓"):
            self.assertIn(marker, template)

    def test_observer_includes_spatial_lineage_events(self):
        events = build_dashboard.build_observer_events(
            [{"t": 1}, {"t": 2}], [], [], None,
            spatial_events=[{
                "turn": 2, "kind": "cluster_split",
                "cluster_id": "cluster:000001",
                "source_cluster_id": "cluster:000001",
                "cluster_ids": ["cluster:000001", "cluster:000004"],
                "x": 0.5, "y": 0.5, "site_count": 4,
                "resident_count": 20,
            }])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "cluster_split")
        self.assertEqual(events[0]["t"], 2)

    def test_observer_preserves_pioneering_start_and_arrival_order(self):
        events = build_dashboard.build_observer_events(
            [{"t": 24}, {"t": 25}], [], [], None,
            spatial_events=[
                {"turn": 24, "kind": "pioneering_started",
                 "pioneering_id": "pioneer:000001",
                 "source_cluster_id": "cluster:000001",
                 "site_id": "site:h000001", "x": .4, "y": .4,
                 "target_x": .7, "target_y": .7},
                {"turn": 25, "kind": "pioneering_settled",
                 "pioneering_id": "pioneer:000001",
                 "source_cluster_id": "cluster:000001",
                 "cluster_id": "cluster:000004", "x": .55, "y": .55},
            ])

        self.assertEqual(
            [(row["t"], row["kind"]) for row in events], [
                (24, "pioneering_started"),
                (25, "pioneering_settled"),
            ])


if __name__ == "__main__":
    unittest.main()
