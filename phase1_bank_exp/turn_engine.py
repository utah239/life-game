# -*- coding: utf-8 -*-
"""銀行・通貨の信用回帰とstage遷移判定の「計画」を扱う純粋ルール。

Step 6B(2026-08-15、behavior-preserving refactoring)でgame.pyから分離した。
main()とsimulate_policy()の両方に重複していた「bank_trust/currency_confidence
を中間値へ回帰させ、その結果でbank_stage/currency_stageの遷移を判定する」
処理の計算部分だけを、RNG・イベント発行・状態更新を一切持たない純粋関数に
まとめる。イベントをどう積むか(main())、ローカル変数へどう反映するか
(simulate_policy())は、この関数の戻り値を使う呼び出し側の責務のまま残す。

イベント保存、世界状態の投影、乱数、LLM、CLIには依存しない。回帰式・stage
遷移判定式そのものは institutions/bank.py・institutions/currency.py に既に
あるので、ここでは呼ばない(呼び出し時点の関数オブジェクトをDependencies
経由で受け取って委譲するだけ)。

このモジュールは game.py・event_store.py・projection.py・engine.py・
policy.py のいずれにも依存しない。
"""
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class InstitutionUpkeepDependencies:
    """plan_confidence_reversion()・plan_institution_transitions()が game.py側の
    銀行・通貨の純粋関数(institutions/bank.py・institutions/currency.pyの
    再公開)を参照するための、明示的な依存の受け渡し容器。WorldStateではない
    (制度スキーマの再設計ではなく、この2関数をgame.pyから独立させるための
    最小限の依存注入)。"""
    bank_trust_reversion_fn: Callable[[float], float]
    currency_confidence_reversion_fn: Callable[[float], float]
    bank_stage_next_fn: Callable[[int, float, int], int]
    currency_stage_next_fn: Callable[[int, float], int]


def plan_confidence_reversion(bank_trust: float, currency_confidence: float, *,
                              dependencies: InstitutionUpkeepDependencies) -> dict:
    """1ターン分の、bank_trust・currency_confidenceそれぞれの中間値への回帰を
    計画する(イベントは積まない・ローカル変数も書き換えない)。現行の
    main()/simulate_policy()に書かれている式をそのまま踏襲する:
    - bank_trust_delta = -bank_trust_reversion_fn(bank_trust)
    - currency_confidence_delta = -currency_confidence_reversion_fn(currency_confidence)
    - bank_trust_afterにはクランプを掛けない(現行main()/simulate_policy()と
      同じく、bank_trustが負に振れても構わないという既存の扱いをそのまま
      維持する)
    - currency_confidence_afterにだけmax(0.0, ...)を適用する(現行実装の
      非対称性をそのまま維持する。通常運用ではbank_trust_reversion_fn/
      currency_confidence_reversion_fnのレート・値域では実際には発生しない
      が、関数の契約としてはこの非対称性を保つ)
    - 丸めは追加しない(bank_trust_reversion_fn/currency_confidence_reversion_fn
      が既にround済みの値を返す前提)"""
    deps = dependencies
    bank_trust_delta = -deps.bank_trust_reversion_fn(bank_trust)
    bank_trust_after = bank_trust + bank_trust_delta
    currency_confidence_delta = -deps.currency_confidence_reversion_fn(currency_confidence)
    currency_confidence_after = max(0.0, currency_confidence + currency_confidence_delta)
    return {
        "bank_trust_delta": bank_trust_delta,
        "bank_trust_after": bank_trust_after,
        "currency_confidence_delta": currency_confidence_delta,
        "currency_confidence_after": currency_confidence_after,
    }


