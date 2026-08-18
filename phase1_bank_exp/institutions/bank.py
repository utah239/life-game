# -*- coding: utf-8 -*-
"""銀行制度の純粋ロジック(trust・状態機械)。

段階的モジュール分割(Step 1、2026-08-14、behavior-preserving refactoring)で
game.py から移動。定数・計算式は一切変更していない——移動前の game.py の
コメント・数値をそのまま引き継ぐ(経緯: docs/plan.md「経済の閉じ方」節、
docs/social-regimes-spec.md「銀行・金融制度」節)。

このモジュールは game.py・イベントログ(events.jsonl)・LLM・print に
依存しない。price_index・compute_wage・compute_salary はここには無い
(CREATION_RATE の CLI 上書きや経済処理に依存するため、game.py 側に残す)。
"""

# 2026-08-13、4回目opusレビュー指摘で追加: NPC・銀行のtrustに上限が無く、
# 履行を重ねるだけで無限に伸びていた(player trustと同じ病理を、bank/NPC trustに
# 再導入していた)。TRUST_CAPと同じ逓減パターンを適用する上限を明示する
# (NPC側のNPC_TRUST_CAPはgame.py側に残る。ここはbank分だけ)。
BANK_TRUST_INITIAL = 50.0        # 銀行の初期信用。仮値
BANK_TRUST_CAP = 100.0
CRISIS_THRESHOLD_BASE = 800.0  # 旧150→80から訂正(下記)。2026-08-14、
                               # bank_credit_lossesがrepay_money(10倍スケール化
                               # 済み)から積み上がる実質損失なので、こちらも10倍


def crisis_threshold(bank_trust: float) -> float:
    """通貨危機の閾値。銀行の信用が厚いほど、より大きな損失に耐えられる
    (2026-08-13追加、ユーザー指摘「閾値も社会全体の信用に依存するはず」の実装)。"""
    return CRISIS_THRESHOLD_BASE * max(0.1, bank_trust / BANK_TRUST_INITIAL)


def bank_trust_gain(current_trust: float) -> float:
    """銀行の信用が履行のたびに増える量。BANK_TRUST_CAPに近づくほど逓減する
    (2026-08-13、4回目opusレビュー指摘: 上限が無く履行のたび+1され続けていた
    ため、crisis_thresholdも比例して無限に伸び、通貨危機が構造的に発火しない
    バグになっていた。contract_fulfill_bonusと同じ逓減パターンで直す)。"""
    return round(1.0 * max(0.0, 1.0 - current_trust / BANK_TRUST_CAP), 6)


# 2026-08-13、5回目opusレビュー指摘: 履行が踏み倒しより圧倒的に多いため、
# bank_trust_gain(上限つき)だけでは履行のたびcrisis_thresholdが単調に
# 持ち上がり続け、「踏み倒しが多いなら銀行がすぐ破綻する」という当初のユーザー
# 要求を満たせていなかった(通貨危機が「終末期の症状」としてしか出ない)。
# ~~player trustと同様、銀行のtrustにも毎ターンの減衰(平時から信認が擦り減る)を
# 入れる~~
#
# 2026-08-14訂正(社会レジーム仕様、実装フェーズ第1弾で発覚): 銀行の状態機械
# (Stage0〜3)を実装して初めて実測したところ、**全seed・全方針でbank_trustが
# T60で3.6〜6.0、T200で0.4〜0.7というほぼ同一の値に収束する**(通貨危機は
# ゼロ回のまま)ことが判明した——「ゼロへ向かう比例減衰」を毎ターン無条件に
# 適用していたため、履行イベント(gain、疎ら)がその継続的な減衰に追いつけず、
# **プレイヤーの行動とほぼ無関係に一定の速さでゼロへ向かう決定論的に近い
# 変数**になっていた。「踏み倒しが多いなら銀行が破綻する」という意図に対して、
# 「踏み倒しが少なくても銀行が破綻する」という意図しない挙動が上書きして
# しまっていた。
#
# NPC trustに既に入れた「中間値への回帰」(`effective_npc_trust`)と同じ
# パターンに変更する: ゼロへの一方通行の減衰ではなく、中間値
# (BANK_TRUST_INITIAL=50、中立)への回帰にする。中間値より高ければ下げる方向、
# 低ければ上げる方向に働く——「平時は中立へ戻ろうとする」設計にし、
# bank_trustを実際に動かすのは履行によるgainと、デフォルト・通貨危機による
# 明示的なペナルティ(このコメントの上と下にある、これらは変更していない)、
# という構図にする。bank_trustは単一エンティティで毎ターン既にイベントを
# 積んでいる(npc_trust_changed reason=bank_trust_upkeep)ため、NPC trustの
# ような「経過ターンをまとめて閉じた式で計算する」近似(イベントの蓄積を
# 避けるための工夫)は不要——単純に「1ターン分の回帰量」を返し、既存の
# decayと同じ形で毎ターン適用するだけでよい。
BANK_TRUST_REVERSION_RATE = 0.05  # 仮値。NPC trust(0.0125、10〜20年規模)より
                                   # 意図的に速くしてある——bank_trustは
                                   # 「社会全体の信認」という、より短い周期で
                                   # 動くべき指標だと判断したため(要再較正)


