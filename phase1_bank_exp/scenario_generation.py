# -*- coding: utf-8 -*-
"""LLMによる状況生成という責務全体(プロンプト構築・応答解析・ターン生成・
整合性チェック)を扱う。

Step 7(2026-08-15、behavior-preserving refactoring)でgame.pyから分離した:
build_situation_prompt・build_settlement_prompt・clean_label・
parse_json_response・generate_normal_turn・generate_settlement_turn・
run_consistency_check の7関数を、game.pyから一括で移動した(今回から作業
粒度を「関数1個ずつ」ではなく「責務全体」に変更)。

call_ollama本体・pick_theme・main()・process_one_situationは対象外
(game.pyに残る)。normal行動の選択肢生成(available_normal_archetypes等)・
清算の選択肢生成(build_settlement_choices)・相手選択(pick_social_
counterparty)・NPC台帳の読み取り(acquaintance_npcs・open_contracts)・
直近の状況履歴の読み取り(recent_situations、EVENTS_PATH経由)は、いずれも
game.py側に既にある(または既に他モジュールへ移動済みの)関数を、
ScenarioGenerationDependencies経由で呼び出し時点の関数オブジェクトとして
受け取る——game.py側のCLI上書き・monkeypatchがそのまま伝播する。

RNG消費順序は元のgenerate_normal_turnのまま維持する(due_turn→
prospective_ethics→repay_money→prospective_retire_turn)。simulate_policy()
側の通常行動生成(順序が異なる)は今回も統一しない・移動しない。

Step 8(2026-08-15)で、Step 7時点の申し送り(random.randint/random.uniform
を直接importして使っていた)を修正した。randint_fn/uniform_fnを
ScenarioGenerationDependencies経由で受け取る形に変え、randomモジュールへの
直接依存を無くした——他の*_fn引数(call_ollama_fn等)と同じ、呼び出し時点の
関数オブジェクトを受け取る一貫した設計にするため。呼び出し回数・引数・順序
(due_turn→prospective_ethics→repay_money→prospective_retire_turn)は
変更していない。

このモジュールは game.py・event_store.py・projection.py のいずれにも
依存しない。jsonモジュール(プロンプト中のresources整形・応答解析)のみ
標準ライブラリとして使う。
"""
import json
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ScenarioGenerationDependencies:
    """この7関数が game.py 側の現在値(LLM呼び出し・イベント保存・状態読み取り
    ヘルパー・選択肢生成・定数)を参照するための、明示的な依存の受け渡し容器。
    WorldStateではない(制度スキーマの再設計ではなく、状況生成をgame.pyから
    独立させるための最小限の依存注入)。呼び出しのたびに現在値を読むため、
    CLIやテストによる差し替えがそのまま反映される(import時点で固定しない)。"""
    # LLM呼び出し・イベント保存(game.py本体に残る)
    call_ollama_fn: Callable[[str, str, str, bool], tuple]
    append_event_fn: Callable[[str, dict], None]
    # 状態読み取りヘルパー(game.py側の既存関数。open_contracts/acquaintance_npcs
    # は純粋、recent_situationsはEVENTS_PATH経由でイベントログを読む)
    open_contracts_fn: Callable[[dict], list]
    recent_situations_fn: Callable[[int], list]
    acquaintance_npcs_fn: Callable[[dict], dict]
    # 通常行動の選択肢生成(engine.py委譲、game.py側の薄いラッパー経由)
    compute_normal_money_modifier_fn: Callable[[dict, int], float]
    available_normal_archetypes_fn: Callable[[int], list]
    build_normal_base_choice_fn: Callable[..., dict]
    compute_social_contract_repay_range_fn: Callable[..., tuple]
    pick_social_counterparty_fn: Callable[..., tuple]
    # 清算の選択肢生成(engine.py委譲、game.py側の薄いラッパー経由)
    build_settlement_choices_fn: Callable[..., list]
    # RNG(Step 8で追加。呼び出し時点のrandom.randint/random.uniformを渡す。
    # randomモジュールへの直接依存をやめ、他の*_fn引数と同じ形にするため)
    randint_fn: Callable[[int, int], int]
    uniform_fn: Callable[[float, float], float]
    # 定数(CLIから変更されるものではないが、他の*_dependenciesと同じく明示的に
    # 受け渡す。テストからのgame.py側の一時差し替えにも呼び出し時点で追随する)
    bank_npc_name: str
    situation_system: str
    settlement_system: str
    consistency_system: str
    currency_stage_normal: int
    local_credit_stage_healthy: int
    enforcement_stage_institutional: int
    bank_stage_healthy: int
    barter_stage_functioning: int
    contract_due_range: tuple
    npc_ethics_range: tuple
    npc_relationship_span_range: tuple


