# -*- coding: utf-8 -*-
"""最新月の粉体SoA packetの形式・保存量・非変更性。"""
from array import array
import base64
import copy
import json
import sys
import unittest

from dashboard import particle_packet


def decode_array(encoded: str, typecode: str) -> list:
    values = array(typecode)
    values.frombytes(base64.b64decode(encoded))
    if sys.byteorder != "little" and values.itemsize > 1:
        values.byteswap()
    return list(values)


class ParticlePacketTest(unittest.TestCase):
    def test_encodes_parallel_arrays_without_mutating_inputs(self):
        residents = [
            {"id": "r1", "birth_turn": 0, "died_turn": None,
             "last_activity_turn": 10, "last_activity": "tools"},
            {"id": "r2", "birth_turn": 0, "died_turn": 10,
             "last_activity_turn": None},
            {"id": "r3", "birth_turn": 5, "died_turn": None,
             "last_activity_turn": None},
        ]
        spatial = {
            "sites": {
                "s2": {"id": "s2", "livelihood": "medicine",
                       "founded_turn": 1, "active": True},
                "s1": {"id": "s1", "livelihood": "food",
                       "founded_turn": 1, "active": True},
            },
            "residents": {
                "r1": {"site_id": "s2", "x": 1.0, "y": 0.0},
                "r2": {"site_id": "s1", "x": 0.5, "y": 0.5},
                "r3": {"site_id": "s1", "x": 0.25, "y": 0.75},
            },
        }
        before = copy.deepcopy((residents, spatial))
        packet = particle_packet.build_particle_packet(
            residents, spatial, 10)

        self.assertEqual((residents, spatial), before)
        self.assertEqual(packet["version"], 2)
        self.assertEqual(packet["encoding"], "base64-le")
        self.assertEqual(packet["turn"], 10)
        self.assertEqual(packet["count"], 2)
        self.assertEqual(packet["site_ids"], ["s1", "s2"])
        self.assertEqual(packet["activity_keys"], [
            "food", "medicine", "shelter", "tools", "unknown"])
        self.assertEqual(
            packet["activity_mode_keys"], ["home", "routine", "primary"])
        self.assertEqual(decode_array(packet["resident_rows"], "I"), [0, 2])
        self.assertEqual(decode_array(packet["site_indices"], "I"), [1, 0])
        self.assertEqual(decode_array(packet["coordinates"], "H"), [
            65535, 0, 16384, 49151])
        self.assertEqual(list(base64.b64decode(packet["activities"])), [3, 0])
        self.assertEqual(list(base64.b64decode(packet["flags"])), [1, 0])
        self.assertEqual(
            list(base64.b64decode(packet["activity_modes"])), [2, 0])
        self.assertEqual(
            list(base64.b64decode(packet["schedules"])), [0, 255, 0, 255])
        self.assertEqual(packet["raw_bytes"], 34)

    def test_encodes_spatial_activity_episode_and_quantized_schedule(self):
        residents = [{
            "id": "r1", "birth_turn": 0, "died_turn": None,
            "last_activity_turn": None,
        }]
        spatial = {
            "sites": {"s1": {
                "id": "s1", "livelihood": "food",
                "founded_turn": 1, "active": True,
            }},
            "residents": {"r1": {
                "site_id": "s1", "x": 0.25, "y": 0.75,
                "activity": "shelter", "activity_mode": "routine",
                "departure_phase": 0.25, "return_phase": 0.75,
            }},
        }

        packet = particle_packet.build_particle_packet(
            residents, spatial, 10)

        self.assertEqual(
            list(base64.b64decode(packet["activities"])), [2])
        self.assertEqual(
            list(base64.b64decode(packet["activity_modes"])), [1])
        self.assertEqual(
            list(base64.b64decode(packet["schedules"])), [64, 191])

    def test_skips_unplaced_inactive_and_not_yet_born_residents(self):
        residents = [
            {"id": "kept", "birth_turn": 0, "died_turn": None},
            {"id": "inactive", "birth_turn": 0, "died_turn": None},
            {"id": "future", "birth_turn": 11, "died_turn": None},
            {"id": "missing", "birth_turn": 0, "died_turn": None},
        ]
        spatial = {
            "sites": {
                "s1": {"id": "s1", "livelihood": "unknown",
                       "founded_turn": 1, "active": True},
                "s2": {"id": "s2", "livelihood": "food",
                       "founded_turn": 1, "active": False},
            },
            "residents": {
                "kept": {"site_id": "s1", "x": -3, "y": float("inf")},
                "inactive": {"site_id": "s2", "x": 0.5, "y": 0.5},
                "future": {"site_id": "s1", "x": 0.5, "y": 0.5},
            },
        }
        packet = particle_packet.build_particle_packet(
            residents, spatial, 10)
        self.assertEqual(packet["count"], 1)
        self.assertEqual(decode_array(packet["resident_rows"], "I"), [0])
        self.assertEqual(decode_array(packet["coordinates"], "H"), [0, 0])
        self.assertEqual(list(base64.b64decode(packet["activities"])), [4])

    def test_returns_none_without_spatial_schema(self):
        self.assertIsNone(particle_packet.build_particle_packet([], {}, 1))
        self.assertIsNone(particle_packet.build_particle_packet(
            [], {"sites": []}, 1))

    def test_packet_is_materially_smaller_than_coordinate_objects(self):
        count = 10_000
        residents = [{
            "id": f"r{index:08d}", "birth_turn": 0, "died_turn": None,
            "last_activity_turn": 10 if index % 100 == 0 else None,
        } for index in range(count)]
        spatial = {
            "sites": {"s1": {
                "id": "s1", "household_id": "h1", "livelihood": "food",
                "founded_turn": 1, "active": True}},
            "residents": {
                row["id"]: {
                    "resident_id": row["id"], "site_id": "s1",
                    "household_id": "h1", "x": (index % 1000) / 999,
                    "y": (index // 1000) / 9, "target_x": 0.5,
                    "target_y": 0.5,
                }
                for index, row in enumerate(residents)
            },
        }
        packet = particle_packet.build_particle_packet(
            residents, spatial, 10)
        legacy_size = len(json.dumps(
            spatial["residents"], separators=(",", ":")).encode())
        packet_size = len(json.dumps(
            packet, separators=(",", ":")).encode())
        self.assertEqual(packet["count"], count)
        self.assertEqual(packet["raw_bytes"], count * 17)
        self.assertLess(packet_size, legacy_size * 0.35)


if __name__ == "__main__":
    unittest.main()
