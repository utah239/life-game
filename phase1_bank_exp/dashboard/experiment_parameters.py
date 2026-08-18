# -*- coding: utf-8 -*-
"""ローカル実験コンソールで公開するパラメータの型・範囲・既定値。

全定数を無差別に公開せず、現在調整中の物々交換・自給制度とrun条件だけを
allowlist化する。適用はexperiment_workerの隔離プロセス内だけで行う。
"""
from dataclasses import asdict, dataclass
import math

from institutions import barter
from institutions.settlement_network import INITIAL_TOTAL_POPULATION


EXPERIMENT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ParameterSpec:
    key: str
    label: str
    group: str
    kind: str
    default: object
    minimum: float = None
    maximum: float = None
    step: float = None
    choices: tuple = ()
    target_attr: str = None
    description: str = ""

    def public_dict(self) -> dict:
        row = asdict(self)
        row.pop("target_attr")
        row["choices"] = list(self.choices)
        return row


def _number(key, label, group, default, minimum, maximum, step, target_attr=None,
            description="", integer=False):
    return ParameterSpec(
        key, label, group, "integer" if integer else "number", default,
        minimum, maximum, step, (), target_attr, description)


PARAMETER_SPECS = (
    ParameterSpec("policy", "方針", "run", "enum", "cautious", choices=(
        {"value": "cautious", "label": "慎重"},
        {"value": "ambitious", "label": "野心的"},
        {"value": "family", "label": "家族思い"},
    )),
    ParameterSpec("talent", "才能", "run", "enum", "random", choices=(
        {"value": "random", "label": "seedでランダム"},
        {"value": "dexterity", "label": "器用さ"},
        {"value": "intellect", "label": "思考力"},
        {"value": "skill", "label": "スキル"},
        {"value": "health", "label": "健康"},
    )),
    _number("seed", "seed", "run", 1, 0, 2147483647, 1, integer=True),
    _number("turns", "ターン数", "run", 1920, 1, 5000, 1, integer=True),
    _number("safety_floor", "安全下限", "run", 30, 0, 100, 1, integer=True),
    _number("bins", "選択グラフ区間数", "run", 60, 1, 240, 1, integer=True),

    _number(
        "initial_population", "初期総人口", "world",
        INITIAL_TOTAL_POPULATION, 1, 10_000_000, 1, integer=True,
        description=(
            "3集落へ既定比率で配賦。4096人を超える分は匿名人口層として保持し、"
            "縮小表示では同じpixelへ人数・色を集約します。")),

    _number("food_initial", "食料初期在庫", "initial", barter.FOOD_STOCK_INITIAL,
            1, 100, 1, "FOOD_STOCK_INITIAL"),
    _number("medicine_initial", "医薬品初期在庫", "initial", barter.MEDICINE_STOCK_INITIAL,
            1, 100, 1, "MEDICINE_STOCK_INITIAL"),
    _number("shelter_initial", "住居初期耐久", "initial", barter.SHELTER_DURABILITY_INITIAL,
            1, 100, 1, "SHELTER_DURABILITY_INITIAL"),
    _number("tools_initial", "道具初期耐久", "initial", barter.TOOLS_DURABILITY_INITIAL,
            1, 100, 1, "TOOLS_DURABILITY_INITIAL"),
    _number("production_initial", "生産能力初期値", "initial",
            barter.PRODUCTION_CAPACITY_INITIAL, 1, 100, 1, "PRODUCTION_CAPACITY_INITIAL"),

    _number("food_upkeep", "食料消費/turn", "upkeep", barter.FOOD_UPKEEP_PER_TURN,
            0, 20, 0.1, "FOOD_UPKEEP_PER_TURN"),
    _number("medicine_upkeep", "医薬品消費/turn", "upkeep",
            barter.MEDICINE_UPKEEP_PER_TURN, 0, 10, 0.1, "MEDICINE_UPKEEP_PER_TURN"),
    _number("shelter_wear", "住居摩耗/turn", "upkeep", barter.SHELTER_WEAR_PER_TURN,
            0, 5, 0.05, "SHELTER_WEAR_PER_TURN"),
    _number("tools_wear", "道具摩耗/turn", "upkeep", barter.TOOLS_WEAR_PER_TURN,
            0, 5, 0.05, "TOOLS_WEAR_PER_TURN"),
    _number("production_disruption", "制度縮退中の生産能力低下", "upkeep",
            barter.PRODUCTION_CAPACITY_DISRUPTION_PER_TURN, 0, 10, 0.1,
            "PRODUCTION_CAPACITY_DISRUPTION_PER_TURN"),
    _number("production_reversion", "生産能力の回帰率", "upkeep",
            barter.PRODUCTION_CAPACITY_REVERSION_RATE, 0, 1, 0.01,
            "PRODUCTION_CAPACITY_REVERSION_RATE"),

    _number("barter_food_gain", "物々交換: 食料", "actions", barter.BARTER_FOOD_GAIN_BASE,
            0, 50, 0.5, "BARTER_FOOD_GAIN_BASE"),
    _number("barter_medicine_gain", "物々交換: 医薬品", "actions",
            barter.BARTER_MEDICINE_GAIN_BASE, 0, 30, 0.5, "BARTER_MEDICINE_GAIN_BASE"),
    _number("subsistence_food_gain", "自給: 食料", "actions",
            barter.SUBSISTENCE_FOOD_GAIN_BASE, 0, 50, 0.5, "SUBSISTENCE_FOOD_GAIN_BASE"),
    _number("subsistence_medicine_gain", "自給: 医薬品", "actions",
            barter.SUBSISTENCE_MEDICINE_GAIN_BASE, 0, 30, 0.5,
            "SUBSISTENCE_MEDICINE_GAIN_BASE"),
    _number("subsistence_shelter_repair", "自給: 住居補修", "actions",
            barter.SUBSISTENCE_SHELTER_REPAIR, 0, 20, 0.5, "SUBSISTENCE_SHELTER_REPAIR"),
    _number("subsistence_tools_repair", "自給: 道具補修", "actions",
            barter.SUBSISTENCE_TOOLS_REPAIR, 0, 20, 0.5, "SUBSISTENCE_TOOLS_REPAIR"),
    _number("subsistence_production_gain", "自給: 生産能力", "actions",
            barter.SUBSISTENCE_PRODUCTION_CAPACITY_GAIN, 0, 20, 0.5,
            "SUBSISTENCE_PRODUCTION_CAPACITY_GAIN"),
    _number("survival_value_scale", "財改善の生存価値倍率", "actions",
            barter.BARTER_SURVIVAL_VALUE_SCALE, 0, 20, 0.25, "BARTER_SURVIVAL_VALUE_SCALE"),

    _number("stage1_exit", "Stage1復旧境界", "stages", barter.BARTER_STAGE1_EXIT,
            0, 100, 1, "BARTER_STAGE1_EXIT"),
    _number("stage1_enter", "Stage1悪化境界", "stages", barter.BARTER_STAGE1_ENTER,
            0, 100, 1, "BARTER_STAGE1_ENTER"),
    _number("stage2_exit", "Stage2復旧境界", "stages", barter.BARTER_STAGE2_EXIT,
            0, 100, 1, "BARTER_STAGE2_EXIT"),
    _number("stage2_enter", "Stage2悪化境界", "stages", barter.BARTER_STAGE2_ENTER,
            0, 100, 1, "BARTER_STAGE2_ENTER"),
    _number("stage3_exit", "Stage3復旧境界", "stages", barter.BARTER_STAGE3_EXIT,
            0, 100, 1, "BARTER_STAGE3_EXIT"),
    _number("stage3_enter", "Stage3悪化境界", "stages", barter.BARTER_STAGE3_ENTER,
            0, 100, 1, "BARTER_STAGE3_ENTER"),

    _number("shortage_energy_penalty", "不足時energyペナルティ", "penalties",
            barter.ESSENTIAL_GOODS_SHORTAGE_ENERGY_PENALTY, -50, 0, 0.5,
            "ESSENTIAL_GOODS_SHORTAGE_ENERGY_PENALTY"),
    _number("shortage_health_penalty", "不足時healthペナルティ", "penalties",
            barter.ESSENTIAL_GOODS_SHORTAGE_HEALTH_PENALTY, -20, 0, 0.25,
            "ESSENTIAL_GOODS_SHORTAGE_HEALTH_PENALTY"),
)

