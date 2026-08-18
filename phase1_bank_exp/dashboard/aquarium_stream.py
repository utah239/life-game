# -*- coding: utf-8 -*-
"""永続水槽の軽量keyframeと論理表示frame契約。

完成bitmapやHTMLを配信せず、現在の数値状態と1か月内の位相だけを配る。
シミュレーションのturn/RNGは増やさず、表示側が同じcanvasへ連続適用する。
"""
from __future__ import annotations

import copy


STREAM_SCHEMA_VERSION = 1
KEYFRAME_SCHEMA_VERSION = 1
FRAME_SCHEMA_VERSION = 2
KEYFRAME_DELTA_SCHEMA_VERSION = 1
LOGICAL_FRAMES_PER_CHUNK = 100
DEFAULT_PRESENTATION_FPS = 30
LIVE_TURN_TAIL = 120
MAX_CHUNK_FRAMES = 150

# 現在画面と短い追い付き区間に必要な項目だけをkeyframeへ載せる。
# spatial_history/全turn/全observer eventは既存HTMLの履歴側に残す。
_CURRENT_KEYS = (
    "meta", "final", "final_resources", "final_traits",
    "npcs", "residents", "households", "spatial_state",
    "particle_frame", "particle_cohorts", "activity_community_ledger",
    "activity_economy_state",
    "organization_state", "institution_trajectories", "settlement_states", "choice_bins",
    "choice_keys", "spatial_keyframes", "spatial_history",
)

# keyframeにイベント本体を1回だけ載せ、各frameはその月に何が動くかを
# bit maskで参照する。100枚へ同じpayloadを複製しない。
FRAME_SIGNAL_ACTIVITY = 1 << 0
FRAME_SIGNAL_TRADE = 1 << 1
FRAME_SIGNAL_MIGRATION = 1 << 2
FRAME_SIGNAL_BIRTH = 1 << 3
FRAME_SIGNAL_ORGANIZATION = 1 << 4
FRAME_SIGNAL_SPATIAL = 1 << 5
FRAME_SIGNAL_POPULATION = 1 << 6

_EVENT_SIGNAL = {
    "household_activity": FRAME_SIGNAL_ACTIVITY,
    "production_activity": FRAME_SIGNAL_ACTIVITY,
    "intersettlement_trade": FRAME_SIGNAL_TRADE,
    "residents_migrated": FRAME_SIGNAL_MIGRATION,
    "population_migrated": FRAME_SIGNAL_MIGRATION,
    "resident_born": FRAME_SIGNAL_BIRTH,
    "organization_founded": FRAME_SIGNAL_ORGANIZATION,
    "organization_reformed": FRAME_SIGNAL_ORGANIZATION,
    "organization_ended": FRAME_SIGNAL_ORGANIZATION,
    "cluster_formed": FRAME_SIGNAL_SPATIAL,
    "cluster_split": FRAME_SIGNAL_SPATIAL,
    "cluster_merged": FRAME_SIGNAL_SPATIAL,
    "cluster_dissolved": FRAME_SIGNAL_SPATIAL,
    "pioneering_started": FRAME_SIGNAL_SPATIAL,
    "pioneering_settled": FRAME_SIGNAL_SPATIAL,
    "pioneering_community_formed": FRAME_SIGNAL_SPATIAL,
    "population_changed": FRAME_SIGNAL_POPULATION,
    "household_goods_migrated": FRAME_SIGNAL_TRADE | FRAME_SIGNAL_MIGRATION,
    "anonymous_goods_migrated": FRAME_SIGNAL_TRADE | FRAME_SIGNAL_MIGRATION,
    "household_goods_acquired": FRAME_SIGNAL_ACTIVITY,
    "household_goods_inherited": FRAME_SIGNAL_ACTIVITY,
    "household_goods_released": FRAME_SIGNAL_ACTIVITY,
    "household_common_goods_accessed": FRAME_SIGNAL_ACTIVITY,
    "household_response_changed": FRAME_SIGNAL_ACTIVITY,
    "household_response_summary": FRAME_SIGNAL_ACTIVITY,
    "household_barter_exchange_summary": FRAME_SIGNAL_TRADE,
}


