# -*- coding: utf-8 -*-
"""main()の対話ループが1状況ぶんの「提示・選択・適用・ナレーション・整合性
チェック」を処理する責務(process_one_situation)を扱う。

Step 8(2026-08-15、behavior-preserving refactoring)でgame.pyから移動した。
main()内のネスト関数だったprocess_one_situationを、処理全体のまとまりの
まま(関数を分割せず)ここへ移す。状況表示・選択肢表示・手動/自動選択・
state_applied/契約/清算効果イベント・stats更新・ナレーション生成・
code check/consistency checkをすべて含む。

main()側に残るもの: ターン開始時の自動処理(regen/income/decay等)・
期限到来契約のループ制御・時間予算ループ制御・月末の成長判定・
character_diedの判定——process_one_situationはこれらのループから
「1状況ぶん」だけを呼ばれる。simulate_policy()は対象外(イベント方式では
なく直接更新方式のまま、今回も統一しない)。

元のprocess_one_situationはmain()のローカル変数(turn・args・stats・
situations_this_run・latencies・decay)をクロージャ経由で参照していた
(mutableなstats/situations_this_run/latenciesはdictやlistの要素代入・
appendで書き換えていたため、nonlocal宣言なしでも呼び出し側と共有できて
いた)。ここでは明示的な引数として受け取る形にする——mutableな3つは
そのまま呼び出し側と同じオブジェクト参照を渡す(従来どおり共有される)。

元のnonlocal parse_failures宣言は、関数本体のどこからも実際には代入されて
いない未使用の宣言だった(Step 6Aで発見済み)。移動にあたり、この1点だけは
指示により削除する(それ以外の仕様整理・数値調整はしない)。

theme_trait引数は元の実装でも関数本体から一度も参照されていない未使用の
パラメータ(呼び出し元との既存シグネチャ互換のためだけに存在する)——
今回の移動でもそのまま維持する(新たな発見だが、修正はしない)。

このモジュールは game.py・event_store.py・projection.py のいずれにも
依存しない。append_event・reduce_state・LLM呼び出し・engine/event_builders
系の計算はすべてInteractiveRuntimeDependencies経由のcallbackとして受け取る。
"""
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class InteractiveRuntimeDependencies:
    """process_one_situation()が game.py 側の現在値(イベント保存・状態読み取り・
    選択肢の採否判定・自動選択・数値ルール・LLM呼び出し・定数)を参照するための、
    明示的な依存の受け渡し容器。WorldStateではない(制度スキーマの再設計ではなく、
    対話ループの1状況処理をgame.pyから独立させるための最小限の依存注入)。
    呼び出しのたびに現在値を読むため、CLIやテストによる差し替えがそのまま
    反映される(import時点で固定しない)。"""
    append_event_fn: Callable[[str, dict], None]
    reduce_state_fn: Callable[[], dict]
    is_affordable_fn: Callable[..., bool]
    auto_select_fn: Callable[..., int]
    policy_score_fn: Callable[..., float]
    plan_normal_action_resolution_fn: Callable[..., dict]
    clamp_gain_fn: Callable[[dict, dict, dict], dict]
    build_normal_contract_events_fn: Callable[..., list]
    plan_settlement_resolution_fn: Callable[..., dict]
    build_settlement_effect_events_fn: Callable[..., list]
    call_ollama_fn: Callable[[str, str, str, bool], tuple]
    code_check_narration_fn: Callable[[list, str], list]
    run_consistency_check_fn: Callable[..., tuple]
    policy_vectors: dict
    npc_trust_initial: float
    bank_npc_id: str
    resource_ja: dict
    allowed_resources: set
    narration_system: str