SPEC_BY_KEY = {spec.key: spec for spec in PARAMETER_SPECS}
GROUPS = (
    {"key": "run", "label": "実行条件"},
    {"key": "world", "label": "世界・人口"},
    {"key": "initial", "label": "初期状態"},
    {"key": "upkeep", "label": "消費・生産網"},
    {"key": "actions", "label": "物々交換・自給の効果"},
    {"key": "stages", "label": "Stage境界"},
    {"key": "penalties", "label": "不足ペナルティ"},
)


def parameter_schema() -> dict:
    return {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "groups": list(GROUPS),
        "parameters": [spec.public_dict() for spec in PARAMETER_SPECS],
    }


def default_values() -> dict:
    return {spec.key: spec.default for spec in PARAMETER_SPECS}


def _normalize_value(spec: ParameterSpec, raw):
    if spec.kind == "enum":
        allowed = {row["value"] for row in spec.choices}
        if raw not in allowed:
            raise ValueError(f"{spec.key}: expected one of {sorted(allowed)}")
        return raw
    if isinstance(raw, bool):
        raise ValueError(f"{spec.key}: boolean is not a number")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{spec.key}: expected a number") from None
    if not math.isfinite(value):
        raise ValueError(f"{spec.key}: expected a finite number")
    if spec.kind == "integer":
        if not value.is_integer():
            raise ValueError(f"{spec.key}: expected an integer")
        value = int(value)
    if spec.minimum is not None and value < spec.minimum:
        raise ValueError(f"{spec.key}: must be >= {spec.minimum}")
    if spec.maximum is not None and value > spec.maximum:
        raise ValueError(f"{spec.key}: must be <= {spec.maximum}")
    return value


