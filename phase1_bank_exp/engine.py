# -*- coding: utf-8 -*-
"""清算(契約の履行・不履行)・通常行動まわりの数値ルールを置くエンジン層。

段階的モジュール分割で game.py から切り出した:
- Step 4A(2026-08-14): Step 3A(build_settlement_choices)・Step 3C
  (plan_settlement_effects)で main() と simulate_policy() の重複が解消済みの
  2関数を移動。
- Step 4B(2026-08-14): Step 3B(available_normal_archetypes・
  compute_normal_money_modifier・build_normal_base_choice・
  compute_social_contract_repay_range)で同じく重複が解消済みの4関数を移動。
どちらも計算式・分岐順序・戻り値のキー構成・キー順序は一切変更していない
(移動元のコメント・docstringもそのまま引き継ぐ)。

このモジュールは game.py・event_store.py・projection.py のいずれにも
依存しない。institutions.bank・institutions.currency・institutions.local_credit
の純粋関数・定数は直接importしてよい(それ自体が game.py に依存しない
下位レイヤーのため)。
game.py 側の動的な依存は SettlementEngineDependencies・
NormalActionEngineDependencies 経由で呼び出し側から明示的に受け取る——
CREATION_RATE の CLI 上書きや、既存テストの monkeypatch(game.draw_ranges の
差し替え等)が引き続き効くようにするため、import 時点でこれらの関数
オブジェクトを固定しない(game.py 側の互換ラッパーが呼び出しごとに依存
オブジェクトを作り直す)。

ファイルI/O・print・LLM呼び出し・argparse・simulationやmain()のループは
一切持たない(それらは呼び出し側=game.pyの責務)。randomモジュールへの
暗黙参照も持たない(compute_normal_money_modifier()はrngを必須引数として
受け取る、デフォルト値を置かない)。
"""
from dataclasses import dataclass
from typing import Callable

from institutions.bank import bank_trust_gain, crisis_threshold
from institutions.currency import (
    CURRENCY_STAGE_NORMAL,
    CURRENCY_STAGE_ABANDONED,
    CURRENCY_STAGE_WARY,
    CURRENCY_STAGE1_PRICE_PENALTY,
    CURRENCY_CRISIS_HIT,
    currency_confidence_gain,
)
from institutions.local_credit import (
    LOCAL_CREDIT_PROPAGATION_SCALE,
    LOCAL_CREDIT_STAGE_HEALTHY,
    LOCAL_CREDIT_STAGE_PERSONAL,
    LOCAL_CREDIT_STAGE_ISOLATED,
)
from institutions.contract_enforcement import (
    ENFORCEMENT_STAGE_INSTITUTIONAL,
    ENFORCEMENT_STAGE_LOCAL_LEDGER,
    ENFORCEMENT_CAPACITY_INITIAL,
    ENFORCEMENT_DEFAULT_PENALTY,
    enforcement_capacity_gain,
    contract_enforcement_penalty_multiplier,
    contract_enforcement_trust_amplifier,
)
from institutions.bank import BANK_STAGE_HEALTHY, BANK_STAGE_COLLAPSED
from institutions.barter import (
    BARTER_STAGE_FUNCTIONING,
    BARTER_STAGE_THINNED,
    BARTER_ARCHETYPE,
    SUBSISTENCE_ARCHETYPE,
)


@dataclass(frozen=True)
class SettlementEngineDependencies:
    """build_settlement_choices()・plan_settlement_effects()が game.py 側の
    現在値を参照するための、明示的な依存の受け渡し容器。WorldStateではない
    (制度スキーマの再設計ではなく、清算エンジンをgame.pyから独立させるための
    最小限の依存注入)。"""
    draw_contract_repay_labor_fn: Callable[[int, int], dict]
    contract_default_penalty_fn: Callable[[int, int], dict]
    clean_label_fn: Callable[[str, str], str]
    price_index_fn: Callable[[int], float]
    npc_trust_gain_fn: Callable[[float], float]
    npc_trust_initial: float
    crisis_rebase_factor: int


