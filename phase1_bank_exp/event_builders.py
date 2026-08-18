# -*- coding: utf-8 -*-
"""main()が積む一連のイベント(type, payload)の組み立てだけを扱う純粋関数。

Step 6E(2026-08-15、behavior-preserving refactoring)でgame.pyから分離した:
- build_settlement_effect_events(): 清算(履行/不履行)の効果イベント列。
Step 6F(2026-08-15)で追加:
- build_normal_contract_events(): social行動で発生するNPC登場・契約作成の
  イベント列。

いずれもイベントの保存(append_event/event_store)・world状態の読み直し
(reduce_state)・print・stats更新は一切行わない、純粋関数。

このモジュールは game.py・event_store.py・projection.py・engine.py・
policy.py のいずれにも依存しない。
"""
from typing import Callable


def build_settlement_effect_events(contract: dict, choice: dict, effects: dict, turn: int,
                                   bank_npc_id: str, bank_crisis_count: int) -> list:
    """契約清算(履行/不履行)の効果(effects、engine.plan_settlement_effects()の
    戻り値)から、積むべきイベント列を順序を保ったまま組み立てる。戻り値は
    [(event_type, payload), ...] のリスト——実際の保存(append_event)は
    呼び出し側の責務のまま残す(state_applied・contract_settledもこの関数の
    対象外、呼び出し側が別途積む)。

    維持するイベント順序・payload・reasonは main() の元実装のまま:
    - 銀行債務・履行: npc_trust_changed(wage_debt_fulfilled) →
      (money型のみ) npc_wallet_changed(wage_debt_repaid) →
      currency_confidence_changed(money_settlement_fulfilled) →
      enforcement_capacity_changed(settlement_fulfilled)
    - 銀行債務・不履行: npc_trust_changed(wage_debt_defaulted) →
      (危機発生時のみ) bank_crisis → npc_trust_changed(bank_crisis) →
      enforcement_capacity_changed(settlement_defaulted)
      ——危機時にcurrency_confidence_changedを新設しない(通貨confidenceの
      低下は従来どおりprojection.pyがbank_crisisイベントを解釈して適用する)
    - 社会契約(履行/不履行いずれも): npc_trust_changed(social_contract_{outcome})
      → community_trust_changed(local_credit_propagation)(2026-08-15追加、
      「NPC間評判伝播」の片側。銀行債務では発行しない)→
      enforcement_capacity_changed(settlement_{outcome})

    enforcement_capacity_changed(2026-08-15追加、契約執行制度)は銀行債務・
    社会契約どちらの分岐でも(履行/不履行いずれでも)必ず発行する——
    community_trust_changedと違いこちらは全分岐共通(「第三者執行という
    同じ仕組みが試される」という解釈、engine.plan_settlement_effectsの
    docstring参照)。各分岐の末尾に置く。

    bank_crisis_count(危機カウントのpayloadに+1して使う)は呼び出し時点の値を
    そのまま受け取る——このモジュール自身はreduce_state()を呼ばない。"""
    events = []
    if contract.get("is_bank_debt") and choice["settle"] == "fulfilled":
        events.append(("npc_trust_changed", {
            "npc_id": bank_npc_id, "delta": effects["bank_trust_delta"],
            "reason": "wage_debt_fulfilled", "turn": turn,
        }))
        if choice["key"] == "money":
            events.append(("npc_wallet_changed", {
                "npc_id": bank_npc_id, "delta": effects["bank_wallet_delta"],
                "reason": "wage_debt_repaid", "turn": turn,
            }))
            events.append(("currency_confidence_changed", {
                "turn": turn, "delta": effects["currency_confidence_delta"],
                "reason": "money_settlement_fulfilled",
            }))
        events.append(("enforcement_capacity_changed", {
            "turn": turn, "delta": effects["enforcement_capacity_delta"],
            "reason": "settlement_fulfilled",
        }))
    elif contract.get("is_bank_debt") and choice["settle"] == "defaulted":
        events.append(("npc_trust_changed", {
            "npc_id": bank_npc_id, "delta": effects["bank_trust_delta"],
            "reason": "wage_debt_defaulted", "turn": turn,
        }))
        if effects["crisis_triggered"]:
            events.append(("bank_crisis", {
                "turn": turn, "rebase_factor": effects["rebase_factor"],
                "credit_losses": effects["bank_credit_losses_after"],
                "crisis_count": bank_crisis_count + 1,
            }))
            events.append(("npc_trust_changed", {
                "npc_id": bank_npc_id, "delta": effects["crisis_bank_trust_delta"],
                "reason": "bank_crisis", "turn": turn,
            }))
        events.append(("enforcement_capacity_changed", {
            "turn": turn, "delta": effects["enforcement_capacity_delta"],
            "reason": "settlement_defaulted",
        }))
    elif not contract.get("is_bank_debt"):
        events.append(("npc_trust_changed", {
            "npc_id": contract["counterparty"], "delta": effects["counterparty_trust_delta"],
            "reason": f"social_contract_{choice['settle']}", "turn": turn,
        }))
        events.append(("community_trust_changed", {
            "turn": turn, "delta": effects["community_trust_delta"],
            "reason": "local_credit_propagation",
        }))
        events.append(("enforcement_capacity_changed", {
            "turn": turn, "delta": effects["enforcement_capacity_delta"],
            "reason": f"settlement_{choice['settle']}",
        }))
    return events


def build_normal_contract_events(choice: dict, state_now: dict, turn: int, contract_id: str, *,
                                 initial_trust_for_new_npc_fn: Callable[[dict, int], float],
                                 acquaintance_npcs_fn: Callable[[dict], dict]) -> list:
    """通常行動のsocial型choice(choice["contract"]を持つ)から、NPC登場・
    契約作成のイベント列を順序を保ったまま組み立てる。戻り値は
    [(event_type, payload), ...] のリスト——実際の保存(append_event)・
    contract_idの採番・reduce_state()の呼び出しは呼び出し側の責務のまま残す
    (この関数はstate_nowを読むだけで、world状態を読み直さない)。

    維持するイベント順序・payload・キー名は main() の元実装のまま:
    - 初対面の相手(choice["contract"]["counterparty"]がstate_now["npcs"]に
      まだ無い)なら npc_introduced → contract_created の順。initial_trust_for_
      new_npc_fn(相手の初期trust計算)はこのときだけ呼ぶ——既存NPCの場合は
      呼ばない(重複したtrust計算を避けるため、元の実装から変わらない)。
    - 既存の相手なら contract_created のみ。"""
    c = choice["contract"]
    events = []
    if c["counterparty"] not in state_now["npcs"]:
        # 初対面の相手の初期trustは、プレイヤーの一般的な評判(周りの人間からの
        # 信用値)をNPC_TRUST_INITIALとの加重平均で反映する(2026-08-14追加・
        # 再訂正、ユーザー提案)。
        events.append(("npc_introduced", {
            "id": c["counterparty"], "name": c["counterparty"],
            "birth_turn": turn, "role": "acquaintance",
            "trust": initial_trust_for_new_npc_fn(acquaintance_npcs_fn(state_now), turn),
            "ethics": c["prospective_ethics"],
            "retire_turn": c["prospective_retire_turn"],
        }))
    events.append(("contract_created", {
        "id": contract_id, "counterparty": c["counterparty"], "description": c["description"],
        "created_turn": turn, "due_turn": c["due_turn"],
        "repay_money": c["repay_money"], "origin_choice": choice["label"],
    }))
    return events
