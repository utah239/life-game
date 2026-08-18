# -*- coding: utf-8 -*-
"""イベント列を現在の世界状態へ畳み込む投影層(純粋な reduce)。

段階的モジュール分割(Step 2B、2026-08-14、behavior-preserving refactoring)で
game.py の reduce_state() から切り出した。処理順・計算式・キー名は一切
変更していない——game.py 側の reduce_state() はこのモジュールの
reduce_events() を呼ぶ薄い互換ラッパーになる。

このモジュールは game.py・event_store.py をimportしない。ファイルを直接
開かない、print・LLM・乱数も使わない。game.py に残る動的な依存
(初期資源・初期特性・price_index・effective_npc_trust など、CREATION_RATEの
CLI上書きに影響されうるものを含む)は ProjectionDependencies 経由で
呼び出し側から明示的に受け取る。

銀行・通貨・地域信用・契約執行の状態機械そのもの(bank_stage_next/
currency_stage_next/local_credit_stage_next/enforcement_stage_next等)は
ここでは呼ばない——reduce_events()は「起きたinstitution_transitionイベントを
そのまま状態へ反映するだけ」で、次の遷移を判定する側ではない(既存の
reduce_state()と同じ役割分担)。
"""
from dataclasses import dataclass
from typing import Callable

from institutions.bank import BANK_STAGE_HEALTHY
from institutions.currency import (
    CURRENCY_CONFIDENCE_INITIAL,
    CURRENCY_STAGE_NORMAL,
    CURRENCY_CRISIS_HIT,
)
from institutions.local_credit import (
    LOCAL_CREDIT_TRUST_INITIAL,
    LOCAL_CREDIT_STAGE_HEALTHY,
)
from institutions.contract_enforcement import (
    ENFORCEMENT_CAPACITY_INITIAL,
    ENFORCEMENT_STAGE_INSTITUTIONAL,
)
from institutions.barter import (
    FOOD_STOCK_INITIAL,
    MEDICINE_STOCK_INITIAL,
    SHELTER_DURABILITY_INITIAL,
    TOOLS_DURABILITY_INITIAL,
    PRODUCTION_CAPACITY_INITIAL,
    PRODUCTION_CAPACITY_CAP,
    GOODS_CAP,
    BARTER_STAGE_FUNCTIONING,
)


@dataclass(frozen=True)
class ProjectionDependencies:
    """reduce_events() が game.py 側の現在値を参照するための、明示的な
    依存の受け渡し容器。WorldStateではない(制度スキーマの再設計ではなく、
    投影層をgame.pyから独立させるための最小限の依存注入)。"""
    initial_resources: dict
    initial_traits: dict
    allowed_resources: set
    npc_trust_initial: float
    bank_npc_id: str
    price_index_fn: Callable[[int], float]
    effective_npc_trust_fn: Callable[[dict, int], float]
    trait_min: float = 0.0
    trait_max: float = 100.0


