#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
人生ゲーム(仮) Phase 0 — 縦切り検証プロトタイプ

目的は「遊べるもの」ではなく「計測」。plan.md の設計を実装する前段階として、
以下を実測する:
  - 1ターンあたりのLLM呼び出しレイテンシ
  - 日本語の生成品質(人間が目視で判断する。このスクリプトは判定しない)
  - 3択が優劣勾配に潰れず、異なる資源のトレードオフになっているか
  - ollama(192.168.0.210:11434)への負荷実態

設計方針(2026-08-12、opusレビュー反映・第2回レビューで更新):
  - LLMは「提案」するだけ。資源の増減が実際に適用されるかはコードが決める
    (イベントソーシング。events.jsonl への追記のみ、現在状態は毎回reduceで再構築)
  - 1人・1キャラのみ。世代交代・エントロピー減衰・常時稼働・マルチプレイは対象外
  - 2026-08-12 第2回レビュー: 実測ログで「信頼関係を強化する行動でtrustが減る」等の
    意味論的破綻が見つかった。コスト値をLLMに自由記述させるのが根本原因という指摘を
    受け、--cost-mode code (行動アーキタイプ+コストレンジをコード側で固定し、LLMは
    ラベルと描写のみ生成) を追加。--cost-mode llm (旧来の自由記述、ただし
    ホワイトリスト検証+クリップで保護) と比較できるようにした
  - --consistency-check で「整合性担保役」役を1つ追加し、役割追加のレイテンシ増分を
    測れるようにした(同一モデルの別プロンプトとして実装)

注意: ollama は NAS 本番の paperless-gpt と共用(ai-lab/CLAUDE.md 参照)。
長時間・大量に回し続けるテストはしないこと(数ターン程度に留める)。

使い方:
  python game.py --model qwen2.5vl:7b --turns 5 --cost-mode code
  python game.py --model qwen2.5vl:7b --turns 5 --cost-mode llm --consistency-check

events.jsonl は実行のたびに追記される。最初からやり直したい場合は削除すること。
"""
import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OLLAMA_HOST = "http://192.168.0.210:11434"
EVENTS_PATH = Path(__file__).parent / "events.jsonl"

# ollamaにモデルをVRAMへ留めておく時間。未指定(ollama既定は5分)だと暗黙的になるため、
# ここで明示する。2026-08-12時点では「NAS本番(paperless-gpt)にVRAMを早く返す」ことを
# 優先し、既定と同じ5分のまま明示する運用にした(keep_alive実測タスクの結論)。
KEEP_ALIVE = "5m"

INITIAL_RESOURCES = {
    "time": 10,     # フロー: 使える時間の余裕
    "energy": 10,   # フロー: 体力
    "peace": 10,    # フロー: 心の余裕
    "money": 5,     # ストック: お金
    "trust": 5,     # ストック: 信用
}
ALLOWED_RESOURCES = set(INITIAL_RESOURCES.keys())
MAX_DELTA = 5  # 1回の選択で資源が動く量の上限(LLMが桁を暴走させる対策)

# --cost-mode code で使う、対価の多様性(plan.md「対価の多様性」節)に対応した
# 3つの行動アーキタイプ。資源の増減量はコードが決め、LLMは「この状況で具体的に
# 何をすることになるか」というラベルだけを埋める。
# 2026-08-12修正(opus第3回レビュー指摘): moneyのレンジが元々(-8, -1)で、上のMAX_DELTA=5を
# 自ら超えていた(--cost-mode codeはsanitize_costを通らないため実害は無かったが、コードが
# 自分で決めた上限をコードが破っているのは矛盾)。MAX_DELTA以内に収める。
ACTION_ARCHETYPES = [
    {"key": "money", "name": "お金で解決する", "ranges": {"money": (-5, -1)}},
    {"key": "labor", "name": "自分の時間と労力で解決する", "ranges": {"time": (-4, -1), "energy": (-3, -1)}},
    {"key": "social", "name": "人との関係を使って解決する(頼る・借りを作る)", "ranges": {"trust": (-3, -1)}},
]

SITUATION_SYSTEM_LLM_COST = """あなたは人生シミュレーションゲームのストーリー生成役です。
プレイヤーの現在の資源状態を踏まえて、次の状況を短い日本語の文章(2〜4文)で描写し、
プレイヤーが取れる行動を必ず3つ提示してください。

# 制約(厳守)
- 3つの選択肢は、必ずそれぞれ異なる資源(time/energy/peace/money/trust)を主なコストとして
  消費するようにしてください(3つとも同じ資源を主コストにしないこと)
