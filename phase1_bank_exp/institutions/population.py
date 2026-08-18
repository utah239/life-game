# -*- coding: utf-8 -*-
"""人口維持制度(制度6)の純粋ロジック。

人口は世界全体の単一値ではなく、``settlements`` マップ内の集落ごとの状態として
扱う。このモジュールは1集落の1か月分の出生・背景死亡・再生産人口・Stage遷移を
計画するだけで、game.py、イベントログ、LLM、乱数、他の制度モジュールには依存
しない。必需財との接続は、呼び出し側が既存の ``worst_shortfall`` (0〜100)を
渡す境界に限定する。

数値は人口制度を観察可能にするための初期較正値であり、最適値ではない。人口が
十分で生活基盤が健全なら緩やかに維持され、深い必需財不足が続けば出生が止まり
背景死亡が増える。整数人口で端数が消えないよう、期待出生・死亡の小数部分は集落
自身のcarryへ保存する。乱数を使わないため、同じ世界状態は常に同じ推移を返す。
"""
from __future__ import annotations


POPULATION_STAGE_MAINTAINED = 0
POPULATION_STAGE_DECLINING = 1
POPULATION_STAGE_NON_REPRODUCTIVE = 2
POPULATION_STAGE_ABANDONED = 3
POPULATION_STAGE_EXTINCT = 4

POPULATION_STAGE_NAMES = {
    POPULATION_STAGE_MAINTAINED: "人口維持",
    POPULATION_STAGE_DECLINING: "人口減少",
    POPULATION_STAGE_NON_REPRODUCTIVE: "再生産不能",
    POPULATION_STAGE_ABANDONED: "集落維持不能",
    POPULATION_STAGE_EXTINCT: "個体群絶滅",
}

POPULATION_STAGE_EVENT_NAMES = {
    POPULATION_STAGE_DECLINING: "settlement_population_declining",
    POPULATION_STAGE_NON_REPRODUCTIVE: "settlement_non_reproductive",
    POPULATION_STAGE_ABANDONED: "settlement_abandoned",
    POPULATION_STAGE_EXTINCT: "population_extinct",
}

POPULATION_SCOPE = "settlement"
HOME_SETTLEMENT_ID = "home"
HOME_SETTLEMENT_NAME = "はじまりの集落"

# 年齢構成は表示用の名前付き住民から推定せず、人口会計自身が整数で保持する。
# 15〜64歳を生産年齢とし、3区分の境界を越える人数は月次carryで失わない。
# 個人の正確な誕生月を全人口ぶん保存する方式より、数百万人規模でも状態量が
# 集落数に比例するため、デジタル水槽の集約表示と数値計算の双方に使える。
DEMOGRAPHY_VERSION = 1
AGE_COHORT_CHILDREN = "children"
AGE_COHORT_PRODUCTIVE = "productive"
AGE_COHORT_ELDERLY = "elderly"
AGE_COHORT_KEYS = (
    AGE_COHORT_CHILDREN, AGE_COHORT_PRODUCTIVE, AGE_COHORT_ELDERLY)
PRODUCTIVE_AGE_RANGE = (15, 64)
CHILD_COHORT_DURATION_MONTHS = PRODUCTIVE_AGE_RANGE[0] * 12
PRODUCTIVE_COHORT_DURATION_MONTHS = (
    PRODUCTIVE_AGE_RANGE[1] - PRODUCTIVE_AGE_RANGE[0] + 1) * 12
INITIAL_PRODUCTIVE_SHARE = 0.62
INITIAL_CHILD_SHARE = 0.22

# 初期集落。現在のオフライン水槽は1集落から開始するが、状態表現を最初から
# mapにすることで、移住・分村・他集落追加を単一値スキーマの破壊なしに行える。
POPULATION_INITIAL = 120
REPRODUCTIVE_POPULATION_INITIAL = 42

