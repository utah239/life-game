# -*- coding: utf-8 -*-
"""契約執行制度の純粋ロジック(enforcement_capacity・状態機械)。

2026-08-15、`docs/social-regimes-spec.md`「契約執行制度」節の実装。bank.py・
currency.py・local_credit.pyと同じ形の純粋モジュール——game.py・イベントログ
(events.jsonl)・LLM・printに依存しない。bank.py/currency.py/local_credit.py
のいずれにも依存しない(「銀行崩壊・通貨崩壊・地域信用崩壊・契約執行崩壊を
無条件に連動させない」という既存の社会レジーム仕様の訂正と同じ方針)。

## reachを実装しない近似について

仕様上、契約執行制度の崩壊条件は「このインスタンスのreach(統治力・地理的
概念)が村・家族レベルまで縮退すること」だが、reachの実装(集落・統治力
モデル、制度6「人口維持」の実装を前提とする)は2回にわたり見送られてきた
(docs/social-regimes-spec.md「次の作業」節参照)。

このモジュールはreachを実装せず、代わりに**契約の履行/不履行そのものから
駆動される独立スカラー(enforcement_capacity)への近似**で崩壊条件を表現する
——「制度が正常に機能しているか」を、統治力の及ぶ地理的範囲ではなく、
実際に処理されている契約の帰結(遵守されているか、破られているか)で
測る、という言い換え。これはbank_stage/currency_stage/local_credit_stageが
いずれも仕様の完全なスキーマ(institution_instance、id/kind/scope/status/
confidence/reachを持つ)ではなく単一スカラー+閾値による近似実装である、
という既存の設計妥協と同じ性質。

## 駆動源が「制度間の無条件連動」に反しないことについて

enforcement_capacityは銀行債務・社会契約どちらの履行/不履行からも駆動される
(engine.plan_settlement_effects側で接続)。これは「契約の履行/不履行という
共通のドメインイベント」を複数制度がそれぞれ独立に観測して自分のスカラーを
動かしているだけで、bank_trust(銀行債務由来)とcommunity_trust(社会契約
由来)が既に同じ形で独立に動いているのと同型——**制度スカラー同士を直接
参照する**(例: enforcement_capacityの計算式にbank_trustやcommunity_trustの
値そのものを読む)ことはしない、という一線は守っている。
"""

# --- 契約執行能力(enforcement_capacity) -------------------------------------
ENFORCEMENT_CAPACITY_INITIAL = 50.0  # 仮値。他制度と同じ中立値
ENFORCEMENT_CAPACITY_CAP = 100.0
ENFORCEMENT_CAPACITY_REVERSION_RATE = 0.05  # 仮値。bank_trust_reversion等と
                                              # 同じ速さから出発、要再較正

# 履行のたびの逓減する加算(bank_trust_gain/npc_trust_gain/community_trustの
# 逓減パターンと同型)。銀行債務・社会契約どちらの履行でも同じ式を使う
# (「第三者執行という同じ仕組みが試される」という解釈)。
def enforcement_capacity_gain(current: float) -> float:
    """契約が履行されるたびにenforcement_capacityが回復する量。CAPに近づく
    ほど逓減する。"""
    return round(1.0 * max(0.0, 1.0 - current / ENFORCEMENT_CAPACITY_CAP), 6)


# 不履行1件ごとの固定減算(仮値)。
# 2026-08-15訂正(実測による再較正): 当初-4.0(npc_trustの-8.0の半分)で
# 導入したところ、清算イベントの頻度が高い(1920ターンの60ターン間だけでも
# 履行/不履行あわせて70件超)ため、全seed・全方針でT60までにStage2〜4へ
# 到達する「即座に壊れる」挙動になっていた——bank_trust/currency_confidence
# で踏んだ「一方通行の減衰」とは別種だが、同じ「平時に機能する期間が無い」
# 問題。中間値回帰(ENFORCEMENT_CAPACITY_REVERSION_RATE)とのバランスを
# 実測しながら再較正し、-1.0にした結果: T60・T960まではStage0(健全)を維持し、
# T1920にかけて緩やかにStage1〜2へ推移する(seed・方針で経路も値も分かれる)、
# という「平時は機能・長期の蓄積で劣化」という狙いどおりの挙動になった。
ENFORCEMENT_DEFAULT_PENALTY = -1.0