- 「良い選択・普通の選択・悪い選択」のような単純な優劣にしないでください。それぞれ別の
  トレードオフを持つようにしてください
- costには消費する資源をマイナスの数値、得られる資源をプラスの数値で入れてください
  (使わない資源は省略可)。**選択肢の意味と数値の向きを一致させること**(例:
  「信頼関係を強化する」選択肢でtrustが減る、のような矛盾は禁止)
- 資源が増える選択肢(costの値がプラス)には、その資源が**どこから得られるのか**
  (誰から、何の対価として)が状況描写から読み取れるようにしてください。理由もなく
  資源が増えることは避けてください
- 直近の状況描写(下記に列挙)とは、テーマ・舞台設定・言い回しが重複しない、新しい
  状況を描写してください
- 出力は必ず以下のJSON形式のみ。前後に説明文を付けないこと

{
  "situation": "状況描写(日本語)",
  "choices": [
    {"label": "選択肢の短い説明", "cost": {"資源名": 増減値}},
    {"label": "選択肢の短い説明", "cost": {"資源名": 増減値}},
    {"label": "選択肢の短い説明", "cost": {"資源名": 増減値}}
  ]
}
"""

SITUATION_SYSTEM_CODE_COST = """あなたは人生シミュレーションゲームのストーリー生成役です。
プレイヤーの現在の資源状態を踏まえて、次の状況を短い日本語の文章(2〜4文)で描写して
ください。

続けて、プレイヤーが取れる行動を、以下の3つの対価タイプそれぞれについて1つずつ、
この状況に即した具体的な内容で提示してください(**資源の増減量はあなたが決める必要は
ありません。コード側で決まります**。あなたはその行動が具体的に何をすることかという
ラベルだけを書いてください):

1. money型: お金・費用を払って解決する行動
2. labor型: 自分の時間と労力を使って解決する行動
3. social型: 誰かに頼る・借りを作って解決する行動

直近の状況描写(下記に列挙)とは、テーマ・舞台設定・言い回しが重複しない、新しい
状況を描写してください。

出力は必ず以下のJSON形式のみ。前後に説明文を付けないこと

{
  "situation": "状況描写(日本語)",
  "choices": {
    "money": "money型の行動の具体的な説明",
    "labor": "labor型の行動の具体的な説明",
    "social": "social型の行動の具体的な説明"
  }
}
"""

NARRATION_SYSTEM = """あなたは人生シミュレーションゲームのストーリー生成役です。
プレイヤーが選んだ選択肢と、その結果の資源状態を踏まえて、結果を短い日本語の文章
(2〜3文)で描写してください。**数値そのものには絶対に言及せず**、情景・心情として
描写すること(悪い例:「時間は5、エネルギーは4です」。良い例:「疲れが溜まっていた」)。
出力は説明文を付けず、描写文のみ。"""

CONSISTENCY_SYSTEM = """あなたはこの世界の整合性担保役です。担当は「絶対不変の法則」
(エントロピー増大則・資源保存則・生物学的な限界)への違反検出のみで、法律や社会規範の
妥当性は判定しません。

以下の行動とその結果の資源変化について、次の2点だけを確認してください:
1. 資源保存則: 出処の説明が無いのに資源(特にmoney/trust)が増えていないか
2. エントロピー増大則: 何の対価も払わずに秩序(peace/energy等)が回復していないか

