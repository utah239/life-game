# -*- coding: utf-8 -*-
"""social型契約の相手選択を扱う純粋ルール。

Step 5I(2026-08-14、behavior-preserving refactoring)でgame.pyから分離した。
イベント保存、世界状態の投影、LLM、CLIには依存しない。randomモジュールへの
暗黙依存もない——rngは必須引数として呼び出し側(game.py)から受け取る
(main()側の共有randomモジュール・単体テストでのScripted rngのどちらも
そのまま渡せる、既存のpick_social_counterparty(rng=random)という設計を
踏襲する)。

このモジュールは game.py・event_store.py・projection.py・engine.py・
policy.py のいずれにも依存しない。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class CounterpartySelectionDependencies:
    """pick_social_counterparty()が game.py側の設定値を参照するための、
    明示的な依存の受け渡し容器。WorldStateではない(制度スキーマの再設計では
    なく、相手選択ルールをgame.pyから独立させるための最小限の依存注入)。
    NPC_POOL_CAP_RANGE・NPC_RELATIONSHIP_SPAN_RANGEは関数内で直接使わない
    ため含めない(npc_pool_cap・turnは通常引数のまま呼び出し側が渡す)。"""
    npc_ethics_range: tuple
    npc_reuse_rate: float
    npc_trust_initial: float
    npc_contract_trust_min: float


def pick_social_counterparty(acquaintances: dict, open_counterparties: set,
                             turn: int, npc_pool_cap: int, rng, *,
                             dependencies: CounterpartySelectionDependencies) -> tuple:
    """social型契約の相手を決める(コード側)。probe/main()どちらからも、
    それぞれの内部表現をacquaintances(id→npc dict)とopen_counterparties
    (現在返済待ちの相手名の集合)に正規化してから呼ぶことで、文字どおり
    同じ選択ロジックを共有する。
    戻り値: (name_or_None, is_new)。is_new=Trueのときnameは常にNone
    (新規の名前はLLM〈main()〉または合成カウンタ〈probe〉が呼び出し側で決める。
    固定の名前プールに縛られないことで、疎遠になった相手の名前と衝突しない)。
    """
    # 期限(NPC_RELATIONSHIP_SPAN_RANGE)が来た関係は自然に疎遠になり、
    # プールにも同時人数の枠にも数えない。retire_turnがNone(寿命の概念が
    # 無い相手。銀行など)なら無期限として扱う。
    active = {nid: n for nid, n in acquaintances.items()
              if (n.get("alive", True)
                  and turn < (n.get("retire_turn") or float("inf")))}

    def willing(nid: str, n: dict) -> bool:
        if nid not in open_counterparties:
            return True
        # まだ返し終えていない相手でも、倫理観(お人好しさ)が高いほど重ねて
        # 貸してくれる確率が上がる(一律禁止からの訂正)。
        ethics = n.get("ethics")
        if ethics is None:
            return False
        lo, hi = dependencies.npc_ethics_range
        p = (ethics - lo) / (hi - lo) if hi > lo else 0.5
        return rng.random() < max(0.0, min(1.0, p))

    def trusts_enough(n: dict) -> bool:
        # 2026-08-14追加(ユーザー提案「契約は互いの信用値が十分に高くないと
        # 成立しない」)。既存の相手には最低限のtrustを要求する。初対面
        # (is_new=True)にはこの下限は適用しない——初対面の初期trustは
        # player_reputation()(周りの人間からの信用値)がそのまま反映されるので、
        # ここで二重にゲートしない。
        return n.get("trust", dependencies.npc_trust_initial) >= dependencies.npc_contract_trust_min

    reusable = [n for nid, n in active.items() if willing(nid, n) and trusts_enough(n)]
    at_cap = len(active) >= npc_pool_cap
    if reusable and (at_cap or rng.random() < dependencies.npc_reuse_rate):
        return rng.choice(reusable)["name"], False
    if at_cap:
        # 2026-08-14訂正(ユーザー指摘「全員のtrustが下限を割ればゲームが
        # 詰むのでは?」): 実測したところ、旧実装はここで信用ゲートごと
        # 無視して強制的に誰かとの契約を成立させており、946回の相手選びの
        # うち646回(68.9%)が「信用ゲートを満たさない相手」だった——
        # 「契約は信用が十分でないと成立しない」というルール自体が、
        # プール上限に達した瞬間にほぼ無効化されていた。
        # trustは「開いている借りがあっても重ねて貸してくれるか」
        # (willing)より根が深い制約とみなし、上限に達したときは
        # willingのみ緩めてtrustは維持する2段構えにする。
        trusted_only = [n for nid, n in active.items() if trusts_enough(n)]
        if trusted_only:
            # 上限に達していて1段目(willing)が空でも、信用さえ足りていれば
            # (開いている借りがあっても)無理を承知で頼み込む。
            return rng.choice(trusted_only)["name"], False
        # 信用が足りる相手が既存の顔なじみに誰も居ない——この場合だけは
        # プール上限を一時的に超えてでも新しい人に頼る(信用ゲートは
        # プール上限より優先する、という設計判断。既存の関係の寿命
        # 〈NPC_RELATIONSHIP_SPAN_RANGE〉による自然減で、いずれ上限内に戻る)。
        return None, True
    return None, True