# 2026-08-14(Step 3A、段階的な数値ルール共通化): 契約清算時のmoney/labor/avoid
# 選択肢生成が generate_settlement_turn()(main()側、LLMがラベル文を書く)と
# simulate_policy()(モンテカルロ側、ラベル不要)の2箇所にほぼ同一の形で
# 重複していた。ここで1つの関数に統合する。
def build_settlement_choices(contract: dict, turn: int, currency_stage: int,
                             labels: dict = None, *,
                             enforcement_stage: int = ENFORCEMENT_STAGE_INSTITUTIONAL,
                             dependencies: SettlementEngineDependencies) -> list:
    """契約清算時の選択肢(money/labor/avoid)を生成する。履行/不履行の判定も、
    それに伴う資源変化(コスト)も、すべてここで決まる——2026-08-14、プレイヤー
    個人の汎用trustを削除したのに伴い、履行時の信用ボーナス・不履行時の信用
    ペナルティ(いずれもプレイヤー側trustへの一律の増減だった)は無い。相手
    ごとの信用の増減は呼び出し側の外(npc_trust_gain/bank_trust_gain、main()の
    「4. 適用」節)で別立てで処理する(二重計上を避けるため、ここではやらない)。

    labels=None(simulate_policy向け)なら {"key", "cost", "settle"}、
    labelsを渡す(generate_settlement_turn向け、LLMが書いたラベル文の辞書)なら
    {"key", "label", "cost", "settle"} のキー構成・順序になる。
    currency_stageがCURRENCY_STAGE_ABANDONED(通貨放棄)のときは、誰も受け取らない
    通貨での返済を選択肢から外す(残るのはlabor/avoidのみ)。

    2026-08-15追加(契約執行制度): enforcement_stageに応じて"avoid"選択肢の
    第三者ペナルティ(peace)へcontract_enforcement_penalty_multiplierを掛ける
    ——Stageが進むほど第三者(コード側)による処罰の実効性が下がる、という
    行動的帰結。enforcement_stage省略時はENFORCEMENT_STAGE_INSTITUTIONAL
    (乗率1.0=現状と同一の計算結果)なので既存呼び出しは無改造で動く。
    labels引数より後ろ・キーワード専用にしたのは、既存テストが
    `build_settlement_choices(contract, turn, currency_stage, labels)`と
    位置引数でlabelsを渡しているため(新引数を割り込ませると壊れる)。"""
    money_cost = {"money": contract["repay_money"]}
    labor_cost = dependencies.draw_contract_repay_labor_fn(contract["repay_money"], turn)
    default_cost = dependencies.contract_default_penalty_fn(contract["repay_money"], turn)
    multiplier = contract_enforcement_penalty_multiplier(enforcement_stage)
    if multiplier != 1.0:
        default_cost = {k: round(v * multiplier) for k, v in default_cost.items()}

    if labels is None:
        choices = [
            {"key": "money", "cost": money_cost, "settle": "fulfilled"},
            {"key": "labor", "cost": labor_cost, "settle": "fulfilled"},
            {"key": "avoid", "cost": default_cost, "settle": "defaulted"},
        ]
    else:
        choices = [
            {"key": "money",
             "label": dependencies.clean_label_fn(
                 labels.get("money"), f"{contract['counterparty']}にお金で返す"),
             "cost": money_cost, "settle": "fulfilled"},
            {"key": "labor",
             "label": dependencies.clean_label_fn(
                 labels.get("labor"), f"{contract['counterparty']}を手伝って返す"),
             "cost": labor_cost, "settle": "fulfilled"},
            {"key": "avoid",
             "label": dependencies.clean_label_fn(labels.get("avoid"), "今回は返さずに先延ばしにする"),
             "cost": default_cost, "settle": "defaulted"},
        ]

    if currency_stage == CURRENCY_STAGE_ABANDONED:
        choices = [c for c in choices if c["key"] != "money"]
    return choices


