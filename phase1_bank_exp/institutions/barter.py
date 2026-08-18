# -*- coding: utf-8 -*-
"""物々交換・自給制度(制度5)の純粋ロジック(財の会計・production_capacity・状態機械)。

2026-08-15、`docs/social-regimes-spec.md`「5. 物々交換・自給」節の最小縦断実装
(Step 13)。bank.py・currency.py・local_credit.py・contract_enforcement.pyと
同じ形の純粋モジュール——game.py・イベントログ(events.jsonl)・LLM・printに
依存しない。他のinstitutions/*.pyにも依存しない(「制度崩壊を無条件に連動
させない」という既存の社会レジーム仕様の方針と同じ)。

## 財の会計モデル(仕様の要求「単一のessential_goodsへ統合しない」への対応)

4種の財+1つの独立スカラーを別々に持つ:
- 在庫(ストック、消費されると減り、生産で補充される): food・medicine
- 耐久資本(ストックだが摩耗が非常に遅く、消費ではなく維持で減る): shelter・tools
- production_capacity: 上記いずれでもない独立スカラー(bank_trust等と同型、
  中立値への回帰式を持つ)。財そのものではなく「生産・修繕の効率」を表す。
- labor_service(フロー)はこのモジュールに状態を持たない——barter/subsistence
  行動1回分の効果計算の内部でのみ使われ、ターンを越えて保存しない
  (仕様「フロー: labor_service(ターンを越えて保存しない)」の実装。値を
  永続化する変数を意図的に置かない、という設計そのものが要求への対応)。

## Stage判定がボトルネック型であることについて

barter_stage_next()への入力はworst_shortfall()——4財それぞれの不足度
(0=十分、100=完全枯渇)のうち**最大値**であって平均や合計ではない。
「食料が十分でも医薬品が不足していれば、そちらがボトルネックのまま
Stageを決める」という仕様の要求(「食料過剰で医薬品不足を帳消しにしない」)を、
maxを取るという1点で満たす——平均・合計を経由すると必ず相殺が起きるため、
このモジュールのどの関数も4財のスコアを合算しない。

## 上位制度との連動について

このモジュール自身はbank_stage/currency_stage/local_credit_stage/
enforcement_stageのいずれも参照しない(importしない)——barter_stageは
あくまで4財とproduction_capacityだけから決まる、独立した状態機械。
「物々交換・自給がいつ通常行動の選択肢として現れるか」という上位制度との
接続判定(alternative_economy_triggered相当)は、他の全制度Stageを既に
importしているengine.py側の責務とする(institutions配下では制度間の
横断参照を持たない、という既存の設計原則を保つため)。
"""

# --- Stage定数 ---------------------------------------------------------------
BARTER_STAGE_FUNCTIONING = 0        # 物々交換が機能
BARTER_STAGE_THINNED = 1            # 市場縮小
BARTER_STAGE_SUBSISTENCE_ONLY = 2   # 自給のみ
BARTER_STAGE_SHORTAGE = 3           # 必需財不足
BARTER_STAGE_NAMES = {
    BARTER_STAGE_FUNCTIONING: "機能", BARTER_STAGE_THINNED: "市場縮小",
    BARTER_STAGE_SUBSISTENCE_ONLY: "自給のみ", BARTER_STAGE_SHORTAGE: "必需財不足",
}

# 2026-08-15追加(Step 13)。institution_transitionイベントのtrigger欄に積む
# 固有名(仕様「barter_market_thinned / subsistence_only /
# essential_goods_shortageをStage遷移時に発行する」の実装)。他制度の
# triggerが「〜の閾値」という説明文なのに対し、barterはこの3つの固有名を
# to_stageから直接引く——Stage0(健全)への復帰だけは対応する固有名が
# 仕様に無いため、他制度と同じ説明文にする。
BARTER_STAGE_TRIGGER_NAMES = {
    BARTER_STAGE_FUNCTIONING: "worst_shortfallの閾値",
    BARTER_STAGE_THINNED: "barter_market_thinned",
    BARTER_STAGE_SUBSISTENCE_ONLY: "subsistence_only",
    BARTER_STAGE_SHORTAGE: "essential_goods_shortage",
}

