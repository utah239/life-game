# -*- coding: utf-8 -*-
"""永続空間状態の純粋ルールと住民台帳との同期境界。"""
import copy
import json
import random
import unittest

from institutions import (
    activity_communities, residents, settlement_network, spatial,
)


HOME_ECONOMY = {
    "food": 90.0, "medicine": 92.0, "shelter": 70.0, "tools": 65.0,
    "production_capacity": 60.0, "barter_stage": 0,
    "community_trust": 50.0, "local_credit_stage": 0,
}


class SpatialStateRulesTest(unittest.TestCase):
    def setUp(self):
        self.settlements = settlement_network.initial_settlement_network(
            HOME_ECONOMY)
        self.registry = residents.initial_resident_registry(
            self.settlements, world_seed=23)

    def test_initial_state_is_deterministic_complete_and_rng_free(self):
        registry_before = copy.deepcopy(self.registry)
        rng_before = random.getstate()

        first = spatial.initial_spatial_state(self.registry, turn=1)
        second = spatial.initial_spatial_state(self.registry, turn=1)

        self.assertEqual(first, second)
        self.assertTrue(spatial.spatial_state_matches_registry(
            first, self.registry))
        self.assertEqual(
            len(first["residents"]),
            len(residents.living_residents(self.registry)))
        self.assertEqual(
            len([row for row in first["sites"].values()
                 if row["active"]]),
            residents.active_household_count(self.registry))
        self.assertEqual(random.getstate(), rng_before)
        self.assertEqual(self.registry, registry_before)
        json.dumps(first)

    def test_geometry_does_not_use_legacy_settlement_ids(self):
        remapped = copy.deepcopy(self.registry)
        mapping = {
            settlement_id: f"account-{index}"
            for index, settlement_id in enumerate(self.settlements, 1)}
        for household in remapped["households"].values():
            household["settlement_id"] = mapping[household["settlement_id"]]
        for resident in remapped["residents"].values():
            resident["settlement_id"] = mapping[resident["settlement_id"]]

        original = spatial.initial_spatial_state(self.registry, turn=1)
        changed = spatial.initial_spatial_state(remapped, turn=1)

        geometry = lambda state: {
            key: (row["x"], row["y"], row["home_x"], row["home_y"])
            for key, row in state["sites"].items()}
        self.assertEqual(geometry(original), geometry(changed))
        self.assertNotEqual(
            {row["account_id"] for row in original["sites"].values()},
            {row["account_id"] for row in changed["sites"].values()})

    def test_birth_death_and_migration_are_synchronized_without_rng(self):
        state = spatial.initial_spatial_state(self.registry, turn=1)
        focus_id = residents.select_focus_resident(self.registry, "home", 1)
        changed = residents.apply_population_change(
            self.registry, "home", births=2, deaths=1, turn=12,
            protected_resident_ids=(focus_id,))["registry"]
        migrated = residents.apply_migration(changed, {
            "from_settlement": "home", "to_settlement": "riverside",
            "migrants": 3, "reproductive_migrants": 1,
        }, 12, protected_resident_ids=(focus_id,))["registry"]
        rng_before = random.getstate()

        updated = spatial.synchronize_spatial_state(state, migrated, 12)

        self.assertTrue(spatial.spatial_state_matches_registry(
            updated, migrated))
        self.assertEqual(set(updated["residents"]), {
            row["id"] for row in residents.living_residents(migrated)})
        self.assertTrue(any(
            site["founded_turn"] == 12
            for site in updated["sites"].values()))
        self.assertEqual(random.getstate(), rng_before)
        self.assertTrue(spatial.spatial_state_matches_registry(
            state, self.registry))

    def test_only_recorded_actor_receives_commute_episode(self):
        state = spatial.initial_spatial_state(self.registry, turn=1)
        registry = copy.deepcopy(self.registry)
        household = next(iter(registry["households"].values()))
        members = sorted((
            row for row in registry["residents"].values()
            if row.get("alive", True)
            and row["household_id"] == household["id"]
        ), key=lambda row: row["id"])
        actor = members[0]
        actor["last_activity_turn"] = 12
        actor["last_activity"] = "tools"
        household["last_activity_turn"] = 12
        household["last_activity"] = "tools"
        household["last_actor_id"] = actor["id"]
        rng_before = random.getstate()

        updated = spatial.synchronize_spatial_state(state, registry, 12)

        primary = updated["residents"][actor["id"]]
        self.assertEqual(primary["activity_mode"], "primary")
        self.assertEqual(primary["activity"], "tools")
        self.assertLess(primary["departure_phase"], primary["return_phase"])
        self.assertTrue(all(
            updated["residents"][row["id"]]["activity_mode"] == "home"
            for row in members[1:]))
        self.assertEqual(random.getstate(), rng_before)

    def test_activity_site_can_move_without_moving_home_or_resident_anchors(self):
        state = spatial.initial_spatial_state(self.registry, turn=1)
        site_id = next(iter(state["sites"]))
        site = state["sites"][site_id]
        household_id = site["household_id"]
        home_before = (site["home_x"], site["home_y"])
        residents_before = {
            key: (row["x"], row["y"])
            for key, row in state["residents"].items()
            if row["household_id"] == household_id
        }
        moving = copy.deepcopy(state)
        moving["sites"][site_id]["target_x"] = min(
            0.96, moving["sites"][site_id]["x"] + 0.2)
        moving["sites"][site_id]["target_y"] = min(
            0.96, moving["sites"][site_id]["y"] + 0.1)

        updated = spatial.synchronize_spatial_state(
            moving, self.registry, turn=2)

        self.assertNotEqual(
            (updated["sites"][site_id]["x"],
             updated["sites"][site_id]["y"]),
            (state["sites"][site_id]["x"], state["sites"][site_id]["y"]))
        self.assertEqual(
            (updated["sites"][site_id]["home_x"],
             updated["sites"][site_id]["home_y"]), home_before)
        self.assertEqual({
            key: (row["x"], row["y"])
            for key, row in updated["residents"].items()
            if row["household_id"] == household_id
        }, residents_before)

    def test_explicit_account_change_rehomes_once_then_stays_fixed(self):
        state = spatial.initial_spatial_state(self.registry, turn=1)
        registry = copy.deepcopy(self.registry)
        households = list(registry["households"].values())
        moving = households[0]
        destination = next(
            row for row in households
            if row["settlement_id"] != moving["settlement_id"])
        site_id = f"site:{moving['id']}"
        home_before = (
            state["sites"][site_id]["home_x"],
            state["sites"][site_id]["home_y"])
        moving["settlement_id"] = destination["settlement_id"]
        for resident in registry["residents"].values():
            if resident["household_id"] == moving["id"]:
                resident["settlement_id"] = destination["settlement_id"]

        migrated = spatial.synchronize_spatial_state(state, registry, 2)
        migrated_home = (
            migrated["sites"][site_id]["home_x"],
            migrated["sites"][site_id]["home_y"])
        stable = spatial.synchronize_spatial_state(migrated, registry, 3)

        self.assertNotEqual(migrated_home, home_before)
        self.assertEqual((
            stable["sites"][site_id]["home_x"],
            stable["sites"][site_id]["home_y"]), migrated_home)

    def test_clusters_are_exact_partition_of_active_sites(self):
        state = spatial.initial_spatial_state(self.registry, turn=1)
        clustered_site_ids = [
            site_id for cluster in state["clusters"].values()
            for site_id in cluster["site_ids"]]
        active_site_ids = [
            site_id for site_id, row in state["sites"].items()
            if row["active"]]

        self.assertCountEqual(clustered_site_ids, active_site_ids)
        self.assertEqual(
            sum(row["resident_count"] for row in state["clusters"].values()),
            len(state["residents"]))
        self.assertEqual(len(clustered_site_ids), len(set(clustered_site_ids)))
        self.assertEqual(
            set(state["clusters"]), {
                key for key, row in state["cluster_lineages"].items()
                if row["active"]})
        self.assertEqual(
            len([event for event in state["cluster_events"]
                 if event["kind"] == "cluster_formed"]),
            len(state["clusters"]))

    def test_cluster_lineage_survives_split_and_merge_without_account_geometry(self):
        state = spatial.initial_spatial_state(self.registry, turn=1)
        # 全活動点を一つへ集め、追跡開始時点を一つの集落にする。
        together = copy.deepcopy(state)
        for site in together["sites"].values():
            site["x"] = site["target_x"] = 0.5
            site["y"] = site["target_y"] = 0.5
        merged = spatial.synchronize_spatial_state(
            together, self.registry, turn=2)
        self.assertEqual(len(merged["clusters"]), 1)
        original_id = next(iter(merged["clusters"]))

        # legacy settlement/accountを変えず、座標だけで二つへ分かれる。
        separated = copy.deepcopy(merged)
        site_ids = sorted(separated["sites"])
        for index, site_id in enumerate(site_ids):
            site = separated["sites"][site_id]
            site["x"] = site["target_x"] = 0.24 if index % 2 else 0.76
            site["y"] = site["target_y"] = 0.5
        split = spatial.synchronize_spatial_state(
            separated, self.registry, turn=3)
        self.assertEqual(len(split["clusters"]), 2)
        self.assertIn(original_id, split["clusters"])
        split_event = split["cluster_events"][-1]
        self.assertEqual(split_event["kind"], "cluster_split")
        self.assertEqual(split_event["source_cluster_id"], original_id)
        self.assertCountEqual(split_event["cluster_ids"], split["clusters"])
        child_id = next(key for key in split["clusters"] if key != original_id)
        self.assertEqual(
            split["cluster_lineages"][child_id]["parent_ids"], [original_id])

        reunited = copy.deepcopy(split)
        for site in reunited["sites"].values():
            site["x"] = site["target_x"] = 0.5
            site["y"] = site["target_y"] = 0.5
        after = spatial.synchronize_spatial_state(
            reunited, self.registry, turn=4)
        self.assertEqual(set(after["clusters"]), {original_id})
        self.assertEqual(after["cluster_events"][-1]["kind"], "cluster_merged")
        self.assertEqual(
            after["cluster_lineages"][child_id]["merged_into"], original_id)
        self.assertFalse(after["cluster_lineages"][child_id]["active"])
        self.assertEqual(state["updated_turn"], 1)

    def test_site_transfer_between_existing_clusters_is_not_lineage_churn(self):
        state = spatial.initial_spatial_state(self.registry, turn=1)
        self.assertGreaterEqual(len(state["clusters"]), 2)

        # 各既存クラスタを十分に密な点へまとめ、1世帯だけを別の既存
        # クラスタへ移す。これは境界を越えた移住であり、新しい集落の誕生や
        # 古い集落全体の吸収ではない。
        dense = copy.deepcopy(state)
        for cluster in state["clusters"].values():
            for site_id in cluster["site_ids"]:
                site = dense["sites"][site_id]
                site["x"] = site["target_x"] = cluster["centroid_x"]
                site["y"] = site["target_y"] = cluster["centroid_y"]
        stable = spatial.synchronize_spatial_state(
            dense, self.registry, turn=2)
        source_id = max(
            stable["clusters"],
            key=lambda key: stable["clusters"][key]["site_count"])
        destination_id = next(
            key for key in sorted(stable["clusters"]) if key != source_id)
        moving_site_id = stable["clusters"][source_id]["site_ids"][-1]
        destination = stable["clusters"][destination_id]
        moved = copy.deepcopy(stable)
        moving_site = moved["sites"][moving_site_id]
        moving_site["x"] = moving_site["target_x"] = destination["centroid_x"]
        moving_site["y"] = moving_site["target_y"] = destination["centroid_y"]
        event_count = len(stable["cluster_events"])

        after = spatial.synchronize_spatial_state(
            moved, self.registry, turn=3)

        self.assertEqual(set(after["clusters"]), set(stable["clusters"]))
        self.assertNotIn(
            moving_site_id, after["clusters"][source_id]["site_ids"])
        self.assertIn(
            moving_site_id, after["clusters"][destination_id]["site_ids"])
        self.assertFalse(any(
            event["kind"] in ("cluster_split", "cluster_merged")
            for event in after["cluster_events"][event_count:]))

    def test_version_one_state_upgrades_without_consuming_rng(self):
        legacy = spatial.initial_spatial_state(self.registry, turn=1)
        legacy["version"] = 1
        for key in ("cluster_lineages", "cluster_events", "next_cluster_serial"):
            legacy.pop(key)
        rng_before = random.getstate()

        upgraded = spatial.synchronize_spatial_state(
            legacy, self.registry, turn=2)

        self.assertEqual(upgraded["version"], spatial.SPATIAL_STATE_VERSION)
        self.assertTrue(upgraded["cluster_lineages"])
        self.assertTrue(all(
            event["reason"] == "legacy_upgrade"
            for event in upgraded["cluster_events"]
            if event["kind"] == "cluster_formed"))
        self.assertEqual(random.getstate(), rng_before)

    def test_compaction_tracks_compacted_registry(self):
        state = spatial.initial_spatial_state(self.registry, turn=1)
        victim = residents.living_residents(self.registry, "upland")[0]
        dead = residents.mark_resident_died(
            self.registry, victim["id"], 10, "background")["registry"]
        updated = spatial.synchronize_spatial_state(state, dead, 10)
        compact_registry = residents.compact_registry(dead, cutoff_turn=20)

        compact = spatial.compact_spatial_state(
            updated, compact_registry)

        self.assertNotIn(victim["id"], compact["residents"])
        self.assertTrue(spatial.spatial_state_matches_registry(
            compact, compact_registry))
        self.assertEqual(
            sum(row["resident_count"] for row in compact["clusters"].values()),
            len(compact["residents"]))

    def test_keyframe_is_compact_deterministic_and_rng_free(self):
        state = spatial.initial_spatial_state(self.registry, turn=1)
        before = copy.deepcopy(state)
        rng_before = random.getstate()

        frame = spatial.build_spatial_keyframe(state)

        self.assertEqual(frame, spatial.build_spatial_keyframe(state, 1))
        self.assertEqual(frame["turn"], 1)
        self.assertEqual(len(frame["sites"]), len([
            row for row in state["sites"].values() if row["active"]]))
        self.assertEqual(len(frame["residents"]), len(state["residents"]))
        self.assertEqual(len(frame["clusters"]), len(state["clusters"]))
        self.assertTrue(all(len(row) == 17 for row in frame["sites"]))
        self.assertEqual(
            sum(row[10] for row in frame["sites"]),
            sum(row["population_weight"] for row in state["sites"].values()
                if row["active"]))
        self.assertTrue(all(
            row[11] is None and row[12:15] == [0, 0, 0]
            and row[15] == {} and row[16] == {}
            for row in frame["sites"]))
        self.assertTrue(all(len(row) == 9 for row in frame["residents"]))
        self.assertTrue(all(
            row[5] in ("home", "routine", "primary")
            and 0 <= row[7] < row[8] <= 1
            for row in frame["residents"]))
        self.assertTrue(all(len(row) == 10 for row in frame["clusters"]))
        self.assertTrue(all(isinstance(row[8], bool)
                            for row in frame["clusters"]))
        self.assertTrue(all(row[9] for row in frame["clusters"]))
        self.assertEqual(frame["version"], spatial.SPATIAL_KEYFRAME_VERSION)
        self.assertEqual(state, before)
        self.assertEqual(random.getstate(), rng_before)
        json.dumps(frame)

    def test_version_three_state_upgrades_with_safe_home_episodes(self):
        state = spatial.initial_spatial_state(self.registry, turn=1)
        legacy = copy.deepcopy(state)
        legacy["version"] = spatial.PIONEERING_SPATIAL_STATE_VERSION
        for row in legacy["residents"].values():
            for key in ("activity_mode", "activity", "departure_phase",
                        "return_phase"):
                row.pop(key, None)
        before = copy.deepcopy(legacy)
        rng_before = random.getstate()

        upgraded = spatial._upgrade_spatial_state(legacy, turn=2)

        self.assertEqual(set(upgraded["residents"]), set(legacy["residents"]))
        self.assertTrue(all(
            row["activity_mode"] == "home"
            and row["departure_phase"] == 0.0
            and row["return_phase"] == 1.0
            for row in upgraded["residents"].values()))
        self.assertEqual(legacy, before)
        self.assertEqual(random.getstate(), rng_before)

    def test_version_four_state_builds_keyframe_with_safe_activity_defaults(self):
        legacy = spatial.initial_spatial_state(self.registry, turn=1)
        legacy["version"] = spatial.ACTIVITY_EPISODE_SPATIAL_STATE_VERSION
        for site in legacy["sites"].values():
            for key in (
                    "last_activity_turn", "activity_worker_count",
                    "activity_named_worker_count",
                    "activity_anonymous_worker_count",
                    "activity_output_by_good",
                    "activity_worker_count_by_good"):
                site.pop(key, None)

        frame = spatial.build_spatial_keyframe(legacy)

        self.assertTrue(frame["sites"])
        self.assertTrue(all(
            row[11] is None and row[12:15] == [0, 0, 0]
            and row[15] == {} and row[16] == {}
            for row in frame["sites"]))

    def test_version_five_state_upgrades_with_per_good_worker_defaults(self):
        legacy = spatial.initial_spatial_state(self.registry, turn=1)
        legacy["version"] = spatial.SITE_ACTIVITY_SPATIAL_STATE_VERSION
        for site in legacy["sites"].values():
            site.pop("activity_worker_count_by_good", None)
        before = copy.deepcopy(legacy)

        upgraded = spatial._upgrade_spatial_state(legacy, turn=2)

        self.assertEqual(legacy, before)
        self.assertEqual(upgraded["version"], spatial.SPATIAL_STATE_VERSION)
        self.assertTrue(all(
            site["activity_worker_count_by_good"] == {}
            for site in upgraded["sites"].values()))

    def _pioneering_fixture(self, turn=24):
        state = spatial.initial_spatial_state(self.registry, turn=1)
        cluster_id = max(
            state["clusters"],
            key=lambda key: state["clusters"][key]["site_count"])
        site_id = state["clusters"][cluster_id]["site_ids"][0]
        household_id = state["sites"][site_id]["household_id"]
        registry = copy.deepcopy(self.registry)
        household = registry["households"][household_id]
        actor = next(
            row for row in registry["residents"].values()
            if row.get("alive", True)
            and row["household_id"] == household_id)
        household.update({
            "activity_count": 10,
            "last_activity_turn": turn,
            "last_activity": household["livelihood"],
            "last_actor_id": actor["id"],
        })
        actor["last_activity_turn"] = turn
        actor["last_activity"] = household["livelihood"]
        return state, registry, site_id, cluster_id

    def test_version_two_checkpoint_recovers_pioneering_sequence_and_accounting(self):
        state, registry, _, _ = self._pioneering_fixture()
        started = spatial.synchronize_spatial_state(
            state, registry, spatial.PIONEERING_FIRST_TURN)
        legacy = copy.deepcopy(started)
        legacy["version"] = spatial.CLUSTERED_SPATIAL_STATE_VERSION
        legacy.pop("pioneering_sequence")
        legacy.pop("last_pioneering_turn")
        for cluster in legacy["clusters"].values():
            for key in ("provisional", "accounting_parent_id",
                        "pioneering_site_count"):
                cluster.pop(key, None)

        expected = spatial.synchronize_spatial_state(
            started, registry, spatial.PIONEERING_FIRST_TURN + 1)
        upgraded = spatial.synchronize_spatial_state(
            legacy, registry, spatial.PIONEERING_FIRST_TURN + 1)

        self.assertEqual(upgraded, expected)
        self.assertEqual(upgraded["version"], spatial.SPATIAL_STATE_VERSION)
        self.assertEqual(upgraded["pioneering_sequence"], 1)
        self.assertEqual(
            upgraded["last_pioneering_turn"], spatial.PIONEERING_FIRST_TURN)
        self.assertTrue(all(
            "provisional" in row and "accounting_parent_id" in row
            for row in upgraded["clusters"].values()))

    def test_pioneering_starts_from_real_activity_deterministically_and_rng_free(self):
        state, registry, site_id, cluster_id = self._pioneering_fixture()
        state_before = copy.deepcopy(state)
        registry_before = copy.deepcopy(registry)
        rng_before = random.getstate()

        first = spatial.synchronize_spatial_state(state, registry, 24)
        second = spatial.synchronize_spatial_state(state, registry, 24)

        self.assertEqual(first, second)
        started = [
            event for event in first["cluster_events"]
            if event["kind"] == "pioneering_started"]
        self.assertEqual(len(started), 1)
        event = started[0]
        self.assertEqual(event["source_cluster_id"], cluster_id)
        self.assertEqual(event["site_id"], site_id)
        self.assertEqual(
            event["resident_id"],
            registry["households"][event["household_id"]]["last_actor_id"])
        self.assertNotEqual(
            (event["x"], event["y"]),
            (event["target_x"], event["target_y"]))
        self.assertNotEqual(
            (first["sites"][site_id]["x"], first["sites"][site_id]["y"]),
            (state["sites"][site_id]["x"], state["sites"][site_id]["y"]))
        self.assertTrue(spatial.spatial_state_matches_registry(
            first, registry))
        self.assertEqual(state, state_before)
        self.assertEqual(registry, registry_before)
        self.assertEqual(random.getstate(), rng_before)

    def test_pioneering_activity_creates_a_provisional_outpost_without_population_loss(self):
        state, registry, site_id, source_cluster_id = (
            self._pioneering_fixture())
        after = spatial.synchronize_spatial_state(
            state, registry, spatial.PIONEERING_FIRST_TURN)
        for turn in range(spatial.PIONEERING_FIRST_TURN + 1,
                          spatial.PIONEERING_FIRST_TURN + 20):
            after = spatial.synchronize_spatial_state(
                after, registry, turn)
            if any(event["kind"] == "pioneering_settled"
                   for event in after["cluster_events"]):
                break

        settled = [
            event for event in after["cluster_events"]
            if event["kind"] == "pioneering_settled"]
        self.assertEqual(len(settled), 1)
        self.assertEqual(settled[0]["site_id"], site_id)
        self.assertEqual(
            settled[0]["source_cluster_id"], source_cluster_id)
        self.assertNotEqual(
            settled[0]["cluster_id"], source_cluster_id)
        self.assertTrue(any(
            event["kind"] == "cluster_split"
            for event in after["cluster_events"]))
        self.assertGreater(len(after["clusters"]), len(state["clusters"]))

        ledger = activity_communities.build_activity_community_ledger(
            after, registry, self.settlements, turn)
        promoted = activity_communities.promote_activity_communities(
            ledger, registry)
        self.assertTrue(
            activity_communities.activity_community_ledger_matches_sources(
                ledger, after, registry, self.settlements))
        self.assertEqual(len(ledger["provisional_clusters"]), 1)
        provisional_id = settled[0]["cluster_id"]
        self.assertIn(provisional_id, ledger["provisional_clusters"])
        self.assertNotIn(provisional_id, promoted["settlements"])
        frame = spatial.build_spatial_keyframe(after, turn)
        provisional_row = next(
            row for row in frame["clusters"] if row[0] == provisional_id)
        self.assertTrue(provisional_row[8])
        self.assertEqual(provisional_row[9], source_cluster_id)
        self.assertEqual(
            set(promoted["settlements"]),
            spatial.accounting_activity_cluster_ids(after))
        self.assertEqual(
            sum(row["population"] for row in promoted["settlements"].values()),
            sum(row["population"] for row in self.settlements.values()))

    def test_three_pioneering_sites_form_one_accounting_community(self):
        state = spatial.initial_spatial_state(self.registry, turn=1)
        source_cluster_id = max(
            state["clusters"],
            key=lambda key: state["clusters"][key]["site_count"])
        registry = copy.deepcopy(self.registry)
        settlements = copy.deepcopy(self.settlements)
        last_ledger = None

        for start_turn in (24, 264, 504):
            site_id = next(
                site_id
                for site_id in state["clusters"][source_cluster_id]["site_ids"]
                if state["sites"][site_id].get(
                    "pioneering_started_turn") is None)
            household_id = state["sites"][site_id]["household_id"]
            household = registry["households"][household_id]
            actor = next(
                row for row in registry["residents"].values()
                if row.get("alive", True)
                and row["household_id"] == household_id)
            household.update({
                "activity_count": start_turn,
                "last_activity_turn": start_turn,
                "last_activity": household["livelihood"],
                "last_actor_id": actor["id"],
            })
            actor["last_activity_turn"] = start_turn
            actor["last_activity"] = household["livelihood"]
            for turn in range(start_turn, start_turn + 12):
                state = spatial.synchronize_spatial_state(
                    state, registry, turn)
                last_ledger = (
                    activity_communities.build_activity_community_ledger(
                        state, registry, settlements, turn))
                promoted = activity_communities.promote_activity_communities(
                    last_ledger, registry)
                settlements = promoted["settlements"]
                registry = promoted["registry"]
                state = spatial.relabel_spatial_accounts(state, registry)

        formed = [
            event for event in state["cluster_events"]
            if event["kind"] == "pioneering_community_formed"]
        self.assertEqual(len(formed), 1)
        community_id = formed[0]["cluster_id"]
        self.assertIn(community_id, settlements)
        self.assertNotIn(community_id, last_ledger["provisional_clusters"])
        self.assertEqual(
            state["clusters"][community_id]["site_count"],
            spatial.PIONEERING_COMMUNITY_SITE_THRESHOLD)
        self.assertGreaterEqual(
            state["clusters"][community_id]["resident_count"],
            spatial.PIONEERING_COMMUNITY_POPULATION_THRESHOLD)
        self.assertEqual(
            sum(row["population"] for row in settlements.values()),
            sum(row["population"] for row in self.settlements.values()))
        self.assertEqual(
            set(settlements), spatial.accounting_activity_cluster_ids(state))
        self.assertEqual(
            sum(row["population_weight"] for row in state["sites"].values()
                if row.get("active", True)),
            sum(row["population"] for row in self.settlements.values()))

    def test_pioneering_global_cooldown_prevents_event_chatter(self):
        state, registry, _, _ = self._pioneering_fixture()
        first = spatial.synchronize_spatial_state(state, registry, 24)
        changed = copy.deepcopy(registry)
        for household in changed["households"].values():
            household["last_activity_turn"] = 25
            household["activity_count"] = 99
        after = spatial.synchronize_spatial_state(first, changed, 25)

        self.assertEqual(len([
            event for event in after["cluster_events"]
            if event["kind"] == "pioneering_started"]), 1)
        self.assertEqual(after["last_pioneering_turn"], 24)


if __name__ == "__main__":
    unittest.main()
