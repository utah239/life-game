# -*- coding: utf-8 -*-
"""イベントソーシング方式のゲームセッション実行という責務全体を扱う。

Step 10(2026-08-15、behavior-preserving refactoring)でgame.pyのmain()から
一括で移動した(今回も関数1個ずつではなく責務全体の粒度):
- NPC(銀行・地域経済)/キャラクター(誕生・才能・npc_pool_cap)の初期化
- 死亡済みキャラクターでの再開の拒否
- ターン開始時の自動処理(regen/銀行・通貨の信用回帰/制度遷移/income/salary/decay)
- 期限到来契約の清算ループ
- 通常行動の時間予算ループ
- 月末の成長判定・死亡判定・端数休息
- セッション終了時の計測サマリ(bigrams/repetition_reportによる繰り返し度も含む)

main()に残るもの: argparse・CLI設定値の上書き(D_BASE/CREATION_RATE)・
negative-control/simulate/policy-check/policy-probeの分岐・
run_game_session(args)の呼び出し。EVENTS_PATHの設定自体もgame.py側の
グローバル変数を書き換える処理なのでmain()に残り、その時点の値をGameSession
Configへ渡す。

process_one_situation(1状況の提示・選択・適用・ナレーション・整合性チェック)
はinteractive_runtime.pyの実装をそのまま呼ぶ(再実装しない)。呼び出しの
たびにturn・decayが変わるため、このモジュール内のネスト関数(main()に
あった元の構造のまま)としてクロージャ経由でそれらを渡す。

このモジュールは game.py・event_store.py・projection.py のいずれにも
依存しない。interactive_runtime(process_one_situation)は明示的に許可
されている依存として直接importする。random.seed/choice/randintは
Dependencies経由で呼び出し時点の関数オブジェクトを受け取る(直接importしない)。
"""
from dataclasses import dataclass
from typing import Callable

import interactive_runtime


@dataclass(frozen=True)
class GameSessionDependencies:
    """run_game_session()が game.py 側の現在値(RNG・イベント保存/状態読み取り・
    ターン処理の既存共有関数・表示関数・process_one_situationの依存構築)を
    参照するための、明示的な依存の受け渡し容器。WorldStateではない(制度
    スキーマの再設計ではなく、セッション実行をgame.pyから独立させるための
    最小限の依存注入)。呼び出しのたびに現在値を読むため、CLIやテストによる
    差し替えがそのまま反映される(import時点で固定しない)。"""
    seed_fn: Callable[[int], None]
    choice_fn: Callable[[list], object]
    randint_fn: Callable[[int, int], int]
    append_event_fn: Callable[[str, dict], None]
    reduce_state_fn: Callable[[], dict]
    compute_regen_fn: Callable[[dict, dict], dict]
    plan_confidence_reversion_fn: Callable[[float, float], dict]
    plan_institution_transitions_fn: Callable[..., dict]
    # 2026-08-15追加(地域信用制度)。bank/currencyと並行するcommunity_trustの
    # 回帰・stage遷移(institutions/local_credit.pyの再公開、turn_engine.py経由)。
    plan_local_credit_reversion_fn: Callable[[float], dict]
    plan_local_credit_transition_fn: Callable[[int, float], dict]
    # 2026-08-15追加(契約執行制度)。bank/currency/local_creditと並行する
    # enforcement_capacityの回帰・stage遷移(institutions/contract_enforcement.py
    # の再公開、turn_engine.py経由)。
    plan_enforcement_reversion_fn: Callable[[float], dict]
    plan_enforcement_transition_fn: Callable[[int, float], dict]
    # 2026-08-15追加(Step 13、物々交換・自給制度)。他制度と違い単一スカラーの
    # reversionではなく、4財+production_capacityのupkeepをまとめて計画する
    # (institutions/barter.pyの再公開、turn_engine.py経由)。
    plan_barter_upkeep_fn: Callable[..., dict]
    plan_barter_transition_fn: Callable[[int, float], dict]
    worst_shortfall_fn: Callable[[float, float, float, float], tuple]
    essential_goods_shortage_penalty_fn: Callable[[int], dict]
    barter_choice_effects_fn: Callable[[str, float], dict]
    # 上位制度(銀行・通貨・地域信用・契約執行)の崩壊・縮退判定
    # (engine.alternative_economy_triggeredの再公開、game.py側の薄い
    # ラッパー経由)。available_normal_archetypes_fn同様、engine.pyを直接
    # importせずDependencies経由で受け取る。
    alternative_economy_triggered_fn: Callable[[int, int, int, int], bool]
    compute_income_fn: Callable[[int, int], dict]
    compute_salary_fn: Callable[[int], int]
    compute_decay_fn: Callable[[int, dict], dict]
    format_resources_fn: Callable[[dict], str]
    format_contracts_fn: Callable[[dict], str]
    due_contracts_fn: Callable[[dict, int], list]
    generate_settlement_turn_fn: Callable[..., dict]
    generate_normal_turn_fn: Callable[..., dict]
    pick_theme_fn: Callable[[list, list], tuple]
    compute_trait_step_fn: Callable[..., tuple]
    age_at_fn: Callable[[int], float]
    format_traits_fn: Callable[[dict, str], str]
    is_affordable_fn: Callable[..., bool]
    clamp_gain_fn: Callable[[dict, dict, dict], dict]
    draw_prorated_rest_fn: Callable[[int, float], dict]
    price_index_fn: Callable[[int], float]
    open_contracts_fn: Callable[[dict], list]
    # process_one_situation(interactive_runtime.py、再実装しない)が必要とする
    # InteractiveRuntimeDependenciesを、呼び出しのたびに新しく組み立てる
    # game.py側のヘルパー(game._interactive_runtime_dependencies)をそのまま
    # 受け取る——ここでも呼び出し時点の関数オブジェクト・定数に追随する。
    interactive_runtime_dependencies_fn: Callable[[], "interactive_runtime.InteractiveRuntimeDependencies"]
    # 2026-08-15追加(自己レビュー指摘への対応): サマリの繰り返し度計測
    # (repetition_report/bigrams)の実装本体はこのモジュールだけに置き、
    # game.py側は薄い互換ラッパーにする。run_game_session()はこのモジュール
    # 自身のrepetition_report()をsibling呼び出しするのではなく、
    # repetition_report_fn経由でgame.py側の関数オブジェクト(game.
    # repetition_report、内部でgame.bigramsも呼ぶ)を呼ぶ——game.
    # repetition_report/game.bigramsのmonkeypatchが伝播するようにする。
    repetition_report_fn: Callable[[list], dict]


