# -*- coding: utf-8 -*-
"""「LLMを使わないオフライン検証・シミュレーション」という責務全体を扱う。

Step 9(2026-08-15、behavior-preserving refactoring)でgame.pyから一括で
移動した(今回も関数1個ずつではなく責務全体の粒度):
- 方針検証系: simulate_policy・check_trajectory_criteria・run_policy_check・
  run_policy_probe(PolicySimulationDependencies経由)
- 特性シミュレーション系: equilibrium・simulate_forced・simulate_mixed・
  run_simulation(TraitSimulationDependencies経由。equilibrium自体は
  game.py固有の依存を一切持たない純粋関数のためdependencies引数を持たない)

simulate_policyはevents.jsonlに一切触れない直接更新方式のまま(main()の
イベント方式とは統一しない、今回も踏襲)。random.seed/choice/randint/
uniform/random/choices・sys.exit・既存のルール関数(compute_regen等)・
表示関数(format_resources等)・定数(CHECKPOINT_TURNS等)はすべて
Dependencies経由で呼び出し時点の値を受け取る——game.py側のCLI上書き・
monkeypatchがそのまま伝播する。

このモジュールは game.py・event_store.py・projection.py・
scenario_generation.py・interactive_runtime.py のいずれにも依存しない。

2026-08-15(Step 12、`--policy-check`の制度レジーム対応再設計): 制度Stageの
定数(BANK_STAGE_COLLAPSED等)をinstitutions/*から直接importする——engine.py
が既にinstitutions.bank/currency/local_credit/contract_enforcementを直接
importしている先例と同じ扱い(下位レイヤーへの直接依存は許容されている)。
シミュレーション本体(simulate_policy)の数値・選択・RNG消費順序・制度遷移の
判定ロジックは一切変更しない——このStepで触るのは計測(institution_
trajectories)・合否基準(check_trajectory_criteria)・集計表示
(run_policy_check)だけ。
"""
import copy
import json
from dataclasses import dataclass
from typing import Callable

from institutions.bank import BANK_STAGE_COLLAPSED
from institutions.currency import CURRENCY_STAGE_ABANDONED
from institutions.local_credit import LOCAL_CREDIT_STAGE_ISOLATED
from institutions.contract_enforcement import ENFORCEMENT_STAGE_LOCAL_LEDGER
from institutions.barter import (
    BARTER_STAGE_FUNCTIONING,
    BARTER_STAGE_THINNED,
    BARTER_STAGE_SUBSISTENCE_ONLY,
    BARTER_STAGE_SHORTAGE,
    goods_capacity,
    goods_coverage,
    normalize_provisioning_scale,
)
from institutions.population import (
    HOME_SETTLEMENT_ID,
    POPULATION_STAGE_MAINTAINED,
    apply_character_death,
    initial_settlement,
    plan_population_turn,
    population_transition_event,
    upgrade_settlement_demography,
    world_is_extinct,
)
from institutions.npc_population import (
    mark_npc_died,
    npc_due_to_die,
    npc_life_schedule,
)
from institutions.settlement_network import (
    COMMUNITY_HEALTH_INITIAL,
    COMMUNITY_HEALTH_REVERSION_RATE,
    SETTLEMENT_NETWORK_VERSION,
    initial_settlement_network,
    plan_migration,
    select_focus_settlement,
    upgrade_single_settlement,
)
from institutions.intersettlement_trade import plan_intersettlement_trade
from institutions.household_needs import build_household_needs_state
from institutions.household_goods import (
    credit_household_action_goods,
    household_goods_coverage,
    initial_household_goods_state,
    plan_household_goods_lifecycle,
    plan_household_goods_provisioning,
    reconcile_household_goods_state,
    upgrade_household_goods_state,
    verify_household_goods_state,
)
from institutions.household_agency import (
    community_priority_pressure,
    household_priority_by_id,
    initial_household_agency_state,
    plan_household_agency,
    upgrade_household_agency_state,
    verify_household_agency_state,
)
from institutions.household_mobility import plan_household_migration
from institutions.resident_relationships import (
    household_relationship_support,
    initial_resident_relationship_state,
    plan_resident_relationships,
    upgrade_resident_relationship_state,
)
from institutions.activity_communities import (
    ACTIVITY_COMMUNITY_ACCOUNTING_VERSION,
    build_activity_community_ledger,
    promote_activity_communities,
)
from institutions.activity_economy import (
    background_production_labor_plan,
    initial_activity_economy_state,
    plan_activity_economy,
)
from institutions.production_practice import (
    normalize_production_practice,
    plan_production_practice,
    population_weighted_production_practice,
    production_productivity_factors,
)
from institutions.organizations import (
    ORGANIZATION_WORKFORCE_KINDS,
    active_organizations,
    initial_organization_state,
    plan_organization_effects,
    plan_organization_turn,
    reconcile_organization_state_asset_claims,
    record_organization_effects,
    upgrade_organization_state,
)
from institutions.residents import (
    RESIDENT_REGISTRY_VERSION,
    active_household_count,
    apply_migration as apply_resident_migration,
    apply_population_change as apply_resident_population_change,
    assign_focus as assign_resident_focus,
    initial_resident_registry,
    living_population_count,
    living_residents,
    mark_resident_died,
    materialize_cohort_resident,
    registry_matches_settlements,
    resident_age_years,
    select_focus_resident,
    upgrade_resident_registry,
)
from institutions.spatial import (
    SPATIAL_KEYFRAME_INTERVAL,
    build_spatial_keyframe,
    initial_spatial_state,
    spatial_state_matches_registry,
    relabel_spatial_accounts,
    synchronize_spatial_state,
)


VISUALIZE_TRACE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PolicySimulationDependencies:
    """simulate_policy()・check_trajectory_criteria()・run_policy_check()・
    run_policy_probe()が game.py 側の現在値(RNG・既存の共有ルール関数・
    表示関数・定数)を参照するための、明示的な依存の受け渡し容器。
    WorldStateではない(制度スキーマの再設計ではなく、方針検証をgame.pyから
    独立させるための最小限の依存注入)。呼び出しのたびに現在値を読むため、
    CLIやテストによる差し替えがそのまま反映される(import時点で固定しない)。"""
    # RNG
    seed_fn: Callable[[int], None]
    choice_fn: Callable[[list], object]
    randint_fn: Callable[[int, int], int]
    uniform_fn: Callable[[float, float], float]
    getstate_fn: Callable[[], object]
    setstate_fn: Callable[[object], None]
    # ターン開始処理・成長・価格水準(既存の共有関数、game.py側の薄いラッパー経由)
    compute_regen_fn: Callable[[dict, dict], dict]
    plan_confidence_reversion_fn: Callable[[float, float], dict]
    effective_npc_trust_fn: Callable[[dict, int], float]
    plan_institution_transitions_fn: Callable[..., dict]
    # 2026-08-15追加(地域信用制度)。bank/currencyと並行するcommunity_trustの
    # 回帰・stage遷移(institutions/local_credit.pyの再公開、turn_engine.py経由)。
    plan_local_credit_reversion_fn: Callable[[float], dict]
    plan_local_credit_transition_fn: Callable[[int, float], dict]
    # 2026-08-15追加(契約執行制度)。bank/currency/local_creditと並行する
    # enforcement_capacityの回帰・stage遷移(institutions/contract_enforcement.py
    # の再公開、turn_engine.py経由)。
    plan_enforcement_reversion_fn: Callable[[float], dict]
    plan_enforcement_transition_fn: Callable[[int, float], dict]
    # 2026-08-15追加(Step 13、物々交換・自給制度)。他制度と違い単一スカラーの
    # reversionではなく4財+production_capacityのupkeepをまとめて計画する
    # (institutions/barter.pyの再公開、turn_engine.py経由)。
    plan_barter_upkeep_fn: Callable[..., dict]
    plan_barter_transition_fn: Callable[[int, float], dict]
    worst_shortfall_fn: Callable[..., tuple]
    essential_goods_shortage_penalty_fn: Callable[[int], dict]
    barter_choice_effects_fn: Callable[[str, float], dict]
    alternative_economy_triggered_fn: Callable[[int, int, int, int], bool]
    compute_income_fn: Callable[[int, int], dict]
    compute_salary_fn: Callable[[int], int]
    compute_decay_fn: Callable[[int, dict], dict]
    compute_trait_step_fn: Callable[..., tuple]
    price_index_fn: Callable[[int], float]
    draw_prorated_rest_fn: Callable[[int, float], dict]
    # 清算・通常行動の選択肢生成・適用(既存の共有関数)
    build_settlement_choices_fn: Callable[..., list]
    plan_settlement_resolution_fn: Callable[..., dict]
    compute_normal_money_modifier_fn: Callable[..., float]
    available_normal_archetypes_fn: Callable[[int, int], list]
    build_normal_base_choice_fn: Callable[..., dict]
    pick_social_counterparty_fn: Callable[..., tuple]
    compute_social_contract_repay_range_fn: Callable[..., tuple]
    plan_normal_action_resolution_fn: Callable[..., dict]
    initial_trust_for_new_npc_fn: Callable[[dict, int], float]
    # 方針・可否判定・題材
    is_affordable_fn: Callable[..., bool]
    auto_select_fn: Callable[..., int]
    policy_score_fn: Callable[..., float]
    clamp_gain_fn: Callable[[dict, dict, dict], dict]
    pick_theme_fn: Callable[[list, list], tuple]
    # 表示・補助(run_policy_check/run_policy_probe/check_trajectory_criteria)
    age_at_fn: Callable[[int], float]
    format_resources_fn: Callable[[dict], str]
    format_traits_fn: Callable[[dict, str], str]
    exit_fn: Callable[[int], None]
    # 定数
    initial_resources: dict
    initial_traits: dict
    traits: list
    npc_pool_cap_range: tuple
    bank_trust_initial: float
    bank_stage_healthy: int
    currency_confidence_initial: float
    currency_stage_normal: int
    local_credit_trust_initial: float
    local_credit_stage_healthy: int
    enforcement_capacity_initial: float
    enforcement_stage_institutional: int
    # 2026-08-15追加(Step 13、物々交換・自給制度)。
    food_stock_initial: float
    medicine_stock_initial: float
    shelter_durability_initial: float
    tools_durability_initial: float
    production_capacity_initial: float
    production_capacity_cap: float
    barter_stage_functioning: int
    goods_cap: float
    checkpoint_turns: list
    contract_due_range: tuple
    theme_concern_traits: dict
    policy_vectors: dict
    g_base: dict
    trait_min: float
    trait_max: float
    turn_time_budget: float
    min_activity_hours: float
    npc_ethics_range: tuple
    npc_relationship_span_range: tuple
    growable_traits: list
    trait_ja: dict
    # 2026-08-15追加(自己レビュー指摘への対応): run_policy_check/
    # run_policy_probeがsimulate_policy/check_trajectory_criteriaを直接
    # (このモジュール自身のsibling関数として)呼んでいたため、game.py側で
    # game.simulate_policy等をmonkeypatchしても伝播しない回帰があった。
    # 他の*_fn引数と同じ差し替え口を追加する——game.py側のラッパーが
    # game.simulate_policy(呼び出し時点でmonkeypatchされうる関数オブジェクト)
    # をそのまま渡せるようにする(engine.plan_settlement_resolutionの
    # plan_settlement_effects_fnと同じパターン)。省略時(None)はこのモジュール
    # 自身のsimulate_policy/check_trajectory_criteriaを使う。
    simulate_policy_fn: Callable[..., dict] = None
    check_trajectory_criteria_fn: Callable[[dict], list] = None
    # 2026-08-15追加(Step 12): check_enforcement_convergence()も同じ理由
    # (monkeypatch伝播)で差し替え口を持つ。run_policy_checkがこのモジュール
    # 自身のsibling関数を直接呼ぶと、game.check_enforcement_convergenceを
    # monkeypatchしても伝播しない回帰になるため、他の*_fnと同じパターンにする。
    check_enforcement_convergence_fn: Callable[[list], dict] = None


@dataclass(frozen=True)
class TraitSimulationDependencies:
    """simulate_forced()・simulate_mixed()・run_simulation()が game.py 側の
    現在値(RNG・compute_trait_step・pick_theme・定数)を参照するための、
    明示的な依存の受け渡し容器。equilibrium()自体はgame.py固有の依存を一切
    持たない純粋関数だが、run_simulation()がそれをsibling呼び出しする際に
    monkeypatch伝播が必要なため、equilibrium_fnはこの容器に含める
    (equilibrium()を単体で呼ぶ側はこの容器を使わなくてよい)。"""
    seed_fn: Callable[[int], None]
    random_fn: Callable[[], float]
    choices_fn: Callable[..., list]
    compute_trait_step_fn: Callable[..., tuple]
    pick_theme_fn: Callable[[list, list], tuple]
    age_at_fn: Callable[[int], float]
    traits: list
    initial_traits: dict
    trait_min: float
    trait_max: float
    theme_concern_traits: dict
    trait_ja: dict
    d_base: float
    g_base: dict
    b_talent: float
    # 2026-08-15追加(自己レビュー指摘への対応): run_simulationが
    # equilibrium/simulate_forced/simulate_mixedを直接(このモジュール自身の
    # sibling関数として)呼んでいたため、game.py側でgame.equilibrium等を
    # monkeypatchしても伝播しない回帰があった。省略時(None)はこのモジュール
    # 自身のequilibrium/simulate_forced/simulate_mixedを使う。
    equilibrium_fn: Callable[[float, float, float, float], float] = None
    simulate_forced_fn: Callable[..., float] = None
    simulate_mixed_fn: Callable[..., dict] = None


# ==============================================================================
# 制度Stage計測(2026-08-15、Step 12「--policy-checkの制度レジーム対応再設計」
# で追加)。simulate_policy()が毎ターン評価しているbank_stage/currency_stage/
# local_credit_stage/enforcement_stageの値を読むだけの、乱数もシミュレーション
# ロジックも一切持たない純粋な計測用ヘルパー。simulate_policy()の既存の計算・
# 選択には影響しない(既存フィールドは無変更、新しいキーの追加のみ)。
# ==============================================================================

def _new_institution_tracking() -> dict:
    """1つの制度についての、Stage履歴計測の初期状態。全制度がStage0(健全)から
    始まる、という既存の初期値と対応させてfinal_stage=0から始める。"""
    return {
        "stage_turns": {},        # {stage: そのStageで過ごしたターン数}
        "first_reached_turn": {}, # {stage: そのStageに初めて到達したturn}
        "first_left_healthy_turn": None,  # Stage0を最初に離れたturn(無ければNone)
        "transition_count": 0,    # Stageが変化した回数(悪化・復旧いずれも)
        "recovery_count": 0,      # うち、Stageが下がった(改善した)回数
        "transition_history": [], # [{turn, from_stage, to_stage}, ...]
        "worst_stage": 0,         # それまでに到達した最悪のStage
        "final_stage": 0,         # 直近のStage(次回呼び出しのprevious_stageにもなる)
    }


def _record_institution_stage(tracking: dict, turn: int, stage: int) -> None:
    """1ターン分のStage計測を記録する(副作用のみ、戻り値なし)。乱数・
    シミュレーションの数値/選択ロジックには一切触れない——毎ターン、すでに
    確定した後のstage値を読んで計測用の辞書を更新するだけ。"""
    previous_stage = tracking["final_stage"]
    tracking["stage_turns"][stage] = tracking["stage_turns"].get(stage, 0) + 1
    if stage not in tracking["first_reached_turn"]:
        tracking["first_reached_turn"][stage] = turn
    if stage != 0 and tracking["first_left_healthy_turn"] is None:
        tracking["first_left_healthy_turn"] = turn
    if stage != previous_stage:
        tracking["transition_count"] += 1
        tracking["transition_history"].append({
            "turn": turn, "from_stage": previous_stage, "to_stage": stage})
        if stage < previous_stage:
            tracking["recovery_count"] += 1
    if stage > tracking["worst_stage"]:
        tracking["worst_stage"] = stage
    tracking["final_stage"] = stage


def _record_settlement_outcome(counts_by_stage: dict, stage: int, outcome: str) -> None:
    """契約執行のenforcement_stage別に、清算の履行/不履行件数を記録する
    (契約執行制度専用。他の3制度には対応する契約清算という概念が無いため、
    このヘルパーだけ別立てにする)。"""
    bucket = counts_by_stage.setdefault(stage, {"fulfilled": 0, "defaulted": 0})
    bucket[outcome] = bucket.get(outcome, 0) + 1


def _record_choice_by_stage(counts_by_stage: dict, stage: int, choice_key: str) -> None:
    """通常行動の選択件数を制度Stage別に記録する。選択済みの結果を読むだけで、
    選択・乱数・シミュレーション状態には影響しない。"""
    bucket = counts_by_stage.setdefault(stage, {})
    bucket[choice_key] = bucket.get(choice_key, 0) + 1


def _choice_counts_excluding_stage(counts_by_stage: dict, excluded_stage: int) -> dict:
    """選択肢が利用不能なStageを除き、通常行動件数を集約する。可逆制度では
    「一度でも崩壊したか」ではなく、実際に利用可能だった期間だけを評価する。"""
    totals = {}
    for stage, bucket in counts_by_stage.items():
        if stage == excluded_stage:
            continue
        for key, count in bucket.items():
            totals[key] = totals.get(key, 0) + count
    return totals


def _worst_stage_reached_by_turn(tracking: dict, checkpoint: int) -> int:
    """checkpointまでに初回到達済みの最悪Stageを返す。生涯worst_stageを
    早期チェックポイントの説明へ誤表示しないための表示・評価用ヘルパー。"""
    return max(
        (stage for stage, turn in tracking.get("first_reached_turn", {}).items()
         if turn <= checkpoint),
        default=0)


def _transition_chatter_metrics(tracking: dict, rapid_window_turns: int) -> dict:
    """制度遷移履歴から、寿命の長さと切り離した「閾値付近のばたつき」を計測する。

    rapid_same_boundary_recross_countは、直前の遷移をrapid_window_turns以内に
    同じStage境界で逆向きに横切った回数。総遷移回数や100ターン当たり遷移率も
    観測値として返すが、合否は直接的な再横断回数で判定する。
    """
    history = tracking.get("transition_history", [])
    direction_reversal_count = 0
    rapid_same_boundary_recross_count = 0
    for previous, current in zip(history, history[1:]):
        previous_direction = previous["to_stage"] - previous["from_stage"]
        current_direction = current["to_stage"] - current["from_stage"]
        reversed_direction = previous_direction * current_direction < 0
        if reversed_direction:
            direction_reversal_count += 1
        same_boundary = ({previous["from_stage"], previous["to_stage"]}
                         == {current["from_stage"], current["to_stage"]})
        if (reversed_direction and same_boundary
                and current["turn"] - previous["turn"] <= rapid_window_turns):
            rapid_same_boundary_recross_count += 1
    observed_turns = sum(tracking.get("stage_turns", {}).values())
    transition_count = len(history)
    return {
        "history_available": "transition_history" in tracking,
        "observed_turns": observed_turns,
        "transition_count": transition_count,
        "transition_rate_per_100_turns": (
            transition_count / observed_turns * 100 if observed_turns else 0.0),
        "direction_reversal_count": direction_reversal_count,
        "rapid_same_boundary_recross_count": rapid_same_boundary_recross_count,
    }


def _tuple_tree(value):
    """JSON往復でlistになったrandom.getstate()をsetstate()用tupleへ戻す。"""
    if isinstance(value, list):
        return tuple(_tuple_tree(item) for item in value)
    return value


def _integer_keyed(mapping: dict) -> dict:
    """JSON objectで文字列化された整数キーを戻す。"""
    return {int(key): value for key, value in mapping.items()}


def _restore_resume_state(raw: dict) -> dict:
    """world checkpointのJSON表現をsimulate_policy内部表現へ正規化する。"""
    state = copy.deepcopy(raw)
    state["random_state"] = _tuple_tree(state["random_state"])
    for name in (
            "bank_tracking", "currency_tracking", "local_credit_tracking",
            "enforcement_tracking", "barter_tracking", "population_tracking"):
        tracking = state.get(name)
        if tracking is None:
            continue
        tracking["stage_turns"] = _integer_keyed(tracking.get("stage_turns", {}))
        tracking["first_reached_turn"] = _integer_keyed(
            tracking.get("first_reached_turn", {}))
    for name in (
            "enforcement_settlement_counts_by_stage",
            "normal_choices_by_currency_stage",
            "normal_choices_by_local_credit_stage",
            "settlement_counts_by_checkpoint", "trajectory"):
        if name in state:
            state[name] = _integer_keyed(state[name])
    return state


