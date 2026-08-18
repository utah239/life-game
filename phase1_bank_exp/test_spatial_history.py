# -*- coding: utf-8 -*-
"""空間keyframe差分履歴の可逆性・順序・容量。"""
import copy
import json
import unittest

from dashboard import spatial_history


def reconstruct(history: dict) -> list:
    state = {"sites": {}, "residents": {}, "clusters": {}}
    frames = []
    for delta in history["frames"]:
        for name in state:
            changes = delta.get(name, {})
            for key in changes.get("remove", []):
                state[name].pop(key, None)
            for row in changes.get("upsert", []):
                state[name][str(row[0])] = row
        frames.append({
            "turn": delta["turn"],
            **{name: list(rows.values()) for name, rows in state.items()},
        })
    return frames


class SpatialHistoryTest(unittest.TestCase):
    def test_round_trips_add_change_remove_without_mutation(self):
        frames = [
            {"turn": 1,
             "sites": [["s1", "h1", "food", "c1", .1, .2, .1, .2, 1]],
             "residents": [["r1", "s1", "h1", .1, .2]],
             "clusters": [["c1", .1, .2, 1, 1, 1, [], ["s1"]]]},
            {"turn": 12,
             "sites": [
                 ["s1", "h1", "food", "c1", .1, .2, .1, .2, 1],
                 ["s2", "h2", "tools", "c1", .4, .5, .4, .5, 12]],
             "residents": [
                 ["r1", "s2", "h1", .4, .5],
                 ["r2", "s2", "h2", .41, .51]],
             "clusters": [["c1", .25, .35, 2, 2, 1, [], ["s1", "s2"]]]},
            {"turn": 24,
             "sites": [["s2", "h2", "tools", "c2", .4, .5, .4, .5, 12]],
             "residents": [["r2", "s2", "h2", .41, .51]],
             "clusters": [["c2", .4, .5, 1, 1, 24, ["c1"], ["s2"]]]},
        ]
        before = copy.deepcopy(frames)
        history = spatial_history.build_spatial_history(frames)
        self.assertEqual(frames, before)
        self.assertEqual(history["version"], 1)
        self.assertEqual(history["source_frame_count"], 3)
        self.assertEqual(reconstruct(history), frames)
        self.assertEqual(
            history["frames"][1]["sites"]["upsert"],
            [frames[1]["sites"][1]])
        self.assertEqual(
            history["frames"][2]["residents"]["remove"], ["r1"])
        self.assertEqual(
            history["frames"][2]["clusters"]["remove"], ["c1"])

    def test_omits_unchanged_collections(self):
        frame = {
            "turn": 1, "sites": [["s1"]],
            "residents": [["r1"]], "clusters": [["c1"]]}
        history = spatial_history.build_spatial_history([
            frame, {**frame, "turn": 2}])
        self.assertEqual(history["frames"][1], {"turn": 2})

    def test_rejects_non_monotonic_turns_and_duplicate_ids(self):
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            spatial_history.build_spatial_history([
                {"turn": 2}, {"turn": 2}])
        with self.assertRaisesRegex(ValueError, "duplicate residents"):
            spatial_history.build_spatial_history([{
                "turn": 1, "residents": [["r1"], ["r1"]]}])

    def test_delta_size_tracks_changes_not_population_times_frames(self):
        resident_count = 1000
        residents = [[f"r{index}", "s1", "h1", index / resident_count, .5]
                     for index in range(resident_count)]
        frames = []
        for turn in range(1, 101):
            rows = list(residents)
            changed = list(rows[turn % resident_count])
            changed[3] = turn / 100
            rows[turn % resident_count] = changed
            frames.append({
                "turn": turn,
                "sites": [["s1", "h1", "food", "c1", .5, .5, .5, .5, 1]],
                "residents": rows,
                "clusters": [["c1", .5, .5, 1, resident_count, 1, [], ["s1"]]],
            })
            residents = rows
        history = spatial_history.build_spatial_history(frames)
        full_size = len(json.dumps(frames, separators=(",", ":")).encode())
        delta_size = len(json.dumps(
            history, separators=(",", ":")).encode())
        self.assertLess(delta_size, full_size * 0.03)
        self.assertEqual(reconstruct(history), frames)

    def test_empty_history_is_versioned(self):
        self.assertEqual(spatial_history.build_spatial_history([]), {
            "version": 1, "source_frame_count": 0, "frames": []})


if __name__ == "__main__":
    unittest.main()
