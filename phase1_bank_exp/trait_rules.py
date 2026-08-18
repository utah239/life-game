# -*- coding: utf-8 -*-
"""特性の成長・基底減衰・老化を扱う純粋ルール。

Step 5C(2026-08-14、behavior-preserving refactoring)でgame.pyから分離した。
イベント保存、世界状態の投影、乱数、LLM、CLIには依存しない。CLIから変更可能な
D_BASEを含む設定値はTraitRulesDependenciesを介して呼び出し時に受け取る。
"""
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class TraitRulesDependencies:
    """特性ルールをgame.pyの設定値から独立させるための動的依存。"""
    start_age: float
    years_per_turn: float
    health_decay_at_start: float
    health_decay_per_year: float
    talent_bonus: float
    growth_base: dict
    growable_traits: list
    health_care_gain: dict
    traits: list
    trait_min: float
    trait_max: float
    d_base: float


def age_at(turn: int, *, dependencies: TraitRulesDependencies) -> float:
    """ターン番号を開始年齢からの経過年齢へ変換する。"""
    return dependencies.start_age + turn * dependencies.years_per_turn


def health_decay(turn: int, *, dependencies: TraitRulesDependencies,
                 age_at_fn: Callable[[int], float] = None) -> float:
    """老化に連動する健康の基底減衰を線形に計算する。"""
    age = (age_at_fn(turn) if age_at_fn
           else age_at(turn, dependencies=dependencies))
    return (dependencies.health_decay_at_start
            + dependencies.health_decay_per_year
            * (age - dependencies.start_age))


def talent_multiplier(talent, trait, *,
                      dependencies: TraitRulesDependencies) -> float:
    """生まれ持った才能と成長対象が一致する場合の倍率を返す。"""
    return 1.0 + dependencies.talent_bonus if talent == trait else 1.0


def compute_trait_step(traits: dict, talent, theme_trait, choice_key: str,
                       turn: int, kind: str, *,
                       dependencies: TraitRulesDependencies,
                       talent_multiplier_fn: Callable = None,
                       health_decay_fn: Callable[[int], float] = None) -> tuple:
    """このターンの特性差分と、発火した成長情報を返す。

    kindは呼び出し側との互換のため保持するが判定には使わない。清算ターンでも
    theme_traitがあれば成長機会になる。健康は行動による回復と同じターンに
    老化の減衰を必ず受ける。戻す差分は上下限へクリップ済み。
    """
    delta = {}
    fired = None
    grown = None

    if theme_trait and choice_key in dependencies.growth_base:
        m = (talent_multiplier_fn(talent, theme_trait) if talent_multiplier_fn
             else talent_multiplier(
                 talent, theme_trait, dependencies=dependencies))
        if theme_trait in dependencies.growable_traits:
            gain = (dependencies.growth_base[choice_key] * m
                    * (1.0 - traits[theme_trait] / 100.0))
        else:
            gain = (dependencies.health_care_gain.get(choice_key, 0.0) * m
                    * (1.0 - traits["health"] / 100.0))
        if gain > 0:
            delta[theme_trait] = gain
            grown = theme_trait
            fired = {
                "trait": theme_trait,
                "via": choice_key,
                "talent": m > 1.0,
            }

    for trait_name in dependencies.traits:
        if trait_name == "health":
            decay = (health_decay_fn(turn) if health_decay_fn
                     else health_decay(turn, dependencies=dependencies))
            delta[trait_name] = delta.get(trait_name, 0.0) - decay
        elif trait_name != grown:
            delta[trait_name] = delta.get(trait_name, 0.0) - dependencies.d_base

    out = {}
    for trait_name, value in delta.items():
        current = traits[trait_name]
        new = max(
            dependencies.trait_min,
            min(dependencies.trait_max, current + value))
        applied = round(new - current, 6)
        if applied:
            out[trait_name] = applied
    return out, fired