def simulate_policy(policy_name: str, turns: int, seed: int, safety_floor: int,
                    talent: str = None, checkpoint_turns: list = None, *,
                    dependencies: PolicySimulationDependencies, trace: dict = None,
                    continue_world: bool = False,
                    resume_state: dict = None,
                    initial_population: int = None) -> dict:
    """1つの方針でゲームの数値部分だけを回す。events.jsonl には触らない。
    戻り値には選択分布・最終資源・特性・各ターンで3方針が一致したかの記録を含む。
    checkpoint_turns で指定したターンごとに主要な値のスナップショットを取り、
    "trajectory" として返す(反証可能な合否基準の材料。単一時点の終端値だけを
    見て「均衡した」と誤判定しないため)。

    2026-08-16追加(可視化ダッシュボード用のオプトイン計測): traceに
    {"turns": [], "settlements": [], "npc_introductions": []}を持つ辞書を渡すと、
    このリストへ副作用として詳細な記録を追加する(戻り値の辞書の形は一切変更
    しない)。trace=None(既定)なら追加のRNG消費・計算コストは発生しない
    ——既存のcheckpoint記録と完全に独立した経路で、既存の合否判定・実測結果
    には一切影響しない。turnsの各行は、そのターンの所得・行動・清算・成長を
    反映したturn_endスナップショット。ターン開始処理だけで死亡した場合も、
    死亡を反映した状態をそのターンのturn_endとして1件記録する。

    ``continue_world=True`` はデジタル水槽用の世界継続モード。個人死亡を
    シミュレーション終端にせず、未清算契約を信用判定外のorphanedへ移し、同じ
    集落の別の人物へ焦点を移す。制度・財・NPC・集落人口は継続し、全集落人口が
    0になった ``extinction`` だけが世界の終端になる。既定Falseはpolicy-checkと
    既存一人生涯モデルを完全に維持する。"""
    deps = dependencies
    checkpoint_turns = set(
        checkpoint_turns if checkpoint_turns is not None else deps.checkpoint_turns)
    if resume_state is not None:
        if not continue_world:
            raise ValueError("resume_state requires continue_world=True")
        state = _restore_resume_state(resume_state)
        if state.get("policy") != policy_name or int(state.get("seed")) != seed:
            raise ValueError("resume_state policy/seed does not match this run")
        if int(state.get("safety_floor")) != safety_floor:
            raise ValueError("resume_state safety_floor does not match this run")
        if state.get("world_extinct_turn") is not None:
            raise ValueError("an extinct world cannot be resumed")
        start_turn = int(state["completed_turn"]) + 1
        if turns < start_turn:
            raise ValueError("turns must be greater than resume_state completed_turn")
        deps.setstate_fn(state["random_state"])
        # checkpointは内部実装の全mutable値を持つ。deepcopy済みのstateから復元し、
        # 呼び出し元が保持するcheckpoint自体を以後の計算で変更しない。
        resources = state["resources"]
        traits = state["traits"]
        talent = state["talent"]
        npc_pool_cap = state["npc_pool_cap"]
        contracts, counts, npcs = state["contracts"], state["counts"], state["npcs"]
        contract_sequence = state.get("contract_sequence", len(contracts))
        npc_sequence = state.get(
            "npc_sequence",
            sum(1 for n in npcs.values() if n.get("role") == "acquaintance"))
        used_places, used_concerns = state["used_places"], state["used_concerns"]
        blocked, min_seen, agree_log = state["blocked"], state["min_seen"], state["agree_log"]
        overridden, fires = state["overridden"], state["fires"]
        bank_money, economy_money = state["bank_money"], state["economy_money"]
        bank_repaid_count = state["bank_repaid_count"]
        bank_credit_losses = state["bank_credit_losses"]
        bank_crisis_count, bank_trust, bank_stage = (
            state["bank_crisis_count"], state["bank_trust"], state["bank_stage"])
        currency_confidence, currency_stage = (
            state["currency_confidence"], state["currency_stage"])
        community_trust, local_credit_stage = (
            state["community_trust"], state["local_credit_stage"])
        enforcement_capacity, enforcement_stage = (
            state["enforcement_capacity"], state["enforcement_stage"])
        food, medicine, shelter, tools = (
            state["food"], state["medicine"], state["shelter"], state["tools"])
        production_capacity, barter_stage = (
            state["production_capacity"], state["barter_stage"])
        barter_active = state["barter_active"]
        barter_activated_turn = state["barter_activated_turn"]
        alternative_economy_available = state["alternative_economy_available"]
        barter_shortage_penalty_applied_count = state[
            "barter_shortage_penalty_applied_count"]
        goods_min_seen = state["goods_min_seen"]
        trajectory, death_turn = state["trajectory"], state["death_turn"]
        settlements, population_stage = state["settlements"], state["population_stage"]
        settlement_network_version = int(state.get("settlement_network_version", 0))
        focus_settlement_id = state.get("focus_settlement_id", HOME_SETTLEMENT_ID)
        migration_total = int(state.get("migration_total", 0))
        trade_volume_total = float(state.get("trade_volume_total", 0.0))
        trade_event_count = int(state.get("trade_event_count", 0))
        resident_registry = state.get("resident_registry")
        spatial_state = state.get("spatial_state")
        activity_community_ledger = state.get("activity_community_ledger")
        activity_community_accounting_version = int(state.get(
            "activity_community_accounting_version", 0))
        activity_economy_state = state.get(
            "activity_economy_state", initial_activity_economy_state())
        organization_state = upgrade_organization_state(state.get(
            "organization_state", initial_organization_state()))
        household_goods_state = upgrade_household_goods_state(state.get(
            "household_goods_state", initial_household_goods_state(
                int(state.get("completed_turn", 0)))))
        household_agency_state = upgrade_household_agency_state(state.get(
            "household_agency_state", initial_household_agency_state(
                int(state.get("completed_turn", 0)))))
        resident_relationship_state = upgrade_resident_relationship_state(
            state.get(
                "resident_relationship_state",
                initial_resident_relationship_state(
                    int(state.get("completed_turn", 0)))))
        focus_resident_id = state.get("focus_resident_id")
        population_tracking = state["population_tracking"]
        generation = state["generation"]
        generation_started_turn = state["generation_started_turn"]
        character_alive = state["character_alive"]
        character_death_count = state["character_death_count"]
        npc_death_count = state.get(
            "npc_death_count",
            sum(1 for n in npcs.values() if not n.get("alive", True)))
        world_extinct_turn = state["world_extinct_turn"]
        bank_tracking = state["bank_tracking"]
        currency_tracking = state["currency_tracking"]
        local_credit_tracking = state["local_credit_tracking"]
        enforcement_tracking = state["enforcement_tracking"]
        barter_tracking = state["barter_tracking"]
        enforcement_settlement_counts_by_stage = state[
            "enforcement_settlement_counts_by_stage"]
        normal_choices_by_currency_stage = state["normal_choices_by_currency_stage"]
        normal_choices_by_local_credit_stage = state[
            "normal_choices_by_local_credit_stage"]
        settlement_outcome_counts = state["settlement_outcome_counts"]
        settlement_counts_by_checkpoint = state["settlement_counts_by_checkpoint"]
    else:
        start_turn = 1
        deps.seed_fn(seed)
        resources = dict(deps.initial_resources)
        traits = dict(deps.initial_traits)
        talent = talent or deps.choice_fn(deps.traits)
        npc_pool_cap = deps.randint_fn(*deps.npc_pool_cap_range)
        contracts, counts, npcs = [], {}, {}
        contract_sequence = 0
        npc_sequence = 0
        used_places, used_concerns = [], []
        blocked, min_seen, agree_log = 0, dict(deps.initial_resources), []
        overridden = fires = 0
        bank_money = economy_money = bank_repaid_count = 0
        bank_credit_losses = 0.0
        bank_crisis_count = 0
        bank_trust, bank_stage = deps.bank_trust_initial, deps.bank_stage_healthy
        currency_confidence = deps.currency_confidence_initial
        currency_stage = deps.currency_stage_normal
        community_trust = deps.local_credit_trust_initial
        local_credit_stage = deps.local_credit_stage_healthy
        enforcement_capacity = deps.enforcement_capacity_initial
        enforcement_stage = deps.enforcement_stage_institutional
        food, medicine = deps.food_stock_initial, deps.medicine_stock_initial
        shelter, tools = deps.shelter_durability_initial, deps.tools_durability_initial
        production_capacity = deps.production_capacity_initial
        barter_stage = deps.barter_stage_functioning
        barter_active = False
        barter_activated_turn = None
        alternative_economy_available = False
        barter_shortage_penalty_applied_count = 0
        goods_min_seen = {
            "food": food, "medicine": medicine, "shelter": shelter,
            "tools": tools, "production_capacity": production_capacity}
        trajectory = {}
        death_turn = None
        settlements = (initial_settlement_network({
            "food": food, "medicine": medicine, "shelter": shelter,
            "tools": tools, "production_capacity": production_capacity,
            "barter_stage": barter_stage,
            "community_trust": community_trust,
            "local_credit_stage": local_credit_stage,
        }, total_population=initial_population) if continue_world else {})
        settlement_network_version = (
            SETTLEMENT_NETWORK_VERSION if continue_world else 0)
        focus_settlement_id = HOME_SETTLEMENT_ID
        migration_total = 0
        trade_volume_total = 0.0
        trade_event_count = 0
        resident_registry = (initial_resident_registry(
            settlements, seed, turn=1) if continue_world else None)
        spatial_state = None
        activity_community_ledger = None
        activity_community_accounting_version = 0
        activity_economy_state = initial_activity_economy_state()
        organization_state = initial_organization_state()
        household_goods_state = initial_household_goods_state()
        household_agency_state = initial_household_agency_state()
        resident_relationship_state = initial_resident_relationship_state()
        focus_resident_id = (select_focus_resident(
            resident_registry, focus_settlement_id, 1)
            if continue_world else None)
        if focus_resident_id is not None:
            resident_registry = assign_resident_focus(
                resident_registry, focus_resident_id, 1, 1, talent)
        population_stage = (POPULATION_STAGE_MAINTAINED
                            if continue_world else None)
        population_tracking = (_new_institution_tracking()
                               if continue_world else None)
        generation = 1
        generation_started_turn = 1
        character_alive = True
        character_death_count = 0
        npc_death_count = 0
        world_extinct_turn = None
        bank_tracking = _new_institution_tracking()
        currency_tracking = _new_institution_tracking()
        local_credit_tracking = _new_institution_tracking()
        enforcement_tracking = _new_institution_tracking()
        barter_tracking = _new_institution_tracking()
        enforcement_settlement_counts_by_stage = {}
        normal_choices_by_currency_stage = {}
        normal_choices_by_local_credit_stage = {}
        settlement_outcome_counts = {"fulfilled": 0, "defaulted": 0}
        settlement_counts_by_checkpoint = {}

    if continue_world:
        if (settlement_network_version < SETTLEMENT_NETWORK_VERSION
                or not all(
                    "local_economy" in row
                    and "community_trust" in row["local_economy"]
                    and "local_credit_stage" in row["local_economy"]
                    for row in settlements.values())):
            settlements = upgrade_single_settlement(settlements, {
                "food": food, "medicine": medicine, "shelter": shelter,
                "tools": tools, "production_capacity": production_capacity,
                "barter_stage": barter_stage,
                "community_trust": community_trust,
                "local_credit_stage": local_credit_stage,
            })
            settlement_network_version = SETTLEMENT_NETWORK_VERSION
        if focus_settlement_id not in settlements:
            focus_settlement_id = (
                select_focus_settlement(settlements) or HOME_SETTLEMENT_ID)
        migration_turn = max(1, start_turn - 1)
        resident_registry_version = (
            int(resident_registry.get("version", 0))
            if isinstance(resident_registry, dict) else 0)
        if resident_registry_version < 1:
            # 旧checkpointの匿名人口へ現在時点で名前を割り当てる。人口・集落・
            # RNGは一切変えず、過去の出生死亡を遡って捏造もしない。
            resident_registry = initial_resident_registry(
                settlements, seed, turn=migration_turn,
                legacy_snapshot=resume_state is not None)
            # 名前付き住民IDが無いcheckpointには、それと対応する正規の空間
            # 状態も存在しない。混在した不完全データは新台帳から再構築する。
            spatial_state = None
            focus_resident_id = select_focus_resident(
                resident_registry, focus_settlement_id, migration_turn)
            if (focus_resident_id is None
                    and resident_registry.get("cohort_mode", False)
                    and settlements[focus_settlement_id]["population"] > 0):
                materialized = materialize_cohort_resident(
                    resident_registry, focus_settlement_id, migration_turn)
                resident_registry = materialized["registry"]
                focus_resident_id = materialized["resident_id"]
            if focus_resident_id is not None:
                resident_registry = assign_resident_focus(
                    resident_registry, focus_resident_id, migration_turn,
                    generation, talent)
        elif resident_registry_version > RESIDENT_REGISTRY_VERSION:
            raise ValueError(
                "resident registry version is newer than this runtime")
        elif resident_registry_version < RESIDENT_REGISTRY_VERSION:
            # 名前付き台帳がある場合は再生成しない。ID・氏名・世帯を
            # 保ったまま、新しい観察欄だけを追加する。
            resident_registry = upgrade_resident_registry(resident_registry)
        if (focus_resident_id not in resident_registry.get("residents", {})
                or not resident_registry["residents"][focus_resident_id].get(
                    "alive", True)):
            focus_resident_id = select_focus_resident(
                resident_registry, focus_settlement_id, migration_turn)
            if (focus_resident_id is None
                    and resident_registry.get("cohort_mode", False)
                    and settlements[focus_settlement_id]["population"] > 0):
                materialized = materialize_cohort_resident(
                    resident_registry, focus_settlement_id, migration_turn)
                resident_registry = materialized["registry"]
                focus_resident_id = materialized["resident_id"]
            if focus_resident_id is not None:
                resident_registry = assign_resident_focus(
                    resident_registry, focus_resident_id, migration_turn,
                    generation, talent)
        if not registry_matches_settlements(resident_registry, settlements):
            raise ValueError(
                "resident registry population does not match settlements")
        if spatial_state is None:
            # 旧checkpointの移行経路。既存settlementの数や座標を初期配置へ
            # 使わず、活動場所と住民IDだけから決定論的に構築する。
            spatial_state = initial_spatial_state(
                resident_registry, migration_turn)
        elif not spatial_state_matches_registry(
                spatial_state, resident_registry):
            raise ValueError(
                "spatial state does not match resident registry")
        # schema 1の既存checkpointにはNPC寿命が無い。過去へ遡って突然死亡させず、
        # 移行した月を登場月として決定論的な予定を付与する。新しく登場するNPCは
        # 下の作成箇所で実際の登場月から予定を持つ。
        migration_turn = max(0, start_turn - 1)
        for npc_id, npc in npcs.items():
            if npc.get("role") != "acquaintance":
                continue
            npc.setdefault("alive", True)
            if npc.get("death_turn") is None:
                introduced_turn = npc.get("introduced_turn")
                if introduced_turn is None:
                    introduced_turn = migration_turn
                schedule = npc_life_schedule(seed, str(npc_id), introduced_turn)
                for key, value in schedule.items():
                    npc.setdefault(key, value)

        # 活動点の近接成分を人口・財・信用の保存則付き共同体台帳へ投影する。
        # checkpointに旧版の台帳があっても、正本の3入力から常に再構築する。
        activity_community_ledger = build_activity_community_ledger(
            spatial_state, resident_registry, settlements,
            int(spatial_state.get("updated_turn", migration_turn)))
        promoted = promote_activity_communities(
            activity_community_ledger, resident_registry)
        settlements = promoted["settlements"]
        resident_registry = promoted["registry"]
        spatial_state = relabel_spatial_accounts(
            spatial_state, resident_registry)
        activity_community_accounting_version = (
            ACTIVITY_COMMUNITY_ACCOUNTING_VERSION)
        focus_settlement_id = (
            activity_community_ledger.get("resident_communities", {}).get(
                focus_resident_id)
            or select_focus_settlement(settlements)
            or focus_settlement_id)
        activity_community_ledger = build_activity_community_ledger(
            spatial_state, resident_registry, settlements,
            int(spatial_state.get("updated_turn", migration_turn)))
        population_stage = max((
            row["stage"] for row in settlements.values()
            if row["population"] > 0), default=4)
        # 初回昇格で複数の旧会計が混ざり得るため、焦点ローカル値も新しい
        # 活動共同体の保存済み配賦値へ切り替える。
        focus_economy = settlements[focus_settlement_id]["local_economy"]
        food, medicine = focus_economy["food"], focus_economy["medicine"]
        shelter, tools = focus_economy["shelter"], focus_economy["tools"]
        production_capacity = focus_economy["production_capacity"]
        barter_stage = focus_economy["barter_stage"]
        community_trust = focus_economy["community_trust"]
        local_credit_stage = focus_economy["local_credit_stage"]

        # 旧checkpointの活動共同体IDが初回昇格で置き換わった場合、次の月次
        # organization planより先に世帯財台帳が現在在庫を読む。存在しない旧IDの
        # claimだけをここで解放し、物理財を失わず共用分として受け入れられるようにする。
        organization_state = reconcile_organization_state_asset_claims(
            organization_state, settlements)

    household_needs_state = None

    def _refresh_household_needs(updated_turn: int) -> None:
        """人口正本から需要を再計算し、各ローカル経済へ参照値だけを投影する。"""
        nonlocal household_needs_state
        if not continue_world:
            return
        household_needs_state = build_household_needs_state(
            settlements, resident_registry, int(updated_turn))
        for settlement_id, row in household_needs_state[
                "communities"].items():
            settlements[settlement_id]["local_economy"][
                "demand_scales_by_good"] = dict(
                    row["demand_scale_by_good"])

    if continue_world:
        _refresh_household_needs(max(0, start_turn - 1))
        if (not verify_household_goods_state(
                household_goods_state, settlements, resident_registry,
                household_needs_state, organization_state)
                or not verify_household_agency_state(
                    household_agency_state, household_goods_state,
                    household_needs_state)):
            initial_agency = plan_household_agency(
                household_agency_state, household_goods_state, settlements,
                resident_registry, household_needs_state, organization_state,
                max(0, start_turn - 1), barter_active=barter_active)
            household_agency_state = initial_agency["state"]
            household_goods_state = initial_agency[
                "household_goods_state"]
        # 初期昇格直後の台帳にも新しい需要参照を反映する。
        activity_community_ledger = build_activity_community_ledger(
            spatial_state, resident_registry, settlements,
            int(spatial_state.get("updated_turn", max(0, start_turn - 1))))

    def _sync_focus_economy() -> None:
        """既存のfocal用ローカル変数を、現在の集落レコードへ投影する。"""
        if not continue_world:
            return
        economy = settlements[focus_settlement_id]["local_economy"]
        economy.update({
            "food": food, "medicine": medicine, "shelter": shelter,
            "tools": tools, "production_capacity": production_capacity,
            "barter_stage": barter_stage,
            "community_trust": community_trust,
            "local_credit_stage": local_credit_stage,
            "community_health": traits["health"],
        })

    def _load_focus_economy() -> None:
        """観察対象が別集落へ移ったとき、その生活基盤をfocal変数へ読む。"""
        nonlocal food, medicine, shelter, tools, production_capacity, barter_stage
        nonlocal community_trust, local_credit_stage
        economy = settlements[focus_settlement_id]["local_economy"]
        food = economy["food"]
        medicine = economy["medicine"]
        shelter = economy["shelter"]
        tools = economy["tools"]
        production_capacity = economy["production_capacity"]
        barter_stage = economy["barter_stage"]
        community_trust = economy["community_trust"]
        local_credit_stage = economy["local_credit_stage"]

    def _focus_provisioning_scale() -> float:
        if not continue_world:
            return 1.0
        return normalize_provisioning_scale(
            settlements[focus_settlement_id]["local_economy"].get(
                "provisioning_scale"))

    def _sync_spatial_state() -> None:
        """当月の住民台帳を永続空間へ1回だけ反映する。"""
        nonlocal spatial_state, activity_community_ledger
        nonlocal settlements, resident_registry, focus_settlement_id
        nonlocal activity_community_accounting_version, population_stage
        if continue_world:
            spatial_state = synchronize_spatial_state(
                spatial_state, resident_registry, turn,
                activity_economy_state=activity_economy_state)
            activity_community_ledger = build_activity_community_ledger(
                spatial_state, resident_registry, settlements, turn)
            promoted = promote_activity_communities(
                activity_community_ledger, resident_registry)
            settlements = promoted["settlements"]
            resident_registry = promoted["registry"]
            spatial_state = relabel_spatial_accounts(
                spatial_state, resident_registry)
            activity_community_accounting_version = (
                ACTIVITY_COMMUNITY_ACCOUNTING_VERSION)
            focus_settlement_id = (
                activity_community_ledger.get(
                    "resident_communities", {}).get(focus_resident_id)
                or select_focus_settlement(settlements)
                or focus_settlement_id)
            _refresh_household_needs(turn)
            activity_community_ledger = build_activity_community_ledger(
                spatial_state, resident_registry, settlements, turn)
            population_stage = max((
                row["stage"] for row in settlements.values()
                if row["population"] > 0), default=4)
            _load_focus_economy()

    def _sync_organizations() -> None:
        """確定済み人口・活動点・制度Stageから組織台帳を更新する。"""
        nonlocal organization_state
        if not continue_world:
            return
        previous_event_count = len(organization_state.get("events", ()))
        organization_state = plan_organization_turn(
            organization_state, activity_community_ledger, spatial_state,
            turn, bank_stage=bank_stage, currency_stage=currency_stage,
            enforcement_stage=enforcement_stage,
            resident_registry=resident_registry)
        if trace is not None:
            trace["organization_events"].extend(
                organization_state.get("events", ())[previous_event_count:])

    def _record_household_goods_events(events: list[dict]) -> None:
        if trace is not None and events:
            trace.setdefault("household_goods_events", []).extend(
                copy.deepcopy(events))

    def _record_household_agency_events(events: list[dict]) -> None:
        if trace is not None and events:
            trace.setdefault("household_agency_events", []).extend(
                copy.deepcopy(events))

    def _record_resident_relationship_events(events: list[dict]) -> None:
        if trace is not None and events:
            trace.setdefault("resident_relationship_events", []).extend(
                copy.deepcopy(events))

    def _sync_household_agency() -> None:
        """確定済みの財内訳から、共用アクセスと次月の世帯対応を決める。"""
        nonlocal household_agency_state, household_goods_state
        nonlocal resident_relationship_state
        if not continue_world:
            return
        relationship_support = household_relationship_support(
            resident_relationship_state, resident_registry)
        planned = plan_household_agency(
            household_agency_state, household_goods_state, settlements,
            resident_registry, household_needs_state, organization_state,
            turn, barter_active=barter_active,
            relationship_support_by_household=relationship_support)
        household_agency_state = planned["state"]
        household_goods_state = planned["household_goods_state"]
        relationships = plan_resident_relationships(
            resident_relationship_state, resident_registry,
            organization_state, planned["relationship_routes"], turn)
        resident_relationship_state = relationships["state"]
        _record_household_goods_events(planned["household_goods_events"])
        _record_household_agency_events(planned["events"])
        _record_resident_relationship_events(relationships["events"])

    def _apply_previous_organization_effects() -> dict:
        """前月末の組織が生む協調余剰を今月へ1回だけ適用する。"""
        nonlocal organization_state
        nonlocal food, medicine, shelter, tools, production_capacity
        nonlocal community_trust
        # claimは共同体在庫の内数である。barter upkeep後に実在庫が減った
        # 場合は、効果計画より先に所有分も比例縮小して二重利用を防ぐ。
        organization_state = reconcile_organization_state_asset_claims(
            organization_state, settlements)
        effects = plan_organization_effects(organization_state)
        applied = copy.deepcopy(effects)
        applied["settlements"] = {}
        applied_community_ids = set()
        for settlement_id, deltas in sorted(
                effects.get("settlements", {}).items()):
            settlement = settlements.get(settlement_id)
            if settlement is None or int(settlement.get("population", 0)) <= 0:
                continue
            economy = settlement["local_economy"]
            provisioning_scale = normalize_provisioning_scale(
                economy.get("provisioning_scale"))
            local_goods_cap = goods_capacity(provisioning_scale)
            realized_deltas = {}
            for field in ("food", "medicine", "shelter", "tools"):
                delta = float(deltas.get(f"{field}_delta", 0.0))
                if delta:
                    before = float(economy.get(field, 0.0))
                    after = round(max(0.0, min(
                        local_goods_cap, before + delta)), 6)
                    economy[field] = after
                    realized = round(after - before, 6)
                    if realized:
                        realized_deltas[f"{field}_delta"] = realized
            capacity_delta = float(deltas.get(
                "production_capacity_delta", 0.0))
            if capacity_delta:
                before = float(economy.get("production_capacity", 0.0))
                after = round(max(0.0, min(
                    deps.production_capacity_cap,
                    before + capacity_delta
                )), 6)
                economy["production_capacity"] = after
                realized = round(after - before, 6)
                if realized:
                    realized_deltas["production_capacity_delta"] = realized
            trust_delta = float(deltas.get("community_trust_delta", 0.0))
            if trust_delta:
                before = float(economy.get("community_trust", 50.0))
                after = round(max(0.0, min(
                    100.0, before + trust_delta)), 6)
                economy["community_trust"] = after
                realized = round(after - before, 6)
                if realized:
                    realized_deltas["community_trust_delta"] = realized
            health_delta = float(deltas.get("community_health_delta", 0.0))
            if health_delta:
                if settlement_id == focus_settlement_id:
                    before = float(traits["health"])
                    after = round(max(
                        deps.trait_min, min(
                            deps.trait_max, before + health_delta)
                    ), 6)
                    traits["health"] = after
                    economy["community_health"] = traits["health"]
                else:
                    before = float(economy.get(
                        "community_health", COMMUNITY_HEALTH_INITIAL))
                    after = round(max(0.0, min(
                        100.0, before + health_delta)), 6)
                    economy["community_health"] = after
                realized = round(after - before, 6)
                if realized:
                    realized_deltas["community_health_delta"] = realized
            local_worst, local_good = deps.worst_shortfall_fn(
                economy["food"], economy["medicine"], economy["shelter"],
                economy["tools"], economy["production_capacity"],
                provisioning_scale=provisioning_scale,
                demand_scales_by_good=economy.get(
                    "demand_scales_by_good"))
            economy["worst_shortfall"] = local_worst
            economy["worst_good"] = local_good
            applied_community_ids.add(settlement_id)
            if realized_deltas:
                applied["settlements"][settlement_id] = realized_deltas
        applied["contributors"] = [
            row for row in effects.get("contributors", ())
            if row.get("community_id") in applied_community_ids]
        applied["organization_count"] = len(applied["contributors"])
        organization_state = record_organization_effects(
            organization_state, applied, turn)
        if trace is not None and applied["settlements"]:
            trace["organization_effects"].append(
                dict(copy.deepcopy(applied), turn=int(turn)))
        _load_focus_economy()
        return applied

    def _gross_production_from_upkeep(upkeep_plan: dict) -> dict:
        raw = upkeep_plan.get("gross_production")
        if isinstance(raw, dict):
            return {
                good: max(0.0, float(raw.get(good, 0.0)))
                for good in ("food", "medicine", "shelter", "tools")}
        # 外部テストや旧callbackが新しい追加キーを返さない場合も呼び出しを
        # 壊さない。正のnet変化だけを観察用grossの下限として扱う。
        return {
            good: max(0.0, float(upkeep_plan.get(
                f"{good}_delta", 0.0)))
            for good in ("food", "medicine", "shelter", "tools")}

    # 2026-08-16追加(可視化ダッシュボード用trace)。checkpointスナップショット
    # (下の"if turn in checkpoint_turns:")とtrace["turns"]記録が同じフィールド
    # 構成を二重実装しないよう、1つのクロージャにまとめる。ループ内の現在値を
    # 呼び出し時点で読むだけの純粋な読み取り関数——数値・選択・RNGには
    # 一切触れない。
    def _snapshot() -> dict:
        snapshot = {
            "resources": dict(resources), "traits": dict(traits),
            "real_money": resources.get("money", 0) / deps.price_index_fn(turn),
            "bank_trust": bank_trust, "bank_crisis_count": bank_crisis_count,
            "peace_min": min_seen.get("peace", resources.get("peace", 0)),
            "bank_stage": bank_stage, "currency_confidence": currency_confidence,
            "currency_stage": currency_stage,
            "community_trust": community_trust, "local_credit_stage": local_credit_stage,
            "enforcement_capacity": enforcement_capacity, "enforcement_stage": enforcement_stage,
            "food": food, "medicine": medicine, "shelter": shelter, "tools": tools,
            "production_capacity": production_capacity, "barter_stage": barter_stage,
            "alternative_economy_available": alternative_economy_available,
        }
        if continue_world:
            focus = settlements[focus_settlement_id]
            focus_economy = focus["local_economy"]
            focus_scale = normalize_provisioning_scale(
                focus_economy.get("provisioning_scale"))
            focus_demand_scales = dict(
                focus_economy.get("demand_scales_by_good", {}))
            focus_coverage = goods_coverage(
                focus_economy["food"], focus_economy["medicine"],
                focus_economy["shelter"], focus_economy["tools"],
                focus_scale, focus_demand_scales)
            world_scale = sum(normalize_provisioning_scale(
                row["local_economy"].get("provisioning_scale"))
                for row in settlements.values())
            world_goods = {
                good: sum(float(row["local_economy"].get(good, 0.0))
                          for row in settlements.values())
                for good in ("food", "medicine", "shelter", "tools")}
            world_coverage = goods_coverage(
                world_goods["food"], world_goods["medicine"],
                world_goods["shelter"], world_goods["tools"], world_scale,
                household_needs_state["world_demand_scale_by_good"])
            focus_practice = normalize_production_practice(
                focus["local_economy"].get(
                    "production_practice_by_good"))
            world_practice = population_weighted_production_practice(
                (row["population"], row["local_economy"].get(
                    "production_practice_by_good"))
                for row in settlements.values())
            current_organizations = active_organizations(organization_state)
            focus_resident = resident_registry["residents"].get(
                focus_resident_id, {})
            focus_household = resident_registry["households"].get(
                focus_resident.get("household_id"), {})
            focus_household_id = str(
                focus_resident.get("household_id") or "")
            focus_household_goods = dict(
                household_goods_state.get("households", {}).get(
                    focus_household_id, {}).get("holdings", {}))
            focus_household_goods_coverage = household_goods_coverage(
                household_goods_state, household_needs_state,
                focus_household_id)
            focus_household_response = dict(
                household_agency_state.get("households", {}).get(
                    focus_household_id, {}))
            personal_turn = max(1, turn - generation_started_turn + 1)
            snapshot.update({
                "settlement_id": focus_settlement_id,
                "provisioning_scale": focus_scale,
                "demand_scales_by_good": focus_demand_scales,
                "goods_coverage_by_good": focus_coverage,
                "world_provisioning_scale": round(world_scale, 12),
                "world_demand_scales_by_good": dict(
                    household_needs_state["world_demand_scale_by_good"]),
                "world_demand_quantities_by_good": dict(
                    household_needs_state[
                        "world_demand_quantity_by_good"]),
                "world_goods_totals": world_goods,
                "world_goods_coverage_by_good": world_coverage,
                "population": focus["population"],
                "reproductive_population": focus["reproductive_population"],
                "productive_population": focus["productive_population"],
                "age_cohorts": dict(focus["age_cohorts"]),
                "population_stage": population_stage,
                "total_population": sum(
                    row["population"] for row in settlements.values()),
                "total_productive_population": sum(
                    row["productive_population"]
                    for row in settlements.values()),
                "settlement_populations": {
                    sid: row["population"] for sid, row in settlements.items()},
                "settlement_reproductive_populations": {
                    sid: row["reproductive_population"]
                    for sid, row in settlements.items()},
                "settlement_productive_populations": {
                    sid: row["productive_population"]
                    for sid, row in settlements.items()},
                "settlement_age_cohorts": {
                    sid: dict(row["age_cohorts"])
                    for sid, row in settlements.items()},
                "settlement_stages": {
                    sid: row["stage"] for sid, row in settlements.items()},
                "settlement_community_trusts": {
                    sid: row["local_economy"]["community_trust"]
                    for sid, row in settlements.items()},
                "settlement_local_credit_stages": {
                    sid: row["local_economy"]["local_credit_stage"]
                    for sid, row in settlements.items()},
                "settlement_barter_stages": {
                    sid: row["local_economy"]["barter_stage"]
                    for sid, row in settlements.items()},
                "settlement_worst_shortfalls": {
                    sid: row["local_economy"].get("worst_shortfall", 0.0)
                    for sid, row in settlements.items()},
                "settlement_provisioning_scales": {
                    sid: normalize_provisioning_scale(
                        row["local_economy"].get("provisioning_scale"))
                    for sid, row in settlements.items()},
                "settlement_demand_scales_by_good": {
                    sid: dict(row["local_economy"].get(
                        "demand_scales_by_good", {}))
                    for sid, row in settlements.items()},
                "settlement_goods_coverage_by_good": {
                    sid: goods_coverage(
                        row["local_economy"]["food"],
                        row["local_economy"]["medicine"],
                        row["local_economy"]["shelter"],
                        row["local_economy"]["tools"],
                        row["local_economy"].get(
                            "provisioning_scale", 1.0),
                        row["local_economy"].get(
                            "demand_scales_by_good"))
                    for sid, row in settlements.items()},
                "settlement_trade_sent_totals": {
                    sid: row["local_economy"].get("trade_sent_total", 0.0)
                    for sid, row in settlements.items()},
                "settlement_trade_received_totals": {
                    sid: row["local_economy"].get("trade_received_total", 0.0)
                    for sid, row in settlements.items()},
                "production_practice_by_good": focus_practice,
                "production_productivity_factors_by_good": (
                    production_productivity_factors(focus_practice)),
                "world_production_practice_by_good": world_practice,
                "world_production_productivity_factors_by_good": (
                    production_productivity_factors(world_practice)),
                "settlement_production_practice_by_good": {
                    sid: normalize_production_practice(
                        row["local_economy"].get(
                            "production_practice_by_good"))
                    for sid, row in settlements.items()},
                "migration_total": migration_total,
                "trade_volume_total": trade_volume_total,
                "trade_event_count": trade_event_count,
                "generation": generation,
                "character_alive": character_alive,
                "character_age": deps.age_at_fn(personal_turn),
                "focus_resident_id": focus_resident_id,
                "focus_resident_name": focus_resident.get("name"),
                "focus_resident_age": (resident_age_years(
                    focus_resident, turn) if focus_resident else None),
                "focus_household_id": focus_resident.get("household_id"),
                "focus_household_name": focus_household.get("name"),
                "focus_household_goods": focus_household_goods,
                "focus_household_goods_coverage_by_good": (
                    focus_household_goods_coverage),
                "focus_household_response": focus_household_response,
                "world_household_priority_counts_by_good": dict(
                    household_agency_state.get(
                        "world_priority_household_counts_by_good", {})),
                "world_household_priority_pressure_by_good": dict(
                    household_agency_state.get(
                        "world_priority_pressure_by_good", {})),
                "settlement_household_priority_pressure_by_good": {
                    sid: dict(row.get("priority_pressure_by_good", {}))
                    for sid, row in household_agency_state.get(
                        "communities", {}).items()},
                "world_household_goods_holdings": dict(
                    household_goods_state.get(
                        "world_household_holdings", {})),
                "world_anonymous_goods_holdings": dict(
                    household_goods_state.get(
                        "world_anonymous_holdings", {})),
                "world_common_goods_pool": dict(
                    household_goods_state.get("world_common_pool", {})),
                "world_organization_goods_claims": dict(
                    household_goods_state.get(
                        "world_organization_claims", {})),
                "world_household_barter_volume_by_good": dict(
                    household_goods_state.get(
                        "world_household_barter_volume_by_good", {})),
                "world_household_barter_exchange_count": int(
                    household_goods_state.get(
                        "world_household_barter_exchange_count", 0)),
                "world_resident_relationship_count": int(
                    resident_relationship_state.get(
                        "world_relationship_count", 0)),
                "world_cross_community_relationship_count": int(
                    resident_relationship_state.get(
                        "world_cross_community_relationship_count", 0)),
                "world_relationship_interactions_by_kind": dict(
                    resident_relationship_state.get(
                        "world_interactions_by_kind", {})),
                "living_resident_count": living_population_count(
                    resident_registry),
                "named_living_resident_count": len(living_residents(
                    resident_registry)),
                "active_household_count": active_household_count(
                    resident_registry),
                "spatial_cluster_count": len(
                    spatial_state.get("clusters", {})),
                "focus_activity_community_id": (
                    activity_community_ledger.get(
                        "resident_communities", {}).get(focus_resident_id)),
                "activity_community_populations": {
                    cid: row["population"] for cid, row in
                    activity_community_ledger.get("communities", {}).items()},
                "activity_community_reproductive_populations": {
                    cid: row["reproductive_population"] for cid, row in
                    activity_community_ledger.get("communities", {}).items()},
                "activity_community_productive_populations": {
                    cid: row["productive_population"] for cid, row in
                    activity_community_ledger.get("communities", {}).items()},
                "activity_community_stages": {
                    cid: row["population_stage"] for cid, row in
                    activity_community_ledger.get("communities", {}).items()},
                "activity_community_trusts": {
                    cid: row["local_economy"]["community_trust"]
                    for cid, row in activity_community_ledger.get(
                        "communities", {}).items()},
                "activity_community_local_credit_stages": {
                    cid: row["local_economy"]["local_credit_stage"]
                    for cid, row in activity_community_ledger.get(
                        "communities", {}).items()},
                "activity_community_barter_stages": {
                    cid: row["local_economy"]["barter_stage"]
                    for cid, row in activity_community_ledger.get(
                        "communities", {}).items()},
                "activity_community_worst_shortfalls": {
                    cid: row["local_economy"]["worst_shortfall"]
                    for cid, row in activity_community_ledger.get(
                        "communities", {}).items()},
                "activity_community_provisioning_scales": {
                    cid: normalize_provisioning_scale(
                        row["local_economy"].get("provisioning_scale"))
                    for cid, row in activity_community_ledger.get(
                        "communities", {}).items()},
                "activity_community_demand_scales_by_good": {
                    cid: dict(row["local_economy"].get(
                        "demand_scales_by_good", {}))
                    for cid, row in activity_community_ledger.get(
                        "communities", {}).items()},
                "activity_community_goods_coverage_by_good": {
                    cid: goods_coverage(
                        row["local_economy"]["food"],
                        row["local_economy"]["medicine"],
                        row["local_economy"]["shelter"],
                        row["local_economy"]["tools"],
                        row["local_economy"].get(
                            "provisioning_scale", 1.0),
                        row["local_economy"].get(
                            "demand_scales_by_good"))
                    for cid, row in activity_community_ledger.get(
                        "communities", {}).items()},
                "activity_community_trade_sent_totals": {
                    cid: row["local_economy"]["trade_sent_total"]
                    for cid, row in activity_community_ledger.get(
                        "communities", {}).items()},
                "activity_community_trade_received_totals": {
                    cid: row["local_economy"]["trade_received_total"]
                    for cid, row in activity_community_ledger.get(
                        "communities", {}).items()},
                "activity_community_production_practice_by_good": {
                    cid: normalize_production_practice(
                        row["local_economy"].get(
                            "production_practice_by_good"))
                    for cid, row in activity_community_ledger.get(
                        "communities", {}).items()},
                "organization_count": len(current_organizations),
                "organization_kind_counts": {
                    kind: sum(1 for row in current_organizations
                              if row["kind"] == kind)
                    for kind in sorted({
                        row["kind"] for row in current_organizations})},
                "organization_trust_stage_counts": {
                    str(stage): sum(1 for row in current_organizations
                                    if row["trust_stage"] == stage)
                    for stage in sorted({
                        row["trust_stage"] for row in current_organizations})},
                "organization_operating_status_counts": {
                    status: sum(1 for row in current_organizations
                                if row.get("operating_status", "stable")
                                == status)
                    for status in sorted({
                        row.get("operating_status", "stable")
                        for row in current_organizations})},
                "organization_operating_reserve_total": round(sum(
                    float(row.get("operating_reserve", 6.0))
                    for row in current_organizations), 6),
                "organization_workforce_total": sum(
                    int(row.get("workforce_count", 0))
                    for row in current_organizations
                    if row.get("kind") in ORGANIZATION_WORKFORCE_KINDS),
                "organization_eligible_workforce_total": sum(
                    int(row.get(
                        "eligible_workforce_count",
                        row.get("productive_member_count", 0)))
                    for row in current_organizations
                    if row.get("kind") in ORGANIZATION_WORKFORCE_KINDS),
                "organization_named_worker_total": sum(
                    len(row.get("named_worker_ids", ()))
                    for row in current_organizations
                    if row.get("kind") in ORGANIZATION_WORKFORCE_KINDS),
                "organization_unrepresented_workforce_total": sum(
                    int(row.get("unrepresented_workforce_count", 0))
                    for row in current_organizations
                    if row.get("kind") in ORGANIZATION_WORKFORCE_KINDS),
                "organization_mean_member_continuity": round(
                    sum(float(row.get("member_continuity", 1.0))
                        for row in current_organizations)
                    / len(current_organizations), 6
                ) if current_organizations else 1.0,
                "organization_asset_claim_totals": {
                    field: round(sum(float(row.get(
                        "asset_claims", {}).get(field, 0.0))
                        for row in current_organizations), 6)
                    for field in (
                        "food", "medicine", "shelter", "tools")},
                "organization_asset_claim_acquisition_totals": dict(
                    organization_state.get(
                        "asset_claim_acquisition_totals", {})),
                "organization_community_distribution_totals": dict(
                    organization_state.get(
                        "community_distribution_totals", {})),
                "organization_effect_totals": dict(
                    organization_state.get("effect_totals", {})),
                "organization_effect_application_count": int(
                    organization_state.get("effect_application_count", 0)),
                "organization_last_effect_turn": organization_state.get(
                    "last_effect_turn"),
                "world_extinct": world_extinct_turn is not None,
            })
        return snapshot

    def _record_trace_turn() -> None:
        if trace is not None:
            trace["turns"].append(dict(_snapshot(), turn=turn, phase="turn_end"))
            if (continue_world
                    and (turn == 1 or turn % SPATIAL_KEYFRAME_INTERVAL == 0)):
                trace["spatial_keyframes"].append(
                    build_spatial_keyframe(spatial_state, turn))

    if continue_world and trace is not None:
        trace.setdefault("character_events", [])
        trace.setdefault("population_events", [])
        trace.setdefault("npc_events", [])
        trace.setdefault("trade_events", [])
        trace.setdefault("resident_events", [])
        trace.setdefault("spatial_keyframes", [])
        trace.setdefault("organization_events", [])
        trace.setdefault("organization_effects", [])
        trace.setdefault("household_goods_events", [])
        trace.setdefault("household_agency_events", [])
        trace.setdefault("resident_relationship_events", [])

    def _record_current_turn_once() -> None:
        """世代交代/世界終端で通常のloop末尾へ到達しないターンを1回記録する。"""
        if continue_world:
            _sync_focus_economy()
            _sync_spatial_state()
            _sync_organizations()
            _sync_household_agency()
        if turn in checkpoint_turns:
            settlement_counts_by_checkpoint[turn] = dict(settlement_outcome_counts)
            trajectory[turn] = _snapshot()
        _record_trace_turn()

    def _handle_character_death(cause: str,
                                population_already_removed: bool = False) -> bool:
        """個人死亡を記録し、世界が残れば次の焦点人物へ切り替える。

        戻り値は世界を継続できるか。continue_world=Falseでは呼ばれない。
        """
        nonlocal resources, traits, talent, npc_pool_cap
        nonlocal used_places, used_concerns, generation, generation_started_turn
        nonlocal character_alive, character_death_count, death_turn
        nonlocal population_stage, world_extinct_turn, focus_settlement_id
        nonlocal resident_registry, focus_resident_id
        nonlocal household_goods_state, settlements

        _sync_focus_economy()
        character_alive = False
        character_death_count += 1
        if death_turn is None:
            death_turn = turn
        previous_focus_id = focus_settlement_id
        focus_before = settlements[previous_focus_id]
        focus_resident = resident_registry["residents"].get(
            focus_resident_id)
        focus_resident_age = (
            resident_age_years(focus_resident, turn)
            if focus_resident is not None else None)
        focus_after = (focus_before if population_already_removed
                       else apply_character_death(
                           focus_before, focus_resident_age))
        settlements[previous_focus_id] = focus_after
        resident_death_events = []
        if focus_resident is not None and focus_resident.get("alive", True):
            resident_death = mark_resident_died(
                resident_registry, focus_resident_id, turn, cause)
            resident_registry = resident_death["registry"]
            resident_death_events = resident_death["events"]
            if trace is not None:
                trace["resident_events"].extend(resident_death_events)
        # 焦点人物の死亡は通常の人口upkeep経路を通らない。空世帯の相続・
        # 共用返還を記録前に確定し、死亡月のsnapshotと再開stateを一致させる。
        _refresh_household_needs(turn)
        if any(event.get("kind") == "household_closed"
               for event in resident_death_events):
            household_lifecycle = plan_household_goods_lifecycle(
                household_goods_state, settlements, resident_registry,
                household_needs_state, organization_state,
                resident_death_events, turn)
            household_goods_state = household_lifecycle["state"]
            settlements = household_lifecycle["settlements"]
            _record_household_goods_events(household_lifecycle["events"])
        else:
            reconciled = reconcile_household_goods_state(
                household_goods_state, settlements, resident_registry,
                household_needs_state, organization_state, turn)
            household_goods_state = reconciled["state"]
            _record_household_goods_events(reconciled["events"])
        _load_focus_economy()
        living_stages = [
            row["stage"] for row in settlements.values()
            if row["population"] > 0]
        population_stage = max(living_stages, default=4)
        orphaned_ids = []
        for contract in contracts:
            if contract.get("status") == "open":
                contract["status"] = "orphaned"
                contract["orphaned_turn"] = turn
                orphaned_ids.append(contract["id"])
        if trace is not None:
            trace["character_events"].append({
                "turn": turn,
                "kind": "character_died",
                "generation": generation,
                "cause": cause,
                "settlement_id": previous_focus_id,
                "population_after": focus_after["population"],
                "orphaned_contract_ids": orphaned_ids,
                "resident_id": focus_resident_id,
                "resident_name": (focus_resident.get("name")
                                  if focus_resident else None),
            })
        if population_tracking is not None:
            # 明示的な個人死亡でStageが変わる場合も、同月の最終Stageを記録へ反映。
            population_tracking["final_stage"] = population_stage
            population_tracking["worst_stage"] = max(
                population_tracking["worst_stage"], population_stage)

        if (focus_after["population"] <= 0 and trace is not None
                and not population_already_removed):
            trace["population_events"].append({
                "turn": turn, "kind": "population_extinct",
                "settlement_id": previous_focus_id,
                "population": 0,
            })
        if world_is_extinct(settlements):
            world_extinct_turn = turn
            if trace is not None:
                trace["population_events"].append({
                    "turn": turn, "kind": "extinction",
                })
            _record_current_turn_once()
            return False

        _record_current_turn_once()
        if focus_after["population"] <= 0:
            next_focus = select_focus_settlement(settlements)
            if next_focus is None:
                return False
            focus_settlement_id = next_focus
        generation += 1
        generation_started_turn = turn + 1
        resources = dict(deps.initial_resources)
        traits = dict(deps.initial_traits)
        talent = deps.choice_fn(deps.traits)
        npc_pool_cap = deps.randint_fn(*deps.npc_pool_cap_range)
        used_places, used_concerns = [], []
        character_alive = True
        _load_focus_economy()
        previous_focus_resident_id = focus_resident_id
        focus_resident_id = select_focus_resident(
            resident_registry, focus_settlement_id, turn + 1,
            exclude_id=previous_focus_resident_id)
        if (focus_resident_id is None
                and resident_registry.get("cohort_mode", False)
                and settlements[focus_settlement_id]["population"] > 0):
            materialized = materialize_cohort_resident(
                resident_registry, focus_settlement_id, turn + 1)
            resident_registry = materialized["registry"]
            focus_resident_id = materialized["resident_id"]
        if focus_resident_id is None:
            # 集落人口と住民台帳の一致を入口で検証しているため通常は到達しない。
            # 壊れたcheckpointを匿名の次世代で隠さず、明示的に停止する。
            raise ValueError("no living resident available for successor")
        resident_registry = assign_resident_focus(
            resident_registry, focus_resident_id, turn + 1,
            generation, talent)
        successor = resident_registry["residents"][focus_resident_id]
        successor_household = resident_registry["households"].get(
            successor["household_id"], {})
        if trace is not None:
            trace["character_events"].append({
                "turn": turn,
                "kind": "successor_selected",
                "generation": generation,
                "talent": talent,
                "settlement_id": focus_settlement_id,
                "population": settlements[focus_settlement_id]["population"],
                "resident_id": focus_resident_id,
                "resident_name": successor["name"],
                "resident_age": resident_age_years(successor, turn + 1),
                "household_id": successor["household_id"],
                "household_name": successor_household.get("name"),
            })
        return True

    last_completed_turn = start_turn - 1
    for turn in range(start_turn, turns + 1):
        # 例外で関数自体が失敗した場合はcheckpointを返さない。通常のcontinue/
        # break経路では、このturnのturn_endを必ず記録してから抜けるため、ここで
        # 完了予定turnを更新してよい。
        last_completed_turn = turn
        gross_production_by_community = {}
        labor_plans_by_community = {}
        upkeep_plans_by_community = {}
        goods_before_by_community = ({
            settlement_id: {
                good: float(settlement["local_economy"].get(good, 0.0))
                for good in ("food", "medicine", "shelter", "tools")}
            for settlement_id, settlement in settlements.items()
        } if continue_world else {})
        resident_lifecycle_events = []
        # decay は「このターン開始時点(regen/income適用前)の残高」を見る
        # (main() 側・simulate_money.py の検証モデルと同じ順序にするため)。
        resources_before = dict(resources)
        for k, v in deps.compute_regen_fn(resources, traits).items():
            resources[k] += v
        # 2026-08-14訂正: ゼロへの一方通行の減衰から、中間値への回帰に変更
        # (bank_trust_reversionのdocstring参照。全方針が同じ値に収束していた
        # 問題への対応)。
        # 2026-08-15(Step 6B、段階的モジュール分割): 回帰量の計算をturn_engine.
        # plan_confidence_reversion()へ委譲した(afterの式・クランプの掛け方は
        # 変更していない)。
        reversion = deps.plan_confidence_reversion_fn(bank_trust, currency_confidence)
        bank_trust = reversion["bank_trust_after"]
        # 2026-08-15追加(地域信用制度)。community_trustもbank_trust/
        # currency_confidenceと同じ「中間値への回帰」を毎ターン適用する
        # (plan_local_credit_reversion()、turn_engine.py経由でinstitutions/
        # local_credit.pyのcommunity_trust_reversionへ委譲)。
        local_credit_reversion = deps.plan_local_credit_reversion_fn(community_trust)
        community_trust = local_credit_reversion["community_trust_after"]
        # 2026-08-15追加(契約執行制度)。enforcement_capacityも同じ「中間値への
        # 回帰」を毎ターン適用する(plan_enforcement_reversion()、turn_engine.py
        # 経由でinstitutions/contract_enforcement.pyのenforcement_capacity_
        # reversionへ委譲)。
        enforcement_reversion = deps.plan_enforcement_reversion_fn(enforcement_capacity)
        enforcement_capacity = enforcement_reversion["enforcement_capacity_after"]
        # 2026-08-14追加(社会レジーム仕様、実装フェーズ第1弾)。通貨の信用も
        # bank_trustと同じ発想だが独立した式・独立した変数で毎ターン中間値へ
        # 回帰する(2026-08-14訂正: 当初の一方通行の減衰が同じ問題を抱えて
        # いたためbank_trustと同じ形に変更、上記コメント参照)。
        currency_confidence = reversion["currency_confidence_after"]
        # 名前付きNPCの自然死。これは集落人口の背景死亡から抽出した観察対象で
        # あり、settlement.populationはここでは減らさない。死亡した貸し手との
        # 未清算契約は不履行ではなくorphanedとし、信用ペナルティを発生させない。
        if continue_world:
            for npc_id, npc in list(npcs.items()):
                if not npc_due_to_die(npc, turn):
                    continue
                orphaned_ids = []
                for contract in contracts:
                    if (contract.get("status") == "open"
                            and contract.get("counterparty") == npc_id):
                        contract["status"] = "orphaned"
                        contract["orphaned_turn"] = turn
                        contract["orphaned_reason"] = "counterparty_died"
                        orphaned_ids.append(contract["id"])
                npcs[npc_id] = mark_npc_died(npc, turn)
                npc_death_count += 1
                if trace is not None:
                    trace["npc_events"].append({
                        "turn": turn,
                        "kind": "npc_died",
                        "name": npc.get("name", str(npc_id)),
                        "cause": "natural",
                        "birth_turn": npc.get("birth_turn"),
                        "age": (round((turn - npc["birth_turn"]) / 12, 3)
                                if npc.get("birth_turn") is not None else None),
                        "settlement_id": npc.get(
                            "settlement_id", HOME_SETTLEMENT_ID),
                        "orphaned_contract_ids": orphaned_ids,
                    })

        # 顔なじみNPCのtrustを中間値へ回帰させる(2026-08-14追加、main()の
        # reduce_state()最終パスと同じ式effective_npc_trustを毎ターン1回分だけ
        # 適用する形——probeはnpcs辞書を直接書き換えているので「最後の変化からの
        # 経過ターン」を都度計算する代わりに、毎ターンtrust_updated_turnをturnに
        # 揃えながら1ターン分ずつ進める。指数減衰なので結果は同じ)。
        for n in npcs.values():
            if not n.get("alive", True):
                continue
            n["trust"] = deps.effective_npc_trust_fn(n, turn)
            n["trust_updated_turn"] = turn
        # 銀行・通貨の状態機械を評価する(main()と同じロジック、共有関数)。
        # 2026-08-15(Step 6B): 遷移判定そのものをturn_engine.
        # plan_institution_transitions()へ委譲した(判定材料・呼び出し順序は
        # 変更していない)。
        transitions = deps.plan_institution_transitions_fn(
            bank_stage, bank_trust, bank_crisis_count, currency_stage, currency_confidence)
        bank_stage = transitions["bank_stage_after"]
        currency_stage = transitions["currency_stage_after"]
        # 2026-08-15追加(地域信用制度)。bank/currencyと並行してlocal_credit_
        # stageも毎ターン評価する(plan_local_credit_transition()、turn_engine.py
        # 経由でinstitutions/local_credit.pyのlocal_credit_stage_nextへ委譲)。
        local_credit_transition = deps.plan_local_credit_transition_fn(local_credit_stage, community_trust)
        local_credit_stage = local_credit_transition["local_credit_stage_after"]
        # 2026-08-15追加(契約執行制度)。bank/currency/local_creditと並行して
        # enforcement_stageも毎ターン評価する(plan_enforcement_transition()、
        # turn_engine.py経由でinstitutions/contract_enforcement.pyの
        # enforcement_stage_nextへ委譲)。
        enforcement_transition = deps.plan_enforcement_transition_fn(
            enforcement_stage, enforcement_capacity)
        enforcement_stage = enforcement_transition["enforcement_stage_after"]
        # Step 13.1: 財の会計は平時から常時動く。上位制度の縮退は現在の
        # 行動解禁とproduction_capacityへの分断圧力だけを決め、過去に一度でも
        # 発火したか(barter_active/activated_turn)とは分離する。
        alternative_economy_available = deps.alternative_economy_triggered_fn(
            bank_stage, currency_stage, local_credit_stage, enforcement_stage)
        if alternative_economy_available and not barter_active:
            barter_active = True
            barter_activated_turn = turn
        focus_settlement = (settlements.get(focus_settlement_id, {})
                            if continue_world else {})
        focus_provisioning_scale = _focus_provisioning_scale()
        focus_demand_scales = (
            dict(focus_settlement.get("local_economy", {}).get(
                "demand_scales_by_good", {})) if continue_world else None)
        focus_labor_plan = (background_production_labor_plan(
            focus_settlement.get("population", 0),
            focus_settlement.get("productive_population", 0),
            goods={
                "food": food, "medicine": medicine,
                "shelter": shelter, "tools": tools},
            provisioning_scale=focus_provisioning_scale,
            demand_scales_by_good=focus_demand_scales,
            household_pressure_by_good=community_priority_pressure(
                household_agency_state, focus_settlement_id))
            if continue_world else None)
        focus_productivity_factors = (
            production_productivity_factors(
                focus_settlement.get("local_economy", {}).get(
                    "production_practice_by_good"))
            if continue_world else None)
        barter_upkeep = deps.plan_barter_upkeep_fn(
            food, medicine, shelter, tools, production_capacity,
            disrupted=alternative_economy_available,
            labor_factor=(focus_labor_plan["labor_factor"]
                          if focus_labor_plan else 1.0),
            labor_factors_by_good=(focus_labor_plan["labor_factor_by_good"]
                                   if focus_labor_plan else None),
            productivity_factors_by_good=focus_productivity_factors,
            provisioning_scale=focus_provisioning_scale,
            demand_scales_by_good=focus_demand_scales)
        if continue_world:
            labor_plans_by_community[focus_settlement_id] = focus_labor_plan
            upkeep_plans_by_community[focus_settlement_id] = barter_upkeep
            gross_production_by_community[focus_settlement_id] = (
                _gross_production_from_upkeep(barter_upkeep))
        food = barter_upkeep["food_after"]
        medicine = barter_upkeep["medicine_after"]
        shelter = barter_upkeep["shelter_after"]
        tools = barter_upkeep["tools_after"]
        production_capacity = barter_upkeep["production_capacity_after"]
        worst_score, worst_good = deps.worst_shortfall_fn(
            food, medicine, shelter, tools, production_capacity,
            provisioning_scale=focus_provisioning_scale,
            demand_scales_by_good=focus_demand_scales)
        # 単一人物モードでは従来どおり直ちにStageと不足ペナルティを確定する。
        # 世界モードは全集落のupkeep後に交易し、その後の在庫で各Stageを1回だけ
        # 評価する(交易前後で同月に二段階遷移させない)。
        if not continue_world:
            barter_transition = deps.plan_barter_transition_fn(
                barter_stage, worst_score)
            barter_stage = barter_transition["barter_stage_after"]
            shortage_penalty = deps.essential_goods_shortage_penalty_fn(barter_stage)
            if shortage_penalty:
                resources["energy"] = (
                    resources.get("energy", 0) + shortage_penalty["energy"])
                traits["health"] = max(deps.trait_min, min(
                    deps.trait_max,
                    traits["health"] + shortage_penalty["health"]))
                barter_shortage_penalty_applied_count += 1
        goods_min_seen["food"] = min(goods_min_seen["food"], food)
        goods_min_seen["medicine"] = min(goods_min_seen["medicine"], medicine)
        goods_min_seen["shelter"] = min(goods_min_seen["shelter"], shelter)
        goods_min_seen["tools"] = min(goods_min_seen["tools"], tools)
        goods_min_seen["production_capacity"] = min(
            goods_min_seen["production_capacity"], production_capacity)
        background_world_extinct = False
        focus_population_extinct = False
        if continue_world:
            _sync_focus_economy()
            focus_economy = settlements[focus_settlement_id]["local_economy"]
            focus_economy["worst_shortfall"] = worst_score
            focus_economy["worst_good"] = worst_good

            # 焦点外の集落も財会計と地域信用を独立に進める。プレイヤーの社会契約
            # は焦点集落だけへ記録されるが、各集落の信用はそれぞれ中立値へ回帰し、
            # 下の集落間交易の成立実績によって別々に動く。
            for settlement_id, settlement in settlements.items():
                economy = settlement["local_economy"]
                if settlement_id != focus_settlement_id:
                    local_reversion = deps.plan_local_credit_reversion_fn(
                        economy["community_trust"])
                    economy["community_trust"] = local_reversion[
                        "community_trust_after"]
                    if int(settlement.get("population", 0)) <= 0:
                        economy["local_credit_stage"] = LOCAL_CREDIT_STAGE_ISOLATED
                    else:
                        economy["local_credit_stage"] = (
                            deps.plan_local_credit_transition_fn(
                                economy["local_credit_stage"],
                                economy["community_trust"])[
                                    "local_credit_stage_after"])
                    local_provisioning_scale = normalize_provisioning_scale(
                        economy.get("provisioning_scale"))
                    local_demand_scales = dict(
                        economy.get("demand_scales_by_good", {}))
                    local_labor_plan = background_production_labor_plan(
                        settlement.get("population", 0),
                        settlement.get("productive_population", 0),
                        goods={
                            good: economy.get(good, 0.0)
                            for good in ("food", "medicine", "shelter", "tools")},
                        provisioning_scale=local_provisioning_scale,
                        demand_scales_by_good=local_demand_scales,
                        household_pressure_by_good=community_priority_pressure(
                            household_agency_state, settlement_id))
                    local_upkeep = deps.plan_barter_upkeep_fn(
                        economy["food"], economy["medicine"], economy["shelter"],
                        economy["tools"], economy["production_capacity"],
                        disrupted=alternative_economy_available,
                        labor_factor=local_labor_plan["labor_factor"],
                        labor_factors_by_good=local_labor_plan[
                            "labor_factor_by_good"],
                        productivity_factors_by_good=(
                            production_productivity_factors(
                                economy.get(
                                    "production_practice_by_good"))),
                        provisioning_scale=local_provisioning_scale,
                        demand_scales_by_good=local_demand_scales)
                    labor_plans_by_community[settlement_id] = local_labor_plan
                    upkeep_plans_by_community[settlement_id] = local_upkeep
                    gross_production_by_community[settlement_id] = (
                        _gross_production_from_upkeep(local_upkeep))
                    economy.update({
                        "food": local_upkeep["food_after"],
                        "medicine": local_upkeep["medicine_after"],
                        "shelter": local_upkeep["shelter_after"],
                        "tools": local_upkeep["tools_after"],
                        "production_capacity": local_upkeep["production_capacity_after"],
                    })
                    local_worst, local_good = deps.worst_shortfall_fn(
                        economy["food"], economy["medicine"], economy["shelter"],
                        economy["tools"], economy["production_capacity"],
                        provisioning_scale=local_provisioning_scale,
                        demand_scales_by_good=local_demand_scales)
                    economy["worst_shortfall"] = local_worst
                    economy["worst_good"] = local_good

            # 前月末に成立していた組織だけが、今月のupkeep後に協調余剰を
            # 生む。今月末に新設・改組された組織は次月まで効かない。
            _apply_previous_organization_effects()
            focus_economy = settlements[focus_settlement_id]["local_economy"]
            worst_score = focus_economy["worst_shortfall"]
            worst_good = focus_economy["worst_good"]

            # この月のgross背景生産を、生産年齢人口・世帯・活動場所へ保存配賦
            # する。財の増減は上のbarter upkeepが正本なので二重計上しない。
            # monkeypatchされた旧upkeepがgross内訳を返さない場合は、正のnet
            # deltaだけを後方互換な観察量として扱う。
            for settlement_id, settlement in settlements.items():
                if settlement_id in gross_production_by_community:
                    continue
                gross_production_by_community[settlement_id] = {
                    good: 0.0
                    for good in ("food", "medicine", "shelter", "tools")}
            activity_plan = plan_activity_economy(
                activity_economy_state, resident_registry, spatial_state,
                settlements, gross_production_by_community, turn,
                labor_plans_by_community=labor_plans_by_community,
                household_priorities=household_priority_by_id(
                    household_agency_state))
            activity_economy_state = activity_plan["state"]
            resident_registry = activity_plan["registry"]
            # 月初時点の経験倍率で今月の生産を確定した後、実際の財別実働から
            # 翌月用の共同体経験を更新する。活動台帳は財を二重生産しない。
            for settlement_id, settlement in settlements.items():
                labor_plan = labor_plans_by_community.get(settlement_id)
                if labor_plan is None:
                    continue
                economy = settlement["local_economy"]
                practice_plan = plan_production_practice(
                    economy.get("production_practice_by_good"),
                    labor_plan["required_worker_count_by_good"],
                    labor_plan["active_worker_count_by_good"])
                economy["production_practice_by_good"] = (
                    practice_plan["practice_after_by_good"])
            household_provisioning = plan_household_goods_provisioning(
                household_goods_state, settlements, resident_registry,
                household_needs_state, organization_state,
                activity_economy_state, spatial_state,
                goods_before_by_community, upkeep_plans_by_community, turn,
                finalize=False)
            household_goods_state = household_provisioning["state"]
            _record_household_goods_events(
                household_provisioning["events"])
            if trace is not None:
                trace["resident_events"].extend(activity_plan["events"])

            # 携行できる財だけを信用容量の範囲で移す。plan側がdeepcopyを返し、
            # 世界総量を保存する。住居・生産能力は集落に固定されたまま。
            trade = plan_intersettlement_trade(settlements, turn)
            settlements = trade["settlements"]
            trade_volume_total = round(
                trade_volume_total + trade["volume"], 6)
            trade_event_count += len(trade["events"])
            if trace is not None:
                trace["trade_events"].extend(trade["events"])
            # 交易後の在庫で、各集落の物々交換Stage・不足実害・人口変化を
            # 1回だけ確定する。
            for settlement_id, settlement in list(settlements.items()):
                economy = settlement["local_economy"]
                local_worst, local_good = deps.worst_shortfall_fn(
                    economy["food"], economy["medicine"], economy["shelter"],
                    economy["tools"], economy["production_capacity"],
                    provisioning_scale=normalize_provisioning_scale(
                        economy.get("provisioning_scale")),
                    demand_scales_by_good=economy.get(
                        "demand_scales_by_good"))
                economy["worst_shortfall"] = local_worst
                economy["worst_good"] = local_good
                economy["barter_stage"] = deps.plan_barter_transition_fn(
                    economy["barter_stage"], local_worst)["barter_stage_after"]
                local_penalty = deps.essential_goods_shortage_penalty_fn(
                    economy["barter_stage"])
                if settlement_id == focus_settlement_id:
                    _load_focus_economy()
                    worst_score, worst_good = local_worst, local_good
                    if local_penalty:
                        resources["energy"] = (
                            resources.get("energy", 0)
                            + local_penalty["energy"])
                        traits["health"] = max(deps.trait_min, min(
                            deps.trait_max,
                            traits["health"] + local_penalty["health"]))
                        barter_shortage_penalty_applied_count += 1
                else:
                    health = economy.get(
                        "community_health", COMMUNITY_HEALTH_INITIAL)
                    if local_penalty:
                        health += local_penalty.get("health", 0.0)
                    else:
                        health += ((COMMUNITY_HEALTH_INITIAL - health)
                                   * COMMUNITY_HEALTH_REVERSION_RATE)
                    economy["community_health"] = max(
                        0.0, min(100.0, health))

                population_plan = plan_population_turn(
                    settlement, economy["worst_shortfall"],
                    (traits["health"] if settlement_id == focus_settlement_id
                     else economy["community_health"]))
                after_settlement = population_plan["settlement"]
                settlements[settlement_id] = after_settlement
                resident_plan = apply_resident_population_change(
                    resident_registry, settlement_id,
                    population_plan["births"], population_plan["deaths"], turn,
                    protected_resident_ids=(focus_resident_id,))
                resident_registry = resident_plan["registry"]
                resident_lifecycle_events.extend(resident_plan["events"])
                if trace is not None:
                    trace["resident_events"].extend(resident_plan["events"])
                if trace is not None and (
                        population_plan["births"] or population_plan["deaths"]):
                    trace["population_events"].append({
                        "turn": turn, "kind": "population_changed",
                        "settlement_id": settlement_id,
                        "births": population_plan["births"],
                        "deaths": population_plan["deaths"],
                        "population": after_settlement["population"],
                        "reproductive_population":
                            after_settlement["reproductive_population"],
                        "productive_population":
                            after_settlement["productive_population"],
                        "age_cohorts": dict(
                            after_settlement["age_cohorts"]),
                    })
                if trace is not None and population_plan["transitioned"]:
                    trace["population_events"].append({
                        "turn": turn,
                        "kind": (population_transition_event(
                            after_settlement["stage"])
                            or "settlement_population_recovered"),
                        "settlement_id": settlement_id,
                        "from_stage": population_plan["from_stage"],
                        "to_stage": after_settlement["stage"],
                        "population": after_settlement["population"],
                        "reproductive_population":
                            after_settlement["reproductive_population"],
                        "productive_population":
                            after_settlement["productive_population"],
                    })

            migration = plan_migration(settlements, turn)
            settlements = migration["settlements"]
            migration_total += migration["migrants"]
            migration_events = [
                plan_household_migration(
                    migration_event, household_agency_state,
                    resident_registry, turn)
                for migration_event in migration["events"]]
            for migration_event in migration_events:
                resident_migration = apply_resident_migration(
                    resident_registry, migration_event, turn,
                    protected_resident_ids=(focus_resident_id,))
                resident_registry = resident_migration["registry"]
                resident_lifecycle_events.extend(
                    resident_migration["events"])
                if trace is not None:
                    trace["resident_events"].extend(
                        resident_migration["events"])
            if trace is not None:
                trace["population_events"].extend(migration_events)
            if not registry_matches_settlements(
                    resident_registry, settlements):
                raise RuntimeError(
                    "resident registry drifted from settlement population")
            # 出生・死亡・移住後の人口/年齢構成を同月の行動評価と表示へ反映。
            # 移住は需要を人と一緒に動かすだけなので世界需要合計を変えない。
            _refresh_household_needs(turn)
            # 集落間交易で物理在庫が減った送り手では、月初に有効だった組織claimが
            # 現在在庫を上回り得る。世帯claimを現在在庫へ合わせるより先に、同じ
            # 内訳である組織claimも比例縮小する。財自体は交易ですでに移動済みで、
            # ここでは所有内訳だけを更新する。
            organization_state = reconcile_organization_state_asset_claims(
                organization_state, settlements)
            lifecycle_kinds = {
                event.get("kind") for event in resident_lifecycle_events}
            if lifecycle_kinds & {
                    "household_split", "residents_migrated",
                    "household_closed"}:
                household_lifecycle = plan_household_goods_lifecycle(
                    household_goods_state, settlements, resident_registry,
                    household_needs_state, organization_state,
                    resident_lifecycle_events, turn,
                    reconcile_organization_state_fn=(
                        reconcile_organization_state_asset_claims))
                household_goods_state = household_lifecycle["state"]
                settlements = household_lifecycle["settlements"]
                organization_state = household_lifecycle[
                    "organization_state"]
                _record_household_goods_events(
                    household_lifecycle["events"])
            else:
                reconciled = reconcile_household_goods_state(
                    household_goods_state, settlements, resident_registry,
                    household_needs_state, organization_state, turn)
                household_goods_state = reconciled["state"]
                _record_household_goods_events(reconciled["events"])
            _load_focus_economy()
            living_stages = [
                row["stage"] for row in settlements.values()
                if row["population"] > 0]
            population_stage = max(living_stages, default=4)
            focus_population_extinct = (
                settlements[focus_settlement_id]["population"] <= 0)
            if world_is_extinct(settlements):
                background_world_extinct = True
                world_extinct_turn = turn
                character_alive = False
                if trace is not None:
                    trace["population_events"].append({
                        "turn": turn, "kind": "extinction",
                    })
        # 2026-08-15追加(Step 12/13)。5制度とも、このターンで確定したstage値を
        # 計測用の辞書へ記録するだけ(数値・選択ロジックには一切影響しない)。
        # barterも常時計算後のStageを記録する。
        _record_institution_stage(bank_tracking, turn, bank_stage)
        _record_institution_stage(currency_tracking, turn, currency_stage)
        _record_institution_stage(local_credit_tracking, turn, local_credit_stage)
        _record_institution_stage(enforcement_tracking, turn, enforcement_stage)
        _record_institution_stage(barter_tracking, turn, barter_stage)
        if continue_world:
            _record_institution_stage(
                population_tracking, turn, population_stage)
        if background_world_extinct:
            _record_current_turn_once()
            break
        if focus_population_extinct:
            if _handle_character_death(
                    "settlement_population_extinct",
                    population_already_removed=True):
                continue
            break
        # 2026-08-14追加(ユーザー指示「体力、健康が0の状態は死と定義したい」、
        # 他のhealth<=0チェックと同じ扱い)。Stage3の実害が致命傷になりうるため、
        # ここ(ターン開始処理の直後、清算・通常行動の前)でも判定する。
        if traits["health"] <= 0:
            if continue_world:
                if _handle_character_death("essential_goods_shortage"):
                    continue
                break
            else:
                death_turn = turn
                _record_trace_turn()
                break
        wage_income = deps.compute_income_fn(turn, bank_stage)
        for k, v in wage_income.items():
            resources[k] = resources.get(k, 0) + v
        if wage_income.get("money"):
            # 賃金前借りは「銀行からの借入(債券)」として契約化する
            # (docs/plan.md「v1実装のopusレビューと訂正」)。
            bank_money -= wage_income["money"]
            contract_sequence += 1
            contracts.append({"id": f"c{contract_sequence}", "status": "open",
                              "due_turn": turn + deps.randint_fn(*deps.contract_due_range),
                              "repay_money": -wage_income["money"],  # 符号はmain()側と同じ理由
                              "is_bank_debt": True})
        # 2026-08-14追加(P0-3「所得と融資を分離する」)。前借り(上記、返済義務
        # あり)とは別に、返済義務の無い本当の所得(salary)を同じタイミングで
        # 支払う。契約を作らない=通貨の恒久的な発行として扱う。
        # 2026-08-14訂正(外部レビュー指摘・社会レジーム仕様との整合): 発行主体を
        # 銀行(bank_money)から切り離す——「銀行が壊れても労働による所得は残る」
        # という設計意図に対し、銀行の財布を直接減らす実装は矛盾していた。
        # probe側はnpcs辞書を持たずbank_money/bank_trustを素の変数として
        # 持つ設計なので、同じパターンでeconomy_moneyを新設する。
        salary = deps.compute_salary_fn(turn)
        if salary:
            resources["money"] = resources.get("money", 0) + salary
            economy_money -= salary
        for k, v in deps.compute_decay_fn(turn, resources_before).items():
            resources[k] = resources.get(k, 0) + v

        due = sorted([c for c in contracts if c["status"] == "open" and c["due_turn"] <= turn],
                     key=lambda c: (c["due_turn"], c["id"]))

        if due:
            # --- settlement(清算)ターン。main()と同じく、時間予算とは無関係。
            # 2026-08-14訂正(7回目opusレビュー指摘・重大5、main()と同じ修正):
            # 従来はdue[0]だけを処理していたため、社会契約が月2.9件ペースで
            # 発生するのに清算が月1件しか進まず恒常的に渋滞していた(実測で
            # 1920ターン中80.7%が清算ターン)。同じターンで期限到来分をまとめて
            # 片付ける。成長判定(compute_trait_step)はD_BASE/health_decayの
            # 月内複数回適用を防ぐため、月内でG_BASE対象の選択(money/labor)が
            # 最初に出た回を使って月末に1回だけまとめて適用する(main()と同じ
            # 理由、通常ターンの時間予算ループとも同じパターン)。
            growth_choice_key = None
            growth_theme_trait = None
            for contract in due:
                if contract["status"] != "open":
                    continue  # 同じターン内の先行する清算で既に片付いた(通常は無い)
                # 2026-08-14、main()と同じくプレイヤー個人の汎用trustを削除した
                # ことに伴い、fulfill_bonus(プレイヤー側trustへの一律増分で、
                # 相手ごとのtrust増分と二重計上だった)を廃止。default_penaltyは
                # 通貨をpeaceに差し替えて復元した(上記contract_default_penaltyの
                # コメント参照。「不履行が完全に無料になりデフォルト率100%まで
                # 悪化する」重大な回帰の修正)。
                # 2026-08-14(Step 3A): 選択肢生成はbuild_settlement_choices()に
                # 統合済み(main()のgenerate_settlement_turnと共通)。
                choices = deps.build_settlement_choices_fn(
                    contract, turn, currency_stage, enforcement_stage=enforcement_stage)
                kind = "清算"
                # 2026-08-13追加(4回目opusレビュー提案、main()と同じ): 清算ターンにも
                # 成長の機会を与える(裏側だけの抽選。ナレーションは無いのでLLMには
                # 一切関係しない)。
                _, settle_concern = deps.pick_theme_fn(used_places, used_concerns)
                theme_trait = deps.theme_concern_traits.get(settle_concern)

                blocked += sum(1 for c in choices if not deps.is_affordable_fn(c, resources))
                picks = {p: deps.auto_select_fn(choices, resources, "balanced", p, safety_floor, turn)
                         for p in deps.policy_vectors}
                agree_log.append({"turn": turn, "kind": kind,
                                  "picks": {p: choices[i]["key"] for p, i in picks.items()},
                                  "agree": len(set(picks.values())) == 1})
                idx = picks[policy_name]
                raw = max(range(len(choices)),
                          key=lambda i: deps.policy_score_fn(
                              choices[i], deps.policy_vectors[policy_name], turn, resources))
                if raw != idx:
                    overridden += 1
                choice = choices[idx]
                delta = deps.clamp_gain_fn(dict(choice["cost"]), resources, traits)
                for k, v in delta.items():
                    resources[k] = resources.get(k, 0) + v
                counts[f"{kind}:{choice['key']}"] = counts.get(f"{kind}:{choice['key']}", 0) + 1

                # contractはdueに入っている時点でcontracts内の同一オブジェクトへの
                # 参照なので、直接書き換えればcontracts側にも反映される。
                contract["status"] = choice["settle"]
                # 2026-08-15追加(Step 12)。この清算がどのenforcement_stageで
                # 起きたか(履行/不履行別)を計測するだけ——enforcement_stage
                # 自体はこのターンの冒頭で既に確定済みの値をそのまま読む。
                _record_settlement_outcome(
                    enforcement_settlement_counts_by_stage, enforcement_stage, choice["settle"])
                settlement_outcome_counts[choice["settle"]] = (
                    settlement_outcome_counts.get(choice["settle"], 0) + 1)
                # 2026-08-16追加(可視化ダッシュボード用trace)。1件の清算イベント
                # (誰との・いくらの契約が・履行/不履行どちらになったか)を記録する。
                if trace is not None:
                    trace["settlements"].append({
                        "turn": turn, "counterparty": contract.get("counterparty"),
                        "is_bank_debt": bool(contract.get("is_bank_debt")),
                        "choice_key": choice["key"], "settle": choice["settle"],
                        "repay_money": contract["repay_money"],
                        "enforcement_stage": enforcement_stage,
                        "settlement_id": contract.get(
                            "settlement_id", focus_settlement_id),
                    })
                # 2026-08-14(Step 3C-3、段階的な数値ルール共通化): 清算の数値効果は
                # plan_settlement_effects()に集約済み(main()と共通)。清算する契約
                # 1件につき正確に1回だけ呼び、戻り値でローカル変数を直接更新する
                # だけにする(main()と違いイベントは積まない、直接更新方式のまま)。
                cp_id = contract.get("counterparty")
                cp_trust_before = (npcs[cp_id]["trust"]
                                   if cp_id is not None and cp_id in npcs else None)
                # 2026-08-15(Step 6C、段階的な数値ルール共通化): 「選択済みchoice
                # →plan_settlement_effects呼び出し」の接着をplan_settlement_
                # resolution()へ委譲した(呼び出し回数・渡す引数は変更していない)。
                effects = deps.plan_settlement_resolution_fn(
                    contract, choice, turn,
                    bank_trust=bank_trust, currency_confidence=currency_confidence,
                    bank_credit_losses=bank_credit_losses, counterparty_trust=cp_trust_before,
                    enforcement_stage=enforcement_stage, enforcement_capacity=enforcement_capacity,
                )["effects"]
                # 銀行債務が履行された場合、銀行のtrustが少し上がる(BANK_TRUST_CAPに
                # 近づくほど逓減)。money型はさらに銀行の帳簿にお金が戻る(=貨幣の破壊)。
                # main()側と同じ条件(game.pyの「4. 適用」節参照)。
                if contract.get("is_bank_debt") and choice["settle"] == "fulfilled":
                    bank_trust += effects["bank_trust_delta"]
                    if choice["key"] == "money":
                        bank_money += effects["bank_wallet_delta"]
                        bank_repaid_count += 1
                        # 2026-08-14追加(社会レジーム仕様、実装フェーズ第1弾)。
                        # money型で実際に履行されるたびに通貨の信用も回復する
                        # (bank_trustとは独立の変数)。
                        currency_confidence += effects["currency_confidence_delta"]
                    # 2026-08-15追加(契約執行制度)。銀行債務・社会契約どちらの
                    # 履行/不履行でもenforcement_capacityを動かす(下記3分岐とも同じ)。
                    enforcement_capacity = max(
                        0.0, enforcement_capacity + effects["enforcement_capacity_delta"])
                elif not contract.get("is_bank_debt") and choice["settle"] == "fulfilled":
                    # 顔なじみNPC自身のtrustも上がる(main()と同じ、NPC_TRUST_CAPに近づくほど逓減)。
                    if cp_id in npcs:
                        npcs[cp_id]["trust"] += effects["counterparty_trust_delta"]
                    # 2026-08-15追加(地域信用制度、「NPC間評判伝播」の片側)。
                    community_trust = max(0.0, community_trust + effects["community_trust_delta"])
                    enforcement_capacity = max(
                        0.0, enforcement_capacity + effects["enforcement_capacity_delta"])
                elif not contract.get("is_bank_debt") and choice["settle"] == "defaulted":
                    if cp_id in npcs:
                        npcs[cp_id]["trust"] += effects["counterparty_trust_delta"]
                    community_trust = max(0.0, community_trust + effects["community_trust_delta"])
                    enforcement_capacity = max(
                        0.0, enforcement_capacity + effects["enforcement_capacity_delta"])
                # 通貨危機の判定・発生(main()側と同じロジック)。この危機で
                # 開いている契約のrepay_moneyが割り直されるが、それはこの
                # for契約ループの後続でまだ処理していない契約にも同じ
                # オブジェクト参照経由で反映されるので、次のcontractの
                # money_cost/labor_cost計算にも自然に反映される。
                if contract.get("is_bank_debt") and choice["settle"] == "defaulted":
                    bank_trust += effects["bank_trust_delta"]  # 銀行自身のtrustも毀損
                    bank_credit_losses = effects["bank_credit_losses_after"]
                    enforcement_capacity = max(
                        0.0, enforcement_capacity + effects["enforcement_capacity_delta"])
                    if effects["crisis_triggered"]:
                        factor = effects["rebase_factor"]
                        resources["money"] = round(resources.get("money", 0) / factor)
                        bank_money = round(bank_money / factor)
                        for c in contracts:
                            if c["status"] == "open":
                                c["repay_money"] = round(c["repay_money"] / factor)
                        bank_credit_losses = 0.0
                        bank_trust += effects["crisis_bank_trust_delta"]  # 危機そのものが信用を大きく損なう
                        bank_crisis_count += 1
                        # 2026-08-14追加(社会レジーム仕様、実装フェーズ第1弾)。
                        # 通貨危機は通貨の信用にも独立にダメージを与える。
                        currency_confidence += effects["crisis_currency_confidence_delta"]

                if growth_choice_key is None and choice["key"] in deps.g_base:
                    growth_choice_key = choice["key"]
                    growth_theme_trait = theme_trait

                for k, v in resources.items():
                    min_seen[k] = min(min_seen[k], v)

            trait_delta, fired = deps.compute_trait_step_fn(
                traits, talent, growth_theme_trait, growth_choice_key or "avoid",
                (turn - generation_started_turn + 1 if continue_world else turn),
                "settlement")
            for k, v in trait_delta.items():
                traits[k] = max(deps.trait_min, min(deps.trait_max, traits[k] + v))
            if fired:
                fires += 1
            # 2026-08-14追加(ユーザー指示「体力、健康が0の状態は死と定義したい」)。
            # health<=0での即死。この時点で未清算の契約(due以外の残りopen分も
            # 含む)はそのままopenで残す——死亡を不履行(default)として扱うと
            # 相手の信用・銀行の信用損失にペナルティが波及してしまうため、
            # 意図的に「信用の判定の対象外」にする(ユーザー念押し済み)。
            if traits["health"] <= 0:
                if continue_world:
                    if _handle_character_death("health_depleted_after_settlement"):
                        continue
                    break
                else:
                    death_turn = turn
                    _record_trace_turn()
                    break
        else:
            # --- 時間予算制(2026-08-14追加、main()と同じ構造。docs/plan.md
            # 「時間予算制」節)。月内の時間予算が尽きるまで「1つ選ぶ」を繰り返す。
            # 成長判定+老化由来の減衰(compute_trait_step)は月に1回だけ呼ぶ
            # (D_BASE/health_decayを月内で複数回適用しないため)。ただし
            # 「月内で最初に選んだ行動」で決め打ちすると、その回がたまたま
            # G_BASE対象外の「休息」だった場合に月の成長機会がまるごと潰れて
            # しまう(2026-08-14に発見・訂正: --policy-checkでhealthがT960で
            # 0.0まで急落したのはこれが原因だった)。月内でG_BASE対象の行動が
            # 一度でも選ばれていれば、それを使って月末に1回だけ成長判定する。
            budget = deps.turn_time_budget
            growth_choice_key = None
            growth_theme_trait = None
            while budget >= deps.min_activity_hours:
                _, concern = deps.pick_theme_fn(used_places, used_concerns)
                theme_trait = deps.theme_concern_traits.get(concern)
                # 顔なじみがいれば、その中からランダムに1人を売り手として価格に反映する
                # (docs/plan.md「[将来] 財の価格形成」の簡略実装、main()と同じロジック)。
                # 2026-08-14(Step 3B): money modifier・archetype選定・基礎choice生成は
                # compute_normal_money_modifier/available_normal_archetypes/
                # build_normal_base_choiceへ統合済み(main()のgenerate_normal_turnと共通)。
                acquaintances_dict = {n["name"]: n for n in npcs.values()
                                      if (n.get("role") == "acquaintance"
                                          and n.get("alive", True)
                                          and n.get(
                                              "settlement_id",
                                              HOME_SETTLEMENT_ID)
                                          == focus_settlement_id)}
                money_modifier = deps.compute_normal_money_modifier_fn(acquaintances_dict, currency_stage)
                archetypes = deps.available_normal_archetypes_fn(
                    currency_stage, local_credit_stage, bank_stage, enforcement_stage, barter_stage)
                alternative_available = any(
                    a["key"] in ("barter", "subsistence") for a in archetypes)
                goods_state = ({
                    "food": food, "medicine": medicine, "shelter": shelter,
                    "tools": tools, "production_capacity": production_capacity,
                    "provisioning_scale": _focus_provisioning_scale(),
                } if alternative_available else None)
                if goods_state is not None and continue_world:
                    goods_state["demand_scales_by_good"] = dict(
                        settlements[focus_settlement_id]["local_economy"].get(
                            "demand_scales_by_good", {}))
                choices = []
                for a in archetypes:
                    ch = (deps.build_normal_base_choice_fn(
                            a, turn, money_modifier, goods_state=goods_state)
                          if goods_state is not None else
                          deps.build_normal_base_choice_fn(a, turn, money_modifier))
                    if a.get("creates_contract"):
                        # 相手を決める(2026-08-13変更、NPC永続化の展開): main()と
                        # 文字どおり同じpick_social_counterpartyを使う(5回目opusレビュー
                        # 「probe/main()のNPC生成則が異なる」の対応)。新規の場合の
                        # 名前だけは、probeにLLMが無いので合成カウンタ(main()側は
                        # LLMの自由生成)で決める——名前の生成方式自体は共有しない
                        # 設計だが、選択ロジック(再利用確率・上限・寿命・お人好し
                        # 判定)は完全に共有する。
                        # 銀行債務(賃金前借り)にはcounterpartyが無いので除く。
                        open_cps = {c["counterparty"] for c in contracts
                                   if c["status"] == "open" and c.get("counterparty")}
                        cp_name, is_new = deps.pick_social_counterparty_fn(
                            acquaintances_dict, open_cps, turn, npc_pool_cap)
                        # RNG順序(prospective_ethics→prospective_retire_turn→
                        # due_turn→repay_money)は元のsimulate_policyのまま維持する
                        # (main()のgenerate_normal_turnとは順序が異なるが、今回は
                        # 統一しない)。
                        if is_new:
                            npc_sequence += 1
                            cp_name = f"npc{npc_sequence}"
                            prospective_ethics = deps.uniform_fn(*deps.npc_ethics_range)
                            prospective_retire_turn = turn + deps.randint_fn(*deps.npc_relationship_span_range)
                            existing = None
                        else:
                            existing = acquaintances_dict[cp_name]
                            prospective_ethics = None
                            prospective_retire_turn = None
                        due_turn = turn + deps.randint_fn(*deps.contract_due_range)
                        trust_for_limit, lo, hi = deps.compute_social_contract_repay_range_fn(
                            acquaintances_dict, existing, prospective_ethics, turn, due_turn)
                        ch["contract"] = {"due_turn": due_turn,
                                          "repay_money": deps.randint_fn(round(min(lo, hi)), round(max(lo, hi))),
                                          "counterparty": cp_name,
                                          "prospective_ethics": prospective_ethics,
                                          "prospective_retire_turn": prospective_retire_turn}
                    choices.append(ch)
                kind = "通常"

                # 2026-08-14訂正(6回目opusレビュー指摘・高4): 予算が
                # MIN_ACTIVITY_HOURS以上残っていても、その回に抽選されたhoursが
                # 偶然どれも予算を超えることがある(全archetypeの下限の最小値で
                # ループ条件を作っているため)。auto_selectの「どれも払えない」
                # フォールバックはbudgetを見ないため、予算オーバーのまま強制的に
                # 選択が行われ、予算が負に沈んでいた(実測: auto_select呼び出しの
                # 9.6%がここに落ち、その全てでrestが選ばれていた)。この月は
                # ここで打ち切り、残り時間は月末の自動休息に回す。
                if not any(deps.is_affordable_fn(c, resources, budget) for c in choices):
                    break

                blocked += sum(1 for c in choices if not deps.is_affordable_fn(c, resources, budget))
                # 同じ(状態, 選択肢)に対して3方針が何を選ぶかを記録する(反実仮想の比較)。
                # 軌跡が分岐した後も横並びで比べられるようにするため。
                picks = {p: deps.auto_select_fn(choices, resources, "balanced", p, safety_floor,
                                        turn, budget) for p in deps.policy_vectors}
                agree_log.append({"turn": turn, "kind": kind,
                                  "picks": {p: choices[i]["key"] for p, i in picks.items()},
                                  "agree": len(set(picks.values())) == 1})

                idx = picks[policy_name]
                # 生存制約(1段目)が方針の第一希望を実際に却下した回数を数える。
                raw = max(range(len(choices)),
                          key=lambda i: deps.policy_score_fn(
                              choices[i], deps.policy_vectors[policy_name], turn, resources))
                if raw != idx:
                    overridden += 1
                choice = choices[idx]
                # 2026-08-15(Step 6D、段階的な数値ルール共通化): 「選択済みchoice
                # →clamp_gain適用」の接着をplan_normal_action_resolution()へ
                # 委譲した(呼び出し回数・渡す引数は変更していない)。
                delta = deps.plan_normal_action_resolution_fn(choice, resources, traits)["delta"]
                for k, v in delta.items():
                    resources[k] = resources.get(k, 0) + v
                counts[f"{kind}:{choice['key']}"] = counts.get(f"{kind}:{choice['key']}", 0) + 1
                _record_choice_by_stage(
                    normal_choices_by_currency_stage, currency_stage, choice["key"])
                _record_choice_by_stage(
                    normal_choices_by_local_credit_stage, local_credit_stage, choice["key"])
                # 2026-08-15追加(Step 13、物々交換・自給制度)。barter/subsistence
                # が選ばれた回だけ、財への効果を加算する(既存の資源コスト
                # 〈energy/peace〉の適用は上のdelta適用で他のarchetypeと同じ経路で
                # 既に完了済み——これは財という「別帳簿」への効果のみ)。
                if choice["key"] in ("barter", "subsistence"):
                    action_goods_before = {
                        "food": food, "medicine": medicine,
                        "shelter": shelter, "tools": tools,
                    }
                    barter_effects = choice.get("goods_effects") or \
                        deps.barter_choice_effects_fn(choice["key"], production_capacity)
                    local_goods_cap = goods_capacity(
                        _focus_provisioning_scale())
                    food = max(0.0, min(
                        local_goods_cap, food + barter_effects["food_delta"]))
                    medicine = max(0.0, min(
                        local_goods_cap,
                        medicine + barter_effects["medicine_delta"]))
                    shelter = max(0.0, min(
                        local_goods_cap,
                        shelter + barter_effects["shelter_delta"]))
                    tools = max(0.0, min(
                        local_goods_cap, tools + barter_effects["tools_delta"]))
                    production_capacity = max(0.0, min(
                        deps.production_capacity_cap,
                        production_capacity + barter_effects["production_capacity_delta"]))
                    goods_min_seen["food"] = min(goods_min_seen["food"], food)
                    goods_min_seen["medicine"] = min(goods_min_seen["medicine"], medicine)
                    goods_min_seen["shelter"] = min(goods_min_seen["shelter"], shelter)
                    goods_min_seen["tools"] = min(goods_min_seen["tools"], tools)
                    goods_min_seen["production_capacity"] = min(
                        goods_min_seen["production_capacity"], production_capacity)
                    if continue_world:
                        # 共同体総量への効果を正本へ先に反映し、その実現増分だけを
                        # 行動した世帯の内訳へ帰属させる。capで失われたraw効果を
                        # 世帯保有へ計上しない。
                        _sync_focus_economy()
                        focus_resident = resident_registry[
                            "residents"].get(focus_resident_id, {})
                        action_goods = {
                            good: round(max(
                                0.0, value - action_goods_before[good]), 6)
                            for good, value in {
                                "food": food, "medicine": medicine,
                                "shelter": shelter, "tools": tools,
                            }.items()
                        }
                        action_credit = credit_household_action_goods(
                            household_goods_state, settlements,
                            resident_registry, household_needs_state,
                            organization_state,
                            str(focus_resident.get("household_id") or ""),
                            focus_settlement_id, action_goods, turn,
                            reason=choice["key"])
                        household_goods_state = action_credit["state"]
                        _record_household_goods_events(
                            action_credit["events"])
                if "contract" in choice:
                    c = choice["contract"]
                    if c["counterparty"] not in npcs:
                        # 初対面の相手の初期trustは、プレイヤーの一般的な評判
                        # (周りの人間からの信用値)をNPC_TRUST_INITIALとの
                        # 加重平均で反映する(2026-08-14追加・再訂正、ユーザー提案)。
                        # 2026-08-15追加(地域信用制度、「NPC間評判伝播」のもう
                        # 片側)。2026-08-15訂正(ユーザーレビュー指摘への対応):
                        # Stage判定はrelationship_rules.initial_trust_for_new_npc
                        # 自体が行うようになったため、ここではlocal_credit_stage・
                        # community_trustを無条件に渡すだけでよい(game.
                        # build_normal_contract_eventsラッパーと同じ配線方式)。
                        initial_trust = deps.initial_trust_for_new_npc_fn(
                            acquaintances_dict, turn,
                            local_credit_stage=local_credit_stage, community_trust=community_trust)
                        npc_record = {
                            "name": c["counterparty"], "role": "acquaintance", "money": 0,
                            "settlement_id": focus_settlement_id,
                            "trust": initial_trust,
                            "trust_updated_turn": turn,  # 中間値への回帰(2026-08-14追加)の起点
                            "ethics": c["prospective_ethics"],
                            "retire_turn": c["prospective_retire_turn"],
                        }
                        if continue_world:
                            npc_record.update(npc_life_schedule(
                                seed, c["counterparty"], turn))
                        npcs[c["counterparty"]] = npc_record
                        # 2026-08-16追加(可視化ダッシュボード用trace)。新規NPC登場
                        # イベントを1件記録する(既存の登場ロジックは無変更)。
                        if trace is not None:
                            trace["npc_introductions"].append({
                                "turn": turn, "name": c["counterparty"], "initial_trust": initial_trust,
                            })
                    contract_sequence += 1
                    contracts.append({"id": f"c{contract_sequence}", "status": "open",
                                      "due_turn": c["due_turn"], "repay_money": c["repay_money"],
                                      "counterparty": c["counterparty"],
                                      "settlement_id": focus_settlement_id})

                # 特性の成長・老化由来の減衰は月末に1回だけ適用する(下記)。
                # ここでは「G_BASE対象の行動が月内で最初に選ばれた回」だけ記録する。
                if growth_choice_key is None and choice["key"] in deps.g_base:
                    growth_choice_key = choice["key"]
                    growth_theme_trait = theme_trait

                for k, v in resources.items():
                    min_seen[k] = min(min_seen[k], v)
                budget -= choice.get("hours", 0)

            # 月に1回だけcompute_trait_stepを呼ぶ(D_BASE/health_decayの二重適用を
            # 防ぐため)。月内にG_BASE対象の行動が一度も無かった(全部休息)場合も、
            # 老化由来の減衰だけは必ず適用する("rest"はG_BASE非対象なので
            # growth_theme_trait=Noneのまま渡せば成長は起きない)。
            trait_delta, fired = deps.compute_trait_step_fn(
                traits, talent, growth_theme_trait, growth_choice_key or "rest",
                (turn - generation_started_turn + 1 if continue_world else turn),
                "normal")
            for k, v in trait_delta.items():
                traits[k] = max(deps.trait_min, min(deps.trait_max, traits[k] + v))
            if fired:
                fires += 1
            # 2026-08-14追加(ユーザー指示「体力、健康が0の状態は死と定義したい」、
            # main()の清算ブロックと同じ)。未清算の契約はopenのまま残し、
            # 不履行(default)としては扱わない(信用の判定の対象外)。
            if traits["health"] <= 0:
                if continue_world:
                    if _handle_character_death("health_depleted_after_normal_action"):
                        continue
                    break
                else:
                    death_turn = turn
                    _record_trace_turn()
                    break

            if budget > 0:
                # 端数は自動的に休息に充当する(main()と同じ、コード側のみ)。
                # 2026-08-14訂正(高5): 残り時間に按分する(draw_prorated_rest)。
                rest_delta = deps.clamp_gain_fn(
                    deps.draw_prorated_rest_fn(turn, budget), resources, traits)
                for k, v in rest_delta.items():
                    resources[k] = resources.get(k, 0) + v
                for k, v in resources.items():
                    min_seen[k] = min(min_seen[k], v)

        if continue_world:
            _sync_focus_economy()
            # 出生・死亡・世帯分割・移住を含む当月の住民台帳がすべて
            # 確定してから、永続空間をちょうど1回だけ進める。
            _sync_spatial_state()
            _sync_organizations()
            _sync_household_agency()
        if turn in checkpoint_turns:
            settlement_counts_by_checkpoint[turn] = dict(settlement_outcome_counts)
            # 2026-08-16(可視化ダッシュボード用trace追加時のリファクタリング):
            # 中身は_snapshot()へ切り出した(下のtrace["turns"]記録と二重実装
            # しないため)。キー構成・値は一切変更していない。
            trajectory[turn] = _snapshot()
        _record_trace_turn()

    result = {
        "policy": policy_name, "counts": counts, "resources": resources,
        "trajectory": trajectory,
        "min_seen": min_seen, "blocked": blocked, "agree_log": agree_log,
        "contracts": contracts, "overridden": overridden,
        "bank_money": bank_money, "economy_money": economy_money,
        "bank_repaid_count": bank_repaid_count,
        "bank_crisis_count": bank_crisis_count, "bank_trust": bank_trust,
        "bank_stage": bank_stage, "currency_confidence": currency_confidence,
        "currency_stage": currency_stage,
        "community_trust": community_trust, "local_credit_stage": local_credit_stage,
        "enforcement_capacity": enforcement_capacity, "enforcement_stage": enforcement_stage,
        # 2026-08-15追加(Step 13、物々交換・自給制度)。
        "food": food, "medicine": medicine, "shelter": shelter, "tools": tools,
        "provisioning_scale": _focus_provisioning_scale(),
        "goods_coverage_by_good": goods_coverage(
            food, medicine, shelter, tools, _focus_provisioning_scale(),
            (settlements[focus_settlement_id]["local_economy"].get(
                "demand_scales_by_good") if continue_world else None)),
        "production_capacity": production_capacity, "barter_stage": barter_stage,
        "barter_active": barter_active, "goods_min_seen": goods_min_seen,
        "barter_activated_turn": barter_activated_turn,
        "alternative_economy_available": alternative_economy_available,
        "barter_shortage_penalty_applied_count": barter_shortage_penalty_applied_count,
        "trade_volume_total": trade_volume_total,
        "trade_event_count": trade_event_count,
        "real_money": resources.get("money", 0) / deps.price_index_fn(turns),
        "traits": traits, "talent": talent, "fires": fires, "npcs": npcs,
        "npc_death_count": npc_death_count,
        "death_turn": death_turn,
        # 2026-08-15追加(Step 12、--policy-checkの制度レジーム対応再設計)。
        # 既存キーは一切変更していない、新規キーの追加のみ。
        # 2026-08-15追加(Step 13): institution_trajectoriesに"barter"を追加
        # (5制度目)。
        "institution_trajectories": {
            "bank": bank_tracking,
            "currency": currency_tracking,
            "local_credit": local_credit_tracking,
            "contract_enforcement": dict(
                enforcement_tracking,
                settlement_counts_by_stage=enforcement_settlement_counts_by_stage),
            "barter": barter_tracking,
        },
        "policy_check_metrics": {
            "normal_choices_by_currency_stage": normal_choices_by_currency_stage,
            "normal_choices_by_local_credit_stage": normal_choices_by_local_credit_stage,
            "settlement_counts_by_checkpoint": settlement_counts_by_checkpoint,
        },
    }
    if continue_world:
        # デジタル水槽の再開境界。simulate_policy内でターンを越えて生存する
        # mutable値をすべて保存し、乱数状態も含めて次回呼び出しへ渡す。
        # copy.deepcopyにより、返却後にresultや呼び出し元が状態を変更しても
        # checkpointの内容が連動して変化しない。
        resume_checkpoint = copy.deepcopy({
            "schema_version": 1,
            "policy": policy_name,
            "seed": seed,
            "safety_floor": safety_floor,
            "completed_turn": last_completed_turn,
            "random_state": deps.getstate_fn(),
            "resources": resources,
            "traits": traits,
            "talent": talent,
            "npc_pool_cap": npc_pool_cap,
            "contract_sequence": contract_sequence,
            "npc_sequence": npc_sequence,
            "contracts": contracts,
            "counts": counts,
            "npcs": npcs,
            "used_places": used_places,
            "used_concerns": used_concerns,
            "blocked": blocked,
            "min_seen": min_seen,
            "agree_log": agree_log,
            "overridden": overridden,
            "fires": fires,
            "bank_money": bank_money,
            "economy_money": economy_money,
            "bank_repaid_count": bank_repaid_count,
            "bank_credit_losses": bank_credit_losses,
            "bank_crisis_count": bank_crisis_count,
            "bank_trust": bank_trust,
            "bank_stage": bank_stage,
            "currency_confidence": currency_confidence,
            "currency_stage": currency_stage,
            "community_trust": community_trust,
            "local_credit_stage": local_credit_stage,
            "enforcement_capacity": enforcement_capacity,
            "enforcement_stage": enforcement_stage,
            "food": food,
            "medicine": medicine,
            "shelter": shelter,
            "tools": tools,
            "production_capacity": production_capacity,
            "barter_stage": barter_stage,
            "barter_active": barter_active,
            "barter_activated_turn": barter_activated_turn,
            "alternative_economy_available": alternative_economy_available,
            "barter_shortage_penalty_applied_count":
                barter_shortage_penalty_applied_count,
            "goods_min_seen": goods_min_seen,
            "trajectory": trajectory,
            "death_turn": death_turn,
            "settlements": settlements,
            "settlement_network_version": settlement_network_version,
            "focus_settlement_id": focus_settlement_id,
            "migration_total": migration_total,
            "trade_volume_total": trade_volume_total,
            "trade_event_count": trade_event_count,
            "resident_registry": resident_registry,
            "spatial_state": spatial_state,
            "activity_community_ledger": activity_community_ledger,
            "activity_community_accounting_version":
                activity_community_accounting_version,
            "activity_economy_state": activity_economy_state,
            "organization_state": organization_state,
            "household_needs_state": household_needs_state,
            "household_goods_state": household_goods_state,
            "household_agency_state": household_agency_state,
            "resident_relationship_state": resident_relationship_state,
            "focus_resident_id": focus_resident_id,
            "population_stage": population_stage,
            "population_tracking": population_tracking,
            "generation": generation,
            "generation_started_turn": generation_started_turn,
            "character_alive": character_alive,
            "character_death_count": character_death_count,
            "npc_death_count": npc_death_count,
            "world_extinct_turn": world_extinct_turn,
            "bank_tracking": bank_tracking,
            "currency_tracking": currency_tracking,
            "local_credit_tracking": local_credit_tracking,
            "enforcement_tracking": enforcement_tracking,
            "barter_tracking": barter_tracking,
            "enforcement_settlement_counts_by_stage":
                enforcement_settlement_counts_by_stage,
            "normal_choices_by_currency_stage": normal_choices_by_currency_stage,
            "normal_choices_by_local_credit_stage":
                normal_choices_by_local_credit_stage,
            "settlement_outcome_counts": settlement_outcome_counts,
            "settlement_counts_by_checkpoint": settlement_counts_by_checkpoint,
        })
        result.update({
            "world_mode": True,
            "demand_scales_by_good": dict(
                settlements[focus_settlement_id]["local_economy"].get(
                    "demand_scales_by_good", {})),
            "settlements": settlements,
            "settlement_network_version": settlement_network_version,
            "focus_settlement_id": focus_settlement_id,
            "migration_total": migration_total,
            "trade_volume_total": trade_volume_total,
            "trade_event_count": trade_event_count,
            "resident_registry": resident_registry,
            "spatial_state": spatial_state,
            "activity_community_ledger": activity_community_ledger,
            "activity_community_accounting_version":
                activity_community_accounting_version,
            "activity_economy_state": activity_economy_state,
            "organization_state": organization_state,
            "household_needs_state": household_needs_state,
            "household_goods_state": household_goods_state,
            "household_agency_state": household_agency_state,
            "resident_relationship_state": resident_relationship_state,
            "focus_resident_id": focus_resident_id,
            "population_stage": population_stage,
            "generation": generation,
            "character_alive": character_alive,
            "character_death_count": character_death_count,
            "world_extinct": world_extinct_turn is not None,
            "world_extinct_turn": world_extinct_turn,
            "resume_state": resume_checkpoint,
        })
        result["institution_trajectories"]["population"] = population_tracking
    return result


