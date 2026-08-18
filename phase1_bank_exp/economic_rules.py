# -*- coding: utf-8 -*-
"""価格水準・賃金前借り・所得の計算を扱う純粋ルール。

Step 5D(2026-08-14、behavior-preserving refactoring)でgame.pyから分離した。
イベント保存、世界状態の投影、乱数、LLM、CLIには依存しない。CLIから変更可能な
CREATION_RATEを含む設定値はEconomicRulesDependenciesを介して呼び出し時に
受け取る(import時点で固定しない——game.py側の互換ラッパーが呼び出しごとに
依存オブジェクトを作り直す)。

このモジュールは game.py・event_store.py・projection.py・engine.py・
policy.py のいずれにも依存しない。institutions側の定数(銀行stageの区分値)も、
呼び出し側(game.py)がすでにimport済みの値をDependencies経由でそのまま渡す形に
統一する(このモジュール自身はinstitutionsをimportしない)。
"""
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class EconomicRulesDependencies:
    """price_index()・compute_wage()・compute_salary()・compute_income()が
    game.py側の設定値(CLIで上書きされうるCREATION_RATEを含む)を参照するための、
    明示的な依存の受け渡し容器。WorldStateではない(制度スキーマの再設計ではなく、
    経済ルールをgame.pyから独立させるための最小限の依存注入)。"""
    creation_rate: float
    income_interval: int
    wage_base: float
    salary_base: float
    bank_stage_healthy: int
    bank_stage_contraction: int
    bank_stage_halted: int
    bank_stage_collapsed: int
    stage1_advance_shrink: float
    stage2_advance_shrink: float


def price_index(turn: int, *, dependencies: EconomicRulesDependencies) -> float:
    """価格水準(=マネーサプライM_totalの、初期値からの相対的な伸び)。
    M_total(t) = M_total(0) × (1+CREATION_RATE)^t なので、比だけを取れば
    M_total(0)の具体的な値は不要(約分されて消える)。通貨危機(デノミ)が
    起きても、この式自体は変わらない(名目残高が直接割り直されるだけ)。"""
    return (1.0 + dependencies.creation_rate) ** turn


def compute_wage(turn: int, bank_stage: int, *, dependencies: EconomicRulesDependencies,
                 price_index_fn: Callable[[int], float] = None) -> int:
    """このターンに支払われる賃金前借り(名目額、返済義務あり)。実質賃金が
    一定になるよう価格水準に連動する。docs/plan.md「経済の閉じ方 v1実装設計」の
    wage_nominal(t) = wage_base × price_index(t)。

    2026-08-14追記(銀行の状態機械): Stage1(信用収縮)・Stage2(融資停止)では
    規模を縮小する。Stage3(銀行制度消滅、不可逆)でのみ完全に0にする。

    2026-08-14訂正(実装直後の実測で発覚した設計ミス): 当初Stage2は完全に0
    (新規発行なし)にしていたが、`bank_trust_gain`は「銀行債務が履行された
    とき」にしか発火しないため、新規の前借りそのものが無くなると履行される
    債務も無くなり、bank_trustが二度と回復できない片道の罠になっていた
    (ヒステリシスで復帰条件を用意したのに、その復帰に必要なgainの発生源
    そのものを止めてしまっていた)。Stage2でも小さいながら発行を続け、
    回復の芽を残す。"""
    if turn % dependencies.income_interval != 0:
        return 0
    if bank_stage >= dependencies.bank_stage_collapsed:
        return 0
    idx = (price_index_fn(turn) if price_index_fn
           else price_index(turn, dependencies=dependencies))
    amount = dependencies.wage_base * idx
    if bank_stage == dependencies.bank_stage_contraction:
        amount *= dependencies.stage1_advance_shrink
    elif bank_stage == dependencies.bank_stage_halted:
        amount *= dependencies.stage2_advance_shrink
    return round(amount)


def compute_salary(turn: int, *, dependencies: EconomicRulesDependencies,
                   price_index_fn: Callable[[int], float] = None) -> int:
    """このターンに支払われる本当の所得(名目額、返済義務なし)。2026-08-14追加
    (P0-3「所得と融資を分離する」)。compute_wage(前借り)と同じ支払いタイミング・
    同じ価格連動だが、対応する契約(返済請求権)を作らない別経路。

    意図的に`bank_stage`を引数に取らない——銀行がどの段階にあっても
    (Stage3〈銀行制度消滅〉でも)継続する、という設計をそのまま表す
    (docs/social-regimes-spec.md「銀行が壊れても労働による所得は残る」)。"""
    if turn % dependencies.income_interval != 0:
        return 0
    idx = (price_index_fn(turn) if price_index_fn
           else price_index(turn, dependencies=dependencies))
    return round(dependencies.salary_base * idx)


def compute_income(turn: int, bank_stage: int, *, dependencies: EconomicRulesDependencies,
                   compute_wage_fn: Callable[[int, int], int] = None) -> dict:
    """賃金前借り(銀行からの実際の振込)。docs/plan.md「経済の閉じ方 v1実装設計」
    により、無から生まれるのではなく銀行NPCとの取引として扱う。ここでは
    プレイヤー側のdeltaのみを返す(従来と同じ形を維持)。銀行側の反対仕訳
    (npc_wallet_changed、delta=-wage)は呼び出し側で別途記録すること
    (下限が無い=銀行が発行した信用そのもの)。"""
    wage = (compute_wage_fn(turn, bank_stage) if compute_wage_fn
           else compute_wage(turn, bank_stage, dependencies=dependencies))
    return {"money": wage} if wage else {}