# 1か月当たりの期待率。健全時は出生が背景死亡をわずかに上回る。
MONTHLY_BIRTH_RATE = 0.007
MONTHLY_BASE_DEATH_RATE = 0.002
MONTHLY_SHORTAGE_DEATH_RATE = 0.025
MONTHLY_HEALTH_DEATH_RATE = 0.004
REPRODUCTIVE_SHARE = 0.35

# Stage境界。Stage1は期待純増減、Stage2/3は人数そのものを判定材料にする。
# Stage4はpopulation==0で不可逆。Stage1〜3は移住等の将来入力で回復できる。
POPULATION_DECLINE_ENTER = -0.05
POPULATION_DECLINE_EXIT = 0.05
REPRODUCTIVE_POPULATION_ENTER = 18
REPRODUCTIVE_POPULATION_EXIT = 24
ABANDONED_POPULATION_ENTER = 12
ABANDONED_POPULATION_EXIT = 16


def initial_age_cohorts(population: int,
                        reproductive_population: int = 0) -> dict:
    """人口合計を保存する初期3年齢区分を返す。

    既存の``reproductive_population``は生活基盤を含む実効的な再生産人口で
    あり、新しい生産年齢人口とは同義ではない。ただし再生産人口全員が生産
    年齢区分へ収まるよう、初期値の下限として使う。
    """
    population = max(0, int(population))
    reproductive = max(0, min(population, int(reproductive_population)))
    productive = min(
        population, max(reproductive, round(population * INITIAL_PRODUCTIVE_SHARE)))
    remaining = population - productive
    children = min(remaining, round(population * INITIAL_CHILD_SHARE))
    return {
        AGE_COHORT_CHILDREN: children,
        AGE_COHORT_PRODUCTIVE: productive,
        AGE_COHORT_ELDERLY: remaining - children,
    }


def age_cohort_for_age(age_years: float) -> str:
    """年齢を人口会計の3区分へ写像する。"""
    age = max(0.0, float(age_years))
    if age < PRODUCTIVE_AGE_RANGE[0]:
        return AGE_COHORT_CHILDREN
    if age <= PRODUCTIVE_AGE_RANGE[1]:
        return AGE_COHORT_PRODUCTIVE
    return AGE_COHORT_ELDERLY


def upgrade_settlement_demography(settlement: dict) -> dict:
    """旧集落へ年齢会計を補い、既存人口を一人も増減させない。"""
    after = dict(settlement)
    population = max(0, int(after.get("population", 0)))
    reproductive = max(0, min(
        population, int(after.get("reproductive_population", 0))))
    raw = after.get("age_cohorts")
    if raw is None:
        cohorts = initial_age_cohorts(population, reproductive)
    else:
        cohorts = {
            key: max(0, int(raw.get(key, 0))) for key in AGE_COHORT_KEYS}
        if sum(cohorts.values()) != population:
            raise ValueError("age cohort total does not match settlement population")
    raw_carry = after.get("age_transition_carry", {})
    carry = {
        "children_to_productive": max(
            0.0, float(raw_carry.get("children_to_productive", 0.0))),
        "productive_to_elderly": max(
            0.0, float(raw_carry.get("productive_to_elderly", 0.0))),
    }
    after.update({
        "demography_version": DEMOGRAPHY_VERSION,
        "age_cohorts": cohorts,
        "age_transition_carry": carry,
        "productive_population": cohorts[AGE_COHORT_PRODUCTIVE],
    })
    return after


def _allocate_integer(total: int, capacities: dict[str, int]) -> dict[str, int]:
    """合計を容量比で整数配賦する。同率時はAGE_COHORT_KEYS順。"""
    total = max(0, min(int(total), sum(capacities.values())))
    if total == 0:
        return {key: 0 for key in AGE_COHORT_KEYS}
    denominator = sum(capacities.values())
    exact = {
        key: total * capacities[key] / denominator for key in AGE_COHORT_KEYS}
    allocated = {
        key: min(capacities[key], int(exact[key])) for key in AGE_COHORT_KEYS}
    remainder = total - sum(allocated.values())
    order = sorted(AGE_COHORT_KEYS, key=lambda key: (
        -(exact[key] - allocated[key]), AGE_COHORT_KEYS.index(key)))
    for key in order:
        if remainder <= 0:
            break
        if allocated[key] < capacities[key]:
            allocated[key] += 1
            remainder -= 1
    if remainder:
        raise RuntimeError("age cohort allocation failed")
    return allocated