def _event_density_tail(history: dict | None, first_turn: int) -> dict:
    if not isinstance(history, dict) or history.get("version") != 1:
        return {"version": 1, "settlement_ids": [], "activity_keys": [],
                "population": [], "migrations": [], "activities": []}
    out = {
        "version": 1,
        "settlement_ids": list(history.get("settlement_ids", ())),
        "activity_keys": list(history.get("activity_keys", ())),
    }
    for name in ("population", "migrations", "activities"):
        out[name] = [
            list(row) for row in history.get(name, ())
            if row and int(row[0]) >= first_turn
        ]
    return out


def build_live_keyframe(dashboard_data: dict) -> dict:
    """dashboard全履歴から、同一canvasへ適用できる現在keyframeを作る。"""
    if not isinstance(dashboard_data, dict):
        raise TypeError("dashboard_data must be a dict")
    turns = dashboard_data.get("turns", ())
    tail = list(turns[-LIVE_TURN_TAIL:])
    first_turn = int(tail[0].get("t", 0)) if tail else 0
    keyframe = {
        "schema_version": KEYFRAME_SCHEMA_VERSION,
        "turns": tail,
        "observer_events": [
            row for row in dashboard_data.get("observer_events", ())
            if int(row.get("t", 0)) >= first_turn
        ],
        "settlements": [
            row for row in dashboard_data.get("settlements", ())
            if int(row.get("t", 0)) >= first_turn
        ],
        "event_density_history": _event_density_tail(
            dashboard_data.get("event_density_history"), first_turn),
    }
    for name in _CURRENT_KEYS:
        keyframe[name] = dashboard_data.get(name)
    # workerのresultを呼び出し側が後で再利用しても配信snapshotが変化しない。
    return copy.deepcopy(keyframe)


def _signal_masks(keyframe: dict) -> dict[int, int]:
    masks: dict[int, int] = {}
    for event in keyframe.get("observer_events", ()):
        turn = int(event.get("t", 0))
        masks[turn] = masks.get(turn, 0) | _EVENT_SIGNAL.get(
            str(event.get("kind", "")), 0)
    history = keyframe.get("event_density_history", {})
    for row in history.get("population", ()):
        if row:
            turn = int(row[0])
            masks[turn] = masks.get(turn, 0) | FRAME_SIGNAL_POPULATION
    for row in history.get("migrations", ()):
        if row:
            turn = int(row[0])
            masks[turn] = masks.get(turn, 0) | FRAME_SIGNAL_MIGRATION
    for row in history.get("activities", ()):
        if row:
            turn = int(row[0])
            masks[turn] = masks.get(turn, 0) | FRAME_SIGNAL_ACTIVITY
    return masks


def _frame_turns(keyframe: dict, world: dict) -> list[int]:
    available = sorted({
        int(row.get("t", 0)) for row in keyframe.get("turns", ())
        if int(row.get("t", 0)) >= 0
    })
    current = int(world.get("summary", {}).get("completed_turn", 0))
    if not available:
        return [current]
    start = int(world.get("frame_turn_start", current))
    end = int(world.get("frame_turn_end", current))
    if end < start:
        start = end = current
    selected = [turn for turn in available if start <= turn <= end]
    if not selected:
        selected = [available[-1]]
    # 1 logical frame未満の月を作らない。大きな手動catch-upでは観察窓の
    # 直近100か月を順番に再生し、それ以前は既存の履歴UIから参照する。
    return selected[-LOGICAL_FRAMES_PER_CHUNK:]


def _build_logical_frames(first_sequence: int, turns: list[int],
                          signal_masks: dict[int, int]) -> list[dict]:
    frames = []
    frame_count = LOGICAL_FRAMES_PER_CHUNK
    turn_count = max(1, len(turns))
    for turn_index, turn in enumerate(turns):
        first_index = round(turn_index * frame_count / turn_count)
        after_index = round((turn_index + 1) * frame_count / turn_count)
        local_count = max(1, after_index - first_index)
        for index in range(first_index, after_index):
            offset = index - first_index
            phase = (offset / (local_count - 1)
                     if local_count > 1 else 1.0)
            frames.append({
                "sequence": first_sequence + index,
                "turn": int(turn),
                "month_phase": phase,
                "signal_mask": int(signal_masks.get(int(turn), 0)),
            })
    if len(frames) != frame_count:
        raise RuntimeError("logical frame allocation failed")
    return frames