# 2026-08-14(Step 3C-1、段階的な数値ルール共通化): 契約の履行・不履行によって
# 発生する数値効果(銀行trust・銀行wallet・通貨confidence・相手NPC trust・
# 通貨危機の判定と影響)が、main()のイベント生成経路とsimulate_policy()の
# 直接更新経路の両方にほぼ同一の形で重複していた。ここで1つの純粋関数に
# 集約する。イベントの保存方法(main()はイベントを積む、simulate_policyは
# ローカル変数を直接更新する)そのものは統一しない——共有するのは
# 「清算によって発生する意味上の効果の計算」だけ。
def plan_settlement_effects(contract: dict, choice_key: str, outcome: str, turn: int,
                            bank_trust: float, currency_confidence: float,
                            bank_credit_losses: float, counterparty_trust: float = None,
                            *, dependencies: SettlementEngineDependencies,
                            enforcement_stage: int = ENFORCEMENT_STAGE_INSTITUTIONAL,
                            enforcement_capacity: float = ENFORCEMENT_CAPACITY_INITIAL) -> dict:
    """契約の清算(履行/不履行)によって生じる数値効果を計算する。イベント生成・
    状態更新は一切行わない純粋関数——引数のdict(contract)は変更せず、乱数・
    ファイルI/O・print・LLM呼び出しも行わない。戻り値のキー構成は分岐に
    よらず常に固定(該当しない項目は0.0/None/Falseで埋める)。

    呼び出し側(main()はイベント追加、simulate_policy()はローカル変数の直接
    更新)は、この戻り値を使って以下を組み立てる:
    - 銀行債務・履行(money/labor問わず): bank_trust_gain分の信用回復
      (bank_trust_delta)。money型のみ銀行walletへの返済(bank_wallet_delta)と
      currency_confidence_gain分の通貨信用回復(currency_confidence_delta)。
    - 銀行債務・不履行: real_loss(実質損失)ぶんbank_credit_lossesに加算
      (bank_credit_losses_after、危機によるリセット前の累積値)。銀行trustも
      real_loss×0.5だけ毀損する(下限0でクランプ、bank_trust_delta)。
      その後のbank_trustを使ったcrisis_threshold・crisis_triggered判定。
      危機発生時は銀行trustがさらに半減し(crisis_bank_trust_delta)、通貨の
      信用もCURRENCY_CRISIS_HITぶん低下する(crisis_currency_confidence_delta、
      0クランプ済み)。rebase_factorはCRISIS_REBASE_FACTOR固定。
    - 社会契約・履行/不履行: 相手NPCのtrustがnpc_trust_gain分回復するか、
      -8.0される(counterparty_trust_delta)。銀行関連の項目はすべて0.0/None。
      あわせて地域全体の信用(community_trust)にも、counterparty_trust_delta
      にLOCAL_CREDIT_PROPAGATION_SCALEを掛けた分だけ波及する
      (community_trust_delta、2026-08-15追加、「NPC間評判伝播」の片側——
      1人との取引の結果が村全体の信用にも少しずつ影響する)。銀行債務では
      波及しない(community_trust_deltaは常に0.0のまま)。
    - 契約執行能力(enforcement_capacity): 銀行債務・社会契約どちらの履行/
      不履行でも動く(enforcement_capacity_delta、2026-08-15追加。「第三者
      執行という同じ仕組みが試される」という解釈——履行のたびに
      enforcement_capacity_gain分回復、不履行のたびにENFORCEMENT_DEFAULT_
      PENALTY分減衰)。また社会契約の分岐では、enforcement_stageが進むほど
      (Stage2「地域台帳への移行」以上)counterparty_trust_delta・
      community_trust_deltaにcontract_enforcement_trust_amplifierを掛けて
      増幅する——第三者執行が弱まるほど非公式な制裁(個人間の信頼毀損・地域
      評判)が相対的に強まる、という接続(地域信用が契約執行の代替執行層に
      なる、という仕様の記述の実装)。
    """
    result = {
        "bank_trust_delta": 0.0,
        "bank_wallet_delta": 0.0,
        "currency_confidence_delta": 0.0,
        "counterparty_trust_delta": None,
        "community_trust_delta": 0.0,
        "enforcement_capacity_delta": 0.0,
        "real_loss": 0.0,
        "bank_credit_losses_after": bank_credit_losses,
        "crisis_threshold": None,
        "crisis_triggered": False,
        "crisis_bank_trust_delta": None,
        "crisis_currency_confidence_delta": None,
        "rebase_factor": None,
    }

    if contract.get("is_bank_debt"):
        if outcome == "fulfilled":
            # 履行(money・labor問わず)は銀行のtrustを少し回復させる
            # (BANK_TRUST_CAPに近づくほど逓減)。
            result["bank_trust_delta"] = bank_trust_gain(bank_trust)
            result["enforcement_capacity_delta"] = enforcement_capacity_gain(enforcement_capacity)
            if choice_key == "money":
                # money型で履行された場合だけ、銀行の帳簿にお金が戻る
                # (=貨幣の破壊)。labor型は銀行walletを動かさない。
                result["bank_wallet_delta"] = -contract["repay_money"]
                # money型で実際に履行されるたびに通貨の信用も回復する
                # (bank_trustとは独立の変数)。
                result["currency_confidence_delta"] = currency_confidence_gain(currency_confidence)
        elif outcome == "defaulted":
            real_loss = abs(contract["repay_money"]) / dependencies.price_index_fn(turn)
            result["real_loss"] = real_loss
            result["enforcement_capacity_delta"] = ENFORCEMENT_DEFAULT_PENALTY
            # 銀行自身のtrustも毀損する(下限0でクランプ)。
            result["bank_trust_delta"] = -min(bank_trust, real_loss * 0.5)
            bank_trust_after_loss = bank_trust + result["bank_trust_delta"]
            result["bank_credit_losses_after"] = bank_credit_losses + real_loss
            result["crisis_threshold"] = crisis_threshold(bank_trust_after_loss)
            if result["bank_credit_losses_after"] >= result["crisis_threshold"]:
                result["crisis_triggered"] = True
                # 危機そのものが銀行の信用を大きく損なう(半減)。
                result["crisis_bank_trust_delta"] = -bank_trust_after_loss * 0.5
                # 通貨危機は通貨の信用にも独立にダメージを与える(0クランプ済み
                # ——projection.pyのbank_crisisイベント解釈と同じ式)。
                result["crisis_currency_confidence_delta"] = (
                    max(0.0, currency_confidence - CURRENCY_CRISIS_HIT) - currency_confidence)
                result["rebase_factor"] = dependencies.crisis_rebase_factor
    else:
        # 社会契約(顔なじみNPCとの契約)。顔なじみNPC自身のtrustが履行/不履行で
        # 動く(履行はNPC_TRUST_CAPに近づくほど逓減、不履行は固定-8.0)。
        if outcome == "fulfilled":
            base_trust = (counterparty_trust if counterparty_trust is not None
                          else dependencies.npc_trust_initial)
            result["counterparty_trust_delta"] = dependencies.npc_trust_gain_fn(base_trust)
            result["enforcement_capacity_delta"] = enforcement_capacity_gain(enforcement_capacity)
        elif outcome == "defaulted":
            result["counterparty_trust_delta"] = -8.0
            result["enforcement_capacity_delta"] = ENFORCEMENT_DEFAULT_PENALTY
        # 2026-08-15追加(契約執行制度): enforcement_stageが進むほど非公式な
        # 制裁(個人間の信頼毀損・地域評判)が相対的に強まる(上記docstring参照)。
        amplifier = contract_enforcement_trust_amplifier(enforcement_stage)
        result["counterparty_trust_delta"] = result["counterparty_trust_delta"] * amplifier
        result["community_trust_delta"] = (
            result["counterparty_trust_delta"] * LOCAL_CREDIT_PROPAGATION_SCALE)

    return result


