#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
お金の減衰モデル比較(検証用、ollama不使用)。

plan.md「資源体系全体の統一」の訂正: お金の減衰を固定量(-1/ターン)から
残高に比例する率に変える(インフレ・保管運用の摩擦)。opus第7回相当のレビューが
指摘した「固定量だと傾き1のアフィン写像になり、無限発散か死の螺旋の二択しか
持たない」という診断を、実際に数値でシミュレートして確認する。

比較対象:
  A) 現行(Phase 1実装): 固定減衰 -1/ターン、収入は4ターンごとに固定額+4
  B) 訂正案: 比例減衰 -rate*M/ターン、収入は同じ(平均+1/ターン)

Bで安定した平衡点 M* = income_rate / decay_rate に収束することを確認する。
"""
import random

INCOME_PER_TURN_AVG = 1.0  # 現行の INCOME_AMOUNT=4 / INCOME_INTERVAL=4 と同じ平均
TURNS = 2000
TRIALS = 50


def simulate_fixed_decay(decay_amount: float, initial: float, income_avg: float,
                         turns: int, choice_cost_avg: float = 0.0) -> list:
    """A) 現行方式: 固定量減衰。income は毎ターン平均値を連続適用(離散の谷は無視し、
    純粋に力学のクラスだけを見る)。"""
    m = initial
    trace = [m]
    for _ in range(turns):
        m = m - decay_amount + income_avg - choice_cost_avg
        trace.append(m)
    return trace


def simulate_proportional_decay(decay_rate: float, initial: float, income_avg: float,
                                turns: int, choice_cost_avg: float = 0.0) -> list:
    """B) 訂正案: 比例減衰。"""
    m = initial
    trace = [m]
    for _ in range(turns):
        m = m - decay_rate * m + income_avg - choice_cost_avg
        trace.append(m)
    return trace


def main():
    print("=== A) 現行: 固定減衰(-1/ターン)。income平均 = 1.0/ターン ===")
    for label, income in [("収支ちょうど釣り合う設定", 1.0),
                          ("行動コストで少し赤字(平均-0.3)", 0.7)]:
        trace = simulate_fixed_decay(1.0, 8.0, income, TURNS)
        print(f"  {label}: T0={trace[0]:.1f} T50={trace[50]:.1f} "
              f"T500={trace[500]:.1f} T{TURNS}={trace[TURNS]:.1f}")
    print("  → 収支が釣り合わない限り、線形に発散(青天井)か線形に沈む(死の螺旋)"
          "の二択。釣り合う場合も、外乱(行動コスト)が加わるだけで即座にどちらかへ倒れる"
          "(測度ゼロの均衡)。実際、2列目は income をわずか0.3減らしただけでT2000時点で"
          f"{simulate_fixed_decay(1.0, 8.0, 0.7, TURNS)[TURNS]:.0f}まで際限なく沈む。\n")

    print("=== B) 訂正案: 比例減衰(decay_rate × 残高)。income平均 = 1.0/ターン ===")
    for decay_rate in [0.05, 0.1, 0.2]:
        m_star = INCOME_PER_TURN_AVG / decay_rate
        trace = simulate_proportional_decay(decay_rate, 8.0, INCOME_PER_TURN_AVG, TURNS)
        print(f"  decay_rate={decay_rate}: 理論上の平衡点 M*={m_star:.1f} / "
              f"実際の到達値(T{TURNS})={trace[TURNS]:.2f}")
    print()

    print("=== B) 外乱(行動コストの変動)への頑健性 ===")
    decay_rate = 0.1
    for choice_cost_avg in [0.0, 0.3, 0.6, 0.9]:
        # 初期値を変えても同じ平衡点に収束するか(大域的安定性の確認)
        results = []
        for initial in [0.0, 8.0, 50.0]:
            trace = simulate_proportional_decay(decay_rate, initial,
                                                 INCOME_PER_TURN_AVG, TURNS,
                                                 choice_cost_avg)
            results.append(trace[TURNS])
        m_star = (INCOME_PER_TURN_AVG - choice_cost_avg) / decay_rate
        print(f"  行動コスト平均={choice_cost_avg}: 理論値M*={m_star:.2f} / "
              f"初期値0,8,50から収束した値={[f'{r:.2f}' for r in results]}")
    print("  → 初期値によらず同じ平衡点に収束する(安定不動点)。行動コストが増えても"
          "発散・死の螺旋にはならず、平衡点が滑らかに下がるだけ。\n")

    print("=== ランダムウォーク的な行動コスト(実プレイに近い外乱)でも安定するか ===")
    random.seed(1)
    for decay_rate in [0.1]:
        m = 8.0
        trace = [m]
        for _ in range(TURNS):
            noisy_cost = random.choice([0, 0, 0, 2, 3])  # 3択のうちmoney型を選ぶ頻度を模した粗いモデル
            m = m - decay_rate * m + INCOME_PER_TURN_AVG - noisy_cost * 0.3
            m = max(m, -50)  # 実装では is_affordable で防がれる部分、ここでは参考として下限だけ緩く置く
            trace.append(m)
        print(f"  decay_rate={decay_rate}, ランダムな行動コスト: "
              f"T50={trace[50]:.1f} T500={trace[500]:.1f} T{TURNS}={trace[TURNS]:.1f}")
        print(f"  T1000以降の分散: {max(trace[1000:]) - min(trace[1000:]):.1f} "
              "(発散していれば増え続けるはずだが、幅は頭打ちになる)")


if __name__ == "__main__":
    main()
