#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最新月の粉体描画に必要な個体列をcompactなSoAへ変換する。

住民の正本や再開checkpointは変更しない。HTMLへ渡す観察projectionだけを、
座標objectの辞書からlittle-endian TypedArray用base64へ畳む。
"""
from array import array
import base64
import math
import sys


PARTICLE_PACKET_VERSION = 2
PARTICLE_PACKET_ENCODING = "base64-le"
COORDINATE_SCALE = 65535
ACTIVITY_KEYS = ("food", "medicine", "shelter", "tools", "unknown")
_ACTIVITY_CODES = {name: index for index, name in enumerate(ACTIVITY_KEYS)}
ACTIVITY_MODE_KEYS = ("home", "routine", "primary")
_ACTIVITY_MODE_CODES = {
    name: index for index, name in enumerate(ACTIVITY_MODE_KEYS)}


def _coordinate(value) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    if not math.isfinite(number):
        number = 0.0
    number = max(0.0, min(1.0, number))
    return int(number * COORDINATE_SCALE + 0.5)


def _phase(value, fallback: float) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = fallback
    if not math.isfinite(number):
        number = fallback
    return int(max(0.0, min(1.0, number)) * 255 + 0.5)


def build_particle_packet(residents: list, spatial_state: dict,
                          turn: int) -> dict | None:
    """現在生存する住民を、描画順を保ったSoA packetへ変換する。

    ``resident_rows``はdashboardの``residents``配列indexであり、名前・親・
    世帯などの詳細をpacketへ重複させない。site/activity/座標/active flagは
    同じparticle indexで対応する。
    """
    if not isinstance(spatial_state, dict):
        return None
    positions = spatial_state.get("residents")
    sites_source = spatial_state.get("sites")
    if not isinstance(positions, dict) or not isinstance(sites_source, dict):
        return None

    site_rows = sorted(
        (row for row in sites_source.values()
         if row.get("active", True)
         and int(row.get("founded_turn", 0) or 0) <= int(turn)),
        key=lambda row: str(row.get("id", "")))
    site_ids = [str(row.get("id", "")) for row in site_rows]
    site_index = {site_id: index for index, site_id in enumerate(site_ids)}

    resident_rows = array("I")
    site_indices = array("I")
    coordinates = array("H")
    activities = bytearray()
    flags = bytearray()
    activity_modes = bytearray()
    schedules = bytearray()

    for row_index, resident in enumerate(residents):
        birth_turn = resident.get("birth_turn")
        registered_turn = resident.get("registered_turn")
        died_turn = resident.get("died_turn")
        if birth_turn is not None and int(birth_turn) > int(turn):
            continue
        if registered_turn is not None and int(registered_turn) > int(turn):
            continue
        if died_turn is not None and int(died_turn) <= int(turn):
            continue
        position = positions.get(resident.get("id"))
        if not isinstance(position, dict):
            continue
        resident_site = str(position.get("site_id", ""))
        packed_site_index = site_index.get(resident_site)
        if packed_site_index is None:
            continue
        site = site_rows[packed_site_index]
        active = resident.get("last_activity_turn") == turn
        activity = position.get("activity") or (
            resident.get("last_activity") if active else None
        ) or site.get("livelihood") or "unknown"
        mode = position.get("activity_mode") or (
            "primary" if active else "home")
        resident_rows.append(row_index)
        site_indices.append(packed_site_index)
        coordinates.append(_coordinate(position.get("x")))
        coordinates.append(_coordinate(position.get("y")))
        activities.append(_ACTIVITY_CODES.get(activity, _ACTIVITY_CODES["unknown"]))
        flags.append(1 if active else 0)
        activity_modes.append(_ACTIVITY_MODE_CODES.get(
            mode, _ACTIVITY_MODE_CODES["home"]))
        schedules.append(_phase(position.get("departure_phase"), 0.0))
        schedules.append(_phase(position.get("return_phase"), 1.0))

    if sys.byteorder != "little":
        resident_rows.byteswap()
        site_indices.byteswap()
        coordinates.byteswap()
    raw_bytes = (len(resident_rows) * 4 + len(site_indices) * 4
                 + len(coordinates) * 2 + len(activities) + len(flags)
                 + len(activity_modes) + len(schedules))
    return {
        "version": PARTICLE_PACKET_VERSION,
        "encoding": PARTICLE_PACKET_ENCODING,
        "turn": int(turn),
        "count": len(resident_rows),
        "site_ids": site_ids,
        "activity_keys": list(ACTIVITY_KEYS),
        "activity_mode_keys": list(ACTIVITY_MODE_KEYS),
        "coordinate_scale": COORDINATE_SCALE,
        "raw_bytes": raw_bytes,
        "resident_rows": base64.b64encode(
            resident_rows.tobytes()).decode("ascii"),
        "site_indices": base64.b64encode(
            site_indices.tobytes()).decode("ascii"),
        "coordinates": base64.b64encode(
            coordinates.tobytes()).decode("ascii"),
        "activities": base64.b64encode(bytes(activities)).decode("ascii"),
        "flags": base64.b64encode(bytes(flags)).decode("ascii"),
        "activity_modes": base64.b64encode(
            bytes(activity_modes)).decode("ascii"),
        "schedules": base64.b64encode(bytes(schedules)).decode("ascii"),
    }