@dataclass(frozen=True)
class GameSessionConfig:
    """run_game_session()が game.py 側の現在値(定数)を参照するための、
    明示的な設定値の受け渡し容器。events_pathはmain()がEVENTS_PATHグローバルを
    書き換えた**後**の値を、呼び出し時点でここへ渡す。"""
    events_path: object
    bank_npc_id: str
    bank_npc_name: str
    bank_trust_initial: float
    economy_npc_id: str
    economy_npc_name: str
    npc_trust_initial: float
    traits: list
    npc_pool_cap_range: tuple
    initial_traits: dict
    start_age: float
    years_per_turn: float
    bank_stage_names: dict
    currency_stage_names: dict
    local_credit_stage_names: dict
    local_credit_scope: str
    enforcement_stage_names: dict
    enforcement_scope: str
    barter_stage_names: dict
    barter_stage_trigger_names: dict
    barter_scope: str
    contract_due_range: tuple
    theme_concern_traits: dict
    g_base: dict
    b_talent: float
    trait_ja: dict
    turn_time_budget: float
    min_activity_hours: float
    initial_resources: dict
    policy_vectors: dict


def bigrams(text: str) -> set:
    t = "".join(text.split())
    return {t[i:i + 2] for i in range(len(t) - 1)}


def repetition_report(situations: list, *, bigrams_fn: Callable[[str], set] = None) -> dict:
    """全ペアの文字bigram Jaccard類似度。最大・平均と、0.5超のペア数を返す。

    bigrams_fn(省略時はこのモジュール自身のbigrams()を使う)は、game.py側の
    ラッパーがgame.bigrams(呼び出し時点でmonkeypatchされうる関数オブジェクト)
    をそのまま渡せるようにするための差し替え口——他のengine.py/scenario_
    generation.py内関数の*_fn引数と同じパターン(呼び出し元の互換ラッパー経由の
    monkeypatchを伝播させるため)。"""
    if len(situations) < 2:
        return {"pairs": 0, "max": 0.0, "mean": 0.0, "near_duplicates": 0}
    fn = bigrams_fn or bigrams
    grams = [fn(s) for s in situations]
    sims = []
    dup = 0
    for i in range(len(grams)):
        for j in range(i + 1, len(grams)):
            union = grams[i] | grams[j]
            sim = len(grams[i] & grams[j]) / len(union) if union else 0.0
            sims.append(sim)
            if sim > 0.5:
                dup += 1
    return {"pairs": len(sims), "max": max(sims),
            "mean": sum(sims) / len(sims), "near_duplicates": dup}