# local_creditの"community"と同じ扱い(中央機関ではなく地域内で完結する制度)。
BARTER_SCOPE = "community"

# --- 財の初期値・上限 ---------------------------------------------------------
# 2026-08-17: 制度レジーム中心の再評価で選抜した候補#81851を、当面の
# 既定値として採用。生存率・寿命は選抜得点に含めず、制度経路の多様性、
# scenario応答、代替経済の実利用、復旧、遷移品質を基準に選んだ値である。
FOOD_STOCK_INITIAL = 90.0
MEDICINE_STOCK_INITIAL = 92.0
SHELTER_DURABILITY_INITIAL = 1.0
TOOLS_DURABILITY_INITIAL = 21.0
PRODUCTION_CAPACITY_INITIAL = 21.0
PRODUCTION_CAPACITY_CAP = 100.0
GOODS_CAP = 100.0  # food/medicine/shelter/toolsに共通の上限(無制限な蓄積を防ぐ、仮値)

# 財の値は「基準的な共同体1個分」の物量である。活動クラスタが
# 分裂・合流したときに在庫だけを配賦すると、空間表現を変えただけで
# 不足率と消費量が変わってしまう。provisioning_scaleはその共同体に
# 帰属する生活基盤の規模で、財と一緒に加算保存する。一方、
# production_capacityは規模ではなく強度値なので、このscaleには含めない。
GOODS_ACCOUNTING_VERSION = 1
PROVISIONING_SCALE_INITIAL = 1.0

PRODUCTION_CAPACITY_REVERSION_RATE = 0.15
# 上位制度が縮退して代替経済が必要な間だけ、生産網の分断をproduction_capacity
# の低下として表す。財の消費そのものは常時続く一方、健全時は下記の背景生産が
# 消費・摩耗を相殺する。これにより「金融制度が壊れた瞬間に食料が存在し始める」
# のではなく、平時から存在する生活基盤の生産効率だけが制度崩壊の影響を受ける。
PRODUCTION_CAPACITY_DISRUPTION_PER_TURN = 0.3


def production_capacity_reversion(current: float) -> float:
    """1ターン分の、中間値(PRODUCTION_CAPACITY_INITIAL)への回帰量。
    enforcement_capacity_reversion等と同じ形。"""
    return round((current - PRODUCTION_CAPACITY_INITIAL) * PRODUCTION_CAPACITY_REVERSION_RATE, 6)


def normalize_provisioning_scale(value=None) -> float:
    """財会計規模を非負のfloatへ正規化する。

    旧データで省略された場合は従来の0〜100会計1個分と解釈する。
    0は生活基盤を持たないreserveの表現として許し、負値だけを拒否する。
    """
    if value is None:
        return PROVISIONING_SCALE_INITIAL
    scale = float(value)
    if scale < 0.0:
        raise ValueError("provisioning_scale must be non-negative")
    return scale


def goods_reference(good: str, provisioning_scale: float = 1.0, *,
                    demand_scale: float | None = None) -> float:
    """財別の不足判定基準を、共同体の需要規模へ拡張する。

    ``demand_scale``を省略した既存経路では、従来どおり生活基盤規模を需要規模
    とみなす。世界モードは人口から求めた需要を明示し、在庫上限・背景生産に
    使う``provisioning_scale``と分離する。
    """
    references = {
        "food": FOOD_SHORTFALL_REFERENCE,
        "medicine": MEDICINE_SHORTFALL_REFERENCE,
        "shelter": SHELTER_SHORTFALL_REFERENCE,
        "tools": TOOLS_SHORTFALL_REFERENCE,
    }
    if good not in references:
        raise KeyError(good)
    scale = (provisioning_scale if demand_scale is None else demand_scale)
    return float(references[good]) * normalize_provisioning_scale(scale)


def goods_capacity(provisioning_scale: float = 1.0) -> float:
    """共同体の財在庫上限。scale=1で従来のGOODS_CAPと一致。"""
    return float(GOODS_CAP) * normalize_provisioning_scale(
        provisioning_scale)


