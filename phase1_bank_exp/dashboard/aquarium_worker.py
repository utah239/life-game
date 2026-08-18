#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""永続デジタル水槽を開始・再開する隔離worker。

手動診断用のJSON/単発pickleに加え、同じworldだけを連続更新するpickle loopを持つ。
loopはworld開始ごとに新しいプロセスへ交換されるため、物々交換パラメータをgameの
import前に適用する既存境界と、別world間でグローバル定数/RNGを漏らさない性質を保つ。
"""
import copy
import json
from pathlib import Path
import pickle
import sys
import time

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from dashboard.build_dashboard import (  # noqa: E402
    activity_labor_summary,
    build_dashboard_data,
    organization_labor_summary,
)
from dashboard.experiment_parameters import (  # noqa: E402
    apply_barter_overrides,
    validate_request,
)
from institutions import barter  # noqa: E402
from institutions.production_practice import (  # noqa: E402
    normalize_production_practice,
    population_weighted_production_practice,
    production_productivity_factors,
)
from institutions.residents import (  # noqa: E402
    active_household_count,
    anonymous_population_count,
    compact_registry as compact_resident_registry,
    living_population_count,
    living_residents,
    resident_age_years,
)
from institutions.spatial import compact_spatial_state  # noqa: E402


AQUARIUM_WORLD_SCHEMA_VERSION = 1
MAX_ADVANCE_MONTHS = 5000
DEFAULT_HISTORY_MONTHS = 2400
MIN_HISTORY_MONTHS = 120
MAX_HISTORY_MONTHS = 120000


def _history_months(raw) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("history_months must be an integer") from None
    if not MIN_HISTORY_MONTHS <= value <= MAX_HISTORY_MONTHS:
        raise ValueError(
            f"history_months must be between {MIN_HISTORY_MONTHS} and {MAX_HISTORY_MONTHS}")
    return value


def _compact_observation_window(result: dict, checkpoint: dict,
                                history_months: int) -> None:
    """世界の力学を変えず、表示・診断だけの古い詳細を観察窓から落とす。

    通し番号、累積count、Stage滞在時間、初回到達、資源最小値はcheckpointへ
    残す。終了契約は将来の判定に使われないので落とせる一方、NPCは疎遠後も
    価格modifierの抽選母集団に残る現行仕様なので、checkpointからは落とさない。
    """
    completed = int(checkpoint["completed_turn"])
    cutoff = max(1, completed - history_months + 1)
    result["history_start_turn"] = cutoff
    result["contract_total"] = checkpoint.get(
        "contract_sequence", len(checkpoint["contracts"]))
    result["npc_total"] = checkpoint.get(
        "npc_sequence", len(checkpoint["npcs"]))
    if cutoff <= 1:
        return

    for name, stream in result["trace"].items():
        if name == "spatial_keyframes":
            # 観察窓の先頭月を補間する左側anchorだけは1件残す。
            prior = [row for row in stream if int(row["turn"]) < cutoff]
            stream[:] = ((prior[-1:] if prior else []) + [
                row for row in stream if int(row["turn"]) >= cutoff])
        else:
            stream[:] = [
                row for row in stream
                if int(row.get("turn", completed)) >= cutoff]

    recent_agreement = [
        row for row in checkpoint["agree_log"] if int(row["turn"]) >= cutoff]
    checkpoint["agree_log"] = recent_agreement
    result["agree_log"] = copy.deepcopy(recent_agreement)

    # 終了契約は清算traceと累積通し番号に履歴を残し、再開状態にはopenだけを置く。
    open_contracts = [
        contract for contract in checkpoint["contracts"]
        if contract.get("status") == "open"]
    checkpoint["contracts"] = open_contracts
    result["contracts"] = copy.deepcopy(open_contracts)
    open_counterparties = {
        contract.get("counterparty") for contract in open_contracts
        if contract.get("counterparty")}

    # 画面へ渡す観察用NPCだけを最近の関係へ絞る。再開checkpointでは、疎遠な
    # NPCもcompute_normal_money_modifierの抽選母集団なので全件を保持する。
    # ここをcheckpointまで削ると、乱数のchoice対象と価格が変わってしまう。
    def keep_npc(key, npc):
        retire_turn = npc.get("retire_turn")
        died_turn = npc.get("died_turn")
        return (npc.get("role") != "acquaintance" or not retire_turn
                or retire_turn >= cutoff or key in open_counterparties
                or npc.get("name") in open_counterparties
                or (died_turn is not None and died_turn >= cutoff))

    observable_npcs = {
        key: npc for key, npc in checkpoint["npcs"].items()
        if keep_npc(key, npc)}
    result["npcs"] = copy.deepcopy(observable_npcs)

    # 住民台帳は将来の力学で生存者だけを参照する。観察窓より古い死者と閉鎖
    # 世帯は累積countへ畳み、生存者・ID通し番号・現在世帯を保持する。
    if checkpoint.get("resident_registry"):
        compacted_registry = compact_resident_registry(
            checkpoint["resident_registry"], cutoff)
        checkpoint["resident_registry"] = compacted_registry
        result["resident_registry"] = copy.deepcopy(compacted_registry)
        if checkpoint.get("spatial_state"):
            compacted_spatial = compact_spatial_state(
                checkpoint["spatial_state"], compacted_registry)
            checkpoint["spatial_state"] = compacted_spatial
            result["spatial_state"] = copy.deepcopy(compacted_spatial)

    organization_state = checkpoint.get("organization_state")
    if isinstance(organization_state, dict):
        organization_state["events"] = [
            row for row in organization_state.get("events", ())
            if int(row.get("turn", completed)) >= cutoff]
        retained = {}
        for organization_id, row in organization_state.get(
                "organizations", {}).items():
            ended_turn = row.get("ended_turn")
            if (not row.get("active", True) and ended_turn is not None
                    and int(ended_turn) < cutoff):
                continue
            for history_name in ("form_history", "operating_history"):
                history = list(row.get(history_name, ()))
                prior = [item for item in history
                         if int(item.get("turn", 0)) < cutoff]
                row[history_name] = ((prior[-1:] if prior else []) + [
                    item for item in history
                    if int(item.get("turn", 0)) >= cutoff])
            retained[organization_id] = row
        organization_state["organizations"] = retained
        checkpoint["organization_state"] = organization_state
        result["organization_state"] = copy.deepcopy(organization_state)

    # pick_themeが参照するのは末尾5件だけ。全履歴を保持しても次の選択には
    # 影響しないため、再開状態をその必要量へ縮める。
    checkpoint["used_places"] = checkpoint["used_places"][-5:]
    checkpoint["used_concerns"] = checkpoint["used_concerns"][-5:]

    tracking_names = (
        "bank_tracking", "currency_tracking", "local_credit_tracking",
        "enforcement_tracking", "barter_tracking", "population_tracking")
    result_names = (
        "bank", "currency", "local_credit", "contract_enforcement",
        "barter", "population")
    for checkpoint_name, result_name in zip(tracking_names, result_names):
        tracking = checkpoint.get(checkpoint_name)
        if tracking is not None:
            tracking["transition_history"] = [
                row for row in tracking.get("transition_history", [])
                if int(row["turn"]) >= cutoff]
        public_tracking = result["institution_trajectories"].get(result_name)
        if public_tracking is not None:
            public_tracking["transition_history"] = [
                row for row in public_tracking.get("transition_history", [])
                if int(row["turn"]) >= cutoff]


def _trace_data_from_result(config: dict, result: dict,
                            history_months: int) -> tuple[dict, dict, dict]:
    checkpoint = result.pop("resume_state")
    compact_started = time.perf_counter()
    _compact_observation_window(result, checkpoint, history_months)
    compact_seconds = time.perf_counter() - compact_started
    result["experiment_parameters"] = config["values"]
    dashboard_started = time.perf_counter()
    dashboard_data = build_dashboard_data(result, config["run"]["bins"])
    dashboard_seconds = time.perf_counter() - dashboard_started
    dashboard_data["meta"]["aquarium_live"] = True
    dashboard_data["experiment_parameters"] = config["values"]
    return checkpoint, dashboard_data, {
        "compact_observation_seconds": round(compact_seconds, 6),
        "build_dashboard_seconds": round(dashboard_seconds, 6),
    }


def _run_segment(config: dict, target_turn: int, *, checkpoint: dict = None,
                 trace: dict = None,
                 history_months: int = DEFAULT_HISTORY_MONTHS) -> tuple[dict, dict, dict, dict]:
    # game/engineがbarter定数を値importする前に適用する。workerはこの1区間で
    # 終了するので、次の区間は保存済みconfigから同じ値を改めて適用する。
    simulation_started = time.perf_counter()
    apply_barter_overrides(barter, config["barter_overrides"])
    import game

    run = config["run"]
    data = game.collect_visualize_trace(
        run["seed"], run["policy"], target_turn, run["safety_floor"],
        run["talent"], world_mode=True, resume_state=checkpoint, trace=trace,
        include_resume_state=True,
        initial_population=run["initial_population"])
    simulation_seconds = time.perf_counter() - simulation_started
    checkpoint, dashboard_data, timings = _trace_data_from_result(
        config, data, history_months)
    timings["simulation_seconds"] = round(simulation_seconds, 6)
    return checkpoint, data["trace"], dashboard_data, timings


def _summary(checkpoint: dict, history_months: int) -> dict:
    focus_id = checkpoint.get("focus_settlement_id", "home")
    focus = checkpoint["settlements"][focus_id]
    focus_economy = focus.get("local_economy", {})
    focus_scale = barter.normalize_provisioning_scale(
        focus_economy.get("provisioning_scale"))
    focus_demand_scales = dict(
        focus_economy.get("demand_scales_by_good", {}))
    focus_coverage = barter.goods_coverage(
        focus_economy.get("food", 0.0),
        focus_economy.get("medicine", 0.0),
        focus_economy.get("shelter", 0.0),
        focus_economy.get("tools", 0.0), focus_scale,
        focus_demand_scales)
    world_scale = sum(barter.normalize_provisioning_scale(
        row.get("local_economy", {}).get("provisioning_scale"))
        for row in checkpoint["settlements"].values())
    world_goods = {
        good: sum(float(row.get("local_economy", {}).get(good, 0.0))
                  for row in checkpoint["settlements"].values())
        for good in ("food", "medicine", "shelter", "tools")}
    needs_state = checkpoint.get("household_needs_state", {})
    world_demand_scales = dict(
        needs_state.get("world_demand_scale_by_good", {}))
    world_demand_quantities = dict(
        needs_state.get("world_demand_quantity_by_good", {}))
    world_coverage = barter.goods_coverage(
        world_goods["food"], world_goods["medicine"],
        world_goods["shelter"], world_goods["tools"], world_scale,
        world_demand_scales or None)
    focus_practice = normalize_production_practice(
        focus.get("local_economy", {}).get(
            "production_practice_by_good"))
    world_practice = population_weighted_production_practice(
        (row.get("population", 0), row.get("local_economy", {}).get(
            "production_practice_by_good"))
        for row in checkpoint["settlements"].values())
    settlement_rows = [{
        "id": sid, "name": row.get("name", sid),
        "population": row["population"],
        "reproductive_population": row["reproductive_population"],
        "productive_population": row.get("productive_population", 0),
        "age_cohorts": dict(row.get("age_cohorts", {})),
        "stage": row["stage"],
        "community_trust": row.get("local_economy", {}).get(
            "community_trust", checkpoint.get("community_trust", 50.0)),
        "local_credit_stage": row.get("local_economy", {}).get(
            "local_credit_stage", checkpoint.get("local_credit_stage", 0)),
        "barter_stage": row.get("local_economy", {}).get(
            "barter_stage", checkpoint.get("barter_stage", 0)),
        "provisioning_scale": barter.normalize_provisioning_scale(
            row.get("local_economy", {}).get("provisioning_scale")),
        "demand_scales_by_good": dict(
            row.get("local_economy", {}).get(
                "demand_scales_by_good", {})),
        "goods_coverage_by_good": barter.goods_coverage(
            row.get("local_economy", {}).get("food", 0.0),
            row.get("local_economy", {}).get("medicine", 0.0),
            row.get("local_economy", {}).get("shelter", 0.0),
            row.get("local_economy", {}).get("tools", 0.0),
            row.get("local_economy", {}).get("provisioning_scale", 1.0),
            row.get("local_economy", {}).get(
                "demand_scales_by_good")),
        "production_practice_by_good": normalize_production_practice(
            row.get("local_economy", {}).get(
                "production_practice_by_good")),
    } for sid, row in checkpoint["settlements"].items()]
    registry = checkpoint.get("resident_registry", {})
    focus_resident_id = checkpoint.get("focus_resident_id")
    focus_resident = registry.get("residents", {}).get(
        focus_resident_id, {})
    focus_household = registry.get("households", {}).get(
        focus_resident.get("household_id"), {})
    household_activity_total = sum(
        int(row.get("activity_count", 0))
        for row in registry.get("households", {}).values())
    spatial_state = checkpoint.get("spatial_state", {})
    activity_ledger = checkpoint.get("activity_community_ledger", {})
    activity_economy = checkpoint.get("activity_economy_state", {})
    activity_economy_communities = list(
        activity_economy.get("communities", {}).values())
    activity_labor_by_good = activity_labor_summary(
        activity_economy_communities)
    activity_required_worker_total = sum(
        int(row.get("required_worker_count", 0))
        for row in activity_economy_communities)
    activity_unassigned_productive_total = sum(
        int(row.get("unassigned_productive_population", 0))
        for row in activity_economy_communities)
    activity_labor_constrained_community_count = sum(
        int(row.get("active_worker_count", 0))
        < int(row.get("required_worker_count", 0))
        for row in activity_economy_communities)
    activity_labor_factors = [
        float(row.get("background_production_labor_factor", 1.0))
        for row in activity_economy_communities
        if int(row.get("required_worker_count", 0)) > 0]
    active_organization_rows = [
        row for row in checkpoint.get(
            "organization_state", {}).get("organizations", {}).values()
        if row.get("active", True)]
    organization_labor = organization_labor_summary(
        active_organization_rows)
    organization_mean_member_continuity = (
        sum(float(row.get("member_continuity", 1.0))
            for row in active_organization_rows)
        / len(active_organization_rows)
        if active_organization_rows else 1.0)
    organization_asset_claim_totals = {
        field: round(sum(float(row.get(
            "asset_claims", {}).get(field, 0.0))
            for row in active_organization_rows), 6)
        for field in ("food", "medicine", "shelter", "tools")}
    return {
        "completed_turn": checkpoint["completed_turn"],
        "history_start_turn": max(
            1, checkpoint["completed_turn"] - history_months + 1),
        "generation": checkpoint["generation"],
        "character_death_count": checkpoint["character_death_count"],
        "npc_death_count": checkpoint.get("npc_death_count", 0),
        "focus_settlement_id": focus_id,
        "settlement_count": len(settlement_rows),
        "settlements": settlement_rows,
        "population": focus["population"],
        "reproductive_population": focus["reproductive_population"],
        "productive_population": focus.get("productive_population", 0),
        "total_population": sum(row["population"] for row in settlement_rows),
        "total_productive_population": sum(
            row["productive_population"] for row in settlement_rows),
        "provisioning_scale": focus_scale,
        "demand_scales_by_good": focus_demand_scales,
        "goods_coverage_by_good": focus_coverage,
        "world_provisioning_scale": round(world_scale, 12),
        "world_demand_scales_by_good": world_demand_scales,
        "world_demand_quantities_by_good": world_demand_quantities,
        "world_goods_totals": world_goods,
        "world_goods_coverage_by_good": world_coverage,
        "production_practice_by_good": focus_practice,
        "production_productivity_factors_by_good": (
            production_productivity_factors(focus_practice)),
        "world_production_practice_by_good": world_practice,
        "world_production_productivity_factors_by_good": (
            production_productivity_factors(world_practice)),
        "population_stage": checkpoint["population_stage"],
        "migration_total": checkpoint.get("migration_total", 0),
        "trade_volume_total": checkpoint.get("trade_volume_total", 0.0),
        "trade_event_count": checkpoint.get("trade_event_count", 0),
        "living_resident_count": living_population_count(registry),
        "named_living_resident_count": len(living_residents(registry)),
        "anonymous_resident_count": anonymous_population_count(registry),
        "resident_total": (
            len(registry.get("residents", {}))
            + int(registry.get("archived_resident_count", 0))),
        "household_count": active_household_count(registry),
        "household_activity_total": household_activity_total,
        "activity_site_count": len([
            row for row in spatial_state.get("sites", {}).values()
            if row.get("active", True)]),
        "activity_cluster_count": len(spatial_state.get("clusters", {})),
        "activity_provisional_cluster_count": sum(
            1 for row in spatial_state.get("clusters", {}).values()
            if row.get("provisional", False)),
        "activity_accounting_cluster_count": sum(
            1 for row in spatial_state.get("clusters", {}).values()
            if not row.get("provisional", False)),
        "activity_cluster_lineage_count": len(
            spatial_state.get("cluster_lineages", {})),
        "activity_cluster_event_count": len(
            spatial_state.get("cluster_events", [])),
        "activity_community_count": len(
            activity_ledger.get("communities", {})),
        "activity_community_accounting_version": checkpoint.get(
            "activity_community_accounting_version", 0),
        "activity_worker_total": sum(
            int(activity.get("worker_count", 0))
            for community in activity_economy.get(
                "communities", {}).values()
            for activity in community.get("activities", {}).values()),
        "activity_anonymous_worker_total": sum(
            int(activity.get("anonymous_worker_count", 0))
            for community in activity_economy.get(
                "communities", {}).values()
            for activity in community.get("activities", {}).values()),
        "activity_required_worker_total": activity_required_worker_total,
        "activity_unassigned_productive_total": (
            activity_unassigned_productive_total),
        "activity_labor_constrained_community_count": (
            activity_labor_constrained_community_count),
        "activity_min_labor_factor": round(
            min(activity_labor_factors) if activity_labor_factors else 0.0,
            6),
        "activity_required_workers_by_good": (
            activity_labor_by_good["required"]),
        "activity_workers_by_good": activity_labor_by_good["active"],
        "activity_labor_constrained_communities_by_good": (
            activity_labor_by_good["constrained_communities"]),
        "activity_min_labor_factor_by_good": (
            activity_labor_by_good["min_factor"]),
        "unassigned_activity_account_count": len(
            activity_ledger.get("unassigned_accounts", {})),
        "organization_count": len(active_organization_rows),
        "organization_eligible_workforce_total": (
            organization_labor["eligible"]),
        "organization_workforce_total": organization_labor["active"],
        "organization_named_worker_total": organization_labor["named"],
        "organization_unrepresented_workforce_total": (
            organization_labor["unrepresented"]),
        "organization_workforce_affiliation_ratio": (
            organization_labor["active_to_eligible_ratio"]),
        "organization_mean_member_continuity": round(
            organization_mean_member_continuity, 6),
        "organization_asset_claim_totals": organization_asset_claim_totals,
        "organization_asset_claim_acquisition_totals": dict(
            checkpoint.get("organization_state", {}).get(
                "asset_claim_acquisition_totals", {})),
        "organization_community_distribution_totals": dict(
            checkpoint.get("organization_state", {}).get(
                "community_distribution_totals", {})),
        "focus_resident_id": focus_resident_id,
        "focus_resident_name": focus_resident.get("name"),
        "focus_resident_age": (resident_age_years(
            focus_resident, checkpoint["completed_turn"])
            if focus_resident else None),
        "focus_household_name": focus_household.get("name"),
        "bank_stage": checkpoint["bank_stage"],
        "currency_stage": checkpoint["currency_stage"],
        "local_credit_stage": checkpoint["local_credit_stage"],
        "enforcement_stage": checkpoint["enforcement_stage"],
        "barter_stage": checkpoint["barter_stage"],
        "world_extinct": checkpoint["world_extinct_turn"] is not None,
        "world_extinct_turn": checkpoint["world_extinct_turn"],
    }


def _validated_config(values: dict) -> dict:
    return validate_request({"values": values})


def execute(payload: dict) -> dict:
    execute_started = time.perf_counter()
    if not isinstance(payload, dict):
        raise ValueError("request must be an object")
    action = payload.get("action")
    if action == "start":
        config = _validated_config(payload.get("values", {}))
        history_months = _history_months(payload.get(
            "history_months", DEFAULT_HISTORY_MONTHS))
        try:
            target_turn = int(payload.get(
                "initial_months", config["run"]["turns"]))
        except (TypeError, ValueError):
            raise ValueError("initial_months must be an integer") from None
        if target_turn < 1 or target_turn > MAX_ADVANCE_MONTHS:
            raise ValueError(
                f"initial_months must be between 1 and {MAX_ADVANCE_MONTHS}")
        checkpoint, trace, dashboard_data, timings = _run_segment(
            config, target_turn, history_months=history_months)
        world = {
            "schema_version": AQUARIUM_WORLD_SCHEMA_VERSION,
            "config": {"values": config["values"]},
            "history_months": history_months,
            "checkpoint": checkpoint,
            "trace": trace,
        }
        summary_started = time.perf_counter()
        world["summary"] = _summary(checkpoint, history_months)
        timings["summary_seconds"] = round(
            time.perf_counter() - summary_started, 6)
        timings["total_execute_seconds"] = round(
            time.perf_counter() - execute_started, 6)
        return {
            "world": world, "dashboard_data": dashboard_data,
            "timings": timings}

    if action == "advance":
        source = payload.get("world")
        if not isinstance(source, dict):
            raise ValueError("world must be an object")
        if source.get("schema_version") != AQUARIUM_WORLD_SCHEMA_VERSION:
            raise ValueError("unsupported aquarium world schema")
        # stdinから復号したsourceはこのworker呼び出しだけが所有する。8〜20MiBへ
        # 成長するcheckpoint+trace全体をdeepcopyすると、1か月計算よりコピーの
        # 方が重くなる。rootと追記/compact対象のlistコンテナだけを分離し、
        # checkpoint自体はsimulate_policy._restore_resume_state側の既存deepcopyで
        # 引き続き入力から隔離する。
        prepare_started = time.perf_counter()
        world = dict(source)
        source_trace = source.get("trace", {})
        trace = {
            name: list(stream) for name, stream in source_trace.items()}
        prepare_seconds = time.perf_counter() - prepare_started
        config = _validated_config(world["config"]["values"])
        history_months = _history_months(world.get(
            "history_months", DEFAULT_HISTORY_MONTHS))
        try:
            months = int(payload.get("months"))
        except (TypeError, ValueError):
            raise ValueError("months must be an integer") from None
        if months < 1 or months > MAX_ADVANCE_MONTHS:
            raise ValueError(f"months must be between 1 and {MAX_ADVANCE_MONTHS}")
        checkpoint = world["checkpoint"]
        if checkpoint.get("world_extinct_turn") is not None:
            raise ValueError("an extinct world cannot be advanced")
        target_turn = int(checkpoint["completed_turn"]) + months
        checkpoint, trace, dashboard_data, timings = _run_segment(
            config, target_turn, checkpoint=checkpoint, trace=trace,
            history_months=history_months)
        world["checkpoint"] = checkpoint
        world["trace"] = trace
        summary_started = time.perf_counter()
        world["summary"] = _summary(checkpoint, history_months)
        timings["summary_seconds"] = round(
            time.perf_counter() - summary_started, 6)
        timings["prepare_world_seconds"] = round(prepare_seconds, 6)
        timings["total_execute_seconds"] = round(
            time.perf_counter() - execute_started, 6)
        return {
            "world": world, "dashboard_data": dashboard_data,
            "timings": timings}

    raise ValueError("action must be start or advance")


def run_pickle_loop(input_stream, output_stream) -> None:
    """同一world専用の逐次request loop。

    最初のstart/完全advanceだけがworld全体を受け取り、以後のadvanceは月数だけを
    受け取る。例外後の部分状態を再利用しないよう、error responseを返して終了する。
    """
    retained_world = None
    while True:
        try:
            payload = pickle.load(input_stream)
        except EOFError:
            return
        if payload == {"action": "shutdown"}:
            return
        try:
            if (isinstance(payload, dict)
                    and payload.get("action") == "advance"
                    and "world" not in payload):
                if retained_world is None:
                    raise ValueError(
                        "stateful advance requires an initialized world")
                payload = {
                    "action": "advance", "world": retained_world,
                    "months": payload.get("months"),
                }
            result = execute(payload)
        except Exception as exc:
            pickle.dump({
                "__aquarium_worker_error__":
                    f"{type(exc).__name__}: {exc}",
            }, output_stream, protocol=pickle.HIGHEST_PROTOCOL)
            output_stream.flush()
            return
        retained_world = result["world"]
        pickle.dump(
            result, output_stream, protocol=pickle.HIGHEST_PROTOCOL)
        output_stream.flush()


def main() -> None:
    try:
        if "--pickle-loop" in sys.argv[1:]:
            # AquariumManagerがworldごとに所有する信頼済み子プロセス専用。
            run_pickle_loop(sys.stdin.buffer, sys.stdout.buffer)
        elif "--pickle" in sys.argv[1:]:
            # AquariumManagerが起動した同一ホスト内の信頼済み子プロセス専用。
            # HTTP requestや任意ファイルをpickle境界へ直接渡してはならない。
            payload = pickle.load(sys.stdin.buffer)
            result = execute(payload)
            pickle.dump(
                result, sys.stdout.buffer, protocol=pickle.HIGHEST_PROTOCOL)
        else:
            # 手動診断・既存契約テスト用の可搬JSON境界は残す。
            payload = json.load(sys.stdin)
            result = execute(payload)
            json.dump(
                result, sys.stdout, ensure_ascii=False,
                separators=(",", ":"))
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
