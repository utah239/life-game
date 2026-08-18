#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
モデル切替コスト・アンロード後の再ロード時間を測る使い捨てベンチスクリプト。

opusレビュー(第2回)の指摘: events_llama3_run1.jsonl の初回37.5秒は「コールドスタート」
ではなく「qwen2.5vl:7bが常駐した直後にllama3:latestをロードしたモデルスワップのコスト」
である可能性が高い、という指摘を受けて、意図的にn回スワップさせて実測する。

計測1: モデルスワップコスト — qwen→llama3→qwen→llama3 と交互に呼び、切替のたびに
       かかる時間を記録する(同モデル連続呼び出しとの差分がスワップコスト)
計測2: keep_alive=0でアンロードを強制した直後の再ロード時間

結果はJSON1行ずつ標準出力に出すだけ。ゲーム本体(game.py)には組み込まない使い捨て。

使い方: python bench_model_swap.py
"""
import json
import time
import urllib.request

OLLAMA_HOST = "http://192.168.0.210:11434"
MODELS = ["qwen2.5vl:7b", "llama3:latest"]


def call(model: str, keep_alive="5m") -> float:
    payload = {
        "model": model,
        "prompt": "こんにちは、一言だけ挨拶してください。",
        "stream": False,
        "keep_alive": keep_alive,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/generate", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    start = time.monotonic()
    with urllib.request.urlopen(req, timeout=180) as resp:
        json.loads(resp.read().decode("utf-8"))
    return time.monotonic() - start


def main():
    print("=== 計測1: モデルスワップコスト(交互に4回) ===")
    sequence = [MODELS[0], MODELS[1], MODELS[0], MODELS[1]]
    prev_model = None
    for i, model in enumerate(sequence, 1):
        elapsed = call(model)
        swapped = model != prev_model
        print(f"  {i}. model={model} swap={'YES' if swapped else 'no '} latency={elapsed:.1f}秒")
        prev_model = model

    print("\n=== 計測2: keep_alive=0で強制アンロード後の再ロード時間 ===")
    model = MODELS[0]
    warm = call(model, keep_alive="5m")
    print(f"  ウォーム呼び出し: {warm:.1f}秒")
    call(model, keep_alive="0")  # 応答直後にアンロードさせる
    print("  (keep_alive=0で呼び出し、アンロード指示済み)")
    time.sleep(3)  # アンロードが反映されるのを少し待つ
    reload_latency = call(model, keep_alive="5m")
    print(f"  再ロード直後の呼び出し: {reload_latency:.1f}秒")


if __name__ == "__main__":
    main()
