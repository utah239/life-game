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
  1. ストック資産(money/trust)の維持コスト = エントロピー原則の実装。
     何もしなくても DECAY_RULES に従って毎ターン目減りする(decay_applied イベント)。
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

注意: ollama は NAS 本番の paperless-gpt と共用(ai-lab/CLAUDE.md)。
長時間・大量に回し続けるテストはしないこと。

使い方:
  python game.py --turns 12 --auto --seed 1            # 自動プレイ(検証用)
  python game.py --turns 10                            # 対話プレイ
  python game.py --turns 12 --auto --theme-injection off  # 繰り返し対策の対照群
  python game.py --turns 12 --auto --events events_run2.jsonl

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

OLLAMA_HOST = "http://192.168.0.210:11434"
DEFAULT_EVENTS_NAME = "events.jsonl"
EVENTS_PATH = Path(__file__).parent / DEFAULT_EVENTS_NAME  # main() で上書きされうる

# ollamaにモデルをVRAMへ留めておく時間。Phase 0 の判断("NAS本番へVRAMを早く返す"を
# 優先し、ollama既定と同じ5分を意図的に明示する)をそのまま踏襲。
KEEP_ALIVE = "5m"

INITIAL_RESOURCES = {
    "time": 10,     # フロー: 使える時間の余裕
    "energy": 10,   # フロー: 体力
    "peace": 10,    # フロー: 心の余裕
    "money": 8,     # ストック: お金(Phase 0 は 5。維持コストを入れたぶん初期値を上げた)
    "trust": 8,     # ストック: 信用
}
ALLOWED_RESOURCES = set(INITIAL_RESOURCES.keys())
MAX_DELTA = 5  # 1回の選択で資源が動く量の上限(LLMが桁を暴走させる対策。llmモード用)

# --- エントロピー原則(ストック資産の維持コスト) ------------------------------
# 「何もしなくても緩やかに目減りする」。**ストック(money/trust)だけ**を減衰させる。
# interval=n は「nターンに1回」。対価を払う行動で相殺できる程度の小ささにしてある。
DECAY_RULES = [
    {"resource": "money", "interval": 1, "amount": -1},  # 生活費。毎ターン -1
    {"resource": "trust", "interval": 3, "amount": -1},  # 疎遠。3ターンに1回 -1
]

# --- フロー資源の回復(run1 の実測を受けて追加) --------------------------------
# 初版(events_run1_auto14.jsonl)は time/energy を減らす一方で回復手段が無く、
# 14ターンで time:-21 / energy:-14 まで沈んで破綻した。1ターン=数日〜数週間なので、
# フロー資源はターンごとに一定量戻る(上限は初期値)。ストックには適用しない。
#
# ただし run2 では回復量が labor のコストを完全に上回ってしまい、
# **labor型が実質タダの支配戦略**になった(15ターン中13ターンで labor が選ばれ、
# 契約が1件も発生しなかった)。回復は labor のコストをわずかに下回る量にして、
# labor を連打すると少しずつ削られる(=たまに money/social を使わざるを得ない)
# 状態にしてある。labor平均 time-4/energy-3 に対し回復 +3/+2 で、1ターンあたり純減1。
REGEN_RULES = {"time": 3, "energy": 2, "peace": 3}

# --- 収入(同じく run1 を受けて追加) -------------------------------------------
# money は DECAY で毎ターン -1 されるだけで、増える経路がまったく無かった(死の螺旋)。
# 定期収入をコード側のイベントとして入れ、維持コストと釣り合わせる。
# 平均すると +4/4ターン = +1/ターン で decay とちょうど相殺し、行動のぶんだけ純減する。
INCOME_INTERVAL = 4
INCOME_AMOUNT = 4

# 契約を履行したときの信用の回復。trust は DECAY で減る一方なので、
# 「借りを作って、きちんと返す」ことが唯一の信用の作り方になる。
CONTRACT_FULFILL_BONUS = {"trust": 2}

# 資源はこの値を下回る支払いができない(払えない選択肢は選べなくなる)。
# ただし「不履行」だけは常に選べる(払えないから踏み倒す、が成立するように)。
AFFORD_FLOOR = 0

# ==============================================================================
# 特性(器用さ・思考力・スキル・健康)— plan.md「特性の成長経路」の実装
# ==============================================================================
# 資源(フロー/ストック)とは別階層の、0〜100スケールでゆっくり動く値。
# 資源と同じく **すべてコードが決める**(LLMには特性の存在すら知らせていない)。
#
#   基底減衰: 使わなかったターンは T -= D_base
#   成長:     labor/money型の選択が題材の特性と一致したターンに
#             ΔT = G_base × M_talent × (1 - T/100)
#
# D_base の初期値 0.12 は plan.md の再計算値(f≈0.167 を前提に、labor型・才能なしの
# 平衡値が負に落ちない = Phase 1 run1 の「死の螺旋」と同型の破綻を避ける値)。
# それが実測でも妥当かを確かめるのがこの実験の主目的。
TRAITS = ["dexterity", "intellect", "skill", "health"]
TRAIT_JA = {"dexterity": "器用さ", "intellect": "思考力",
            "skill": "スキル", "health": "健康"}
GROWABLE_TRAITS = ["dexterity", "intellect", "skill"]  # 健康は別モデル(下記)

INITIAL_TRAITS = {"dexterity": 50.0, "intellect": 50.0, "skill": 50.0, "health": 80.0}
TRAIT_MIN, TRAIT_MAX = 0.0, 100.0

D_BASE = 0.12          # 基底減衰(器用さ・思考力・スキル)
G_BASE = {"labor": 2.0, "money": 4.0}   # 基礎成長量。money型(教育)の方が速い
B_TALENT = 0.5         # 才能補正。誕生時のスキルと一致すれば M_talent = 1 + B_talent

# --- 健康の別モデル(plan.md「健康は別モデルとして切り離す」) -------------------
# 練習で伸びるものではないので、成長式ではなく
#   ・老化に連動した基底減衰(毎ターン無条件。今回は指示どおり単純な線形)
#   ・money型(医療)での回復
# の2本立てにする。老年ほど加速する非線形カーブはこの実験の対象外。
# 時間粒度は plan.md「世界の時間の流れ方」の決定に合わせる:
#   1ターン = 1ヶ月、1年 = 12ターン。開始年齢は意思決定年齢(13歳、新規スポーンの起点)。
# → 80年の人生 ≒ 960ターン。特性の平衡値(数百ターンで収束)は一生の中で到達しうる、
#   というのがこの粒度での前提になる。
START_AGE = 13
YEARS_PER_TURN = 1.0 / 12.0
HEALTH_DECAY_AT_START = 0.02   # START_AGE 時点の1ターンあたり減衰(= 年 0.24)
HEALTH_DECAY_PER_YEAR = 0.004  # 1歳あたり増える減衰量(線形。老年ほど加速する
                               # 非線形カーブは plan.md どおり今回の対象外)
# 健康の回復量(G_base 相当)。
# plan.md は「回復はmoney型(医療)・peace型に近い行動で得る」としており、当初は
# **money型のみ**で実装した。しかし実測(run1/run2)で、Phase 1の経済では money型が
# 40ターン中22〜28ターンで「資源不足で選べない」状態になり、**健康の回復経路が
# 事実上1度も発火しなかった**(run1: health/money = 0件)。回復経路ゼロは Phase 1 の
# run1 で踏んだ「死の螺旋」と同型なので、labor型(休養・自炊・運動を自力でやる)にも
# 小さい回復を与える。money型(医療)の方が速い、という plan.md の序列は保つ。
HEALTH_CARE_GAIN = {"money": 4.0, "labor": 1.5}


def age_at(turn: int) -> float:
    return START_AGE + turn * YEARS_PER_TURN


