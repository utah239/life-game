# -*- coding: utf-8 -*-
"""名前付き住民・世帯台帳の純粋ルール。"""
import copy
import json
import random
import unittest

from institutions import population, residents, settlement_network


HOME_ECONOMY = {
    "food": 90.0, "medicine": 92.0, "shelter": 70.0, "tools": 65.0,
    "production_capacity": 60.0, "barter_stage": 0,
    "community_trust": 50.0, "local_credit_stage": 0,
}


class ResidentRegistryRulesTest(unittest.TestCase):
    def setUp(self):
        self.settlements = settlement_network.initial_settlement_network(
            HOME_ECONOMY)

    def test_initial_registry_names_every_resident_without_rng_or_input_mutation(self):
        before = copy.deepcopy(self.settlements)
        rng_before = random.getstate()
        registry = residents.initial_resident_registry(
            self.settlements, world_seed=17)
        self.assertTrue(residents.registry_matches_settlements(
            registry, self.settlements))
        self.assertEqual(len(registry["residents"]), 250)
        self.assertGreater(len(registry["households"]), 1)
        self.assertTrue(all(row["name"] for row in registry["residents"].values()))
        self.assertTrue(all(
            row["household_id"] in registry["households"]
            for row in registry["residents"].values()))
        self.assertEqual(registry, residents.initial_resident_registry(
            self.settlements, world_seed=17))
        self.assertEqual(random.getstate(), rng_before)
        self.assertEqual(self.settlements, before)

    def test_focus_selection_uses_thirteen_year_old_and_assignment_is_persistent(self):
        registry = residents.initial_resident_registry(
            self.settlements, world_seed=3)
        focus_id = residents.select_focus_resident(registry, "home", 1)
        self.assertEqual(
            residents.resident_age_years(registry["residents"][focus_id], 1),
            13.0)
        assigned = residents.assign_focus(
            registry, focus_id, 1, generation=1, talent="health")
        self.assertEqual(assigned["residents"][focus_id]["focus_count"], 1)
        self.assertEqual(assigned["residents"][focus_id]["talent"], "health")
        self.assertEqual(registry["residents"][focus_id]["focus_count"], 0)

    def test_birth_and_background_death_keep_focus_when_population_remains(self):
        registry = residents.initial_resident_registry(
            self.settlements, world_seed=5)
        focus_id = residents.select_focus_resident(registry, "home", 1)
        before = copy.deepcopy(registry)
        plan = residents.apply_population_change(
            registry, "home", births=2, deaths=3, turn=12,
            protected_resident_ids=(focus_id,))
        after = plan["registry"]
        self.assertTrue(after["residents"][focus_id]["alive"])
        self.assertEqual(
            len(residents.living_residents(after, "home")),
            len(residents.living_residents(registry, "home")) - 1)
        self.assertEqual(
            [event["kind"] for event in plan["events"]
             if event["kind"].startswith("resident_")],
            ["resident_born", "resident_born",
             "resident_died", "resident_died", "resident_died"])
        newborns = [row for row in after["residents"].values()
                    if row["origin"] == "birth"]
        self.assertEqual(len(newborns), 2)
        self.assertTrue(all(row["birth_turn"] == 12 for row in newborns))
        self.assertEqual(registry, before)

    def test_mark_resident_died_targets_exact_identity(self):
        registry = residents.initial_resident_registry(
            self.settlements, world_seed=6)
        target = residents.living_residents(registry, "upland")[-1]
        plan = residents.mark_resident_died(
            registry, target["id"], 20, "focal_character")
        self.assertFalse(plan["registry"]["residents"][target["id"]]["alive"])
        self.assertEqual(plan["events"][0]["resident_id"], target["id"])
        self.assertTrue(registry["residents"][target["id"]]["alive"])

    def test_migration_moves_named_people_and_preserves_total(self):
        registry = residents.initial_resident_registry(
            self.settlements, world_seed=7)
        focus_id = residents.select_focus_resident(registry, "home", 1)
        event = {
            "from_settlement": "home", "to_settlement": "riverside",
            "migrants": 6, "reproductive_migrants": 2,
        }
        before = copy.deepcopy(registry)
        plan = residents.apply_migration(
            registry, event, 10, protected_resident_ids=(focus_id,))
        after = plan["registry"]
        self.assertEqual(len(residents.living_residents(after)), 250)
        self.assertEqual(len(residents.living_residents(after, "home")), 114)
        self.assertEqual(len(residents.living_residents(after, "riverside")), 84)
        self.assertEqual(after["residents"][focus_id]["settlement_id"], "home")
        migrated = next(
            row for row in plan["events"]
            if row["kind"] == "residents_migrated")
        self.assertEqual(len(migrated["residents"]), 6)
        self.assertTrue(all(
            after["households"][row["household_id"]]["settlement_id"]
            == "riverside" for row in migrated["residents"]))
        self.assertEqual(registry, before)

    def test_compaction_drops_only_old_dead_records_and_keeps_sequences(self):
        registry = residents.initial_resident_registry(
            self.settlements, world_seed=8)
        old_id, recent_id = [
            row["id"] for row in residents.living_residents(
                registry, "upland")[:2]]
        old = residents.mark_resident_died(
            registry, old_id, 10, "background")["registry"]
        recent = residents.mark_resident_died(
            old, recent_id, 90, "background")["registry"]
        sequence = recent["resident_sequence"]
        compact = residents.compact_registry(recent, cutoff_turn=50)
        self.assertNotIn(old_id, compact["residents"])
        self.assertIn(recent_id, compact["residents"])
        self.assertEqual(compact["resident_sequence"], sequence)
        self.assertEqual(
            len(residents.living_residents(compact)),
            len(residents.living_residents(recent)))
        self.assertEqual(compact["archived_resident_count"], 1)

    def test_legacy_snapshot_preserves_historical_totals_without_adding_population(self):
        self.settlements["home"]["births_total"] = 9
        self.settlements["home"]["deaths_total"] = 4
        registry = residents.initial_resident_registry(
            self.settlements, 11, turn=200, legacy_snapshot=True)
        self.assertTrue(registry["legacy_snapshot"])
        self.assertEqual(registry["births_total"], 9)
        self.assertEqual(registry["deaths_total"], 4)
        self.assertTrue(residents.registry_matches_settlements(
            registry, self.settlements))

    def test_version_one_upgrade_preserves_named_people_and_adds_activity_fields(self):
        current = residents.initial_resident_registry(
            self.settlements, world_seed=12)
        old = copy.deepcopy(current)
        old["version"] = 1
        for household in old["households"].values():
            for key in ("livelihood", "activity_count", "last_activity_turn",
                        "last_activity", "last_actor_id"):
                household.pop(key)
        for resident in old["residents"].values():
            for key in ("activity_count", "last_activity_turn",
                        "last_activity"):
                resident.pop(key)
        before = copy.deepcopy(old)
        rng_before = random.getstate()

        upgraded = residents.upgrade_resident_registry(old)

        self.assertEqual(
            upgraded["version"], residents.RESIDENT_REGISTRY_VERSION)
        self.assertEqual(
            {key: row["name"] for key, row in upgraded["residents"].items()},
            {key: row["name"] for key, row in old["residents"].items()})
        self.assertEqual(
            {key: row["name"] for key, row in upgraded["households"].items()},
            {key: row["name"] for key, row in old["households"].items()})
        self.assertTrue(all(
            row["livelihood"] in residents.HOUSEHOLD_LIVELIHOODS
            and row["activity_count"] == 0
            for row in upgraded["households"].values()))
        self.assertTrue(all(
            row["activity_count"] == 0
            for row in upgraded["residents"].values()))
        self.assertTrue(all(
            sum(row["activity_counts_by_good"].values())
            + row["unclassified_activity_count"] == row["activity_count"]
            for row in (*upgraded["residents"].values(),
                        *upgraded["households"].values())))
        self.assertEqual(old, before)
        self.assertEqual(random.getstate(), rng_before)

    def test_version_three_history_stays_unclassified_until_new_activity(self):
        current = residents.initial_resident_registry(
            self.settlements, world_seed=121)
        legacy = copy.deepcopy(current)
        legacy["version"] = 3
        household = next(iter(legacy["households"].values()))
        resident = next(row for row in legacy["residents"].values()
                        if row["household_id"] == household["id"])
        for row in (household, resident):
            row["activity_count"] = 7
            row.pop("activity_counts_by_good", None)
            row.pop("unclassified_activity_count", None)

        upgraded = residents.upgrade_resident_registry(legacy)
        for row_id, collection in (
                (household["id"], upgraded["households"]),
                (resident["id"], upgraded["residents"])):
            row = collection[row_id]
            self.assertEqual(row["unclassified_activity_count"], 7)
            self.assertEqual(sum(row["activity_counts_by_good"].values()), 0)
            residents.record_activity_count(row, "tools")
            self.assertEqual(row["activity_count"], 8)
            self.assertEqual(row["activity_counts_by_good"]["tools"], 1)
            self.assertEqual(row["unclassified_activity_count"], 7)

    def test_household_activity_prefers_shortage_specialist_and_working_age_actor(self):
        registry = residents.initial_resident_registry(
            self.settlements, world_seed=13)
        before = copy.deepcopy(registry)
        rng_before = random.getstate()

        plan = residents.plan_household_activity(
            registry, "home", turn=24, preferred_good="medicine")
        event = plan["event"]
        household = plan["registry"]["households"][event["household_id"]]
        actor = plan["registry"]["residents"][event["resident_id"]]

        self.assertEqual(event["kind"], "household_activity")
        self.assertEqual(event["activity"], "medicine")
        self.assertEqual(event["livelihood"], "medicine")
        self.assertTrue(event["responding_to_shortage"])
        self.assertGreaterEqual(event["resident_age"], 15)
        self.assertLessEqual(event["resident_age"], 74)
        self.assertEqual(household["activity_count"], 1)
        self.assertEqual(household["activity_counts_by_good"]["medicine"], 1)
        self.assertEqual(household["last_actor_id"], actor["id"])
        self.assertEqual(actor["activity_count"], 1)
        self.assertEqual(actor["activity_counts_by_good"]["medicine"], 1)
        self.assertEqual(registry, before)
        self.assertEqual(random.getstate(), rng_before)

    def test_household_activities_record_one_event_per_living_settlement(self):
        registry = residents.initial_resident_registry(
            self.settlements, world_seed=14)
        preferred = {
            "home": "food", "riverside": "medicine", "upland": "tools"}

        first = residents.plan_household_activities(
            registry, turn=7, preferred_goods=preferred)
        second = residents.plan_household_activities(
            registry, turn=7, preferred_goods=preferred)

        self.assertEqual(first, second)
        self.assertEqual(len(first["events"]), 3)
        self.assertEqual(
            [row["settlement_id"] for row in first["events"]],
            list(preferred))
        self.assertEqual(
            [row["activity"] for row in first["events"]],
            list(preferred.values()))
        self.assertEqual(sum(
            row["activity_count"]
            for row in first["registry"]["households"].values()), 3)

    def test_household_activity_without_living_resident_is_noop(self):
        registry = residents.initial_resident_registry(
            {"empty": {"population": 0}}, world_seed=15)
        plan = residents.plan_household_activity(
            registry, "empty", turn=1, preferred_good="food")
        self.assertIsNone(plan["event"])
        self.assertEqual(plan["registry"], registry)