def _plan_age_cohorts(settlement: dict, births: int,
                      deaths: int) -> tuple[dict, dict]:
    """既存人口を1か月加齢させ、出生・死亡後の区分とcarryを返す。"""
    before = upgrade_settlement_demography(settlement)
    cohorts = dict(before["age_cohorts"])
    carries = dict(before["age_transition_carry"])

    child_accumulator = (
        carries["children_to_productive"]
        + cohorts[AGE_COHORT_CHILDREN] / CHILD_COHORT_DURATION_MONTHS)
    productive_accumulator = (
        carries["productive_to_elderly"]
        + cohorts[AGE_COHORT_PRODUCTIVE]
        / PRODUCTIVE_COHORT_DURATION_MONTHS)
    children_to_productive = min(
        cohorts[AGE_COHORT_CHILDREN], int(child_accumulator))
    productive_to_elderly = min(
        cohorts[AGE_COHORT_PRODUCTIVE], int(productive_accumulator))
    cohorts[AGE_COHORT_CHILDREN] -= children_to_productive
    cohorts[AGE_COHORT_PRODUCTIVE] += (
        children_to_productive - productive_to_elderly)
    cohorts[AGE_COHORT_ELDERLY] += productive_to_elderly
    cohorts[AGE_COHORT_CHILDREN] += max(0, int(births))

    death_allocations = _allocate_integer(max(0, int(deaths)), cohorts)
    for key in AGE_COHORT_KEYS:
        cohorts[key] -= death_allocations[key]
    carries = {
        "children_to_productive": round(
            child_accumulator - children_to_productive, 12),
        "productive_to_elderly": round(
            productive_accumulator - productive_to_elderly, 12),
    }
    if not sum(cohorts.values()):
        carries = {key: 0.0 for key in carries}
    return cohorts, carries


def initial_settlement(settlement_id: str = HOME_SETTLEMENT_ID,
                       name: str = HOME_SETTLEMENT_NAME,
                       population: int = POPULATION_INITIAL,
                       reproductive_population: int =
                       REPRODUCTIVE_POPULATION_INITIAL) -> dict:
    """集落の初期状態を返す。引数はテスト・将来の複数集落生成用。"""
    population = max(0, int(population))
    reproductive_population = max(
        0, min(population, int(reproductive_population)))
    stage = (POPULATION_STAGE_EXTINCT if population == 0
             else POPULATION_STAGE_MAINTAINED)
    row = {
        "id": settlement_id,
        "name": name,
        "population": population,
        "reproductive_population": reproductive_population,
        "stage": stage,
        "birth_carry": 0.0,
        "death_carry": 0.0,
        "births_total": 0,
        "deaths_total": 0,
        "last_births": 0,
        "last_deaths": 0,
        "last_expected_net_change": 0.0,
    }
    return upgrade_settlement_demography(row)