def process_one_situation(tr: dict, theme_trait, cur_state: dict, budget, do_growth: bool, *,
                          turn: int, args, stats: dict, situations_this_run: list,
                          latencies: list, decay: dict,
                          dependencies: InteractiveRuntimeDependencies):
    """1つの状況を提示・選択・適用・(必要なら)成長判定・ナレーション・
    整合性チェックまで処理する。2026-08-14追加(時間予算制の統合)。
    通常ターンは月内の時間予算が尽きるまで複数回呼ばれうるため、
    この単位に切り出した(settlementは今までどおり1回だけ呼ぶ)。
    成長判定(compute_trait_step)は月に1回だけ(do_growthがTrueの回のみ)
    ——判定機会そのものは「1ターン=1ヶ月に1回」という既存の較正を
    崩さないため。戻り値: 選ばれたchoice(通常ターンのみ"hours"キーを
    持つ)。手動プレイで q が入力されたらNoneを返す。"""
    deps = dependencies
    deps.append_event_fn("situation_presented",
                         {"turn": turn, "kind": tr["kind"], "situation": tr["situation"]})
    situations_this_run.append(tr["situation"])

    print(f"[{tr['latency']:.1f}秒] {tr['situation']}")
    choices = tr["choices"]
    available = []
    for i, c in enumerate(choices, 1):
        cost_str = ", ".join(f"{k}{v:+d}" for k, v in c["cost"].items()) or "変化なし"
        hours_str = f"{c['hours']}h, " if "hours" in c else ""
        extra = ""
        if "contract" in c:
            extra = (f" [借りが発生: {c['contract']['counterparty']}"
                     f" / 期限T{c['contract']['due_turn']}]")
        if c.get("settle") == "defaulted":
            extra = " [不履行になる]"
        elif c.get("settle") == "fulfilled":
            extra = " [借りを返す]"
        # 払えない/時間が足りない選択肢は選べない(資源・時間が青天井に
        # マイナスへ沈むのを防ぐ)。不履行だけは常に選べる。
        if deps.is_affordable_fn(c, cur_state["resources"], budget):
            available.append(i)
        else:
            reason = ("時間が足りず選べない"
                     if budget is not None and c.get("hours", 0) > budget
                     else "資源不足で選べない")
            extra += f" ※{reason}"
            stats["blocked_choices"] += 1
        print(f"  {i}. {c['label']} ({hours_str}{cost_str}){extra}")

    # --- 3. 選択 -------------------------------------------------------
    if args.auto:
        idx = deps.auto_select_fn(choices, cur_state["resources"], args.auto_policy,
                                  args.policy, args.safety_floor, turn, budget)
        score_str = ""
        if args.policy != "none":
            vec = deps.policy_vectors[args.policy]
            score_str = " / 方針スコア " + ", ".join(
                f"{c['key']}={deps.policy_score_fn(c, vec, turn, cur_state['resources']):+.2f}"
                for c in choices)
            deps.append_event_fn("auto_choice", {
                "turn": turn, "policy": args.policy, "kind": tr["kind"],
                "picked": choices[idx]["key"],
                "scores": {c["key"]: round(deps.policy_score_fn(c, vec, turn, cur_state["resources"]), 3)
                          for c in choices},
                "affordable": [c["key"] for c in choices
                               if deps.is_affordable_fn(c, cur_state["resources"], budget)],
            })
        print(f"選択(自動/{args.auto_policy}+{args.policy}): {idx + 1}{score_str}")
    else:
        valid = [str(i) for i in available] or [str(i) for i in range(1, len(choices) + 1)]
        sel = None
        while sel not in valid + ["q"]:
            sel = input(f"選択 ({'/'.join(valid)}, qで終了): ").strip()
        if sel == "q":
            return None
        idx = int(sel) - 1
    choice = choices[idx]

    # --- 4. 適用(資源・契約テーブルの更新はすべてコード側) --------------
    # 2026-08-15(Step 6D、段階的な数値ルール共通化): 通常行動(tr["kind"]
    # =="normal")の「選択済みchoice→clamp_gain適用」だけをplan_normal_
    # action_resolution()へ委譲した。process_one_situationは清算にも
    # 使われる共有関数なので、settlementは対象外のまま従来のclamp_gain
    # 直接呼び出しを維持する(Step 6Cのplan_settlement_resolutionが
    # 別途扱う領域のため、ここでは触らない)。
    if tr["kind"] == "normal":
        choice["cost"] = deps.plan_normal_action_resolution_fn(
            choice, cur_state["resources"], cur_state["traits"])["delta"]
    else:
        choice["cost"] = deps.clamp_gain_fn(choice["cost"], cur_state["resources"], cur_state["traits"])
    deps.append_event_fn("state_applied", {"turn": turn, "delta": choice["cost"],
                                           "choice_key": choice["key"],
                                           "choice_label": choice["label"]})
    if "contract" in choice:
        state_now = deps.reduce_state_fn()
        cid = f"c{len(state_now['contracts']) + 1}"
        c = choice["contract"]
        # 2026-08-15(Step 6F、段階的モジュール分割): NPC登場・契約作成の
        # イベント列の組み立てをbuild_normal_contract_events()へ委譲した。
        # contract_idの採番・reduce_state()の呼び出しはここに残す
        # (相手が新規かどうかの判定に必要なstate_nowを渡すだけ)。
        for event_type, payload in deps.build_normal_contract_events_fn(choice, state_now, turn, cid):
            deps.append_event_fn(event_type, payload)
        stats["contracts_created"] += 1
        print(f"    → 契約発生: {cid} / {c['counterparty']} / 期限T{c['due_turn']}")
    if choice.get("settle"):
        deps.append_event_fn("contract_settled", {
            "id": tr["contract"]["id"], "turn": turn, "outcome": choice["settle"],
            "settled_by": choice["key"], "delta": {},  # 資源変化はstate_applied側に計上済み
        })
        stats["fulfilled" if choice["settle"] == "fulfilled" else "defaulted"] += 1
        print(f"    → 契約 {tr['contract']['id']} は "
              f"{'履行' if choice['settle'] == 'fulfilled' else '不履行'}")
        # 2026-08-14(Step 3C-2、段階的な数値ルール共通化): 清算の数値効果
        # (銀行trust・銀行wallet・通貨confidence・相手NPC trust・通貨危機の
        # 判定と影響)はplan_settlement_effects()に集約済み(simulate_policyと
        # 共通)。ここでは清算する契約1件につき正確に1回だけ呼び、戻り値を
        # 使って従来と同じイベントを同じ順序・同じpayload・同じreasonで積む
        # だけにする——projection.pyのイベント意味論(bank_crisisイベントが
        # currency_confidenceの低下を解釈する等)は変更しない。
        cp_id = None if tr["contract"].get("is_bank_debt") else tr["contract"]["counterparty"]
        cp_trust_before = (cur_state["npcs"].get(cp_id, {}).get("trust", deps.npc_trust_initial)
                           if cp_id is not None else None)
        # 2026-08-15(Step 6C、段階的な数値ルール共通化): 「選択済みchoice
        # →plan_settlement_effects呼び出し」の接着をplan_settlement_
        # resolution()へ委譲した(呼び出し回数・渡す引数は変更していない)。
        effects = deps.plan_settlement_resolution_fn(
            tr["contract"], choice, turn,
            bank_trust=cur_state["npcs"][deps.bank_npc_id]["trust"],
            currency_confidence=cur_state["currency_confidence"],
            bank_credit_losses=cur_state["bank_credit_losses"],
            counterparty_trust=cp_trust_before,
            # 2026-08-15追加(契約執行制度)。
            enforcement_stage=cur_state["enforcement_stage"],
            enforcement_capacity=cur_state["enforcement_capacity"],
        )["effects"]
        # 2026-08-15(Step 6E、段階的モジュール分割): イベント列の組み立て
        # (順序・payload・reasonの決定)はbuild_settlement_effect_events()へ
        # 委譲した。ここでは戻り値を順番にappend_eventするだけにする
        # (state_applied・contract_settledは対象外、既に上で積んである)。
        # bank_crisis_countはcur_state(このcontractの処理開始時点で
        # reduce_state()したもの)の値をそのまま使う——ここまでの間に
        # bank_crisisイベントは一切appendされていないので、旧実装の
        # 「reduce_state()を都度呼び直す」のと同じ値になる。
        for event_type, payload in deps.build_settlement_effect_events_fn(
                tr["contract"], choice, effects, turn,
                deps.bank_npc_id, cur_state["bank_crisis_count"]):
            deps.append_event_fn(event_type, payload)
        if (tr["contract"].get("is_bank_debt") and choice["settle"] == "defaulted"
                and effects["crisis_triggered"]):
            # 危機そのものが銀行の信用を大きく損なう(半減)。通貨
            # confidenceの低下は、従来どおりprojection.pyがbank_crisis
            # イベントを解釈して適用する(currency_confidence_changed
            # イベントを新しく追加しない)。
            bank_trust_now = (cur_state["npcs"][deps.bank_npc_id]["trust"]
                              + effects["bank_trust_delta"])
            print(f"    ★ 通貨危機発生(信用損失 {effects['bank_credit_losses_after']:.1f} "
                  f"≥ 閾値 {effects['crisis_threshold']:.1f}〈銀行trust "
                  f"{bank_trust_now:.1f}由来〉): "
                  f"moneyが1/{effects['rebase_factor']}にデノミされた")

    # --- 4b. 特性の更新(docs/plan.md「特性の成長経路」) -----------------
    # 資源と同じく、増減の判定・量はすべてコード側。LLMには関与させない
    # (LLMは題材の文章を書くだけで、その題材がどの特性に対応するかも知らない)。
    # 2026-08-14訂正(重大5対応): 清算ターンを複数契約まとめて処理する
    # ようにしたのに伴い、do_growthは呼び出し側で常にFalseになった
    # (成長判定は呼び出し側〈settlement/normal どちらのループも〉が
    # 月末に1回だけまとめて行う)。do_growth=Trueの経路は使われなく
    # なったので削除し、do_growthパラメータ自体も後方互換のためだけに
    # 残す(呼び出し側を書き換える手間を避けるため。実質は常にFalse)。
    new_state = deps.reduce_state_fn()

    ckey = f"{'清算' if tr['kind'] == 'settlement' else '通常'}:{choice['key']}"
    stats["choice_counts"][ckey] = stats["choice_counts"].get(ckey, 0) + 1
    for k, v in new_state["resources"].items():
        stats["min_seen"][k] = min(stats["min_seen"].get(k, v), v)

    # --- 5. ナレーション -------------------------------------------------
    # ナレーションには数値ではなく「何が減って何が増えたか」の向きだけを渡す。
    # 数値をそのまま渡すと読み上げてしまい、向きを渡さないと無から回復を捏造する
    # (Phase 0 で観測された2つの破綻)。維持コスト(decay)はsettlementでのみ
    # ここで即座に適用されるため、そのナレーションにだけ含める(通常ターンの
    # 各activityは月末にまとめて処理するため、個々のナレーションには含めない
    # ——2026-08-14、成長判定の月末一本化に伴う変更)。
    turn_delta = dict(decay if do_growth else {})
    for k, v in choice["cost"].items():
        turn_delta[k] = turn_delta.get(k, 0) + v
    down_keys = [k for k, v in turn_delta.items() if v < 0]
    down = [deps.resource_ja[k] for k in down_keys]
    up_keys = [k for k, v in turn_delta.items() if v > 0]
    up = [deps.resource_ja[k] for k in up_keys]
    not_up_keys = [k for k in deps.allowed_resources if k not in up_keys]
    narration_prompt = (
        f"選んだ選択肢: {choice['label']}\n"
        f"この行動で減ったもの: {'、'.join(down) if down else 'なし'}\n"
        f"増えたもの: {'、'.join(up) if up else 'なし'}"
    )
    if choice.get("settle") == "fulfilled":
        narration_prompt += f"\n補足: {tr['contract']['counterparty']} への借りを返し終えた。"
    elif choice.get("settle") == "defaulted":
        narration_prompt += (f"\n補足: {tr['contract']['counterparty']} への借りを"
                             f"返せなかった。相手の心証は悪い。")
    elif "contract" in choice:
        narration_prompt += (f"\n補足: {choice['contract']['counterparty']} に"
                             f"借りができた。いずれ返さねばならない。")
    narration, n_latency = deps.call_ollama_fn(args.model, deps.narration_system, narration_prompt,
                                               want_json=False)
    latencies.append(n_latency)
    deps.append_event_fn("llm_call", {"role": "narration", "model": args.model,
                                      "latency_sec": n_latency, "raw": narration})
    print(f"[{n_latency:.1f}秒] {narration.strip()}")

    # --- 6. 整合性担保役 --------------------------------------------------
    # 2026-08-12修正: ナレーション**後**に、ナレーション文そのものを検査する形に
    # 変更(旧版はコストの数値をナレーション生成の前に見ていたため、実際に破綻が
    # 起きていた場所=ナレーション文を一度も見ておらず、構造上ずっと空回りしていた)。
    # さらに、否定制御テストでLLM版の検出率が低いと分かったため(33%)、
    # コード側の正規表現チェック(無料・瞬時)を先に走らせ、どちらかが引っかかれば
    # 矛盾とする。
    code_issues = deps.code_check_narration_fn(not_up_keys, narration)
    if code_issues:
        stats["code_check_hits"] += 1
        deps.append_event_fn("code_check_flag", {"turn": turn, "issues": code_issues})
        print(f"  [コード側チェック] 矛盾検出: {'; '.join(code_issues)}")
    if not args.no_consistency_check:
        flagged, reason, c_latency = deps.run_consistency_check_fn(
            args.model, down, up, narration, latencies)
        if flagged:
            stats["llm_check_hits"] += 1
        verdict_str = f"矛盾検出: {reason}" if flagged else "OK"
        print(f"  [整合性担保役(LLM), {c_latency:.1f}秒] {verdict_str}")
    print()
    return choice