def health_decay(turn: int) -> float:
    """老化に連動する健康の基底減衰(単純な線形)。"""
    return HEALTH_DECAY_AT_START + HEALTH_DECAY_PER_YEAR * (age_at(turn) - START_AGE)


def talent_multiplier(talent, trait) -> float:
    return 1.0 + B_TALENT if talent == trait else 1.0


def compute_trait_step(traits: dict, talent, theme_trait, choice_key: str,
                       turn: int, kind: str) -> tuple:
    """このターンの特性の増減を計算する。戻り値は (delta, fired)。

    fired は成長が発火した場合 {"trait","via","talent"} 、しなければ None。
    delta は0〜100にクリップ済みの実適用量(イベントに書くのはこちら。
    reduce_state 側が上限下限を知らなくて済むようにする = clamp_gain と同じ考え方)。
    """
    delta = {}
    fired = None
    grown = None
    # --- 成長 ---------------------------------------------------------------
    # 清算ターン(kind == "settlement")には題材が無いので、成長は発火しない。
    # これは机上の f の計算には入っていない要素で、実測でどれだけ効くかが見どころ。
    if kind == "normal" and theme_trait and choice_key in G_BASE:
        m = talent_multiplier(talent, theme_trait)
        if theme_trait in GROWABLE_TRAITS:
            gain = G_BASE[choice_key] * m * (1.0 - traits[theme_trait] / 100.0)
        else:
            # 健康題材。money型 = 医療にお金を払う(速い)、labor型 = 休養・自炊・運動を
            # 自力でやる(遅い)。どちらにせよ老化の減衰は同じターンに必ず乗る。
            gain = HEALTH_CARE_GAIN[choice_key] * m * (1.0 - traits["health"] / 100.0)
        if gain > 0:
            delta[theme_trait] = gain
            grown = theme_trait
            fired = {"trait": theme_trait, "via": choice_key, "talent": m > 1.0}
    # --- 基底減衰 -----------------------------------------------------------
    for t in TRAITS:
        if t == "health":
            # 老化は行動と無関係に必ず進む(医療で回復しても同じターンに歳は取る)
            delta[t] = delta.get(t, 0.0) - health_decay(turn)
        elif t != grown:
            delta[t] = delta.get(t, 0.0) - D_BASE
    # --- クリップ -----------------------------------------------------------
    out = {}
    for t, v in delta.items():
        cur = traits[t]
        new = max(TRAIT_MIN, min(TRAIT_MAX, cur + v))
        applied = round(new - cur, 6)
        if applied:
            out[t] = applied
    return out, fired

# --- 行動アーキタイプ(コストはコードが決める。Phase 0 --cost-mode code と同じ) ---
ACTION_ARCHETYPES = [
    {"key": "money",  "name": "お金で解決する",
     "ranges": {"money": (-4, -2), "peace": (0, 1)}},
    {"key": "labor",  "name": "自分の時間と労力で解決する",
     "ranges": {"time": (-5, -3), "energy": (-4, -2)}},
    {"key": "social", "name": "人に頼る(借りを作る)",
     "ranges": {"trust": (-2, -1)}, "creates_contract": True},
]

# social型を選ぶと発生する契約(債務)のパラメータ。すべてコードが決める。
CONTRACT_DUE_RANGE = (2, 4)          # 何ターン後が期限か
CONTRACT_REPAY_MONEY = (-4, -2)      # 履行時にお金で返す場合の額
CONTRACT_REPAY_LABOR = {"time": (-4, -2), "energy": (-3, -2)}  # 労力で返す場合
CONTRACT_DEFAULT_PENALTY = {"trust": -3}  # 不履行のペナルティ(信用の失墜)

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
# --- 題材 → 特性の対応表(2026-08-13、trait実験で追加) -----------------------
# plan.md「特性の成長経路」の
#   「THEME_CONCERNS の各項目を4つの特性(器用さ・思考力・スキル・健康)の
#     どれか1つに紐づける表をコード側に持つ。LLMには関与させない」
# をそのまま実装したもの。24項目を4特性に**均等**(6項目ずつ)に割り振ってある。
# 均等割りは plan.md の机上計算(f = (6/24)×(2/3) ≈ 0.167)の前提でもあるので、
# 前提を実測で検証できるよう意図的に崩していない。
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

SITUATION_SYSTEM = """あなたは人生シミュレーションゲームのストーリー生成役です。
プレイヤーの現在の資源状態を踏まえて、次の状況を短い日本語の文章(2〜4文)で描写して
ください。

続けて、プレイヤーが取れる行動を、以下の3つの対価タイプそれぞれについて1つずつ、
この状況に即した具体的な内容で提示してください(**資源の増減量はあなたが決める必要は
ありません。コード側で決まります**。あなたはその行動が具体的に何をすることかという
ラベルだけを書いてください):

1. money型: お金・費用を払って解決する行動
2. labor型: 自分の時間と労力を使って解決する行動
3. social型: 誰かに頼る・借りを作って解決する行動

social型については、**誰に頼るのか**(相手の呼び名。例:「隣の佐藤さん」「兄」
「元同僚の田口」)も答えてください。相手は状況に自然に登場する人物にしてください。

出力は必ず以下のJSON形式のみ。前後に説明文を付けないこと。

{
  "situation": "状況描写(日本語)",
  "choices": {
    "money": "money型の行動の具体的な説明",
    "labor": "labor型の行動の具体的な説明",
    "social": "social型の行動の具体的な説明"
  },
  "social_counterparty": "頼る相手の呼び名",
  "social_favor": "その相手がプレイヤーにしてくれること(15字程度)"
}

social_favor は「**相手がプレイヤーに何をしてくれるのか**」を、相手を主語にして
書いてください(良い例:「修理を手伝ってくれる」「お金を立て替えてくれる」。
悪い例:「〜を頼む」「〜を依頼する」のようにプレイヤー側の動作を書くこと)。
"""

SETTLEMENT_SYSTEM = """あなたは人生シミュレーションゲームのストーリー生成役です。
プレイヤーには**期限の来た借り(約束)**があります。その借りの相手・内容・経過ターン数を
踏まえて、「相手から催促・確認が来る場面」を短い日本語の文章(2〜4文)で描写してください。

# 借りの向き(絶対に間違えないこと)
**助けてもらったのはプレイヤーの側**であり、**返す義務があるのもプレイヤーの側**です。
相手はプレイヤーに貸しがあり、それを返すよう求めてきます。逆(相手がプレイヤーに
返さなければならない、プレイヤーが相手に何かを教える側)にしては絶対にいけません。

続けて、プレイヤーが取れる行動を3つ、この状況に即した具体的な内容で提示してください
(**資源の増減量はあなたが決める必要はありません。コード側で決まります**):

1. money型: お金を返す・費用で清算する
2. labor型: 手伝いや労働で返す
3. avoid型: 今回は返さずに先延ばしにする・言い訳をする

出力は必ず以下のJSON形式のみ。前後に説明文を付けないこと。

{
  "situation": "状況描写(日本語)",
  "choices": {
    "money": "お金で返す行動の具体的な説明",
    "labor": "労力で返す行動の具体的な説明",
    "avoid": "先延ばしにする行動の具体的な説明"
  }
}
"""

NARRATION_SYSTEM = """あなたは人生シミュレーションゲームのストーリー生成役です。
プレイヤーが選んだ選択肢と、その結果を踏まえて、結果を短い日本語の文章(2〜3文)で
描写してください。

# 制約(厳守)
- **数値そのものには絶対に言及しない**。情景・心情として描写すること
  (悪い例:「時間は5、エネルギーは4です」。良い例:「疲れが溜まっていた」)
- **「減ったもの」に挙がっていないものが回復した・増えたと書かないこと**。
  「増えたもの」に何も挙がっていない場合、何かが良くなったとは書かないこと
  (よくある誤り:お金も信頼も減っているのに「信頼が高まった」と書いてしまう)
- 出力は説明文を付けず、描写文のみ"""