# 2026-08-15追加(Step 12.1)。生涯の総遷移回数は長く生存したケースほど増えるため、
# 過剰往復の判定には使わない。同じ境界を12ターン以内に逆向きへ再横断した回数を
# 直接数え、1回までは暫定許容する。どちらも観測開始時の仮値・要再較正。
ENFORCEMENT_RAPID_RECROSS_WINDOW_TURNS = 12
ENFORCEMENT_MAX_RAPID_RECROSSES = 1

# 2026-08-15追加(Step 13)。物々交換・自給専用の新基準で使う仮の閾値
# (契約執行の基準11〜13と同じ設計思想を再利用する、仕様「契約執行と同じ
# 短期再横断指標を再利用する」の実装)。ウィンドウは契約執行と同じ12ターンを
# 流用し、許容回数だけ独立に持つ——barter_stageは4段階(境界3つ)しか無い点は
# enforcement_stageと同じだが、財の劣化速度がenforcement_capacityとは
# 異なるため、許容回数は共有せず別定数にする。
BARTER_RAPID_RECROSS_WINDOW_TURNS = ENFORCEMENT_RAPID_RECROSS_WINDOW_TURNS
BARTER_MAX_RAPID_RECROSSES = 1


def check_trajectory_criteria(r: dict, *, dependencies: PolicySimulationDependencies) -> list:
    """反証可能な合否基準の判定(2026-08-13追加、5回目opusレビュー指摘への対応)。
    「T960の1点だけを見て均衡と判定する」誤り(2・3・5回目のレビューで繰り返し
    発生)を機械的に防ぐため、CHECKPOINT_TURNSの複数観測点の推移でPASS/FAILを
    出す。戻り値は [{"name","passed","detail","category","applicable_regime",
    "observed_regime"}, ...]。"passed"は True(PASS)/False(FAIL)/
    None(N/A、評価に必要なデータが無い、または制度崩壊により評価対象そのものが
    利用不能)の3値。

    r: simulate_policy()の戻り値そのもの。2026-08-14訂正(6回目opusレビュー
    指摘・優先順位5): 従来はtrajectory(チェックポイント断面)しか見ておらず、
    「通常行動の91%がrest」「80年でNPCが2人」「3方針の選択が99.2%一致」という、
    経済として明らかに壊れている状態を全部PASSさせていた。counts/agree_log/npcsも
    見る基準7〜10を追加した。

    2026-08-14再訂正(ユーザーが持ち込んだ外部レビュー指摘・P0-2「合否判定が
    比較指標として不安定」): 死亡等でtrajectoryのチェックポイントが揃わないと
    基準そのものが行ごと欠落し、走行ごとにPASS/FAILの分母(基準の総数)が
    変わってしまい、「PASS X/FAIL Y」という合計値だけでは走行間の比較が
    できなかった。**固定分母**にするため、データが足りない基準も行ごと消さず、
    passed=None(N/A)として必ず出力するようにした(死亡した走行のほうが
    基準数が少なく見え、結果としてFAILが減って見える、という誤解を防ぐ)。

    2026-08-15追加(Step 12/12.1、`--policy-check`の制度レジーム対応再設計): 各基準に
    category(core/bank/currency/local_credit/contract_enforcement/
    regime_choice_economy)・applicable_regime(この基準が前提とする制度状態)・
    observed_regime(実際に観測された制度状態)を付与した。可逆制度に依存する
    選択基準は、対象の選択肢が利用可能だった期間だけを評価し、観測が無い場合
    だけN/Aにする。契約執行(3)専用の新基準(基準11〜13)も追加した。"""
    deps = dependencies
    trajectory = r["trajectory"]
    total_settle = sum(v for k, v in r["counts"].items() if k.startswith("清算"))
    default_rate = r["counts"].get("清算:avoid", 0) / total_settle if total_settle else None
    policy_check_metrics = r.get("policy_check_metrics", {})
    settlement_counts_by_checkpoint = policy_check_metrics.get(
        "settlement_counts_by_checkpoint", {})
    if "policy_check_metrics" in r:
        settlement_counts_960 = settlement_counts_by_checkpoint.get(960)
        total_settle_960 = (sum(settlement_counts_960.values())
                            if settlement_counts_960 is not None else 0)
        default_rate_960 = (settlement_counts_960.get("defaulted", 0) / total_settle_960
                            if total_settle_960 else None)
    else:
        # Step 12以前の保存結果・テストfixtureとの後方互換。新しいsimulate_policy()
        # の戻り値では必ず上の期間別計測を使う。
        default_rate_960 = default_rate
    results = []
    turns_sorted = sorted(trajectory.keys())
    inst_traj = r.get("institution_trajectories", {})
    bank_traj = inst_traj.get("bank", _new_institution_tracking())
    currency_traj = inst_traj.get("currency", _new_institution_tracking())
    local_credit_traj = inst_traj.get("local_credit", _new_institution_tracking())
    enforcement_traj = inst_traj.get("contract_enforcement", _new_institution_tracking())
    barter_traj = inst_traj.get("barter", _new_institution_tracking())

    def get(turn, *path):
        node = trajectory[turn]
        for p in path:
            node = node[p]
        return node

    def add(name, passed, detail, *, category="core", applicable_regime="any", observed_regime="any"):
        results.append({"name": name, "passed": passed, "detail": detail,
                        "category": category, "applicable_regime": applicable_regime,
                        "observed_regime": observed_regime})

    # --- レジーム判定(2026-08-15追加、Step 12)。数値閾値は変更せず、既存基準
    # の評価対象そのものが制度崩壊で利用不能になった場合だけをN/Aにするための
    # 判定材料。「いつか一度でも到達したか」を見る(currency放棄・local_credit
    # 孤立はどちらも可逆なので、チェックポイント断面だけでは見逃すため)。
    currency_ever_abandoned = CURRENCY_STAGE_ABANDONED in currency_traj["first_reached_turn"]
    local_credit_ever_isolated = LOCAL_CREDIT_STAGE_ISOLATED in local_credit_traj["first_reached_turn"]
    # bank消滅(BANK_STAGE_COLLAPSED)は不可逆なので、T960時点のStageがそのまま
    # 「T960までに消滅したか」の判定になる。
    bank_collapsed_by_960 = (960 in trajectory and get(960, "bank_stage") == BANK_STAGE_COLLAPSED)

    # 基準1: 成長可能な特性は、T960→T1920で大きくは動かない(=一方的な下降
    # 継続ではなく、真に停滞している)。
    for t in deps.growable_traits:
        name = f"特性安定性({deps.trait_ja[t]}, T960→T1920)"
        if 960 in trajectory and 1920 in trajectory:
            v960, v1920 = get(960, "traits", t), get(1920, "traits", t)
            add(name, abs(v1920 - v960) <= 10.0,
                f"{v960:.1f} → {v1920:.1f} (差{v1920 - v960:+.1f}, 許容±10)")
        else:
            add(name, None, "T960/T1920のいずれかが未到達(死亡等)のためN/A")

    # 基準2: healthは設計地平(T960=80年)まではSAFETY_FLOOR退化境界(24)を超えない。
    # 2026-08-14訂正(7回目opusレビュー指摘・重大6): T960の1点しか見ていな
    # かったため、「T960は基準を満たすがT1920(160年)ではhealthがほぼ0まで
    # 崩壊する」ケース(実測15ケース中14ケース)を検出できなかった——他の
    # GROWABLE_TRAITSと違いhealth_decayは年齢に線形で無条件に効くので、
    # T1920でも同じ床(24)を要求する意味はある(money/laborの選択比率が
    # 健全ならHEALTH_CARE_GAINで十分上回れる水準——不可能な基準ではない)。
    for cp in (960, 1920):
        name = f"health(T{cp}時点でSAFETY_FLOOR退化境界24以上)"
        if cp in trajectory:
            h = get(cp, "traits", "health")
            add(name, h >= 24.0, f"health={h:.1f}")
        else:
            add(name, None, f"T{cp}が未到達(死亡等)のためN/A")

    # 基準2b(2026-08-14追加、ユーザー指示「体力、健康が0の状態は死と定義
    # したい」に伴う訂正): health<=0で即死しシミュレーションを打ち切るように
    # したため、死亡がT960より前に起きると、そのトラジェクトリのチェックポイント
    # 自体が記録されず基準2(health>=24)が静かにスキップされてしまう
    # (「不合格」の情報が消える)。死亡年齢がT960(設計地平=80年)より前なら
    # 明示的にFAILとして可視化する。生存した場合はPASS(死亡していない=
    # 設計地平T960まで生きていることが保証されている、max_turns>=960のため)。
    death_turn = r.get("death_turn")
    if death_turn is not None:
        add("生存(T960の設計地平までは生存している)", death_turn >= 960,
            f"ターン{death_turn}(年齢{deps.age_at_fn(death_turn):.0f})で死亡")
    else:
        add("生存(T960の設計地平までは生存している)", True, "死亡せず生存")

    # 基準3(2026-08-14削除): 「trustは全観測点で有界」はプレイヤー個人trustの
    # 廃止に伴い削除した(resources["trust"]がもう存在しない)。相手ごとの
    # trust〈NPC/銀行〉の有界性チェックは今後の課題(TODOとして残す)。

    # 基準4: 通貨危機。T960までのデフォルト率が高い(>8%)方針はT960までに1回以上、
    # 低い(<=3%)方針は0回であるべき(「踏み倒しが多いなら銀行がすぐ破綻する」)。
    # T960までのdefault_rate=None(その時点までに清算が無い)、960未到達の
    # いずれもN/A。生涯率とT960の危機回数を混ぜない。
    # 2026-08-15追加(Step 12): bank_crisisは銀行債務のデフォルトから駆動される
    # ため、銀行制度そのものが崩壊済み(消滅・不可逆)のケースをcategory="bank"
    # とし、消滅後は「健全な銀行制度」を前提にした基準として成立しないためN/Aに
    # する(銀行消滅後は新規融資が発行されないため、bank_crisisイベント自体が
    # 構造的に発生しなくなる——「危機0回」は健全の証ではなく評価対象の消失)。
    have960 = 960 in trajectory
    crisis960 = get(960, "bank_crisis_count") if have960 else None
    hi_name = "通貨危機(高デフォルト率→T960までに1回以上)"
    lo_name = "通貨危機(低デフォルト率→T960までに0回)"
    bank_regime = f"bank_collapsed_by_960={bank_collapsed_by_960}"
    if bank_collapsed_by_960:
        add(hi_name, None, "T960までに銀行制度が消滅済みのためN/A(新規融資が発行されず評価対象が無い)",
            category="bank", applicable_regime="bank_not_collapsed", observed_regime=bank_regime)
        add(lo_name, None, "T960までに銀行制度が消滅済みのためN/A(新規融資が発行されず評価対象が無い)",
            category="bank", applicable_regime="bank_not_collapsed", observed_regime=bank_regime)
    elif not have960 or default_rate_960 is None:
        add(hi_name, None, "T960未到達、または清算が一度も無いためN/A",
            category="bank", applicable_regime="bank_not_collapsed", observed_regime=bank_regime)
        add(lo_name, None, "T960未到達、または清算が一度も無いためN/A",
            category="bank", applicable_regime="bank_not_collapsed", observed_regime=bank_regime)
    elif default_rate_960 > 0.08:
        add(hi_name, crisis960 >= 1, f"危機{crisis960}回, T960までのデフォルト率{default_rate_960:.1%}",
            category="bank", applicable_regime="bank_not_collapsed", observed_regime=bank_regime)
        add(lo_name, None, f"T960までのデフォルト率{default_rate_960:.1%}が対象範囲外(<=3%)のためN/A",
            category="bank", applicable_regime="bank_not_collapsed", observed_regime=bank_regime)
    elif default_rate_960 <= 0.03:
        add(hi_name, None, f"T960までのデフォルト率{default_rate_960:.1%}が対象範囲外(>8%)のためN/A",
            category="bank", applicable_regime="bank_not_collapsed", observed_regime=bank_regime)
        add(lo_name, crisis960 == 0, f"危機{crisis960}回, T960までのデフォルト率{default_rate_960:.1%}",
            category="bank", applicable_regime="bank_not_collapsed", observed_regime=bank_regime)
    else:
        add(hi_name, None, f"T960までのデフォルト率{default_rate_960:.1%}がどちらの対象範囲(<=3%/>8%)にも該当しないためN/A",
            category="bank", applicable_regime="bank_not_collapsed", observed_regime=bank_regime)
        add(lo_name, None, f"T960までのデフォルト率{default_rate_960:.1%}がどちらの対象範囲(<=3%/>8%)にも該当しないためN/A",
            category="bank", applicable_regime="bank_not_collapsed", observed_regime=bank_regime)
    # 2026-08-14追加(7回目opusレビュー指摘・重大7): 「デフォルト率が高い
    # なら1回以上」という下限しか無く、実測で1920ターン中99〜104回
    # (≒19ターンに1回)発生していても常時PASSしていた。「稀な破滅的
    # イベント」という設計意図(bank_crisisのコメント参照)に対する上限が
    # 無かったので反証可能にする。閾値5は仮値(この基準がFAILし続けるなら
    # 「稀」の想定自体を見直す必要がある、という意味での仮の線)。
    if bank_collapsed_by_960:
        add("通貨危機が稀である(T960までに5回以下)", None,
            "T960までに銀行制度が消滅済みのためN/A", category="bank",
            applicable_regime="bank_not_collapsed", observed_regime=bank_regime)
    elif have960:
        add("通貨危機が稀である(T960までに5回以下)", crisis960 <= 5,
            f"危機{crisis960}回, T960までのデフォルト率"
            f"{'N/A' if default_rate_960 is None else f'{default_rate_960:.1%}'}",
            category="bank", applicable_regime="bank_not_collapsed", observed_regime=bank_regime)
    else:
        add("通貨危機が稀である(T960までに5回以下)", None, "T960未到達のためN/A",
            category="bank", applicable_regime="bank_not_collapsed", observed_regime=bank_regime)

    # 基準5: real_moneyが「山なり→崩壊」ではないこと(T960→T1920で1/5未満に
    # 崩れていないか)。「ほぼ0」の閾値は2026-08-14、10倍スケール化に合わせて
    # 1.0→10.0に再較正。
    # 2026-08-15追加(Step 12): 通貨放棄後は「通常の通貨経済」という前提が
    # 崩れる(誰も中央通貨を受け取らなくなる)ため、T960/T1920いずれかの時点で
    # 通貨が放棄されていればN/A(real_moneyの下落は「通貨制度崩壊の帰結」で
    # あって「経済の失敗」ではない、という区別)。
    name5 = "real_moneyの後半崩壊が無いこと(T960→T1920)"
    currency_regime5 = f"currency_ever_abandoned={currency_ever_abandoned}"
    if 960 in trajectory and 1920 in trajectory:
        currency_abandoned_in_window = (get(960, "currency_stage") == CURRENCY_STAGE_ABANDONED
                                        or get(1920, "currency_stage") == CURRENCY_STAGE_ABANDONED)
        if currency_abandoned_in_window:
            add(name5, None, "T960またはT1920時点で通貨が放棄されているためN/A"
                             "(通常通貨レジームの前提が崩れている)",
                category="currency", applicable_regime="currency_not_abandoned",
                observed_regime=currency_regime5)
        else:
            rm960, rm1920 = get(960, "real_money"), get(1920, "real_money")
            add(name5, rm960 < 10.0 or rm1920 >= rm960 * 0.2, f"{rm960:.1f} → {rm1920:.1f}",
                category="currency", applicable_regime="currency_not_abandoned",
                observed_regime=currency_regime5)
    else:
        add(name5, None, "T960/T1920のいずれかが未到達(死亡等)のためN/A",
            category="currency", applicable_regime="currency_not_abandoned",
            observed_regime=currency_regime5)

    # 基準6: peaceが実際に機能しているか(一度も初期値を下回っていなければ
    # 「死んだ資源」の疑いをFAILとして可視化する)。制度Stageに関係なく維持
    # (基礎基準、category="core")。
    name6 = "peaceが実際に機能している(初期値を下回ったことがある)"
    if turns_sorted:
        peace_min = min(get(t, "peace_min") for t in turns_sorted)
        add(name6, peace_min < deps.initial_resources["peace"], f"観測された最小値={peace_min}")
    else:
        add(name6, None, "チェックポイントに一度も到達していない(早期死亡)ためN/A")

    # 基準7(2026-08-14追加): 通常ターンの選択が単一のarchetypeへ極端に
    # 偏っていないか。「restが91%」のような支配戦略の再発を検出する。
    # 2026-08-15追加(Step 12.1): money型はcurrency放棄中、social型はlocal_credit
    # 孤立中、それぞれ選択肢から除外される。どちらも可逆制度なので、生涯全体を
    # N/Aにはせず、選択肢が存在したStageの通常行動だけを分母にする。利用可能期間に
    # 通常行動が一度も無い場合だけN/A。labor/restは全期間・category="core"のまま。
    normal_counts = {k.split(":", 1)[1]: v for k, v in r["counts"].items()
                     if k.startswith("通常")}
    for key in ("money", "labor", "social", "rest"):
        name7 = f"選択の多様性(通常:{key}が5%〜80%の範囲)"
        if key == "money":
            category7, applicable7 = "regime_choice_economy", "currency_not_abandoned"
            observed7 = f"currency_ever_abandoned={currency_ever_abandoned}"
            stage_counts = policy_check_metrics.get("normal_choices_by_currency_stage")
            applicable_counts = (_choice_counts_excluding_stage(
                stage_counts, CURRENCY_STAGE_ABANDONED) if stage_counts is not None else
                (normal_counts if not currency_ever_abandoned else {}))
        elif key == "social":
            category7, applicable7 = "local_credit", "local_credit_not_isolated"
            observed7 = f"local_credit_ever_isolated={local_credit_ever_isolated}"
            stage_counts = policy_check_metrics.get("normal_choices_by_local_credit_stage")
            applicable_counts = (_choice_counts_excluding_stage(
                stage_counts, LOCAL_CREDIT_STAGE_ISOLATED) if stage_counts is not None else
                (normal_counts if not local_credit_ever_isolated else {}))
        else:
            category7, applicable7, observed7 = "core", "any", "any"
            applicable_counts = normal_counts
        applicable_total = sum(applicable_counts.values())
        if applicable_total:
            share = applicable_counts.get(key, 0) / applicable_total
            period_label = "適用期間" if key in ("money", "social") else "全期間"
            add(name7, 0.05 <= share <= 0.80,
                f"{key}={share:.1%}({applicable_counts.get(key, 0)}/{applicable_total}, {period_label})",
                category=category7, applicable_regime=applicable7, observed_regime=observed7)
        else:
            add(name7, None, "選択肢が利用可能な期間に通常行動が一度も無いためN/A",
                category=category7, applicable_regime=applicable7, observed_regime=observed7)

    # 基準8(2026-08-14追加): デフォルト率が過大でないか。50%を超えると
    # 「借りたら基本踏み倒す」が既定戦略になっており、社会的契約という
    # 仕組み自体が意味を失っている。制度Stageによる免除は設けない(不履行が
    # 過大かどうかはレジームに関わらず常に意味のある問いのため)。
    name8 = "デフォルト率が過大でない(<=50%)"
    if default_rate is not None:
        add(name8, default_rate <= 0.5, f"デフォルト率{default_rate:.1%}",
            category="regime_choice_economy")
    else:
        add(name8, None, "清算が一度も無いためN/A", category="regime_choice_economy")

    # 基準9(2026-08-14追加): 3方針(cautious/ambitious/family)の選択が
    # ある程度は割れているか。「99%以上一致」は方針ベクトルが機能して
    # いないことの反証可能なサイン。
    name9 = "方針ベクトルが機能している(3方針の不一致率>=1%)"
    agree_flags = [row["agree"] for row in r["agree_log"]]
    if agree_flags:
        disagree_rate = 1 - (sum(agree_flags) / len(agree_flags))
        add(name9, disagree_rate >= 0.01, f"不一致率{disagree_rate:.1%}({len(agree_flags)}時点中)")
    else:
        add(name9, None, "選択機会が一度も無いためN/A")

    # 基準10(2026-08-14追加): NPC永続化(顔なじみの蓄積)が実際に駆動されて
    # いるか。「80年間でNPCが2人しか登場しない」ような、restが選択肢を
    # 独占してsocialがほぼ発火しない状態を検出する。これはtrajectoryや
    # 清算回数に依存せず常に計算できるのでN/Aにはならない。
    npc_count = sum(1 for n in r["npcs"].values() if n.get("role") == "acquaintance")
    add("NPC永続化が機能している(生涯の顔なじみNPC数>=3)", npc_count >= 3,
        f"NPC数={npc_count}")

    # 基準11〜13(2026-08-15追加、Step 12): 契約執行(3)専用の新基準。
    # institution_trajectories["contract_enforcement"]のfirst_reached_turnを
    # 使い、「Stage2(地域台帳への移行)以上へ最初に到達したturn」を求める
    # (チェックポイント断面だけでは、途中で一時的にStage2以上へ落ちてから
    # 回復したケースを見逃すため、「いつ最初に到達したか」で判定する)。
    first_stage2plus_turn = min(
        (t for stage, t in enforcement_traj["first_reached_turn"].items()
         if stage >= ENFORCEMENT_STAGE_LOCAL_LEDGER), default=None)
    for checkpoint, name_suffix in ((60, "T60"), (960, "T960")):
        name11 = f"契約執行: {name_suffix}までにStage2以上へ早期崩壊していない"
        # 死亡等でchekcpointまでシミュレーションが到達していない場合はN/A
        # (その期間に何が起きたはずかを判定する材料が無いため)。
        if death_turn is not None and death_turn < checkpoint:
            add(name11, None, f"ターン{death_turn}で死亡・{name_suffix}未到達のためN/A",
                category="contract_enforcement", applicable_regime="any",
                observed_regime=f"death_turn={death_turn}")
            continue
        collapsed_early = first_stage2plus_turn is not None and first_stage2plus_turn <= checkpoint
        worst_stage_by_checkpoint = _worst_stage_reached_by_turn(enforcement_traj, checkpoint)
        add(name11, not collapsed_early,
            (f"{name_suffix}までにStage2以上へ到達(初回到達ターン={first_stage2plus_turn})"
             if collapsed_early else
             f"{name_suffix}までStage0/1を維持(worst_stage={worst_stage_by_checkpoint})"),
            category="contract_enforcement", applicable_regime="any",
            observed_regime=f"first_stage2plus_turn={first_stage2plus_turn}")

    name13 = "契約執行: 閾値付近で過剰な往復遷移を起こしていない"
    chatter = _transition_chatter_metrics(
        enforcement_traj, ENFORCEMENT_RAPID_RECROSS_WINDOW_TURNS)
    rapid_recrosses = chatter["rapid_same_boundary_recross_count"]
    if not chatter["history_available"]:
        add(name13, None, "遷移履歴の無い旧形式の結果のためN/A",
            category="contract_enforcement", applicable_regime="any",
            observed_regime="transition_history=missing")
    else:
        add(name13, rapid_recrosses <= ENFORCEMENT_MAX_RAPID_RECROSSES,
            f"{ENFORCEMENT_RAPID_RECROSS_WINDOW_TURNS}ターン以内の同一境界再横断={rapid_recrosses}回"
            f"(許容<={ENFORCEMENT_MAX_RAPID_RECROSSES}), 総遷移={chatter['transition_count']}回, "
            f"100ターン当たり={chatter['transition_rate_per_100_turns']:.2f}回, "
            f"方向反転={chatter['direction_reversal_count']}回",
            category="contract_enforcement", applicable_regime="any",
            observed_regime=f"rapid_same_boundary_recross_count={rapid_recrosses}")

    # 基準14〜17(2026-08-15追加、Step 13): 物々交換・自給(5)専用の新基準。
    # 契約執行の基準11〜13と同じ設計(早期崩壊・過剰往復遷移)に加え、この
    # 制度特有の「代替経路が実際に使われるか」「Stage3の実害が実際に効くか」の
    # 2件を追加する(仕様「少なくとも以下を判定する」の4項目)。
    first_barter_stage2plus_turn = min(
        (t for stage, t in barter_traj["first_reached_turn"].items()
         if stage >= BARTER_STAGE_SUBSISTENCE_ONLY), default=None)
    activated_turn = r.get("barter_activated_turn")
    observed_until = death_turn if death_turn is not None else max(
        (sum(barter_traj["stage_turns"].values()), 0))
    for active_horizon in (60, 200):
        name14 = (f"物々交換: 発火後{active_horizon}ターン以内に"
                  "Stage2(自給のみ)以上へ早期崩壊していない")
        if activated_turn is None:
            add(name14, None, "代替経済が発火しなかったためN/A",
                category="barter", applicable_regime="alternative_economy_triggered",
                observed_regime="barter_activated_turn=None")
            continue
        elapsed_to_stage2 = (None if first_barter_stage2plus_turn is None else
                             first_barter_stage2plus_turn - activated_turn)
        collapsed_early = elapsed_to_stage2 is not None and elapsed_to_stage2 < active_horizon
        observed_active_turns = max(0, observed_until - activated_turn + 1)
        if not collapsed_early and observed_active_turns < active_horizon:
            add(name14, None,
                f"発火後{observed_active_turns}ターンで走行終了・判定期間未到達のためN/A",
                category="barter", applicable_regime="alternative_economy_triggered",
                observed_regime=f"barter_activated_turn={activated_turn}")
            continue
        add(name14, not collapsed_early,
            (f"発火後{elapsed_to_stage2}ターンでStage2以上へ到達"
             if collapsed_early else
             f"発火後{active_horizon}ターンまでStage0/1を維持"),
            category="barter", applicable_regime="alternative_economy_triggered",
            observed_regime=(f"barter_activated_turn={activated_turn}, "
                             f"first_stage2plus_turn={first_barter_stage2plus_turn}"))

    # 基準15: 上位制度の崩壊・縮退でbarter_activeになった期間、代替経路
    # (barter/subsistence)が実際に選ばれているか。healthyな走行(barter_active
    # が一度もtrueにならない)ではそもそも評価対象が無いためN/A(基準を緩めた
    # わけではなく、仕組みが試される機会自体が無かったという区別)。
    name15 = "物々交換: barter_active期間にbarter/subsistenceが実際に使われている"
    barter_active = r.get("barter_active", False)
    if not barter_active:
        add(name15, None, "barter_activeが一度もtrueにならなかったためN/A"
                          "(上位制度が健全なまま推移した)",
            category="barter", applicable_regime="alternative_economy_triggered",
            observed_regime="barter_active=False")
    else:
        alt_choice_count = (r["counts"].get("通常:barter", 0)
                            + r["counts"].get("通常:subsistence", 0))
        add(name15, alt_choice_count > 0,
            f"barter選択={r['counts'].get('通常:barter', 0)}回, "
            f"subsistence選択={r['counts'].get('通常:subsistence', 0)}回",
            category="barter", applicable_regime="alternative_economy_triggered",
            observed_regime="barter_active=True")

    # 基準16: Stage3(必需財不足)に到達した走行では、滞在ターン数
    # (shortage_turns)ぶん過不足なくenergy/healthへの実害が適用されているか
    # (仕様「Stage3で不足ペナルティが実際に発生する」の直接検証——適用回数の
    # カウンタとstage_turnsの突き合わせなので、他の要因〈老化減衰等〉との
    # 混同が起きない)。Stage3に一度も到達していない走行はN/A。
    name16 = "物々交換: Stage3到達時に必需財不足ペナルティが実際に発生している"
    shortage_turns = barter_traj["stage_turns"].get(BARTER_STAGE_SHORTAGE, 0)
    if shortage_turns == 0:
        add(name16, None, "Stage3(必需財不足)に一度も到達していないためN/A",
            category="barter", applicable_regime="barter_stage_shortage_reached",
            observed_regime=f"shortage_turns={shortage_turns}")
    else:
        applied = r.get("barter_shortage_penalty_applied_count", 0)
        add(name16, applied == shortage_turns,
            f"shortage_turns={shortage_turns}, ペナルティ適用回数={applied}",
            category="barter", applicable_regime="barter_stage_shortage_reached",
            observed_regime=f"shortage_turns={shortage_turns}")

    name17 = "物々交換: 閾値付近で過剰な短期再横断を起こしていない"
    barter_chatter = _transition_chatter_metrics(barter_traj, BARTER_RAPID_RECROSS_WINDOW_TURNS)
    barter_rapid_recrosses = barter_chatter["rapid_same_boundary_recross_count"]
    if not barter_chatter["history_available"]:
        add(name17, None, "遷移履歴の無い旧形式の結果のためN/A",
            category="barter", applicable_regime="any",
            observed_regime="transition_history=missing")
    else:
        add(name17, barter_rapid_recrosses <= BARTER_MAX_RAPID_RECROSSES,
            f"{BARTER_RAPID_RECROSS_WINDOW_TURNS}ターン以内の同一境界再横断={barter_rapid_recrosses}回"
            f"(許容<={BARTER_MAX_RAPID_RECROSSES}), 総遷移={barter_chatter['transition_count']}回, "
            f"100ターン当たり={barter_chatter['transition_rate_per_100_turns']:.2f}回, "
            f"方向反転={barter_chatter['direction_reversal_count']}回",
            category="barter", applicable_regime="any",
            observed_regime=f"rapid_same_boundary_recross_count={barter_rapid_recrosses}")

    return results