def plan_institution_transitions(bank_stage: int, bank_trust: float, bank_crisis_count: int,
                                 currency_stage: int, currency_confidence: float, *,
                                 dependencies: InstitutionUpkeepDependencies) -> dict:
    """銀行・通貨の状態機械の1ターン分の遷移判定を計画する(イベントは積まない・
    ローカル変数も書き換えない)。現行のmain()/simulate_policy()と同じく、
    bank_stage_next_fn→currency_stage_next_fnの順で呼ぶ(この順序自体に副作用
    上の意味は無いが、既存の呼び出し順をそのまま踏襲する)。"""
    deps = dependencies
    bank_stage_after = deps.bank_stage_next_fn(bank_stage, bank_trust, bank_crisis_count)
    currency_stage_after = deps.currency_stage_next_fn(currency_stage, currency_confidence)
    return {
        "bank_stage_after": bank_stage_after,
        "currency_stage_after": currency_stage_after,
        "bank_transitioned": bank_stage_after != bank_stage,
        "currency_transitioned": currency_stage_after != currency_stage,
    }


@dataclass(frozen=True)
class LocalCreditUpkeepDependencies:
    """plan_local_credit_reversion()・plan_local_credit_transition()が game.py側の
    地域信用の純粋関数(institutions/local_credit.pyの再公開)を参照するための、
    明示的な依存の受け渡し容器。2026-08-15追加、地域信用制度の実装。既存の
    InstitutionUpkeepDependencies(銀行・通貨)は無改造のまま、別クラスとして
    追加する——institutions/local_credit.pyがbank.py/currency.pyのどちらにも
    依存しない独立モジュールであることと対応させ、制度ごとに小さいコンテキスト
    単位で追加できるようにする。"""
    community_trust_reversion_fn: Callable[[float], float]
    local_credit_stage_next_fn: Callable[[int, float], int]


def plan_local_credit_reversion(community_trust: float, *,
                                dependencies: LocalCreditUpkeepDependencies) -> dict:
    """1ターン分の、community_trustの中間値への回帰を計画する(イベントは
    積まない・ローカル変数も書き換えない)。plan_confidence_reversion()の
    currency_confidence側と同じ形:
    - community_trust_delta = -community_trust_reversion_fn(community_trust)
    - community_trust_afterにmax(0.0, ...)を適用する(currency_confidenceと
      同じ「confidence系」の値域として扱う)"""
    deps = dependencies
    community_trust_delta = -deps.community_trust_reversion_fn(community_trust)
    community_trust_after = max(0.0, community_trust + community_trust_delta)
    return {
        "community_trust_delta": community_trust_delta,
        "community_trust_after": community_trust_after,
    }


def plan_local_credit_transition(local_credit_stage: int, community_trust: float, *,
                                 dependencies: LocalCreditUpkeepDependencies) -> dict:
    """地域信用の状態機械の1ターン分の遷移判定を計画する(イベントは積まない・
    ローカル変数も書き換えない)。plan_institution_transitions()と同じ形。"""
    deps = dependencies
    local_credit_stage_after = deps.local_credit_stage_next_fn(local_credit_stage, community_trust)
    return {
        "local_credit_stage_after": local_credit_stage_after,
        "local_credit_transitioned": local_credit_stage_after != local_credit_stage,
    }


@dataclass(frozen=True)
class ContractEnforcementUpkeepDependencies:
    """plan_enforcement_reversion()・plan_enforcement_transition()が game.py側の
    契約執行の純粋関数(institutions/contract_enforcement.pyの再公開)を参照
    するための、明示的な依存の受け渡し容器。2026-08-15追加、契約執行制度の
    実装。LocalCreditUpkeepDependenciesと同じ理由(制度ごとに小さいコンテキスト
    単位で追加できるようにする)で別クラスとして追加する。"""
    enforcement_capacity_reversion_fn: Callable[[float], float]
    enforcement_stage_next_fn: Callable[[int, float], int]


def plan_enforcement_reversion(enforcement_capacity: float, *,
                               dependencies: ContractEnforcementUpkeepDependencies) -> dict:
    """1ターン分の、enforcement_capacityの中間値への回帰を計画する(イベントは
    積まない・ローカル変数も書き換えない)。plan_local_credit_reversion()と
    同じ形(community_trust_afterと同じくmax(0.0, ...)でクランプする)。"""
    deps = dependencies
    enforcement_capacity_delta = -deps.enforcement_capacity_reversion_fn(enforcement_capacity)
    enforcement_capacity_after = max(0.0, enforcement_capacity + enforcement_capacity_delta)
    return {
        "enforcement_capacity_delta": enforcement_capacity_delta,
        "enforcement_capacity_after": enforcement_capacity_after,
    }