def enforcement_capacity_reversion(current: float) -> float:
    """1ターン分の、中間値(ENFORCEMENT_CAPACITY_INITIAL)への回帰量。
    bank_trust_reversion等と同じ形(呼び出し側は
    `enforcement_capacity -= enforcement_capacity_reversion(enforcement_capacity)`)。"""
    return round((current - ENFORCEMENT_CAPACITY_INITIAL) * ENFORCEMENT_CAPACITY_REVERSION_RATE, 6)


# --- 契約執行の状態機械(docs/social-regimes-spec.md「契約執行制度」節の5段階) --
# Stage0(institutional、制度的執行。現状)/Stage1(執行の遅延・不確実性の増加)/
# Stage2(local_ledger、地域台帳への移行——第三者機関ではなく共同体の共有記録・
# 評判で執行を代替)/Stage3(personal、個人間。記録者不在、直接的な信頼・報復
# のみで執行)/Stage4(none、第三者執行不能。「何も起きない」ではなく「法的な
# 意味での契約という概念が機能しなくなる」の意味——当事者間の反応〈取引拒絶・
# 信用低下・追放・報復〉は地域信用・家族互酬の層で引き続き起こり得る)。
#
# 仕様は「reachが村・家族レベルまで縮退」を崩壊条件とし、個々の契約の
# record_statusがunprovableまで劣化すればその契約は事実上不可逆としているが、
# 制度インスタンス全体としての不可逆条件は明記されていない。人口機構(制度6)
# が無く不可逆を正当化できないため、local_creditと同じ判断で全段階を可逆にする。
ENFORCEMENT_STAGE_INSTITUTIONAL = 0  # 制度的執行(現状)
ENFORCEMENT_STAGE_DELAYED = 1        # 執行の遅延・不確実性
ENFORCEMENT_STAGE_LOCAL_LEDGER = 2   # 地域台帳への移行
ENFORCEMENT_STAGE_PERSONAL = 3       # 個人間のみ
ENFORCEMENT_STAGE_NONE = 4           # 第三者執行不能
ENFORCEMENT_STAGE_NAMES = {
    0: "制度的執行", 1: "執行の遅延", 2: "地域台帳移行", 3: "個人間執行", 4: "第三者執行不能",
}

# local_creditの"community"と対になる定数(ユーザーレビュー指摘4件のうち
# scope="nation"固定を是正した際の設計判断と同じ形)。契約執行は中央の司法・
# 統治制度という前提なので"nation"のまま(bank/currencyと同じ扱い)。
ENFORCEMENT_SCOPE = "nation"

# ヒステリシス(崩壊方向は一定の条件、復旧方向はより厳しい条件〈enterより
# 高いexit閾値〉にする。bank_stage_next等と同じ方針)。
ENFORCEMENT_STAGE1_ENTER = 42.0  # 仮値
ENFORCEMENT_STAGE1_EXIT = 48.0   # 仮値
ENFORCEMENT_STAGE2_ENTER = 30.0  # 仮値
ENFORCEMENT_STAGE2_EXIT = 37.0   # 仮値
ENFORCEMENT_STAGE3_ENTER = 18.0  # 仮値
ENFORCEMENT_STAGE3_EXIT = 25.0   # 仮値
ENFORCEMENT_STAGE4_ENTER = 6.0   # 仮値
ENFORCEMENT_STAGE4_EXIT = 13.0   # 仮値


