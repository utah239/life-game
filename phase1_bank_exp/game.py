#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
人生ゲーム(仮) Phase 1 — 1人で遊べる最小限の人生シミュレーション

仕様: ../docs/phase1-spec.md(実装対象の唯一の正)
土台: ../phase0/game.py(検証済みの要素をそのまま引き継ぐ。phase0側は変更しない)

Phase 0 から引き継いだもの:
  - イベントソーシング(events.jsonl への追記のみ。現在状態は毎回 reduce で再構築)
  - コスト決定のコード化(ACTION_ARCHETYPES。LLMはラベルと描写のみ)
  - 整合性担保役ロール(絶対法則違反のみを見る軽量プロンプト)
  - キー名のホワイトリスト検証・クリップ(sanitize_cost)
  - keep_alive の明示設定("5m")

Phase 1 で新しく足したもの:
  1. ストック資産(trust)の維持コスト = エントロピー原則の実装。固定量で減衰し、
     decay_applied イベントとして記録される。money は2026-08-13、比例減衰から
     「銀行NPC+マネーサプライ(M_total)+価格水準」方式に置き換えた(下記7)。
  2. 契約・債務テーブル。「相手・内容・期限・履行状態」を持つレコードを同じ
     events.jsonl に乗せる(contract_created / contract_settled)。
     social型の選択は「借りを作る」= 契約の生成。期限(due_turn)が来たターンは
     コード側が強制的に「清算ターン」に切り替え、履行/不履行が確定する。
     **判定はすべてコードが行い、LLMには一切させない**(LLMは相手の名前と描写だけ)。
  3. 状況の繰り返し対策(Phase 0 で未解決だった問題)。指示ベースの
     「重複するな」は効かなかったので、コード側の題材リストからランダムに
     お題を強制注入する方式に変えた。--theme-injection off で対照群が取れる。
  4. 自動プレイモード(--auto)。ollama への負荷を抑えつつ複数ターンの一貫性を
     実測するため。--seed で再現可能。
  5. 特性(器用さ・思考力・スキル・健康)。docs/plan.md「特性の成長経路」の実装。
     labor/money型の選択が題材の特性と一致すると成長し、使わなければ緩やかに衰える。
     健康は別モデル(老化に連動した基底減衰+医療/自己ケアでの回復)。長期の平衡値は
     --simulate(ollama不使用の長期モンテカルロ)で検証できる。
  6. 代打ちAIの方針ベクトル(--policy)。docs/plan.md「代打ちAIの権限境界」の実装。
     資源1点あたりの時間換算コスト(SHADOW_PRICE)と方針の重みベクトルの内積で選ぶ
     (2026-08-13、L1正規化+risk_aversion方式から訂正。旧方式は policy_score_legacy
     として比較用に残す)。生存制約(worst_after)の上に重ねる2段階選択。
     --policy-probe で ollama不使用の選択ロジック検証ができる。
  7. 銀行NPC・マネーサプライ(M_total)・価格水準(2026-08-13、phase1_bank_exp/で
     実装。docs/plan.md「経済の閉じ方 v1実装設計」)。moneyの比例減衰を、
     「無から生まれる収入」を実際の取引にする形へ置き換えた。賃金は銀行からの
     借入(債券)として契約化され、money型で履行されたときだけ銀行の帳簿に戻る
     (=貨幣の破壊)。踏み倒しのペナルティ(trust)は返済額に比例させてある
     (固定値だと、名目上膨らむ債務ほど踏み倒しが割安になってしまうため)。
     実装中に自分で見つけた2つの設計ミス(M_totalの定義、価格指数の伸び率)の
     訂正、および統合前のopusレビューで見つかった重大な指摘(名目シンクの消失・
     複式記帳の片側だけの実装)とその対応は docs/plan.md に経緯ごと記録してある。

NPCの永続化(npc_introduced/npc_wallet_changed/npc_died)は既存のイベントソーシング
パターン(資源・特性と同じ)をそのまま延長したもの。5・6はもともと独立コピー
(../phase1_trait_exp/, ../phase1_policy_exp/)で別々に実験していたものを、検証
(実測・opusレビュー)を経て本体に1回で統合した。各実験での検証過程は
../phase1_trait_exp/README.md・../phase1_policy_exp/README.md、資源体系全体の統一・
経済の閉じ方の設計判断は ../docs/plan.md を参照。

注意: ollama は NAS 本番の paperless-gpt と共用(ai-lab/CLAUDE.md)。
長時間・大量に回し続けるテストはしないこと。

使い方:
  python game.py --turns 12 --auto --seed 1                   # 自動プレイ(検証用)
  python game.py --turns 10                                    # 対話プレイ
  python game.py --turns 12 --auto --theme-injection off       # 繰り返し対策の対照群
  python game.py --turns 12 --auto --events events_run2.jsonl
  python game.py --turns 18 --auto --policy cautious --seed 1  # 代打ちAIの方針ベクトル
  python game.py --policy-probe --probe-seeds 1,2,3,4,5        # 方針ベクトルのオフライン検証
  python game.py --policy-check --probe-seeds 1,2,3,4,5        # 反証可能な合否基準でPASS/FAIL判定
  python game.py --simulate --sim-turns 2000                   # (非推奨。--policy-checkを使うこと)
  python game.py --negative-control                            # 整合性担保役の否定制御テスト