def population_stage_next(current_stage: int, population: int,
                          reproductive_population: int,
                          expected_net_change: float) -> int:
    """1か月分の人口Stage遷移。

    悪化は深い状態へ即座に進み、回復はヒステリシスを満たしたとき1段階ずつ。
    人口ゼロ(Stage4)はその集落について不可逆であり、外部移住による再興は新しい
    集落インスタンスとして表現する。
    """
    if current_stage == POPULATION_STAGE_EXTINCT:
        return POPULATION_STAGE_EXTINCT
    if population <= 0:
        return POPULATION_STAGE_EXTINCT
    if (population < ABANDONED_POPULATION_ENTER
            and current_stage < POPULATION_STAGE_ABANDONED):
        return POPULATION_STAGE_ABANDONED
    if (reproductive_population < REPRODUCTIVE_POPULATION_ENTER
            and current_stage < POPULATION_STAGE_NON_REPRODUCTIVE):
        return POPULATION_STAGE_NON_REPRODUCTIVE
    if (expected_net_change < POPULATION_DECLINE_ENTER
            and current_stage < POPULATION_STAGE_DECLINING):
        return POPULATION_STAGE_DECLINING
    if current_stage == POPULATION_STAGE_ABANDONED:
        return (POPULATION_STAGE_NON_REPRODUCTIVE
                if population >= ABANDONED_POPULATION_EXIT
                else POPULATION_STAGE_ABANDONED)
    if current_stage == POPULATION_STAGE_NON_REPRODUCTIVE:
        return (POPULATION_STAGE_DECLINING
                if (population >= ABANDONED_POPULATION_EXIT
                    and reproductive_population >= REPRODUCTIVE_POPULATION_EXIT)
                else POPULATION_STAGE_NON_REPRODUCTIVE)
    if current_stage == POPULATION_STAGE_DECLINING:
        return (POPULATION_STAGE_MAINTAINED
                if expected_net_change >= POPULATION_DECLINE_EXIT
                else POPULATION_STAGE_DECLINING)
    return current_stage


def plan_population_turn(settlement: dict, worst_shortfall: float,
                         health: float) -> dict:
    """1集落の1か月分の人口変化を計画し、新しいdictと差分を返す。

    ``worst_shortfall`` は食料・医薬品・住居・道具・生産力のボトルネック不足度。
    平均化しないため、一つの必需財の枯渇を他の余剰で相殺しない。
    入力dictは変更しない。
    """
    before = upgrade_settlement_demography(settlement)
    population = max(0, int(before.get("population", 0)))
    reproductive = max(
        0, min(population, int(before.get("reproductive_population", 0))))
    current_stage = int(before.get("stage", POPULATION_STAGE_MAINTAINED))
    if current_stage == POPULATION_STAGE_EXTINCT or population == 0:
        after = dict(before, population=0, reproductive_population=0,
                     stage=POPULATION_STAGE_EXTINCT, last_births=0,
                     last_deaths=0, last_expected_net_change=0.0)
        after.update({
            "age_cohorts": {key: 0 for key in AGE_COHORT_KEYS},
            "age_transition_carry": {
                "children_to_productive": 0.0,
                "productive_to_elderly": 0.0,
            },
            "productive_population": 0,
        })
        return {
            "settlement": after, "births": 0, "deaths": 0,
            "expected_births": 0.0, "expected_deaths": 0.0,
            "expected_net_change": 0.0, "support": 0.0,
            "from_stage": current_stage,
            "to_stage": POPULATION_STAGE_EXTINCT,
            "transitioned": current_stage != POPULATION_STAGE_EXTINCT,
        }

    shortage = max(0.0, min(100.0, float(worst_shortfall))) / 100.0
    # focal characterのhealth=80は既存ゲームの通常初期値であり、集落全体が
    # 20%不健康という意味ではない。50以上は共同体の健康基盤を満たすとみなし、
    # 50未満の深刻な悪化だけを人口へ接続する。
    bounded_health = max(0.0, min(100.0, float(health)))
    health_stress = max(0.0, (50.0 - bounded_health) / 50.0)
    health_support = 1.0 - health_stress
    goods_support = 1.0 - shortage
    support = min(goods_support, health_support)

    expected_births = reproductive * MONTHLY_BIRTH_RATE * support
    death_rate = (MONTHLY_BASE_DEATH_RATE
                  + MONTHLY_SHORTAGE_DEATH_RATE * shortage * shortage
                  + MONTHLY_HEALTH_DEATH_RATE * health_stress)
    expected_deaths = population * death_rate
    expected_net = expected_births - expected_deaths

    birth_accumulator = float(before.get("birth_carry", 0.0)) + expected_births
    death_accumulator = float(before.get("death_carry", 0.0)) + expected_deaths
    births = int(birth_accumulator)
    deaths = min(population + births, int(death_accumulator))
    population_after = max(0, population + births - deaths)

    # 再生産人口は総人口と生活基盤の双方に制約される。0.5の底を置くことで
    # 一時的な不足だけで即座に全員が再生産不能になる不連続を避ける。
    reproductive_factor = 0.5 + 0.5 * support
    reproductive_after = min(
        population_after,
        max(0, round(population_after * REPRODUCTIVE_SHARE
                     * reproductive_factor)))
    to_stage = population_stage_next(
        current_stage, population_after, reproductive_after, expected_net)
    age_cohorts, age_transition_carry = _plan_age_cohorts(
        before, births, deaths)
    if sum(age_cohorts.values()) != population_after:
        raise RuntimeError("age cohort population drift")
    after = dict(before)
    after.update({
        "population": population_after,
        "reproductive_population": reproductive_after,
        "stage": to_stage,
        "birth_carry": round(birth_accumulator - births, 12),
        "death_carry": round(death_accumulator - int(death_accumulator), 12),
        "births_total": int(before.get("births_total", 0)) + births,
        "deaths_total": int(before.get("deaths_total", 0)) + deaths,
        "last_births": births,
        "last_deaths": deaths,
        "last_expected_net_change": round(expected_net, 12),
        "age_cohorts": age_cohorts,
        "age_transition_carry": age_transition_carry,
        "productive_population": age_cohorts[AGE_COHORT_PRODUCTIVE],
    })
    return {
        "settlement": after,
        "births": births,
        "deaths": deaths,
        "expected_births": expected_births,
        "expected_deaths": expected_deaths,
        "expected_net_change": expected_net,
        "support": support,
        "from_stage": current_stage,
        "to_stage": to_stage,
        "transitioned": to_stage != current_stage,
    }


