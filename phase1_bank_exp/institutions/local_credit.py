# -*- coding: utf-8 -*-
"""地域信用制度の純粋ロジック(community_trust・状態機械)。

2026-08-15、`docs/social-regimes-spec.md`「地域信用(顔なじみ間の貸し借り・
共有台帳・評判)」節の実装。bank.py・currency.pyと同じ形の純粋モジュール
——game.py・イベントログ(events.jsonl)・LLM・printに依存しない。

`community_trust`は`bank_trust`/`currency_confidence`と完全に同型の独立
スカラー(NPCとしてはモデル化しない)。理由: `offline_simulation.py`
(direct-mutation経路)では銀行・地域経済すらNPCとしてモデル化されておらず
(ローカル変数のみ)、疑似NPC化は2つの世界エンジンの一方にしか自然に馴染まない
ため。「NPC間評判伝播」は、(a)社会契約の履行/不履行のたびに個人trustの
変化の一部がcommunity_trustへも波及する、(b)新規relationshipの初期trust
計算がcommunity_trustを(Stage0/1のときだけ)加味する、という2方向の接続で
表現する(接続部はengine.py/relationship_rules.py側)。bank.pyにもcurrency.py
にも依存しない——「銀行崩壊・通貨崩壊・地域信用崩壊を無条件に連動させない」
という既存の社会レジーム仕様の訂正と同じ方針。

2026-08-15追記(ユーザーレビュー指摘への対応、「記録」と「参照」の非対称性を
明示的な設計判断にする): 上記(a)「記録」(社会契約の結果をcommunity_trustへ
反映する処理、engine.plan_settlement_effectsのcommunity_trust_delta計算・
event_builders.build_settlement_effect_eventsのcommunity_trust_changed
イベント発行)は**local_credit_stageに関係なく常に行う**——不変イベントログ
(docs/social-regimes-spec.md「不変イベントログと『社会が利用できる記録』の
区別」節)と同じ精神で、「地域社会の集合的な記憶(エンジン層の値)」自体は
制度が壊れても書き換えを止めない。止まるのは上記(b)「参照」(新規relationship
の初期trust計算がこの記憶を読みに行くかどうか)だけで、Stage2(個人化)以上
では「共有台帳・評判の伝播が機能しなくなり、個々の一対一関係のみが頼り」
という仕様の記述どおり、この記憶への**アクセス**が絶たれる、という表現。
「記録は続くが読めなくなる」は、契約執行制度(3)の`record_status`
(`intact`→...→`unprovable`)と同じ発想——エンジンの真実性を保ったまま
「制度や記録そのものが壊れる」を表現する、という仕様全体の一貫した原則。
"""

# --- 地域全体の信用(community_trust) -----------------------------------------
LOCAL_CREDIT_TRUST_INITIAL = 50.0  # 仮値。NPC_TRUST_INITIAL/BANK_TRUST_INITIALと
                                    # 同じ発想の中立値
# bank_trust/currency_confidenceで踏んだ教訓(「ゼロへの一方通行の減衰」は
# プレイヤーの行動とほぼ無関係に一定の速さでゼロへ収束する政策非依存の変数を
# 作ってしまう)を、community_trustでは最初から回避する。導入直後から
# 中間値への回帰にしておく。
LOCAL_CREDIT_TRUST_REVERSION_RATE = 0.05  # 仮値。bank_trust_reversionと同じ速さ
                                           # から出発、要再較正

# 個人取引→地域全体への波及の強さ。社会契約(is_bank_debt=False)の履行/不履行で
# 生じる個人trustのdelta(npc_trust_gainの逓減、または不履行の固定penalty)に
# この倍率を掛けた分だけcommunity_trustも動く。community_trust専用の
# 「CAP付き逓減」関数は持たない(個人trust側のCAP逓減をそのまま継承するため)。
LOCAL_CREDIT_PROPAGATION_SCALE = 0.15  # 仮値。要再較正

# 地域全体→新規relationshipの初期trustへの反映の強さ(「NPC間評判伝播」の
# もう片側)。新規relationshipの初期trust計算(initial_trust_for_new_npc)で、
# player_reputation(現役の顔なじみだけの平均、疎遠になると忘れる)と
# community_trust(疎遠になっても忘れない、地域全体の記憶)をこの重みで
# 単純平均する。local_credit_stageがStage2(個人化)以上のときはこの重みを
# 適用せず、従来どおりplayer_reputationのみを使う(呼び出し側=game.pyの責務)。
LOCAL_CREDIT_INITIAL_TRUST_WEIGHT = 0.5  # 仮値。要再較正

