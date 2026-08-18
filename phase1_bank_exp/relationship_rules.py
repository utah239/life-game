# -*- coding: utf-8 -*-
"""対人信用(NPCのtrust・評判)・契約上限を扱う純粋ルール。

段階的モジュール分割(behavior-preserving refactoring)でgame.pyから分離した:
- Step 5F(2026-08-14): npc_price_modifier・npc_trust_gain・player_reputation・
  initial_trust_for_new_npc・contract_credit_multiplier・contract_credit_limit。
- Step 5G(2026-08-14): effective_npc_trust(NPC信用の中間値への指数回帰)。
  projection.pyのProjectionDependencies.effective_npc_trust_fnはgame.py側の
  互換ラッパー(関数オブジェクト)を保持する接続方式のままで、projection.py
  自体は変更していない。

イベント保存、世界状態の投影、乱数、LLM、CLIには依存しない。CLIから変更可能な
CREATION_RATEに連動するprice_indexも、RelationshipRulesDependencies経由で
呼び出し時点の関数を受け取る(import時点で固定しない)。

このモジュールは game.py・event_store.py・projection.py・engine.py・
policy.py のいずれにも依存しない。pick_social_counterparty(NPC生成・
疎遠化に関わる)は今回の対象外——別Stepで扱う。
"""
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class RelationshipRulesDependencies:
    """npc_price_modifier()・npc_trust_gain()・player_reputation()・
    initial_trust_for_new_npc()・contract_credit_multiplier()・
    contract_credit_limit()・effective_npc_trust()が game.py側の設定値・
    価格水準を参照するための、明示的な依存の受け渡し容器。WorldStateではない
    (制度スキーマの再設計ではなく、対人信用ルールをgame.pyから独立させるための
    最小限の依存注入)。acquaintances・npcといったNPCそのもののデータは
    この容器に持たせず、各関数の通常引数として渡す。"""
    npc_trust_initial: float
    npc_contract_trust_min: float
    npc_reputation_weight: float
    npc_trust_cap: float
    contract_credit_multiplier_max: float
    income_interval: int
    salary_base: float
    price_index_fn: Callable[[int], float]
    npc_trust_reversion_rate: float
    # 2026-08-15追加(地域信用制度、ユーザーレビュー指摘への対応: Stage判定・
    # ブレンド式はgame.py側ではなくここに実装すべき、という是正)。
    # initial_trust_for_new_npc()がcommunity_trustをブレンドするかどうかの
    # 閾値と重み。institutions.local_credit.LOCAL_CREDIT_STAGE_PERSONAL/
    # LOCAL_CREDIT_INITIAL_TRUST_WEIGHTの値をgame.py側が渡す。
    local_credit_stage_personal: int
    local_credit_initial_trust_weight: float


def npc_price_modifier(npc: dict, *, dependencies: RelationshipRulesDependencies) -> float:
    """財の価格に掛ける倍率。倫理観が低い(悪徳)ほど、信用が薄いほど高くつく。
    docs/plan.md「[将来] 財の価格形成」の簡略実装
    (price = base_cost × ethics_modifier × brand_premium。需要供給はまだ未実装)。
    新規の相手(NPCがまだ存在しない)は中立(1.0)として扱う。

    2026-08-13、4回目opusレビュー指摘で訂正: 旧trust_factorはNPC_TRUST_INITIAL
    (=50)のとき0.8になっており、「倫理観と無関係に、NPCが永続化された瞬間に
    一律2割引」というバグだった。NPC_TRUST_INITIALのとき1.0になるよう中心を
    合わせる(trust=0→1.3倍・trust=100→0.7倍、線形)。"""
    ethics = npc.get("ethics", 50.0)
    trust = npc.get("trust", dependencies.npc_trust_initial)
    ethics_factor = 1.5 - ethics / 100.0                     # ethics=20→1.3 / 80→0.7
    trust_factor = 1.0 - 0.006 * (trust - dependencies.npc_trust_initial)  # trust=50(基準)→1.0
    return max(0.5, ethics_factor * trust_factor)