def apply_character_death(settlement: dict,
                          age_years: float | None = None) -> dict:
    """焦点人物1人の死亡を集落人口へ反映する。

    契約不履行や信用低下は発生させない。この関数は人口帳簿だけを変更し、入力
    dictは変更しない。背景死亡とは別に明示された1人なのでdeaths_totalへ加える。
    """
    before = upgrade_settlement_demography(settlement)
    population = max(0, int(before.get("population", 0)))
    population_after = max(0, population - 1)
    reproductive_after = min(
        population_after,
        max(0, int(before.get("reproductive_population", 0))))
    current_stage = int(before.get("stage", POPULATION_STAGE_MAINTAINED))
    to_stage = population_stage_next(
        current_stage, population_after, reproductive_after,
        float(before.get("last_expected_net_change", 0.0)))
    after = dict(before)
    cohorts = dict(before["age_cohorts"])
    preferred = (age_cohort_for_age(age_years)
                 if age_years is not None else AGE_COHORT_PRODUCTIVE)
    candidates = (preferred, AGE_COHORT_ELDERLY,
                  AGE_COHORT_PRODUCTIVE, AGE_COHORT_CHILDREN)
    removed = False
    for key in candidates:
        if population and cohorts.get(key, 0) > 0:
            cohorts[key] -= 1
            removed = True
            break
    if bool(population) != removed:
        raise RuntimeError("age cohort character death drift")
    after.update({
        "population": population_after,
        "reproductive_population": reproductive_after,
        "stage": to_stage,
        "deaths_total": int(before.get("deaths_total", 0)) + (1 if population else 0),
        "last_deaths": int(before.get("last_deaths", 0)) + (1 if population else 0),
        "age_cohorts": cohorts,
        "productive_population": cohorts[AGE_COHORT_PRODUCTIVE],
    })
    return after


def population_transition_event(stage: int) -> str | None:
    """悪化Stageに対応する仕様上のイベント名。Stage0復旧はNone。"""
    return POPULATION_STAGE_EVENT_NAMES.get(stage)


def world_is_extinct(settlements: dict) -> bool:
    """登録済み集落があり、その全てが人口ゼロなら世界人口絶滅。"""
    return bool(settlements) and all(
        int(settlement.get("population", 0)) <= 0
        for settlement in settlements.values())
