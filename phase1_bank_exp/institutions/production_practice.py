# -*- coding: utf-8 -*-
"""必需財生産の共同体経験と生産性を扱う純粋ルール。

``food``/``medicine``/``shelter``/``tools`` の在庫は分裂時に合計を
保存する外延量だが、生産経験は0〜100の強度値である。したがってこのモジュール
自身は経験を人口比で分割・加算しない。活動共同体の分裂では同じ実践を継承し、
合流では人口加重平均する境界を ``activity_communities.py`` が担当する。

当月の生産には月初時点の経験を使い、当月の実働による学習・無稼働による忘却は
翌月から効かせる。乱数、game.py、イベント、ファイルI/Oには依存しない。
"""
from __future__ import annotations

import math
from collections.abc import Iterable


PRODUCTION_PRACTICE_VERSION = 1
PRACTICE_GOODS = ("food", "medicine", "shelter", "tools")
PRODUCTION_PRACTICE_INITIAL = 0.0
PRODUCTION_PRACTICE_CAP = 100.0

# 必要人数が全員稼働し続けたとき、未習熟分が120か月で半減する。
PRACTICE_LEARNING_HALF_LIFE_MONTHS = 120
PRACTICE_LEARNING_RATE = (
    1.0 - 0.5 ** (1.0 / PRACTICE_LEARNING_HALF_LIFE_MONTHS))

# その財の担い手が一人もいないと、蓄積した実践が60か月で半減する。
PRACTICE_IDLE_DECAY_HALF_LIFE_MONTHS = 60
PRACTICE_IDLE_DECAY_RATE = (
    1.0 - 0.5 ** (1.0 / PRACTICE_IDLE_DECAY_HALF_LIFE_MONTHS))

# 経験100でも背景生産を最大20%だけ押し上げる。労働者0なら背景生産は
# labor factor側で0のままであり、知識だけが無人で生産することはない。
PRACTICE_MAX_PRODUCTIVITY_BONUS = 0.20


def _bounded_score(value: object) -> float:
    score = float(value)
    if not math.isfinite(score):
        raise ValueError("production practice score must be finite")
    return round(max(
        0.0, min(PRODUCTION_PRACTICE_CAP, score)), 6)


def initial_production_practice() -> dict[str, float]:
    """全財が未習熟の新しい共同体経験を返す。"""
    return {good: PRODUCTION_PRACTICE_INITIAL for good in PRACTICE_GOODS}


def normalize_production_practice(raw: dict | None) -> dict[str, float]:
    """旧checkpointや部分dictを完全な0〜100の財別状態へ正規化する。"""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise TypeError("production practice must be a dict")
    return {
        good: _bounded_score(raw.get(good, PRODUCTION_PRACTICE_INITIAL))
        for good in PRACTICE_GOODS
    }


def practice_productivity_factor(score: float) -> float:
    """経験値を背景生産倍率1.0〜1.2へ変換する。"""
    bounded = _bounded_score(score)
    return round(
        1.0 + PRACTICE_MAX_PRODUCTIVITY_BONUS
        * bounded / PRODUCTION_PRACTICE_CAP,
        6,
    )


def production_productivity_factors(
        practice_by_good: dict | None) -> dict[str, float]:
    """財別経験を財別生産倍率へ変換する。"""
    practice = normalize_production_practice(practice_by_good)
    return {
        good: practice_productivity_factor(practice[good])
        for good in PRACTICE_GOODS
    }


def population_weighted_production_practice(
        weighted_practices: Iterable[tuple[int | float, dict | None]],
        ) -> dict[str, float]:
    """複数共同体の強度値を、人口を重みとして世界値へ集約する。

    生産経験は外延量ではないため単純合計できない。人口0の土地在庫は世界の
    人的経験へ寄与させず、正の人口を持つ共同体だけを加重平均する。入力が空、
    または総人口0なら新規共同体と同じ初期値を返す。
    """
    total_population = 0.0
    totals = {good: 0.0 for good in PRACTICE_GOODS}
    for population, raw_practice in weighted_practices:
        weight = float(population)
        if not math.isfinite(weight):
            raise ValueError("production practice population must be finite")
        if weight <= 0.0:
            continue
        practice = normalize_production_practice(raw_practice)
        total_population += weight
        for good in PRACTICE_GOODS:
            totals[good] += weight * practice[good]
    if total_population <= 0.0:
        return initial_production_practice()
    return {
        good: _bounded_score(totals[good] / total_population)
        for good in PRACTICE_GOODS
    }


def plan_production_practice(
        practice_by_good: dict | None,
        required_worker_count_by_good: dict,
        active_worker_count_by_good: dict) -> dict:
    """当月実働から翌月の共同体経験を計画する。

    学習速度には ``active / required`` を掛けるため、人員不足時に完全稼働と
    同じ速さで経験が蓄積しない。一人以上が稼働していれば学習し、0人の財だけ
    忘却する。実働人数は必要人数を越えてはならない。
    """
    before = normalize_production_practice(practice_by_good)
    required = {
        good: max(0, int((required_worker_count_by_good or {}).get(good, 0)))
        for good in PRACTICE_GOODS
    }
    active = {
        good: max(0, int((active_worker_count_by_good or {}).get(good, 0)))
        for good in PRACTICE_GOODS
    }
    for good in PRACTICE_GOODS:
        if active[good] > required[good]:
            raise ValueError(
                f"active workers exceed required workers for {good}")

    after = {}
    coverage = {}
    for good in PRACTICE_GOODS:
        coverage[good] = round(
            active[good] / required[good], 6) if required[good] else 0.0
        current = before[good]
        if active[good] > 0:
            value = current + (
                PRODUCTION_PRACTICE_CAP - current
            ) * PRACTICE_LEARNING_RATE * coverage[good]
        else:
            value = current * (1.0 - PRACTICE_IDLE_DECAY_RATE)
        after[good] = _bounded_score(value)

    return {
        "version": PRODUCTION_PRACTICE_VERSION,
        "practice_before_by_good": before,
        "practice_after_by_good": after,
        "practice_delta_by_good": {
            good: round(after[good] - before[good], 6)
            for good in PRACTICE_GOODS
        },
        "labor_coverage_by_good": coverage,
        # 当月生産用。afterは翌月から使うため双方を明示する。
        "productivity_factors_by_good": production_productivity_factors(
            before),
        "productivity_factors_after_by_good": (
            production_productivity_factors(after)),
    }