events.jsonl は実行のたびに追記される。最初からやり直すには削除するか --events で別名にする。
"""
import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# 2026-08-14(Step 1、段階的モジュール分割、behavior-preserving refactoring)。
# 銀行・通貨の純粋ロジックを institutions/ 配下へ切り出し、ここで明示的に
# 再公開する(from ... import * は使わない)。これにより game.py 内の既存の
# 呼び出し方(bank_stage_next(...) 等、"institutions." プレフィックス無し)は
# 変更していない。institutions/bank.py・institutions/currency.py は game.py・
# イベントログ・LLM・print のいずれにも依存しない。
from institutions.bank import (
    BANK_TRUST_INITIAL,
    BANK_TRUST_CAP,
    CRISIS_THRESHOLD_BASE,
    crisis_threshold,
    bank_trust_gain,
    BANK_TRUST_REVERSION_RATE,
    bank_trust_reversion,
    BANK_STAGE_HEALTHY,
    BANK_STAGE_CONTRACTION,
    BANK_STAGE_HALTED,
    BANK_STAGE_COLLAPSED,
    BANK_STAGE_NAMES,
    BANK_STAGE1_ENTER,
    BANK_STAGE1_EXIT,
    BANK_STAGE2_ENTER,
    BANK_STAGE2_EXIT,
    BANK_STAGE3_CRISIS_COUNT,
    BANK_STAGE1_ADVANCE_SHRINK,
    BANK_STAGE2_ADVANCE_SHRINK,
    bank_stage_next,
)
from institutions.currency import (
    CURRENCY_CONFIDENCE_INITIAL,
    CURRENCY_CONFIDENCE_CAP,
    CURRENCY_CONFIDENCE_REVERSION_RATE,
    CURRENCY_CRISIS_HIT,
    CURRENCY_STAGE_NORMAL,
    CURRENCY_STAGE_WARY,
    CURRENCY_STAGE_ABANDONED,
    CURRENCY_STAGE_NAMES,
    CURRENCY_STAGE1_ENTER,
    CURRENCY_STAGE1_EXIT,
    CURRENCY_STAGE3_ENTER,
    CURRENCY_STAGE3_EXIT,
    CURRENCY_STAGE1_PRICE_PENALTY,
    currency_confidence_reversion,
    currency_confidence_gain,
    currency_stage_next,
)
# 2026-08-15、地域信用制度の実装。institutions/local_credit.pyも同じ形で
# 明示的に再公開する(bank.py/currency.pyと同じプレフィックス無し呼び出し方を
# 維持するため)。local_credit.pyはbank.py・currency.pyのどちらにも依存しない。
from institutions.local_credit import (
    LOCAL_CREDIT_TRUST_INITIAL,
    LOCAL_CREDIT_TRUST_REVERSION_RATE,
    LOCAL_CREDIT_PROPAGATION_SCALE,
    LOCAL_CREDIT_INITIAL_TRUST_WEIGHT,
    LOCAL_CREDIT_STAGE_HEALTHY,
    LOCAL_CREDIT_STAGE_CONTRACTION,
    LOCAL_CREDIT_STAGE_PERSONAL,
    LOCAL_CREDIT_STAGE_ISOLATED,
    LOCAL_CREDIT_STAGE_NAMES,
    LOCAL_CREDIT_STAGE1_ENTER,
    LOCAL_CREDIT_STAGE1_EXIT,
    LOCAL_CREDIT_STAGE2_ENTER,
    LOCAL_CREDIT_STAGE2_EXIT,
    LOCAL_CREDIT_STAGE3_ENTER,
    LOCAL_CREDIT_STAGE3_EXIT,
    LOCAL_CREDIT_SCOPE,
    community_trust_reversion,
    local_credit_stage_next,
)
# 2026-08-15、契約執行制度の実装。institutions/contract_enforcement.pyも同じ形で
# 明示的に再公開する。bank.py/currency.py/local_credit.pyのいずれにも依存しない。
from institutions.contract_enforcement import (
    ENFORCEMENT_CAPACITY_INITIAL,
    ENFORCEMENT_CAPACITY_CAP,
    ENFORCEMENT_CAPACITY_REVERSION_RATE,
    ENFORCEMENT_DEFAULT_PENALTY,
    ENFORCEMENT_STAGE_INSTITUTIONAL,
    ENFORCEMENT_STAGE_DELAYED,
    ENFORCEMENT_STAGE_LOCAL_LEDGER,
    ENFORCEMENT_STAGE_PERSONAL,
    ENFORCEMENT_STAGE_NONE,
    ENFORCEMENT_STAGE_NAMES,
    ENFORCEMENT_SCOPE,
    ENFORCEMENT_STAGE1_ENTER,
    ENFORCEMENT_STAGE1_EXIT,
    ENFORCEMENT_STAGE2_ENTER,
    ENFORCEMENT_STAGE2_EXIT,
    ENFORCEMENT_STAGE3_ENTER,
    ENFORCEMENT_STAGE3_EXIT,
    ENFORCEMENT_STAGE4_ENTER,
    ENFORCEMENT_STAGE4_EXIT,
    enforcement_capacity_reversion,
    enforcement_capacity_gain,
    enforcement_stage_next,
    contract_enforcement_penalty_multiplier,
    contract_enforcement_trust_amplifier,
)
# 2026-08-15、物々交換・自給制度(制度5)の実装(Step 13)。
# institutions/barter.pyも同じ形で明示的に再公開する。他のinstitutions/*.pyの
# いずれにも依存しない(barter.py自身のdocstring「上位制度との連動について」参照)。
from institutions.barter import (
    BARTER_STAGE_FUNCTIONING,
    BARTER_STAGE_THINNED,
    BARTER_STAGE_SUBSISTENCE_ONLY,
    BARTER_STAGE_SHORTAGE,
    BARTER_STAGE_NAMES,
    BARTER_STAGE_TRIGGER_NAMES,
    BARTER_SCOPE,
    FOOD_STOCK_INITIAL,
    MEDICINE_STOCK_INITIAL,
    SHELTER_DURABILITY_INITIAL,
    TOOLS_DURABILITY_INITIAL,
    PRODUCTION_CAPACITY_INITIAL,
    PRODUCTION_CAPACITY_CAP,
    PRODUCTION_CAPACITY_DISRUPTION_PER_TURN,
    GOODS_CAP,
    GOODS_ACCOUNTING_VERSION,
    PROVISIONING_SCALE_INITIAL,
    BARTER_STAGE1_ENTER,
    BARTER_STAGE1_EXIT,
    BARTER_STAGE2_ENTER,
    BARTER_STAGE2_EXIT,
    BARTER_STAGE3_ENTER,
    BARTER_STAGE3_EXIT,
    FOOD_UPKEEP_PER_TURN,
    MEDICINE_UPKEEP_PER_TURN,
    SHELTER_WEAR_PER_TURN,
    TOOLS_WEAR_PER_TURN,
    ESSENTIAL_GOODS_SHORTAGE_ENERGY_PENALTY,
    ESSENTIAL_GOODS_SHORTAGE_HEALTH_PENALTY,
    BARTER_ARCHETYPE,
    SUBSISTENCE_ARCHETYPE,
    production_capacity_reversion,
    barter_goods_upkeep,
    background_goods_production,
    normalize_provisioning_scale,
    goods_reference,
    goods_capacity,
    goods_coverage,
    barter_choice_effects,
    barter_choice_evaluation,
    worst_shortfall,
    barter_stage_next,
    essential_goods_shortage_penalty,
)
# 2026-08-14(Step 2A、段階的モジュール分割)。イベントログの保存層。
# event_store.py はgame.pyをimportしない(パスは呼び出し側=game.pyが渡す)。
from event_store import append_jsonl_event, iter_jsonl_events
# 2026-08-14(Step 2B、段階的モジュール分割)。イベント列→世界状態の投影層。
# projection.py はgame.py・event_store.pyのいずれもimportしない
# (game.py側の動的な依存はProjectionDependencies経由で明示的に渡す、下記)。
from projection import ProjectionDependencies, reduce_events
# 2026-08-14(Step 4A、段階的モジュール分割)。清算(契約の履行・不履行)
# まわりの数値ルールを置くエンジン層。engine.py はgame.py・event_store.py・
# projection.pyのいずれにも依存しない(game.py側の動的な依存は
# SettlementEngineDependencies経由で明示的に渡す、下記の
# _settlement_engine_dependencies()参照)。モジュールごとimportするのは、
# game.py側の互換ラッパーがengine.SettlementEngineDependencies(...)・
# engine.build_settlement_choices(...)の形でそのまま呼べるようにするため。
import engine
# 2026-08-14(Step 5A、段階的モジュール分割)。main()とsimulate_policy()が
# 共用する方針評価・自動選択層。動的な資源ルールと乱数選択は
# PolicyDependencies経由で受け取る。
import policy as policy_engine
# 2026-08-14(Step 5B、段階的モジュール分割)。資源の減衰・回復・上限・
# 支払い可能性を扱う純粋ルール。設定値は呼び出し時に依存として渡す。
import resource_rules
# 2026-08-14(Step 5C、段階的モジュール分割)。特性の成長・基底減衰・老化を
# 扱う純粋ルール。CLIから変更可能なD_BASEも呼び出し時に依存として渡す。
import trait_rules
# 2026-08-14(Step 5D、段階的モジュール分割)。価格水準・賃金前借り・所得の
# 計算を扱う純粋ルール。CLIから変更可能なCREATION_RATEも呼び出し時に依存
# として渡す。
import economic_rules
# 2026-08-14(Step 5E、段階的モジュール分割)。LLM入力のコスト検証と行動
# アーキタイプのコスト・所要時間抽選を扱う純粋ルール。乱数(random.randint)・
# price_indexも呼び出し時に依存として渡す。
import action_costs
# 2026-08-14(Step 5F、段階的モジュール分割)。対人信用(NPCのtrust・評判)・
# 契約上限を扱う純粋ルール。price_indexも呼び出し時に依存として渡す。
import relationship_rules
# 2026-08-14(Step 5H、段階的モジュール分割)。契約清算(labor返済・不履行
# ペナルティ)のコスト計算を扱う純粋ルール。乱数(random.uniform)・
# price_indexも呼び出し時に依存として渡す。
import contract_costs
# 2026-08-14(Step 5I、段階的モジュール分割)。social型契約の相手選択を
# 扱う純粋ルール。rngは必須引数として呼び出し側から渡す(randomモジュールへの
# 暗黙依存なし)。
import counterparty_selection
# 2026-08-15(Step 6B、段階的モジュール分割)。main()とsimulate_policy()に
# 重複していた、銀行・通貨の信用回帰とstage遷移判定の「計画」部分(イベント
# 発行・状態更新は含まない)を扱う純粋ルール。
import turn_engine
# 2026-08-15(Step 6E、段階的モジュール分割)。main()の清算効果イベント列の
# 組み立てを扱う純粋関数。イベント保存(append_event)自体は行わない。
import event_builders
# 2026-08-15(Step 7、段階的モジュール分割)。「LLMによる状況生成」責務全体
# (プロンプト構築・応答解析・ターン生成・整合性チェック)をまとめて移動した。
# call_ollama本体・pick_theme・main()・process_one_situationはgame.py側に残る。
import scenario_generation
# 2026-08-15(Step 8、段階的モジュール分割)。main()内のネスト関数だった
# process_one_situation(1状況の提示・選択・適用・ナレーション・整合性チェック)
# を、処理全体のまとまりのまま移動した。
import interactive_runtime
# 2026-08-15(Step 9、段階的モジュール分割)。「LLMを使わないオフライン検証・
# シミュレーション」責務全体(simulate_policy・check_trajectory_criteria・
# run_policy_check・run_policy_probe・equilibrium・simulate_forced・
# simulate_mixed・run_simulation)をまとめて移動した。
import offline_simulation
# 2026-08-15(Step 10、段階的モジュール分割)。main()の本体だった「イベント
# ソーシング方式のゲームセッション実行」責務全体(NPC/キャラクター初期化・
# ターン開始処理・清算ループ・時間予算ループ・月末成長・死亡判定・サマリ)を
# まとめて移動した。main()にはargparse・CLI設定値の上書き・各種オフライン
# モードの分岐・run_game_session()の呼び出しだけが残る。
import game_session
# 2026-08-15(Step 11、段階的モジュール分割)。「LLM接続・プロンプト方針・
# ナレーション整合性検証」責務全体(call_ollama・code_check_narration・
# run_negative_control・システムプロンプト4種・矛盾検出の検出設定・
# NEGATIVE_CONTROL_CASES)をまとめて移動した。
import llm_integration

OLLAMA_HOST = "http://192.168.0.210:11434"
DEFAULT_EVENTS_NAME = "events.jsonl"
EVENTS_PATH = Path(__file__).parent / DEFAULT_EVENTS_NAME  # main() で上書きされうる

# ollamaにモデルをVRAMへ留めておく時間。Phase 0 の判断("NAS本番へVRAMを早く返す"を
# 優先し、ollama既定と同じ5分を意図的に明示する)をそのまま踏襲。
KEEP_ALIVE = "5m"

INITIAL_RESOURCES = {
    # "time" は2026-08-14に削除(時間予算制〈TURN_TIME_BUDGET〉へ統合)。
    # 月内に使える時間はもはやストック/フロー資源ではなく、ターンごとに
    # 新規に配られる予算(繰り越し無し)として別枠で管理する。
    # "trust"(プレイヤー個人の抽象的な信用)は2026-08-14に削除(ユーザー提案
    # 「自分の信用というステータスというより、相手からの信用に一本化したい。
    # 相手がいて初めて値を持つものとしたい」)。既にNPC個別のtrust
    # (npc_trust_gain等)・銀行のtrust(bank_trust)という「相手ごとの信用」が
    # 完全に別立てで実装済みで、プレイヤー側の汎用trustはそれと二重に
    # 帳簿を付けていただけだった(履行/不履行のたび両方が動いていた)。
    # 「相手がいなければ値を持たない」という設計をそのまま体現する形で、
    # 汎用trustを削除し、相手固有のtrust(NPC/銀行)一本に統合する。
    "energy": 100,   # フロー: 体力
    "peace": 100,    # フロー: 心の余裕
    "money": 0,     # ストック: お金(Phase 0 は 5。維持コストを入れたぶん初期値を上げた。
                    # 2026-08-13、維持コストの実体は銀行NPC+価格水準方式に変わったが
                    # 初期値自体は変更していない)
}
ALLOWED_RESOURCES = set(INITIAL_RESOURCES.keys())
MAX_DELTA = 50  # 1回の選択で資源が動く量の上限(LLMが桁を暴走させる対策。llmモード用)。
                # 2026-08-14、INITIAL_RESOURCESの10倍スケール化に合わせて再較正
                # (labor archetypeの最大コスト-50に対応)

# --- エントロピー原則(ストック資産の維持コスト) ------------------------------
# 「何もしなくても緩やかに目減りする」。
#
# money: 2026-08-13、比例減衰を「銀行NPC+マネーサプライ(M_total)+価格水準」方式に
#   置き換えた(docs/plan.md「経済の閉じ方 v1実装設計」)。旧・比例減衰
#   (`money -= rate × money`、固定量からの訂正)は、この設計の近似解(誰が・なぜ
#   貨幣を作っているかを省略し、正味の効果だけを個人の財布に直接書いたもの)だった、
#   という関係。詳細は BANK_NPC_ID・CREATION_RATE 周りのコメントを参照。
#   同じ現象を二重に説明しないよう、DECAY_RULESからmoneyは削除する
#   (ただしopusレビューで「シンクが無くなり無限発散側の病理が再来した」と指摘され、
#   賃金を「銀行への返済義務のある借入」として契約化する形で改めてシンクを設けた。
#   詳細は docs/plan.md「v1実装のopusレビューと訂正」を参照)。
# trust: 2026-08-14、プレイヤー個人の汎用trust自体を削除した(相手ごとの信用
#   〈NPC/銀行のtrust〉への一本化。上記INITIAL_RESOURCESのコメント参照)ため、
#   DECAY_RULESは空のまま残す(REGEN_RULESと同じく安全なno-op)。
DECAY_RULES = []

# --- フロー資源の回復 -------------------------------------------------------
# 初版(events_run1_auto14.jsonl)はtime/energyを減らす一方で回復手段が無く
# 破綻した経緯があり、しばらくは「ターンごとに自動で一定量戻る」受動的な
# regenで対応していた。2026-08-14、時間予算制の統合にあたりユーザー指摘
# 「フロー資源の回復の概念の整理が必要?」を受けて廃止し、回復経路を
# 「休息アクティビティ(能動的に時間を使う)」+「月内の余り時間の自動休息換算」
# の1つに一本化した(受動的regenとの併存は、休息を選ぶ意味を薄めるため)。
# REGEN_RULESは空のまま残す(compute_regenは空なら何もしない安全なno-op)。
REGEN_RULES = {}

# 上限(resource_cap)を持つフロー資源の一覧。2026-08-14追加。
# 元々はclamp_gainがREGEN_RULESのキー集合を「上限クリップすべき資源」の
# 目印として流用していたが、REGEN_RULESを空にした際にclamp_gainの上限
# クリップ判定まで無効化してしまっていた(=peace/energyが上限100を
# 超えられるバグ)。「受動的regenの有無」と「上限を持つかどうか」は別の
# 概念なので、明示的な定数として分離する。
FLOW_RESOURCES = {"energy", "peace"}

# --- 収入(元は run1 の死の螺旋対策として追加) ---------------------------------
# 2026-08-13、無条件でmoney+4が湧く仕様から、銀行NPCからの賃金支払いへ置き換えた
# (docs/plan.md「経済の閉じ方 v1実装設計」)。WAGE_BASE(下記)が
# price_index(0)=1のときこのINCOME_AMOUNTと一致するよう作ってあるため、
# ゲーム開始直後の挙動は変えていない。
INCOME_INTERVAL = 4
INCOME_AMOUNT = 40  # 2026-08-14、10倍スケール化(WAGE_BASEが自動追従)

# 契約を履行/不履行したときのプレイヤー個人trustへの増減(CONTRACT_FULFILL_BONUS・
# contract_fulfill_bonus・contract_default_penalty)は、2026-08-14に削除した
# (ユーザー提案「相手からの信用に一本化したい」。汎用trust自体の削除に伴い、
# 相手ごとのtrust増減〈npc_trust_gain/bank_trust_gain、main()の「4. 適用」節〉
# との二重計上を解消した)。

# 資源はこの値を下回る支払いができない(払えない選択肢は選べなくなる)。
# ただし「不履行」だけは常に選べる(払えないから踏み倒す、が成立するように)。
AFFORD_FLOOR = 0

# ==============================================================================
# 銀行NPC・マネーサプライ(M_total)・価格水準 — docs/plan.md「経済の閉じ方 v1実装設計」
# ==============================================================================
# 「お金の比例減衰をマネーサプライの増加で表現する方が現実的では」という指摘から
# 詰めた設計。従来の`INCOME_AMOUNT`(条件無しでmoney+4)を「銀行からの賃金振込」
# という**実際の取引**に直し、「お金が動くときは必ず契約(取引相手)を伴う」という
# 既存ルールを収入にも適用する(凍結していた「経済の閉じ方」節の一部解除)。
#
# 重要な訂正の経緯: 当初「M_total=追跡している全財布の合計」で定義しようとしたが、
# NPCが銀行1体だけの構成だと、賃金の振込(銀行→プレイヤー)は振替にすぎず合計は
# 変化しない(振替は定義上ゼロサム)。**M_totalは特定の財布の合計ではなく、
# プレイヤーの行動と無関係に背景で伸びる独立したスカラー**として持つ(現実の
# マネーサプライ統計が中央銀行自身の負債を含めないのと同じ発想。ただし本ゲームでは
# 「他に数える主体」も無いので、その代わりに外生的な背景プロセスとして近似する)。
#
# さらに実装着手直前、`docs/plan.md`「資源体系全体の統一」で検証済みの
# `rate=0.125`(比例減衰の係数)をそのまま`creation_rate`に転用しようとして、
# 実測で1年後4.11倍・80年後1.28×10⁴⁹倍という破綻した数字だと判明(旧`rate`は
# 「プレイヤーの財布の均衡点」を決めるゲームバランス上の数字で、複利で伸び続ける
# 価格指数の妥当性は保証していなかった)。年率3%相当(80年で約11倍)に選び直した。
BANK_NPC_ID = "bank"
BANK_NPC_NAME = "中央銀行"
# 2026-08-14追加(外部レビュー指摘・社会レジーム仕様との整合): 「実給与」が
# まだ銀行発行になっており、銀行が崩壊すると給与の支払主体が無くなる矛盾が
# あった(docs/social-regimes-spec.md「銀行・金融制度」Stage2の設計意図
# 「銀行が壊れても労働による所得は残る」に反する)。salary(前借りでは
# ない本当の所得、SALARY_BASE)の発行主体を銀行から切り離すため、別の
# NPCを新設する——「地域経済・労働市場」という、銀行の生死と独立に存在する
# 主体からの支払い、という位置づけ。
ECONOMY_NPC_ID = "economy"
ECONOMY_NPC_NAME = "地域経済"
# 1ターン(=1ヶ月)あたりの価格水準の伸び率。年率約3%相当(仮値)。
# 概念上はこれは**銀行自身の金融政策の結果**であり、本来は銀行の信用(plan.md
# 「[将来] 銀行NPC自身のtrust」)に応じて動く変数のはず。ただしtrust自体を
# v1では実装しないため、「今の金融政策のもとでの固定値」というスナップショットと
# して定数のまま置く(trustを実装する段になったら、この定数を銀行の状態から
# 導出する関数に置き換える)。
CREATION_RATE = 0.0025
WAGE_BASE = INCOME_AMOUNT  # price_index(0)=1のとき、旧INCOME_AMOUNTと一致するように

# 2026-08-14追加(ユーザーが持ち込んだ外部レビュー指摘・P0-3「所得と融資を
# 分離する」)。従来のWAGE_BASEは「銀行からの全額借入(前借り)」で、money型で
# 返済すると受け取った額と返す額が完全に相殺し純所得がゼロになる構造だった
# (実測で確認済み)。これがmoney選択率が慢性的に約1%だった真因——
# SHADOW_PRICEの較正では直らない。WAGE_BASE(前借り、返済義務あり)自体は
# そのまま維持しつつ、返済義務の無い「本当の所得」をSALARY_BASEとして
# 別立てで新設する。銀行が発行する点は前借りと同じだが、対応する契約
# (npc_wallet_changedのみでcontract_createdが無い=返済請求権を持たない)を
# 作らない——「銀行が恒久的に通貨を発行する」経路として前借り(貸付・
# 返済請求あり)と明確に区別する(P0-4「bank.moneyの定義」の土台にもなる)。
SALARY_BASE = 25  # 仮値。960ターン×3seed×3方針でスイープした結果:
                   # 15→money選択率3.6%/20→4.3%/25→5.2%/30→5.8%
                   # (デフォルト率は27.8%→27.8%→25.1%→21.3%と単調改善)。
                   # 25で選択の多様性基準(money>=5%)を初めて上回ったため採用

# --- 通貨危機(2026-08-13追加、ユーザー指摘「踏み倒しが多いなら銀行がすぐ破綻する
# ようにしたい」への対応) -------------------------------------------------------
# docs/plan.md「経済の閉じ方」で設計転換した「均衡点ではなく踊り場+通貨危機」の
# 実装。銀行債務の踏み倒し(信用損失、実質値)が閾値を超えると、通貨のデノミ
# ネーションが起きる。price_index()自体は変えない(=毎ターンの計算は今までどおり
# ターン数だけの純関数のまま)設計にした——危機は「価格水準を状態化して不連続に
# ジャンプさせる」のではなく、**名目の残高を直接割り直す一度きりの出来事**として
# 表現する方が、既存のprice_index呼び出し箇所(8か所以上)に手を入れずに済み、
# 「デノミで実質価値が一度に失われる」という危機の痛みも自然に表現できる。
#
# 2026-08-13、4回目opusレビュー指摘で訂正: 20は名目スケールに対して過大で、
# 仮に発火してもopen契約のrepay_moneyがほぼ全て0に丸まり、罰ではなく徳政令に
# なっていた。名目スケール(典型的な賃金債券が数十程度)に合わせて3へ縮小。
CRISIS_REBASE_FACTOR = 3  # 仮値。危機発生時、名目moneyをこの倍率で割り直す

# --- NPCの信用(trust)・倫理観(ethics) — 財の価格形成の土台(2026-08-13追加) ---
# docs/plan.md「[将来] 銀行NPC自身のtrust」「[将来] 財の価格形成」の実装。
# 「少ないパラメータで均衡させようとするのではなく、要素を増やしながら実証する」
# というユーザーの方針転換を受け、trustの局所的な微調整はやめてこちらに進む。
NPC_TRUST_INITIAL = 50.0         # NPC(銀行以外)の初期信用。仮値(顔なじみが
                                  # 誰も居ない、評判の実績がまだ無い最初の相手に
                                  # 使う中立値。2人目以降はplayer_reputation()を使う)
# BANK_TRUST_INITIALはinstitutions/bank.pyへ移動(Step 1、2026-08-14)。
NPC_ETHICS_RANGE = (20.0, 80.0)  # 倫理観の抽選レンジ。低いほど悪徳(強欲)、高いほど良心的

# 2026-08-14追加(ユーザー提案「契約は互いの信用値が十分に高くないと成立しない。
# 相手がどういう契約を持っているか、周りの人間からの信用値という情報によっても
# 信用値は変動する」):
# (1) 契約(新規のsocial型)の成立には、既存の相手ならtrustが一定以上必要にする。
#     新規の相手(初対面)はまだ個別の実績が無いのでこの下限の対象外とし、その
#     代わり(2)の評判が初期trustとして反映される、という形にする(「初対面でも
#     完全に無関係ではいられない」という設計)。
# (2) 「周りの人間からの信用値」= プレイヤーの一般的な評判。個々のNPC関係とは
#     別に社会ネットワークをまるごと作るのではなく、既存の顔なじみのtrustの
#     単純平均で近似する(新しい仕組みを増やさない)。新規NPCの初期trustに
#     この評判を使うことで、「悪評が広まれば初対面の相手からも警戒される」
#     「良い評判があれば初対面でも多少信用される」を、既存のnpc_trust一本の
#     仕組みだけで表現する。
NPC_CONTRACT_TRUST_MIN = 35.0  # 仮値。既存の相手との新規契約に必要な最低trust
                                # (NPC_TRUST_INITIAL=50から、defaultの-8.0が
                                # 2回続けば下回る水準)
# 2026-08-14訂正(ユーザー指摘「全員のtrustが下限を割ればゲームが詰むのでは?」
# への実測での検証で発覚): 上記(1)のゲートをプール上限フォールバックにも
# 厳密適用したところ(pick_social_counterparty参照)、新規紹介が91.6%まで
# 急増し、960ターンで503人もの別人が登場、その**全員**のtrustが
# NPC_CONTRACT_TRUST_MIN未満(平均17.9・最大でも34.9)という「評判崩壊
# スパイラル」が起きた。原因はplayer_reputationが疎遠になった相手も含めて
# 平均していたため、低trustの一見さんが増えるほど評判がさらに下がり、次の
# 新規relationshipの初期trustも下がる…という自己強化ループだった。
# 2点訂正する: (a) 評判の集計対象を現役(疎遠になっていない)関係だけに絞る
# (turnを追加引数に取る)。(b) 新規relationshipの初期trustを評判に完全に
# 連動させず、NPC_TRUST_INITIAL(中立)との加重平均にする——評判が0まで
# 落ちても初期trustが必ずNPC_CONTRACT_TRUST_MINを上回るように
# NPC_REPUTATION_WEIGHTを選ぶ((1-0.25)×50+0.25×0=37.5>35)。
NPC_REPUTATION_WEIGHT = 0.25  # 仮値。新規relationshipの初期trustにおける評判の影響度


def _relationship_rules_dependencies() -> relationship_rules.RelationshipRulesDependencies:
    """現在のgame.py設定から対人信用ルールの動的依存を組み立てる。
    NPC_TRUST_CAP・CONTRACT_CREDIT_MULTIPLIER_MAX・NPC_TRUST_REVERSION_RATEは
    この関数より後方で定義されるが、関数本体内の参照は呼び出し時に解決される
    ので問題ない(importの実行順序には影響しない)。"""
    return relationship_rules.RelationshipRulesDependencies(
        npc_trust_initial=NPC_TRUST_INITIAL,
        npc_contract_trust_min=NPC_CONTRACT_TRUST_MIN,
        npc_reputation_weight=NPC_REPUTATION_WEIGHT,
        npc_trust_cap=NPC_TRUST_CAP,
        contract_credit_multiplier_max=CONTRACT_CREDIT_MULTIPLIER_MAX,
        income_interval=INCOME_INTERVAL,
        salary_base=SALARY_BASE,
        price_index_fn=price_index,
        npc_trust_reversion_rate=NPC_TRUST_REVERSION_RATE,
        local_credit_stage_personal=LOCAL_CREDIT_STAGE_PERSONAL,
        local_credit_initial_trust_weight=LOCAL_CREDIT_INITIAL_TRUST_WEIGHT,
    )


def player_reputation(acquaintances: dict, turn: int) -> float:
    """relationship_rules.pyの実装を再公開する互換ラッパー。"""
    return relationship_rules.player_reputation(
        acquaintances, turn, dependencies=_relationship_rules_dependencies())


def initial_trust_for_new_npc(acquaintances: dict, turn: int,
                              local_credit_stage: int = None,
                              community_trust: float = None) -> float:
    """relationship_rules.pyの実装を再公開する互換ラッパー。

    2026-08-15訂正(ユーザーレビュー指摘への対応): community_trustをブレンド
    するかどうかのStage判定・ブレンド式は、以前このラッパー内に書かれていた
    が、「game.pyは配線だけ」という境界を守るためrelationship_rules.
    initial_trust_for_new_npc()自体へ移設した。このラッパーは呼び出し時点の
    player_reputation関数オブジェクトを渡すだけの薄い配線になる。"""
    return relationship_rules.initial_trust_for_new_npc(
        acquaintances, turn, dependencies=_relationship_rules_dependencies(),
        player_reputation_fn=player_reputation,
        local_credit_stage=local_credit_stage, community_trust=community_trust)


# 2026-08-14追加(ユーザー提案「信用は高くなっても低くなってもその後何もしなければ
# 10〜20年という長い時間をかけてだんだんと中間値に向かっていく。それに対して
# プラスの行動〈契約の履行や寄付やボランティア等〉をするか、マイナスの行動
# 〈契約の不履行や犯罪行為等〉をするかという話」)。
# 履行/不履行のたびに離散的にtrustを動かすだけだと、一度傷付いた・盛り上がった
# 関係がそのまま固定されてしまう(「何もしなければ現状維持」)。何もしなくても
# 極端さが薄れていく、という指摘を受けて、NPC_TRUST_INITIAL(中立)への指数的な
# 回帰を導入する。
# 「寄付・ボランティア(プラス)」「犯罪行為(マイナス)」は現状は行動として
# 実装が無い(履行/不履行のみ)ので、今回はこの回帰の仕組みだけを入れる。
# 新しい行動アーキタイプの追加は別の実装ステップとして切り出す(docs/plan.md参照)。
#
# main()はイベントソーシングなので、経過ターンぶん「回帰イベント」を1件ずつ
# 積む実装(NPCの数×ターン数ぶん)は現実的でない。「式にする」方針のとおり、
# npc_trust_changedが最後に起きたターンからの経過ターン数を使った閉じた式
# (指数減衰の解)で、読み出し時にその場で計算する——ログには最後の明示的な
# 変化だけを記録し、回帰そのものはイベントを積まない導出値として扱う。
NPC_TRUST_REVERSION_RATE = 0.0125  # 仮値。10年(120ターン)で元のズレの約8割、
                                    # 15年(180ターン)で約9割、20年(240ターン)で
                                    # 約95%が中立値(NPC_TRUST_INITIAL)に戻る速さ


def effective_npc_trust(npc: dict, turn: int) -> float:
    """relationship_rules.pyの実装を再公開する互換ラッパー。

    projection.pyのProjectionDependencies.effective_npc_trust_fnには、この
    ラッパーの関数オブジェクトそのものがPROJECTION_DEPENDENCIES構築時
    (import時1回)に渡される。ラッパー内部で_relationship_rules_dependencies()
    を毎回構築するため、NPC_TRUST_INITIAL・NPC_TRUST_REVERSION_RATEの現在値は
    呼び出し時に解決される——PROJECTION_DEPENDENCIES自体を動的factory化する
    必要はない(Step 5G、既存のPROJECTION_DEPENDENCIES構築方式は変更しない)。"""
    return relationship_rules.effective_npc_trust(
        npc, turn, dependencies=_relationship_rules_dependencies())

# --- NPC永続化の展開(2026-08-13追加、ユーザー指摘「複数NPCの永続化が無いと
# 本格的な検証が出来ない」) -----------------------------------------------------
# 5回目opusレビューが指摘した「probe/main()でNPC生成則が違う」の根本原因は、
# main()側がLLMの自由な命名(social_counterpartyをLLMが答える)に頼っており、
# 「同じ名前が2回目以降に出てきたら同一人物」という**再現率が制御できない賭け**
# だったこと。ACTION_ARCHETYPESで確立した「コストはコードが決める、LLMは
# ラベルと描写のみ」という設計原則を、社会的契約の相手選びにも広げる
# ——**誰に頼るかもコードが決め、LLMには「この名前の人物として描写せよ」と
# 指示するだけにする**。これによりprobe/main()は文字どおり同じ関数
# (pick_social_counterparty)で相手を選ぶようになり、「probeでは合成、実走は
# LLM任せ」という非対称が構造的に解消される。
#
# 第2弾(同日、ユーザーから3件の指摘を受けて訂正):
# 1. 「8人って少なすぎない?」→ 実測したところ960ターン(≈80年)で1人あたり
#    24〜36回も貸し借りを繰り返しており、同じ相手と生涯ずっと付き合い続ける
#    という不自然さが出ていた。既存だが未使用だったnpc_died/alive基盤とは
#    別に、関係そのものに寿命(NPC_RELATIONSHIP_SPAN_RANGE)を持たせ、
#    期限が来た関係は自然に疎遠になってプールから外れるようにした(死亡では
#    なく疎遠——npc_diedは将来の「NPCの死」用に温存する)
# 2. 「2回以上借りを作れないというのもよく分からない制限。お人好しなら
#    何回でも貸してくれるのでは」→ 一律禁止をやめ、その相手の倫理観
#    (ethics、既存の軸をそのまま再利用)が高いほど、まだ返し終えていなくても
#    重ねて貸してくれる確率を上げる形にした
# 3. 「人によって付き合う人数に差もあるよね」→ NPC_POOL_CAPを固定値ではなく
#    レンジにし、talentと同じ「生まれつきの個人差を1回だけ抽選してイベントに
#    固定する」パターンでcharacter_born時にキャラごとの値を確定する
#    (npc_pool_cap。reduce_state経由でstateから読める)
NPC_POOL_CAP_RANGE = (8, 20)             # 仮値。生まれつきの社交性(同時に
                                          # 付き合える人数の上限)の抽選レンジ
NPC_RELATIONSHIP_SPAN_RANGE = (120, 240)  # 仮値。関係の寿命(10〜20年、
                                          # YEARS_PER_TURN=1/12より)。これを
                                          # 超えると自然に疎遠になる
NPC_REUSE_RATE = 0.7   # 顔なじみが1人以上いて再利用可能なとき、既存の相手を選ぶ確率
                        # (旧・probeのみのハードコード値をmain()にも展開し統一)


def _counterparty_selection_dependencies() -> counterparty_selection.CounterpartySelectionDependencies:
    """現在のgame.py設定から相手選択ルールの動的依存を組み立てる。
    呼び出しのたびに現在値を読むため、CLIやテストによる定数の一時差し替えが
    そのまま反映される(import時点で固定しない)。"""
    return counterparty_selection.CounterpartySelectionDependencies(
        npc_ethics_range=NPC_ETHICS_RANGE,
        npc_reuse_rate=NPC_REUSE_RATE,
        npc_trust_initial=NPC_TRUST_INITIAL,
        npc_contract_trust_min=NPC_CONTRACT_TRUST_MIN,
    )


def pick_social_counterparty(acquaintances: dict, open_counterparties: set,
                              turn: int, npc_pool_cap: int, rng=random) -> tuple:
    """counterparty_selection.pyの実装を再公開する互換ラッパー。"""
    return counterparty_selection.pick_social_counterparty(
        acquaintances, open_counterparties, turn, npc_pool_cap, rng,
        dependencies=_counterparty_selection_dependencies())

# 2026-08-13、4回目opusレビュー指摘で追加: NPC・銀行のtrustに上限が無く、
# 履行を重ねるだけで無限に伸びていた(player trustと同じ病理を、bank/NPC trustに
# 再導入していた)。TRUST_CAPと同じ逓減パターンを適用する上限を明示する
# (BANK_TRUST_CAP・CRISIS_THRESHOLD_BASEはinstitutions/bank.pyへ移動、Step 1)。
NPC_TRUST_CAP = 100.0


def npc_price_modifier(npc: dict) -> float:
    """relationship_rules.pyの実装を再公開する互換ラッパー。"""
    return relationship_rules.npc_price_modifier(
        npc, dependencies=_relationship_rules_dependencies())


# 2026-08-14(Step 1、段階的モジュール分割): 銀行・通貨の定数と純粋関数
# (crisis_threshold/bank_trust_gain/bank_trust_reversion/bank_stage_next、
# currency_confidence_reversion/currency_confidence_gain/currency_stage_next、
# および関連定数)は institutions/bank.py・institutions/currency.py へ移動した
# (ファイル冒頭のimport節で再公開しているので、このファイル内での呼び出し方は
# 変わらない)。移動先で定数・計算式・コメントはそのまま引き継いでいる。


def _institution_upkeep_dependencies() -> turn_engine.InstitutionUpkeepDependencies:
    """現在のgame.py関数(institutions/bank.py・institutions/currency.pyの
    再公開)から、銀行・通貨の信用回帰とstage遷移判定の動的依存を組み立てる。
    ここで渡す4関数はgame.py側の名前解決を経由するので、テストによる
    monkeypatch(例: game.bank_stage_next = ...)も呼び出し時点で反映される。"""
    return turn_engine.InstitutionUpkeepDependencies(
        bank_trust_reversion_fn=bank_trust_reversion,
        currency_confidence_reversion_fn=currency_confidence_reversion,
        bank_stage_next_fn=bank_stage_next,
        currency_stage_next_fn=currency_stage_next,
    )


def plan_confidence_reversion(bank_trust: float, currency_confidence: float) -> dict:
    """turn_engine.pyの実装を再公開する互換ラッパー。"""
    return turn_engine.plan_confidence_reversion(
        bank_trust, currency_confidence, dependencies=_institution_upkeep_dependencies())


def plan_institution_transitions(bank_stage: int, bank_trust: float, bank_crisis_count: int,
                                 currency_stage: int, currency_confidence: float) -> dict:
    """turn_engine.pyの実装を再公開する互換ラッパー。"""
    return turn_engine.plan_institution_transitions(
        bank_stage, bank_trust, bank_crisis_count, currency_stage, currency_confidence,
        dependencies=_institution_upkeep_dependencies())


def _local_credit_upkeep_dependencies() -> turn_engine.LocalCreditUpkeepDependencies:
    """現在のgame.py関数(institutions/local_credit.pyの再公開)から、地域信用の
    信用回帰とstage遷移判定の動的依存を組み立てる。_institution_upkeep_dependencies()
    と同じ理由(monkeypatch伝播)で、呼び出しのたびに新規構築する。"""
    return turn_engine.LocalCreditUpkeepDependencies(
        community_trust_reversion_fn=community_trust_reversion,
        local_credit_stage_next_fn=local_credit_stage_next,
    )


def plan_local_credit_reversion(community_trust: float) -> dict:
    """turn_engine.pyの実装を再公開する互換ラッパー。"""
    return turn_engine.plan_local_credit_reversion(
        community_trust, dependencies=_local_credit_upkeep_dependencies())


def plan_local_credit_transition(local_credit_stage: int, community_trust: float) -> dict:
    """turn_engine.pyの実装を再公開する互換ラッパー。"""
    return turn_engine.plan_local_credit_transition(
        local_credit_stage, community_trust, dependencies=_local_credit_upkeep_dependencies())


def _contract_enforcement_upkeep_dependencies() -> turn_engine.ContractEnforcementUpkeepDependencies:
    """現在のgame.py関数(institutions/contract_enforcement.pyの再公開)から、
    契約執行の信用回帰とstage遷移判定の動的依存を組み立てる。
    _local_credit_upkeep_dependencies()と同じ理由(monkeypatch伝播)で、
    呼び出しのたびに新規構築する。"""
    return turn_engine.ContractEnforcementUpkeepDependencies(
        enforcement_capacity_reversion_fn=enforcement_capacity_reversion,
        enforcement_stage_next_fn=enforcement_stage_next,
    )


def plan_enforcement_reversion(enforcement_capacity: float) -> dict:
    """turn_engine.pyの実装を再公開する互換ラッパー。"""
    return turn_engine.plan_enforcement_reversion(
        enforcement_capacity, dependencies=_contract_enforcement_upkeep_dependencies())


def plan_enforcement_transition(enforcement_stage: int, enforcement_capacity: float) -> dict:
    """turn_engine.pyの実装を再公開する互換ラッパー。"""
    return turn_engine.plan_enforcement_transition(
        enforcement_stage, enforcement_capacity,
        dependencies=_contract_enforcement_upkeep_dependencies())


def _barter_upkeep_dependencies() -> turn_engine.BarterUpkeepDependencies:
    """現在のgame.py関数(institutions/barter.pyの再公開)から、物々交換・自給の
    財upkeepとstage遷移判定の動的依存を組み立てる。_contract_enforcement_
    upkeep_dependencies()と同じ理由(monkeypatch伝播)で、呼び出しのたびに
    新規構築する。"""
    return turn_engine.BarterUpkeepDependencies(
        barter_goods_upkeep_fn=barter_goods_upkeep,
        background_goods_production_fn=background_goods_production,
        production_capacity_reversion_fn=production_capacity_reversion,
        barter_stage_next_fn=barter_stage_next,
        goods_cap=GOODS_CAP,
        production_capacity_cap=PRODUCTION_CAPACITY_CAP,
        production_capacity_disruption_per_turn=PRODUCTION_CAPACITY_DISRUPTION_PER_TURN,
    )


def plan_barter_upkeep(food: float, medicine: float, shelter: float, tools: float,
                       production_capacity: float, disrupted: bool = False,
                       labor_factor: float = 1.0,
                       labor_factors_by_good: dict | None = None,
                       productivity_factors_by_good: dict | None = None,
                       provisioning_scale: float = 1.0,
                       demand_scales_by_good: dict | None = None) -> dict:
    """turn_engine.pyの実装を再公開する互換ラッパー。"""
    return turn_engine.plan_barter_upkeep(
        food, medicine, shelter, tools, production_capacity, disrupted,
        labor_factor, labor_factors_by_good, productivity_factors_by_good,
        provisioning_scale, demand_scales_by_good,
        dependencies=_barter_upkeep_dependencies())


def plan_barter_transition(barter_stage: int, worst_shortfall_score: float) -> dict:
    """turn_engine.pyの実装を再公開する互換ラッパー。"""
    return turn_engine.plan_barter_transition(
        barter_stage, worst_shortfall_score, dependencies=_barter_upkeep_dependencies())


def npc_trust_gain(current_trust: float) -> float:
    """relationship_rules.pyの実装を再公開する互換ラッパー。"""
    return relationship_rules.npc_trust_gain(
        current_trust, dependencies=_relationship_rules_dependencies())


def _economic_rules_dependencies() -> economic_rules.EconomicRulesDependencies:
    """現在のgame.py設定から経済ルールの動的依存を組み立てる。"""
    return economic_rules.EconomicRulesDependencies(
        creation_rate=CREATION_RATE,
        income_interval=INCOME_INTERVAL,
        wage_base=WAGE_BASE,
        salary_base=SALARY_BASE,
        bank_stage_healthy=BANK_STAGE_HEALTHY,
        bank_stage_contraction=BANK_STAGE_CONTRACTION,
        bank_stage_halted=BANK_STAGE_HALTED,
        bank_stage_collapsed=BANK_STAGE_COLLAPSED,
        stage1_advance_shrink=BANK_STAGE1_ADVANCE_SHRINK,
        stage2_advance_shrink=BANK_STAGE2_ADVANCE_SHRINK,
    )


def price_index(turn: int) -> float:
    """economic_rules.pyの実装を再公開する互換ラッパー。"""
    return economic_rules.price_index(
        turn, dependencies=_economic_rules_dependencies())


def compute_wage(turn: int, bank_stage: int = BANK_STAGE_HEALTHY) -> int:
    """economic_rules.pyの実装を再公開する互換ラッパー。"""
    return economic_rules.compute_wage(
        turn, bank_stage,
        dependencies=_economic_rules_dependencies(),
        price_index_fn=price_index)


def compute_salary(turn: int) -> int:
    """economic_rules.pyの実装を再公開する互換ラッパー。"""
    return economic_rules.compute_salary(
        turn, dependencies=_economic_rules_dependencies(),
        price_index_fn=price_index)


# ==============================================================================
# 特性(器用さ・思考力・スキル・健康)— docs/plan.md「特性の成長経路」の実装
# ==============================================================================
# 資源(フロー/ストック)とは別階層の、0〜100スケールでゆっくり動く値。
# 資源と同じく **すべてコードが決める**(LLMには特性の存在すら知らせていない)。
#
#   基底減衰: 使わなかったターンは T -= D_base
#   成長:     labor/money型の選択が題材の特性と一致したターンに
#             ΔT = G_base × M_talent × (1 - T/100)
#
# D_base=0.12 は phase1_trait_exp/README.md で実測・検証済みの値
# (labor型・才能なしの平衡値が負に落ちない=死の螺旋を避ける値)。
TRAITS = ["dexterity", "intellect", "skill", "health"]
TRAIT_JA = {"dexterity": "器用さ", "intellect": "思考力",
            "skill": "スキル", "health": "健康"}
GROWABLE_TRAITS = ["dexterity", "intellect", "skill"]  # 健康は別モデル(下記)

INITIAL_TRAITS = {"dexterity": 50.0, "intellect": 50.0, "skill": 50.0, "health": 80.0}
TRAIT_MIN, TRAIT_MAX = 0.0, 100.0

D_BASE = 0.12          # 基底減衰(器用さ・思考力・スキル)
# 基礎成長量。money型(教育)の方が速い。2026-08-13、social型を追加
# (labor:2.0/money:4.0より小さい1.0)。3回目opusレビューで「trustが発散すると
# social型が支配戦略化し、labor/money型がほぼ選ばれなくなって特性成長の機会が
# 壊滅する」と指摘された。policy_scoreの内部(残高を見ない設計)を直すのではなく、
# 「どの型が選ばれても、多少なりとも成長の道は残る」という形で成長システム側を
# 頑健にする(「trustをいじって局所最適を探すより要素を増やす」というユーザー方針)。
# 対人関係で解決する経験も、多少はスキル等の糧になる、という自然な解釈でもある。
G_BASE = {"labor": 2.0, "money": 4.0, "social": 1.0}
B_TALENT = 0.5         # 才能補正。誕生時のスキルと一致すれば M_talent = 1 + B_talent

# --- 健康の別モデル(plan.md「健康は別モデルとして切り離す」) -------------------
# 練習で伸びるものではないので、成長式ではなく
#   ・老化に連動した基底減衰(毎ターン無条件。単純な線形)
#   ・money型(医療)/labor型(自己ケア)での回復
# の2本立て。時間粒度は docs/plan.md「世界の時間の流れ方」の決定に合わせる:
#   1ターン = 1ヶ月、1年 = 12ターン。開始年齢は意思決定年齢(13歳、新規スポーンの起点)。
START_AGE = 13
YEARS_PER_TURN = 1.0 / 12.0
HEALTH_DECAY_AT_START = 0.02   # START_AGE 時点の1ターンあたり減衰(= 年 0.24)
HEALTH_DECAY_PER_YEAR = 0.004  # 1歳あたり増える減衰量(線形。老年ほど加速する
                               # 非線形カーブは今回の対象外)
# 健康の回復量(G_base 相当)。money型(医療)のみだと、Phase 1の経済では money型が
# 資源不足で選べないターンが多く、回復経路が事実上発火しないと分かったため、
# labor型(休養・自炊・運動)にも小さい回復を与える(money型の方が速い、という
# 序列は保つ)。
HEALTH_CARE_GAIN = {"money": 4.0, "labor": 1.5}


def _trait_rules_dependencies() -> trait_rules.TraitRulesDependencies:
    """現在のgame.py設定から特性ルールの動的依存を組み立てる。"""
    return trait_rules.TraitRulesDependencies(
        start_age=START_AGE,
        years_per_turn=YEARS_PER_TURN,
        health_decay_at_start=HEALTH_DECAY_AT_START,
        health_decay_per_year=HEALTH_DECAY_PER_YEAR,
        talent_bonus=B_TALENT,
        growth_base=G_BASE,
        growable_traits=GROWABLE_TRAITS,
        health_care_gain=HEALTH_CARE_GAIN,
        traits=TRAITS,
        trait_min=TRAIT_MIN,
        trait_max=TRAIT_MAX,
        d_base=D_BASE,
    )


def age_at(turn: int) -> float:
    """trait_rules.pyの実装を再公開する互換ラッパー。"""
    return trait_rules.age_at(
        turn, dependencies=_trait_rules_dependencies())


def health_decay(turn: int) -> float:
    """trait_rules.pyの実装を再公開する互換ラッパー。"""
    return trait_rules.health_decay(
        turn,
        dependencies=_trait_rules_dependencies(),
        age_at_fn=age_at)


def talent_multiplier(talent, trait) -> float:
    """trait_rules.pyの実装を再公開する互換ラッパー。"""
    return trait_rules.talent_multiplier(
        talent, trait, dependencies=_trait_rules_dependencies())


def compute_trait_step(traits: dict, talent, theme_trait, choice_key: str,
                       turn: int, kind: str) -> tuple:
    """trait_rules.pyの実装を既存公開シグネチャで再公開する互換ラッパー。"""
    return trait_rules.compute_trait_step(
        traits, talent, theme_trait, choice_key, turn, kind,
        dependencies=_trait_rules_dependencies(),
        talent_multiplier_fn=talent_multiplier,
        health_decay_fn=health_decay)


# --- 行動アーキタイプ(コストはコードが決める。Phase 0 --cost-mode code と同じ) ---
# 2026-08-13追加(5回目opusレビュー指摘・優先順位5): peaceは全実測で一度も
# 初期値10を下回ったことが無かった。REGEN_RULESで毎ターン+3回復する一方、
# 減らす経路がmoney型の(0,1)=正のみで、5資源のうち1つが恒久的な定数に
# なっていた。labor(無理をする消耗)とsocial(人に借りを作る気まずさ・
# 気疲れ)にpeaceのコストを足し、実際にシンクとして機能させる。money型の
# (0,1)はそのまま残す(「お金を払って心の余裕を買う」という既存の解釈を
# 変えない)。moneyだけがpeaceを増やせる、という非対称性は据え置き。
# 2026-08-14、INITIAL_RESOURCESの10倍スケール化(time/energy/peace/trust:×10、
# money:フロー〈賃金・価格〉のスケールとして×10。詳細はdocs/plan.md参照)に
# 合わせて、すべてのコストレンジを10倍に再較正。SHADOW_PRICE等の比率パラメータは
# 全資源が一様に10倍されるので変更不要(相対順序が保たれるため)。
#
# 2026-08-14、時間予算制の統合(docs/plan.md「時間予算制」節)により、
# time資源そのものを廃止し、代わりに各archetypeに"hours"(この行動に何時間
# かかるか、TURN_TIME_BUDGETから引かれる)を持たせた。laborのコストから
# "time"を除き、新たに「休息」archetypeを追加した(G_BASEには含めない=
# 休息は成長チャネルではない、というユーザーの元設計どおり)。
# 2026-08-14訂正(6回目opusレビュー指摘・優先順位7): 1ターン=1ヶ月
# (YEARS_PER_TURN=1/12)なのに、TURN_TIME_BUDGET=24とhoursレンジ(2〜9h)は
# 「1日」の粒度だった——「人は月に4〜9時間しか眠らない/働かない」という
# 計算になっていた。1ターン=1日に変える(=既存の契約期限・CHECKPOINT_TURNS・
# 年齢換算・成長較正を全部作り直す)よりも、hours側を可処分時間の月間規模
# (月100〜200時間程度、レビュー実測に基づく目安)に引き直す方が既存の
# 較正を壊さない。相対比率(money:labor≈1:2.17、labor=rest)は据え置いた。
# (2026-08-14、1回目の再較正でlabor:restの対称性を誤って崩し〈40-90 vs
# 20-50〉、デフォルト率が63〜68%まで悪化する回帰を実測で確認・訂正した)。
TURN_TIME_BUDGET = 150  # 仮値。1ヶ月に配分できる可処分時間(繰り越し無し)
MONEY_TIME_COST_RANGE = (10, 25)   # 仮値。お金で解決する行動にかかる時間
SOCIAL_TIME_COST_RANGE = (10, 25)  # 仮値。人に頼る行動にかかる時間
# 「休息の時間は4〜9hと幅があるよね。全部に関して言えることだけど」
# (2026-08-13、ユーザー指摘)を受け、labor/restも固定値ではなくレンジにする。
# labor/restは常に同じレンジ(対称)にする——旧(4,9)/(4,9)の関係を保つ。
LABOR_TIME_COST_RANGE = (25, 55)  # 仮値
REST_TIME_COST_RANGE = (25, 55)   # 仮値
MIN_ACTIVITY_HOURS = min(MONEY_TIME_COST_RANGE[0], LABOR_TIME_COST_RANGE[0],
                         SOCIAL_TIME_COST_RANGE[0], REST_TIME_COST_RANGE[0])
# 予算がこれ未満に減ったら、その月はもうLLMに新しい行動を選ばせず、
# 残り時間を自動的に休息へ充当して打ち切る(docs/plan.md「時間予算制」節)。

ACTION_ARCHETYPES = [
    {"key": "money",  "name": "お金で解決する", "hours": MONEY_TIME_COST_RANGE,
     "ranges": {"money": (-40, -20), "peace": (0, 10)}},
    {"key": "labor",  "name": "自分の時間と労力で解決する", "hours": LABOR_TIME_COST_RANGE,
     "ranges": {"energy": (-40, -20), "peace": (-20, -10)}},
    # 2026-08-14訂正(ユーザー提案「相手からの信用に一本化したい」): socialの
    # 対価からもtrustコストを外した。「頼ること自体の代償」はpeaceのみで表現し、
    # 実際の信用の増減は契約の履行/不履行という結果(相手ごとのnpc_trust)でのみ
    # 起きる、という「相手がいて初めて値を持つ」設計に合わせた。
    {"key": "social", "name": "人に頼る(借りを作る)", "hours": SOCIAL_TIME_COST_RANGE,
     "ranges": {"peace": (-20, -10)}, "creates_contract": True},
    # 2026-08-14訂正(6回目opusレビュー指摘・最優先2): policy_scoreは負のコスト
    # だけを合計して符号反転するため、コスト無しのrestは常にスコア0=最大値になり、
    # 数式的に必ず選ばれる支配戦略になっていた(実測: 通常行動の91%がrest、
    # NPCが80年で2人しか登場しない)。SITUATION_SYSTEMのプロンプトはrestを
    # 「この状況を無理に解決せず、いったん休む」と定義しているので、
    # 解決しなかったことの代償を持たせる必要がある。当初はtrustの小さなコスト
    # (-5,-2)を追加していたが、同日中にプレイヤー個人trustそのものを廃止した
    # ため(上記コメント参照)、moneyへ差し替えた(「休んでいる間もお金は
    # 出ていく」という現実的な解釈、かつSHADOW_PRICE["money"]=3.0でtrustの
    # ときと同程度の影のコストになるよう較正: (-8,-3)×3.0≈平均16.5 ≈
    # 旧(-5,-2)×4.5≈平均15.75)。
    {"key": "rest",   "name": "休息する", "hours": REST_TIME_COST_RANGE,
     "ranges": {"peace": (20, 40), "energy": (15, 35), "money": (-8, -3)}},
]

# 2026-08-15追加(Step 13、物々交換・自給制度)。barter/subsistenceは
# ACTION_ARCHETYPES本体(常時提示される4型)には含めない——上位制度の崩壊・
# 縮退時にengine.available_normal_archetypes()が条件付きで追加する「代替
# 経路」であり、無条件に選択肢へ出る既存4型とは扱いが異なるため(健全な
# 期間に支配戦略化しないための設計、engine.alternative_economy_triggered
# docstring参照)。ただしdraw_archetype_cost/draw_archetype_hoursはキーで
# 引くだけの汎用ルックアップなので、costの生成対象としては両方まとめて
# 公開する必要がある(_action_cost_dependencies()のaction_archetypesに使う)。
ACTION_ARCHETYPES_ALL = ACTION_ARCHETYPES + [BARTER_ARCHETYPE, SUBSISTENCE_ARCHETYPE]

# social型を選ぶと発生する契約(債務)のパラメータ。すべてコードが決める。
CONTRACT_DUE_RANGE = (2, 4)          # 何ターン後が期限か
CONTRACT_REPAY_MONEY = (-40, -20)    # 履行時にお金で返す場合の額(price_indexで連動)
                                      # 2026-08-14、10倍スケール化

# 2026-08-14追加(ユーザー提案「収入以上の契約は本来は結べず、信用がとても
# 高い場合のみ結べる」「信用が低い場合は収入以下の契約も断られるようになる」)。
# 契約額(repay_money)の絶対値に、収入基準の上限を課す。上限そのものは
# 相手の信用で伸び縮みする——中立(NPC_TRUST_INITIAL)で「期限までに得られる
# はずの収入」ちょうど、契約成立ゲートの下限(NPC_CONTRACT_TRUST_MIN)で0
# (実質お断り)、信用の上限(NPC_TRUST_CAP)でCONTRACT_CREDIT_MULTIPLIER_MAX倍、
# という3点を通る区分線形にする(npc_price_modifierのtrust_factorと同じ
# 「中立を基準にした線形」パターンの再利用。下限側だけ既存の契約成立ゲートの
# 値〈NPC_CONTRACT_TRUST_MIN〉に接続することで、「もう頼れないほど信用が
# 低い」の基準を2箇所に分けて持たない)。
# 実装上の割り切り: 「相手が断る」を選択肢そのものの非表示ではなく、
# repay_moneyが0まで切り詰められる(=貸してもらえる額が実質ゼロ)という形で
# 近似している——プロンプト構造・選択肢数を変えずに済むため。
CONTRACT_CREDIT_MULTIPLIER_MAX = 3.0  # 仮値。trust=NPC_TRUST_CAPのとき収入の何倍まで借りられるか


def contract_credit_multiplier(trust: float) -> float:
    """relationship_rules.pyの実装を再公開する互換ラッパー。"""
    return relationship_rules.contract_credit_multiplier(
        trust, dependencies=_relationship_rules_dependencies())


def contract_credit_limit(trust: float, turn: int, due_turn: int) -> float:
    """relationship_rules.pyの実装を再公開する互換ラッパー。"""
    return relationship_rules.contract_credit_limit(
        trust, turn, due_turn, dependencies=_relationship_rules_dependencies(),
        contract_credit_multiplier_fn=contract_credit_multiplier)

# 労力返済の係数(実質債務1ptあたり)。2026-08-13訂正(2回目のopusレビュー指摘、
# --policy-probeの実測で確認): 旧実装は債務額と無関係な固定レンジ
# (time:-4..-2, energy:-3..-2)だったため、影のコストがmoney型返済
# (常に「実質債務×SHADOW_PRICE["money"]」)より一貫して安く(2〜7 vs 13〜20)、
# 決定論的にlabor一択になっていた(money型返済のシンクが一度も発火しない)。
# money型と同程度の影のコストになるよう再較正した:
# K_energy×SHADOW_PRICE["energy"] ≈ SHADOW_PRICE["money"] となるようKを選んだ
# (2.0×1.5=3.0)。
# 2026-08-14訂正(6回目opusレビュー指摘・最優先1): 時間予算制の統合でtime資源
# そのものを廃止したのに、この係数表にだけ"time"キーが取り残されていた。
# is_affordableがresources.get("time",0)を常に0未満と判定するため、清算の
# labor返済が構造的に選択不能になっており(実測241回中0回)、これがデフォルト率
# 約50%・通貨危機41回・trust発散(-900台)の真因だった(旧K_time=1.333分の
# 影のコストが丸ごと消えていたため、旧K_energy=1.111だけではmoney返済の56%
# しかなくlaborが不当に安かった、という二重の問題)。
# K_energy=2.0への再較正で、SHADOW_PRICE["trust"]=4.5(5回目opusレビューで
# 導出、labor返済コスト込みの値)との整合も保たれる。
CONTRACT_REPAY_LABOR_COEF = {"energy": 2.0}
CONTRACT_REPAY_LABOR_SPREAD = 0.25  # 係数のばらつき幅(±25%。旧レンジのランダム性を残す)


def _contract_cost_dependencies() -> contract_costs.ContractCostDependencies:
    """現在のgame.py設定から契約清算コストルールの動的依存を組み立てる。
    CONTRACT_DEFAULT_PENALTY_RATEはこの関数より後方で定義されるが、関数本体
    内の参照は呼び出し時に解決されるので問題ない(importの実行順序には
    影響しない)。"""
    return contract_costs.ContractCostDependencies(
        contract_repay_labor_coef=CONTRACT_REPAY_LABOR_COEF,
        contract_repay_labor_spread=CONTRACT_REPAY_LABOR_SPREAD,
        contract_default_penalty_rate=CONTRACT_DEFAULT_PENALTY_RATE,
        uniform_fn=random.uniform,
        price_index_fn=price_index,
    )


def draw_contract_repay_labor(repay_money: int, turn: int) -> dict:
    """contract_costs.pyの実装を再公開する互換ラッパー。"""
    return contract_costs.draw_contract_repay_labor(
        repay_money, turn, dependencies=_contract_cost_dependencies())

# 不履行のペナルティ(信用の失墜)。2026-08-13訂正(opusレビュー+ユーザー指摘):
# 固定値(旧: trust-3)のままだと、返済額(price_indexで連動して名目上大きくなる)が
# 育つほど踏み倒しが相対的に割安になってしまう。返済額に比例させることで、
# 大きな債務ほど踏み倒しにくくする(現状の典型額-4〜-2・平均-3のときに旧来の
# 固定値とほぼ一致するよう RATE=1.0 を選んだ)。
#
# 2026-08-13、2回目のopusレビューでさらに訂正: 上記の比例化は名目のrepay_moneyを
# そのまま使っており、SHADOW_PRICEで直したのと同じ名目/実質の取り違えが残っていた
# (実質債務は常に一定なのに、ペナルティだけがprice_indexの伸びに合わせて
# 一方的に膨らんでいた)。実質値(price_indexで割った額)に対して比例させる。
#
# 2026-08-13、さらに訂正(ユーザー指摘「踏み倒しがなぜそんなに発生するのか」への
# 調査で発覚): RATE=1.0は旧・固定ペナルティ(-3)を基準にしただけで、labor返済の
# コストを債務比例に作り直した(前回の修正)際に踏み倒し側を見直していなかった。
# 実測すると、慎重・野心的方針では踏み倒しの影のコストがmoney型・labor型の
# どちらより安く、**踏み倒しが構造的に最安の選択肢**になっていた(実測デフォルト率
# 慎重14.4%・野心的12.4%、家族思い6.4%)。RATE=2.0に引き上げて、少なくとも
# labor型返済(最も安い正規の経路)より踏み倒しの方が高くつくようにする。
# CONTRACT_DEFAULT_PENALTY_RATE・contract_default_penalty()は2026-08-14、
# プレイヤー個人trustの削除に伴い一度削除したが、実測で「不履行(avoid)」の
# costが空dict{}になったことで、policy_scoreの影のコスト計算上は不履行が
# 完全に無料になり(is_affordableも常にTrueを返す)、**デフォルト率が
# 全ケース100%まで悪化し、特性がT960で軒並み0近くまで崩壊する**という
# 重大な回帰が起きた。相手ごとのtrust(npc_trust/bank_trust)は今までどおり
# 毀損するが、それはresources/ALLOWED_RESOURCESの外側の別帳簿であるため、
# policy_score・auto_selectのworst_after判定からは見えない——踏み倒しに対する
# 定量的な抑止力がpolicy_scoreの視点から消えていたのが原因。
# 通貨(trust)をpeaceに差し替えて復元する(「踏み倒すと心が痛む」という
# 自然な解釈。social/restのコストでも既にpeaceを「関係にまつわる心理的な
# 対価」として使っているのと同じ発想)。RATEは旧trust版と同じ2.0を踏襲。
CONTRACT_DEFAULT_PENALTY_RATE = 2.0


def contract_default_penalty(repay_money: int, turn: int) -> dict:
    """contract_costs.pyの実装を再公開する互換ラッパー。"""
    return contract_costs.contract_default_penalty(
        repay_money, turn, dependencies=_contract_cost_dependencies())

# --- 方針ベクトル(代打ちAIの権限境界、docs/plan.md「実装への具体化」) ----------
# plan.md の決定: 不在プレイヤーを代打ちする際、方針宣言をテキストのままLLMに読ませて
# 判断させるのではなく、**各資源軸への重みベクトル**として持ち、コスト(ベクトル)との
# 内積で選ぶ。決定論的・LLM呼び出し不要。
#
# 重みの意味(ここを間違えると解釈が反転するので明記する):
#   w[r] = 「資源 r を得ることにどれだけ価値を置くか」= 「r を失うのをどれだけ嫌うか」。
#   したがって **w[money] が大きい方針ほど、money を払う選択肢を避ける**(お金を
#   減らしたくないので、時間・体力・信用の方を差し出す)。「野心的=money型を選ぶ」
#   ではないことに注意(phase1_policy_exp/README.md「実測」参照)。
#
# risk_aversion(λ)は旧方式(policy_score_legacy、L1正規化)専用のパラメータ。
# 現行の時間換算方式(policy_score)では使わない(下記参照)。
RISK_REFERENCE_LOSS = 6.0  # 総損失をこの値で割って無次元化する(旧方式用)

POLICY_VECTORS = {
    "cautious": {
        "label": "慎重",
        "weights": {"energy": 1.0, "peace": 1.0, "money": 1.1},
        "risk_aversion": 2.5,
    },
    "ambitious": {
        "label": "野心的",
        "weights": {"energy": 0.3, "peace": 0.1, "money": 1.6},
        "risk_aversion": -0.4,
    },
    "family": {
        "label": "家族思い",
        "weights": {"energy": 0.5, "peace": 1.0, "money": 0.5},
        "risk_aversion": 0.5,
    },
}
# 2026-08-14、weightsから"time"(time資源の廃止)・"trust"(プレイヤー個人trustの
# 廃止)を削除。policy_scoreはweights.get(k,1.0)で参照するので、キーが無い資源は
# 既定1.0になる(既存の挙動どおり、実害は無かった)。

# 2段階選択の1段目(生存制約)のしきい値。選択後の最小資源がこの値以上なら「安全」。
# 安全な選択肢が1つも無い場合は、最も傷の浅い(worst_after が最大の)選択肢群に絞る
# = 従来の balanced と同じ挙動になり、方針ベクトルはその中の同点崩しに退く。
SAFETY_FLOOR = 30  # 2026-08-14、10倍スケール化

# --- 状況の繰り返し対策 --------------------------------------------------------
# Phase 0 で「直近の状況をプロンプトに載せて『重複するな』と指示する」方式は効かなかった。
# 代わりにコード側の題材リストからランダムに強制注入する(場×関心事の直積)。
# 2026-08-12追加: 15ターン程度で関心事が一巡しかけ、near-duplicateが再出現していた
# (run4「壊れた設備の修理」T2/T11/T14)。プールを16→24に拡張して枯渇を遅らせる。
THEME_PLACES = [
    "職場", "実家", "近所の商店街", "病院の待合室", "駅前のカフェ", "アパートの部屋",
    "市役所の窓口", "友人の家", "夜道", "図書館", "銭湯", "小さな工場",
    "地元の祭りの準備", "引っ越し先の新しい町", "学生時代の同窓会", "農地",
    "通勤電車の中", "近所の公園", "スーパーのレジ前", "美容院", "取引先のオフィス",
    "子どもの通う学校", "山あいの温泉宿", "オンライン会議",
]
# --- 題材 → 特性の対応表(2026-08-13) ----------------------------------------
# docs/plan.md「特性の成長経路」の「THEME_CONCERNS の各項目を4つの特性(器用さ・
# 思考力・スキル・健康)のどれか1つに紐づける表をコード側に持つ。LLMには関与させない」
# をそのまま実装したもの。24項目を4特性に均等(6項目ずつ)に割り振ってある。
THEME_CONCERN_TRAITS = {
    # 器用さ(手を動かして直す・整える・段取りする)
    "壊れた設備の修理": "dexterity",
    "住まいの問題": "dexterity",
    "紛失物": "dexterity",
    "誰かからの頼まれごと": "dexterity",
    "手続きの不備": "dexterity",
    "予定の重複": "dexterity",
    # 思考力(考えて筋道を立てる・見極める・計算する)
    "急な出費": "intellect",
    "予算の見直し": "intellect",
    "資格や試験の準備": "intellect",
    "見知らぬ人からの申し出": "intellect",
    "仕事の評価": "intellect",
    "誤解によるすれ違い": "intellect",
    # スキル(対人・社会でのふるまい)
    "人間関係のこじれ": "skill",
    "親族の相談ごと": "skill",
    "近所とのいざこざ": "skill",
    "古い約束の再燃": "skill",
    "古い友人からの連絡": "skill",
    "新しい役割への戸惑い": "skill",
    # 健康(体調・休養・老い)
    "健康の不安": "health",
    "季節の変わり目の体調": "health",
    "体力の衰えへの不安": "health",
    "騒音トラブル": "health",       # 睡眠が削られる
    "天候による予定変更": "health",  # 無理をすると体に来る
    "締め切り": "health",           # 徹夜・過労
}
THEME_CONCERNS = list(THEME_CONCERN_TRAITS.keys())

SITUATION_SYSTEM = llm_integration.SITUATION_SYSTEM
SETTLEMENT_SYSTEM = llm_integration.SETTLEMENT_SYSTEM
NARRATION_SYSTEM = llm_integration.NARRATION_SYSTEM

RESOURCE_JA = {"energy": "体力", "peace": "心の余裕", "money": "お金"}
# 2026-08-14、"trust"を削除(プレイヤー個人trustの廃止。相手ごとのtrustへの
# 一本化。上記INITIAL_RESOURCESのコメント参照)。

# 2026-08-15(Step 11、段階的モジュール分割): 「LLM接続・プロンプト方針・
# ナレーション整合性検証」責務全体の実装本体はllm_integration.pyへ移動した
# (今回も関数1個ずつではなく責務全体の粒度)。ここに残すのは、既存の定数名の
# 再公開と、既存関数の薄い互換ラッパーだけ。
#
# _llm_integration_dependencies()・_llm_integration_config()は呼び出しの
# たびに新しいDependencies/Configを組み立てる(import時点で固定しない)。
# 理由は他の_xxx_dependencies()と同じ: 既存テストによるgame.py側関数
# (urllib.request.urlopen/Request・time.monotonic・sys.exit・sys.stderr・
# game.code_check_narration・game.run_consistency_check等)のmonkeypatchに
# 呼び出し時点で追随するため。
def _llm_integration_dependencies() -> llm_integration.LlmIntegrationDependencies:
    return llm_integration.LlmIntegrationDependencies(
        urlopen_fn=urllib.request.urlopen,
        request_cls=urllib.request.Request,
        monotonic_fn=time.monotonic,
        exit_fn=sys.exit,
        stderr=sys.stderr,
        code_check_narration_fn=code_check_narration,
        run_consistency_check_fn=run_consistency_check,
    )


def _llm_integration_config() -> llm_integration.LlmIntegrationConfig:
    return llm_integration.LlmIntegrationConfig(
        ollama_host=OLLAMA_HOST,
        keep_alive=KEEP_ALIVE,
        resource_synonyms=RESOURCE_SYNONYMS,
        positive_patterns=POSITIVE_PATTERNS,
        negation_markers=NEGATION_MARKERS,
        contradiction_window=CONTRADICTION_WINDOW,
        negation_lookahead=NEGATION_LOOKAHEAD,
        negative_control_cases=NEGATIVE_CONTROL_CASES,
        ja_to_key=JA_TO_KEY,
        allowed_resources=ALLOWED_RESOURCES,
    )


RESOURCE_SYNONYMS = llm_integration.RESOURCE_SYNONYMS
POSITIVE_PATTERNS = llm_integration.POSITIVE_PATTERNS
CONTRADICTION_WINDOW = llm_integration.CONTRADICTION_WINDOW
NEGATION_MARKERS = llm_integration.NEGATION_MARKERS
NEGATION_LOOKAHEAD = llm_integration.NEGATION_LOOKAHEAD


def code_check_narration(not_up_keys: list, narration: str) -> list:
    """llm_integration.pyの実装を再公開する互換ラッパー。"""
    return llm_integration.code_check_narration(
        not_up_keys, narration, config=_llm_integration_config())


CONSISTENCY_SYSTEM = llm_integration.CONSISTENCY_SYSTEM


# ==============================================================================
# ollama / イベントログ
# ==============================================================================

def call_ollama(model: str, system: str, user: str, want_json: bool) -> tuple:
    """llm_integration.pyの実装を再公開する互換ラッパー。"""
    return llm_integration.call_ollama(
        model, system, user, want_json,
        dependencies=_llm_integration_dependencies(),
        config=_llm_integration_config())



def append_event(event_type: str, data: dict) -> None:
    """2026-08-14(Step 2A、段階的モジュール分割)。実体はevent_store.pyへ
    移動した。EVENTS_PATHはmain()実行中に書き換えられる(--eventsオプション、
    テストでのEVENTS_PATH差し替え)ため、呼び出しのたびに現在のEVENTS_PATH
    グローバルを読んでevent_storeへ渡す薄いラッパーとして残す。"""
    return append_jsonl_event(EVENTS_PATH, event_type, data)


def iter_events():
    """2026-08-14(Step 2A)。実体はevent_store.pyへ移動。理由はappend_event
    と同じ(EVENTS_PATHは呼び出し時点の値を見る必要がある)。"""
    return iter_jsonl_events(EVENTS_PATH)


# ==============================================================================
# 状態の再構築(イベントソーシング)
# ==============================================================================

# 2026-08-14(Step 2B、段階的モジュール分割)。reduce_events()が参照する
# game.py側の動的な依存を1つにまとめたインスタンス。price_index_fnには
# 関数オブジェクトそのもの(game.price_index)を渡すので、main()で
# CREATION_RATEがCLI上書きされた場合も、呼び出し時点のgame.CREATION_RATEを
# 見る既存挙動をそのまま維持する(price_index関数の中身がグローバルの
# CREATION_RATEを参照しているため、モジュール側の値が変わればこの関数呼び出しの
# 結果も自動的に追随する)。effective_npc_trust_fnも同様に関数オブジェクトを渡す
# (NPC_TRUST_INITIAL・NPC_TRUST_REVERSION_RATEは関数内部で参照されるので、
# ここで個別に渡す必要はない)。
PROJECTION_DEPENDENCIES = ProjectionDependencies(
    initial_resources=INITIAL_RESOURCES,
    initial_traits=INITIAL_TRAITS,
    allowed_resources=ALLOWED_RESOURCES,
    npc_trust_initial=NPC_TRUST_INITIAL,
    bank_npc_id=BANK_NPC_ID,
    price_index_fn=price_index,
    effective_npc_trust_fn=effective_npc_trust,
    trait_min=TRAIT_MIN,
    trait_max=TRAIT_MAX,
)


def reduce_state() -> dict:
    """events.jsonl を畳み込んで現在の世界状態を再構築する。

    2026-08-14(Step 2B、段階的モジュール分割)。実体はprojection.pyの
    reduce_events()へ移動した。iter_events()はEVENTS_PATHを呼び出し時点の
    値で読む必要がある(--eventsオプション、テストでのEVENTS_PATH差し替え)ため、
    このラッパー自体は毎回iter_events()を呼び直す薄い形のまま残す。
    """
    return reduce_events(iter_events(), PROJECTION_DEPENDENCIES)


def format_traits(traits: dict, talent=None) -> str:
    return " / ".join(
        f"{TRAIT_JA[t]}:{traits[t]:.1f}" + ("*" if t == talent else "")
        for t in TRAITS
    )


def open_contracts(state: dict) -> list:
    return [c for c in state["contracts"].values() if c["status"] == "open"]


def due_contracts(state: dict, turn: int) -> list:
    return sorted(
        [c for c in open_contracts(state) if c["due_turn"] <= turn],
        key=lambda c: (c["due_turn"], c["id"]),
    )


def format_resources(resources: dict) -> str:
    return " / ".join(f"{k}:{v}" for k, v in resources.items())


def format_contracts(state: dict) -> str:
    opens = open_contracts(state)
    if not opens:
        return "(なし)"
    return " | ".join(
        f"{c['id']}:{c['counterparty']}に「{c['description']}」(期限T{c['due_turn']})"
        for c in opens
    )


# ==============================================================================
# コスト・減衰
# ==============================================================================

def _action_cost_dependencies() -> action_costs.ActionCostDependencies:
    """現在のgame.py設定から行動コスト生成ルールの動的依存を組み立てる。"""
    return action_costs.ActionCostDependencies(
        allowed_resources=ALLOWED_RESOURCES,
        max_delta=MAX_DELTA,
        # 2026-08-15追加(Step 13): barter/subsistenceのcost/hoursも引けるよう
        # ACTION_ARCHETYPES_ALL(4型+barter+subsistence)を渡す——「どのarchetype
        # が選択肢として提示されるか」(available_normal_archetypes、条件付き)と
        # 「archetypeのcost式を引けるか」(ここ、常に全種類を引ける)は別の関心事
        # (moneyのcost式が通貨放棄後も定義自体は存在し続けるのと同じ扱い)。
        action_archetypes=ACTION_ARCHETYPES_ALL,
        rest_time_cost_range=REST_TIME_COST_RANGE,
        randint_fn=random.randint,
        price_index_fn=price_index,
    )


def sanitize_cost(raw_cost: dict) -> tuple:
    """action_costs.pyの実装を再公開する互換ラッパー。"""
    return action_costs.sanitize_cost(
        raw_cost, dependencies=_action_cost_dependencies())


def draw_ranges(ranges: dict) -> dict:
    """action_costs.pyの実装を再公開する互換ラッパー。"""
    return action_costs.draw_ranges(
        ranges, dependencies=_action_cost_dependencies())


def indexed_money_ranges(ranges: dict, turn: int) -> dict:
    """action_costs.pyの実装を再公開する互換ラッパー。"""
    return action_costs.indexed_money_ranges(
        ranges, turn, dependencies=_action_cost_dependencies())


def draw_archetype_cost(key: str, turn: int) -> dict:
    """action_costs.pyの実装を再公開する互換ラッパー。"""
    return action_costs.draw_archetype_cost(
        key, turn, dependencies=_action_cost_dependencies(),
        draw_ranges_fn=draw_ranges,
        indexed_money_ranges_fn=indexed_money_ranges)


def draw_archetype_hours(key: str) -> int:
    """action_costs.pyの実装を再公開する互換ラッパー。"""
    return action_costs.draw_archetype_hours(
        key, dependencies=_action_cost_dependencies())


def draw_prorated_rest(turn: int, hours: float) -> dict:
    """action_costs.pyの実装を再公開する互換ラッパー。"""
    return action_costs.draw_prorated_rest(
        turn, hours, dependencies=_action_cost_dependencies(),
        draw_archetype_cost_fn=draw_archetype_cost)


def _resource_rules_dependencies() -> resource_rules.ResourceRulesDependencies:
    """現在のgame.py設定から資源ルールの動的依存を組み立てる。"""
    return resource_rules.ResourceRulesDependencies(
        decay_rules=DECAY_RULES,
        regen_rules=REGEN_RULES,
        initial_resources=INITIAL_RESOURCES,
        initial_traits=INITIAL_TRAITS,
        flow_resources=FLOW_RESOURCES,
        afford_floor=AFFORD_FLOOR,
    )


def compute_decay(turn: int, resources: dict) -> dict:
    """resource_rules.pyの実装を再公開する互換ラッパー。"""
    return resource_rules.compute_decay(
        turn, resources, dependencies=_resource_rules_dependencies())


def resource_cap(res: str, traits: dict) -> float:
    """resource_rules.pyの実装を再公開する互換ラッパー。"""
    return resource_rules.resource_cap(
        res, traits, dependencies=_resource_rules_dependencies())


def compute_regen(resources: dict, traits: dict) -> dict:
    """resource_rules.pyの実装を再公開する互換ラッパー。"""
    return resource_rules.compute_regen(
        resources, traits,
        dependencies=_resource_rules_dependencies(),
        resource_cap_fn=resource_cap)


def compute_income(turn: int, bank_stage: int = BANK_STAGE_HEALTHY) -> dict:
    """economic_rules.pyの実装を再公開する互換ラッパー。"""
    return economic_rules.compute_income(
        turn, bank_stage,
        dependencies=_economic_rules_dependencies(),
        compute_wage_fn=compute_wage)


def scale_toward_bound(v: float, current: float, cap: float) -> float:
    """resource_rules.pyの実装を再公開する互換ラッパー。"""
    return resource_rules.scale_toward_bound(v, current, cap)


def clamp_gain(delta: dict, resources: dict, traits: dict) -> dict:
    """resource_rules.pyの実装を再公開する互換ラッパー。"""
    return resource_rules.clamp_gain(
        delta, resources, traits,
        dependencies=_resource_rules_dependencies(),
        resource_cap_fn=resource_cap,
        scale_toward_bound_fn=scale_toward_bound)


def is_affordable(choice: dict, resources: dict, budget: float = None) -> bool:
    """resource_rules.pyの実装を再公開する互換ラッパー。"""
    return resource_rules.is_affordable(
        choice, resources, budget,
        dependencies=_resource_rules_dependencies(),
        resource_cap_fn=resource_cap,
        scale_toward_bound_fn=scale_toward_bound)


# ==============================================================================
# プロンプト構築
# ==============================================================================

def recent_situations(n: int = 3) -> list:
    texts = []
    for event in iter_events():
        if event["type"] == "situation_presented":
            texts.append(event["data"]["situation"])
    return texts[-n:]


def pick_theme(used_places: list, used_concerns: list, avoid_last: int = 5) -> tuple:
    """題材を引く。直近で使った場所・関心事は候補から外す。
    戻り値は (題材文字列, 関心事)。関心事は THEME_CONCERN_TRAITS で特性に変換する。

    run3 で「夜道 × 近所とのいざこざ」(T6)と「夜道 × 誰かからの頼まれごと」(T9)が
    引かれ、**場所が同じだけで状況描写がほぼ逐語的に一致した**(類似度0.95)。
    題材を注入しても、場所が被れば繰り返しが復活する。ランダムに引き直すのではなく
    直近を除外する方式にした。"""
    def choose(pool: list, used: list) -> str:
        fresh = [x for x in pool if x not in used[-avoid_last:]]
        return random.choice(fresh or pool)
    place = choose(THEME_PLACES, used_places)
    concern = choose(THEME_CONCERNS, used_concerns)
    used_places.append(place)
    used_concerns.append(concern)
    return f"{place} × {concern}", concern


# 2026-08-15(Step 7、段階的モジュール分割): 「LLMによる状況生成」責務全体
# (build_situation_prompt・build_settlement_prompt・clean_label・
# parse_json_response・generate_normal_turn・generate_settlement_turn・
# run_consistency_checkの7関数)の実装本体はscenario_generation.pyへ移動した
# (今回から作業粒度を関数1個ずつではなく責務単位に変更)。ここに残すのは、
# 既存の公開シグネチャを保つ薄い互換ラッパーだけ。
#
# _scenario_generation_dependencies()は呼び出しのたびに新しいDependenciesを
# 組み立てる(import時点で固定しない)。理由は他の_xxx_dependencies()と同じ:
# (1) CLIで上書きされうる値(price_index経由のCREATION_RATE等、間接的に
#     compute_social_contract_repay_range_fn等を経由)に追随するため、
# (2) 既存テストがgame.call_ollama・game.generate_normal_turn等を一時的に
#     差し替えて確認しているため(importで関数オブジェクトを固定すると、この
#     monkeypatchが効かなくなる)。
# 2026-08-15(Step 8): randint_fn/uniform_fnにrandom.randint/random.uniformを
# 渡す(_contract_cost_dependencies()のuniform_fnと同じ形。random.randint/
# random.uniform自体をmonkeypatchする既存テストの慣例にもそのまま追随する)。
def _scenario_generation_dependencies() -> scenario_generation.ScenarioGenerationDependencies:
    return scenario_generation.ScenarioGenerationDependencies(
        call_ollama_fn=call_ollama,
        append_event_fn=append_event,
        open_contracts_fn=open_contracts,
        recent_situations_fn=recent_situations,
        acquaintance_npcs_fn=acquaintance_npcs,
        compute_normal_money_modifier_fn=compute_normal_money_modifier,
        available_normal_archetypes_fn=available_normal_archetypes,
        build_normal_base_choice_fn=build_normal_base_choice,
        compute_social_contract_repay_range_fn=compute_social_contract_repay_range,
        pick_social_counterparty_fn=pick_social_counterparty,
        build_settlement_choices_fn=build_settlement_choices,
        randint_fn=random.randint,
        uniform_fn=random.uniform,
        bank_npc_name=BANK_NPC_NAME,
        situation_system=SITUATION_SYSTEM,
        settlement_system=SETTLEMENT_SYSTEM,
        consistency_system=CONSISTENCY_SYSTEM,
        currency_stage_normal=CURRENCY_STAGE_NORMAL,
        local_credit_stage_healthy=LOCAL_CREDIT_STAGE_HEALTHY,
        enforcement_stage_institutional=ENFORCEMENT_STAGE_INSTITUTIONAL,
        bank_stage_healthy=BANK_STAGE_HEALTHY,
        barter_stage_functioning=BARTER_STAGE_FUNCTIONING,
        contract_due_range=CONTRACT_DUE_RANGE,
        npc_ethics_range=NPC_ETHICS_RANGE,
        npc_relationship_span_range=NPC_RELATIONSHIP_SPAN_RANGE,
    )


def build_situation_prompt(state: dict, turn: int, theme: str,
                           counterparty_hint: tuple = None) -> str:
    """scenario_generation.pyの実装を再公開する互換ラッパー。"""
    return scenario_generation.build_situation_prompt(
        state, turn, theme, counterparty_hint,
        dependencies=_scenario_generation_dependencies())


def build_settlement_prompt(state: dict, turn: int, contract: dict) -> str:
    """scenario_generation.pyの実装を再公開する互換ラッパー。"""
    return scenario_generation.build_settlement_prompt(state, turn, contract)


# ==============================================================================
# ターン処理
# ==============================================================================

def clean_label(text: str, fallback: str) -> str:
    """scenario_generation.pyの実装を再公開する互換ラッパー。"""
    return scenario_generation.clean_label(text, fallback)


def parse_json_response(raw: str) -> dict:
    """scenario_generation.pyの実装を再公開する互換ラッパー。"""
    return scenario_generation.parse_json_response(raw)


def acquaintance_npcs(state: dict) -> dict:
    """銀行・地域経済(インフラ役のNPC)を除いた、社会型契約の相手になりうる
    永続NPCの一覧(2026-08-13追加、docs/plan.md「NPC永続化の拡張」。
    2026-08-14、ECONOMY_NPC_ID追加に伴いrole基準に変更——インフラ役の
    NPCが増えるたびにこの関数を書き換えずに済むようにする)。"""
    return {
        nid: n for nid, n in state["npcs"].items()
        if n.get("role") == "acquaintance" and n.get("alive", True)
    }


# 2026-08-14(Step 4B、段階的モジュール分割): 通常行動の選択肢生成4関数
# (Step 3Bでgenerate_normal_turn()とsimulate_policy()の重複を解消済み)の
# 実装本体はengine.pyへ移動した。ここに残すのは、既存の公開シグネチャを
# 保つ薄い互換ラッパーだけ(Step 4Aのbuild_settlement_choices等と同じ形)。
#
# _normal_action_engine_dependencies()は呼び出しのたびに新しいNormalAction
# EngineDependenciesを組み立てる(import時点で固定しない)。理由はStep 4Aと
# 同じ: (1) CREATION_RATEのCLI上書きに追随するため、(2) 既存テストが
# game.draw_ranges等を一時的に差し替えて呼び出し回数を確認しているため
# (importで関数オブジェクトを固定すると、このmonkeypatchが効かなくなる)。
def _normal_action_engine_dependencies() -> engine.NormalActionEngineDependencies:
    return engine.NormalActionEngineDependencies(
        action_archetypes=ACTION_ARCHETYPES,
        npc_price_modifier_fn=npc_price_modifier,
        draw_ranges_fn=draw_ranges,
        indexed_money_ranges_fn=indexed_money_ranges,
        draw_archetype_cost_fn=draw_archetype_cost,
        draw_archetype_hours_fn=draw_archetype_hours,
        initial_trust_for_new_npc_fn=initial_trust_for_new_npc,
        contract_repay_money=CONTRACT_REPAY_MONEY,
        contract_credit_limit_fn=contract_credit_limit,
        barter_choice_evaluation_fn=barter_choice_evaluation,
        clamp_gain_fn=clamp_gain,
    )


def available_normal_archetypes(currency_stage: int,
                                local_credit_stage: int = LOCAL_CREDIT_STAGE_HEALTHY,
                                bank_stage: int = BANK_STAGE_HEALTHY,
                                enforcement_stage: int = ENFORCEMENT_STAGE_INSTITUTIONAL,
                                barter_stage: int = BARTER_STAGE_FUNCTIONING) -> list:
    return engine.available_normal_archetypes(
        currency_stage, local_credit_stage, bank_stage, enforcement_stage, barter_stage,
        dependencies=_normal_action_engine_dependencies())


def alternative_economy_triggered(bank_stage: int = BANK_STAGE_HEALTHY,
                                  currency_stage: int = CURRENCY_STAGE_NORMAL,
                                  local_credit_stage: int = LOCAL_CREDIT_STAGE_HEALTHY,
                                  enforcement_stage: int = ENFORCEMENT_STAGE_INSTITUTIONAL) -> bool:
    """engine.pyの実装を再公開する互換ラッパー。"""
    return engine.alternative_economy_triggered(
        bank_stage, currency_stage, local_credit_stage, enforcement_stage)


def compute_normal_money_modifier(acquaintances: dict, currency_stage: int,
                                  rng=random) -> float:
    return engine.compute_normal_money_modifier(
        acquaintances, currency_stage, rng,
        dependencies=_normal_action_engine_dependencies())


def build_normal_base_choice(archetype: dict, turn: int, money_modifier: float,
                             label: str = None, goods_state: dict = None) -> dict:
    return engine.build_normal_base_choice(
        archetype, turn, money_modifier, label, goods_state,
        dependencies=_normal_action_engine_dependencies())


def compute_social_contract_repay_range(acquaintances: dict, existing, prospective_ethics,
                                        turn: int, due_turn: int) -> tuple:
    return engine.compute_social_contract_repay_range(
        acquaintances, existing, prospective_ethics, turn, due_turn,
        dependencies=_normal_action_engine_dependencies())


def plan_normal_action_resolution(choice: dict, resources: dict, traits: dict) -> dict:
    """engine.pyの実装を再公開する互換ラッパー。"""
    return engine.plan_normal_action_resolution(
        choice, resources, traits, dependencies=_normal_action_engine_dependencies())


def generate_normal_turn(model: str, state: dict, turn: int, theme: str, latencies: list) -> dict:
    """scenario_generation.pyの実装を再公開する互換ラッパー。"""
    return scenario_generation.generate_normal_turn(
        model, state, turn, theme, latencies,
        dependencies=_scenario_generation_dependencies())


# 2026-08-14(Step 4A、段階的モジュール分割): build_settlement_choices()・
# plan_settlement_effects()の実装本体はengine.pyへ移動した(Step 3A・3Cで
# main()とsimulate_policyの重複を解消済みの2関数を、依存方向を確定させる
# ためにここへ移す)。ここに残すのは、既存の公開シグネチャを保つ薄い互換
# ラッパーだけ。
#
# _settlement_engine_dependencies()は呼び出しのたびに新しいSettlementEngine
# Dependenciesを組み立てる(import時点で固定しない)。理由は2つ:
# (1) CREATION_RATEがCLIで上書きされた場合、price_index関数の中身が現在の
#     game.CREATION_RATEを見る既存挙動を維持する必要がある。
# (2) BuildSettlementChoicesTestがgame.draw_contract_repay_laborを一時的に
#     差し替えて呼び出し回数を確認している——importで関数オブジェクトを
#     固定すると、この既存テストのmonkeypatchが効かなくなる。
def _settlement_engine_dependencies() -> engine.SettlementEngineDependencies:
    return engine.SettlementEngineDependencies(
        draw_contract_repay_labor_fn=draw_contract_repay_labor,
        contract_default_penalty_fn=contract_default_penalty,
        clean_label_fn=clean_label,
        price_index_fn=price_index,
        npc_trust_gain_fn=npc_trust_gain,
        npc_trust_initial=NPC_TRUST_INITIAL,
        crisis_rebase_factor=CRISIS_REBASE_FACTOR,
    )


def build_settlement_choices(contract: dict, turn: int, currency_stage: int,
                             labels: dict = None, *,
                             enforcement_stage: int = ENFORCEMENT_STAGE_INSTITUTIONAL) -> list:
    return engine.build_settlement_choices(
        contract, turn, currency_stage, labels,
        enforcement_stage=enforcement_stage,
        dependencies=_settlement_engine_dependencies())


def plan_settlement_effects(contract: dict, choice_key: str, outcome: str, turn: int,
                            bank_trust: float, currency_confidence: float,
                            bank_credit_losses: float, counterparty_trust: float = None,
                            *, enforcement_stage: int = ENFORCEMENT_STAGE_INSTITUTIONAL,
                            enforcement_capacity: float = ENFORCEMENT_CAPACITY_INITIAL) -> dict:
    return engine.plan_settlement_effects(
        contract, choice_key, outcome, turn, bank_trust, currency_confidence,
        bank_credit_losses, counterparty_trust,
        dependencies=_settlement_engine_dependencies(),
        enforcement_stage=enforcement_stage, enforcement_capacity=enforcement_capacity)


def plan_settlement_resolution(contract: dict, choice: dict, turn: int,
                               bank_trust: float, currency_confidence: float,
                               bank_credit_losses: float, counterparty_trust: float = None,
                               *, enforcement_stage: int = ENFORCEMENT_STAGE_INSTITUTIONAL,
                               enforcement_capacity: float = ENFORCEMENT_CAPACITY_INITIAL) -> dict:
    """engine.pyの実装を再公開する互換ラッパー。"""
    return engine.plan_settlement_resolution(
        contract, choice, turn, bank_trust, currency_confidence,
        bank_credit_losses, counterparty_trust,
        dependencies=_settlement_engine_dependencies(),
        plan_settlement_effects_fn=plan_settlement_effects,
        enforcement_stage=enforcement_stage, enforcement_capacity=enforcement_capacity)


def build_settlement_effect_events(contract: dict, choice: dict, effects: dict, turn: int,
                                   bank_npc_id: str, bank_crisis_count: int) -> list:
    """event_builders.pyの実装を再公開する互換ラッパー。"""
    return event_builders.build_settlement_effect_events(
        contract, choice, effects, turn, bank_npc_id, bank_crisis_count)


def build_normal_contract_events(choice: dict, state_now: dict, turn: int,
                                 contract_id: str) -> list:
    """event_builders.pyの実装を再公開する互換ラッパー。initial_trust_for_new_npc・
    acquaintance_npcsは呼び出し時点のgame.py側の関数オブジェクトをそのまま渡す
    (import時点で固定しない——他の*_fn引数と同じ、monkeypatch伝播のため)。

    2026-08-15訂正(ユーザーレビュー指摘への対応): local_credit_stageによる
    ブレンド可否の判定は、以前ここに書かれていたが、
    relationship_rules.initial_trust_for_new_npc()自体へ移設した(上記
    initial_trust_for_new_npcラッパーのdocstring参照)。ここは
    state_now(無ければ通常/中立扱いにフォールバック)から読んだ値をそのまま
    渡すだけの配線になる——event_builders.py自体は無改造のまま
    (initial_trust_for_new_npc_fnは常に(acquaintances, turn)の2引数で
    呼ばれる)。"""
    local_credit_stage = state_now.get("local_credit_stage", LOCAL_CREDIT_STAGE_HEALTHY)
    community_trust = state_now.get("community_trust", LOCAL_CREDIT_TRUST_INITIAL)

    def trust_fn(acq, t):
        return initial_trust_for_new_npc(
            acq, t, local_credit_stage=local_credit_stage, community_trust=community_trust)

    return event_builders.build_normal_contract_events(
        choice, state_now, turn, contract_id,
        initial_trust_for_new_npc_fn=trust_fn,
        acquaintance_npcs_fn=acquaintance_npcs)


def generate_settlement_turn(model: str, state: dict, turn: int, contract: dict,
                             latencies: list) -> dict:
    """scenario_generation.pyの実装を再公開する互換ラッパー。"""
    return scenario_generation.generate_settlement_turn(
        model, state, turn, contract, latencies,
        dependencies=_scenario_generation_dependencies())


def run_consistency_check(model: str, down: list, up: list, narration: str,
                          latencies: list, log: bool = True) -> tuple:
    """scenario_generation.pyの実装を再公開する互換ラッパー。"""
    return scenario_generation.run_consistency_check(
        model, down, up, narration, latencies, log,
        dependencies=_scenario_generation_dependencies())


# 2026-08-15(Step 11、段階的モジュール分割): NEGATIVE_CONTROL_CASES・
# run_negative_control()の実装本体はllm_integration.pyへ移動した。
NEGATIVE_CONTROL_CASES = llm_integration.NEGATIVE_CONTROL_CASES
JA_TO_KEY = {v: k for k, v in RESOURCE_JA.items()}


def run_negative_control(model: str) -> None:
    """llm_integration.pyの実装を再公開する互換ラッパー。"""
    return llm_integration.run_negative_control(
        model, dependencies=_llm_integration_dependencies(),
        config=_llm_integration_config())


# --- 時間換算コスト(2026-08-13、opusレビュー+docs/plan.md「資源体系全体の統一」) --
# 「慎重方針が、回復するはずの体力を安全だと誤認し、回復しないお金を危険だと
# 正しく認識できなかった」問題への対応。原因は policy_score が近視眼的で、
# 「その場のコストの大きさ」と「実際に取り戻すのに何ターンかかるか(影のコスト)」
# を混同していたこと。統一原則により、全資源が「時間を投じて回復・成長させる
# ストック」になったので、各資源1点あたりの影のコスト(時間換算)を計算できる。
#
# p_i = 1点を取り戻すのに必要な時間換算コスト。既存の REGEN_RULES / INCOME_AMOUNT /
# CONTRACT_FULFILL_BONUS から逆算した仮値(実測ではなく机上の見積もり、要調整):
#   time  : 定義上 1.0(数値の物差しそのもの)
#   energy: 暗黙の休養(仮に2時間相当)で+2回復 → 1.0/pt だが、体力の方が
#           やや"重い"とみなし 1.5/pt に設定(要調整)
#   peace : 暗黙の休養(仮に3時間相当)で+3回復 → 1.0/pt
#   money : INCOME_AMOUNT=4 が4ターンごと=平均+1/ターン。暗黙の労働時間を
#           仮に3時間相当とすると 3.0/pt (お金は時間に対して"高い")
#   (旧trust: 4.5/pt。2026-08-14、プレイヤー個人trustの廃止に伴い削除
#    〈上記INITIAL_RESOURCESのコメント参照〉。social/restのコストからも
#    trustが外れたため、この定数がpolicy_scoreの計算に影響することはもう無い)
SHADOW_PRICE = {"energy": 1.5, "peace": 1.0, "money": 3.0}
# 2026-08-14、"time"キー(time資源の廃止)・"trust"キー(プレイヤー個人trustの
# 廃止)を削除。policy_scoreはSHADOW_PRICE.get(k,1.0)で参照するので実害は無かった。

# 2026-08-14追加(7回目opusレビュー指摘・重大4)。socialのコスト辞書
# (ACTION_ARCHETYPES)にはその場のpeace消耗しか無く、将来の返済義務
# (契約のrepay_money)が一切織り込まれていなかった。結果、他のどの
# archetypeより圧倒的に「安い」支配的戦略になっており(money/laborの
# 選択比率5%未満FAILの主因)、清算ターン渋滞の修正(#5)で通常ターンが
# より頻繁に回るようになるとむしろ悪化した(social比率58%→64〜68%、
# デフォルト率58%→82%)。SOCIAL_FUTURE_COST_WEIGHT(仮値1.0)で、契約の
# 名目repay_moneyを実質化してmoney軸の影のコストとして前計上する
# (将来の自分に先送りする負担を、今の意思決定にも反映させる)。
SOCIAL_FUTURE_COST_WEIGHT = 2.0  # 仮値。1.0→2.0でlabor選択比率が9.0%→9.7%まで
                                  # 改善(選択の多様性基準5%を初めて上回った)。
                                  # 2.0以上はサチる(実測でlabor/money/social比率が
                                  # 全く動かなくなる——is_affordableがmoney/laborを
                                  # 弾いて選べなくしている回が支配的で、policy_score
                                  # の選好では動かせない領域に入るため。この先の
                                  # 改善にはis_affordableを弾く根本原因〈収入と
                                  # 契約額のバランス〉側の調整が要る)


def _policy_dependencies() -> policy_engine.PolicyDependencies:
    """呼び出し時点のgame.py依存を方針層へ渡す。monkeypatchとCLI上書きを維持。"""
    return policy_engine.PolicyDependencies(
        risk_reference_loss=RISK_REFERENCE_LOSS,
        shadow_price=SHADOW_PRICE,
        social_future_cost_weight=SOCIAL_FUTURE_COST_WEIGHT,
        price_index_fn=price_index,
        scale_toward_bound_fn=scale_toward_bound,
        resource_cap_fn=resource_cap,
        is_affordable_fn=is_affordable,
        policy_vectors=POLICY_VECTORS,
        random_choice_fn=random.choice,
    )


def normalize_cost(cost: dict) -> dict:
    return policy_engine.normalize_cost(cost)


def policy_score_legacy(choice: dict, vec: dict) -> float:
    return policy_engine.policy_score_legacy(
        choice, vec, dependencies=_policy_dependencies())


def policy_score(choice: dict, vec: dict, turn: int = 0, resources: dict = None) -> float:
    """policy.pyの実装を既存公開シグネチャで再公開する互換ラッパー。"""
    return policy_engine.policy_score(
        choice, vec, turn, resources, dependencies=_policy_dependencies())


def auto_select(choices: list, resources: dict, policy: str,
                policy_name: str = None, safety_floor: int = SAFETY_FLOOR,
                turn: int = 0, budget: float = None) -> int:
    """policy.pyの実装を既存公開シグネチャで再公開する互換ラッパー。"""
    return policy_engine.auto_select(
        choices, resources, policy, policy_name, safety_floor, turn, budget,
        dependencies=_policy_dependencies())


# ==============================================================================
# 方針ベクトルのオフライン検証(--policy-probe)
# ==============================================================================
# ollama を1度も呼ばずに、経済(decay/regen/income/契約/清算)とコスト抽選だけを
# 回して、方針ごとの選択分布・破産の有無を測る。CLAUDE.md の「ollama は NAS 本番と
# 共用。長時間・大量に呼び続けない」を守りつつ、LLM の出力ゆらぎを排除した
# 決定論的な観測ができる(状況描写とラベルは選択に一切影響しないので、
# 選択ロジックの検証としてはこれで十分)。
#
# 2026-08-13、4回目opusレビュー指摘で全面改訂: それまでこの関数はhealth固定
# (energy上限を動かさない)・NPC永続化/価格形成なし、で特性も追跡していなかった。
# その出力(選択分布)をそのまま`--simulate`に入力して特性成長を検証していたため、
# 「2つの単純化を跨いで検証結果を使い回した時点で、どちらの単純化も暗黙に
# 『無かったこと』にされた仮想世界を検証していた」という事態を招いた
# (「解消した」という報告も、その後の自己訂正も両方とも誤りだった)。
# 検証装置をmain()の実際のゲームループと一本化し、traits・health・NPC永続化
# ・価格形成をすべて含めて回すよう拡張した(新しい仕組みを別に作るのではなく、
# main()と同じ関数=pick_theme/compute_trait_step/npc_price_modifier/
# resource_cap等をそのまま再利用する)。

# 反証可能な合否基準のためのトラジェクトリ観測点(2026-08-13追加、5回目opus
# レビュー指摘: 「T960の1点だけを見て均衡と判定する」誤りが2・3・5回目と
# 繰り返された。単一時点ではなく複数時点の推移を機械的に記録・判定できるように
# する。CHECKPOINT_TURNSはこの観測点の既定値。
CHECKPOINT_TURNS = [60, 200, 420, 960, 1440, 1920]


# 2026-08-15(Step 9、段階的モジュール分割): 「LLMを使わないオフライン検証・
# シミュレーション」責務全体の実装本体はoffline_simulation.pyへ移動した
# (今回も関数1個ずつではなく責務全体の粒度)。ここに残すのは、既存の公開
# シグネチャを保つ薄い互換ラッパーだけ。
#
# _policy_simulation_dependencies()・_trait_simulation_dependencies()は
# 呼び出しのたびに新しいDependenciesを組み立てる(import時点で固定しない)。
# 理由は他の_xxx_dependencies()と同じ: CLIで上書きされうる値(D_BASE等)・
# 既存テストによるgame.py側関数のmonkeypatchに呼び出し時点で追随するため。
def _policy_simulation_dependencies() -> offline_simulation.PolicySimulationDependencies:
    return offline_simulation.PolicySimulationDependencies(
        seed_fn=random.seed,
        choice_fn=random.choice,
        randint_fn=random.randint,
        uniform_fn=random.uniform,
        getstate_fn=random.getstate,
        setstate_fn=random.setstate,
        compute_regen_fn=compute_regen,
        plan_confidence_reversion_fn=plan_confidence_reversion,
        effective_npc_trust_fn=effective_npc_trust,
        plan_institution_transitions_fn=plan_institution_transitions,
        plan_local_credit_reversion_fn=plan_local_credit_reversion,
        plan_local_credit_transition_fn=plan_local_credit_transition,
        plan_enforcement_reversion_fn=plan_enforcement_reversion,
        plan_enforcement_transition_fn=plan_enforcement_transition,
        plan_barter_upkeep_fn=plan_barter_upkeep,
        plan_barter_transition_fn=plan_barter_transition,
        worst_shortfall_fn=worst_shortfall,
        essential_goods_shortage_penalty_fn=essential_goods_shortage_penalty,
        barter_choice_effects_fn=barter_choice_effects,
        alternative_economy_triggered_fn=alternative_economy_triggered,
        compute_income_fn=compute_income,
        compute_salary_fn=compute_salary,
        compute_decay_fn=compute_decay,
        compute_trait_step_fn=compute_trait_step,
        price_index_fn=price_index,
        draw_prorated_rest_fn=draw_prorated_rest,
        build_settlement_choices_fn=build_settlement_choices,
        plan_settlement_resolution_fn=plan_settlement_resolution,
        compute_normal_money_modifier_fn=compute_normal_money_modifier,
        available_normal_archetypes_fn=available_normal_archetypes,
        build_normal_base_choice_fn=build_normal_base_choice,
        pick_social_counterparty_fn=pick_social_counterparty,
        compute_social_contract_repay_range_fn=compute_social_contract_repay_range,
        plan_normal_action_resolution_fn=plan_normal_action_resolution,
        initial_trust_for_new_npc_fn=initial_trust_for_new_npc,
        is_affordable_fn=is_affordable,
        auto_select_fn=auto_select,
        policy_score_fn=policy_score,
        clamp_gain_fn=clamp_gain,
        pick_theme_fn=pick_theme,
        age_at_fn=age_at,
        format_resources_fn=format_resources,
        format_traits_fn=format_traits,
        exit_fn=sys.exit,
        initial_resources=INITIAL_RESOURCES,
        initial_traits=INITIAL_TRAITS,
        traits=TRAITS,
        npc_pool_cap_range=NPC_POOL_CAP_RANGE,
        bank_trust_initial=BANK_TRUST_INITIAL,
        bank_stage_healthy=BANK_STAGE_HEALTHY,
        currency_confidence_initial=CURRENCY_CONFIDENCE_INITIAL,
        currency_stage_normal=CURRENCY_STAGE_NORMAL,
        local_credit_trust_initial=LOCAL_CREDIT_TRUST_INITIAL,
        local_credit_stage_healthy=LOCAL_CREDIT_STAGE_HEALTHY,
        enforcement_capacity_initial=ENFORCEMENT_CAPACITY_INITIAL,
        enforcement_stage_institutional=ENFORCEMENT_STAGE_INSTITUTIONAL,
        food_stock_initial=FOOD_STOCK_INITIAL,
        medicine_stock_initial=MEDICINE_STOCK_INITIAL,
        shelter_durability_initial=SHELTER_DURABILITY_INITIAL,
        tools_durability_initial=TOOLS_DURABILITY_INITIAL,
        production_capacity_initial=PRODUCTION_CAPACITY_INITIAL,
        production_capacity_cap=PRODUCTION_CAPACITY_CAP,
        barter_stage_functioning=BARTER_STAGE_FUNCTIONING,
        goods_cap=GOODS_CAP,
        checkpoint_turns=CHECKPOINT_TURNS,
        contract_due_range=CONTRACT_DUE_RANGE,
        theme_concern_traits=THEME_CONCERN_TRAITS,
        policy_vectors=POLICY_VECTORS,
        g_base=G_BASE,
        trait_min=TRAIT_MIN,
        trait_max=TRAIT_MAX,
        turn_time_budget=TURN_TIME_BUDGET,
        min_activity_hours=MIN_ACTIVITY_HOURS,
        npc_ethics_range=NPC_ETHICS_RANGE,
        npc_relationship_span_range=NPC_RELATIONSHIP_SPAN_RANGE,
        growable_traits=GROWABLE_TRAITS,
        trait_ja=TRAIT_JA,
        # 2026-08-15追加(自己レビュー指摘への対応): run_policy_check/
        # run_policy_probe内部でのsimulate_policy/check_trajectory_criteria
        # 呼び出しをgame.py側の関数オブジェクト経由にし、game.simulate_policy等の
        # monkeypatchが伝播するようにする。
        simulate_policy_fn=simulate_policy,
        check_trajectory_criteria_fn=check_trajectory_criteria,
        check_enforcement_convergence_fn=check_enforcement_convergence,
    )


def simulate_policy(policy_name: str, turns: int, seed: int, safety_floor: int,
                    talent: str = None, checkpoint_turns: list = None, *,
                    trace: dict = None, continue_world: bool = False,
                    resume_state: dict = None,
                    initial_population: int = None) -> dict:
    """offline_simulation.pyの実装を再公開する互換ラッパー。"""
    return offline_simulation.simulate_policy(
        policy_name, turns, seed, safety_floor, talent, checkpoint_turns,
        dependencies=_policy_simulation_dependencies(), trace=trace,
        continue_world=continue_world, resume_state=resume_state,
        initial_population=initial_population)


def check_trajectory_criteria(r: dict) -> list:
    """offline_simulation.pyの実装を再公開する互換ラッパー。"""
    return offline_simulation.check_trajectory_criteria(
        r, dependencies=_policy_simulation_dependencies())


def check_enforcement_convergence(run_results: list) -> dict:
    """offline_simulation.pyの実装を再公開する互換ラッパー。
    2026-08-15追加(Step 12、`--policy-check`の制度レジーム対応再設計)。"""
    return offline_simulation.check_enforcement_convergence(run_results)


def run_policy_check(seeds: list, safety_floor: int) -> None:
    """offline_simulation.pyの実装を再公開する互換ラッパー。"""
    return offline_simulation.run_policy_check(
        seeds, safety_floor, dependencies=_policy_simulation_dependencies())


def run_policy_probe(turns: int, seeds: list, safety_floor: int) -> None:
    """offline_simulation.pyの実装を再公開する互換ラッパー。"""
    return offline_simulation.run_policy_probe(
        turns, seeds, safety_floor, dependencies=_policy_simulation_dependencies())


def collect_visualize_trace(seed: int, policy_name: str, turns: int, safety_floor: int,
                            talent: str = None, *, world_mode: bool = True,
                            resume_state: dict = None, trace: dict = None,
                            include_resume_state: bool = False,
                            initial_population: int = None) -> dict:
    """ファイルI/Oなしで可視化データを収集するローカルUI向けラッパー。"""
    return offline_simulation.collect_visualize_trace(
        seed, policy_name, turns, safety_floor, talent,
        world_mode=world_mode, resume_state=resume_state, trace=trace,
        include_resume_state=include_resume_state,
        initial_population=initial_population,
        dependencies=_policy_simulation_dependencies())


def run_visualize_trace(seed: int, policy_name: str, turns: int, safety_floor: int,
                        talent: str, output_path: str, *,
                        world_mode: bool = True,
                        initial_population: int = None) -> dict:
    """offline_simulation.pyの実装を再公開する互換ラッパー。"""
    return offline_simulation.run_visualize_trace(
        seed, policy_name, turns, safety_floor, talent, output_path,
        world_mode=world_mode, initial_population=initial_population,
        dependencies=_policy_simulation_dependencies())


# ==============================================================================
# 特性の長期シミュレーション(ollamaを呼ばない)
# ==============================================================================

def _trait_simulation_dependencies() -> offline_simulation.TraitSimulationDependencies:
    return offline_simulation.TraitSimulationDependencies(
        seed_fn=random.seed,
        random_fn=random.random,
        choices_fn=random.choices,
        compute_trait_step_fn=compute_trait_step,
        pick_theme_fn=pick_theme,
        age_at_fn=age_at,
        traits=TRAITS,
        initial_traits=INITIAL_TRAITS,
        trait_min=TRAIT_MIN,
        trait_max=TRAIT_MAX,
        theme_concern_traits=THEME_CONCERN_TRAITS,
        trait_ja=TRAIT_JA,
        d_base=D_BASE,
        g_base=G_BASE,
        b_talent=B_TALENT,
        # 2026-08-15追加(自己レビュー指摘への対応): run_simulation内部での
        # equilibrium/simulate_forced/simulate_mixed呼び出しをgame.py側の
        # 関数オブジェクト経由にし、game.equilibrium等のmonkeypatchが
        # 伝播するようにする。
        equilibrium_fn=equilibrium,
        simulate_forced_fn=simulate_forced,
        simulate_mixed_fn=simulate_mixed,
    )


def equilibrium(f: float, g_base: float, m_talent: float, d_base: float) -> float:
    """offline_simulation.pyの実装を再公開する互換ラッパー(dependencies不要、
    game.py固有の依存を一切持たない純粋関数のため)。"""
    return offline_simulation.equilibrium(f, g_base, m_talent, d_base)


def simulate_forced(f: float, g_base: float, m_talent: float, d_base: float,
                    turns: int, trials: int, start: float = 50.0) -> float:
    """offline_simulation.pyの実装を再公開する互換ラッパー。"""
    return offline_simulation.simulate_forced(
        f, g_base, m_talent, d_base, turns, trials, start,
        dependencies=_trait_simulation_dependencies())


def simulate_mixed(turns: int, trials: int, dist: list, no_theme_rate: float,
                   talent: str) -> dict:
    """offline_simulation.pyの実装を再公開する互換ラッパー。"""
    return offline_simulation.simulate_mixed(
        turns, trials, dist, no_theme_rate, talent,
        dependencies=_trait_simulation_dependencies())


def run_simulation(turns: int, trials: int, dist: list, no_theme_rate: float,
                   seed, f_override) -> None:
    """offline_simulation.pyの実装を再公開する互換ラッパー。"""
    return offline_simulation.run_simulation(
        turns, trials, dist, no_theme_rate, seed, f_override,
        dependencies=_trait_simulation_dependencies())



# ==============================================================================
# 繰り返し度の計測(Phase 0 は目視だったので、数値で出せるようにした)
# ==============================================================================
# 2026-08-15(Step 10、自己レビュー指摘への対応): 実装本体はgame_session.pyへ
# 移動した(重複実装を解消し、game.bigrams/game.repetition_reportの
# monkeypatchがrun_game_session()経由でも伝播するようにするため)。

def bigrams(text: str) -> set:
    """game_session.pyの実装を再公開する互換ラッパー。"""
    return game_session.bigrams(text)


def repetition_report(situations: list) -> dict:
    """game_session.pyの実装を再公開する互換ラッパー。bigrams_fnには
    呼び出し時点のgame.bigrams(monkeypatchされうる関数オブジェクト)を
    そのまま渡す。"""
    return game_session.repetition_report(situations, bigrams_fn=bigrams)


# ==============================================================================
# メイン
# ==============================================================================

# 2026-08-15(Step 8、段階的モジュール分割): main()内のネスト関数だった
# process_one_situationの実装本体はinteractive_runtime.pyへ移動した。
# main()側には、turn・args・stats・situations_this_run・latencies・decayを
# 渡す薄いネスト関数(呼び出しのたびにこの関数のクロージャ経由でそれらの
# 現在値を拾い、interactive_runtime.process_one_situation()へ委譲する)だけを
# main()本体の中に残す——mutableなstats/situations_this_run/latenciesは
# 従来どおり同じオブジェクト参照を共有する。
#
# _interactive_runtime_dependencies()は呼び出しのたびに新しいDependenciesを
# 組み立てる(import時点で固定しない)。理由は他の_xxx_dependencies()と同じ:
# CLIで上書きされうる値・既存テストによるmonkeypatchに呼び出し時点で追随する。
def _interactive_runtime_dependencies() -> interactive_runtime.InteractiveRuntimeDependencies:
    return interactive_runtime.InteractiveRuntimeDependencies(
        append_event_fn=append_event,
        reduce_state_fn=reduce_state,
        is_affordable_fn=is_affordable,
        auto_select_fn=auto_select,
        policy_score_fn=policy_score,
        plan_normal_action_resolution_fn=plan_normal_action_resolution,
        clamp_gain_fn=clamp_gain,
        build_normal_contract_events_fn=build_normal_contract_events,
        plan_settlement_resolution_fn=plan_settlement_resolution,
        build_settlement_effect_events_fn=build_settlement_effect_events,
        call_ollama_fn=call_ollama,
        code_check_narration_fn=code_check_narration,
        run_consistency_check_fn=run_consistency_check,
        policy_vectors=POLICY_VECTORS,
        npc_trust_initial=NPC_TRUST_INITIAL,
        bank_npc_id=BANK_NPC_ID,
        resource_ja=RESOURCE_JA,
        allowed_resources=ALLOWED_RESOURCES,
        narration_system=NARRATION_SYSTEM,
    )


# 2026-08-15(Step 10、段階的モジュール分割): main()の本体だった「イベント
# ソーシング方式のゲームセッション実行」責務全体の実装本体はgame_session.pyへ
# 移動した(今回も関数1個ずつではなく責務全体の粒度)。main()には、
# argparse・CLI設定値の上書き(D_BASE/CREATION_RATE/EVENTS_PATH)・各種
# オフラインモードの分岐・run_game_session(args)の呼び出しだけが残る。
#
# _game_session_dependencies()・_game_session_config()は呼び出しのたびに
# 新しいDependencies/Configを組み立てる(import時点で固定しない)。理由は
# 他の_xxx_dependencies()と同じ: CLIで上書きされうる値(EVENTS_PATH等)・
# 既存テストによるgame.py側関数のmonkeypatchに呼び出し時点で追随するため。
def _game_session_dependencies() -> game_session.GameSessionDependencies:
    return game_session.GameSessionDependencies(
        seed_fn=random.seed,
        choice_fn=random.choice,
        randint_fn=random.randint,
        append_event_fn=append_event,
        reduce_state_fn=reduce_state,
        compute_regen_fn=compute_regen,
        plan_confidence_reversion_fn=plan_confidence_reversion,
        plan_institution_transitions_fn=plan_institution_transitions,
        plan_local_credit_reversion_fn=plan_local_credit_reversion,
        plan_local_credit_transition_fn=plan_local_credit_transition,
        plan_enforcement_reversion_fn=plan_enforcement_reversion,
        plan_enforcement_transition_fn=plan_enforcement_transition,
        plan_barter_upkeep_fn=plan_barter_upkeep,
        plan_barter_transition_fn=plan_barter_transition,
        worst_shortfall_fn=worst_shortfall,
        essential_goods_shortage_penalty_fn=essential_goods_shortage_penalty,
        barter_choice_effects_fn=barter_choice_effects,
        alternative_economy_triggered_fn=alternative_economy_triggered,
        compute_income_fn=compute_income,
        compute_salary_fn=compute_salary,
        compute_decay_fn=compute_decay,
        format_resources_fn=format_resources,
        format_contracts_fn=format_contracts,
        due_contracts_fn=due_contracts,
        generate_settlement_turn_fn=generate_settlement_turn,
        generate_normal_turn_fn=generate_normal_turn,
        pick_theme_fn=pick_theme,
        compute_trait_step_fn=compute_trait_step,
        age_at_fn=age_at,
        format_traits_fn=format_traits,
        is_affordable_fn=is_affordable,
        clamp_gain_fn=clamp_gain,
        draw_prorated_rest_fn=draw_prorated_rest,
        price_index_fn=price_index,
        open_contracts_fn=open_contracts,
        interactive_runtime_dependencies_fn=_interactive_runtime_dependencies,
        repetition_report_fn=repetition_report,
    )


def _game_session_config() -> game_session.GameSessionConfig:
    return game_session.GameSessionConfig(
        events_path=EVENTS_PATH,
        bank_npc_id=BANK_NPC_ID,
        bank_npc_name=BANK_NPC_NAME,
        bank_trust_initial=BANK_TRUST_INITIAL,
        economy_npc_id=ECONOMY_NPC_ID,
        economy_npc_name=ECONOMY_NPC_NAME,
        npc_trust_initial=NPC_TRUST_INITIAL,
        traits=TRAITS,
        npc_pool_cap_range=NPC_POOL_CAP_RANGE,
        initial_traits=INITIAL_TRAITS,
        start_age=START_AGE,
        years_per_turn=YEARS_PER_TURN,
        bank_stage_names=BANK_STAGE_NAMES,
        currency_stage_names=CURRENCY_STAGE_NAMES,
        local_credit_stage_names=LOCAL_CREDIT_STAGE_NAMES,
        local_credit_scope=LOCAL_CREDIT_SCOPE,
        enforcement_stage_names=ENFORCEMENT_STAGE_NAMES,
        enforcement_scope=ENFORCEMENT_SCOPE,
        barter_stage_names=BARTER_STAGE_NAMES,
        barter_stage_trigger_names=BARTER_STAGE_TRIGGER_NAMES,
        barter_scope=BARTER_SCOPE,
        contract_due_range=CONTRACT_DUE_RANGE,
        theme_concern_traits=THEME_CONCERN_TRAITS,
        g_base=G_BASE,
        b_talent=B_TALENT,
        trait_ja=TRAIT_JA,
        turn_time_budget=TURN_TIME_BUDGET,
        min_activity_hours=MIN_ACTIVITY_HOURS,
        initial_resources=INITIAL_RESOURCES,
        policy_vectors=POLICY_VECTORS,
    )


def run_game_session(args) -> None:
    """game_session.pyの実装を再公開する互換ラッパー。"""
    return game_session.run_game_session(
        args, dependencies=_game_session_dependencies(), config=_game_session_config())


def main() -> None:
    global EVENTS_PATH, D_BASE, CREATION_RATE
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen2.5vl:7b")
    parser.add_argument("--turns", type=int, default=None)
    parser.add_argument("--events", default=DEFAULT_EVENTS_NAME,
                        help="イベントログのファイル名(phase1/ 以下)")
    parser.add_argument("--auto", action="store_true", help="自動プレイ(検証用)")
    parser.add_argument("--auto-policy", choices=["balanced", "random"], default="balanced")
    parser.add_argument("--policy", choices=["none"] + list(POLICY_VECTORS), default="none",
                        help="代打ちAIの方針ベクトル。生存制約(worst_after)の上に重ねる")
    parser.add_argument("--safety-floor", type=int, default=SAFETY_FLOOR,
                        help="方針ベクトルを効かせてよい最低ライン(選択後の最小資源)")
    parser.add_argument("--policy-probe", action="store_true",
                        help="ollamaを呼ばず、方針ごとの選択分布だけをオフラインで測る")
    parser.add_argument("--probe-seeds", default="1,2,3,4,5",
                        help="--policy-probe/--policy-check で使うseedのカンマ区切り")
    parser.add_argument("--policy-check", action="store_true",
                        help="反証可能な合否基準(CHECKPOINT_TURNSの複数時点)でPASS/FAILを判定する。"
                             "「単一時点だけを見て均衡と誤判定する」を防ぐための検証コマンド")
    parser.add_argument("--visualize-run", action="store_true",
                        help="1本のシミュレーション(--seed×--policy)を毎ターン粒度で記録し、"
                             "可視化用JSONへ書き出す(ollamaは呼ばない)。--policyが未指定なら"
                             "cautiousを使う。--turnsを指定しなければCHECKPOINT_TURNSの最大値"
                             "(1920)まで回す")
    parser.add_argument("--visualize-output", default="visualize_trace.json",
                        help="--visualize-runの出力先JSONパス")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--theme-injection", choices=["on", "off"], default="on",
                        help="状況の繰り返し対策(コード側の題材をランダム注入)。offで対照群")
    parser.add_argument("--no-consistency-check", action="store_true",
                        help="整合性担保役の呼び出しを省く(既定は有効)")
    parser.add_argument("--negative-control", action="store_true",
                        help="ゲームを回さず、整合性担保役の否定制御テストだけを実行する")
    parser.add_argument("--talent", choices=TRAITS, default=None,
                        help="生まれ持ったスキル(既定: ランダム)")
    parser.add_argument("--endowment", type=int, default=0,
                        help="生まれの資産(money に一度だけ加算)。"
                             "money型=教育・医療の成長経路が金持ちだけのものか、を試す用")
    parser.add_argument("--simulate", action="store_true",
                        help="ollamaを一切呼ばず、特性の成長式だけを長期モンテカルロで回す")
    parser.add_argument("--sim-turns", type=int, default=2000)
    parser.add_argument("--sim-trials", type=int, default=300)
    parser.add_argument("--sim-f", type=float, default=None,
                        help="1特性あたりの発火頻度f(既定: 選択分布と題材分布から自動)")
    parser.add_argument("--sim-choice-dist", default="0.3333,0.3333,0.3334",
                        help="money,labor,social の選択確率(実測値を入れて再現する用)")
    parser.add_argument("--sim-no-theme-rate", type=float, default=0.0,
                        help="題材が無いターン(清算ターン等)の割合")
    parser.add_argument("--d-base", type=float, default=None, help="D_base の上書き")
    parser.add_argument("--creation-rate", type=float, default=None,
                        help="価格水準の伸び率(CREATION_RATE)の上書き。既定0.0025(年率約3%%)。"
                             "2026-08-13、旧--money-decay-rateから改名"
                             "(DECAY_RULESからmoneyを削除したため、その上書きは意味を失った)")
    args = parser.parse_args()
    turns_was_unspecified = args.turns is None
    if args.turns is None:
        args.turns = 12
    if args.turns < 1:
        parser.error("--turns must be at least 1")

    if args.d_base is not None:
        D_BASE = args.d_base
    if args.creation_rate is not None:
        CREATION_RATE = args.creation_rate

    if args.negative_control:
        run_negative_control(args.model)
        return

    if args.simulate:
        dist = [float(x) for x in args.sim_choice_dist.split(",")]
        run_simulation(args.sim_turns, args.sim_trials, dist,
                       args.sim_no_theme_rate, args.seed, args.sim_f)
        return

    if args.policy_check:
        seeds = [int(s) for s in args.probe_seeds.split(",") if s.strip()]
        run_policy_check(seeds, args.safety_floor)
        return

    if args.policy_probe:
        seeds = [int(s) for s in args.probe_seeds.split(",") if s.strip()]
        run_policy_probe(args.turns, seeds, args.safety_floor)
        return

    if args.visualize_run:
        seed = args.seed if args.seed is not None else 1
        policy_name = args.policy if args.policy != "none" else "cautious"
        # --turnsは既定12(main()のLLM対話セッション向け)——--visualize-runで
        # 明示的に変更されていなければ、--policy-checkと同じ設計地平
        # (CHECKPOINT_TURNSの最大値)まで回す。
        turns = max(CHECKPOINT_TURNS) if turns_was_unspecified else args.turns
        run_visualize_trace(seed, policy_name, turns, args.safety_floor,
                           args.talent, args.visualize_output)
        return

    EVENTS_PATH = Path(__file__).parent / args.events
    run_game_session(args)


if __name__ == "__main__":
    main()