RESOURCE_JA = {"time": "時間の余裕", "energy": "体力", "peace": "心の余裕",
               "money": "お金", "trust": "人からの信用"}

# --- コード側の矛盾検出(2026-08-12追加、opus第3回レビュー反映) ---------------
# LLM版の整合性担保役は否定制御テストで検出率33%(2/3見逃し)にとどまった
# (下記 run_negative_control 参照)。「そもそも大半はコードで検出可能」という
# レビューの指摘どおり、正規表現ベースの軽量チェックを先に挟む。ollama呼び出しが
# 不要なので無料・瞬時。LLM版はこれと併用する(どちらかが引っかかれば矛盾とする)。
RESOURCE_SYNONYMS = {
    "money": ["お金", "金銭", "財布", "資金"],
    "trust": ["信頼", "信用"],
    "time": ["時間", "余裕"],
    "energy": ["体力", "エネルギー", "元気"],
    "peace": ["心の余裕", "平穏", "安らぎ", "心"],
}
# 「減った資源」の近くにあると矛盾を疑う、上向きの表現
POSITIVE_PATTERNS = ["高まっ", "深まっ", "強まっ", "増し", "増え", "回復",
                     "良くなっ", "楽になっ", "戻", "豊かになっ", "満たされ", "癒され",
                     "取り戻"]
CONTRADICTION_WINDOW = 15  # 資源の単語の前後何文字を見るか

# 2026-08-12追加(実データ69ターンでの検証で判明): 「高まったというわけではなかった」
# 「回復したというよりは」のように、上向き表現の直後に否定・逆接が来ると、実際には
# 矛盾していない。誤検出の主要因だったので、直後の否定マーカーがあれば除外する。
NEGATION_MARKERS = ["というわけではな", "わけではな", "というよりは", "とは言えな",
                    "ではなかった", "わけでもな", "とまではいかな"]
NEGATION_LOOKAHEAD = 20  # 上向き表現の直後、何文字先まで否定マーカーを探すか


def code_check_narration(not_up_keys: list, narration: str) -> list:
    """「今回増えていない資源」(減った、または変化していない資源。キーのリスト)に
    ついて、ナレーション文中でその資源に触れつつ上向きの表現が近くに無いかを
    正規表現ベースで探す。減った資源だけでなく「変化していない資源」も対象にする
    (「何の対価も払わず秩序が回復した」というエントロピー違反パターンを拾うため。
    下記の否定制御テストcase3=「何もしていないのに満たされていく」がこれ)。
    上向き表現の直後に否定マーカーがある場合は除外する(「高まったというわけでは
    なかった」等、実際には矛盾していないケースの誤検出対策)。
    戻り値は矛盾の説明のリスト(空なら検出なし)。LLMを呼ばないので瞬時・無料。"""
    issues = []
    for key in not_up_keys:
        for syn in RESOURCE_SYNONYMS.get(key, []):
            idx = narration.find(syn)
            if idx < 0:
                continue
            win_start = max(0, idx - CONTRADICTION_WINDOW)
            window = narration[win_start: idx + len(syn) + CONTRADICTION_WINDOW]
            for pos in POSITIVE_PATTERNS:
                pos_in_win = window.find(pos)
                if pos_in_win < 0:
                    continue
                pos_abs = win_start + pos_in_win
                lookahead = narration[pos_abs: pos_abs + len(pos) + NEGATION_LOOKAHEAD]
                if any(neg in lookahead for neg in NEGATION_MARKERS):
                    continue  # 直後に否定表現があるので矛盾とはみなさない
                issues.append(f"{key}(減ったはず): 「{syn}」の近くに「{pos}」")
                break
    return issues

CONSISTENCY_SYSTEM = """あなたはこの世界の整合性担保役です。担当は「絶対不変の法則」
(エントロピー増大則・資源保存則)への違反検出のみで、法律や社会規範の妥当性は
判定しません。

【2026-08-12 修正、2回目】旧版(自由記述で「OK」/一言、コスト数値のみ検査)は
ナレーション文を検査対象にしても、否定制御テストで3/3件の矛盾を見逃した(0%検出)。
JSON形式の強制+具体例の提示で検出率が上がるか試す2回目の修正版。

以下の情報を照らし合わせ、**ナレーション文だけを見て**、実際の資源の増減の向きと
矛盾していないかを判定してください:
1. 「実際に減った資源」に対応する言葉を、ナレーション文が増えた・回復した・
   良くなったという意味合いで使っていないか
2. 「実際に増えていない資源」について、ナレーション文が増えた・良くなったと
   書いていないか

# 具体例
例1(矛盾あり): 減った資源=人からの信用 / 増えた資源=なし /
ナレーション文「信頼関係もぐっと深まった気がした。」
→ 減ったはずの信用が「深まった」(増えた)と書かれている。矛盾あり。

例2(矛盾なし): 減った資源=お金 / 増えた資源=なし /
ナレーション文「財布は軽くなったが、代わりに知識と経験を得られた。」
→ お金が減ったことと整合している。知識・経験は資源の増減表に無い言葉なので対象外。
矛盾なし。

# 出力形式(JSON、これ以外の文字列を出力しないこと)
{"contradiction": true または false, "reason": "矛盾がある場合はどの資源か(15字程度)、無ければ空文字"}
"""


# ==============================================================================
# ollama / イベントログ
# ==============================================================================

def call_ollama(model: str, system: str, user: str, want_json: bool) -> tuple:
    """ollamaにプロンプトを投げ、(応答テキスト, レイテンシ秒)を返す"""
    payload = {
        "model": model,
        "prompt": user,
        "system": system,
        "stream": False,
        "keep_alive": KEEP_ALIVE,
    }
    if want_json:
        payload["format"] = "json"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/generate", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"[ollama接続エラー] {OLLAMA_HOST} に到達できません: {e}", file=sys.stderr)
        sys.exit(1)
    elapsed = time.monotonic() - start
    return body.get("response", ""), elapsed


def append_event(event_type: str, data: dict) -> None:
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "type": event_type,
        "data": data,
    }
    with EVENTS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def iter_events():
    if not EVENTS_PATH.exists():
        return
    with EVENTS_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


# ==============================================================================
# 状態の再構築(イベントソーシング)
# ==============================================================================

def reduce_state() -> dict:
    """events.jsonl を畳み込んで現在の世界状態を再構築する。

    資源に加え、契約・債務テーブルと現在ターン番号も同じログから再構築する。
    ここが唯一の「今の状態」の定義であり、メモリ上に状態を持ち回さない。
    """
    resources = dict(INITIAL_RESOURCES)
    traits = dict(INITIAL_TRAITS)
    talent = None
    contracts = {}
    turn = 0
    for event in iter_events():
        etype, data = event["type"], event["data"]
        if etype == "turn_started":
            turn = data["turn"]
        elif etype == "character_born":
            talent = data.get("talent")
        elif etype == "trait_changed":
            for k, v in data.get("delta", {}).items():
                if k in traits:
                    traits[k] = round(traits[k] + v, 6)
        elif etype in ("state_applied", "decay_applied", "regen_applied",
                       "income_applied", "contract_settled", "endowment_applied"):
            for k, v in data.get("delta", {}).items():
                if k in ALLOWED_RESOURCES:
                    resources[k] = resources.get(k, 0) + v
        if etype == "contract_created":
            contracts[data["id"]] = {
                "id": data["id"],
                "counterparty": data["counterparty"],
                "description": data["description"],
                "created_turn": data["created_turn"],
                "due_turn": data["due_turn"],
                "repay_money": data["repay_money"],
                "status": "open",
            }
        elif etype == "contract_settled":
            c = contracts.get(data["id"])
            if c:
                c["status"] = data["outcome"]          # fulfilled / defaulted
                c["settled_turn"] = data["turn"]
                c["settled_by"] = data.get("settled_by")
    return {"resources": resources, "contracts": contracts, "turn": turn,
            "traits": traits, "talent": talent}


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