# 2026-08-15(Step 6C、段階的な数値ルール共通化): main()・simulate_policy()の
# 清算ループには「選択済みchoiceからplan_settlement_effects()を呼ぶ」という
# 定型の接着コードがそれぞれ独立に書かれていた(main()はcur_state経由、
# simulate_policyはローカル変数経由でbank_trust等を渡す点だけが異なり、
# plan_settlement_effects()の呼び出し自体は同じ形)。この関数はその接着部分
# だけをまとめる——選択肢生成(build_settlement_choices)・選択index決定
# (main()は手動/自動選択、simulate_policyは複数方針の集計という別の責務を
# 持つため、意図的に共通化しない)・イベント発行・resources/npcs等のローカル
# 変数更新・contract["status"]の更新は、いずれも呼び出し側の責務のまま残す。
def plan_settlement_resolution(contract: dict, choice: dict, turn: int,
                               bank_trust: float, currency_confidence: float,
                               bank_credit_losses: float, counterparty_trust: float = None,
                               *, dependencies: SettlementEngineDependencies,
                               plan_settlement_effects_fn: Callable = None,
                               enforcement_stage: int = ENFORCEMENT_STAGE_INSTITUTIONAL,
                               enforcement_capacity: float = ENFORCEMENT_CAPACITY_INITIAL) -> dict:
    """清算1件分の「選択済みchoice→数値効果」の接着。choice(build_settlement_
    choices()が返す辞書のうち、既に選ばれた1件——{"key","cost","settle"}を
    最低限持つ)から choice["key"]・choice["settle"] を取り出して
    plan_settlement_effects()を正確に1回だけ呼ぶ。それ以外の計算・分岐は
    一切持たない(戻り値のキー構成・計算式はplan_settlement_effects()に
    委譲したまま)。戻り値は {"choice": choice, "effects": effects} —— choiceを
    素通りさせているのは、呼び出し側がchoiceそのものを保持していなくても
    (将来の呼び出し形が変わっても)この戻り値だけでイベント payload
    (choice["key"]・choice["label"]等)を組み立てられるようにするため。

    plan_settlement_effects_fn(省略時はこのモジュール自身のplan_settlement_
    effects()を使う)は、game.py側のラッパーがgame.plan_settlement_effects
    (呼び出し時点でmonkeypatchされうる関数オブジェクト)をそのまま渡せる
    ようにするための差し替え口——他のengine.py内関数の*_fn引数と同じ理由
    (呼び出し元の互換ラッパー経由のmonkeypatchを伝播させるため)。

    2026-08-15追加(契約執行制度): enforcement_stage・enforcement_capacityを
    plan_settlement_effects(_fn)へキーワードで転送する。省略時はいずれも
    ENFORCEMENT_STAGE_INSTITUTIONAL/ENFORCEMENT_CAPACITY_INITIAL(=現状と
    同一の計算結果)なので既存呼び出しは無改造で動く。"""
    effects = (plan_settlement_effects_fn(
                   contract, choice["key"], choice["settle"], turn,
                   bank_trust, currency_confidence, bank_credit_losses, counterparty_trust,
                   enforcement_stage=enforcement_stage, enforcement_capacity=enforcement_capacity)
              if plan_settlement_effects_fn else
              plan_settlement_effects(
                   contract, choice["key"], choice["settle"], turn,
                   bank_trust, currency_confidence, bank_credit_losses, counterparty_trust,
                   dependencies=dependencies,
                   enforcement_stage=enforcement_stage, enforcement_capacity=enforcement_capacity))
    return {"choice": choice, "effects": effects}