def bank_trust_reversion(current_trust: float) -> float:
    """1ターン分の、中間値(BANK_TRUST_INITIAL)への回帰量。呼び出し側は
    `bank_trust -= bank_trust_reversion(bank_trust)` として使う
    (中間値より高ければ正の値=引くと下がる、低ければ負の値=引くと上がる)。"""
    return round((current_trust - BANK_TRUST_INITIAL) * BANK_TRUST_REVERSION_RATE, 6)


# --- 銀行の状態機械(2026-08-14追加、docs/social-regimes-spec.md「銀行・金融
# 制度」節、実装フェーズ第1弾) -------------------------------------------------
# `bank_trust`という「劣化要因」自体は既に機能していたが、閾値を割っても
# 何も起きない(Stage遷移が無い)、という「再分類」で見つかった穴を埋める。
# Stage2(取り付け・支払停止)は「預金へのアクセス」という、現状のphase1_bank_exp
# には無い概念(moneyは常に手元にある現金として扱っており、銀行預金という
# 別枠が無い)が必要になるため、この実装では意図的にスキップする——仕様書の
# Stage0/1/2/4のうち、既存の仕組み(bank_trust・price_index)だけで実装できる
# 0/1/2/3(=仕様書のStage4)の4段階に絞った(仕様書のStage番号とは1つずれる)。
BANK_STAGE_HEALTHY = 0       # 健全
BANK_STAGE_CONTRACTION = 1   # 信用収縮(前借りの規模が縮む)
BANK_STAGE_HALTED = 2        # 融資停止(新規の前借りを発行しない。salaryは継続)
BANK_STAGE_COLLAPSED = 3     # 銀行制度消滅(不可逆)
BANK_STAGE_NAMES = {0: "健全", 1: "信用収縮", 2: "融資停止", 3: "銀行制度消滅"}

# ヒステリシス(docs/social-regimes-spec.md「ヒステリシス」節の指針どおり、
# 崩壊方向は一定の条件、復旧方向はより厳しい条件〈enterより高いexit閾値〉にする)。
BANK_STAGE1_ENTER = 30.0  # 仮値。bank_trustがこれを下回るとStage1へ(即座)
BANK_STAGE1_EXIT = 40.0   # 仮値。Stage1から復帰するにはこれを上回る必要がある
BANK_STAGE2_ENTER = 12.0  # 仮値
BANK_STAGE2_EXIT = 20.0   # 仮値
BANK_STAGE3_CRISIS_COUNT = 15  # 仮値。通貨危機の累積回数がこれに達すると
                                # Stage3(不可逆)。bank_trustの閾値だけだと
                                # 危機のたびに一時的に持ち直して「巻き戻るだけ」
                                # で終わってしまうため、別の指標(危機の累積回数)
                                # でも判定する
BANK_STAGE1_ADVANCE_SHRINK = 0.6   # 仮値。Stage1で前借り額をこの倍率に縮小
BANK_STAGE2_ADVANCE_SHRINK = 0.15  # 仮値。Stage2ではゼロにしない(下記参照)


def bank_stage_next(current_stage: int, bank_trust: float, bank_crisis_count: int) -> int:
    """銀行の状態機械の1ターン分の遷移判定。崩壊方向(悪化)は即座に反映、
    復旧方向はヒステリシス(exit閾値)を満たすまで現状維持する。Stage3は不可逆。"""
    if current_stage == BANK_STAGE_COLLAPSED:
        return BANK_STAGE_COLLAPSED
    if bank_crisis_count >= BANK_STAGE3_CRISIS_COUNT:
        return BANK_STAGE_COLLAPSED
    if bank_trust < BANK_STAGE2_ENTER:
        return BANK_STAGE_HALTED
    if bank_trust < BANK_STAGE1_ENTER and current_stage < BANK_STAGE_CONTRACTION:
        return BANK_STAGE_CONTRACTION
    if current_stage == BANK_STAGE_HALTED:
        return BANK_STAGE_CONTRACTION if bank_trust >= BANK_STAGE2_EXIT else BANK_STAGE_HALTED
    if current_stage == BANK_STAGE_CONTRACTION:
        return BANK_STAGE_HEALTHY if bank_trust >= BANK_STAGE1_EXIT else BANK_STAGE_CONTRACTION
    return current_stage