def sanitize_cost(raw_cost: dict) -> tuple:
    """LLMが返したcost辞書を検証する(Phase 0 から継承)。
    許可された資源名以外は捨て、値は±MAX_DELTAにクリップする。
    Phase 1 の通常フローではLLMがコストを書かないため出番は無いが、防御として残す。"""
    clean, rejected, clipped = {}, [], []
    for k, v in raw_cost.items():
        if k not in ALLOWED_RESOURCES:
            rejected.append(k)
            continue
        try:
            v = int(v)
        except (TypeError, ValueError):
            rejected.append(k)
            continue
        if v > MAX_DELTA or v < -MAX_DELTA:
            clipped.append(k)
            v = max(-MAX_DELTA, min(MAX_DELTA, v))
        if v != 0:
            clean[k] = v
    return clean, rejected, clipped


def draw_ranges(ranges: dict) -> dict:
    return {res: random.randint(lo, hi) for res, (lo, hi) in ranges.items()}


def draw_archetype_cost(key: str) -> dict:
    archetype = next(a for a in ACTION_ARCHETYPES if a["key"] == key)
    cost = draw_ranges(archetype["ranges"])
    return {k: v for k, v in cost.items() if v != 0}


def compute_decay(turn: int) -> dict:
    """このターンに適用される維持コスト(エントロピー)を計算する。LLMは関与しない。"""
    delta = {}
    for rule in DECAY_RULES:
        if turn % rule["interval"] == 0:
            delta[rule["resource"]] = delta.get(rule["resource"], 0) + rule["amount"]
    return delta


def compute_regen(resources: dict) -> dict:
    """フロー資源の回復量。上限(初期値)を超えないぶんだけ戻す。
    イベントには計算後の具体値を書くので、reduce_state 側は上限を知らなくてよい。"""
    delta = {}
    for res, amount in REGEN_RULES.items():
        room = INITIAL_RESOURCES[res] - resources.get(res, 0)
        gain = max(0, min(amount, room))
        if gain:
            delta[res] = gain
    return delta


def compute_income(turn: int) -> dict:
    return {"money": INCOME_AMOUNT} if turn % INCOME_INTERVAL == 0 else {}


def clamp_gain(delta: dict, resources: dict) -> dict:
    """フロー資源が上限(初期値)を超えないように、プラス側の増分だけ削る。
    削った結果をイベントに書くので、reduce_state は上限を知らなくてよい。
    (run2 で money型選択の peace+1 が積み上がり peace:11 になっていた)"""
    out = {}
    for k, v in delta.items():
        if v > 0 and k in REGEN_RULES:
            v = max(0, min(v, INITIAL_RESOURCES[k] - resources.get(k, 0)))
        if v:
            out[k] = v
    return out


def is_affordable(choice: dict, resources: dict) -> bool:
    """払えるか。不履行(踏み倒し)は常に選べる。"""
    if choice.get("settle") == "defaulted":
        return True
    for k, v in choice["cost"].items():
        if v < 0 and resources.get(k, 0) + v < AFFORD_FLOOR:
            return False
    return True


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


def build_situation_prompt(state: dict, turn: int, theme: str) -> str:
    """状況生成プロンプト。

    題材(theme)を**先頭**に置き、「この題材以外の話題を出すな」と排他的に指示するのが
    繰り返し対策の要。Phase 1 の probe(README「状況の繰り返し対策」節)で、題材を
    プロンプト末尾に置いた場合(類似度 最大0.42・前ターンの話題の混入あり)より
    明確に効くことを実測した。直近履歴は先頭40字に切り詰めて「参考」として後ろに置く
    (全文を載せると few-shot の見本として機能してしまい、逆に模倣を誘発する)。
    """
    parts = []
    if theme:
        parts += [f"■今回必ず描写する題材: 「{theme}」",
                  "この題材以外の話題を出さないこと。場所も出来事もこの題材から決めること。"]
    parts += [f"現在のターン: {turn}",
              f"現在の資源状態: {json.dumps(state['resources'], ensure_ascii=False)}"]
    opens = open_contracts(state)
    if opens:
        # run3 では、この未清算の借りが**状況の主題を乗っ取った**(c5の「家具組み立て」が
        # T14・T15の状況を占拠した)。さらにLLMが清算ターンでもないのに「借りを返す」
        # 選択肢を出し、それを選んでも契約テーブルは動かない(=表示と実態の食い違い)
        # という破綻も起きた。用途を「背景の一言」に厳しく限定する。
        pending = "\n".join(
            f"- {c['counterparty']} に「{c['description']}」の借りがある(期限: ターン{c['due_turn']})"
            for c in opens
        )
        parts.append(
            f"(背景情報)まだ返していない借り:\n{pending}\n"
            "この借りは**状況の主題にしないこと**。心に引っかかっている程度に一言触れるだけに\n"
            "とどめ、借りの返済・清算を選択肢に含めてはいけない(返済の場面は別途用意される)。"
        )
        used_names = sorted({c["counterparty"] for c in opens})
        parts.append(f"social型で頼る相手は、次の人物以外にすること: {'、'.join(used_names)}")
    history = recent_situations(3)
    if history:
        parts.append("(参考)過去に描写済みの状況。二度と同じ話をしないこと:\n"
                     + "\n".join(f"- {s[:40]}" for s in history))
    return "\n".join(parts)


def build_settlement_prompt(state: dict, turn: int, contract: dict) -> str:
    overdue = turn - contract["due_turn"]
    return "\n".join([
        f"現在のターン: {turn}",
        f"現在の資源状態: {json.dumps(state['resources'], ensure_ascii=False)}",
        f"貸してくれた側(債権者): {contract['counterparty']}",
        f"プレイヤーが {contract['counterparty']} にしてもらったこと: {contract['description']}",
        f"→ したがって、返す義務があるのはプレイヤー。{contract['counterparty']} が催促してくる側。",
        f"借りができたターン: {contract['created_turn']} / 期限: ターン{contract['due_turn']}"
        + (f"(**{overdue}ターン超過**)" if overdue > 0 else "(今日が期限)"),
    ])


# ==============================================================================
# ターン処理
# ==============================================================================

def clean_label(text: str, fallback: str) -> str:
    """LLMがJSONスキーマの説明文をそのまま値に混ぜてくることがあるので剥がす。
    実測例(run1 T4): "お金で返す行動の具体的な説明: アドバイス代として100円を返済する。"
    """
    if not isinstance(text, str) or not text.strip():
        return fallback
    text = text.strip()
    for marker in ("の具体的な説明:", "の具体的な説明:", "型の行動:", "の行動:"):
        pos = text.find(marker)
        if 0 <= pos <= 30:
            text = text[pos + len(marker):].strip()
            break
    return text or fallback


def parse_json_response(raw: str) -> dict:
    """format=json を付けていても稀に前後にゴミが付くので、最外の{}を拾う保険を入れる"""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start:end + 1])
        raise