def build_situation_prompt(state: dict, turn: int, theme: str,
                           counterparty_hint: tuple = None, *,
                           dependencies: ScenarioGenerationDependencies) -> str:
    """状況生成プロンプト。

    題材(theme)を**先頭**に置き、「この題材以外の話題を出すな」と排他的に指示するのが
    繰り返し対策の要。Phase 1 の probe(README「状況の繰り返し対策」節)で、題材を
    プロンプト末尾に置いた場合(類似度 最大0.42・前ターンの話題の混入あり)より
    明確に効くことを実測した。直近履歴は先頭40字に切り詰めて「参考」として後ろに置く
    (全文を載せると few-shot の見本として機能してしまい、逆に模倣を誘発する)。

    counterparty_hint: pick_social_counterparty()の戻り値(name, is_new)。
    2026-08-13追加(NPC永続化の展開)。social型の相手を「誰でもよいから自然に」
    ではなく「この名前の人物として描写せよ」と明示的に固定する。実際の相手決定は
    コード側(pick_social_counterparty)がすでに終えており、ここはLLMへの指示に
    反映するだけ。
    """
    deps = dependencies
    parts = []
    if theme:
        parts += [f"■今回必ず描写する題材: 「{theme}」",
                  "この題材以外の話題を出さないこと。場所も出来事もこの題材から決めること。"]
    parts += [f"現在のターン: {turn}",
              f"現在の資源状態: {json.dumps(state['resources'], ensure_ascii=False)}"]
    opens = deps.open_contracts_fn(state)
    if opens:
        # run3 では、この未清算の借りが**状況の主題を乗っ取った**(c5の「家具組み立て」が
        # T14・T15の状況を占拠した)。さらにLLMが清算ターンでもないのに「借りを返す」
        # 選択肢を出し、それを選んでも契約テーブルは動かない(=表示と実態の食い違い)
        # という破綻も起きた。用途を「背景の一言」に厳しく限定する。
        pending = "\n".join(
            f"- {c['counterparty']} に「{c['description']}」の借りがある(期限: ターン{c['due_turn']})"
            for c in opens
        )
        parts.append(
            f"(背景情報)まだ返していない借り:\n{pending}\n"
            "この借りは**状況の主題にしないこと**。心に引っかかっている程度に一言触れるだけに\n"
            "とどめ、借りの返済・清算を選択肢に含めてはいけない(返済の場面は別途用意される)。"
        )
    if counterparty_hint:
        name, is_new = counterparty_hint
        if is_new:
            parts.append(
                "social型(人に頼る)を選ぶ場合、頼る相手は今回はじめて登場する"
                "新しい人物にすること(名前を自由に考えて、状況に自然に登場させること)。"
            )
        else:
            parts.append(
                f"social型(人に頼る)を選ぶ場合、頼る相手の名前は必ず「{name}」にすること"
                "(すでに面識のある人物として、これまでの関係を踏まえて自然に描写すること)。"
            )
    history = deps.recent_situations_fn(3)
    if history:
        parts.append("(参考)過去に描写済みの状況。二度と同じ話をしないこと:\n"
                     + "\n".join(f"- {s[:40]}" for s in history))
    return "\n".join(parts)


def build_settlement_prompt(state: dict, turn: int, contract: dict) -> str:
    overdue = turn - contract["due_turn"]
    return "\n".join([
        f"現在のターン: {turn}",
        f"現在の資源状態: {json.dumps(state['resources'], ensure_ascii=False)}",
        f"貸してくれた側(債権者): {contract['counterparty']}",
        f"プレイヤーが {contract['counterparty']} にしてもらったこと: {contract['description']}",
        f"→ したがって、返す義務があるのはプレイヤー。{contract['counterparty']} が催促してくる側。",
        f"借りができたターン: {contract['created_turn']} / 期限: ターン{contract['due_turn']}"
        + (f"(**{overdue}ターン超過**)" if overdue > 0 else "(今日が期限)"),
    ])


def clean_label(text: str, fallback: str) -> str:
    """LLMがJSONスキーマの説明文をそのまま値に混ぜてくることがあるので剥がす。
    実測例(run1 T4): "お金で返す行動の具体的な説明: アドバイス代として100円を返済する。"
    """
    if not isinstance(text, str) or not text.strip():
        return fallback
    text = text.strip()
    for marker in ("の具体的な説明:", "の具体的な説明:", "型の行動:", "の行動:"):
        pos = text.find(marker)
        if 0 <= pos <= 30:
            text = text[pos + len(marker):].strip()
            break
    return text or fallback


def parse_json_response(raw: str) -> dict:
    """format=json を付けていても稀に前後にゴミが付くので、最外の{}を拾う保険を入れる"""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start:end + 1])
        raise


