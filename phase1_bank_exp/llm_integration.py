# -*- coding: utf-8 -*-
"""「LLM接続・プロンプト方針・ナレーション整合性検証」という責務全体を扱う。

Step 11(2026-08-15、behavior-preserving refactoring)でgame.pyから一括で
移動した(今回も関数1個ずつではなく責務全体の粒度):
- call_ollama(ollamaへのHTTP呼び出し)
- code_check_narration(正規表現ベースのコード側矛盾検出)
- run_negative_control(整合性担保役の否定制御テスト、--negative-control)
- システムプロンプト4種(SITUATION_SYSTEM・SETTLEMENT_SYSTEM・NARRATION_SYSTEM・
  CONSISTENCY_SYSTEM)
- コード側矛盾検出の検出設定(RESOURCE_SYNONYMS・POSITIVE_PATTERNS・
  NEGATION_MARKERS・CONTRADICTION_WINDOW・NEGATION_LOOKAHEAD)
- NEGATIVE_CONTROL_CASES

game.py側には、既存関数の薄い互換ラッパーと、既存定数名の再公開(単純な
モジュール属性の別名代入)を残す。scenario_generation.py・
interactive_runtime.pyは、game.py側のcall_ollama/code_check_narration
ラッパーを経由してこのモジュールを間接的に使う——両モジュールの責務・
既存の呼び出し方は一切変更していない。

このモジュールは game.py・event_store.py・projection.py・
scenario_generation.py・interactive_runtime.py のいずれにも依存しない。
urllib.error(例外クラスの参照のみ)とjsonは標準ライブラリとして使う。
urlopen/Request/monotonic/exit/stderrはDependencies経由で呼び出し時点の
関数オブジェクトを受け取る(urllib.request/time/sysを直接importしない)。
"""
import json
import urllib.error
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class LlmIntegrationDependencies:
    """call_ollama()・run_negative_control()が game.py 側の現在値(HTTP通信・
    時刻計測・終了処理・標準エラー出力・既存の共有関数)を参照するための、
    明示的な依存の受け渡し容器。WorldStateではない(制度スキーマの再設計では
    なく、LLM接続をgame.pyから独立させるための最小限の依存注入)。呼び出しの
    たびに現在値を読むため、CLIやテストによる差し替えがそのまま反映される
    (import時点で固定しない)。"""
    urlopen_fn: Callable[..., object]
    request_cls: Callable[..., object]
    monotonic_fn: Callable[[], float]
    exit_fn: Callable[[int], None]
    stderr: object
    # run_negative_control()専用。game.code_check_narration・game.
    # run_consistency_check(いずれもgame.py側の互換ラッパー)をそのまま
    # 受け取ることで、既存のmonkeypatchが引き続き効くようにする。
    code_check_narration_fn: Callable[[list, str], list]
    run_consistency_check_fn: Callable[..., tuple]


@dataclass(frozen=True)
class LlmIntegrationConfig:
    """call_ollama()・code_check_narration()・run_negative_control()が
    game.py 側の現在値(定数)を参照するための、明示的な設定値の受け渡し容器。
    ja_to_key・allowed_resourcesはgame.py側の他の定数(RESOURCE_JA・
    ALLOWED_RESOURCES、いずれもこのモジュールの対象外)から導出される値を
    呼び出し時点でそのまま受け取る。"""
    ollama_host: str
    keep_alive: str
    resource_synonyms: dict
    positive_patterns: list
    negation_markers: list
    contradiction_window: int
    negation_lookahead: int
    negative_control_cases: list
    ja_to_key: dict
    allowed_resources: set