class HybridResidentCohortTest(unittest.TestCase):
    def setUp(self):
        self.settlements = {
            "a": population.initial_settlement(
                "a", population=600_000,
                reproductive_population=210_000),
            "b": population.initial_settlement(
                "b", population=400_000,
                reproductive_population=140_000),
        }

    def build(self):
        return residents.initial_resident_registry(
            self.settlements, world_seed=17)

    def test_million_people_use_bounded_named_records_and_exact_cohort_total(self):
        before = copy.deepcopy(self.settlements)
        rng_before = random.getstate()

        registry = self.build()

        self.assertTrue(registry["cohort_mode"])
        self.assertEqual(
            len(residents.living_residents(registry)),
            residents.NAMED_RESIDENT_LIMIT)
        self.assertEqual(
            residents.anonymous_population_count(registry),
            1_000_000 - residents.NAMED_RESIDENT_LIMIT)
        self.assertEqual(
            residents.living_counts_by_settlement(registry),
            {"a": 600_000, "b": 400_000})
        self.assertLessEqual(
            len(registry["households"]),
            residents.NAMED_RESIDENT_LIMIT //
            residents.INITIAL_HOUSEHOLD_TARGET_SIZE + 2)
        self.assertIsNotNone(
            residents.select_focus_resident(registry, "a", 1))
        self.assertLess(
            len(json.dumps(registry, ensure_ascii=False).encode("utf-8")),
            3 * 1024 * 1024)
        self.assertTrue(residents.registry_matches_settlements(
            registry, self.settlements))
        self.assertEqual(self.settlements, before)
        self.assertEqual(random.getstate(), rng_before)

    def test_births_and_deaths_update_anonymous_cohort_before_named_sample(self):
        registry = self.build()
        named_ids = set(registry["residents"])
        before = copy.deepcopy(registry)
        rng_before = random.getstate()

        plan = residents.apply_population_change(
            registry, "a", births=100, deaths=50, turn=2)
        after = plan["registry"]
        expected = copy.deepcopy(self.settlements)
        expected["a"]["population"] += 50

        self.assertEqual(plan["events"], [])
        self.assertEqual(set(after["residents"]), named_ids)
        self.assertEqual(
            residents.living_counts_by_settlement(after)["a"], 600_050)
        self.assertTrue(residents.registry_matches_settlements(
            after, expected))
        self.assertEqual(registry, before)
        self.assertEqual(random.getstate(), rng_before)

    def test_large_migration_moves_cohort_without_materializing_people(self):
        registry = self.build()
        before = copy.deepcopy(registry)
        rng_before = random.getstate()

        plan = residents.apply_migration(registry, {
            "from_settlement": "a", "to_settlement": "b",
            "migrants": 100_000, "reproductive_migrants": 35_000,
        }, turn=2)
        after = plan["registry"]
        event = plan["events"][-1]
        expected = copy.deepcopy(self.settlements)
        expected["a"]["population"] -= 100_000
        expected["b"]["population"] += 100_000

        self.assertEqual(event["kind"], "residents_migrated")
        self.assertEqual(event["migrants"], 100_000)
        self.assertEqual(event["residents"], [])
        self.assertEqual(residents.living_counts_by_settlement(after), {
            "a": 500_000, "b": 500_000})
        self.assertTrue(residents.registry_matches_settlements(
            after, expected))
        self.assertEqual(registry, before)
        self.assertEqual(random.getstate(), rng_before)

    def test_materialized_successor_relabels_existing_cohort_member(self):
        settlement = {"a": population.initial_settlement(
            "a", population=100, reproductive_population=35)}
        registry = residents.initial_resident_registry(
            settlement, 19, named_resident_limit=1)
        first_id = next(iter(registry["residents"]))
        dead = residents.mark_resident_died(
            registry, first_id, turn=20, cause="focal_character")["registry"]
        before_total = residents.living_population_count(dead)
        rng_before = random.getstate()

        plan = residents.materialize_cohort_resident(dead, "a", turn=21)
        after = plan["registry"]
        successor = after["residents"][plan["resident_id"]]

        self.assertNotEqual(plan["resident_id"], first_id)
        self.assertEqual(successor["origin"], "cohort_materialized")
        self.assertEqual(successor["registered_turn"], 21)
        self.assertEqual(residents.resident_age_years(successor, 21), 13.0)
        self.assertEqual(
            residents.living_population_count(after), before_total)
        self.assertEqual(random.getstate(), rng_before)


if __name__ == "__main__":
    unittest.main()