@dataclass(frozen=True)
class NormalActionEngineDependencies:
    """available_normal_archetypes()・compute_normal_money_modifier()・
    build_normal_base_choice()・compute_social_contract_repay_range()・
    plan_normal_action_resolution()が game.py 側の現在値(ACTION_ARCHETYPES・
    CONTRACT_REPAY_MONEY・NPC関連関数など、いずれも game.py 固有で
    institutionsには属さない)を参照するための、明示的な依存の受け渡し容器。
    WorldStateではない(制度スキーマの再設計ではなく、通常行動エンジンを
    game.pyから独立させるための最小限の依存注入)。"""
    action_archetypes: list
    npc_price_modifier_fn: Callable[[dict], float]
    draw_ranges_fn: Callable[[dict], dict]
    indexed_money_ranges_fn: Callable[[dict, int], dict]
    draw_archetype_cost_fn: Callable[[str, int], dict]
    draw_archetype_hours_fn: Callable[[str], int]
    initial_trust_for_new_npc_fn: Callable[[dict, int], float]
    contract_repay_money: tuple
    contract_credit_limit_fn: Callable[[float, int, int], float]
    barter_choice_evaluation_fn: Callable[..., dict]
    # 2026-08-15(Step 6D、段階的な数値ルール共通化)で追加。
    # plan_normal_action_resolution()専用(他の3関数は使わない)。
    clamp_gain_fn: Callable[[dict, dict, dict], dict] = None


# 2026-08-14(Step 3B、段階的な数値ルール共通化): generate_normal_turn()(main()側、
# LLMがラベル文を書く)とsimulate_policy()(モンテカルロ側、ラベル不要)で
# 重複していた通常行動の選択肢生成を、4つのtop-level関数へ切り出した
# (Step 4Bでengine.pyへ移動)。現在のRNG消費順序は維持する(main/simulateで
# 順序が異なる箇所——新規NPCのdue_turn/ethics/repay_money/retire_turnの
# 並び——はそのまま残し、統一しない)。