def plan_enforcement_transition(enforcement_stage: int, enforcement_capacity: float, *,
                                dependencies: ContractEnforcementUpkeepDependencies) -> dict:
    """契約執行の状態機械の1ターン分の遷移判定を計画する(イベントは積まない・
    ローカル変数も書き換えない)。plan_local_credit_transition()と同じ形。"""
    deps = dependencies
    enforcement_stage_after = deps.enforcement_stage_next_fn(enforcement_stage, enforcement_capacity)
    return {
        "enforcement_stage_after": enforcement_stage_after,
        "enforcement_transitioned": enforcement_stage_after != enforcement_stage,
    }


@dataclass(frozen=True)
class BarterUpkeepDependencies:
    """plan_barter_upkeep()・plan_barter_transition()が game.py側の物々交換・
    自給の純粋関数(institutions/barter.pyの再公開)を参照するための、明示的な
    依存の受け渡し容器。2026-08-15追加(Step 13)。他の*UpkeepDependenciesと
    同じ理由で別クラスとして追加する。他制度と違い、財(food/medicine/shelter/
    tools)は単一スカラーではなく4つ独立に持つため、reversion系1関数では
    表現できない——upkeep(基礎消費・背景生産+production_capacity変化)を1つの
    関数にまとめる。各capなどのplain値は
    このモジュールがinstitutions/*.pyを直接importしない方針を保つための
    受け渡し(PolicySimulationDependencies等、既存Dependenciesもplain値と
    Callableを混在させている)。"""
    barter_goods_upkeep_fn: Callable[[], dict]
    background_goods_production_fn: Callable[[float], dict]
    production_capacity_reversion_fn: Callable[[float], float]
    barter_stage_next_fn: Callable[[int, float], int]
    goods_cap: float
    production_capacity_cap: float
    production_capacity_disruption_per_turn: float