def goods_coverage(food: float, medicine: float, shelter: float, tools: float,
                   provisioning_scale: float = 1.0,
                   demand_scales_by_good: dict | None = None) -> dict:
    """各財の基準量に対する充足率(0〜100%)。

    過剰在庫は物量として保持するが、充足率は観察用に100%で
    上限を設ける。scale=0は必要量も0なので100%とする。
    """
    values = {
        "food": food, "medicine": medicine,
        "shelter": shelter, "tools": tools,
    }
    result = {}
    for good, value in values.items():
        reference = goods_reference(
            good, provisioning_scale,
            demand_scale=(demand_scales_by_good or {}).get(good))
        result[good] = round(
            100.0 if reference <= 0.0 else
            max(0.0, min(100.0, float(value) / reference * 100.0)), 6)
    return result


# --- 毎ターンの基礎消費・摩耗(選択に関係なく適用される、health_decayと同型) ----
# food/medicineは「在庫」、shelter/toolsは「耐久資本」という会計上の区別は
# 維持する。値は候補#81851の制度経路を再現する当面の較正値。
FOOD_UPKEEP_PER_TURN = 9.4
MEDICINE_UPKEEP_PER_TURN = 9.3
SHELTER_WEAR_PER_TURN = 3.95
TOOLS_WEAR_PER_TURN = 2.45


def barter_goods_upkeep() -> dict:
    """毎ターンの基礎消費・摩耗量(4財それぞれの負のdelta)。乱数を使わない
    固定値——呼び出し側が現在値に加算し、0.0でクランプする(このモジュール
    自身はクランプしない、bank_trust_reversion等と同じ「量だけを返す」設計)。"""
    return {
        "food_delta": -FOOD_UPKEEP_PER_TURN,
        "medicine_delta": -MEDICINE_UPKEEP_PER_TURN,
        "shelter_delta": -SHELTER_WEAR_PER_TURN,
        "tools_delta": -TOOLS_WEAR_PER_TURN,
    }


def background_goods_production(production_capacity: float) -> dict:
    """毎ターンの背景生産・保守。

    現在のPRODUCTION_CAPACITY_INITIALでは基礎消費・摩耗をちょうど相殺する。
    生産能力が下がれば在庫・耐久資本が純減し、上がれば備蓄と修繕が進む。
    production_capacityは[0, CAP]へクランプして倍率の上振れを制限する。
    """
    bounded_capacity = max(0.0, min(PRODUCTION_CAPACITY_CAP, production_capacity))
    factor = bounded_capacity / PRODUCTION_CAPACITY_INITIAL
    return {
        "food_delta": round(FOOD_UPKEEP_PER_TURN * factor, 6),
        "medicine_delta": round(MEDICINE_UPKEEP_PER_TURN * factor, 6),
        "shelter_delta": round(SHELTER_WEAR_PER_TURN * factor, 6),
        "tools_delta": round(TOOLS_WEAR_PER_TURN * factor, 6),
    }


# --- barter/subsistence行動1回分の、財への効果 --------------------------------
# productionはproduction_capacityで強度が変わる(capacity=50で基準倍率1.0、
# 0で0.5倍、100で1.5倍の線形)——production_capacityが0でも生産が完全に
# 止まらない設計(「自給ゼロ」は別途、財そのものの枯渇〈Stage3〉で表現する)。
# barterはfood/medicineのみ(仲介による融通、耐久資本の修理は含まない)。
# subsistenceはfood/medicineの補充に加え、shelter/toolsの補修とproduction_
# capacity自体の底上げも行う(「自給が独立した生産基盤を育てる」という仕様の
# 描写に対応)。両者の補充量の大小は固定の仕様にせず、較正値として独立に扱う。
BARTER_FOOD_GAIN_BASE = 8.0
BARTER_MEDICINE_GAIN_BASE = 1.5
SUBSISTENCE_FOOD_GAIN_BASE = 18.5
SUBSISTENCE_MEDICINE_GAIN_BASE = 17.0
SUBSISTENCE_SHELTER_REPAIR = 15.5
SUBSISTENCE_TOOLS_REPAIR = 12.5
SUBSISTENCE_PRODUCTION_CAPACITY_GAIN = 4.0