def check_enforcement_convergence(run_results: list) -> dict:
    """T1920まで生存した全ケースのenforcement_stageが単一Stageへ一律収束して
    いないかを判定する、走行横断(cross-run)の反証可能な基準(2026-08-15追加、
    Step 12)。check_trajectory_criteria()は単一run(1つのseed×policyの結果)
    しか見ないため、複数run分の結果をまとめて見るこの基準だけは独立した関数に
    する——run_policy_check()が全seed×全policyのsimulate_policy()戻り値を
    集めてから、ループの外で1回だけ呼ぶ。

    2026-08-15、ユーザーレビュー指摘: 実測では全生存ケースがStage2に収束して
    いた(bank_trust初期実装の「政策非依存の収束」と同種の問題か、reversionと
    事件圧力の平衡点がたまたまStage2域にあるだけかは未判定)。この基準は
    **現状のバランスに合わせて基準を緩めず、問題を可視化する**ことが目的
    ——現状の実装のままではFAILする可能性が高いことを承知の上で追加する。

    run_results: 各要素がsimulate_policy()の戻り値そのもの(全seed×全policy
    分)。戻り値は{"name","passed","detail","category","applicable_regime",
    "observed_regime"}——check_trajectory_criteria()の個々の基準と同じ形。"""
    survivor_stages = [r.get("enforcement_stage") for r in run_results if r.get("death_turn") is None]
    name = "契約執行: T1920生存ケースが単一Stageへ一律収束していない"
    if len(survivor_stages) < 2:
        return {"name": name, "passed": None,
               "detail": f"T1920生存ケースが{len(survivor_stages)}件しかなく、収束の有無を判定できないためN/A",
               "category": "contract_enforcement", "applicable_regime": "any",
               "observed_regime": f"survivor_count={len(survivor_stages)}"}
    distinct_stages = sorted(set(survivor_stages))
    breakdown = {stage: survivor_stages.count(stage) for stage in distinct_stages}
    return {"name": name, "passed": len(distinct_stages) > 1,
           "detail": f"生存{len(survivor_stages)}件のenforcement_stage分布={breakdown}"
                    + ("(単一Stageへ一律収束している)" if len(distinct_stages) == 1 else ""),
           "category": "contract_enforcement", "applicable_regime": "any",
           "observed_regime": f"distinct_stages={distinct_stages}"}


