# -*- coding: utf-8 -*-
"""継続不足に基づく世帯移住の純粋ルールと台帳接続。"""
import copy
import random
import unittest

import game
from dashboard.build_dashboard import build_dashboard_data
from institutions import household_goods, household_mobility, population, residents
from institutions.settlement_network import initial_settlement_network


HOME_ECONOMY = {
    "food": 90.0, "medicine": 92.0, "shelter": 70.0, "tools": 65.0,
    "production_capacity": 60.0, "barter_stage": 0,
    "community_trust": 50.0, "local_credit_stage": 0,
}


def _planner_registry():
    return {
        "households": {
            "h1": {"id": "h1", "settlement_id": "a", "active": True,
                   "last_migration_turn": None},
            "h2": {"id": "h2", "settlement_id": "a", "active": True,
                   "last_migration_turn": None},
            "h3": {"id": "h3", "settlement_id": "a", "active": True,
                   "last_migration_turn": 15},
        },
        "residents": {
            "r1": {"id": "r1", "household_id": "h1",
                   "settlement_id": "a", "alive": True},
            "r2": {"id": "r2", "household_id": "h1",
                   "settlement_id": "a", "alive": True},
            "r3": {"id": "r3", "household_id": "h2",
                   "settlement_id": "a", "alive": True},
            "r4": {"id": "r4", "household_id": "h3",
                   "settlement_id": "a", "alive": True},
        },
    }


def _response(household_id, months, shortfall, priority="food"):
    return {
        "household_id": household_id, "settlement_id": "a",
        "priority_good": priority,
        "consecutive_shortage_months": months,
        "shortfall_by_good": {
            "food": shortfall, "medicine": 0.0,
            "shelter": 0.0, "tools": 0.0},
    }


def _agency():
    return {
        "households": {
            "h1": _response("h1", 5, 0.8),
            "h2": _response("h2", 2, 0.9),
            "h3": _response("h3", 9, 1.0),
        },
        "communities": {
            "a": {"priority_pressure_by_good": {"food": 0.8}},
            "b": {"priority_pressure_by_good": {
                "food": 0.2, "medicine": 0.2,
                "shelter": 0.2, "tools": 0.2}},
        },
    }


class HouseholdMigrationPlannerTest(unittest.TestCase):
    def test_only_persistent_relieved_and_cooled_down_household_is_preferred(self):
        event = {
            "turn": 20, "kind": "population_migrated",
            "from_settlement": "a", "to_settlement": "b",
            "migrants": 4, "reproductive_migrants": 1,
        }
        planned = household_mobility.plan_household_migration(
            event, _agency(), _planner_registry(), 20)
        self.assertEqual(planned["preferred_household_ids"], ["h1"])
        self.assertEqual(planned["household_migration_motives"], [{
            "household_id": "h1", "priority_good": "food",
            "consecutive_shortage_months": 5,
            "source_shortfall": 0.8,
            "destination_pressure": 0.2,
            "expected_relief": 0.6,
            "household_size": 2,
        }])

    def test_no_observed_destination_relief_means_no_household_preference(self):
        event = {
            "from_settlement": "a", "to_settlement": "missing",
            "migrants": 4,
        }
        planned = household_mobility.plan_household_migration(
            event, _agency(), _planner_registry(), 20)
        self.assertEqual(planned["preferred_household_ids"], [])
        self.assertEqual(planned["household_migration_motives"], [])

    def test_candidate_order_prefers_duration_then_relief_and_is_bounded(self):
        registry = _planner_registry()
        agency = _agency()
        registry["households"]["h4"] = {
            "id": "h4", "settlement_id": "a", "active": True,
            "last_migration_turn": None}
        registry["residents"]["r5"] = {
            "id": "r5", "household_id": "h4",
            "settlement_id": "a", "alive": True}
        agency["households"]["h4"] = _response("h4", 6, 0.5)
        event = {
            "from_settlement": "a", "to_settlement": "b", "migrants": 4}
        planned = household_mobility.plan_household_migration(
            event, agency, registry, 40)
        self.assertEqual(planned["preferred_household_ids"], ["h3", "h4", "h1"])
        self.assertLessEqual(
            len(planned["preferred_household_ids"]),
            household_mobility.HOUSEHOLD_MIGRATION_PREFERENCE_LIMIT)

    def test_inputs_and_rng_are_unchanged(self):
        event = {
            "from_settlement": "a", "to_settlement": "b", "migrants": 4}
        agency = _agency()
        registry = _planner_registry()
        before = copy.deepcopy((event, agency, registry))
        random.seed(123)
        rng_before = random.getstate()
        household_mobility.plan_household_migration(
            event, agency, registry, 20)
        self.assertEqual((event, agency, registry), before)
        self.assertEqual(random.getstate(), rng_before)