def barter_choice_effects(choice_key: str, production_capacity: float) -> dict:
    """barter/subsistence行動1回分の、財への効果(選択された場合だけ発生する、
    毎ターンのbarter_goods_upkeep()とは別枠で加算される)。choice_keyが
    "barter"/"subsistence"以外なら全て0.0(通常行動〈money/labor/social/rest〉
    は財に触れない、という既存の分離を保つ)。"""
    bounded_capacity = max(0.0, min(PRODUCTION_CAPACITY_CAP, production_capacity))
    factor = 0.5 + bounded_capacity / PRODUCTION_CAPACITY_CAP
    if choice_key == "barter":
        return {
            "food_delta": round(BARTER_FOOD_GAIN_BASE * factor, 6),
            "medicine_delta": round(BARTER_MEDICINE_GAIN_BASE * factor, 6),
            "shelter_delta": 0.0, "tools_delta": 0.0, "production_capacity_delta": 0.0,
        }
    if choice_key == "subsistence":
        return {
            "food_delta": round(SUBSISTENCE_FOOD_GAIN_BASE * factor, 6),
            "medicine_delta": round(SUBSISTENCE_MEDICINE_GAIN_BASE * factor, 6),
            "shelter_delta": SUBSISTENCE_SHELTER_REPAIR, "tools_delta": SUBSISTENCE_TOOLS_REPAIR,
            "production_capacity_delta": SUBSISTENCE_PRODUCTION_CAPACITY_GAIN,
        }
    return {"food_delta": 0.0, "medicine_delta": 0.0, "shelter_delta": 0.0,
           "tools_delta": 0.0, "production_capacity_delta": 0.0}


# --- 財ごとの不足度(ボトルネック判定の材料) -----------------------------------
# 仮値。各財の初期値を基準(100%充足)とした、不足の百分率(0=十分、
# 100=完全枯渇)——単位系の異なる4財(food/medicineの在庫量、shelter/toolsの
# 耐久度0-100)を同じ0-100スケールに正規化し、maxで比較できるようにする。
FOOD_SHORTFALL_REFERENCE = FOOD_STOCK_INITIAL
MEDICINE_SHORTFALL_REFERENCE = MEDICINE_STOCK_INITIAL
SHELTER_SHORTFALL_REFERENCE = SHELTER_DURABILITY_INITIAL
TOOLS_SHORTFALL_REFERENCE = TOOLS_DURABILITY_INITIAL


def _shortfall_pct(value: float, reference: float) -> float:
    return max(0.0, (reference - value) / reference * 100.0) if reference else 0.0


def food_shortfall(food: float, provisioning_scale: float = 1.0, *,
                   demand_scale: float | None = None) -> float:
    return _shortfall_pct(
        food, goods_reference(
            "food", provisioning_scale, demand_scale=demand_scale))


def medicine_shortfall(medicine: float,
                       provisioning_scale: float = 1.0, *,
                       demand_scale: float | None = None) -> float:
    return _shortfall_pct(
        medicine, goods_reference(
            "medicine", provisioning_scale, demand_scale=demand_scale))


def shelter_shortfall(shelter: float,
                      provisioning_scale: float = 1.0, *,
                      demand_scale: float | None = None) -> float:
    return _shortfall_pct(
        shelter, goods_reference(
            "shelter", provisioning_scale, demand_scale=demand_scale))


def tools_shortfall(tools: float, provisioning_scale: float = 1.0, *,
                    demand_scale: float | None = None) -> float:
    return _shortfall_pct(
        tools, goods_reference(
            "tools", provisioning_scale, demand_scale=demand_scale))


def production_capacity_shortfall(production_capacity: float) -> float:
    return _shortfall_pct(production_capacity, PRODUCTION_CAPACITY_INITIAL)


def _shortfall_scores(food: float, medicine: float, shelter: float, tools: float,
                      production_capacity: float = None,
                      provisioning_scale: float = 1.0,
                      demand_scales_by_good: dict | None = None) -> dict:
    demand = demand_scales_by_good or {}
    scores = {
        "food": food_shortfall(
            food, provisioning_scale, demand_scale=demand.get("food")),
        "medicine": medicine_shortfall(
            medicine, provisioning_scale, demand_scale=demand.get("medicine")),
        "shelter": shelter_shortfall(
            shelter, provisioning_scale, demand_scale=demand.get("shelter")),
        "tools": tools_shortfall(
            tools, provisioning_scale, demand_scale=demand.get("tools")),
    }
    if production_capacity is not None:
        scores["production_capacity"] = production_capacity_shortfall(production_capacity)
    return scores