def npc_trust_gain(current_trust: float, *,
                   dependencies: RelationshipRulesDependencies) -> float:
    """顔なじみNPC自身の信用が履行のたびに増える量。NPC_TRUST_CAPに近づくほど
    逓減する(2026-08-13、4回目opusレビュー指摘: 上限が無いと17回ほどの履行で
    誰でもnpc_price_modifierの下限倍率〈0.5〉に張り付き、ethics軸〈倫理観〉が
    長期的に洗い流されてしまうため)。"""
    return round(3.0 * max(0.0, 1.0 - current_trust / dependencies.npc_trust_cap), 6)


def player_reputation(acquaintances: dict, turn: int, *,
                      dependencies: RelationshipRulesDependencies) -> float:
    """プレイヤーの一般的な評判(2026-08-14追加、2026-08-14再訂正)。現役の
    (疎遠になっていない)顔なじみのtrustの単純平均。誰も居なければ実績が
    無いのでNPC_TRUST_INITIAL(中立)を返す。"""
    active = {nid: n for nid, n in acquaintances.items()
              if (n.get("alive", True)
                  and turn < (n.get("retire_turn") or float("inf")))}
    if not active:
        return dependencies.npc_trust_initial
    return (sum(n.get("trust", dependencies.npc_trust_initial) for n in active.values())
           / len(active))


def initial_trust_for_new_npc(acquaintances: dict, turn: int, *,
                              dependencies: RelationshipRulesDependencies,
                              player_reputation_fn: Callable = None,
                              local_credit_stage: int = None,
                              community_trust: float = None) -> float:
    """新規relationshipの初期trust。評判をそのまま使うと自己強化スパイラルが
    起きるため、NPC_TRUST_INITIAL(中立)との加重平均にして悪評の影響を
    和らげる——それでも評判が反映はされる、という着地点。

    2026-08-15追加(地域信用制度、「NPC間評判伝播」のもう片側): local_credit_
    stageがdependencies.local_credit_stage_personal(Stage2「個人化」)未満の
    ときだけ、community_trust(疎遠になっても忘れない、地域全体の記憶)を
    player_reputation(現役の顔なじみだけの平均、疎遠になると忘れる)と
    dependencies.local_credit_initial_trust_weightで単純平均する。Stage2
    以上、またはlocal_credit_stage/community_trustが渡されなければ(省略時の
    既定)、従来どおりplayer_reputationのみを使う——spec「Stage2: 共有台帳・
    評判の伝播が機能しなくなる」の実装。このStage判定・ブレンド式は元は
    game.py側のラッパーに書かれていたが、「game.pyは配線だけ」という
    アーキテクチャ境界を守るためここへ移設した(ユーザーレビュー指摘への
    対応、2026-08-15)。呼び出し側(game.py)はstate_nowから読んだ値を
    そのまま渡すだけになる。"""
    base_rep_fn = player_reputation_fn or (
        lambda acq, t: player_reputation(acq, t, dependencies=dependencies))
    if (community_trust is not None and local_credit_stage is not None
            and local_credit_stage < dependencies.local_credit_stage_personal):
        w = dependencies.local_credit_initial_trust_weight
        rep = (1 - w) * base_rep_fn(acquaintances, turn) + w * community_trust
    else:
        rep = base_rep_fn(acquaintances, turn)
    return ((1 - dependencies.npc_reputation_weight) * dependencies.npc_trust_initial
           + dependencies.npc_reputation_weight * rep)


def contract_credit_multiplier(trust: float, *,
                               dependencies: RelationshipRulesDependencies) -> float:
    """信用に応じた「収入の何倍まで契約を結べるか」の倍率。"""
    if trust >= dependencies.npc_trust_initial:
        span = dependencies.npc_trust_cap - dependencies.npc_trust_initial
        frac = (trust - dependencies.npc_trust_initial) / span if span > 0 else 0.0
        return 1.0 + frac * (dependencies.contract_credit_multiplier_max - 1.0)
    span = dependencies.npc_trust_initial - dependencies.npc_contract_trust_min
    frac = (trust - dependencies.npc_contract_trust_min) / span if span > 0 else 0.0
    return max(0.0, frac)


