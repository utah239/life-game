# -*- coding: utf-8 -*-
"""遠景水槽の匿名イベント密度projection。"""
import copy
import random
import unittest

from dashboard import event_density


class EventDensityHistoryTest(unittest.TestCase):
    def setUp(self):
        self.population_events = [
            {"turn": 2, "kind": "population_changed",
             "settlement_id": "b", "births": 1, "deaths": 0,
             "population": 8, "reproductive_population": 3},
            {"turn": 1, "kind": "population_changed",
             "settlement_id": "a", "births": 2, "deaths": 0,
             "population": 11, "reproductive_population": 5},
            {"turn": 1, "kind": "population_changed",
             "settlement_id": "a", "births": 0, "deaths": 1,
             "population": 10, "reproductive_population": 4},
            {"turn": 3, "kind": "population_migrated",
             "from_settlement": "a", "to_settlement": "b",
             "migrants": 2, "reproductive_migrants": 1},
            {"turn": 3, "kind": "population_migrated",
             "from_settlement": "a", "to_settlement": "b",
             "migrants": 3, "reproductive_migrants": 2},
            {"turn": 4, "kind": "settlement_population_declining",
             "settlement_id": "a"},
        ]
        self.resident_events = [
            {"turn": 1, "kind": "household_activity",
             "settlement_id": "a", "activity": "food",
             "responding_to_shortage": False},
            {"turn": 1, "kind": "household_activity",
             "settlement_id": "a", "activity": "food",
             "responding_to_shortage": True},
            {"turn": 2, "kind": "household_activity",
             "settlement_id": "b", "livelihood": "medicine"},
            {"turn": 2, "kind": "household_activity",
             "settlement_id": "b", "activity": "future_activity"},
            {"turn": 2, "kind": "resident_born",
             "settlement_id": "b"},
        ]

    def test_compact_catalog_rows_and_aggregation(self):
        result = event_density.build_event_density_history(
            self.population_events, self.resident_events)
        self.assertEqual(result, {
            "version": 1,
            # ID catalog order is first observation order; row order is time.
            "settlement_ids": ["b", "a"],
            "activity_keys": [
                "food", "medicine", "shelter", "tools", "unknown"],
            "population": [
                [1, 1, 2, 1, 10, 4],
                [2, 0, 1, 0, 8, 3],
            ],
            "migrations": [[3, 1, 0, 5, 3]],
            "activities": [
                [1, 1, 0, 2, 1],
                [2, 0, 1, 1, 0],
                [2, 0, 4, 1, 0],
            ],
        })

    def test_flow_totals_are_preserved(self):
        result = event_density.build_event_density_history(
            self.population_events, self.resident_events)
        self.assertEqual(
            sum(row[2] for row in result["population"]),
            sum(row.get("births", 0) for row in self.population_events
                if row["kind"] == "population_changed"))
        self.assertEqual(
            sum(row[3] for row in result["population"]),
            sum(row.get("deaths", 0) for row in self.population_events
                if row["kind"] == "population_changed"))
        self.assertEqual(
            sum(row[3] for row in result["migrations"]),
            sum(row.get("migrants", 0) for row in self.population_events
                if row["kind"] == "population_migrated"))
        self.assertEqual(
            sum(row[3] for row in result["activities"]), 4)
        self.assertEqual(
            sum(row[4] for row in result["activities"]), 1)

    def test_production_activity_uses_worker_count_not_event_count(self):
        result = event_density.build_event_density_history([], [{
            "turn": 5, "kind": "production_activity",
            "settlement_id": "a", "activity": "tools",
            "worker_count": 37, "gross_output": 2.4,
        }, {
            "turn": 5, "kind": "production_activity",
            "settlement_id": "a", "activity": "tools",
            "worker_count": 5, "gross_output": 0.5,
        }])
        self.assertEqual(result["activities"], [[5, 0, 3, 42, 0]])

    def test_empty_and_unassigned_inputs(self):
        self.assertEqual(
            event_density.build_event_density_history([], []), {
                "version": 1, "settlement_ids": [],
                "activity_keys": [
                    "food", "medicine", "shelter", "tools", "unknown"],
                "population": [], "migrations": [], "activities": [],
            })
        result = event_density.build_event_density_history([{
            "turn": 1, "kind": "population_changed", "population": 1,
        }], None)
        self.assertEqual(result["settlement_ids"], ["unassigned"])

    def test_inputs_and_rng_are_not_changed(self):
        population_before = copy.deepcopy(self.population_events)
        residents_before = copy.deepcopy(self.resident_events)
        rng_before = random.getstate()
        event_density.build_event_density_history(
            self.population_events, self.resident_events)
        self.assertEqual(self.population_events, population_before)
        self.assertEqual(self.resident_events, residents_before)
        self.assertEqual(random.getstate(), rng_before)


class ObserverDetailThresholdTest(unittest.TestCase):
    def setUp(self):
        self.events = [
            {"t": 1, "kind": "resident_born", "resident_id": "r1"},
            {"t": 1, "kind": "resident_died", "resident_id": "r0"},
            {"t": 1, "kind": "household_activity", "household_id": "h1"},
            {"t": 1, "kind": "residents_migrated", "residents": []},
            {"t": 1, "kind": "household_split"},
            {"t": 1, "kind": "household_closed"},
            {"t": 1, "kind": "production_activity", "worker_count": 999},
            {"t": 1, "kind": "population_changed", "population": 10001},
            {"t": 1, "kind": "institution_transition", "institution": "bank"},
        ]

    def test_limit_is_inclusive_and_preserves_full_event_identity(self):
        result, mode = event_density.observer_events_for_population(
            self.events, 10_000)
        self.assertIs(result, self.events)
        self.assertEqual(mode, "full")

    def test_above_limit_removes_only_individual_events(self):
        result, mode = event_density.observer_events_for_population(
            self.events, 10_001)
        self.assertEqual(mode, "density")
        self.assertEqual([row["kind"] for row in result], [
            "production_activity", "population_changed",
            "institution_transition"])

    def test_filter_does_not_mutate_input_or_consume_rng(self):
        before = copy.deepcopy(self.events)
        rng_before = random.getstate()
        event_density.observer_events_for_population(
            self.events, 10_001, limit=10_000)
        self.assertEqual(self.events, before)
        self.assertEqual(random.getstate(), rng_before)


if __name__ == "__main__":
    unittest.main()