def enforcement_stage_next(current_stage: int, enforcement_capacity: float) -> int:
    """契約執行の状態機械の1ターン分の遷移判定。崩壊方向(悪化)は即座に反映、
    復旧方向はヒステリシス(exit閾値)を満たすまで現状維持する(1段階ずつしか
    戻らない、他制度と同じ挙動)。bank_stage_next/local_credit_stage_nextと
    同じ構造の5段階版(全段階可逆、上記コメント参照)。"""
    if enforcement_capacity < ENFORCEMENT_STAGE4_ENTER:
        return ENFORCEMENT_STAGE_NONE
    if (enforcement_capacity < ENFORCEMENT_STAGE3_ENTER
            and current_stage < ENFORCEMENT_STAGE_PERSONAL):
        return ENFORCEMENT_STAGE_PERSONAL
    if (enforcement_capacity < ENFORCEMENT_STAGE2_ENTER
            and current_stage < ENFORCEMENT_STAGE_LOCAL_LEDGER):
        return ENFORCEMENT_STAGE_LOCAL_LEDGER
    if (enforcement_capacity < ENFORCEMENT_STAGE1_ENTER
            and current_stage < ENFORCEMENT_STAGE_DELAYED):
        return ENFORCEMENT_STAGE_DELAYED
    if current_stage == ENFORCEMENT_STAGE_NONE:
        return (ENFORCEMENT_STAGE_PERSONAL if enforcement_capacity >= ENFORCEMENT_STAGE4_EXIT
               else ENFORCEMENT_STAGE_NONE)
    if current_stage == ENFORCEMENT_STAGE_PERSONAL:
        return (ENFORCEMENT_STAGE_LOCAL_LEDGER if enforcement_capacity >= ENFORCEMENT_STAGE3_EXIT
               else ENFORCEMENT_STAGE_PERSONAL)
    if current_stage == ENFORCEMENT_STAGE_LOCAL_LEDGER:
        return (ENFORCEMENT_STAGE_DELAYED if enforcement_capacity >= ENFORCEMENT_STAGE2_EXIT
               else ENFORCEMENT_STAGE_LOCAL_LEDGER)
    if current_stage == ENFORCEMENT_STAGE_DELAYED:
        return (ENFORCEMENT_STAGE_INSTITUTIONAL if enforcement_capacity >= ENFORCEMENT_STAGE1_EXIT
               else ENFORCEMENT_STAGE_DELAYED)
    return current_stage


# --- Stageごとの行動的帰結(いずれも仮値、要再較正) ---------------------------
_PENALTY_MULTIPLIER = {
    ENFORCEMENT_STAGE_INSTITUTIONAL: 1.0,
    ENFORCEMENT_STAGE_DELAYED: 0.6,
    ENFORCEMENT_STAGE_LOCAL_LEDGER: 0.3,
    ENFORCEMENT_STAGE_PERSONAL: 0.1,
    ENFORCEMENT_STAGE_NONE: 0.0,
}

_TRUST_AMPLIFIER = {
    ENFORCEMENT_STAGE_INSTITUTIONAL: 1.0,
    ENFORCEMENT_STAGE_DELAYED: 1.0,
    ENFORCEMENT_STAGE_LOCAL_LEDGER: 1.5,
    ENFORCEMENT_STAGE_PERSONAL: 2.0,
    ENFORCEMENT_STAGE_NONE: 2.0,
}


def contract_enforcement_penalty_multiplier(stage: int) -> float:
    """第三者ペナルティ(engine.build_settlement_choicesの"avoid"選択肢costの
    peace)へ掛ける倍率。Stageが進むほど第三者による処罰の実効性が下がる
    ——Stage4(第三者執行不能)では0.0(第三者ペナルティが無くなる。ただし
    counterparty_trust/community_trustは別途機能し続ける、下記
    contract_enforcement_trust_amplifier参照。仕様の「Stage4は『何も起きない』
    ではない」という訂正の実装)。"""
    return _PENALTY_MULTIPLIER[stage]


def contract_enforcement_trust_amplifier(stage: int) -> float:
    """社会契約の履行/不履行によるcounterparty_trust_delta・community_trust_
    deltaへ掛ける倍率。Stage2(地域台帳への移行)以上で非公式な制裁(個人間の
    信頼毀損・地域評判)が強まることを1つの係数で表現する——地域信用(4)が
    契約執行の代替執行層として機能する、という接続点。community/counterparty
    それぞれ別軸で増幅の強さを変える精緻化は将来の課題としてここに残す
    (現時点では両方に同じ倍率を適用する)。"""
    return _TRUST_AMPLIFIER[stage]