SITUATION_SYSTEM = """あなたは人生シミュレーションゲームのストーリー生成役です。
プレイヤーの現在の資源状態を踏まえて、次の状況を短い日本語の文章(2〜4文)で描写して
ください。

続けて、プレイヤーが取れる行動を、以下の4つの対価タイプそれぞれについて1つずつ、
この状況に即した具体的な内容で提示してください(**資源の増減量・かかる時間は
あなたが決める必要はありません。コード側で決まります**。あなたはその行動が
具体的に何をすることかというラベルだけを書いてください):

1. money型: お金・費用を払って解決する行動
2. labor型: 自分の時間と労力を使って解決する行動
3. social型: 誰かに頼る・借りを作って解決する行動
4. rest型: 休んで心身を回復する行動(この状況を無理に解決せず、いったん休む)

social型については、**誰に頼るのか**(相手の呼び名)も答えてください。
**今回の相手の名前が指定されている場合は、必ずその名前をそのまま使ってください**
(過去の指定と勝手に変えないこと)。指定が無い場合のみ、状況に自然に登場する
人物として自由に名付けてください。

出力は必ず以下のJSON形式のみ。前後に説明文を付けないこと。

{
  "situation": "状況描写(日本語)",
  "choices": {
    "money": "money型の行動の具体的な説明",
    "labor": "labor型の行動の具体的な説明",
    "social": "social型の行動の具体的な説明",
    "rest": "rest型の行動の具体的な説明"
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
例1(矛盾あり): 減った資源=心の余裕 / 増えた資源=なし /
ナレーション文「心が軽くなった気がした。」
→ 減ったはずの心の余裕が「軽くなった」(良くなった)と書かれている。矛盾あり。

例2(矛盾なし): 減った資源=お金 / 増えた資源=なし /
ナレーション文「財布は軽くなったが、代わりに知識と経験を得られた。」
→ お金が減ったことと整合している。知識・経験は資源の増減表に無い言葉なので対象外。
矛盾なし。

# 出力形式(JSON、これ以外の文字列を出力しないこと)
{"contradiction": true または false, "reason": "矛盾がある場合はどの資源か(15字程度)、無ければ空文字"}
"""