# 2026-08-15追加(Step 12)。category別の表示順序(固定)。未知のcategory
# (例: 旧形式のfake check、"category"キーの無いテスト用スタブ)は
# "uncategorized"として末尾にまとめて表示する。
CATEGORY_DISPLAY_ORDER = ["core", "bank", "currency", "local_credit",
                          "contract_enforcement", "barter", "regime_choice_economy"]


def _tally_by_category(checks: list) -> dict:
    """checkの結果リスト([{"passed":...,"category":...}, ...])から、
    category別のPASS/FAIL/N/A件数を集計する(表示・集計のための純粋な
    ヘルパー、副作用なし・乱数不使用)。"category"キーが無い場合は
    "uncategorized"として扱う(古い形式のfakeとの互換性のため)。"""
    tally = {}
    for c in checks:
        cat = c.get("category") or "uncategorized"
        bucket = tally.setdefault(cat, {"pass": 0, "fail": 0, "na": 0})
        if c["passed"] is None:
            bucket["na"] += 1
        elif c["passed"]:
            bucket["pass"] += 1
        else:
            bucket["fail"] += 1
    return tally


def run_policy_check(seeds: list, safety_floor: int, *,
                     dependencies: PolicySimulationDependencies) -> None:
    """反証可能な合否基準を実際に判定するコマンド(--policy-check)。
    CHECKPOINT_TURNSの最大値(1920)まで回し、check_trajectory_criteria()の
    結果をPASS/FAIL/N/Aで表示する。

    2026-08-14追記(ユーザーが持ち込んだ外部レビュー指摘・P0-1/P0-2への対応):
    (a) 基準1件あたりの分母(=基準の総数、check_trajectory_criteria参照)は
    走行によらず常に一定になるよう固定した。死亡等でデータが無い基準は
    N/Aとして数え、PASS/FAILの分母には含めない——「死亡した走行のほうが
    見かけ上FAILが少ない」という誤解を防ぐ。
    (b) FAILが1件でもあればCI等で検知できるよう、終了コード1で終了する。

    2026-08-15追加(Step 12、`--policy-check`の制度レジーム対応再設計):
    (c) category別のPASS/FAIL/N/A内訳を追加表示する(総合PASS数だけで
    新旧baselineを比較しないため)。
    (d) 全run分のenforcement_stageを集めてcheck_enforcement_convergence()
    (走行横断の収束判定)を1回だけ実行し、他のcategory="contract_enforcement"
    基準と同じ扱いで集計・表示する。
    (e) 死亡・生存はdeath_ages/survivedという別々の変数に明確に分けたまま
    (元から分離されていた)——化けたコンソール出力を字面で読み違えて数値を
    取り違えた実例(2026-08-15)があったため、この分離をテストでも保証する
    (test_characterization.py参照)。"""
    deps = dependencies
    max_turns = max(deps.checkpoint_turns)
    print(f"=== 合否基準チェック (turns={max_turns}, seeds={seeds}, "
          f"safety_floor={safety_floor}) ===\n")
    total_pass = total_fail = total_na = 0
    death_ages = []   # 2026-08-14追加(ユーザー提案「平均寿命を指標に追加したい」)
    survived = 0      # T{max_turns}まで死亡せずに到達した数(=打ち切りデータ)
    all_checks = []   # category別集計用(2026-08-15追加)
    run_results = []  # check_enforcement_convergence用に全run分を保持(2026-08-15追加)
    for seed in seeds:
        print(f"--- seed={seed} ---")
        deps.seed_fn(seed)
        shared_talent = deps.choice_fn(deps.traits)
        for p in deps.policy_vectors:
            r = (deps.simulate_policy_fn(p, max_turns, seed, safety_floor, shared_talent)
                if deps.simulate_policy_fn else
                simulate_policy(p, max_turns, seed, safety_floor, shared_talent, dependencies=deps))
            checks = (deps.check_trajectory_criteria_fn(r)
                     if deps.check_trajectory_criteria_fn else
                     check_trajectory_criteria(r, dependencies=deps))
            print(f"  {deps.policy_vectors[p]['label']}({p}):")
            for c in checks:
                if c["passed"] is None:
                    mark = "N/A "
                    total_na += 1
                elif c["passed"]:
                    mark = "PASS"
                    total_pass += 1
                else:
                    mark = "FAIL"
                    total_fail += 1
                print(f"    [{mark}] {c['name']}: {c['detail']}")
            all_checks.extend(checks)
            run_results.append(r)
            if r.get("death_turn") is not None:
                death_ages.append(deps.age_at_fn(r["death_turn"]))
            else:
                survived += 1
        print()

    # 2026-08-15追加(Step 12): 走行横断の収束判定(全seed×全policy分の
    # enforcement_stageをまとめて見る、check_trajectory_criteria()では
    # 判定できない基準)。他の基準と同じ集計・表示ルールに合流させる。
    convergence_check = (deps.check_enforcement_convergence_fn(run_results)
                         if deps.check_enforcement_convergence_fn else
                         check_enforcement_convergence(run_results))
    print("--- 走行横断の基準 ---")
    if convergence_check["passed"] is None:
        mark = "N/A "
        total_na += 1
    elif convergence_check["passed"]:
        mark = "PASS"
        total_pass += 1
    else:
        mark = "FAIL"
        total_fail += 1
    print(f"    [{mark}] {convergence_check['name']}: {convergence_check['detail']}\n")
    all_checks.append(convergence_check)

    total_checked = total_pass + total_fail + total_na
    print(f"=== 合計: PASS {total_pass} / FAIL {total_fail} / N/A {total_na} "
          f"(分母{total_checked}件、基準数×{len(seeds)}seed×{len(deps.policy_vectors)}方針+"
          f"走行横断1件で固定) ===")

    # 2026-08-15追加(Step 12): category別の内訳(総合PASS数だけで新旧
    # baselineを比較しないため)。
    tally = _tally_by_category(all_checks)
    breakdown_parts = []
    for cat in CATEGORY_DISPLAY_ORDER + sorted(set(tally) - set(CATEGORY_DISPLAY_ORDER)):
        if cat not in tally:
            continue
        b = tally[cat]
        breakdown_parts.append(f"{cat}: PASS{b['pass']}/FAIL{b['fail']}/N{b['na']}")
    print("内訳(category別): " + " / ".join(breakdown_parts))

    # 寿命の集計。生存した分は「その年齢時点でまだ死んでいない」という
    # 打ち切りデータ(censored)であり、真の寿命はそれ以上——平均寿命は
    # 死亡したケースだけで計算し、生存数は別に添える(見た目上の平均を
    # 「1920ターン到達で頭打ち」に引きずられさせないため)。
    total_runs = len(death_ages) + survived
    if death_ages:
        avg_age = sum(death_ages) / len(death_ages)
        print(f"寿命: 死亡{len(death_ages)}/{total_runs}件"
              f"(平均寿命{avg_age:.1f}歳、最短{min(death_ages):.1f}歳、"
              f"最長{max(death_ages):.1f}歳) / "
              f"生存{survived}/{total_runs}件"
              f"(T{max_turns}=年齢{deps.age_at_fn(max_turns):.0f}歳まで到達・打ち切り)")
    else:
        print(f"寿命: 全{total_runs}件が生存"
              f"(T{max_turns}=年齢{deps.age_at_fn(max_turns):.0f}歳まで到達、死亡0件)")
    if total_fail:
        deps.exit_fn(1)


