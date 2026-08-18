#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
使い捨て検証スクリプト。code_check_narration() を、正規表現のチューニングに
使っていない**実データ**(既存の run1〜run5 ログ)に対して走らせ、過学習していないかを
確認する(否定制御テストの7ケースだけに合わせ込んだ可能性を疑うため)。

やること: 各ログを turn_started 単位で読み、その turn の decay + state_applied の delta を
合算して turn_delta を再構築し、対応する narration イベントと突き合わせて
code_check_narration() を実行する。結果を一覧表示するだけ(自動判定はしない。
既知の実バグ、README「4. ナレーションの矛盾は減ったが消えていない」に書いた
run3 T5 / run4 T14 が拾えるかを目視で確認する)。

使い方: python validate_code_check.py
"""
import json
from pathlib import Path

import game  # phase1/game.py から関数を借用

LOG_DIR = Path(__file__).parent
LOGS = [
    "events_run1_auto14.jsonl", "events_run2_auto15.jsonl",
    "events_run3_auto15.jsonl", "events_run4_auto15.jsonl",
    "events_run5_control_notheme.jsonl",
]


def load_events(path: Path) -> list:
    events = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def replay(events: list) -> list:
    """1ターンごとに (turn, delta, narration) のリストを作る"""
    turns = []
    cur_turn, cur_delta, cur_narration = None, {}, None
    for e in events:
        t, d = e["type"], e["data"]
        if t == "turn_started":
            if cur_turn is not None and cur_narration is not None:
                turns.append((cur_turn, cur_delta, cur_narration))
            cur_turn, cur_delta, cur_narration = d["turn"], {}, None
        elif t in ("decay_applied", "state_applied", "regen_applied", "income_applied"):
            # game.py の reduce_state() が資源に加算するイベント種別と揃える。
            # (最初のバージョンは regen_applied / income_applied を数え忘れていて、
            # 実際には増えていた資源を「減ったはず」と誤判定する欠陥があった)
            for k, v in d.get("delta", {}).items():
                cur_delta[k] = cur_delta.get(k, 0) + v
        elif t == "llm_call" and d.get("role") == "narration":
            cur_narration = d["raw"]
    if cur_turn is not None and cur_narration is not None:
        turns.append((cur_turn, cur_delta, cur_narration))
    return turns


def main():
    total, flagged_count = 0, 0
    for name in LOGS:
        path = LOG_DIR / name
        if not path.exists():
            continue
        turns = replay(load_events(path))
        print(f"\n=== {name}({len(turns)}ターン) ===")
        for turn, delta, narration in turns:
            total += 1
            up_keys = [k for k, v in delta.items() if v > 0]
            not_up_keys = [k for k in game.ALLOWED_RESOURCES if k not in up_keys]
            issues = game.code_check_narration(not_up_keys, narration)
            if issues:
                flagged_count += 1
                print(f"  [T{turn}] 検出: {'; '.join(issues)}")
                print(f"         narration: {narration.strip()}")
    print(f"\n=== 合計 {total}ターン中 {flagged_count}ターンで検出 ===")


if __name__ == "__main__":
    main()