# --- 地域信用の状態機械(docs/social-regimes-spec.md「地域信用」節の4段階) ------
# Stage0(通常)/Stage1(信用収縮)/Stage2(個人化。共有台帳・評判の伝播が機能しなく
# なり、個々の一対一関係のみが頼り)/Stage3(孤立。信用できる相手がゼロになる)。
# 純粋な信用状態機械はStage3も含めて可逆に保つ。人口を持たないinteractive
# 経路でも使うため、人口ゼロという外部条件をここへ混ぜない。デジタル水槽の
# 集落別経路では、人口ゼロの集落を上位ループがStage3へ固定し、交易候補からも
# 除外することで「担い手消滅時のみ実質不可逆」を表現する。
LOCAL_CREDIT_STAGE_HEALTHY = 0      # 通常
LOCAL_CREDIT_STAGE_CONTRACTION = 1  # 信用収縮
LOCAL_CREDIT_STAGE_PERSONAL = 2     # 個人化
LOCAL_CREDIT_STAGE_ISOLATED = 3     # 孤立
LOCAL_CREDIT_STAGE_NAMES = {0: "通常", 1: "信用収縮", 2: "個人化", 3: "孤立"}

# 2026-08-15追加(ユーザーレビュー指摘への対応)。institution_transitionイベントの
# scopeフィールドはbank/currencyでは"nation"固定だが、地域信用は最初から「1つの
# 共同体」という設定なので、"nation"(国家)ではなく"community"の方が実態に近い
# ——将来、契約執行制度(3)のreach(国家→地域→村→家族の縮退)を実装する際に
# "nation"という値が衝突・混同しないようにする意図もある。bank/currencyの
# scope="nation"は中央機関としての記述として正しいため変更しない。
LOCAL_CREDIT_SCOPE = "community"

# ヒステリシス(崩壊方向は一定の条件、復旧方向はより厳しい条件〈enterより
# 高いexit閾値〉にする。bank_stage_next/currency_stage_nextと同じ方針)。
LOCAL_CREDIT_STAGE1_ENTER = 38.0  # 仮値。中立(50)に近い狭い帯から始める
LOCAL_CREDIT_STAGE1_EXIT = 45.0   # 仮値
LOCAL_CREDIT_STAGE2_ENTER = 25.0  # 仮値
LOCAL_CREDIT_STAGE2_EXIT = 33.0   # 仮値
LOCAL_CREDIT_STAGE3_ENTER = 12.0  # 仮値
LOCAL_CREDIT_STAGE3_EXIT = 20.0   # 仮値


def community_trust_reversion(current: float) -> float:
    """1ターン分の、中間値(LOCAL_CREDIT_TRUST_INITIAL)への回帰量。
    bank_trust_reversion/currency_confidence_reversionと同じ形(呼び出し側は
    `community_trust -= community_trust_reversion(community_trust)`)。"""
    return round((current - LOCAL_CREDIT_TRUST_INITIAL) * LOCAL_CREDIT_TRUST_REVERSION_RATE, 6)


def local_credit_stage_next(current_stage: int, community_trust: float) -> int:
    """地域信用の状態機械の1ターン分の遷移判定。崩壊方向(悪化)は即座に反映、
    復旧方向はヒステリシス(exit閾値)を満たすまで現状維持する。bank_stage_next
    と同じ構造の4段階版(全段階可逆、上記コメント参照)。"""
    if community_trust < LOCAL_CREDIT_STAGE3_ENTER:
        return LOCAL_CREDIT_STAGE_ISOLATED
    if community_trust < LOCAL_CREDIT_STAGE2_ENTER and current_stage < LOCAL_CREDIT_STAGE_PERSONAL:
        return LOCAL_CREDIT_STAGE_PERSONAL
    if community_trust < LOCAL_CREDIT_STAGE1_ENTER and current_stage < LOCAL_CREDIT_STAGE_CONTRACTION:
        return LOCAL_CREDIT_STAGE_CONTRACTION
    if current_stage == LOCAL_CREDIT_STAGE_ISOLATED:
        return LOCAL_CREDIT_STAGE_PERSONAL if community_trust >= LOCAL_CREDIT_STAGE3_EXIT else LOCAL_CREDIT_STAGE_ISOLATED
    if current_stage == LOCAL_CREDIT_STAGE_PERSONAL:
        return LOCAL_CREDIT_STAGE_CONTRACTION if community_trust >= LOCAL_CREDIT_STAGE2_EXIT else LOCAL_CREDIT_STAGE_PERSONAL
    if current_stage == LOCAL_CREDIT_STAGE_CONTRACTION:
        return LOCAL_CREDIT_STAGE_HEALTHY if community_trust >= LOCAL_CREDIT_STAGE1_EXIT else LOCAL_CREDIT_STAGE_CONTRACTION
    return current_stage