def generate_normal_turn(model: str, state: dict, turn: int, theme: str, latencies: list) -> dict:
    prompt = build_situation_prompt(state, turn, theme)
    raw, latency = call_ollama(model, SITUATION_SYSTEM, prompt, want_json=True)
    latencies.append(latency)
    append_event("llm_call", {"role": "situation", "model": model,
                              "latency_sec": latency, "theme": theme, "raw": raw})
    parsed = parse_json_response(raw)
    if "situation" not in parsed or "choices" not in parsed:
        raise ValueError("situation/choices が無い")

    choices = []
    for archetype in ACTION_ARCHETYPES:
        label = clean_label(parsed["choices"].get(archetype["key"]), archetype["name"])
        choice = {"key": archetype["key"], "label": label,
                  "cost": draw_archetype_cost(archetype["key"])}
        if archetype.get("creates_contract"):
            choice["contract"] = {
                "counterparty": str(parsed.get("social_counterparty") or "知人"),
                "description": str(parsed.get("social_favor") or label)[:40],
                "due_turn": turn + random.randint(*CONTRACT_DUE_RANGE),
                "repay_money": random.randint(*CONTRACT_REPAY_MONEY),
            }
        choices.append(choice)
    return {"situation": parsed["situation"], "choices": choices,
            "latency": latency, "kind": "normal"}


def generate_settlement_turn(model: str, state: dict, turn: int, contract: dict,
                             latencies: list) -> dict:
    prompt = build_settlement_prompt(state, turn, contract)
    raw, latency = call_ollama(model, SETTLEMENT_SYSTEM, prompt, want_json=True)
    latencies.append(latency)
    append_event("llm_call", {"role": "settlement_situation", "model": model,
                              "latency_sec": latency, "contract_id": contract["id"], "raw": raw})
    parsed = parse_json_response(raw)
    if "situation" not in parsed or "choices" not in parsed:
        raise ValueError("situation/choices が無い")
    ch = parsed["choices"]

    # 履行/不履行の判定も、それに伴う資源変化も、すべてコードが決める。
    # 履行するとCONTRACT_FULFILL_BONUS(信用)が戻る。trustはdecayで減る一方なので、
    # 「借りを作って、きちんと返す」ことが実質的に唯一の信用の作り方になっている。
    money_cost = {"money": contract["repay_money"]}
    money_cost.update(CONTRACT_FULFILL_BONUS)
    labor_cost = draw_ranges(CONTRACT_REPAY_LABOR)
    labor_cost.update(CONTRACT_FULFILL_BONUS)
    choices = [
        {"key": "money",
         "label": clean_label(ch.get("money"), f"{contract['counterparty']}にお金で返す"),
         "cost": money_cost, "settle": "fulfilled"},
        {"key": "labor",
         "label": clean_label(ch.get("labor"), f"{contract['counterparty']}を手伝って返す"),
         "cost": labor_cost, "settle": "fulfilled"},
        {"key": "avoid",
         "label": clean_label(ch.get("avoid"), "今回は返さずに先延ばしにする"),
         "cost": dict(CONTRACT_DEFAULT_PENALTY), "settle": "defaulted"},
    ]
    return {"situation": parsed["situation"], "choices": choices,
            "latency": latency, "kind": "settlement", "contract": contract}


def run_consistency_check(model: str, down: list, up: list, narration: str,
                          latencies: list, log: bool = True) -> tuple:
    """ナレーション文が実際の資源変化の向きと矛盾していないかを検査する。
    2026-08-12修正(1回目): 旧版はコストの数値(構造上、違反が発生し得ない)を見ていたため、
    69/69回すべて"OK"を返す空回りだった(phase1初版の実測で判明)。実際に破綻するのは
    ナレーション文なので、そちらを検査対象に変更。
    2026-08-12修正(2回目): 1回目の修正版を否定制御テストにかけたところ、矛盾3件を
    3件とも見逃した(検出率0%)。JSON形式強制+具体例提示に変更し、戻り値を
    (flagged: bool, reason: str, latency) に変える。"""
    prompt = (
        f"実際に減った資源: {'、'.join(down) if down else 'なし'}\n"
        f"実際に増えた資源: {'、'.join(up) if up else 'なし'}\n"
        f"ナレーション文: {narration.strip()}"
    )
    raw, latency = call_ollama(model, CONSISTENCY_SYSTEM, prompt, want_json=True)
    latencies.append(latency)
    if log:
        append_event("llm_call", {"role": "consistency_check", "model": model,
                                  "latency_sec": latency, "raw": raw})
    try:
        parsed = parse_json_response(raw)
        flagged = bool(parsed.get("contradiction"))
        reason = str(parsed.get("reason") or "")
    except Exception:
        flagged, reason = False, f"[JSON解析失敗: {raw[:60]}]"
    return flagged, reason, latency


# ==============================================================================
# 整合性担保役の否定制御テスト(negative control)
# ==============================================================================
# 2回目・3回目のopusレビューが共通して指摘した点: 「OK」が返り続けることは、
# チェックが機能している証拠にはならない(構造上OKしか返せない可能性がある)。
# 意図的に矛盾したケースを流し、NGを検出できることを実際に確認する。
NEGATIVE_CONTROL_CASES = [
    # (down, up, narration, 矛盾を検出すべきか)
    (["人からの信用"], [], "信頼関係もぐっと深まった気がした。", True),
    (["時間の余裕", "体力"], [], "疲れは消え、すっかり元気を取り戻していた。", True),
    ([], [], "何もせずただ座っているだけで、心の底から満たされていくのを感じた。", True),
    (["お金"], [], "財布は軽くなったが、代わりに知識と経験を得られた。", False),
    (["体力"], [], "身体は疲れていたが、それでも前へ進む理由があった。", False),
    (["人からの信用"], [], "信用を失ったことが、これからずしりと重くのしかかりそうだった。", False),
    ([], ["お金"], "思わぬ臨時収入に、財布の中身が少し温かくなった。", False),
]


JA_TO_KEY = {v: k for k, v in RESOURCE_JA.items()}


def run_negative_control(model: str) -> None:
    """コード版(正規表現)とLLM版、両方の検出率を同じケースで比較する。"""
    print(f"=== 整合性担保役 否定制御テスト (model={model}) ===")
    print(f"ケース数: {len(NEGATIVE_CONTROL_CASES)}\n")
    correct_code, correct_llm = 0, 0
    for i, (down, up, narration, should_flag) in enumerate(NEGATIVE_CONTROL_CASES, 1):
        up_keys = [JA_TO_KEY[u] for u in up if u in JA_TO_KEY]
        not_up_keys = [k for k in ALLOWED_RESOURCES if k not in up_keys]
        code_issues = code_check_narration(not_up_keys, narration)
        code_flagged = bool(code_issues)
        code_hit = code_flagged == should_flag
        correct_code += code_hit

        flagged, reason, latency = run_consistency_check(model, down, up, narration, [], log=False)
        llm_hit = flagged == should_flag
        correct_llm += llm_hit

        print(f"case{i} 期待={'検出' if should_flag else '通過'}  narration: {narration}")
        print(f"  [code {'OK' if code_hit else '!!'}] "
              f"{('検出: ' + '; '.join(code_issues)) if code_flagged else '通過'}")
        print(f"  [llm  {'OK' if llm_hit else '!!'}] ({latency:.1f}秒) "
              f"{('検出: ' + reason) if flagged else '通過'}\n")
    n = len(NEGATIVE_CONTROL_CASES)
    print(f"=== 結果: code版 {correct_code}/{n} / llm版 {correct_llm}/{n} ===")


# ==============================================================================
# 自動プレイの方針
# ==============================================================================

def auto_select(choices: list, resources: dict, policy: str) -> int:
    """自動プレイでの選択。--auto-policy で切り替える。
    balanced: 払える選択の中から、選択後にいちばん資源が偏らないものを選ぶ
    random  : 払える選択から完全ランダム(対照群)"""
    ok = [i for i, c in enumerate(choices) if is_affordable(c, resources)]
    if not ok:
        # どれも払えない場合は最も傷が浅いものを選ぶ。
        # run1 では min/max を取り違えていて「最も傷の深い選択肢」を選び続けていた
        # (labor を14ターン中11回選び、time が -21 まで沈んだ原因のひとつ)。
        return max(range(len(choices)),
                   key=lambda i: sum(v for v in choices[i]["cost"].values() if v < 0))
    if policy == "random":
        return random.choice(ok)

    def worst_after(i: int) -> int:
        after = dict(resources)
        for k, v in choices[i]["cost"].items():
            after[k] = after.get(k, 0) + v
        return min(after.values())
    return max(ok, key=worst_after)