def reduce_events(events, dependencies: ProjectionDependencies) -> dict:
    """events(イテラブルなイベントdictの列)を畳み込んで現在の世界状態を
    再構築する。game.py の reduce_state() から処理順そのままに移植した
    (Step 2B、behavior-preserving refactoring)。"""
    deps = dependencies
    resources = dict(deps.initial_resources)
    traits = dict(deps.initial_traits)
    talent = None
    npc_pool_cap = None
    contracts = {}
    npcs = {}
    bank_credit_losses = 0.0
    bank_crisis_count = 0
    turn = 0
    alive = True
    death_turn = None
    bank_stage = BANK_STAGE_HEALTHY
    currency_confidence = CURRENCY_CONFIDENCE_INITIAL
    currency_stage = CURRENCY_STAGE_NORMAL
    community_trust = LOCAL_CREDIT_TRUST_INITIAL
    local_credit_stage = LOCAL_CREDIT_STAGE_HEALTHY
    enforcement_capacity = ENFORCEMENT_CAPACITY_INITIAL
    enforcement_stage = ENFORCEMENT_STAGE_INSTITUTIONAL
    # 2026-08-15追加(Step 13、物々交換・自給制度)。財(food/medicine/shelter/
    # tools)・production_capacity・barter_stageは初期値から始める(旧
    # events.jsonlに新フィールドが無くても、この初期値でそのまま復元できる)。
    # Step 13.1以降のbarter_activeは専用barter_activatedイベントで記録する。
    # Step 13の旧ログはbarter固有のgoods_state_changed/制度遷移から補完する。
    food = FOOD_STOCK_INITIAL
    medicine = MEDICINE_STOCK_INITIAL
    shelter = SHELTER_DURABILITY_INITIAL
    tools = TOOLS_DURABILITY_INITIAL
    production_capacity = PRODUCTION_CAPACITY_INITIAL
    barter_stage = BARTER_STAGE_FUNCTIONING
    barter_active = False
    barter_activated_turn = None
    for event in events:
        etype, data = event["type"], event["data"]
        if etype == "turn_started":
            turn = data["turn"]
        elif etype == "character_born":
            talent = data.get("talent")
            npc_pool_cap = data.get("npc_pool_cap")
        elif etype == "character_died":
            alive = False
            death_turn = data["turn"]
        elif etype == "npc_pool_cap_backfilled":
            npc_pool_cap = data["npc_pool_cap"]
        elif etype == "trait_changed":
            for k, v in data.get("delta", {}).items():
                if k in traits:
                    traits[k] = round(max(
                        deps.trait_min, min(deps.trait_max, traits[k] + v)), 6)
        elif etype == "npc_introduced":
            npcs.setdefault(data["id"], {
                "name": data["name"], "birth_turn": data.get("birth_turn"),
                "role": data.get("role"), "money": 0,
                "trust": data.get("trust", deps.npc_trust_initial),
                "trust_updated_turn": data.get("birth_turn", turn),
                "ethics": data.get("ethics"),
                "retire_turn": data.get("retire_turn"),
                "alive": True,
            })
        elif etype == "npc_wallet_changed":
            if data["npc_id"] not in npcs:
                raise ValueError(f"npc_wallet_changed: 未登場のnpc_id={data['npc_id']!r}")
            npcs[data["npc_id"]]["money"] += data["delta"]
        elif etype == "npc_trust_changed":
            if data["npc_id"] not in npcs:
                raise ValueError(f"npc_trust_changed: 未登場のnpc_id={data['npc_id']!r}")
            n = npcs[data["npc_id"]]
            change_turn = data.get("turn", turn)
            if n.get("role") == "acquaintance":
                n["trust"] = deps.effective_npc_trust_fn(n, change_turn)
            n["trust"] += data["delta"]
            n["trust_updated_turn"] = change_turn
        elif etype == "npc_died":
            if data["npc_id"] not in npcs:
                raise ValueError(f"npc_died: 未登場のnpc_id={data['npc_id']!r}")
            npcs[data["npc_id"]]["alive"] = False
        elif etype in ("state_applied", "decay_applied", "regen_applied",
                       "income_applied", "contract_settled", "endowment_applied",
                       "essential_goods_shortage_applied"):
            for k, v in data.get("delta", {}).items():
                if k in deps.allowed_resources:
                    resources[k] = resources.get(k, 0) + v
        if etype == "contract_created":
            contracts[data["id"]] = {
                "id": data["id"],
                "counterparty": data["counterparty"],
                "description": data["description"],
                "created_turn": data["created_turn"],
                "due_turn": data["due_turn"],
                "repay_money": data["repay_money"],
                "is_bank_debt": data.get("is_bank_debt", False),
                "status": "open",
            }
        elif etype == "contract_settled":
            c = contracts.get(data["id"])
            if c:
                c["status"] = data["outcome"]          # fulfilled / defaulted
                c["settled_turn"] = data["turn"]
                c["settled_by"] = data.get("settled_by")
                if c.get("is_bank_debt") and data["outcome"] == "defaulted":
                    bank_credit_losses += abs(c["repay_money"]) / deps.price_index_fn(data["turn"])
        elif etype == "bank_crisis":
            factor = data["rebase_factor"]
            resources["money"] = round(resources.get("money", 0) / factor)
            if deps.bank_npc_id in npcs:
                npcs[deps.bank_npc_id]["money"] = round(npcs[deps.bank_npc_id]["money"] / factor)
            for c in contracts.values():
                if c["status"] == "open":
                    c["repay_money"] = round(c["repay_money"] / factor)
            bank_credit_losses = 0.0
            bank_crisis_count += 1
            currency_confidence = max(0.0, currency_confidence - CURRENCY_CRISIS_HIT)
        elif etype == "currency_confidence_changed":
            currency_confidence = max(0.0, currency_confidence + data["delta"])
        elif etype == "community_trust_changed":
            community_trust = max(0.0, community_trust + data["delta"])
        elif etype == "enforcement_capacity_changed":
            enforcement_capacity = max(0.0, enforcement_capacity + data["delta"])
        elif etype == "goods_state_changed":
            # 2026-08-15追加(Step 13)。1イベントで4財+production_capacityの
            # deltaをまとめて運ぶ(community_trust_changed等と違い、財は単一
            # スカラーではないため専用の複合イベント形式にする)。存在しない
            # キーは0扱い(仕様「食料過剰で医薬品不足を帳消しにしない」の
            # 実装はcheck_trajectory_criteria側の判定であって、ここは単なる
            # 加算のみ)。production_capacityだけ他制度のconfidence系と同じく
            # 財・production_capacityとも明示した上限内へクランプする。
            food = max(0.0, min(GOODS_CAP, food + data.get("food_delta", 0.0)))
            medicine = max(0.0, min(GOODS_CAP, medicine + data.get("medicine_delta", 0.0)))
            shelter = max(0.0, min(GOODS_CAP, shelter + data.get("shelter_delta", 0.0)))
            tools = max(0.0, min(GOODS_CAP, tools + data.get("tools_delta", 0.0)))
            production_capacity = max(0.0, min(
                PRODUCTION_CAPACITY_CAP,
                production_capacity + data.get("production_capacity_delta", 0.0)))
            # Step 13の旧ログではgoods_state_changed自体が代替経済の発火を
            # 意味した。Step 13.1の平時会計イベントはreasonで区別し、旧ログと
            # 行動イベントだけは従来どおり発火履歴として復元する。
            if data.get("reason") in (
                    "barter_goods_upkeep", "barter_action", "subsistence_action"):
                barter_active = True
                if barter_activated_turn is None:
                    barter_activated_turn = data.get("turn", turn)
        elif etype == "barter_activated":
            barter_active = True
            if barter_activated_turn is None:
                barter_activated_turn = data.get("turn", turn)
        elif etype == "institution_transition":
            if data["institution_id"] == "central_bank":
                bank_stage = data["to_stage"]
            elif data["institution_id"] == "central_currency":
                currency_stage = data["to_stage"]
            elif data["institution_id"] == "local_credit":
                local_credit_stage = data["to_stage"]
            elif data["institution_id"] == "contract_enforcement":
                enforcement_stage = data["to_stage"]
            elif data["institution_id"] == "barter":
                barter_stage = data["to_stage"]
                barter_active = True
                if barter_activated_turn is None:
                    barter_activated_turn = data.get("turn", turn)
    for n in npcs.values():
        if n.get("role") == "acquaintance" and n.get("alive", True):
            n["trust"] = deps.effective_npc_trust_fn(n, turn)
            n["trust_updated_turn"] = turn
    return {"resources": resources, "contracts": contracts, "turn": turn,
            "traits": traits, "talent": talent, "npc_pool_cap": npc_pool_cap, "npcs": npcs,
            "bank_credit_losses": bank_credit_losses, "bank_crisis_count": bank_crisis_count,
            "alive": alive, "death_turn": death_turn,
            "bank_stage": bank_stage, "currency_confidence": currency_confidence,
            "currency_stage": currency_stage,
            "community_trust": community_trust, "local_credit_stage": local_credit_stage,
            "enforcement_capacity": enforcement_capacity, "enforcement_stage": enforcement_stage,
            "food": food, "medicine": medicine, "shelter": shelter, "tools": tools,
            "production_capacity": production_capacity, "barter_stage": barter_stage,
            "barter_active": barter_active,
            "barter_activated_turn": barter_activated_turn}