def _keyed_rows_patch(before: list, after: list, key_name: str) -> dict:
    before_by_key = {row[key_name]: row for row in before}
    after_by_key = {row[key_name]: row for row in after}
    row_patches = []
    for row in after:
        key = row[key_name]
        prior = before_by_key.get(key)
        if prior is None:
            continue
        changed = {
            name: copy.deepcopy(value) for name, value in row.items()
            if name != key_name and prior.get(name, object()) != value
        }
        removed = [name for name in prior
                   if name != key_name and name not in row]
        if changed or removed:
            row_patches.append({
                "key": key, "set": changed, "remove": removed})
    return {
        "order": [row[key_name] for row in after],
        "upsert": [copy.deepcopy(after_by_key[key])
                   for key in (row[key_name] for row in after)
                   if key not in before_by_key],
        "patch": row_patches,
        "remove": [key for key in sorted(before_by_key)
                   if key not in after_by_key],
    }


def _apply_keyed_rows_patch(rows: list, patch: dict,
                            key_name: str) -> list:
    by_key = {row[key_name]: copy.deepcopy(row) for row in rows}
    for key in patch.get("remove", ()):
        by_key.pop(key, None)
    for row in patch.get("upsert", ()):
        by_key[row[key_name]] = copy.deepcopy(row)
    for row_patch in patch.get("patch", ()):
        key = row_patch["key"]
        if key not in by_key:
            raise ValueError("aquarium keyframe delta patches a missing row")
        row = by_key[key]
        for name in row_patch.get("remove", ()):
            row.pop(name, None)
        row.update(copy.deepcopy(row_patch.get("set", {})))
    order = patch.get("order")
    if order is None:
        order = sorted(by_key)
    ordered = [by_key[key] for key in order if key in by_key]
    included = set(order)
    ordered.extend(by_key[key] for key in sorted(by_key) if key not in included)
    return ordered


def _spatial_history_patch(before: dict | None,
                           after: dict | None) -> dict:
    before = before or {"version": 1, "source_frame_count": 0, "frames": []}
    after = after or {"version": 1, "source_frame_count": 0, "frames": []}
    before_frames = {int(row["turn"]): row for row in before.get("frames", ())}
    after_frames = {int(row["turn"]): row for row in after.get("frames", ())}
    return {
        "version": int(after.get("version", 1)),
        "source_frame_count": int(after.get("source_frame_count", 0)),
        "upsert": [copy.deepcopy(after_frames[turn])
                   for turn in sorted(after_frames)
                   if before_frames.get(turn) != after_frames[turn]],
        "remove": [turn for turn in sorted(before_frames)
                   if turn not in after_frames],
    }


def _apply_spatial_history_patch(before: dict | None, patch: dict) -> dict:
    before = before or {"frames": []}
    frames = {int(row["turn"]): copy.deepcopy(row)
              for row in before.get("frames", ())}
    for turn in patch.get("remove", ()):
        frames.pop(int(turn), None)
    for row in patch.get("upsert", ()):
        frames[int(row["turn"])] = copy.deepcopy(row)
    return {
        "version": int(patch.get("version", before.get("version", 1))),
        "source_frame_count": int(patch.get(
            "source_frame_count", before.get("source_frame_count", 0))),
        "frames": [frames[turn] for turn in sorted(frames)],
    }


def build_keyframe_delta(before: dict, after: dict, *,
                         from_revision: int,
                         to_revision: int) -> dict:
    """直前keyframeからの有界差分。イベント本文をframeへ複製しない。"""
    if (before.get("schema_version") != KEYFRAME_SCHEMA_VERSION
            or after.get("schema_version") != KEYFRAME_SCHEMA_VERSION):
        raise ValueError("unsupported aquarium keyframe for delta")
    entity_keys = {"npcs": "name", "residents": "id", "households": "id"}
    entity_names = tuple(entity_keys)
    tail_names = ("observer_events", "settlements")
    excluded = {
        "turns", "spatial_history", "event_density_history",
        *entity_names, *tail_names}
    missing = object()
    current = {
        name: copy.deepcopy(value) for name, value in after.items()
        if name not in excluded and before.get(name, missing) != value
    }
    return {
        "schema_version": KEYFRAME_DELTA_SCHEMA_VERSION,
        "from_revision": int(from_revision),
        "to_revision": int(to_revision),
        "history_start_turn": int(after.get("turns", [{}])[0].get("t", 0))
            if after.get("turns") else 0,
        "turns": _keyed_rows_patch(
            before.get("turns", []), after.get("turns", []), "t"),
        "entities": {
            name: _keyed_rows_patch(
                before.get(name, []) or [], after.get(name, []) or [],
                entity_keys[name])
            for name in entity_names
        },
        "tails": {
            name: copy.deepcopy(after.get(name, []))
            for name in tail_names
        },
        "event_density_history": copy.deepcopy(after.get(
            "event_density_history", {
                "version": 1, "settlement_ids": [], "activity_keys": [],
                "population": [], "migrations": [], "activities": []})),
        "spatial_history": _spatial_history_patch(
            before.get("spatial_history"), after.get("spatial_history")),
        "current": current,
    }