def alternative_economy_triggered(bank_stage: int = BANK_STAGE_HEALTHY,
                                  currency_stage: int = CURRENCY_STAGE_NORMAL,
                                  local_credit_stage: int = LOCAL_CREDIT_STAGE_HEALTHY,
                                  enforcement_stage: int = ENFORCEMENT_STAGE_INSTITUTIONAL) -> bool:
    """上位制度(銀行・通貨・地域信用・契約執行)のいずれかが崩壊・縮退したかを
    判定する(2026-08-15追加、Step 13「物々交換・自給」)。物々交換・自給が
    通常行動の選択肢として現れるかどうかのゲート条件——健全な期間はこれらの
    選択肢自体が提示されないため、支配戦略化しない(仕様の要求「他制度が
    健全な期間にbarter/subsistenceが支配戦略にならないこと」)。

    「崩壊・縮退」の基準: 銀行が消滅(不可逆)・通貨が放棄・地域信用が個人化
    (Stage2)以上・契約執行が地域台帳移行(Stage2)以上のいずれか。地域信用/
    契約執行は「崩壊」ではなく「縮退」の語を使う仕様の表現に合わせ、Stage2
    (中間段階)から既にトリガーする(Stage3/4=完全崩壊まで待たない)。

    institutions/barter.py自身はbank_stage/currency_stage/local_credit_stage/
    enforcement_stageのいずれも参照しない(institutions配下は制度間の横断
    参照を持たないため)——この関数がengine.pyに置かれているのは、engine.py
    が既に4制度すべてのStage定数をimportしている層だから(build_settlement_
    choices・available_normal_archetypesと同じ扱い)。

    currency_stageの既定値はCURRENCY_STAGE_NORMAL。他の引数も各制度の
    Stage0を既定とするため、引数無しでは必ずFalseになる。"""
    return (bank_stage == BANK_STAGE_COLLAPSED
           or currency_stage == CURRENCY_STAGE_ABANDONED
           or local_credit_stage >= LOCAL_CREDIT_STAGE_PERSONAL
           or enforcement_stage >= ENFORCEMENT_STAGE_LOCAL_LEDGER)


def available_normal_archetypes(currency_stage: int,
                                local_credit_stage: int = LOCAL_CREDIT_STAGE_HEALTHY,
                                bank_stage: int = BANK_STAGE_HEALTHY,
                                enforcement_stage: int = ENFORCEMENT_STAGE_INSTITUTIONAL,
                                barter_stage: int = BARTER_STAGE_FUNCTIONING, *,
                                dependencies: NormalActionEngineDependencies) -> list:
    """通常行動で提示するarchetypeの一覧。ACTION_ARCHETYPESの元の順序を維持し、
    CURRENCY_STAGE_ABANDONED(通貨放棄)のときだけmoney型を除外する
    (誰も受け取らない通貨で解決しようとする選択肢を提示しない、という表現。
    Step 3B-1、generate_normal_turn/simulate_policy共通)。

    2026-08-15追加(地域信用制度、ユーザーレビュー指摘への対応、Stage3「孤立」の
    行動的帰結): local_credit_stageがLOCAL_CREDIT_STAGE_ISOLATED(信用できる
    相手がゼロになった状態)のときは、money型除外と同じ要領でsocial型も除外
    する(docs/social-regimes-spec.md「Stage3: 孤立。信用できる相手がゼロに
    なり、社会的契約が一切結べない」の実装)。local_credit_stage省略時は
    LOCAL_CREDIT_STAGE_HEALTHY扱い(=このフィルタは無効、既存呼び出しは
    無改造で動く)。

    2026-08-15追加(Step 13、物々交換・自給制度): bank_stage/enforcement_stage/
    barter_stageはキーワード省略時いずれも健全値なので、既存呼び出し
    (bank_stage/enforcement_stage/barter_stageを渡さない)は無改造で動く。
    alternative_economy_triggered()がTrue(上位制度が崩壊・縮退)のときだけ、
    ACTION_ARCHETYPESに無いbarter/subsistenceをリストの末尾へ追加する
    (除外ではなく追加である点がmoney/socialの扱いと逆——仕様「上位制度が
    崩壊・縮退した場合に、通常行動へbarterまたはsubsistenceを追加する」の
    直接実装)。barterはbarter_stageがFUNCTIONING/THINNED(Stage0/1)のときだけ
    (仕様「Stage0/1ではbarterが利用可能」)、subsistenceはトリガー成立時は
    常に追加する(仕様「Stage2ではbarterを停止し、subsistenceのみを代替経路と
    する」——Stage2以上ではbarterが自然に消え、subsistenceだけが残る)。"""
    result = list(dependencies.action_archetypes)
    if currency_stage == CURRENCY_STAGE_ABANDONED:
        result = [a for a in result if a["key"] != "money"]
    if local_credit_stage == LOCAL_CREDIT_STAGE_ISOLATED:
        result = [a for a in result if a["key"] != "social"]
    if alternative_economy_triggered(bank_stage, currency_stage, local_credit_stage, enforcement_stage):
        if barter_stage in (BARTER_STAGE_FUNCTIONING, BARTER_STAGE_THINNED):
            result = result + [BARTER_ARCHETYPE]
        result = result + [SUBSISTENCE_ARCHETYPE]
    return result