def run_policy_probe(turns: int, seeds: list, safety_floor: int, *,
                     dependencies: PolicySimulationDependencies) -> None:
    deps = dependencies
    print(f"=== 方針ベクトルのオフライン検証 (turns={turns}, seeds={seeds}, "
          f"safety_floor={safety_floor}) ===")
    print("ollama は呼ばない。経済とコスト抽選のみを回して選択ロジックだけを見る。\n")
    all_keys, totals = [], {p: {} for p in deps.policy_vectors}
    agree_turns = disagree_turns = 0
    pairwise = {}
    for seed in seeds:
        print(f"--- seed={seed} ---")
        # 3方針の比較を公平にするため、同じseed内では同じ才能を使う
        # (2026-08-13、検証装置の一本化。才能によって成長の伸びが変わるため)。
        deps.seed_fn(seed)
        shared_talent = deps.choice_fn(deps.traits)
        results = {p: (deps.simulate_policy_fn(p, turns, seed, safety_floor, shared_talent)
                      if deps.simulate_policy_fn else
                      simulate_policy(p, turns, seed, safety_floor, shared_talent, dependencies=deps))
                  for p in deps.policy_vectors}
        for p, r in results.items():
            for k, v in r["counts"].items():
                totals[p][k] = totals[p].get(k, 0) + v
                if k not in all_keys:
                    all_keys.append(k)
            bankrupt = any(v < 0 for v in r["min_seen"].values())
            print(f"  {deps.policy_vectors[p]['label']:5s}({p:9s}) 選択: "
                  + ", ".join(f"{k}×{v}" for k, v in sorted(r["counts"].items()))
                  + f"\n        最終資源 {deps.format_resources_fn(r['resources'])}"
                  f" / 期間中の最小値 {deps.format_resources_fn(r['min_seen'])}"
                  f" / 破産 {'あり' if bankrupt else 'なし'}"
                  f" / 生存制約が第一希望を却下 {r['overridden']}回"
                  f"\n        moneyの実質値 {r['real_money']:.1f}"
                  f" / 銀行の帳簿 {r['bank_money']:+d}"
                  f" / 賃金債券のmoney型返済 {r['bank_repaid_count']}回"
                  f" / 通貨危機 {r['bank_crisis_count']}回"
                  f" / 銀行trust {r['bank_trust']:.1f}"
                  f"\n        特性(才能={deps.trait_ja[r['talent']]}) "
                  f"{deps.format_traits_fn(r['traits'], r['talent'])}"
                  f" / 成長発火 {r['fires']}回 / 顔なじみ {len(r['npcs'])}人")
        # 反実仮想の一致率は、どの方針の軌跡上で測っても同じ形式で取れる。
        # ここでは cautious の軌跡を基準にする(3方針とも全ターン分を記録済み)。
        for row in results["cautious"]["agree_log"]:
            if row["agree"]:
                agree_turns += 1
            else:
                disagree_turns += 1
            for a in deps.policy_vectors:
                for b in deps.policy_vectors:
                    if a < b:
                        key = f"{a}/{b}"
                        pairwise.setdefault(key, [0, 0])
                        pairwise[key][row["picks"][a] != row["picks"][b]] += 1
        print()

    print("=== 合計(全seed) ===")
    for p in deps.policy_vectors:
        print(f"  {deps.policy_vectors[p]['label']:5s}({p:9s}): "
              + ", ".join(f"{k}×{totals[p].get(k, 0)}" for k in sorted(all_keys)))
    n = agree_turns + disagree_turns
    print(f"\n同じ(状態,選択肢)で3方針の選択が割れたターン: {disagree_turns}/{n} "
          f"({disagree_turns / n * 100:.0f}%)")
    for key, (same, diff) in sorted(pairwise.items()):
        print(f"  {key}: 不一致 {diff}/{same + diff} ({diff / (same + diff) * 100:.0f}%)")