def contract_credit_limit(trust: float, turn: int, due_turn: int, *,
                          dependencies: RelationshipRulesDependencies,
                          contract_credit_multiplier_fn: Callable = None) -> float:
    """収入基準の契約額上限(名目、repay_moneyと同じ単位)。期限までに
    得られるはずの所得総額(SALARY_BASE×price_index×支払い回数)に、信用に
    よる倍率(contract_credit_multiplier)を掛ける。repay_moneyは契約時点の
    価格水準で名目額として固定される設計なので、ここもprice_index(turn)
    (現在)を使い、単位を揃える。

    2026-08-14訂正1(P0-3「所得と融資を分離する」): 基準をWAGE_BASE(前借り、
    既に銀行への返済義務がある=可処分ではない)からSALARY_BASE(返済義務の
    無い本当の所得)に変更した。友人が「この人はどれだけ返せそうか」を
    見積もるなら、既に銀行に取られる分ではなく手元に残る分を基準にする方が
    自然なため。
    2026-08-14訂正2(ユーザーが持ち込んだ外部レビュー指摘・P1-5): 従来は
    支払い回数を`(due_turn-turn)/INCOME_INTERVAL`という連続近似で計算して
    いたが、実際の賃金日はINCOME_INTERVALの倍数という離散的なタイミングで
    しか来ない(例: turn=1→due_turn=3では実収入は0回なのに0.5回分を見込んで
    いた)。turn/due_turnそれぞれをINCOME_INTERVALで整数除算した差分で、
    実際に(turn, due_turn]の間に来る支払い回数を数える。"""
    n_payments = due_turn // dependencies.income_interval - turn // dependencies.income_interval
    expected_income = (dependencies.salary_base * dependencies.price_index_fn(turn)
                       * max(0, n_payments))
    multiplier = (contract_credit_multiplier_fn(trust) if contract_credit_multiplier_fn
                 else contract_credit_multiplier(trust, dependencies=dependencies))
    return expected_income * multiplier


# 2026-08-14追加(ユーザー提案「信用は高くなっても低くなってもその後何もしなければ
# 10〜20年という長い時間をかけてだんだんと中間値に向かっていく。それに対して
# プラスの行動〈契約の履行や寄付やボランティア等〉をするか、マイナスの行動
# 〈契約の不履行や犯罪行為等〉をするかという話」)。
# 履行/不履行のたびに離散的にtrustを動かすだけだと、一度傷付いた・盛り上がった
# 関係がそのまま固定されてしまう(「何もしなければ現状維持」)。何もしなくても
# 極端さが薄れていく、という指摘を受けて、NPC_TRUST_INITIAL(中立)への指数的な
# 回帰を導入する。
# 「寄付・ボランティア(プラス)」「犯罪行為(マイナス)」は現状は行動として
# 実装が無い(履行/不履行のみ)ので、今回はこの回帰の仕組みだけを入れる。
#
# main()はイベントソーシングなので、経過ターンぶん「回帰イベント」を1件ずつ
# 積む実装(NPCの数×ターン数ぶん)は現実的でない。「式にする」方針のとおり、
# npc_trust_changedが最後に起きたターンからの経過ターン数を使った閉じた式
# (指数減衰の解)で、読み出し時にその場で計算する——ログには最後の明示的な
# 変化だけを記録し、回帰そのものはイベントを積まない導出値として扱う。
def effective_npc_trust(npc: dict, turn: int, *,
                        dependencies: RelationshipRulesDependencies) -> float:
    """中間値(NPC_TRUST_INITIAL)への回帰を適用した、現在時点のtrust。
    npc["trust"]は「最後に明示的な変化(npc_trust_changed)が起きた時点の値」、
    npc["trust_updated_turn"]はそのときのターンを保持している前提。

    2026-08-14(Step 5G、段階的モジュール分割): game.pyから移動。
    projection.pyのreduce_events()はこの関数をProjectionDependencies.
    effective_npc_trust_fn経由(game.py側の互換ラッパーの関数オブジェクト)で
    呼ぶ——移動にあたりprojection.pyの接続方式・処理順は一切変更していない。"""
    last_trust = npc.get("trust", dependencies.npc_trust_initial)
    last_turn = npc.get("trust_updated_turn")
    if last_turn is None:
        last_turn = npc.get("birth_turn", turn)
    elapsed = max(0, turn - last_turn)
    return (dependencies.npc_trust_initial
           + (last_trust - dependencies.npc_trust_initial)
           * (1 - dependencies.npc_trust_reversion_rate) ** elapsed)