def worst_shortfall(food: float, medicine: float, shelter: float, tools: float,
                    production_capacity: float = None, *,
                    provisioning_scale: float = 1.0,
                    demand_scales_by_good: dict | None = None) -> tuple:
    """4財と生産能力の不足度のうち最悪のものを返す——相殺しない判定
    (このモジュールのdocstring「Stage判定がボトルネック型であることについて」
    参照)。production_capacityを省略した既存呼び出しでは4財だけを評価する
    後方互換を維持する。戻り値は(最悪の不足度, その項目名)。"""
    scores = _shortfall_scores(
        food, medicine, shelter, tools, production_capacity,
        provisioning_scale, demand_scales_by_good)
    worst_good = max(scores, key=scores.get)
    return scores[worst_good], worst_good


# 方針評価へ渡す、短期的な欠乏回避価値の換算係数。資源コストと同じ
# policy_score上の時間換算値として扱う。固定ボーナスではなく、現在の不足度と
# 実際に改善する各項目の割合から求めるため、財が十分なときは小さく、枯渇が
# 近いほど大きくなる。
BARTER_SURVIVAL_VALUE_SCALE = 18.75


def barter_choice_evaluation(choice_key: str, food: float, medicine: float,
                             shelter: float, tools: float,
                             production_capacity: float, *,
                             provisioning_scale: float = 1.0,
                             demand_scales_by_good: dict | None = None) -> dict:
    """1つの通常行動を、財の安全性という観点から評価する。

    barter/subsistence以外は財を変化させない。同じeffectsを実際の適用にも使える
    形で返すことで、方針評価だけが架空の便益を見る二重計上を防ぐ。
    """
    effects = barter_choice_effects(choice_key, production_capacity)
    before = _shortfall_scores(
        food, medicine, shelter, tools, production_capacity,
        provisioning_scale, demand_scales_by_good)
    capacity = goods_capacity(provisioning_scale)
    after_values = {
        "food": max(0.0, min(capacity, food + effects["food_delta"])),
        "medicine": max(0.0, min(capacity, medicine + effects["medicine_delta"])),
        "shelter": max(0.0, min(capacity, shelter + effects["shelter_delta"])),
        "tools": max(0.0, min(capacity, tools + effects["tools_delta"])),
        "production_capacity": max(
            0.0, min(PRODUCTION_CAPACITY_CAP,
                     production_capacity + effects["production_capacity_delta"])),
    }
    after = _shortfall_scores(
        after_values["food"], after_values["medicine"], after_values["shelter"],
        after_values["tools"], after_values["production_capacity"],
        provisioning_scale, demand_scales_by_good)
    avoided = sum(
        max(0.0, before[key] - after[key]) * (1.0 + 2.0 * before[key] / 100.0)
        for key in before)
    worst_after = max(after.values())
    return {
        "goods_effects": effects,
        "goods_safety_after": round(max(0.0, 100.0 - worst_after), 6),
        "goods_survival_value": round(avoided * BARTER_SURVIVAL_VALUE_SCALE, 6),
        "worst_shortfall_before": round(max(before.values()), 6),
        "worst_shortfall_after": round(worst_after, 6),
    }


# --- 物々交換・自給の状態機械(worst_shortfallを入力とする4段階、全段階可逆) ----
# ヒステリシス(悪化方向は即座、復旧方向はより厳しい条件〈enterより低いexit
# 閾値〉で1段階ずつ)。enforcement_stage_next等と同じ構造だが、
# worst_shortfallは「高いほど悪化」なので比較の向きが逆になる。
# 人口制度が未実装で不可逆を正当化できないため、他制度と同じ判断で全段階
# 可逆にする(仕様の「人口制度が未実装なので、不可逆化はまだ行わない」)。
BARTER_STAGE1_ENTER = 40.0
BARTER_STAGE1_EXIT = 13.0
BARTER_STAGE2_ENTER = 48.0
BARTER_STAGE2_EXIT = 17.0
BARTER_STAGE3_ENTER = 85.0
BARTER_STAGE3_EXIT = 63.0