def collect_visualize_trace(seed: int, policy_name: str, turns: int, safety_floor: int,
                            talent: str, *, world_mode: bool = True,
                            resume_state: dict = None, trace: dict = None,
                            include_resume_state: bool = False,
                            initial_population: int = None,
                            dependencies: PolicySimulationDependencies) -> dict:
    """1本のrunを可視化データへまとめる、ファイルI/Oを持たない収集層。

    可視化は「一人生涯の成績表」ではなく世界を眺める用途なので、既定で個人死亡後
    も継続する。policy-checkのsimulate_policy既定値は従来どおりFalseのまま。
    """
    if turns < 1:
        raise ValueError("turns must be at least 1")
    deps = dependencies
    if trace is None:
        trace = {"turns": [], "settlements": [], "npc_introductions": [],
                 "npc_events": [], "character_events": [],
                 "population_events": [], "trade_events": [],
                 "resident_events": [], "spatial_keyframes": [],
                 "organization_events": [], "organization_effects": [],
                 "household_goods_events": [],
                 "household_agency_events": [],
                 "resident_relationship_events": []}
    else:
        # 永続水槽は過去のtraceへ今回区間を追記する。呼び出し側が古いschemaの
        # traceを渡しても、追加済みのイベントstreamだけ安全に補う。
        for name in ("turns", "settlements", "npc_introductions",
                     "npc_events", "character_events", "population_events",
                     "trade_events", "resident_events", "spatial_keyframes",
                     "organization_events", "organization_effects",
                     "household_goods_events", "household_agency_events",
                     "resident_relationship_events"):
            trace.setdefault(name, [])
    resume_kwargs = ({"resume_state": resume_state}
                     if resume_state is not None else {})
    if initial_population is not None:
        resume_kwargs["initial_population"] = initial_population
    r = (deps.simulate_policy_fn(
            policy_name, turns, seed, safety_floor, talent, trace=trace,
            continue_world=world_mode, **resume_kwargs)
        if deps.simulate_policy_fn else
        simulate_policy(policy_name, turns, seed, safety_floor, talent,
                        dependencies=deps, trace=trace,
                        continue_world=world_mode, **resume_kwargs))
    data = {
        "schema_version": VISUALIZE_TRACE_SCHEMA_VERSION,
        "policy": policy_name, "seed": seed, "talent": r["talent"], "turns": turns,
        "death_turn": r["death_turn"],
        "final": {
            "resources": r["resources"], "traits": r["traits"],
            "bank_trust": r["bank_trust"], "bank_stage": r["bank_stage"],
            "currency_confidence": r["currency_confidence"], "currency_stage": r["currency_stage"],
            "community_trust": r["community_trust"], "local_credit_stage": r["local_credit_stage"],
            "enforcement_capacity": r["enforcement_capacity"], "enforcement_stage": r["enforcement_stage"],
            "food": r["food"], "medicine": r["medicine"], "shelter": r["shelter"], "tools": r["tools"],
            "provisioning_scale": r.get("provisioning_scale", 1.0),
            "demand_scales_by_good": r.get("demand_scales_by_good", {}),
            "goods_coverage_by_good": r.get("goods_coverage_by_good", {}),
            "production_capacity": r["production_capacity"], "barter_stage": r["barter_stage"],
        },
        "counts": r["counts"],
        "institution_trajectories": r["institution_trajectories"],
        "npcs": r["npcs"],
        "npc_death_count": r.get("npc_death_count", 0),
        "contracts": r["contracts"],
        "agree_log": r["agree_log"],
        "trace": trace,
    }
    if world_mode:
        # 外部のsimulate_policy_fnや旧checkpointが年齢cohort導入前の
        # settlementを返しても、可視化境界で人口を増減させず現行schemaへ上げる。
        normalized_settlements = {
            settlement_id: upgrade_settlement_demography(row)
            for settlement_id, row in r["settlements"].items()
        }
        data.update({
            "world_mode": True,
            "world_extinct": r["world_extinct"],
            "world_extinct_turn": r["world_extinct_turn"],
            "character_death_count": r["character_death_count"],
            "generation": r["generation"],
            "settlement_states": normalized_settlements,
            "settlement_network_version": r.get("settlement_network_version", 0),
            "focus_settlement_id": r.get(
                "focus_settlement_id", HOME_SETTLEMENT_ID),
            "migration_total": r.get("migration_total", 0),
            "trade_volume_total": r.get("trade_volume_total", 0.0),
            "trade_event_count": r.get("trade_event_count", 0),
            "resident_registry": r.get("resident_registry", {}),
            "spatial_state": r.get("spatial_state", {}),
            "activity_community_ledger": r.get(
                "activity_community_ledger", {}),
            "activity_community_accounting_version": r.get(
                "activity_community_accounting_version", 0),
            "activity_economy_state": r.get(
                "activity_economy_state", initial_activity_economy_state()),
            "organization_state": r.get(
                "organization_state", initial_organization_state()),
            "household_needs_state": r.get(
                "household_needs_state", {}),
            "household_goods_state": r.get(
                "household_goods_state", initial_household_goods_state()),
            "household_agency_state": r.get(
                "household_agency_state", initial_household_agency_state()),
            "resident_relationship_state": r.get(
                "resident_relationship_state",
                initial_resident_relationship_state()),
            "focus_resident_id": r.get("focus_resident_id"),
        })
        focus_id = r.get("focus_settlement_id", HOME_SETTLEMENT_ID)
        focus = normalized_settlements[focus_id]
        data["final"].update({
            "population": focus["population"],
            "reproductive_population": focus["reproductive_population"],
            "productive_population": focus["productive_population"],
            "age_cohorts": dict(focus["age_cohorts"]),
            "total_population": sum(
                row["population"] for row in normalized_settlements.values()),
            "total_productive_population": sum(
                row["productive_population"]
                for row in normalized_settlements.values()),
            "population_stage": r.get("population_stage", focus["stage"]),
            "generation": r["generation"],
            "character_alive": r["character_alive"],
            "world_extinct": r["world_extinct"],
        })
        if include_resume_state:
            data["resume_state"] = r["resume_state"]
    return data


