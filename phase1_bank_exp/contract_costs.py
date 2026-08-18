# -*- coding: utf-8 -*-
"""契約清算(labor返済・不履行ペナルティ)のコスト計算を扱う純粋ルール。

Step 5H(2026-08-14、behavior-preserving refactoring)でgame.pyから分離した。
イベント保存、世界状態の投影、LLM、CLIには依存しない。randomモジュールへの
暗黙依存もない——乱数はContractCostDependencies.uniform_fn経由で受け取る
(呼び出し側=game.pyが呼び出し時点のrandom.uniformを渡す)。CLIから変更可能な
価格水準(price_index)も、ContractCostDependencies.price_index_fn経由で
呼び出し時点の関数を受け取る。

このモジュールは game.py・event_store.py・projection.py・engine.py・
policy.py のいずれにも依存しない。
"""
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ContractCostDependencies:
    """draw_contract_repay_labor()・contract_default_penalty()が game.py側の
    設定値・乱数・価格水準を参照するための、明示的な依存の受け渡し容器。
    WorldStateではない(制度スキーマの再設計ではなく、契約清算コスト計算を
    game.pyから独立させるための最小限の依存注入)。"""
    contract_repay_labor_coef: dict
    contract_repay_labor_spread: float
    contract_default_penalty_rate: float
    uniform_fn: Callable[[float, float], float]
    price_index_fn: Callable[[int], float]


def draw_contract_repay_labor(repay_money: int, turn: int, *,
                              dependencies: ContractCostDependencies) -> dict:
    """労力での契約返済コスト。repay_money(契約締結時点の名目額で固定)を、
    清算時点のprice_indexで実質化してから係数を掛ける。"""
    real = abs(repay_money) / dependencies.price_index_fn(turn)
    out = {}
    for res, coef in dependencies.contract_repay_labor_coef.items():
        lo = coef * (1 - dependencies.contract_repay_labor_spread)
        hi = coef * (1 + dependencies.contract_repay_labor_spread)
        cost = -round(real * dependencies.uniform_fn(lo, hi))
        if cost:
            out[res] = cost
    return out


def contract_default_penalty(repay_money: int, turn: int, *,
                             dependencies: ContractCostDependencies) -> dict:
    real = abs(repay_money) / dependencies.price_index_fn(turn)
    return {"peace": -round(dependencies.contract_default_penalty_rate * real)}