def compute_normal_money_modifier(acquaintances: dict, currency_stage: int, rng, *,
                                  dependencies: NormalActionEngineDependencies) -> float:
    """money型コストの価格倍率(docs/plan.md「[将来] 財の価格形成」の簡略実装)。
    顔なじみがいればその中からランダムに1人を売り手として価格に反映し
    (rng.choiceを正確に1回)、Stage1(選好低下)では追加のペナルティを掛ける
    (通貨を受け取る側が実質的に値切る、という解釈)。顔なじみがまだ居ない
    序盤は中立(1.0)のまま——このときrngは呼ばない(Step 3B-2、
    generate_normal_turn/simulate_policy共通)。"""
    money_modifier = dependencies.npc_price_modifier_fn(rng.choice(list(acquaintances.values()))) \
        if acquaintances else 1.0
    if currency_stage == CURRENCY_STAGE_WARY:
        money_modifier *= CURRENCY_STAGE1_PRICE_PENALTY
    return money_modifier


def build_normal_base_choice(archetype: dict, turn: int, money_modifier: float,
                             label: str = None, goods_state: dict = None, *,
                             dependencies: NormalActionEngineDependencies) -> dict:
    """通常行動archetype1件分の基礎choice(社会契約payloadは含まない——それは
    呼び出し側がarchetype.get("creates_contract")のときだけ別途追加する)。
    money型はmoney_modifierで価格を補正してからindexed_money_ranges/draw_ranges
    で名目額を抽選し、それ以外はdraw_archetype_costを使う。hoursは
    draw_archetype_hoursで抽選する(coreの並びは元のgenerate_normal_turn/
    simulate_policyと同じ: cost→hours)。

    label=None(simulate_policy向け)なら{"key","cost","hours"}、
    labelを渡す(generate_normal_turn向け、既にclean_label適用済みの文字列)
    なら{"key","label","cost","hours"}のキー構成・順序になる
    (Step 3B-3、generate_normal_turn/simulate_policy共通)。"""
    if archetype["key"] == "money":
        ranges = {k: (round(lo * money_modifier), round(hi * money_modifier))
                  for k, (lo, hi) in archetype["ranges"].items() if k == "money"}
        ranges.update({k: v for k, v in archetype["ranges"].items() if k != "money"})
        cost = dependencies.draw_ranges_fn(dependencies.indexed_money_ranges_fn(ranges, turn))
        cost = {k: v for k, v in cost.items() if v != 0}
    else:
        cost = dependencies.draw_archetype_cost_fn(archetype["key"], turn)
    # 2026-08-14追加(時間予算制の統合)。この行動にかかる時間。
    hours = dependencies.draw_archetype_hours_fn(archetype["key"])

    if label is None:
        choice = {"key": archetype["key"], "cost": cost, "hours": hours}
    else:
        choice = {"key": archetype["key"], "label": label, "cost": cost, "hours": hours}
    if goods_state is not None:
        evaluation_kwargs = {
            "provisioning_scale": goods_state.get("provisioning_scale", 1.0)}
        if goods_state.get("demand_scales_by_good") is not None:
            evaluation_kwargs["demand_scales_by_good"] = (
                goods_state["demand_scales_by_good"])
        choice.update(dependencies.barter_choice_evaluation_fn(
            archetype["key"], goods_state["food"], goods_state["medicine"],
            goods_state["shelter"], goods_state["tools"],
            goods_state["production_capacity"], **evaluation_kwargs))
    return choice