def barter_stage_next(current_stage: int, worst_shortfall_score: float) -> int:
    """物々交換・自給の状態機械の1ターン分の遷移判定。worst_shortfall_scoreは
    worst_shortfall()の第1要素(0-100)。enforcement_stage_next/bank_stage_next
    と同じ構造(全段階可逆、悪化即座・復旧はヒステリシス)。"""
    if worst_shortfall_score >= BARTER_STAGE3_ENTER:
        return BARTER_STAGE_SHORTAGE
    if (worst_shortfall_score >= BARTER_STAGE2_ENTER
            and current_stage < BARTER_STAGE_SUBSISTENCE_ONLY):
        return BARTER_STAGE_SUBSISTENCE_ONLY
    if (worst_shortfall_score >= BARTER_STAGE1_ENTER
            and current_stage < BARTER_STAGE_THINNED):
        return BARTER_STAGE_THINNED
    if current_stage == BARTER_STAGE_SHORTAGE:
        return (BARTER_STAGE_SUBSISTENCE_ONLY if worst_shortfall_score <= BARTER_STAGE3_EXIT
               else BARTER_STAGE_SHORTAGE)
    if current_stage == BARTER_STAGE_SUBSISTENCE_ONLY:
        return (BARTER_STAGE_THINNED if worst_shortfall_score <= BARTER_STAGE2_EXIT
               else BARTER_STAGE_SUBSISTENCE_ONLY)
    if current_stage == BARTER_STAGE_THINNED:
        return (BARTER_STAGE_FUNCTIONING if worst_shortfall_score <= BARTER_STAGE1_EXIT
               else BARTER_STAGE_THINNED)
    return current_stage


# --- Stageごとの行動的帰結 ----------------------------------------------------
ESSENTIAL_GOODS_SHORTAGE_ENERGY_PENALTY = -48.0
ESSENTIAL_GOODS_SHORTAGE_HEALTH_PENALTY = -17.75


def essential_goods_shortage_penalty(stage: int) -> dict:
    """Stage3(必需財不足)のときだけ、energy/healthへ実害を与える
    (仕様「Stage3では必需財不足がenergy/healthへ実害を与える」の実装)。
    それ以外のStageでは空dict(何も減らさない)。"""
    if stage == BARTER_STAGE_SHORTAGE:
        return {"energy": ESSENTIAL_GOODS_SHORTAGE_ENERGY_PENALTY,
               "health": ESSENTIAL_GOODS_SHORTAGE_HEALTH_PENALTY}
    return {}


# --- 通常行動archetype(engine.available_normal_archetypesが条件付きで追加する) --
# 既存のACTION_ARCHETYPES(money/labor/social/rest)と同じ資源キー(energy/peace)
# だけを使う——新しい資源キーをINITIAL_RESOURCES/ACTION_ARCHETYPESへ追加すると
# affordability・clamp・policy_score・safety_floorへの影響確認が必要になる
# (Step 13の実装方針)ため、財(food/medicine/shelter/tools/production_capacity)
# はresourcesの外側(bank_trust等と同じ「別帳簿」)に置き、archetypeのcostは
# 既存資源のみで表現する——barter/subsistenceの財への効果はbarter_choice_
# effects()側で別途計算する(plan_settlement_effectsと同じ「costは資源、
# 効果は別帳簿」という既存の分離パターン)。
BARTER_TIME_COST_RANGE = (15, 35)          # 仮値
SUBSISTENCE_TIME_COST_RANGE = (30, 60)     # 仮値。自給はbarterより手間がかかる

BARTER_ARCHETYPE = {
    "key": "barter", "name": "物々交換で必要な物を融通する", "hours": BARTER_TIME_COST_RANGE,
    "ranges": {"energy": (-25, -10), "peace": (-10, -5)},
}
SUBSISTENCE_ARCHETYPE = {
    "key": "subsistence", "name": "自給自足で凌ぐ", "hours": SUBSISTENCE_TIME_COST_RANGE,
    "ranges": {"energy": (-35, -15), "peace": (-15, -5)},
}