# ==============================================================================
# 繰り返し度の計測(Phase 0 は目視だったので、数値で出せるようにした)
# ==============================================================================

def bigrams(text: str) -> set:
    t = "".join(text.split())
    return {t[i:i + 2] for i in range(len(t) - 1)}


def repetition_report(situations: list) -> dict:
    """全ペアの文字bigram Jaccard類似度。最大・平均と、0.5超のペア数を返す"""
    if len(situations) < 2:
        return {"pairs": 0, "max": 0.0, "mean": 0.0, "near_duplicates": 0}
    grams = [bigrams(s) for s in situations]
    sims = []
    dup = 0
    for i in range(len(grams)):
        for j in range(i + 1, len(grams)):
            union = grams[i] | grams[j]
            sim = len(grams[i] & grams[j]) / len(union) if union else 0.0
            sims.append(sim)
            if sim > 0.5:
                dup += 1
    return {"pairs": len(sims), "max": max(sims),
            "mean": sum(sims) / len(sims), "near_duplicates": dup}


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
                    turns: int, trials: int, start: float = 50.0) -> float:
    """「毎ターン確率fで、常に同じ型で発火する」特性を1つだけ回す。
    解析解の答え合わせ用(式の実装と解析解が食い違っていないかの対照)。"""
    total = 0.0
    for _ in range(trials):
        t = start
        for _ in range(turns):
            if random.random() < f:
                t = min(TRAIT_MAX, t + g_base * m_talent * (1.0 - t / 100.0))
            else:
                t = max(TRAIT_MIN, t - d_base)
        total += t
    return total / trials


def simulate_mixed(turns: int, trials: int, dist: list, no_theme_rate: float,
                   talent: str) -> dict:
    """実走と同じ構造(4特性・題材ランダム・3択)で回す。
    compute_trait_step() をそのまま使うので、式の二重実装にはならない。"""
    keys = ["money", "labor", "social"]
    snapshots = {}
    # 1ターン=1ヶ月なので、120=10年、360=30年、960=80年。人生の節目で見る。
    marks = sorted({m for m in (50, 120, 360, 600, 960, turns) if m <= turns})
    acc = {m: {t: 0.0 for t in TRAITS} for m in marks}
    for _ in range(trials):
        traits = dict(INITIAL_TRAITS)
        used_places, used_concerns = [], []
        for turn in range(1, turns + 1):
            if random.random() < no_theme_rate:
                theme_trait, kind = None, "settlement"
            else:
                _, concern = pick_theme(used_places, used_concerns)
                theme_trait, kind = THEME_CONCERN_TRAITS[concern], "normal"
                used_places[:] = used_places[-8:]
                used_concerns[:] = used_concerns[-8:]
            choice_key = random.choices(keys, weights=dist, k=1)[0]
            delta, _fired = compute_trait_step(traits, talent, theme_trait,
                                               choice_key, turn, kind)
            for k, v in delta.items():
                traits[k] += v
            if turn in acc:
                for t in TRAITS:
                    acc[turn][t] += traits[t]
    for m in marks:
        snapshots[m] = {t: acc[m][t] / trials for t in TRAITS}
    return snapshots


