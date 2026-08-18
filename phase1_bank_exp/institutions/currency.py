# -*- coding: utf-8 -*-
"""通貨制度の純粋ロジック(confidence・状態機械)。

段階的モジュール分割(Step 1、2026-08-14、behavior-preserving refactoring)で
game.py から移動。定数・計算式は一切変更していない(経緯:
docs/social-regimes-spec.md「通貨(そのものへの信用)」節)。

このモジュールは game.py・イベントログ(events.jsonl)・LLM・print に
依存しない。bank.py にも依存しない(bank_trustとは独立した指標にする、という
社会レジーム仕様の訂正をそのまま反映)。
"""

# --- 通貨の信用(currency_confidence)と状態機械(2026-08-14追加、
# docs/social-regimes-spec.md「通貨(そのものへの信用)」節) --------------------
# bank_trustとは独立した指標にする(社会レジーム仕様の訂正「銀行崩壊・通貨崩壊・
# 債務執行不能を無条件に連動させない」の実装)。同じ引き金(通貨危機)で両方が
# 同時にダメージを受けることはあるが、片方の値がもう片方を直接決定する式には
# しない。
CURRENCY_CONFIDENCE_INITIAL = 50.0  # 仮値。NPC_TRUST_INITIALと同じ発想の中立値
CURRENCY_CONFIDENCE_CAP = 100.0
# 2026-08-14訂正(bank_trustと同じ問題が発覚): 当初は「ゼロへの一方通行の
# 減衰」だったが、bank_trustの見直しと同じ実測手法で確認したところ、
# 全seed・全方針でT200までにほぼ0へ収束する、政策非依存に近い変数に
# なっていた。bank_trust_reversionと同じ理由・同じパターンで、中間値
# (CURRENCY_CONFIDENCE_INITIAL)への回帰に変更する。
CURRENCY_CONFIDENCE_REVERSION_RATE = 0.05  # 仮値。bank_trust_reversionと同じ速さ
CURRENCY_CRISIS_HIT = 15.0  # 仮値。通貨危機(デノミ)発生時の追加ダメージ

CURRENCY_STAGE_NORMAL = 0     # 通常
CURRENCY_STAGE_WARY = 1       # 選好低下
CURRENCY_STAGE_ABANDONED = 3  # 放棄。番号は仕様書のStage番号に合わせてあり、
                               # Stage2(地域通貨併存)はbarter未実装のため
                               # この実装では意図的にスキップする
CURRENCY_STAGE_NAMES = {0: "通常", 1: "選好低下", 3: "放棄"}

CURRENCY_STAGE1_ENTER = 25.0  # 仮値
CURRENCY_STAGE1_EXIT = 35.0   # 仮値
CURRENCY_STAGE3_ENTER = 8.0   # 仮値
CURRENCY_STAGE3_EXIT = 18.0   # 仮値

CURRENCY_STAGE1_PRICE_PENALTY = 1.4  # 仮値。Stage1でmoney型選択の名目コストに掛ける倍率


def currency_confidence_reversion(current: float) -> float:
    """1ターン分の、中間値(CURRENCY_CONFIDENCE_INITIAL)への回帰量。
    bank_trust_reversionと同じ形(呼び出し側は
    `currency_confidence -= currency_confidence_reversion(currency_confidence)`)。"""
    return round((current - CURRENCY_CONFIDENCE_INITIAL) * CURRENCY_CONFIDENCE_REVERSION_RATE, 6)


def currency_confidence_gain(current: float) -> float:
    """money型で契約(銀行債務)が履行されるたびに回復する量。CAPに近づくほど
    逓減する(既存のbank_trust_gain等と同じ逓減パターンの再利用)。"""
    return round(1.0 * max(0.0, 1.0 - current / CURRENCY_CONFIDENCE_CAP), 6)


def currency_stage_next(current_stage: int, confidence: float) -> int:
    """通貨の状態機械。bank_stage_nextと同じヒステリシス構造(崩壊は即座、
    復旧はexit閾値を満たすまで現状維持)。

    2026-08-14の設計判断: bank_stage(Stage3〈銀行制度消滅〉)と違い、この
    Stage3(放棄)は**不可逆にしない**。地域通貨・物々交換(仕様書のStage2・
    barter)がまだ未実装のこの実装フェーズでは、通貨放棄を不可逆にすると
    プレイヤーが後続手段の無いまま詰む——「崩壊後も縮退した社会形態で
    ゲームが継続する」という仕様の大原則に、現状の実装範囲では反してしまう
    ため。地域通貨/物々交換を実装した段階で、この判断を再検討する。"""
    if confidence < CURRENCY_STAGE3_ENTER:
        return CURRENCY_STAGE_ABANDONED
    if confidence < CURRENCY_STAGE1_ENTER and current_stage < CURRENCY_STAGE_WARY:
        return CURRENCY_STAGE_WARY
    if current_stage == CURRENCY_STAGE_ABANDONED:
        return CURRENCY_STAGE_WARY if confidence >= CURRENCY_STAGE3_EXIT else CURRENCY_STAGE_ABANDONED
    if current_stage == CURRENCY_STAGE_WARY:
        return CURRENCY_STAGE_NORMAL if confidence >= CURRENCY_STAGE1_EXIT else CURRENCY_STAGE_WARY
    return current_stage
