# -*- coding: utf-8 -*-
"""イベントログ(JSONL)の追記・読み出しだけを担当する保存層。

段階的モジュール分割(Step 2A、2026-08-14、behavior-preserving refactoring)で
game.py から切り出した。ドメイン判断(イベント種別ごとの意味づけ・状態の
再構築)は一切持たない——それは projection.py(Step 2B)の役割。

このモジュールは game.py をimportしない。パスは呼び出し側が明示的に渡す
(モジュール内にEVENTS_PATHのようなグローバルな既定パスを持たない)。
"""
import json
from datetime import datetime, timezone
from pathlib import Path


def append_jsonl_event(path, event_type: str, data: dict) -> None:
    """1件のイベントをJSONLとして1行追記する。タイムスタンプはこの保存層で
    付与する(呼び出し側は種別とpayloadだけを渡せばよい、という既存の
    append_event()の契約をそのまま維持)。"""
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "type": event_type,
        "data": data,
    }
    with Path(path).open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def iter_jsonl_events(path):
    """JSONLファイルを先頭から読み、1行ずつdictとしてyieldする。
    ファイルが存在しなければ空のイテレータ(何もyieldしない)。空行は無視する。"""
    p = Path(path)
    if not p.exists():
        return
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)