def _household_members(registry, household_id):
    return [
        row for row in residents.living_residents(registry)
        if row["household_id"] == household_id]


class HouseholdMigrationRegistryTest(unittest.TestCase):
    def test_preferred_whole_household_moves_and_records_reason(self):
        settlements = initial_settlement_network(HOME_ECONOMY)
        registry = residents.initial_resident_registry(
            settlements, world_seed=31)
        household = next(
            row for row in registry["households"].values()
            if row["settlement_id"] == "home"
            and 0 < len(_household_members(registry, row["id"])) <= 6)
        members = _household_members(registry, household["id"])
        reproductive = sum(
            residents.REPRODUCTIVE_AGE_RANGE[0]
            <= residents.resident_age_years(row, 20)
            <= residents.REPRODUCTIVE_AGE_RANGE[1]
            for row in members)
        before = copy.deepcopy(registry)
        plan = residents.apply_migration(registry, {
            "from_settlement": "home", "to_settlement": "riverside",
            "migrants": len(members),
            "reproductive_migrants": reproductive,
            "preferred_household_ids": [household["id"]],
        }, 20)
        after = plan["registry"]
        migration = plan["events"][-1]
        self.assertTrue(all(
            after["residents"][row["id"]]["settlement_id"] == "riverside"
            for row in members))
        self.assertEqual(
            after["households"][household["id"]]["settlement_id"],
            "riverside")
        self.assertEqual(migration["shortage_migrant_count"], len(members))
        self.assertEqual(
            migration["migration_reason"],
            "persistent_household_shortage")
        self.assertEqual(
            migration["shortage_household_ids"], [household["id"]])
        moved_household = after["households"][household["id"]]
        self.assertEqual(moved_household["previous_settlement_id"], "home")
        self.assertEqual(moved_household["last_migration_turn"], 20)
        self.assertEqual(
            moved_household["last_migration_reason"],
            "persistent_household_shortage")
        self.assertEqual(moved_household["migration_count"], 1)
        self.assertEqual(registry, before)

    def test_cohort_world_moves_preferred_named_household_before_anonymous_pool(self):
        settlements = {
            "a": population.initial_settlement(
                "a", population=600_000, reproductive_population=210_000),
            "b": population.initial_settlement(
                "b", population=400_000, reproductive_population=140_000),
        }
        registry = residents.initial_resident_registry(
            settlements, 44, named_resident_limit=128)
        household = next(
            row for row in registry["households"].values()
            if row["settlement_id"] == "a"
            and _household_members(registry, row["id"]))
        members = _household_members(registry, household["id"])
        reproductive = sum(
            residents.REPRODUCTIVE_AGE_RANGE[0]
            <= residents.resident_age_years(row, 20)
            <= residents.REPRODUCTIVE_AGE_RANGE[1]
            for row in members)
        anonymous_before = residents.anonymous_population_count(registry, "a")
        rng_before = random.getstate()
        plan = residents.apply_migration(registry, {
            "from_settlement": "a", "to_settlement": "b",
            "migrants": len(members) + 5,
            "reproductive_migrants": reproductive + 5,
            "preferred_household_ids": [household["id"]],
        }, 20)
        after = plan["registry"]
        migration = plan["events"][-1]
        self.assertEqual(len(migration["residents"]), len(members))
        self.assertEqual(migration["shortage_migrant_count"], len(members))
        self.assertEqual(migration["migration_reason"], "mixed")
        self.assertEqual(
            residents.anonymous_population_count(after, "a"),
            anonymous_before - 5)
        self.assertTrue(all(
            after["residents"][row["id"]]["settlement_id"] == "b"
            for row in members))
        self.assertEqual(random.getstate(), rng_before)

    def test_protected_member_prevents_shortage_driven_whole_household_move(self):
        settlements = initial_settlement_network(HOME_ECONOMY)
        registry = residents.initial_resident_registry(
            settlements, world_seed=32)
        household = next(
            row for row in registry["households"].values()
            if row["settlement_id"] == "home"
            and 1 < len(_household_members(registry, row["id"])) <= 6)
        members = _household_members(registry, household["id"])
        protected = members[0]
        reproductive = sum(
            residents.REPRODUCTIVE_AGE_RANGE[0]
            <= residents.resident_age_years(row, 20)
            <= residents.REPRODUCTIVE_AGE_RANGE[1]
            for row in members)
        plan = residents.apply_migration(registry, {
            "from_settlement": "home", "to_settlement": "riverside",
            "migrants": len(members),
            "reproductive_migrants": reproductive,
            "preferred_household_ids": [household["id"]],
        }, 20, protected_resident_ids=(protected["id"],))
        migration = plan["events"][-1]
        self.assertEqual(
            plan["registry"]["residents"][protected["id"]]["settlement_id"],
            "home")
        self.assertEqual(migration["shortage_migrant_count"], 0)
        self.assertEqual(migration["shortage_household_ids"], [])
        self.assertEqual(migration["migration_reason"], "community_pressure")

    def test_registry_upgrade_adds_migration_history_without_replacing_people(self):
        settlements = initial_settlement_network(HOME_ECONOMY)
        current = residents.initial_resident_registry(
            settlements, world_seed=2)
        legacy = copy.deepcopy(current)
        legacy["version"] = 4
        for household in legacy["households"].values():
            for key in (
                    "previous_settlement_id", "last_migration_turn",
                    "last_migration_reason", "migration_count"):
                household.pop(key, None)
        upgraded = residents.upgrade_resident_registry(legacy)
        self.assertEqual(
            set(upgraded["residents"]), set(current["residents"]))
        self.assertEqual(
            upgraded["version"], residents.RESIDENT_REGISTRY_VERSION)
        self.assertTrue(all(
            row["last_migration_turn"] is None
            and row["migration_count"] == 0
            for row in upgraded["households"].values()))

    def test_version_four_activity_breakdown_remains_strict_during_upgrade(self):
        settlements = initial_settlement_network(HOME_ECONOMY)
        legacy = residents.initial_resident_registry(
            settlements, world_seed=3)
        legacy["version"] = 4
        household = next(iter(legacy["households"].values()))
        household["activity_count"] = 3
        household["activity_counts_by_good"] = {
            "food": 2, "medicine": 0, "shelter": 0, "tools": 0}
        household["unclassified_activity_count"] = 0
        with self.assertRaisesRegex(
                ValueError, "activity count breakdown does not match total"):
            residents.upgrade_resident_registry(legacy)


class HouseholdMigrationWorldIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = game.collect_visualize_trace(
            7, "cautious", 240, game.SAFETY_FLOOR, "health",
            world_mode=True, include_resume_state=True)

    def test_persistent_shortage_moves_real_households_and_preserves_ledgers(self):
        data = self.data
        population_migrations = [
            row for row in data["trace"]["population_events"]
            if row.get("kind") == "population_migrated"]
        resident_migrations = [
            row for row in data["trace"]["resident_events"]
            if row.get("kind") == "residents_migrated"]
        shortage_migrations = [
            row for row in resident_migrations
            if int(row.get("shortage_migrant_count", 0)) > 0]
        self.assertTrue(any(
            row.get("preferred_household_ids")
            for row in population_migrations))
        self.assertTrue(shortage_migrations)
        self.assertTrue(all(
            int(row["shortage_migrant_count"]) == sum(
                member.get("migration_reason")
                == "persistent_household_shortage"
                for member in row.get("residents", ()))
            for row in shortage_migrations))
        self.assertTrue(residents.registry_matches_settlements(
            data["resident_registry"], data["settlement_states"]))
        self.assertTrue(household_goods.verify_household_goods_state(
            data["household_goods_state"], data["settlement_states"],
            data["resident_registry"], data["household_needs_state"],
            data["organization_state"]))
        self.assertEqual(
            data["resume_state"]["resident_registry"],
            data["resident_registry"])
        dashboard = build_dashboard_data(data, 12)
        self.assertTrue(any(
            int(row.get("migration_count", 0)) > 0
            and row.get("last_migration_reason")
            for row in dashboard["households"]))
        self.assertTrue(any(
            row.get("kind") == "residents_migrated"
            and int(row.get("shortage_migrant_count", 0)) > 0
            for row in dashboard["observer_events"]))


if __name__ == "__main__":
    unittest.main()
