# -*- coding: utf-8 -*-
"""集落間交易の純粋ルール。

食料・医薬品・道具だけを、余剰のある集落から不足している集落へ移す。
住居と生産能力は土地に結び付くため移動させない。移送量は送り手と受け手の
地域信用Stageのうち悪い側で制限され、どちらかが孤立していれば交易しない。

この関数は財を生産・消費せず、入力を変更せず、乱数も使わない。したがって
交易前後で各財の世界総量が保存される。交易の成立は双方の共有台帳に小さな
正の実績を残すが、Stage遷移自体はlocal_credit.pyを呼ぶ上位ループの責務。
"""
from __future__ import annotations

import copy

from institutions import barter
from institutions.local_credit import (
    LOCAL_CREDIT_STAGE_CONTRACTION,
    LOCAL_CREDIT_STAGE_HEALTHY,
    LOCAL_CREDIT_STAGE_ISOLATED,
    LOCAL_CREDIT_STAGE_PERSONAL,
)
from institutions.population import POPULATION_STAGE_ABANDONED


PORTABLE_GOODS = ("food", "medicine", "tools")

def trade_references() -> dict:
    """現在のbarter較正値を呼び出し時に読む(UIの動的上書きも反映)。"""
    return {
        "food": barter.FOOD_SHORTFALL_REFERENCE,
        "medicine": barter.MEDICINE_SHORTFALL_REFERENCE,
        "tools": barter.TOOLS_SHORTFALL_REFERENCE,
    }

# 供出後にも基準量の72%を残し、受け手は90%までを目標とする。全量を平均化
# しないため、集落ごとの生活条件と不足は交易後も残る。
TRADE_RESERVE_RATIO = 0.72
TRADE_TARGET_RATIO = 0.90
TRADE_MAX_PER_ROUTE_PER_GOOD = 4.0

# 共有台帳が個人化すると細い取引だけになり、孤立すると停止する。
LOCAL_CREDIT_TRADE_CAPACITY = {
    LOCAL_CREDIT_STAGE_HEALTHY: 1.0,
    LOCAL_CREDIT_STAGE_CONTRACTION: 0.60,
    LOCAL_CREDIT_STAGE_PERSONAL: 0.25,
    LOCAL_CREDIT_STAGE_ISOLATED: 0.0,
}

# 交易成立を「地域内で参照できる履行実績」として双方へ記録する。財の単位差は
# 基準量で正規化し、1イベントが信用値を急変させないよう上限を置く。
TRADE_TRUST_GAIN_SCALE = 5.0
TRADE_TRUST_GAIN_CAP = 0.40


def trade_capacity(local_credit_stage: int) -> float:
    """地域信用Stageから交易能力(0〜1)を返す。"""
    return LOCAL_CREDIT_TRADE_CAPACITY.get(int(local_credit_stage), 0.0)


def _eligible(settlement: dict) -> bool:
    return (int(settlement.get("population", 0)) > 0
            and int(settlement.get("stage", 0)) < POPULATION_STAGE_ABANDONED)


def _trade_trust_gain(amount: float, reference: float) -> float:
    if reference <= 0:
        return 0.0
    return round(min(
        TRADE_TRUST_GAIN_CAP,
        amount / reference * TRADE_TRUST_GAIN_SCALE), 6)


def _demand_reference(economy: dict, good: str) -> float:
    """現在人口の需要基準。旧状態はprovisioning_scaleへfallbackする。"""
    demand = economy.get("demand_scales_by_good") or {}
    return barter.goods_reference(
        good, economy.get("provisioning_scale", 1.0),
        demand_scale=demand.get(good))


def plan_intersettlement_trade(settlements: dict, turn: int) -> dict:
    """1か月分の集落間交易を計画する。

    戻り値は更新済み``settlements``、成立した``events``、移動した財の合計
    ``volume``。イベント順は財順→不足の大きい受け手→余剰の大きい送り手で
    決定的である。
    """
    after = copy.deepcopy(settlements)
    events = []
    volume = 0.0

    active_ids = sorted(
        sid for sid, row in after.items() if _eligible(row))
    if len(active_ids) < 2:
        return {"settlements": after, "events": events, "volume": volume}

    for good in PORTABLE_GOODS:
        destination_ids = sorted(
            active_ids,
            key=lambda sid: (
                -max(0.0, _demand_reference(
                    after[sid]["local_economy"], good)
                    * TRADE_TARGET_RATIO - float(
                        after[sid]["local_economy"].get(good, 0.0))), sid))

        for destination_id in destination_ids:
            destination = after[destination_id]
            destination_economy = destination["local_economy"]
            destination_reference = _demand_reference(
                destination_economy, good)
            target = destination_reference * TRADE_TARGET_RATIO
            need = max(0.0, target - float(destination_economy.get(good, 0.0)))
            if need <= 1e-9:
                continue

            source_ids = sorted(
                (sid for sid in active_ids if sid != destination_id),
                key=lambda sid: (
                    -max(0.0, float(
                        after[sid]["local_economy"].get(good, 0.0))
                        - _demand_reference(
                            after[sid]["local_economy"], good)
                        * TRADE_RESERVE_RATIO),
                    sid))
            for source_id in source_ids:
                source = after[source_id]
                source_economy = source["local_economy"]
                source_reference = _demand_reference(source_economy, good)
                reserve = source_reference * TRADE_RESERVE_RATIO
                spare = max(0.0, float(source_economy.get(good, 0.0)) - reserve)
                if spare <= 1e-9:
                    continue
                capacity = min(
                    trade_capacity(source_economy.get("local_credit_stage", 0)),
                    trade_capacity(destination_economy.get(
                        "local_credit_stage", 0)))
                amount = round(min(
                    spare, need,
                    TRADE_MAX_PER_ROUTE_PER_GOOD * capacity), 6)
                if amount <= 1e-9:
                    continue

                source_economy[good] = round(
                    float(source_economy[good]) - amount, 6)
                destination_economy[good] = round(
                    float(destination_economy[good]) + amount, 6)
                # 大きい会計基盤の一部を運んだだけで信用実績が過大に
                # ならないよう、両者の基準量のうち大きい側で正規化する。
                gain = _trade_trust_gain(
                    amount, max(source_reference, destination_reference))
                source_economy["community_trust"] = min(
                    100.0, round(float(source_economy.get(
                        "community_trust", 50.0)) + gain, 6))
                destination_economy["community_trust"] = min(
                    100.0, round(float(destination_economy.get(
                        "community_trust", 50.0)) + gain, 6))
                source_economy["trade_sent_total"] = round(
                    float(source_economy.get("trade_sent_total", 0.0)) + amount, 6)
                destination_economy["trade_received_total"] = round(
                    float(destination_economy.get(
                        "trade_received_total", 0.0)) + amount, 6)
                source_economy["trade_events_total"] = int(
                    source_economy.get("trade_events_total", 0)) + 1
                destination_economy["trade_events_total"] = int(
                    destination_economy.get("trade_events_total", 0)) + 1

                events.append({
                    "turn": int(turn),
                    "kind": "intersettlement_trade",
                    "from_settlement": source_id,
                    "to_settlement": destination_id,
                    "good": good,
                    "amount": amount,
                    "credit_capacity": capacity,
                    "source_trust_gain": gain,
                    "destination_trust_gain": gain,
                })
                volume = round(volume + amount, 6)
                need = round(need - amount, 6)
                if need <= 1e-9:
                    break

    return {"settlements": after, "events": events, "volume": volume}
