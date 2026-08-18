# -*- coding: utf-8 -*-
"""資源の減衰・回復・上限・支払い可能性を扱う純粋ルール。

Step 5B(2026-08-14、behavior-preserving refactoring)でgame.pyから分離した。
イベント保存、世界状態の投影、乱数、LLM、CLIには依存しない。game.py固有の
設定値はResourceRulesDependenciesを介して呼び出し時に受け取る。
"""
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ResourceRulesDependencies:
    """資源ルールをgame.pyの設定値から独立させるための動的依存。"""
    decay_rules: list
    regen_rules: dict
    initial_resources: dict
    initial_traits: dict
    flow_resources: set
    afford_floor: float


def compute_decay(turn: int, resources: dict, *,
                  dependencies: ResourceRulesDependencies) -> dict:
    """このターンに適用される維持コスト(エントロピー)を計算する。

    decay_rulesが空なら常に{}を返す安全なno-op。
    """
    delta = {}
    for rule in dependencies.decay_rules:
        if rule.get("kind") == "proportional":
            bal = resources.get(rule["resource"], 0)
            amt = -round(rule["rate"] * bal)
            if amt:
                delta[rule["resource"]] = delta.get(rule["resource"], 0) + amt
        else:
            if turn % rule["interval"] == 0:
                delta[rule["resource"]] = delta.get(rule["resource"], 0) + rule["amount"]
    return delta


def resource_cap(res: str, traits: dict, *,
                 dependencies: ResourceRulesDependencies) -> float:
    """フロー資源の上限。energyだけはhealth特性に連動する。"""
    if res == "energy":
        initial_health = dependencies.initial_traits["health"]
        health = traits.get("health", initial_health)
        return dependencies.initial_resources["energy"] * (health / initial_health)
    return dependencies.initial_resources[res]


def compute_regen(resources: dict, traits: dict, *,
                  dependencies: ResourceRulesDependencies,
                  resource_cap_fn: Callable[[str, dict], float] = None) -> dict:
    """フロー資源を上限までの余地の範囲で回復させる。"""
    delta = {}
    for res, amount in dependencies.regen_rules.items():
        cap = (resource_cap_fn(res, traits) if resource_cap_fn
               else resource_cap(res, traits, dependencies=dependencies))
        room = cap - resources.get(res, 0)
        gain = round(max(0, min(amount, room)))
        if gain:
            delta[res] = gain
    return delta


def scale_toward_bound(v: float, current: float, cap: float) -> float:
    """増減を、向かう先の境界に近いほど線形に鈍らせる。"""
    if cap <= 0:
        return v
    if v > 0:
        return v * max(0.0, 1.0 - current / cap)
    if v < 0:
        return v * max(0.0, current / cap)
    return 0.0


def clamp_gain(delta: dict, resources: dict, traits: dict, *,
               dependencies: ResourceRulesDependencies,
               resource_cap_fn: Callable[[str, dict], float] = None,
               scale_toward_bound_fn: Callable[[float, float, float], float] = None) -> dict:
    """境界逓減とフロー資源の上限を適用し、実際に加える差分を返す。"""
    out = {}
    for k, v in delta.items():
        if k == "peace":
            cap = (resource_cap_fn(k, traits) if resource_cap_fn
                   else resource_cap(k, traits, dependencies=dependencies))
            scale_fn = scale_toward_bound_fn or scale_toward_bound
            v = scale_fn(v, resources.get(k, 0), cap)
        if k in dependencies.flow_resources and v > 0:
            cap = (resource_cap_fn(k, traits) if resource_cap_fn
                   else resource_cap(k, traits, dependencies=dependencies))
            v = max(0, min(v, cap - resources.get(k, 0)))
        v = round(v)
        if v:
            out[k] = v
    return out


def is_affordable(choice: dict, resources: dict, budget: float = None, *,
                  dependencies: ResourceRulesDependencies,
                  resource_cap_fn: Callable[[str, dict], float] = None,
                  scale_toward_bound_fn: Callable[[float, float, float], float] = None) -> bool:
    """資源と時間予算から選択肢を支払えるか判定する。

    不履行は常に選択可能。既にフロア未満の資源は、それ以上の選択禁止状態へ
    固定しないためクロス判定の対象外とする。peaceは実効コストで判定する。
    """
    if choice.get("settle") == "defaulted":
        return True
    if budget is not None and choice.get("hours", 0) > budget:
        return False
    for k, v in choice["cost"].items():
        if v < 0:
            current = resources.get(k, 0)
            if k == "peace":
                cap = (resource_cap_fn(k, {}) if resource_cap_fn
                       else resource_cap(k, {}, dependencies=dependencies))
                scale_fn = scale_toward_bound_fn or scale_toward_bound
                eff_v = scale_fn(v, current, cap)
            else:
                eff_v = v
            if (current >= dependencies.afford_floor
                    and current + eff_v < dependencies.afford_floor):
                return False
    return True
