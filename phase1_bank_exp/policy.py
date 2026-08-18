# -*- coding: utf-8 -*-
"""自動プレイの方針評価・選択ロジック。

Step 5A(2026-08-14、behavior-preserving refactoring)でgame.pyから分離した。
main()とsimulate_policy()の双方が使うnormalize_cost / policy_score_legacy /
policy_score / auto_selectの実装本体を置く。数値式・選択順序・RNG消費順序は
移動前から変更していない。

game.py固有の定数・資源ルール・乱数選択はPolicyDependenciesを介して受け取る。
このモジュールはgame.py・engine.py・event_store.py・projection.pyに依存せず、
ファイルI/O・print・LLM・argparse・simulation/mainのループを持たない。
"""
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class PolicyDependencies:
    """方針評価をgame.pyから独立させるための動的依存。WorldStateではない。"""
    risk_reference_loss: float
    shadow_price: dict
    social_future_cost_weight: float
    price_index_fn: Callable[[int], float]
    scale_toward_bound_fn: Callable[[float, float, float], float]
    resource_cap_fn: Callable[[str, dict], float]
    is_affordable_fn: Callable[[dict, dict, float], bool]
    policy_vectors: dict
    random_choice_fn: Callable[[list], int]


def normalize_cost(cost: dict) -> dict:
    """コストベクトルをL1正規化する(Σ|v| = 1)。【2026-08-13時点で不使用、
    旧方式(policy_score_legacy)専用として残す。時間換算方式では出番が無い
    (時間換算という共通尺度がすでに大きさの情報を保持しているため)】"""
    total = sum(abs(v) for v in cost.values())
    if total == 0:
        return {}
    return {k: v / total for k, v in cost.items()}


def policy_score_legacy(choice: dict, vec: dict, *,
                        dependencies: PolicyDependencies) -> float:
    """方針ベクトルと選択肢の合致度、旧方式(L1正規化+risk_aversion)。
    比較用に残す。2026-08-13時点では policy_score() が既定。"""
    weights = vec["weights"]
    unit = normalize_cost(choice["cost"])
    dot = sum(weights.get(k, 0.0) * v for k, v in unit.items())
    loss = sum(-v for v in choice["cost"].values() if v < 0)
    return dot - vec["risk_aversion"] * (loss / dependencies.risk_reference_loss)


def policy_score(choice: dict, vec: dict, turn: int = 0, resources: dict = None, *,
                 dependencies: PolicyDependencies) -> float:
    """方針ベクトルと選択肢の合致度、時間換算版(2026-08-13、既定)。

    生のコストではなく「時間換算した影のコスト」に方針の重みを掛けて合計する。
    正規化(L1)もrisk_aversionも不要になる——時間換算という共通の物差しが
    最初から大きさの情報を保持しているため。値が大きいほどその方針に沿う
    (=時間換算コストが小さい)。

    money型のコストはprice_indexで名目上膨らむため、money軸だけ
    price_index(turn)で割り戻して実質化する。

    resourcesを渡した場合は、clamp_gainと同じ実効値(peaceは
    scale_toward_bound適用後)で評価する。渡さない場合は従来どおり名目のまま。

    contractキーを持つ選択肢には、将来の返済義務(repay_money)を実質化して
    money軸の影のコストとして前計上する。「今は安いが後で高くつく」性質を
    選択時点の評価へ反映するため。
    """
    weights = vec["weights"]
    idx = dependencies.price_index_fn(turn)
    total = 0.0
    for k, v in choice["cost"].items():
        if v < 0:
            eff_v = v
            if resources is not None and k == "peace":
                eff_v = dependencies.scale_toward_bound_fn(
                    v, resources.get(k, 0), dependencies.resource_cap_fn(k, {}))
            real_v = (-eff_v) / idx if k == "money" else (-eff_v)
            total += weights.get(k, 1.0) * dependencies.shadow_price.get(k, 1.0) * real_v
    if "contract" in choice:
        future_real = abs(choice["contract"]["repay_money"]) / idx
        total += (weights.get("money", 1.0)
                  * dependencies.shadow_price.get("money", 1.0)
                  * dependencies.social_future_cost_weight * future_real)
    # 物々交換・自給はresourcesとは別帳簿の財を回復する。choice生成時に
    # institutions.barterが実際の財deltaから計算した欠乏回避価値だけを控除し、
    # 選択器がコストだけを見て安全網を構造的に無視する状態を防ぐ。
    total -= choice.get("goods_survival_value", 0.0)
    return -total


def auto_select(choices: list, resources: dict, policy: str,
                policy_name: str = None, safety_floor: int = 0,
                turn: int = 0, budget: float = None, *,
                dependencies: PolicyDependencies) -> int:
    """自動プレイでの選択。balancedは払える選択の中から、選択後に
    いちばん資源が偏らないものを選ぶ。randomは払える選択から完全ランダム。

    policy_nameが指定された場合は2段階で選ぶ。
    1段目(生存制約): 払えないものを落とし、「選択後の最小資源 >=
    safety_floor」を満たすものだけを残す。1つも残らなければworst_afterが
    最大のものだけに絞る。
    2段目(選好): 1段目を通過した選択肢の中から方針スコア最大を選ぶ。
    つまり方針は「生き延びられる範囲の中でしか」効かない。
    """
    ok = [i for i, c in enumerate(choices)
          if dependencies.is_affordable_fn(c, resources, budget)]
    if not ok:
        # どれも払えない場合は最も傷が浅いものを選ぶ。
        return max(range(len(choices)),
                   key=lambda i: sum(v for v in choices[i]["cost"].values() if v < 0))
    if policy == "random":
        return dependencies.random_choice_fn(ok)

    def worst_after(i: int) -> int:
        after = dict(resources)
        for k, v in choices[i]["cost"].items():
            after[k] = after.get(k, 0) + v
        values = list(after.values())
        if "goods_safety_after" in choices[i]:
            values.append(choices[i]["goods_safety_after"])
        return min(values)

    if not policy_name or policy_name == "none":
        return max(ok, key=worst_after)

    vec = dependencies.policy_vectors[policy_name]
    best_worst = max(worst_after(i) for i in ok)
    threshold = min(safety_floor, best_worst)
    survivable = [i for i in ok if worst_after(i) >= threshold]
    return max(survivable, key=lambda i: policy_score(
        choices[i], vec, turn, resources, dependencies=dependencies))