def run_game_session(args, *, dependencies: GameSessionDependencies,
                     config: GameSessionConfig) -> None:
    """1回のCLI実行ぶんのゲームセッション(イベントソーシング方式)を実行する。
    main()から処理全体のまとまりのまま移動した(Step 10、段階的モジュール分割)。
    events.jsonlへの読み書きはすべてdependencies経由(game.py側のappend_event/
    reduce_stateラッパー)で行う——このモジュール自身はevent_store.py・
    projection.pyのいずれにも依存しない。"""
    deps = dependencies
    cfg = config

    if args.seed is not None:
        deps.seed_fn(args.seed)

    policy_desc = (f"{cfg.policy_vectors[args.policy]['label']}({args.policy})"
                   if args.policy != "none" else "none")
    print(f"=== 人生ゲーム Phase 1 (model={args.model}, auto={args.auto}, "
          f"policy={policy_desc}, theme-injection={args.theme_injection}, seed={args.seed}) ===")
    print(f"events: {cfg.events_path}\n")

    latencies, parse_failures = [], 0
    situations_this_run = []
    used_places, used_concerns = [], []
    stats = {"settlement_turns": 0, "settled_contracts": 0, "contracts_created": 0,
             "fulfilled": 0, "defaulted": 0,
             "decay_total": {}, "income_total": {}, "blocked_choices": 0,
             "code_check_hits": 0, "llm_check_hits": 0,
             "choice_counts": {}, "min_seen": dict(cfg.initial_resources),
             # 特性実験用
             "theme_turns": {}, "no_theme_turns": 0,
             "fire_counts": {}, "fires": 0, "played_turns": 0}

    # --- 銀行NPCの初回登場(docs/plan.md「経済の閉じ方 v1実装設計」) ---------------
    # 世界に1体だけ。まだ居なければturn=0で登場させる。財布の下限は無い
    # (下限が無いこと自体が「銀行は信用で貨幣を創造できる」という設定の表現)。
    pre_state = deps.reduce_state_fn()
    if cfg.bank_npc_id not in pre_state["npcs"]:
        deps.append_event_fn("npc_introduced", {
            "id": cfg.bank_npc_id, "name": cfg.bank_npc_name, "birth_turn": 0,
            "role": "money_issuer", "trust": cfg.bank_trust_initial,
        })
    # 2026-08-14追加(社会レジーム仕様「銀行が壊れても労働による所得は残る」の
    # 前提)。salary(前借りでない本当の所得)の発行主体を銀行から切り離すため、
    # 別のインフラ役NPCを新設する。role="wage_issuer"はacquaintance_npcs()
    # から除外される(role="acquaintance"ではないため)。
    if cfg.economy_npc_id not in pre_state["npcs"]:
        deps.append_event_fn("npc_introduced", {
            "id": cfg.economy_npc_id, "name": cfg.economy_npc_name, "birth_turn": 0,
            "role": "wage_issuer", "trust": cfg.npc_trust_initial,  # 未使用の場を安全な値で埋めるだけ
        })

    # --- 誕生(特性の初期値と、生まれ持ったスキル=才能) ------------------------
    # docs/plan.md では誕生時3択だが、Phase 1では指示どおりランダムに1つ割り当てる。
    # 台帳(events.jsonl)に残すので、再開しても同じ才能が復元される。
    born = deps.reduce_state_fn()
    talent = born["talent"]
    npc_pool_cap = born["npc_pool_cap"]
    if talent is None:
        talent = args.talent or deps.choice_fn(cfg.traits)
        # 「人によって付き合う人数に差もあるよね」(ユーザー指摘、2026-08-13)。
        # talentと同じ「生まれつきの個人差を1回だけ抽選してイベントに固定する」
        # パターンで、社交的な人・そうでない人の差をNPC_POOL_CAP_RANGEから引く。
        npc_pool_cap = deps.randint_fn(*cfg.npc_pool_cap_range)
        deps.append_event_fn("character_born", {
            "talent": talent, "initial_traits": cfg.initial_traits,
            "start_age": cfg.start_age, "years_per_turn": cfg.years_per_turn,
            "npc_pool_cap": npc_pool_cap,
        })
        print(f"誕生: 生まれ持ったスキル = {cfg.trait_ja[talent]} "
              f"(該当する特性の成長が {1 + cfg.b_talent} 倍)")
        if args.endowment:
            # 初期値の定数をいじらず、イベントとして一度だけ積む(台帳だけで
            # 状態が再構築できる、というPhase 0からの性質を壊さないため)
            deps.append_event_fn("endowment_applied",
                                 {"delta": {"money": args.endowment}, "reason": "birth_endowment"})
            print(f"　　生まれの資産: money+{args.endowment}")
    else:
        print(f"(継続) 生まれ持ったスキル = {cfg.trait_ja[talent]}")
        if npc_pool_cap is None:
            # 後方互換(2026-08-14追加、6回目opusレビュー指摘・中9):
            # npc_pool_cap導入前のevents.jsonlを継続したケース。
            npc_pool_cap = deps.randint_fn(*cfg.npc_pool_cap_range)
            deps.append_event_fn("npc_pool_cap_backfilled", {"npc_pool_cap": npc_pool_cap})
            print(f"　　(後方互換) 生まれつきの社交性を補完: "
                  f"同時に付き合える人数の上限 = {npc_pool_cap}")
    print(f"初期特性: {deps.format_traits_fn(born['traits'], talent)}\n")

    # 2026-08-14追加(ユーザーからのレビュー指摘・P0の1): character_diedが
    # reduce_state()に一切反映されておらず、同じログで再開すると死亡済み
    # キャラクターの次ターンを開始できてしまう明確な統合ブロッカーだった。
    # 死亡済みなら続行そのものを拒否する(未清算契約はopenのまま・trust
    # ペナルティ無しという既存の扱いは変えない——ここで止めるだけ)。
    pre_start = deps.reduce_state_fn()
    if not pre_start["alive"]:
        print(f"● このキャラクターは既にターン{pre_start['death_turn']}"
              f"(年齢{deps.age_at_fn(pre_start['death_turn']):.0f})で死亡しています。"
              f"続けることはできません。")
        return
    start_turn = pre_start["turn"]
    for offset in range(1, args.turns + 1):
        turn = start_turn + offset
        deps.append_event_fn("turn_started", {"turn": turn})

        # --- 1. ターン開始時の自動処理。すべてコード側で決まり、LLMは関与しない -------
        #    (a) フロー資源の回復  (b) 定期収入  (c) 維持コスト(エントロピー)
        # decay は「このターン開始時点(regen/income適用前)の残高」を見る。
        # 2026-08-14時点でDECAY_RULESは空だが(moneyは銀行NPC+価格水準方式、
        # trustはプレイヤー個人trustの廃止によりどちらも対象外)、将来
        # 「残高に比例する減衰」を持つ資源が増えたときのためにこの順序
        # (simulate_money.py の検証モデルと同じ)を保っておく。
        pre_turn_state = deps.reduce_state_fn()
        resources_before = pre_turn_state["resources"]
        regen = deps.compute_regen_fn(resources_before, pre_turn_state["traits"])
        if regen:
            deps.append_event_fn("regen_applied", {"turn": turn, "delta": regen, "reason": "flow_recovery"})
        # 銀行trustの毎ターンの中間値への回帰(2026-08-13追加、5回目opusレビュー
        # 指摘への対応が発端。2026-08-14訂正: 当初は「ゼロへの一方通行の減衰」
        # だったが、実装フェーズ第1弾の実測で全方針が同じ値に収束することが
        # 判明したため、NPC trustと同じ「中間値への回帰」に変更した
        # 〈bank_trust_reversionのdocstring参照〉)。simulate_policyの検証で
        # 使ったのと同じ式をそのままイベント化する。
        # 2026-08-15(Step 6B、段階的モジュール分割): 回帰量の計算をturn_engine.
        # plan_confidence_reversion()へ委譲した(イベント発行の判定条件・
        # 積む値は変更していない。旧reversion_amt/currency_reversion_amtは
        # 符号反転しただけのbank_trust_delta/currency_confidence_deltaに
        # 置き換わる)。
        reversion = deps.plan_confidence_reversion_fn(
            pre_turn_state["npcs"][cfg.bank_npc_id]["trust"], pre_turn_state["currency_confidence"])
        if reversion["bank_trust_delta"]:
            deps.append_event_fn("npc_trust_changed", {
                "npc_id": cfg.bank_npc_id, "delta": reversion["bank_trust_delta"],
                "reason": "bank_trust_upkeep", "turn": turn,
            })
        # 2026-08-14追加(社会レジーム仕様、実装フェーズ第1弾)。通貨の信用の
        # 毎ターンの中間値への回帰(bank_trustと同じ発想だが独立した式・
        # 独立した変数。2026-08-14訂正: 一方通行の減衰から回帰に変更した
        # 経緯はbank_trust_reversionのコメント参照)。
        if reversion["currency_confidence_delta"]:
            deps.append_event_fn("currency_confidence_changed", {
                "turn": turn, "delta": reversion["currency_confidence_delta"],
                "reason": "currency_confidence_upkeep",
            })
        # 2026-08-15追加(地域信用制度)。community_trustもbank_trust/
        # currency_confidenceと同じ「中間値への回帰」を毎ターン適用する
        # (plan_local_credit_reversion()、turn_engine.py経由でinstitutions/
        # local_credit.pyのcommunity_trust_reversionへ委譲)。
        local_credit_reversion = deps.plan_local_credit_reversion_fn(pre_turn_state["community_trust"])
        if local_credit_reversion["community_trust_delta"]:
            deps.append_event_fn("community_trust_changed", {
                "turn": turn, "delta": local_credit_reversion["community_trust_delta"],
                "reason": "local_credit_trust_upkeep",
            })
        # 2026-08-15追加(契約執行制度)。enforcement_capacityもbank_trust/
        # currency_confidence/community_trustと同じ「中間値への回帰」を毎ターン
        # 適用する(plan_enforcement_reversion()、turn_engine.py経由で
        # institutions/contract_enforcement.pyのenforcement_capacity_reversion
        # へ委譲)。
        enforcement_reversion = deps.plan_enforcement_reversion_fn(
            pre_turn_state["enforcement_capacity"])
        if enforcement_reversion["enforcement_capacity_delta"]:
            deps.append_event_fn("enforcement_capacity_changed", {
                "turn": turn, "delta": enforcement_reversion["enforcement_capacity_delta"],
                "reason": "enforcement_capacity_upkeep",
            })
        # 銀行・通貨の状態機械を評価する(このターンの減衰・危機を反映した
        # 最新状態で判定するため、ここで一度reduce_state()を取り直す)。
        # 2026-08-15(Step 6B): 遷移判定そのものをturn_engine.
        # plan_institution_transitions()へ委譲した(bank_stage_next→
        # currency_stage_nextという呼び出し順序・判定材料は変更していない)。
        eval_state = deps.reduce_state_fn()
        transitions = deps.plan_institution_transitions_fn(
            eval_state["bank_stage"], eval_state["npcs"][cfg.bank_npc_id]["trust"],
            eval_state["bank_crisis_count"],
            eval_state["currency_stage"], eval_state["currency_confidence"])
        if transitions["bank_transitioned"]:
            deps.append_event_fn("institution_transition", {
                "turn": turn, "institution_id": "central_bank", "kind": "bank",
                "from_stage": eval_state["bank_stage"], "to_stage": transitions["bank_stage_after"],
                "trigger": "bank_trust/bank_crisis_countの閾値",
                "metric_snapshot": {"bank_trust": eval_state["npcs"][cfg.bank_npc_id]["trust"],
                                    "bank_crisis_count": eval_state["bank_crisis_count"]},
                "scope": "nation",
            })
            print(f"    ● 銀行の状態が変化: {cfg.bank_stage_names[eval_state['bank_stage']]}"
                  f" → {cfg.bank_stage_names[transitions['bank_stage_after']]}")
        if transitions["currency_transitioned"]:
            deps.append_event_fn("institution_transition", {
                "turn": turn, "institution_id": "central_currency", "kind": "currency",
                "from_stage": eval_state["currency_stage"], "to_stage": transitions["currency_stage_after"],
                "trigger": "currency_confidenceの閾値",
                "metric_snapshot": {"currency_confidence": eval_state["currency_confidence"]},
                "scope": "nation",
            })
            print(f"    ● 通貨の状態が変化: {cfg.currency_stage_names[eval_state['currency_stage']]}"
                  f" → {cfg.currency_stage_names[transitions['currency_stage_after']]}")
        # 2026-08-15追加(地域信用制度)。bank/currencyと並行してlocal_credit_
        # stageも毎ターン評価する(plan_local_credit_transition()、turn_engine.py
        # 経由でinstitutions/local_credit.pyのlocal_credit_stage_nextへ委譲)。
        local_credit_transition = deps.plan_local_credit_transition_fn(
            eval_state["local_credit_stage"], eval_state["community_trust"])
        if local_credit_transition["local_credit_transitioned"]:
            deps.append_event_fn("institution_transition", {
                "turn": turn, "institution_id": "local_credit", "kind": "local_credit",
                "from_stage": eval_state["local_credit_stage"],
                "to_stage": local_credit_transition["local_credit_stage_after"],
                "trigger": "community_trustの閾値",
                "metric_snapshot": {"community_trust": eval_state["community_trust"]},
                "scope": cfg.local_credit_scope,
            })
            print(f"    ● 地域信用の状態が変化: "
                  f"{cfg.local_credit_stage_names[eval_state['local_credit_stage']]}"
                  f" → {cfg.local_credit_stage_names[local_credit_transition['local_credit_stage_after']]}")
        # 2026-08-15追加(契約執行制度)。bank/currency/local_creditと並行して
        # enforcement_stageも毎ターン評価する(plan_enforcement_transition()、
        # turn_engine.py経由でinstitutions/contract_enforcement.pyの
        # enforcement_stage_nextへ委譲)。
        enforcement_transition = deps.plan_enforcement_transition_fn(
            eval_state["enforcement_stage"], eval_state["enforcement_capacity"])
        if enforcement_transition["enforcement_transitioned"]:
            deps.append_event_fn("institution_transition", {
                "turn": turn, "institution_id": "contract_enforcement", "kind": "contract_enforcement",
                "from_stage": eval_state["enforcement_stage"],
                "to_stage": enforcement_transition["enforcement_stage_after"],
                "trigger": "enforcement_capacityの閾値",
                "metric_snapshot": {"enforcement_capacity": eval_state["enforcement_capacity"]},
                "scope": cfg.enforcement_scope,
            })
            print(f"    ● 契約執行の状態が変化: "
                  f"{cfg.enforcement_stage_names[eval_state['enforcement_stage']]}"
                  f" → {cfg.enforcement_stage_names[enforcement_transition['enforcement_stage_after']]}")
        bank_stage = transitions["bank_stage_after"]
        currency_stage = transitions["currency_stage_after"]
        local_credit_stage = local_credit_transition["local_credit_stage_after"]
        enforcement_stage = enforcement_transition["enforcement_stage_after"]

        # Step 13.1: 財の消費・背景生産は平時から常時計算する。上位制度の
        # 縮退は、現在の代替行動の解禁とproduction_capacityへの分断圧力だけを
        # 決める。「一度でも発火した」という履歴はbarter_activatedイベントで
        # 別に記録し、財の存在・消費開始条件と混同しない。
        alternative_economy_available = deps.alternative_economy_triggered_fn(
            bank_stage, currency_stage, local_credit_stage, enforcement_stage)
        if alternative_economy_available and not eval_state["barter_active"]:
            deps.append_event_fn("barter_activated", {
                "turn": turn, "reason": "alternative_economy_triggered",
            })
        barter_upkeep = deps.plan_barter_upkeep_fn(
            eval_state["food"], eval_state["medicine"], eval_state["shelter"], eval_state["tools"],
            eval_state["production_capacity"], disrupted=alternative_economy_available)
        if any(barter_upkeep[key] != 0.0 for key in (
                "food_delta", "medicine_delta", "shelter_delta", "tools_delta",
                "production_capacity_delta")):
            deps.append_event_fn("goods_state_changed", {
                "turn": turn, "food_delta": barter_upkeep["food_delta"],
                "medicine_delta": barter_upkeep["medicine_delta"],
                "shelter_delta": barter_upkeep["shelter_delta"],
                "tools_delta": barter_upkeep["tools_delta"],
                "production_capacity_delta": barter_upkeep["production_capacity_delta"],
                "reason": "goods_economy_upkeep",
            })
        worst_score, worst_good = deps.worst_shortfall_fn(
            barter_upkeep["food_after"], barter_upkeep["medicine_after"],
            barter_upkeep["shelter_after"], barter_upkeep["tools_after"],
            barter_upkeep["production_capacity_after"])
        barter_transition = deps.plan_barter_transition_fn(eval_state["barter_stage"], worst_score)
        if barter_transition["barter_transitioned"]:
            deps.append_event_fn("institution_transition", {
                "turn": turn, "institution_id": "barter", "kind": "barter",
                "from_stage": eval_state["barter_stage"],
                "to_stage": barter_transition["barter_stage_after"],
                "trigger": cfg.barter_stage_trigger_names[barter_transition["barter_stage_after"]],
                "metric_snapshot": {"worst_shortfall": worst_score, "worst_good": worst_good,
                                    "food": barter_upkeep["food_after"],
                                    "medicine": barter_upkeep["medicine_after"],
                                    "shelter": barter_upkeep["shelter_after"],
                                    "tools": barter_upkeep["tools_after"],
                                    "production_capacity": barter_upkeep["production_capacity_after"]},
                "scope": cfg.barter_scope,
            })
            print(f"    ● 物々交換・自給の状態が変化: "
                  f"{cfg.barter_stage_names[eval_state['barter_stage']]}"
                  f" → {cfg.barter_stage_names[barter_transition['barter_stage_after']]}"
                  f"(ボトルネック={worst_good})")
        barter_stage = barter_transition["barter_stage_after"]
        shortage_penalty = deps.essential_goods_shortage_penalty_fn(barter_stage)
        if shortage_penalty:
            deps.append_event_fn("essential_goods_shortage_applied", {
                "turn": turn, "delta": {"energy": shortage_penalty["energy"]},
                "reason": "essential_goods_shortage",
            })
            deps.append_event_fn("trait_changed", {
                "turn": turn, "delta": {"health": shortage_penalty["health"]},
                "theme_trait": None, "choice_key": None, "fired": None,
                "kind": "essential_goods_shortage",
            })
            print(f"    ● 必需財不足の実害: energy{shortage_penalty['energy']:+.1f} "
                  f"/ health{shortage_penalty['health']:+.1f}")
            shortage_state = deps.reduce_state_fn()
            if shortage_state["traits"]["health"] <= 0:
                deps.append_event_fn("character_died", {
                    "turn": turn, "age": deps.age_at_fn(turn)})
                print(f"\n    ● 必需財不足で健康が尽きた(年齢{deps.age_at_fn(turn):.0f}、"
                      f"ターン{turn})。ここで人生が終わる。")
                break

        income = deps.compute_income_fn(turn, bank_stage)
        if income:
            deps.append_event_fn("income_applied", {"turn": turn, "delta": income, "reason": "periodic_income"})
            for k, v in income.items():
                stats["income_total"][k] = stats["income_total"].get(k, 0) + v
            # 銀行側の反対仕訳(docs/plan.md「経済の閉じ方 v1実装設計」)。
            # 賃金は無から生まれるのではなく、銀行の財布(下限なし)から実際に振り込まれる。
            deps.append_event_fn("npc_wallet_changed", {
                "npc_id": cfg.bank_npc_id, "delta": -income["money"],
                "reason": "wage_payment", "turn": turn,
            })
            # 賃金を「返済義務のある借入(債券)」として契約化する(2026-08-13、
            # opusレビュー「複式記帳が入口だけで出口が無い」への対応+ユーザー指摘
            # 「中央銀行からの支払いはあくまでも債券」)。既存のsocial型契約と同じ
            # 清算ターンの仕組みをそのまま再利用する(is_bank_debt=Trueで銀行債務と
            # 分かるようにし、money型で履行された場合だけ銀行の帳簿に戻す=貨幣の破壊)。
            wage_cid = f"c{len(deps.reduce_state_fn()['contracts']) + 1}"
            deps.append_event_fn("contract_created", {
                "id": wage_cid, "counterparty": cfg.bank_npc_name,
                "description": "銀行からの賃金前借り(返済義務あり)", "created_turn": turn,
                "due_turn": turn + deps.randint_fn(*cfg.contract_due_range),
                # repay_moneyは「清算時にプレイヤーの資源へ直接足すdelta」という
                # 既存の契約の意味論(social型はCONTRACT_REPAY_MONEYで負値)に合わせ、
                # 負値で持つ(正の賃金額をそのまま入れると符号が逆転し、返済するほど
                # お金が増えるバグになる)。
                "repay_money": -income["money"], "origin_choice": "賃金支払い(銀行)",
                "is_bank_debt": True,
            })
        # 2026-08-14追加(P0-3「所得と融資を分離する」)。前借り(上記、返済義務
        # あり)とは別に、返済義務の無い本当の所得(salary)を同じタイミングで
        # 支払う。契約を作らない(=返済請求権を持たない)ことで、恒久的な通貨
        # 発行として前借りと明確に区別する。
        # 2026-08-14訂正(外部レビュー指摘・社会レジーム仕様との整合): 発行主体を
        # BANK_NPC_IDからECONOMY_NPC_ID(地域経済)へ切り離した——「銀行が壊れても
        # 労働による所得は残る」という設計意図に対し、銀行の財布を直接減らす
        # 実装は矛盾していた。
        salary = deps.compute_salary_fn(turn)
        if salary:
            deps.append_event_fn("income_applied", {"turn": turn, "delta": {"money": salary},
                                                     "reason": "labor_salary"})
            stats["income_total"]["money"] = stats["income_total"].get("money", 0) + salary
            deps.append_event_fn("npc_wallet_changed", {
                "npc_id": cfg.economy_npc_id, "delta": -salary,
                "reason": "salary_issuance", "turn": turn,
            })
        decay = deps.compute_decay_fn(turn, resources_before)
        if decay:
            deps.append_event_fn("decay_applied", {"turn": turn, "delta": decay,
                                                    "reason": "stock_maintenance"})
            for k, v in decay.items():
                stats["decay_total"][k] = stats["decay_total"].get(k, 0) + v

        state = deps.reduce_state_fn()
        decay_str = ", ".join(f"{k}{v:+d}" for k, v in decay.items()) or "なし"
        regen_str = ", ".join(f"{k}{v:+d}" for k, v in regen.items()) or "なし"
        # 2026-08-14追加(P0-3): 前借り(要返済)とsalary(要返済なし)を分けて表示する。
        income_str = (", ".join(f"{k}{v:+d}" for k, v in income.items())
                      if income else "なし")
        salary_str = f"money+{salary}" if salary else "なし"
        print(f"--- ターン{turn}(年齢{deps.age_at_fn(turn):.0f}) --- "
              f"資源: {deps.format_resources_fn(state['resources'])}")
        print(f"    回復: {regen_str}  /  前借り(要返済): {income_str}  /  "
              f"所得(返済不要): {salary_str}  /  維持コスト: {decay_str}")
        print(f"    未清算の借り: {deps.format_contracts_fn(state)}")

        # --- 2. 期限の来た契約があれば、強制的に清算ターンにする -------------------
        # 2026-08-13訂正(4回目opusレビュー提案): 清算ターンにも成長の機会を与える
        # (compute_trait_stepの訂正と対になる変更)。ナレーション・プロンプトには
        # 一切出さない、完全に裏側だけの抽選(LLMは題材の存在すら知らない)。
        due = deps.due_contracts_fn(state, turn)

        def process_one_situation(tr, theme_trait, cur_state, budget, do_growth):
            """interactive_runtime.pyの実装をそのまま呼ぶ(再実装しない)。
            turn・decayはこのターンのループ内で変わるため、args・stats・
            situations_this_run・latenciesと合わせてクロージャ経由で渡す
            (main()にあった元のネスト構造をそのまま踏襲)。"""
            return interactive_runtime.process_one_situation(
                tr, theme_trait, cur_state, budget, do_growth,
                turn=turn, args=args, stats=stats, situations_this_run=situations_this_run,
                latencies=latencies, decay=decay,
                dependencies=deps.interactive_runtime_dependencies_fn())

        if due:
            # 2026-08-14訂正(7回目opusレビュー指摘・重大5): 従来はdue[0]だけを
            # 処理して次のターンに回していたが、社会契約が月2.9件ペースで
            # 発生するのに対し清算は月1件しか進まないため恒常的に渋滞し、
            # 実測で1920ターン中80.7%が清算ターンに消費されていた——「月内の
            # 時間配分でストック資源の配分を変える」という時間予算制の主題
            # そのものが、8割のターンで発動していなかった。同じターンの中で
            # 期限到来分をまとめて片付ける(1ターン=1ヶ月の間に複数の用事を
            # 片付けるのは自然、という解釈)。
            # 成長判定(compute_trait_step)は、通常ターンの時間予算ループと同じ
            # 理由(D_BASE/health_decayの月内複数回適用を防ぐ)で、個々の契約
            # ごとにはdo_growth=Falseで抑え、月内でG_BASE対象の選択(money/labor)
            # が最初に出た回を使って月末に1回だけまとめて適用する。
            stats["settlement_turns"] += 1  # 「清算が1件以上あったターン」の回数
            growth_choice_key = None
            growth_theme_trait = None
            quit_requested = False
            for contract in due:
                state_now = deps.reduce_state_fn()
                c_now = state_now["contracts"].get(contract["id"])
                if not c_now or c_now["status"] != "open":
                    continue  # 同じターン内の先行する清算で既に片付いた
                stats["settled_contracts"] += 1  # 「清算した契約」の件数(1ターンで複数ありうる)
                print(f"    [期限到来] {c_now['id']} {c_now['counterparty']}"
                      f"「{c_now['description']}」(期限T{c_now['due_turn']})")
                try:
                    tr = deps.generate_settlement_turn_fn(args.model, state_now, turn, c_now, latencies)
                except Exception as e:
                    print(f"  [解析エラー] {e}")
                    deps.append_event_fn("parse_error", {"turn": turn, "error": str(e)})
                    parse_failures += 1
                    continue
                if args.theme_injection == "on":
                    _, settle_concern = deps.pick_theme_fn(used_places, used_concerns)
                    theme_trait = cfg.theme_concern_traits.get(settle_concern)
                else:
                    theme_trait = None
                result = process_one_situation(tr, theme_trait, state_now, None, False)
                if result is None:
                    quit_requested = True
                    break  # 手動プレイで q が入力された
                if growth_choice_key is None and result["key"] in cfg.g_base:
                    growth_choice_key = result["key"]
                    growth_theme_trait = theme_trait
            if quit_requested:
                break
            trait_delta, fired = deps.compute_trait_step_fn(
                deps.reduce_state_fn()["traits"], talent, growth_theme_trait,
                growth_choice_key or "avoid", turn, "settlement")
            if trait_delta:
                deps.append_event_fn("trait_changed", {
                    "turn": turn, "delta": trait_delta,
                    "theme_trait": growth_theme_trait,
                    "choice_key": growth_choice_key or "avoid",
                    "fired": fired, "kind": "settlement",
                })
            stats["played_turns"] += 1
            if growth_theme_trait:
                stats["theme_turns"][growth_theme_trait] = \
                    stats["theme_turns"].get(growth_theme_trait, 0) + 1
            else:
                stats["no_theme_turns"] += 1
            if fired:
                key = f"{fired['trait']}/{fired['via']}"
                stats["fire_counts"][key] = stats["fire_counts"].get(key, 0) + 1
                stats["fires"] += 1
            settle_end_state = deps.reduce_state_fn()
            fire_str = (f"成長発火: {cfg.trait_ja[fired['trait']]} via {fired['via']}"
                       + ("(才能補正あり)" if fired["talent"] else "")) if fired else "成長なし"
            print(f"    [月間成長判定] 特性: {deps.format_traits_fn(settle_end_state['traits'], talent)}"
                  f"  [{fire_str}]")
            # 2026-08-14追加(ユーザー指示「体力、健康が0の状態は死と定義したい」)。
            # health<=0での即死。その場でこのキャラのシミュレーションを終了する。
            if settle_end_state["traits"]["health"] <= 0:
                deps.append_event_fn("character_died", {"turn": turn, "age": deps.age_at_fn(turn)})
                print(f"\n    ● 健康が尽きた(年齢{deps.age_at_fn(turn):.0f}、ターン{turn})。"
                      f"ここで人生が終わる。")
                break
        else:
            # --- 時間予算制(2026-08-14追加、docs/plan.md「時間予算制」節) ----------
            # 月内の時間予算(TURN_TIME_BUDGET)が尽きるまで、今までの「1つ選ぶ」を
            # 繰り返す。成長判定+老化由来の減衰(compute_trait_step)は月に1回だけ、
            # 月末にまとめて呼ぶ——月内で最初にG_BASE対象の行動が選ばれた回を使う
            # (2026-08-14訂正: 「月内最初の行動」で決め打ちすると、それがG_BASE
            # 対象外の「休息」だった月の成長機会がまるごと潰れる不具合があった)。
            # 全部休息だった月も、老化由来の減衰だけは必ず1回適用する。
            budget = cfg.turn_time_budget
            growth_choice_key = None
            growth_theme_trait = None
            quit_requested = False
            while budget >= cfg.min_activity_hours:
                cur_state = deps.reduce_state_fn()
                if args.theme_injection == "on":
                    theme, concern = deps.pick_theme_fn(used_places, used_concerns)
                    theme_trait = cfg.theme_concern_traits.get(concern)
                else:
                    theme, theme_trait = "", None
                try:
                    tr = deps.generate_normal_turn_fn(args.model, cur_state, turn, theme, latencies)
                except Exception as e:
                    print(f"  [解析エラー] {e}")
                    deps.append_event_fn("parse_error", {"turn": turn, "error": str(e)})
                    parse_failures += 1
                    break  # この月の残り時間はそのまま切り上げる(既に使った分は残る)
                # 2026-08-14訂正(6回目opusレビュー指摘・高4、probeと同じ修正)。
                # その回に抽選されたhoursが偶然どれも残り予算を超えることがある。
                # 打ち切って残り時間は月末の自動休息に回す(probeと挙動を揃える)。
                if not any(deps.is_affordable_fn(c, cur_state["resources"], budget)
                          for c in tr["choices"]):
                    break
                result = process_one_situation(tr, theme_trait, cur_state, budget, False)
                if result is None:
                    quit_requested = True
                    break
                if growth_choice_key is None and result["key"] in cfg.g_base:
                    growth_choice_key = result["key"]
                    growth_theme_trait = theme_trait
                # 2026-08-15追加(Step 13、物々交換・自給制度)。barter/subsistence
                # が選ばれた回だけ、財への効果を追加で積む(既存の資源コスト
                # 〈energy/peace〉の適用はprocess_one_situation側で他のarchetype
                # と同じ経路で既に完了済み——ここは財という「別帳簿」への効果のみ)。
                if result["key"] in ("barter", "subsistence"):
                    barter_effects = result.get("goods_effects") or deps.barter_choice_effects_fn(
                        result["key"], deps.reduce_state_fn()["production_capacity"])
                    deps.append_event_fn("goods_state_changed", {
                        "turn": turn, "food_delta": barter_effects["food_delta"],
                        "medicine_delta": barter_effects["medicine_delta"],
                        "shelter_delta": barter_effects["shelter_delta"],
                        "tools_delta": barter_effects["tools_delta"],
                        "production_capacity_delta": barter_effects["production_capacity_delta"],
                        "reason": f"{result['key']}_action",
                    })
                budget -= result.get("hours", 0)
            if quit_requested:
                break

            # 月に1回だけ、成長判定+老化由来の減衰を適用する。
            trait_delta, fired = deps.compute_trait_step_fn(
                deps.reduce_state_fn()["traits"], talent, growth_theme_trait,
                growth_choice_key or "rest", turn, "normal")
            if trait_delta:
                deps.append_event_fn("trait_changed", {
                    "turn": turn, "delta": trait_delta,
                    "theme_trait": growth_theme_trait,
                    "choice_key": growth_choice_key or "rest",
                    "fired": fired, "kind": "normal",
                })
            stats["played_turns"] += 1
            if growth_theme_trait:
                stats["theme_turns"][growth_theme_trait] = \
                    stats["theme_turns"].get(growth_theme_trait, 0) + 1
            else:
                stats["no_theme_turns"] += 1
            if fired:
                key = f"{fired['trait']}/{fired['via']}"
                stats["fire_counts"][key] = stats["fire_counts"].get(key, 0) + 1
                stats["fires"] += 1
            month_end_state = deps.reduce_state_fn()
            fire_str = (f"成長発火: {cfg.trait_ja[fired['trait']]} via {fired['via']}"
                       + ("(才能補正あり)" if fired["talent"] else "")) if fired else "成長なし"
            print(f"    [月間成長判定] 特性: {deps.format_traits_fn(month_end_state['traits'], talent)}"
                  f"  [{fire_str}]")
            # 2026-08-14追加(ユーザー指示「体力、健康が0の状態は死と定義したい」)。
            # health<=0での即死。その場でこのキャラのシミュレーションを終了する
            # (残り時間の自動休息も行わない)。
            if month_end_state["traits"]["health"] <= 0:
                deps.append_event_fn("character_died", {"turn": turn, "age": deps.age_at_fn(turn)})
                print(f"\n    ● 健康が尽きた(年齢{deps.age_at_fn(turn):.0f}、ターン{turn})。"
                      f"ここで人生が終わる。")
                break

            if budget > 0:
                # 端数は自動的に休息に充当する(plan.md記載の方針。コード側のみで
                # 完結させ、LLMは呼ばない=構造化出力を壊れやすくしない)。
                # 2026-08-14訂正(高5): 残り時間に按分する(draw_prorated_rest)。
                rest_state = deps.reduce_state_fn()
                rest_delta = deps.clamp_gain_fn(deps.draw_prorated_rest_fn(turn, budget),
                                                rest_state["resources"], rest_state["traits"])
                if rest_delta:
                    deps.append_event_fn("state_applied", {
                        "turn": turn, "delta": rest_delta,
                        "choice_key": "rest", "choice_label": "残りの時間を静かに過ごした",
                    })
                print(f"    (残り{budget}時間は自動的に休息に充当: "
                      + (", ".join(f"{k}{v:+d}" for k, v in rest_delta.items()) or "変化なし")
                      + ")")

    # --- サマリ ---------------------------------------------------------------
    final = deps.reduce_state_fn()
    print("=== 計測サマリ ===")
    if latencies:
        print(f"LLM呼び出し回数: {len(latencies)} / 平均 {sum(latencies)/len(latencies):.1f}秒 "
              f"/ 最大 {max(latencies):.1f}秒 / 最小 {min(latencies):.1f}秒")
    print(f"JSON解析失敗: {parse_failures}件")
    print(f"最終資源: {deps.format_resources_fn(final['resources'])}")
    _idx = deps.price_index_fn(final["turn"])
    print(f"価格水準(price_index): {_idx:.3f}  "
          f"/ moneyの実質価値: {final['resources'].get('money', 0) / _idx:.2f}"
          + (f"  / 銀行(中央銀行)の帳簿: {final['npcs'][cfg.bank_npc_id]['money']:+d}"
             if cfg.bank_npc_id in final["npcs"] else "")
          + f"  / 信用損失(直近の危機以降): {final['bank_credit_losses']:.1f}"
            f" / 通貨危機: {final['bank_crisis_count']}回")
    print(f"維持コスト累計: "
          + (", ".join(f"{k}{v:+d}" for k, v in stats['decay_total'].items()) or "なし")
          + " / 収入累計: "
          + (", ".join(f"{k}{v:+d}" for k, v in stats['income_total'].items()) or "なし"))
    print(f"期間中の最小値: {deps.format_resources_fn(stats['min_seen'])}"
          + (" ← 破産(マイナス)あり"
             if any(v < 0 for v in stats["min_seen"].values()) else " (破産なし)"))
    print(f"選択の内訳: "
          + (", ".join(f"{k}×{v}" for k, v in sorted(stats["choice_counts"].items()))
             or "なし"))
    print(f"資源不足で選べなかった選択肢: {stats['blocked_choices']}件")
    print(f"整合性の矛盾検出: コード側 {stats['code_check_hits']}件 / "
          f"LLM側 {stats['llm_check_hits']}件")
    print(f"契約: 発生{stats['contracts_created']}件 / 清算ターン{stats['settlement_turns']}回"
          f"(清算した契約{stats['settled_contracts']}件、1ターンで複数ありうる) "
          f"/ 履行{stats['fulfilled']}件 / 不履行{stats['defaulted']}件 "
          f"/ 未清算{len(deps.open_contracts_fn(final))}件")
    for c in final["contracts"].values():
        print(f"  - {c['id']} {c['counterparty']}「{c['description']}」"
              f"T{c['created_turn']}→期限T{c['due_turn']} : {c['status']}")
    # --- 特性の実測サマリ ------------------------------------------------------
    print(f"--- 特性(生まれ持ったスキル: {cfg.trait_ja[talent]}、"
          f"年齢 {deps.age_at_fn(final['turn']):.0f}) ---")
    print(f"  {deps.format_traits_fn(final['traits'], talent)}")
    played = stats["played_turns"]
    if played:
        base_counts = {}
        for k, v in stats["choice_counts"].items():
            base_key = k.split(":", 1)[-1]
            base_counts[base_key] = base_counts.get(base_key, 0) + v
        print(f"  選択の内訳: "
              + ", ".join(f"{k}={v}({v/played:.0%})" for k, v in sorted(base_counts.items())))
        print(f"  題材のあったターン: {played - stats['no_theme_turns']}/{played} "
              f"(題材なし={stats['no_theme_turns']}: 清算ターン等)")
        print(f"  題材の特性別内訳: "
              + (", ".join(f"{cfg.trait_ja[t]}={n}" for t, n in stats["theme_turns"].items())
                 or "なし"))
        print(f"  成長の発火: {stats['fires']}回 / {played}ターン "
              f"= **実測 f_total = {stats['fires']/played:.3f}**"
              f"(机上の仮定 4×0.167=0.667 に対応)")
        print(f"    1特性あたり f = {stats['fires']/played/len(cfg.traits):.3f} "
              f"(机上の仮定 0.167)")
        print(f"  発火の内訳: "
              + (", ".join(f"{k}={v}" for k, v in sorted(stats["fire_counts"].items()))
                 or "なし"))
    rep = deps.repetition_report_fn(situations_this_run)
    print(f"状況の繰り返し度(文字bigram Jaccard, {rep['pairs']}ペア): "
          f"最大 {rep['max']:.2f} / 平均 {rep['mean']:.2f} / 0.5超のペア {rep['near_duplicates']}件")
    print(f"詳細ログ: {cfg.events_path}")
