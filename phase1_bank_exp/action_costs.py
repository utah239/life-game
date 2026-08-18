# -*- coding: utf-8 -*-
"""LLM入力のコスト検証と、行動アーキタイプのコスト・所要時間抽選を扱う純粋ルール。

Step 5E(2026-08-14、behavior-preserving refactoring)でgame.pyから分離した。
イベント保存、世界状態の投影、LLM、CLIには依存しない。randomモジュールへも
暗黙依存しない——乱数はActionCostDependencies.randint_fn経由で受け取る
(呼び出し側=game.pyが呼び出し時点のrandom.randintを渡す)。CLIから変更可能な
価格水準(price_index)も、ActionCostDependencies.price_index_fn経由で
呼び出し時点の関数を受け取る。

このモジュールは game.py・event_store.py・projection.py・engine.py・
policy.py のいずれにも依存しない。ACTION_ARCHETYPES・REST_TIME_COST_RANGE等の
game.py固有の定数は、ActionCostDependencies経由でそのまま(tuple化・
deepcopy・再設計なし)受け取る。
"""
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ActionCostDependencies:
    """sanitize_cost()・draw_ranges()・indexed_money_ranges()・
    draw_archetype_cost()・draw_archetype_hours()・draw_prorated_rest()が
    game.py側の設定値・乱数・価格水準を参照するための、明示的な依存の
    受け渡し容器。WorldStateではない(制度スキーマの再設計ではなく、行動
    コスト生成ルールをgame.pyから独立させるための最小限の依存注入)。"""
    allowed_resources: set
    max_delta: int
    action_archetypes: list
    rest_time_cost_range: tuple
    randint_fn: Callable[[int, int], int]
    price_index_fn: Callable[[int], float]


def sanitize_cost(raw_cost: dict, *, dependencies: ActionCostDependencies) -> tuple:
    """LLMが返したcost辞書を検証する(Phase 0 から継承)。
    許可された資源名以外は捨て、値は±MAX_DELTAにクリップする。
    Phase 1 の通常フローではLLMがコストを書かないため出番は無いが、防御として残す。"""
    clean, rejected, clipped = {}, [], []
    for k, v in raw_cost.items():
        if k not in dependencies.allowed_resources:
            rejected.append(k)
            continue
        try:
            v = int(v)
        except (TypeError, ValueError):
            rejected.append(k)
            continue
        if v > dependencies.max_delta or v < -dependencies.max_delta:
            clipped.append(k)
            v = max(-dependencies.max_delta, min(dependencies.max_delta, v))
        if v != 0:
            clean[k] = v
    return clean, rejected, clipped


def draw_ranges(ranges: dict, *, dependencies: ActionCostDependencies) -> dict:
    return {res: dependencies.randint_fn(lo, hi) for res, (lo, hi) in ranges.items()}


def indexed_money_ranges(ranges: dict, turn: int, *,
                         dependencies: ActionCostDependencies) -> dict:
    """money(名目額)のレンジだけを価格水準に連動させる。他の資源(時間・体力等、
    貨幣建てでないもの)はそのまま。docs/plan.md「経済の閉じ方 v1実装設計」の
    cost_nominal(t) = cost_base × price_index(t)。"""
    idx = dependencies.price_index_fn(turn)
    out = {}
    for res, (lo, hi) in ranges.items():
        if res == "money":
            out[res] = (round(lo * idx), round(hi * idx))
        else:
            out[res] = (lo, hi)
    return out


def draw_archetype_cost(key: str, turn: int, *, dependencies: ActionCostDependencies,
                        draw_ranges_fn: Callable = None,
                        indexed_money_ranges_fn: Callable = None) -> dict:
    archetype = next(a for a in dependencies.action_archetypes if a["key"] == key)
    ranges = (indexed_money_ranges_fn(archetype["ranges"], turn) if indexed_money_ranges_fn
             else indexed_money_ranges(archetype["ranges"], turn, dependencies=dependencies))
    cost = (draw_ranges_fn(ranges) if draw_ranges_fn
           else draw_ranges(ranges, dependencies=dependencies))
    return {k: v for k, v in cost.items() if v != 0}


def draw_archetype_hours(key: str, *, dependencies: ActionCostDependencies) -> int:
    """2026-08-14追加(時間予算制の統合)。この行動にかかる時間(hours)を
    archetypeの"hours"レンジから抽選する。資源ではないのでprice_indexも
    MAX_DELTAクリップも通さない(draw_archetype_costとは別の軽い関数)。"""
    archetype = next(a for a in dependencies.action_archetypes if a["key"] == key)
    lo, hi = archetype["hours"]
    return dependencies.randint_fn(lo, hi)


def draw_prorated_rest(turn: int, hours: float, *, dependencies: ActionCostDependencies,
                       draw_archetype_cost_fn: Callable = None) -> dict:
    """月末に余った時間(hours)ぶんの休息効果。2026-08-14訂正(6回目opusレビュー
    指摘・高5): 従来は残り時間の多寡に関係なく休息のフルレンジをそのまま
    適用しており、1時間の端数でも4〜9時間分と同じ報酬になっていた
    (実測で約39%の月で発生)。休息archetypeの通常の所要時間
    (REST_TIME_COST_RANGEの中央値)を基準に、実際に使える時間で按分する。"""
    full = (draw_archetype_cost_fn("rest", turn) if draw_archetype_cost_fn
           else draw_archetype_cost("rest", turn, dependencies=dependencies))
    ref_hours = sum(dependencies.rest_time_cost_range) / 2
    factor = min(1.0, hours / ref_hours) if ref_hours else 1.0
    return {k: v * factor for k, v in full.items()}