def generate_normal_turn(model: str, state: dict, turn: int, theme: str, latencies: list, *,
                         dependencies: ScenarioGenerationDependencies) -> dict:
    deps = dependencies
    # money型のコストにも、既存の顔なじみがいれば売り手として価格を左右させる
    # (docs/plan.md「[将来] 財の価格形成」の簡略実装。需要供給はまだ未実装で、
    # 倫理観・信用〈ブランド〉の2軸のみ)。顔なじみがまだ居ない序盤は中立(1.0)。
    acquaintances = deps.acquaintance_npcs_fn(state)
    # 2026-08-14追加(社会レジーム仕様「通貨(そのものへの信用)」節、実装
    # フェーズ第1弾)。stateはreduce_state()の戻り値そのものなので
    # currency_stageも既に含まれている——新しい引数を増やさずに済む。
    currency_stage = state.get("currency_stage", deps.currency_stage_normal)
    # 2026-08-15追加(地域信用制度、ユーザーレビュー指摘への対応、Stage3の
    # 行動的帰結): currency_stageと同じパターンでstateから読む。
    local_credit_stage = state.get("local_credit_stage", deps.local_credit_stage_healthy)
    # 2026-08-15追加(Step 13、物々交換・自給制度)。currency_stage/local_credit_
    # stageと同じパターンでstateから読む。
    bank_stage = state.get("bank_stage", deps.bank_stage_healthy)
    enforcement_stage = state.get("enforcement_stage", deps.enforcement_stage_institutional)
    barter_stage = state.get("barter_stage", deps.barter_stage_functioning)
    money_modifier = deps.compute_normal_money_modifier_fn(acquaintances, currency_stage)

    # social型の相手を先に(LLM呼び出し前に)コード側で決める(2026-08-13追加、
    # NPC永続化の展開)。既存の相手を再利用するときだけLLMに名前を指定する。
    # 新規の場合はNoneを返す約束なので、そのときだけLLMに自由に名付けさせる
    # (固定プールに縛られない。疎遠になった相手の名前とも衝突しない)。
    open_cps = {c["counterparty"] for c in deps.open_contracts_fn(state)}
    counterparty_hint = deps.pick_social_counterparty_fn(
        acquaintances, open_cps, turn, state["npc_pool_cap"])

    prompt = build_situation_prompt(state, turn, theme, counterparty_hint, dependencies=deps)
    raw, latency = deps.call_ollama_fn(model, deps.situation_system, prompt, want_json=True)
    latencies.append(latency)
    deps.append_event_fn("llm_call", {"role": "situation", "model": model,
                                      "latency_sec": latency, "theme": theme, "raw": raw})
    parsed = parse_json_response(raw)
    if "situation" not in parsed or "choices" not in parsed:
        raise ValueError("situation/choices が無い")

    archetypes = deps.available_normal_archetypes_fn(
        currency_stage, local_credit_stage, bank_stage, enforcement_stage, barter_stage)
    alternative_available = any(
        archetype["key"] in ("barter", "subsistence") for archetype in archetypes)
    goods_state = ({
        "food": state["food"], "medicine": state["medicine"],
        "shelter": state["shelter"], "tools": state["tools"],
        "production_capacity": state["production_capacity"],
    } if alternative_available else None)
    choices = []
    for archetype in archetypes:
        label = clean_label(parsed["choices"].get(archetype["key"]), archetype["name"])
        if goods_state is None:
            choice = deps.build_normal_base_choice_fn(
                archetype, turn, money_modifier, label=label)
        else:
            choice = deps.build_normal_base_choice_fn(
                archetype, turn, money_modifier, label=label, goods_state=goods_state)
        if archetype.get("creates_contract"):
            # 既存の相手を再利用する場合はコードが決めた名前をそのまま使う
            # (名前の一致頼みの永続化はやめた)。新規の場合だけ、LLMが自然に
            # 生成した名前(social_counterparty)を採用する(2026-08-13変更)。
            hint_name, is_new = counterparty_hint
            if is_new:
                # 2026-08-14追加(7回目opusレビュー指摘・致命的1): is_new=Trueは
                # 「新規に1人登場させる」という契約のはずが、LLMが返す名前は
                # 自由記述なので、既存NPC(顔なじみ・銀行)と衝突すると
                # npc_introducedが発火せず、pick_social_counterpartyが
                # 決めた「この相手はプールに居ない」という判断(trustゲート・
                # 関係の寿命・pool_capの前提)ごと静かにバイパスされていた
                # (probe側は合成カウンタ名〈npcN〉で常に一意なので、この穴は
                # main()専用で--policy-checkでは検出できない)。
                # 「is_new=True ⇒ 必ず新しいNPCレコードが1件生まれる」という
                # 不変条件を、名前が衝突する限りサフィックスを付けて守る。
                counterparty = str(parsed.get("social_counterparty") or "知人")
                base_name, suffix = counterparty, 2
                while counterparty in state["npcs"] or counterparty == deps.bank_npc_name:
                    counterparty = f"{base_name}{suffix}"
                    suffix += 1
            else:
                counterparty = hint_name
            existing = None if is_new else acquaintances.get(counterparty)
            # RNG順序(due_turn→prospective_ethics→repay_money→
            # prospective_retire_turn)は元のgenerate_normal_turnのまま維持する
            # (simulate_policyとは順序が異なるが、今回は統一しない)。
            due_turn = turn + deps.randint_fn(*deps.contract_due_range)
            prospective_ethics = None if existing else deps.uniform_fn(*deps.npc_ethics_range)
            trust_for_limit, lo, hi = deps.compute_social_contract_repay_range_fn(
                acquaintances, existing, prospective_ethics, turn, due_turn)
            choice["contract"] = {
                "counterparty": counterparty,
                "description": str(parsed.get("social_favor") or label)[:40],
                "due_turn": due_turn,
                "repay_money": deps.randint_fn(round(min(lo, hi)), round(max(lo, hi))),
                # 初対面の場合のみ使う、新規NPC登場時のethics・関係の寿命
                # (2026-08-13追加)。既存NPCならNoneのまま(このターンでは
                # 倫理観・寿命を変えない)。
                "prospective_ethics": prospective_ethics,
                "prospective_retire_turn": None if existing else
                    turn + deps.randint_fn(*deps.npc_relationship_span_range),
            }
        choices.append(choice)
    return {"situation": parsed["situation"], "choices": choices,
            "latency": latency, "kind": "normal"}