def apply_keyframe_delta(before: dict, delta: dict) -> dict:
    """delta契約の参照実装。serverテストと非ブラウザconsumerでも使う。"""
    if delta.get("schema_version") != KEYFRAME_DELTA_SCHEMA_VERSION:
        raise ValueError("unsupported aquarium keyframe delta")
    after = copy.deepcopy(before)
    after.update(copy.deepcopy(delta.get("current", {})))
    after["turns"] = _apply_keyed_rows_patch(
        before.get("turns", []), delta.get("turns", {}), "t")
    entity_keys = {"npcs": "name", "residents": "id", "households": "id"}
    for name, patch in delta.get("entities", {}).items():
        if name not in entity_keys:
            raise ValueError("unsupported aquarium keyframe delta entity")
        after[name] = _apply_keyed_rows_patch(
            before.get(name, []) or [], patch, entity_keys[name])
    for name, rows in delta.get("tails", {}).items():
        if name not in ("observer_events", "settlements"):
            raise ValueError("unsupported aquarium keyframe delta tail")
        after[name] = copy.deepcopy(rows)
    after["event_density_history"] = copy.deepcopy(delta.get(
        "event_density_history", before.get("event_density_history", {})))
    after["spatial_history"] = _apply_spatial_history_patch(
        before.get("spatial_history"), delta.get("spatial_history", {}))
    return after


def build_stream_snapshot(dashboard_data: dict, world: dict,
                          previous_snapshot: dict | None = None) -> dict:
    """1 revision分のkeyframeと100個の意味frameを構築する。"""
    if not isinstance(world, dict):
        raise TypeError("world must be a dict")
    stream_id = str(world.get("stream_id") or "")
    if not stream_id:
        raise ValueError("world stream_id is required")
    last_sequence = int(world.get("frame_sequence_end", 0))
    if last_sequence < LOGICAL_FRAMES_PER_CHUNK:
        raise ValueError("world frame_sequence_end is invalid")
    first_sequence = last_sequence - LOGICAL_FRAMES_PER_CHUNK + 1
    keyframe = build_live_keyframe(dashboard_data)
    turns = _frame_turns(keyframe, world)
    frames = _build_logical_frames(
        first_sequence, turns, _signal_masks(keyframe))
    revision = int(world.get("revision", 0))
    keyframe_delta = None
    delta_from_revision = None
    if (isinstance(previous_snapshot, dict)
            and previous_snapshot.get("stream_id") == stream_id
            and isinstance(previous_snapshot.get("keyframe"), dict)):
        delta_from_revision = int(previous_snapshot.get("base_revision", 0))
        if delta_from_revision < revision:
            keyframe_delta = build_keyframe_delta(
                previous_snapshot["keyframe"], keyframe,
                from_revision=delta_from_revision, to_revision=revision)
    return {
        "schema_version": STREAM_SCHEMA_VERSION,
        "frame_schema_version": FRAME_SCHEMA_VERSION,
        "stream_id": stream_id,
        "base_revision": revision,
        "delta_from_revision": delta_from_revision,
        "first_sequence": first_sequence,
        "last_sequence": last_sequence,
        "fps": DEFAULT_PRESENTATION_FPS,
        "server_time": float(world.get("updated_at", 0.0) or 0.0),
        "tick_seconds": float(
            world.get("clock", {}).get("tick_seconds", 1) or 1),
        "frame_turn_start": turns[0],
        "frame_turn_end": turns[-1],
        "keyframe": keyframe,
        "keyframe_delta": keyframe_delta,
        "frames": frames,
    }