問題が無ければ「OK」とだけ出力してください。疑わしい場合のみ、一言(20字程度)で
理由を述べてください。長い説明は不要です。"""


def call_ollama(model: str, system: str, user: str, want_json: bool) -> tuple:
    """ollamaにプロンプトを投げ、(応答テキスト, レイテンシ秒)を返す"""
    payload = {
        "model": model,
        "prompt": user,
        "system": system,
        "stream": False,
        "keep_alive": KEEP_ALIVE,
    }
    if want_json:
        payload["format"] = "json"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/generate", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"[ollama接続エラー] {OLLAMA_HOST} に到達できません: {e}", file=sys.stderr)
        sys.exit(1)
    elapsed = time.monotonic() - start
    return body.get("response", ""), elapsed


def append_event(event_type: str, data: dict) -> None:
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "type": event_type,
        "data": data,
    }
    with EVENTS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def sanitize_cost(raw_cost: dict) -> tuple:
    """LLMが返したcost辞書を検証する。許可された資源名以外は捨て、値は±MAX_DELTAにクリップする。
    戻り値: (清浄化済みcost, 拒否したキーのリスト, クリップしたキーのリスト)"""
    clean, rejected, clipped = {}, [], []
    for k, v in raw_cost.items():
        if k not in ALLOWED_RESOURCES:
            rejected.append(k)
            continue
        try:
            v = int(v)
        except (TypeError, ValueError):
            rejected.append(k)
            continue
        if v > MAX_DELTA or v < -MAX_DELTA:
            clipped.append(k)
            v = max(-MAX_DELTA, min(MAX_DELTA, v))
        if v != 0:
            clean[k] = v
    return clean, rejected, clipped


def draw_archetype_cost(key: str) -> dict:
    """--cost-mode code 用。アーキタイプのレンジからコードが実際の数値を決める"""
    archetype = next(a for a in ACTION_ARCHETYPES if a["key"] == key)
    return {res: random.randint(lo, hi) for res, (lo, hi) in archetype["ranges"].items()}


def reduce_state() -> dict:
    """events.jsonl を畳み込んで現在の資源状態を再構築する(イベントソーシング)"""
    resources = dict(INITIAL_RESOURCES)
    if not EVENTS_PATH.exists():
        return resources
    with EVENTS_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            if event["type"] == "state_applied":
                for k, v in event["data"]["delta"].items():
                    if k in ALLOWED_RESOURCES:
                        resources[k] = resources.get(k, 0) + v
    return resources


def format_resources(resources: dict) -> str:
    return " / ".join(f"{k}:{v}" for k, v in resources.items())


def recent_situations(n: int = 3) -> list:
    """直近n件の状況描写テキストを events.jsonl から取り出す(同じ状況の繰り返し対策用)"""
    if not EVENTS_PATH.exists():
        return []
    texts = []
    with EVENTS_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            if event["type"] == "llm_call" and event["data"].get("role") == "situation":
                try:
                    parsed = json.loads(event["data"]["raw"])
                    if "situation" in parsed:
                        texts.append(parsed["situation"])
                except Exception:
                    continue
    return texts[-n:]


def print_summary(latencies: list, parse_failures: int, sanitized_count: int) -> None:
    print("\n=== 計測サマリ ===")
    if latencies:
        print(f"LLM呼び出し回数: {len(latencies)}")
        print(f"平均レイテンシ: {sum(latencies) / len(latencies):.1f}秒")
        print(f"最大レイテンシ: {max(latencies):.1f}秒")
        print(f"最小レイテンシ: {min(latencies):.1f}秒")
    print(f"JSON解析失敗: {parse_failures}件")
    print(f"コスト値のサニタイズ発生: {sanitized_count}件")
    print(f"詳細ログ: {EVENTS_PATH}")


def build_user_prompt(resources: dict) -> str:
    history = recent_situations(3)
    user_prompt = f"現在の資源状態: {json.dumps(resources, ensure_ascii=False)}"
    if history:
        history_str = "\n".join(f"- {s}" for s in history)
        user_prompt += f"\n直近の状況描写(これらとは重複しない新しい状況にすること):\n{history_str}"
    return user_prompt


def run_situation_turn_llm_cost(model: str, resources: dict, latencies: list) -> dict:
    """--cost-mode llm: LLMが状況・3択・コストすべてを提案する(旧来モード)"""
    raw, latency = call_ollama(model, SITUATION_SYSTEM_LLM_COST, build_user_prompt(resources), want_json=True)
    latencies.append(latency)
    append_event("llm_call", {"role": "situation", "model": model, "latency_sec": latency, "raw": raw})

    parsed = json.loads(raw)  # 呼び出し元でtry/exceptする
    assert "situation" in parsed and "choices" in parsed and len(parsed["choices"]) == 3

    choices = []
    for c in parsed["choices"]:
        clean, rejected, clipped = sanitize_cost(c.get("cost", {}))
        if rejected or clipped:
            append_event("cost_sanitized", {"label": c["label"], "rejected": rejected, "clipped": clipped})
        choices.append({"label": c["label"], "cost": clean})
    return {"situation": parsed["situation"], "choices": choices, "latency": latency}


def run_situation_turn_code_cost(model: str, resources: dict, latencies: list) -> dict:
    """--cost-mode code: LLMはラベルのみ、コストの数値はコード(アーキタイプのレンジ)が決める"""
    raw, latency = call_ollama(model, SITUATION_SYSTEM_CODE_COST, build_user_prompt(resources), want_json=True)
    latencies.append(latency)
    append_event("llm_call", {"role": "situation", "model": model, "latency_sec": latency, "raw": raw})

    parsed = json.loads(raw)
    assert "situation" in parsed and "choices" in parsed
    choices = []
    for archetype in ACTION_ARCHETYPES:
        label = parsed["choices"].get(archetype["key"], archetype["name"])
        cost = draw_archetype_cost(archetype["key"])
        choices.append({"label": label, "cost": cost})
    return {"situation": parsed["situation"], "choices": choices, "latency": latency}


def run_consistency_check(model: str, choice: dict, new_resources: dict, latencies: list) -> str:
    """--consistency-check: 整合性担保役(絶対法則違反のみ判定)を1ロール追加する"""
    prompt = (
        f"選ばれた行動: {choice['label']}\n"
        f"資源の変化: {json.dumps(choice['cost'], ensure_ascii=False)}\n"
        f"変化後の資源状態: {json.dumps(new_resources, ensure_ascii=False)}"
    )
    raw, latency = call_ollama(model, CONSISTENCY_SYSTEM, prompt, want_json=False)
    latencies.append(latency)
    append_event("llm_call", {"role": "consistency_check", "model": model, "latency_sec": latency, "raw": raw})
    return raw.strip(), latency


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen2.5vl:7b")
    parser.add_argument("--turns", type=int, default=5)
    parser.add_argument("--cost-mode", choices=["llm", "code"], default="code",
                         help="llm=LLMが数値も提案(サニタイズあり) / code=コードがアーキタイプのレンジで決定")
    parser.add_argument("--consistency-check", action="store_true",
                         help="整合性担保役(絶対法則違反検出)の呼び出しを1つ追加し、レイテンシ増分を測る")
    args = parser.parse_args()

    print(f"=== Phase 0 検証プロトタイプ (model={args.model}, cost-mode={args.cost_mode}, "
          f"consistency-check={args.consistency_check}) ===")
    print(f"events: {EVENTS_PATH}")
    print()

    latencies = []
    parse_failures = 0
    sanitized_count = 0

    for turn in range(1, args.turns + 1):
        resources = reduce_state()
        print(f"--- ターン{turn} --- 資源: {format_resources(resources)}")

        before_events = 0
        try:
            with EVENTS_PATH.open("r", encoding="utf-8") as f:
                before_events = sum(1 for line in f if '"cost_sanitized"' in line)
        except FileNotFoundError:
            pass

        try:
            if args.cost_mode == "code":
                turn_result = run_situation_turn_code_cost(args.model, resources, latencies)
            else:
                turn_result = run_situation_turn_llm_cost(args.model, resources, latencies)
        except Exception as e:
            print(f"  [解析エラー] {e}")
            append_event("parse_error", {"role": "situation", "error": str(e)})
            parse_failures += 1
            continue

        with EVENTS_PATH.open("r", encoding="utf-8") as f:
            after_events = sum(1 for line in f if '"cost_sanitized"' in line)
        sanitized_count += after_events - before_events

        print(f"[{turn_result['latency']:.1f}秒] {turn_result['situation']}")
        choices = turn_result["choices"]
        for i, choice in enumerate(choices, 1):
            cost_str = ", ".join(f"{k}{v:+d}" for k, v in choice["cost"].items())
            print(f"  {i}. {choice['label']} ({cost_str})")

        sel = None
        valid = [str(i) for i in range(1, len(choices) + 1)]
        while sel not in valid + ["q"]:
            sel = input(f"選択 (1-{len(choices)}, qで終了): ").strip()
        if sel == "q":
            break

        choice = choices[int(sel) - 1]
        append_event("state_applied", {"delta": choice["cost"], "choice_label": choice["label"]})
        new_resources = reduce_state()

        if args.consistency_check:
            verdict, c_latency = run_consistency_check(args.model, choice, new_resources, latencies)
            print(f"  [整合性担保役, {c_latency:.1f}秒] {verdict}")

        narration_prompt = (
            f"選んだ選択肢: {choice['label']}\n"
            f"結果の資源状態: {json.dumps(new_resources, ensure_ascii=False)}"
        )
        narration, n_latency = call_ollama(args.model, NARRATION_SYSTEM, narration_prompt, want_json=False)
        latencies.append(n_latency)
        append_event("llm_call", {"role": "narration", "model": args.model, "latency_sec": n_latency, "raw": narration})
        print(f"[{n_latency:.1f}秒] {narration.strip()}")
        print()

    print_summary(latencies, parse_failures, sanitized_count)


if __name__ == "__main__":
    main()
