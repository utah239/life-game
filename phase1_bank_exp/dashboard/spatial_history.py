#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""空間keyframeの全件反復を、ID単位の差分履歴へ変換する。"""
from __future__ import annotations


SPATIAL_HISTORY_VERSION = 1
_COLLECTIONS = ("sites", "residents", "clusters")


def _rows_by_id(rows: list, collection: str, turn: int) -> dict:
    indexed = {}
    for row in rows or ():
        if not isinstance(row, list) or not row:
            raise ValueError(
                f"spatial keyframe {collection} row at T{turn} has no id")
        key = str(row[0])
        if key in indexed:
            raise ValueError(
                f"duplicate {collection} id {key!r} at T{turn}")
        indexed[key] = row
    return indexed


def build_spatial_history(keyframes: list) -> dict:
    """完全keyframe列を可逆なupsert/remove列へ変換する。

    入力rowの順序はupsert順として保存する。変化のないcollectionはframeから
    省略し、入力dict/listは変更しない。
    """
    previous = {name: {} for name in _COLLECTIONS}
    frames = []
    prior_turn = None
    for source in keyframes or ():
        turn = int(source["turn"])
        if prior_turn is not None and turn <= prior_turn:
            raise ValueError("spatial keyframe turns must be strictly increasing")
        delta = {"turn": turn}
        for name in _COLLECTIONS:
            current = _rows_by_id(source.get(name, []), name, turn)
            before = previous[name]
            upserts = [
                row for key, row in current.items()
                if before.get(key) != row]
            removals = [key for key in before if key not in current]
            if upserts or removals:
                change = {}
                if upserts:
                    change["upsert"] = upserts
                if removals:
                    change["remove"] = removals
                delta[name] = change
            previous[name] = current
        frames.append(delta)
        prior_turn = turn
    return {
        "version": SPATIAL_HISTORY_VERSION,
        "source_frame_count": len(frames),
        "frames": frames,
    }