# --- コード側の矛盾検出(2026-08-12追加、opus第3回レビュー反映) ---------------
# LLM版の整合性担保役は否定制御テストで検出率33%(2/3見逃し)にとどまった
# (下記 run_negative_control 参照)。「そもそも大半はコードで検出可能」という
# レビューの指摘どおり、正規表現ベースの軽量チェックを先に挟む。ollama呼び出しが
# 不要なので無料・瞬時。LLM版はこれと併用する(どちらかが引っかかれば矛盾とする)。
RESOURCE_SYNONYMS = {
    "money": ["お金", "金銭", "財布", "資金"],
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


def code_check_narration(not_up_keys: list, narration: str, *,
                         config: LlmIntegrationConfig) -> list:
    """「今回増えていない資源」(減った、または変化していない資源。キーのリスト)に
    ついて、ナレーション文中でその資源に触れつつ上向きの表現が近くに無いかを
    正規表現ベースで探す。減った資源だけでなく「変化していない資源」も対象にする
    (「何の対価も払わず秩序が回復した」というエントロピー違反パターンを拾うため。
    下記の否定制御テストcase3=「何もしていないのに満たされていく」がこれ)。
    上向き表現の直後に否定マーカーがある場合は除外する(「高まったというわけでは
    なかった」等、実際には矛盾していないケースの誤検出対策)。
    戻り値は矛盾の説明のリスト(空なら検出なし)。LLMを呼ばないので瞬時・無料。"""
    cfg = config
    issues = []
    for key in not_up_keys:
        for syn in cfg.resource_synonyms.get(key, []):
            idx = narration.find(syn)
            if idx < 0:
                continue
            win_start = max(0, idx - cfg.contradiction_window)
            window = narration[win_start: idx + len(syn) + cfg.contradiction_window]
            for pos in cfg.positive_patterns:
                pos_in_win = window.find(pos)
                if pos_in_win < 0:
                    continue
                pos_abs = win_start + pos_in_win
                lookahead = narration[pos_abs: pos_abs + len(pos) + cfg.negation_lookahead]
                if any(neg in lookahead for neg in cfg.negation_markers):
                    continue  # 直後に否定表現があるので矛盾とはみなさない
                issues.append(f"{key}(減ったはず): 「{syn}」の近くに「{pos}」")
                break
    return issues


# ==============================================================================
# ollama
# ==============================================================================

def call_ollama(model: str, system: str, user: str, want_json: bool, *,
                dependencies: LlmIntegrationDependencies,
                config: LlmIntegrationConfig) -> tuple:
    """ollamaにプロンプトを投げ、(応答テキスト, レイテンシ秒)を返す"""
    deps = dependencies
    cfg = config
    payload = {
        "model": model,
        "prompt": user,
        "system": system,
        "stream": False,
        "keep_alive": cfg.keep_alive,
    }
    if want_json:
        payload["format"] = "json"
    data = json.dumps(payload).encode("utf-8")
    req = deps.request_cls(
        f"{cfg.ollama_host}/api/generate", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    start = deps.monotonic_fn()
    try:
        with deps.urlopen_fn(req, timeout=180) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"[ollama接続エラー] {cfg.ollama_host} に到達できません: {e}", file=deps.stderr)
        deps.exit_fn(1)
    elapsed = deps.monotonic_fn() - start
    return body.get("response", ""), elapsed


# ==============================================================================
# 整合性担保役の否定制御テスト(negative control)
# ==============================================================================
# 2回目・3回目のopusレビューが共通して指摘した点: 「OK」が返り続けることは、
# チェックが機能している証拠にはならない(構造上OKしか返せない可能性がある)。
# 意図的に矛盾したケースを流し、NGを検出できることを実際に確認する。
NEGATIVE_CONTROL_CASES = [
    # (down, up, narration, 矛盾を検出すべきか)
    # 2026-08-14、プレイヤー個人trustの廃止に伴い、"人からの信用"を使っていた
    # 2ケースをお金・心の余裕に差し替えた(ALLOWED_RESOURCESに無い資源では
    # code_check_narrationがそもそも検出対象にできず、テストとして機能しない
    # ため)。
    (["お金"], [], "お財布がずっしり重くなった気がした。", True),
    (["時間の余裕", "体力"], [], "疲れは消え、すっかり元気を取り戻していた。", True),
    ([], [], "何もせずただ座っているだけで、心の底から満たされていくのを感じた。", True),
    (["お金"], [], "財布は軽くなったが、代わりに知識と経験を得られた。", False),
    (["体力"], [], "身体は疲れていたが、それでも前へ進む理由があった。", False),
    (["心の余裕"], [], "心に重くのしかかるものがあった。", False),
    ([], ["お金"], "思わぬ臨時収入に、財布の中身が少し温かくなった。", False),
]


def run_negative_control(model: str, *, dependencies: LlmIntegrationDependencies,
                         config: LlmIntegrationConfig) -> None:
    """コード版(正規表現)とLLM版、両方の検出率を同じケースで比較する。"""
    deps = dependencies
    cfg = config
    print(f"=== 整合性担保役 否定制御テスト (model={model}) ===")
    print(f"ケース数: {len(cfg.negative_control_cases)}\n")
    correct_code, correct_llm = 0, 0
    for i, (down, up, narration, should_flag) in enumerate(cfg.negative_control_cases, 1):
        up_keys = [cfg.ja_to_key[u] for u in up if u in cfg.ja_to_key]
        not_up_keys = [k for k in cfg.allowed_resources if k not in up_keys]
        code_issues = deps.code_check_narration_fn(not_up_keys, narration)
        code_flagged = bool(code_issues)
        code_hit = code_flagged == should_flag
        correct_code += code_hit

        flagged, reason, latency = deps.run_consistency_check_fn(
            model, down, up, narration, [], log=False)
        llm_hit = flagged == should_flag
        correct_llm += llm_hit

        print(f"case{i} 期待={'検出' if should_flag else '通過'}  narration: {narration}")
        print(f"  [code {'OK' if code_hit else '!!'}] "
              f"{('検出: ' + '; '.join(code_issues)) if code_flagged else '通過'}")
        print(f"  [llm  {'OK' if llm_hit else '!!'}] ({latency:.1f}秒) "
              f"{('検出: ' + reason) if flagged else '通過'}\n")
    n = len(cfg.negative_control_cases)
    print(f"=== 結果: code版 {correct_code}/{n} / llm版 {correct_llm}/{n} ===")