def generate_settlement_turn(model: str, state: dict, turn: int, contract: dict,
                             latencies: list, *,
                             dependencies: ScenarioGenerationDependencies) -> dict:
    deps = dependencies
    prompt = build_settlement_prompt(state, turn, contract)
    raw, latency = deps.call_ollama_fn(model, deps.settlement_system, prompt, want_json=True)
    latencies.append(latency)
    deps.append_event_fn("llm_call", {"role": "settlement_situation", "model": model,
                                      "latency_sec": latency, "contract_id": contract["id"],
                                      "raw": raw})
    parsed = parse_json_response(raw)
    if "situation" not in parsed or "choices" not in parsed:
        raise ValueError("situation/choices が無い")

    currency_stage = state.get("currency_stage", deps.currency_stage_normal)
    # 2026-08-15追加(契約執行制度): currency_stageと同じパターンでstateから読む。
    enforcement_stage = state.get("enforcement_stage", deps.enforcement_stage_institutional)
    choices = deps.build_settlement_choices_fn(
        contract, turn, currency_stage, labels=parsed["choices"],
        enforcement_stage=enforcement_stage)
    return {"situation": parsed["situation"], "choices": choices,
            "latency": latency, "kind": "settlement", "contract": contract}


def run_consistency_check(model: str, down: list, up: list, narration: str,
                          latencies: list, log: bool = True, *,
                          dependencies: ScenarioGenerationDependencies) -> tuple:
    """ナレーション文が実際の資源変化の向きと矛盾していないかを検査する。
    2026-08-12修正(1回目): 旧版はコストの数値(構造上、違反が発生し得ない)を見ていたため、
    69/69回すべて"OK"を返す空回りだった(phase1初版の実測で判明)。実際に破綻するのは
    ナレーション文なので、そちらを検査対象に変更。
    2026-08-12修正(2回目): 1回目の修正版を否定制御テストにかけたところ、矛盾3件を
    3件とも見逃した(検出率0%)。JSON形式強制+具体例提示に変更し、戻り値を
    (flagged: bool, reason: str, latency) に変える。"""
    deps = dependencies
    prompt = (
        f"実際に減った資源: {'、'.join(down) if down else 'なし'}\n"
        f"実際に増えた資源: {'、'.join(up) if up else 'なし'}\n"
        f"ナレーション文: {narration.strip()}"
    )
    raw, latency = deps.call_ollama_fn(model, deps.consistency_system, prompt, want_json=True)
    latencies.append(latency)
    if log:
        deps.append_event_fn("llm_call", {"role": "consistency_check", "model": model,
                                          "latency_sec": latency, "raw": raw})
    try:
        parsed = parse_json_response(raw)
        flagged = bool(parsed.get("contradiction"))
        reason = str(parsed.get("reason") or "")
    except Exception:
        flagged, reason = False, f"[JSON解析失敗: {raw[:60]}]"
    return flagged, reason, latency