def validate_stream_snapshot(snapshot: dict) -> None:
    if (not isinstance(snapshot, dict)
            or snapshot.get("schema_version") != STREAM_SCHEMA_VERSION):
        raise ValueError("unsupported aquarium stream file")
    frames = snapshot.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ValueError("aquarium stream has no logical frames")
    frame_schema = int(snapshot.get("frame_schema_version", 1))
    if frame_schema not in (1, FRAME_SCHEMA_VERSION):
        raise ValueError("unsupported aquarium logical frame schema")
    expected = int(snapshot.get("first_sequence", -1))
    for frame in frames:
        if int(frame.get("sequence", -1)) != expected:
            raise ValueError("aquarium stream frame sequence is not contiguous")
        if not 0.0 <= float(frame.get("month_phase", -1.0)) <= 1.0:
            raise ValueError("aquarium stream month_phase is out of range")
        if frame_schema >= 2 and int(frame.get("signal_mask", -1)) < 0:
            raise ValueError("aquarium stream signal_mask is invalid")
        expected += 1
    if expected - 1 != int(snapshot.get("last_sequence", -1)):
        raise ValueError("aquarium stream last_sequence does not match frames")
    delta = snapshot.get("keyframe_delta")
    if delta is not None:
        if not isinstance(delta, dict):
            raise ValueError("aquarium keyframe_delta must be an object")
        if int(delta.get("schema_version", -1)) != KEYFRAME_DELTA_SCHEMA_VERSION:
            raise ValueError("unsupported aquarium keyframe_delta schema")
        if int(delta.get("from_revision", -1)) != int(
                snapshot.get("delta_from_revision", -2)):
            raise ValueError("aquarium keyframe_delta source does not match")
        if int(delta.get("to_revision", -1)) != int(
                snapshot.get("base_revision", -2)):
            raise ValueError("aquarium keyframe_delta target does not match")


def stream_chunk(snapshot: dict, *, client_stream_id: str | None = None,
                 client_revision: int | None = None,
                 after_sequence: int | None = None,
                 limit: int = MAX_CHUNK_FRAMES) -> dict:
    """client cursor以後のframeを返す。revision不一致時はkeyframeも返す。"""
    validate_stream_snapshot(snapshot)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise ValueError("limit must be an integer") from None
    if not 1 <= limit <= MAX_CHUNK_FRAMES:
        raise ValueError(
            f"limit must be between 1 and {MAX_CHUNK_FRAMES}")
    if after_sequence is not None:
        try:
            after_sequence = int(after_sequence)
        except (TypeError, ValueError):
            raise ValueError("after_sequence must be an integer") from None
    if client_revision is not None:
        try:
            client_revision = int(client_revision)
        except (TypeError, ValueError):
            raise ValueError("client_revision must be an integer") from None

    revision = int(snapshot["base_revision"])
    same_stream = client_stream_id == snapshot["stream_id"]
    same_base = (
        same_stream
        and client_revision == revision
    )
    can_delta = (
        same_stream
        and client_revision is not None
        and client_revision == snapshot.get("delta_from_revision")
        and snapshot.get("keyframe_delta") is not None
    )
    first = int(snapshot["first_sequence"])
    cursor = after_sequence if same_base and after_sequence is not None else first - 1
    frames = [
        copy.deepcopy(frame) for frame in snapshot["frames"]
        if int(frame["sequence"]) > cursor
    ][:limit]
    return {
        "schema_version": STREAM_SCHEMA_VERSION,
        "frame_schema_version": int(snapshot.get("frame_schema_version", 1)),
        "exists": True,
        "stream_id": snapshot["stream_id"],
        "base_revision": revision,
        "first_sequence": first,
        "last_sequence": int(snapshot["last_sequence"]),
        "fps": snapshot["fps"],
        "server_time": snapshot["server_time"],
        "tick_seconds": snapshot["tick_seconds"],
        "frame_turn_start": int(snapshot.get(
            "frame_turn_start", snapshot["frames"][0]["turn"])),
        "frame_turn_end": int(snapshot.get(
            "frame_turn_end", snapshot["frames"][-1]["turn"])),
        "keyframe": (None if same_base or can_delta
                     else copy.deepcopy(snapshot["keyframe"])),
        "keyframe_delta": (copy.deepcopy(snapshot["keyframe_delta"])
                           if can_delta else None),
        "frames": frames,
    }