def compute_social_contract_repay_range(acquaintances: dict, existing, prospective_ethics,
                                        turn: int, due_turn: int, *,
                                        dependencies: NormalActionEngineDependencies) -> tuple:
    """social型契約の返済額(repay_money)レンジの純粋部分。乱数は使わない
    ——repay_money自体の抽選(random.randint(lo, hi))・due_turn・
    prospective_ethics・prospective_retire_turnの抽選はすべて呼び出し側に残す
    (Step 3B-4、generate_normal_turn/simulate_policy共通)。

    2026-08-14(ユーザー提案「信用によって借入金利が変わる」)の実装をそのまま
    引き継ぐ: npc_price_modifierは元々「財の価格」用の関数だが、契約の返済額
    にも同じ倍率を使う——初対面でもprospective_ethics/trust_for_limitを既に
    確定させているので、同じ式にそのまま渡して一貫させる。
    2026-08-14(ユーザー提案「収入以上の契約は本来は結べず、信用がとても高い
    場合のみ結べる」「信用が低い場合は収入以下の契約も断られる」)の実装も
    そのまま引き継ぐ: 収入基準の上限(相手の信用で伸び縮み)でrepay_moneyの
    絶対値を切り詰める。lo/hiは負値(債務額)なので、-limit以上に切り上げる
    形のクランプになる。

    戻り値: (trust_for_limit, lo, hi)。"""
    if existing:
        trust_for_limit = existing["trust"]
        price_npc = existing
    else:
        trust_for_limit = dependencies.initial_trust_for_new_npc_fn(acquaintances, turn)
        price_npc = {"trust": trust_for_limit, "ethics": prospective_ethics}
    modifier = dependencies.npc_price_modifier_fn(price_npc)
    # 契約の返済額は、契約が結ばれた時点の価格水準で名目額として固定する
    # (現実の金銭消費貸借と同じ。清算時に改めて指数を掛け直さない)。
    lo, hi = dependencies.indexed_money_ranges_fn(
        {"money": (round(dependencies.contract_repay_money[0] * modifier),
                  round(dependencies.contract_repay_money[1] * modifier))}, turn)["money"]
    limit = dependencies.contract_credit_limit_fn(trust_for_limit, turn, due_turn)
    lo, hi = max(lo, -limit), max(hi, -limit)
    return trust_for_limit, lo, hi


# 2026-08-15(Step 6D、段階的な数値ルール共通化): main()のprocess_one_situation
# (通常行動のkindの場合)とsimulate_policy()の通常行動ループには「選択済み
# choiceのcostへclamp_gainを適用する」という定型の接着コードがそれぞれ
# 独立に書かれていた(Step 6Cのplan_settlement_resolutionと同じ形の重複)。
# この関数はその接着部分だけをまとめる——選択肢生成・auto_select/手動選択・
# budget更新・イベント発行・resources/traitsへの直接反映・NPC登録・契約作成・
# 成長判定は、いずれも呼び出し側の責務のまま残す。settlement(清算)には
# 適用しない(そちらはplan_settlement_resolutionが別に扱う)。
def plan_normal_action_resolution(choice: dict, resources: dict, traits: dict, *,
                                  dependencies: NormalActionEngineDependencies) -> dict:
    """通常行動1件分の「選択済みchoice→適用delta」の接着。choice["cost"]の
    コピー(元のdictは変更しない)へclamp_gain_fnを正確に1回だけ適用する。
    それ以外の計算・分岐は一切持たない(戻り値のキー構成・計算式はclamp_gain
    に委譲したまま)。戻り値は{"choice": choice, "delta": delta}——choiceを
    素通りさせているのはplan_settlement_resolutionと同じ理由(呼び出し側が
    choiceそのものを保持していなくても、この戻り値だけでイベントpayload等を
    組み立てられるようにするため)。"""
    delta = dependencies.clamp_gain_fn(dict(choice["cost"]), resources, traits)
    return {"choice": choice, "delta": delta}