def run_visualize_trace(seed: int, policy_name: str, turns: int, safety_floor: int,
                        talent: str, output_path: str, *, world_mode: bool = True,
                        initial_population: int = None,
                        dependencies: PolicySimulationDependencies) -> dict:
    """collect_visualize_trace()の結果をJSONへ保存する既存CLI向けI/O層。"""
    data = collect_visualize_trace(
        seed, policy_name, turns, safety_floor, talent, world_mode=world_mode,
        initial_population=initial_population,
        dependencies=dependencies)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    trace = data["trace"]
    print(f"=== 可視化データを書き出しました: {output_path} "
          f"(turns記録={len(trace['turns'])}件, "
          f"settlements={len(trace['settlements'])}件, "
          f"npc_introductions={len(trace['npc_introductions'])}件, "
          f"character_events={len(trace.get('character_events', []))}件) ===")
    return data


# ==============================================================================
# 特性の長期シミュレーション(ollamaを呼ばない)
# ==============================================================================
# 実測の壁: D_base=0.12・f≈0.167 だと1ターンあたりの純変化が 0.05〜0.1 程度しかなく、
# **平衡値(70〜90)に達するには数百〜数千ターンかかる**。ollama を使う実走は
# 1回50ターンが上限(NAS本番と共用、CLAUDE.md)なので、平衡値の確認は
# 「実走で f を実測 → その f を入れて式だけを長期に回す」に分ける。
# 式そのものは実走とまったく同じ compute_trait_step() を使う(二重実装にしない)。

def equilibrium(f: float, g_base: float, m_talent: float, d_base: float) -> float:
    """平衡条件 f×G×M×(1-T/100) = (1-f)×D_base の解析解。"""
    if f <= 0 or g_base <= 0:
        return 0.0
    return 100.0 * (1.0 - (1.0 - f) * d_base / (f * g_base * m_talent))


def simulate_forced(f: float, g_base: float, m_talent: float, d_base: float,
                    turns: int, trials: int, start: float = 50.0, *,
                    dependencies: TraitSimulationDependencies) -> float:
    """「毎ターン確率fで、常に同じ型で発火する」特性を1つだけ回す。
    解析解の答え合わせ用(式の実装と解析解が食い違っていないかの対照)。"""
    deps = dependencies
    total = 0.0
    for _ in range(trials):
        t = start
        for _ in range(turns):
            if deps.random_fn() < f:
                t = min(deps.trait_max, t + g_base * m_talent * (1.0 - t / 100.0))
            else:
                t = max(deps.trait_min, t - d_base)
        total += t
    return total / trials


def simulate_mixed(turns: int, trials: int, dist: list, no_theme_rate: float,
                   talent: str, *, dependencies: TraitSimulationDependencies) -> dict:
    """実走と同じ構造(4特性・題材ランダム・3択)で回す。
    compute_trait_step() をそのまま使うので、式の二重実装にはならない。"""
    deps = dependencies
    keys = ["money", "labor", "social"]
    snapshots = {}
    # 1ターン=1ヶ月なので、120=10年、360=30年、960=80年。人生の節目で見る。
    marks = sorted({m for m in (50, 120, 360, 600, 960, turns) if m <= turns})
    acc = {m: {t: 0.0 for t in deps.traits} for m in marks}
    for _ in range(trials):
        traits = dict(deps.initial_traits)
        used_places, used_concerns = [], []
        for turn in range(1, turns + 1):
            if deps.random_fn() < no_theme_rate:
                theme_trait, kind = None, "settlement"
            else:
                _, concern = deps.pick_theme_fn(used_places, used_concerns)
                theme_trait, kind = deps.theme_concern_traits[concern], "normal"
                used_places[:] = used_places[-8:]
                used_concerns[:] = used_concerns[-8:]
            choice_key = deps.choices_fn(keys, weights=dist, k=1)[0]
            delta, _fired = deps.compute_trait_step_fn(traits, talent, theme_trait,
                                               choice_key, turn, kind)
            for k, v in delta.items():
                traits[k] += v
            if turn in acc:
                for t in deps.traits:
                    acc[turn][t] += traits[t]
    for m in marks:
        snapshots[m] = {t: acc[m][t] / trials for t in deps.traits}
    return snapshots


def run_simulation(turns: int, trials: int, dist: list, no_theme_rate: float,
                   seed, f_override, *, dependencies: TraitSimulationDependencies) -> None:
    deps = dependencies
    if seed is not None:
        deps.seed_fn(seed)
    s = sum(dist)
    dist = [d / s for d in dist]
    # 2026-08-13、G_BASEにsocialを追加したのに合わせて、p_labor_or_moneyを
    # p_growth_eligible(labor/money/social すべて)に改める(旧名のままだと
    # social型の成長寄与が計算から漏れる)。
    p_growth_eligible = dist[0] + dist[1] + dist[2]
    # 1特性あたりの発火頻度。題材24項目のうち6項目が該当 = 1/4。
    f_auto = (1.0 - no_theme_rate) * 0.25 * p_growth_eligible
    f = f_override if f_override is not None else f_auto
    print("=== 特性の長期シミュレーション(ollama不使用)===")
    print("★ 非推奨(2026-08-13、5回目opusレビュー指摘): このコマンドは選択分布を")
    print("  turns全体で固定するため、health低下に伴う分布の変化(money/laborへの")
    print("  回帰)を反映できず、--policy-probe/--policy-checkと大きく違う結果を")
    print("  出す(同条件で健康値が3〜10倍違う実測あり)。単発の机上チェック以外の")
    print("  目的では --policy-check を使うこと。\n")
    print(f"D_base={deps.d_base} / G_base={deps.g_base} / B_talent={deps.b_talent} / "
          f"選択分布 money,labor,social={['%.3f' % d for d in dist]} / "
          f"題材なしターン率={no_theme_rate:.3f}")
    print(f"1特性あたりの発火頻度 f = {f:.4f}"
          + ("(指定値)" if f_override is not None else "(選択分布から算出)")
          + f" / 机上の仮定 0.167\n")

    print("--- 4通りの平衡値(解析解 vs 式のモンテカルロ)---")
    print(f"{'条件':<22}{'解析解':>10}{'MC実測':>10}{'f_crit':>12}")
    for label, g, m in [("labor型・才能なし", deps.g_base["labor"], 1.0),
                        ("labor型・才能あり", deps.g_base["labor"], 1.0 + deps.b_talent),
                        ("money型・才能なし", deps.g_base["money"], 1.0),
                        ("money型・才能あり", deps.g_base["money"], 1.0 + deps.b_talent)]:
        an = (deps.equilibrium_fn(f, g, m, deps.d_base) if deps.equilibrium_fn
             else equilibrium(f, g, m, deps.d_base))
        mc = (deps.simulate_forced_fn(f, g, m, deps.d_base, turns, max(50, trials // 4))
             if deps.simulate_forced_fn else
             simulate_forced(f, g, m, deps.d_base, turns, max(50, trials // 4), dependencies=deps))
        # 平衡値が0になる臨界の発火頻度 f_crit = D/(D + G×M)。
        # 実測のfがこれを下回ると「死の螺旋」(ゼロ張り付き)になる。
        f_crit = deps.d_base / (deps.d_base + g * m)
        print(f"{label:<22}{an:>10.1f}{mc:>10.1f}{f_crit:>12.4f}")
    print("  ※解析解が負のときはゼロに張り付く(=「死の螺旋」)。"
          "f_crit は平衡値が0になる臨界の発火頻度\n")

    print(f"--- 実走と同じ構造での推移(4特性混在、{trials}試行の平均)---")
    for talent in deps.traits:
        snaps = (deps.simulate_mixed_fn(turns, trials, dist, no_theme_rate, talent)
                if deps.simulate_mixed_fn else
                simulate_mixed(turns, trials, dist, no_theme_rate, talent, dependencies=deps))
        print(f"  才能={deps.trait_ja[talent]}")
        for m in sorted(snaps):
            print(f"    T{m:<4}(年齢{deps.age_at_fn(m):4.0f}): "
                  + " / ".join(f"{deps.trait_ja[t]}:{snaps[m][t]:5.1f}"
                               + ("*" if t == talent else " ") for t in deps.traits))