def validate_request(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("request body must be an object")
    values = payload.get("values", payload)
    if not isinstance(values, dict):
        raise ValueError("values must be an object")
    unknown = sorted(set(values) - set(SPEC_BY_KEY))
    if unknown:
        raise ValueError(f"unknown parameters: {', '.join(unknown)}")
    normalized = {
        spec.key: _normalize_value(spec, values.get(spec.key, spec.default))
        for spec in PARAMETER_SPECS
    }
    exits = [normalized[f"stage{stage}_exit"] for stage in (1, 2, 3)]
    enters = [normalized[f"stage{stage}_enter"] for stage in (1, 2, 3)]
    exits_increase = all(left < right for left, right in zip(exits, exits[1:]))
    enters_increase = all(left < right for left, right in zip(enters, enters[1:]))
    each_has_hysteresis = all(exit_value < enter_value
                              for exit_value, enter_value in zip(exits, enters))
    if not (exits_increase and enters_increase and each_has_hysteresis):
        raise ValueError(
            "Stage境界は、復旧境界を stage1_exit < stage2_exit < stage3_exit、"
            "悪化境界を stage1_enter < stage2_enter < stage3_enterとし、"
            "各Stageで exit < enter にしてください")
    overrides = {
        spec.target_attr: normalized[spec.key]
        for spec in PARAMETER_SPECS if spec.target_attr is not None
    }
    return {
        "values": normalized,
        "run": {
            "seed": normalized["seed"], "policy": normalized["policy"],
            "turns": normalized["turns"], "safety_floor": normalized["safety_floor"],
            "talent": None if normalized["talent"] == "random" else normalized["talent"],
            "bins": normalized["bins"],
            "initial_population": normalized["initial_population"],
        },
        "barter_overrides": overrides,
    }


def apply_barter_overrides(barter_module, overrides: dict, *,
                           couple_shortfall_references: bool = True) -> None:
    """隔離worker内のinstitutions.barterへallowlist済みの値だけを適用する。"""
    allowed_attrs = {spec.target_attr for spec in PARAMETER_SPECS if spec.target_attr}
    unknown = sorted(set(overrides) - allowed_attrs)
    if unknown:
        raise ValueError(f"unknown barter attributes: {', '.join(unknown)}")
    for name, value in overrides.items():
        setattr(barter_module, name, value)
    reference_aliases = {
        "FOOD_STOCK_INITIAL": "FOOD_SHORTFALL_REFERENCE",
        "MEDICINE_STOCK_INITIAL": "MEDICINE_SHORTFALL_REFERENCE",
        "SHELTER_DURABILITY_INITIAL": "SHELTER_SHORTFALL_REFERENCE",
        "TOOLS_DURABILITY_INITIAL": "TOOLS_SHORTFALL_REFERENCE",
    }
    for source, target in reference_aliases.items():
        if couple_shortfall_references and source in overrides:
            setattr(barter_module, target, overrides[source])