def plan_barter_upkeep(food: float, medicine: float, shelter: float, tools: float,
                       production_capacity: float, disrupted: bool = False,
                       labor_factor: float = 1.0,
                       labor_factors_by_good: dict | None = None,
                       productivity_factors_by_good: dict | None = None,
                       provisioning_scale: float = 1.0,
                       demand_scales_by_good: dict | None = None, *,
                       dependencies: BarterUpkeepDependencies) -> dict:
    """1ターン分の、4財の基礎消費・背景生産とproduction_capacityを計画する。

    財の会計は制度状態にかかわらず常時動く。中立の生産能力では背景生産が
    基礎消費・摩耗を相殺する。ただし`labor_factor`で生産年齢人口による
    必需財部門の労働充足率を掛け、担い手が不足すればgross生産自体を減らす。
    ``labor_factors_by_good``があれば財別の充足率を優先し、総人数を保存した
    配賦が食料・医療・住居保守・道具保守へどう分かれたかを生産へ反映する。
    ``provisioning_scale``は在庫上限・背景生産を拡張し、人口から導出した
    ``demand_scales_by_good``は基礎消費・摩耗だけを財別に拡張する。需要を
    省略した既存経路は従来どおりprovisioning_scaleを使う。これにより空間
    分裂だけで世界総流量を変えず、人口移動・出生・死亡は需要側だけを変える。
    上位制度が縮退した`disrupted=True`の間だけ
    production_capacityへ追加の分断圧力を加える。財は
    [0.0, goods_cap * provisioning_scale]でクランプする。production_capacityは
    規模ではなく強度値なのでscaleを掛けず、明示的に
    [0.0, production_capacity_cap]へクランプする。"""
    deps = dependencies
    scale = max(0.0, float(provisioning_scale))
    demand_scales = {
        good: max(0.0, float((demand_scales_by_good or {}).get(good, scale)))
        for good in ("food", "medicine", "shelter", "tools")}
    upkeep = {
        key: round(float(value) * demand_scales[key.removesuffix("_delta")], 6)
        for key, value in deps.barter_goods_upkeep_fn().items()}
    raw_production = deps.background_goods_production_fn(production_capacity)
    bounded_labor_factor = max(0.0, min(1.0, float(labor_factor)))
    bounded_labor_factors_by_good = {
        key.removesuffix("_delta"): max(0.0, min(
            1.0, float((labor_factors_by_good or {}).get(
                key.removesuffix("_delta"), bounded_labor_factor))))
        for key in raw_production}
    bounded_productivity_factors_by_good = {
        key.removesuffix("_delta"): max(0.0, float(
            (productivity_factors_by_good or {}).get(
                key.removesuffix("_delta"), 1.0)))
        for key in raw_production}
    production = {
        key: round(
            float(value)
            * scale
            * bounded_labor_factors_by_good[key.removesuffix("_delta")]
            * bounded_productivity_factors_by_good[
                key.removesuffix("_delta")],
            6)
        for key, value in raw_production.items()
    }
    deltas = {
        key: round(upkeep[key] + production[key], 6)
        for key in ("food_delta", "medicine_delta", "shelter_delta", "tools_delta")
    }
    gross_consumption = {
        key.removesuffix("_delta"): round(max(0.0, -float(value)), 6)
        for key, value in upkeep.items()
        if key in ("food_delta", "medicine_delta", "shelter_delta", "tools_delta")
    }
    gross_production = {
        key.removesuffix("_delta"): round(max(0.0, float(value)), 6)
        for key, value in production.items()
        if key in ("food_delta", "medicine_delta", "shelter_delta", "tools_delta")
    }
    production_capacity_delta = round(
        -deps.production_capacity_reversion_fn(production_capacity)
        - (deps.production_capacity_disruption_per_turn if disrupted else 0.0), 6)
    capacity = float(deps.goods_cap) * scale
    return {
        # net在庫変化とは別にgross内訳を公開し、活動主体への帰属が
        # 「差し引きゼロなので生産もゼロ」と誤認しないようにする。
        "gross_consumption": gross_consumption,
        "gross_production": gross_production,
        "background_production_labor_factor": bounded_labor_factor,
        "background_production_labor_factors_by_good": (
            bounded_labor_factors_by_good),
        "background_production_productivity_factors_by_good": (
            bounded_productivity_factors_by_good),
        "provisioning_scale": scale,
        "demand_scales_by_good": demand_scales,
        "goods_capacity": capacity,
        "food_delta": deltas["food_delta"],
        "food_after": max(0.0, min(capacity, food + deltas["food_delta"])),
        "medicine_delta": deltas["medicine_delta"],
        "medicine_after": max(0.0, min(capacity, medicine + deltas["medicine_delta"])),
        "shelter_delta": deltas["shelter_delta"],
        "shelter_after": max(0.0, min(capacity, shelter + deltas["shelter_delta"])),
        "tools_delta": deltas["tools_delta"],
        "tools_after": max(0.0, min(capacity, tools + deltas["tools_delta"])),
        "production_capacity_delta": production_capacity_delta,
        "production_capacity_after": max(
            0.0, min(deps.production_capacity_cap,
                     production_capacity + production_capacity_delta)),
    }


def plan_barter_transition(barter_stage: int, worst_shortfall_score: float, *,
                           dependencies: BarterUpkeepDependencies) -> dict:
    """物々交換・自給の状態機械の1ターン分の遷移判定を計画する(イベントは
    積まない・ローカル変数も書き換えない)。plan_local_credit_transition()と
    同じ形——worst_shortfall_score(institutions.barter.worst_shortfall()の
    第1要素)は呼び出し側が直近の食料/医薬品/住居/道具の値から計算して渡す。"""
    deps = dependencies
    barter_stage_after = deps.barter_stage_next_fn(barter_stage, worst_shortfall_score)
    return {
        "barter_stage_after": barter_stage_after,
        "barter_transitioned": barter_stage_after != barter_stage,
    }