def run_simulation(turns: int, trials: int, dist: list, no_theme_rate: float,
                   seed, f_override) -> None:
    if seed is not None:
        random.seed(seed)
    s = sum(dist)
    dist = [d / s for d in dist]
    p_labor_or_money = dist[0] + dist[1]
    # 1特性あたりの発火頻度。題材24項目のうち6項目が該当 = 1/4。
    f_auto = (1.0 - no_theme_rate) * 0.25 * p_labor_or_money
    f = f_override if f_override is not None else f_auto
    print("=== 特性の長期シミュレーション(ollama不使用)===")
    print(f"D_base={D_BASE} / G_base={G_BASE} / B_talent={B_TALENT} / "
          f"選択分布 money,labor,social={['%.3f' % d for d in dist]} / "
          f"題材なしターン率={no_theme_rate:.3f}")
    print(f"1特性あたりの発火頻度 f = {f:.4f}"
          + ("(指定値)" if f_override is not None else "(選択分布から算出)")
          + f" / 机上の仮定 0.167\n")

    print("--- 4通りの平衡値(解析解 vs 式のモンテカルロ)---")
    print(f"{'条件':<22}{'解析解':>10}{'MC実測':>10}{'f_crit':>12}")
    for label, g, m in [("labor型・才能なし", G_BASE["labor"], 1.0),
                        ("labor型・才能あり", G_BASE["labor"], 1.0 + B_TALENT),
                        ("money型・才能なし", G_BASE["money"], 1.0),
                        ("money型・才能あり", G_BASE["money"], 1.0 + B_TALENT)]:
        an = equilibrium(f, g, m, D_BASE)
        mc = simulate_forced(f, g, m, D_BASE, turns, max(50, trials // 4))
        # 平衡値が0になる臨界の発火頻度 f_crit = D/(D + G×M)。
        # 実測のfがこれを下回ると「死の螺旋」(ゼロ張り付き)になる。
        f_crit = D_BASE / (D_BASE + g * m)
        print(f"{label:<22}{an:>10.1f}{mc:>10.1f}{f_crit:>12.4f}")
    print("  ※解析解が負のときはゼロに張り付く(=「死の螺旋」)。"
          "f_crit は平衡値が0になる臨界の発火頻度\n")

    print(f"--- 実走と同じ構造での推移(4特性混在、{trials}試行の平均)---")
    for talent in TRAITS:
        snaps = simulate_mixed(turns, trials, dist, no_theme_rate, talent)
        print(f"  才能={TRAIT_JA[talent]}")
        for m in sorted(snaps):
            print(f"    T{m:<4}(年齢{age_at(m):4.0f}): "
                  + " / ".join(f"{TRAIT_JA[t]}:{snaps[m][t]:5.1f}"
                               + ("*" if t == talent else " ") for t in TRAITS))


# ==============================================================================
# メイン
# ==============================================================================

def main() -> None:
    global EVENTS_PATH, D_BASE
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen2.5vl:7b")
    parser.add_argument("--turns", type=int, default=12)
    parser.add_argument("--events", default=DEFAULT_EVENTS_NAME,
                        help="イベントログのファイル名(phase1/ 以下)")
    parser.add_argument("--auto", action="store_true", help="自動プレイ(検証用)")
    parser.add_argument("--auto-policy", choices=["balanced", "random"], default="balanced")
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
    args = parser.parse_args()

    if args.d_base is not None:
        D_BASE = args.d_base

    if args.negative_control:
        run_negative_control(args.model)
        return

    if args.simulate:
        dist = [float(x) for x in args.sim_choice_dist.split(",")]
        run_simulation(args.sim_turns, args.sim_trials, dist,
                       args.sim_no_theme_rate, args.seed, args.sim_f)
        return

    EVENTS_PATH = Path(__file__).parent / args.events
    if args.seed is not None:
        random.seed(args.seed)

    print(f"=== 人生ゲーム Phase 1 (model={args.model}, auto={args.auto}, "
          f"theme-injection={args.theme_injection}, seed={args.seed}) ===")
    print(f"events: {EVENTS_PATH}\n")

    latencies, parse_failures = [], 0
    situations_this_run = []
    used_places, used_concerns = [], []
    stats = {"settlement_turns": 0, "contracts_created": 0, "fulfilled": 0, "defaulted": 0,
             "decay_total": {}, "income_total": {}, "blocked_choices": 0,
             "code_check_hits": 0, "llm_check_hits": 0,
             # 特性実験用
             "theme_turns": {}, "no_theme_turns": 0, "choice_counts": {},
             "fire_counts": {}, "fires": 0, "played_turns": 0}

    # --- 誕生(特性の初期値と、生まれ持ったスキル=才能)------------------------
    # plan.md では誕生時3択だが、この実験では指示どおりランダムに1つ割り当てる。
    # 台帳(events.jsonl)に残すので、再開しても同じ才能が復元される。
    born = reduce_state()
    talent = born["talent"]
    if talent is None:
        talent = args.talent or random.choice(TRAITS)
        append_event("character_born", {
            "talent": talent, "initial_traits": INITIAL_TRAITS,
            "start_age": START_AGE, "years_per_turn": YEARS_PER_TURN,
        })
        print(f"誕生: 生まれ持ったスキル = {TRAIT_JA[talent]} "
              f"(該当する特性の成長が {1 + B_TALENT} 倍)")
        if args.endowment:
            # 初期値の定数をいじらず、イベントとして一度だけ積む(台帳だけで
            # 状態が再構築できる、というPhase 0からの性質を壊さないため)
            append_event("endowment_applied",
                         {"delta": {"money": args.endowment}, "reason": "birth_endowment"})
            print(f"　　生まれの資産: money+{args.endowment}")
    else:
        print(f"(継続) 生まれ持ったスキル = {TRAIT_JA[talent]}")
    print(f"初期特性: {format_traits(born['traits'], talent)}\n")

    start_turn = reduce_state()["turn"]
    for offset in range(1, args.turns + 1):
        turn = start_turn + offset
        append_event("turn_started", {"turn": turn})

        # --- 1. ターン開始時の自動処理。すべてコード側で決まり、LLMは関与しない -------
        #    (a) フロー資源の回復  (b) 定期収入  (c) 維持コスト(エントロピー)
        regen = compute_regen(reduce_state()["resources"])
        if regen:
            append_event("regen_applied", {"turn": turn, "delta": regen, "reason": "flow_recovery"})
        income = compute_income(turn)
        if income:
            append_event("income_applied", {"turn": turn, "delta": income, "reason": "periodic_income"})
            for k, v in income.items():
                stats["income_total"][k] = stats["income_total"].get(k, 0) + v
        decay = compute_decay(turn)
        if decay:
            append_event("decay_applied", {"turn": turn, "delta": decay,
                                           "reason": "stock_maintenance"})
            for k, v in decay.items():
                stats["decay_total"][k] = stats["decay_total"].get(k, 0) + v

        state = reduce_state()
        decay_str = ", ".join(f"{k}{v:+d}" for k, v in decay.items()) or "なし"
        regen_str = ", ".join(f"{k}{v:+d}" for k, v in regen.items()) or "なし"
        income_str = ", ".join(f"{k}{v:+d}" for k, v in income.items()) or "なし"
        print(f"--- ターン{turn}(年齢{age_at(turn):.0f}) --- "
              f"資源: {format_resources(state['resources'])}")
        print(f"    回復: {regen_str}  /  収入: {income_str}  /  維持コスト: {decay_str}")
        print(f"    未清算の借り: {format_contracts(state)}")

        # --- 2. 期限の来た契約があれば、強制的に清算ターンにする -------------------
        # 清算ターンには題材が無い(= 特性成長の機会が消える)。机上の f の計算には
        # 入っていない要素なので、theme_trait を None のまま通して実測に反映させる。
        due = due_contracts(state, turn)
        theme_trait = None
        try:
            if due:
                contract = due[0]
                stats["settlement_turns"] += 1
                print(f"    [期限到来] {contract['id']} {contract['counterparty']}"
                      f"「{contract['description']}」(期限T{contract['due_turn']})")
                tr = generate_settlement_turn(args.model, state, turn, contract, latencies)
            else:
                if args.theme_injection == "on":
                    theme, concern = pick_theme(used_places, used_concerns)
                    theme_trait = THEME_CONCERN_TRAITS.get(concern)
                else:
                    theme, theme_trait = "", None
                tr = generate_normal_turn(args.model, state, turn, theme, latencies)
        except Exception as e:
            print(f"  [解析エラー] {e}")
            append_event("parse_error", {"turn": turn, "error": str(e)})
            parse_failures += 1
            continue

        append_event("situation_presented",
                     {"turn": turn, "kind": tr["kind"], "situation": tr["situation"]})
        situations_this_run.append(tr["situation"])

        print(f"[{tr['latency']:.1f}秒] {tr['situation']}")
        choices = tr["choices"]
        available = []
        for i, choice in enumerate(choices, 1):
            cost_str = ", ".join(f"{k}{v:+d}" for k, v in choice["cost"].items()) or "変化なし"
            extra = ""
            if "contract" in choice:
                extra = (f" [借りが発生: {choice['contract']['counterparty']}"
                         f" / 期限T{choice['contract']['due_turn']}]")
            if choice.get("settle") == "defaulted":
                extra = " [不履行になる]"
            elif choice.get("settle") == "fulfilled":
                extra = " [借りを返す]"
            # 払えない選択肢は選べない(資源が青天井にマイナスへ沈むのを防ぐ)。
            # 不履行だけは常に選べる = 「払えないから踏み倒す」が成立する。
            if is_affordable(choice, state["resources"]):
                available.append(i)
            else:
                extra += " ※資源不足で選べない"
                stats["blocked_choices"] += 1
            print(f"  {i}. {choice['label']} ({cost_str}){extra}")

        # --- 3. 選択 -----------------------------------------------------------
        if args.auto:
            idx = auto_select(choices, state["resources"], args.auto_policy)
            print(f"選択(自動/{args.auto_policy}): {idx + 1}")
        else:
            valid = [str(i) for i in available] or [str(i) for i in range(1, len(choices) + 1)]
            sel = None
            while sel not in valid + ["q"]:
                sel = input(f"選択 ({'/'.join(valid)}, qで終了): ").strip()
            if sel == "q":
                break
            idx = int(sel) - 1
        choice = choices[idx]

        # --- 4. 適用(資源・契約テーブルの更新はすべてコード側) ------------------
        choice["cost"] = clamp_gain(choice["cost"], state["resources"])
        append_event("state_applied", {"turn": turn, "delta": choice["cost"],
                                       "choice_key": choice["key"],
                                       "choice_label": choice["label"]})
        if "contract" in choice:
            state_now = reduce_state()
            cid = f"c{len(state_now['contracts']) + 1}"
            c = choice["contract"]
            append_event("contract_created", {
                "id": cid, "counterparty": c["counterparty"], "description": c["description"],
                "created_turn": turn, "due_turn": c["due_turn"],
                "repay_money": c["repay_money"], "origin_choice": choice["label"],
            })
            stats["contracts_created"] += 1
            print(f"    → 契約発生: {cid} / {c['counterparty']} / 期限T{c['due_turn']}")
        if choice.get("settle"):
            append_event("contract_settled", {
                "id": tr["contract"]["id"], "turn": turn, "outcome": choice["settle"],
                "settled_by": choice["key"], "delta": {},  # 資源変化は state_applied 側に計上済み
            })
            stats["fulfilled" if choice["settle"] == "fulfilled" else "defaulted"] += 1
            print(f"    → 契約 {tr['contract']['id']} は "
                  f"{'履行' if choice['settle'] == 'fulfilled' else '不履行'}")

        # --- 4b. 特性の更新(2026-08-13追加。plan.md「特性の成長経路」) ----------
        # 資源と同じく、増減の判定・量はすべてコード側。LLMには関与させない
        # (LLMは題材の文章を書くだけで、その題材がどの特性に対応するかも知らない)。
        trait_delta, fired = compute_trait_step(
            state["traits"], talent, theme_trait, choice["key"], turn, tr["kind"])
        if trait_delta:
            append_event("trait_changed", {
                "turn": turn, "delta": trait_delta,
                "theme_trait": theme_trait, "choice_key": choice["key"],
                "fired": fired, "kind": tr["kind"],
            })
        # 実測用の集計(分母は「実際に選択が行われたターン」)
        stats["played_turns"] += 1
        if tr["kind"] == "normal" and theme_trait:
            stats["theme_turns"][theme_trait] = stats["theme_turns"].get(theme_trait, 0) + 1
        if theme_trait is None:
            stats["no_theme_turns"] += 1
        stats["choice_counts"][choice["key"]] = stats["choice_counts"].get(choice["key"], 0) + 1
        if fired:
            key = f"{fired['trait']}/{fired['via']}"
            stats["fire_counts"][key] = stats["fire_counts"].get(key, 0) + 1
            stats["fires"] += 1

        new_state = reduce_state()
        fire_str = (f"成長発火: {TRAIT_JA[fired['trait']]} via {fired['via']}"
                    + ("(才能補正あり)" if fired["talent"] else "")) if fired else "成長なし"
        print(f"    特性: {format_traits(new_state['traits'], talent)}  [{fire_str}]")

        # --- 5. ナレーション -----------------------------------------------------
        # ナレーションには数値ではなく「何が減って何が増えたか」の向きだけを渡す。
        # 数値をそのまま渡すと読み上げてしまい、向きを渡さないと無から回復を捏造する
        # (Phase 0 で観測された2つの破綻)。維持コスト(decay)も同じターンの出来事
        # として合算する。
        turn_delta = dict(decay)
        for k, v in choice["cost"].items():
            turn_delta[k] = turn_delta.get(k, 0) + v
        down_keys = [k for k, v in turn_delta.items() if v < 0]
        down = [RESOURCE_JA[k] for k in down_keys]
        up_keys = [k for k, v in turn_delta.items() if v > 0]
        up = [RESOURCE_JA[k] for k in up_keys]
        not_up_keys = [k for k in ALLOWED_RESOURCES if k not in up_keys]
        narration_prompt = (
            f"選んだ選択肢: {choice['label']}\n"
            f"この行動で減ったもの: {'、'.join(down) if down else 'なし'}\n"
            f"増えたもの: {'、'.join(up) if up else 'なし'}"
        )
        if choice.get("settle") == "fulfilled":
            narration_prompt += f"\n補足: {tr['contract']['counterparty']} への借りを返し終えた。"
        elif choice.get("settle") == "defaulted":
            narration_prompt += (f"\n補足: {tr['contract']['counterparty']} への借りを"
                                 f"返せなかった。相手の心証は悪い。")
        elif "contract" in choice:
            narration_prompt += (f"\n補足: {choice['contract']['counterparty']} に"
                                 f"借りができた。いずれ返さねばならない。")
        narration, n_latency = call_ollama(args.model, NARRATION_SYSTEM, narration_prompt,
                                           want_json=False)
        latencies.append(n_latency)
        append_event("llm_call", {"role": "narration", "model": args.model,
                                  "latency_sec": n_latency, "raw": narration})
        print(f"[{n_latency:.1f}秒] {narration.strip()}")

        # --- 6. 整合性担保役 ------------------------------------------------------
        # 2026-08-12修正: ナレーション**後**に、ナレーション文そのものを検査する形に
        # 変更(旧版はコストの数値をナレーション生成の前に見ていたため、実際に破綻が
        # 起きていた場所=ナレーション文を一度も見ておらず、構造上ずっと空回りしていた)。
        # さらに、否定制御テストでLLM版の検出率が低いと分かったため(33%)、
        # コード側の正規表現チェック(無料・瞬時)を先に走らせ、どちらかが引っかかれば
        # 矛盾とする。
        code_issues = code_check_narration(not_up_keys, narration)
        if code_issues:
            stats["code_check_hits"] += 1
            append_event("code_check_flag", {"turn": turn, "issues": code_issues})
            print(f"  [コード側チェック] 矛盾検出: {'; '.join(code_issues)}")
        if not args.no_consistency_check:
            flagged, reason, c_latency = run_consistency_check(
                args.model, down, up, narration, latencies)
            if flagged:
                stats["llm_check_hits"] += 1
            verdict_str = f"矛盾検出: {reason}" if flagged else "OK"
            print(f"  [整合性担保役(LLM), {c_latency:.1f}秒] {verdict_str}")
        print()

    # --- サマリ ---------------------------------------------------------------
    final = reduce_state()
    print("=== 計測サマリ ===")
    if latencies:
        print(f"LLM呼び出し回数: {len(latencies)} / 平均 {sum(latencies)/len(latencies):.1f}秒 "
              f"/ 最大 {max(latencies):.1f}秒 / 最小 {min(latencies):.1f}秒")
    print(f"JSON解析失敗: {parse_failures}件")
    print(f"最終資源: {format_resources(final['resources'])}")
    print(f"維持コスト累計: "
          + (", ".join(f"{k}{v:+d}" for k, v in stats['decay_total'].items()) or "なし")
          + " / 収入累計: "
          + (", ".join(f"{k}{v:+d}" for k, v in stats['income_total'].items()) or "なし"))
    print(f"資源不足で選べなかった選択肢: {stats['blocked_choices']}件")
    print(f"整合性の矛盾検出: コード側 {stats['code_check_hits']}件 / "
          f"LLM側 {stats['llm_check_hits']}件")
    print(f"契約: 発生{stats['contracts_created']}件 / 清算ターン{stats['settlement_turns']}回 "
          f"/ 履行{stats['fulfilled']}件 / 不履行{stats['defaulted']}件 "
          f"/ 未清算{len(open_contracts(final))}件")
    for c in final["contracts"].values():
        print(f"  - {c['id']} {c['counterparty']}「{c['description']}」"
              f"T{c['created_turn']}→期限T{c['due_turn']} : {c['status']}")
    # --- 特性の実測サマリ ------------------------------------------------------
    print(f"--- 特性(生まれ持ったスキル: {TRAIT_JA[talent]}、年齢 {age_at(final['turn']):.0f}) ---")
    print(f"  {format_traits(final['traits'], talent)}")
    played = stats["played_turns"]
    if played:
        print(f"  選択の内訳: "
              + ", ".join(f"{k}={v}({v/played:.0%})"
                          for k, v in sorted(stats["choice_counts"].items())))
        print(f"  題材のあったターン: {played - stats['no_theme_turns']}/{played} "
              f"(題材なし={stats['no_theme_turns']}: 清算ターン等)")
        print(f"  題材の特性別内訳: "
              + (", ".join(f"{TRAIT_JA[t]}={n}" for t, n in stats["theme_turns"].items())
                 or "なし"))
        print(f"  成長の発火: {stats['fires']}回 / {played}ターン "
              f"= **実測 f_total = {stats['fires']/played:.3f}**"
              f"(机上の仮定 4×0.167=0.667 に対応)")
        print(f"    1特性あたり f = {stats['fires']/played/len(TRAITS):.3f} "
              f"(机上の仮定 0.167)")
        print(f"  発火の内訳: "
              + (", ".join(f"{k}={v}" for k, v in sorted(stats["fire_counts"].items()))
                 or "なし"))
    rep = repetition_report(situations_this_run)
    print(f"状況の繰り返し度(文字bigram Jaccard, {rep['pairs']}ペア): "
          f"最大 {rep['max']:.2f} / 平均 {rep['mean']:.2f} / 0.5超のペア {rep['near_duplicates']}件")
    print(f"詳細ログ: {EVENTS_PATH}")


if __name__ == "__main__":
    main()
