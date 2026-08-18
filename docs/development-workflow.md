# 開発・PR品質ゲート

機能変更は意味のまとまりごとにブランチを切り、Pull Requestで完結させる。
default branch (`master`) 宛ての同一リポジトリPRは、以下をすべて通過すると
GitHub Actionsがreview済みheadをsquash mergeし、remote branchを削除する。

1. patchのwhitespaceと競合マーカー
2. 全PythonのAST、UTF-8、生成物・巨大ファイル混入
3. `institutions`の暗黙乱数と、分離済み層から`game/event_store/projection`への逆依存
4. Dashboard inline JavaScriptの構文
5. `game.py --help`
6. 制度レジーム別`--policy-check` baseline
7. Linux / Windowsの全unittest

draft、fork由来、default branch以外をbaseにした積み上げPR、または
`manual-merge` / `do-not-merge`ラベル付きPRは自動マージしない。
積み上げPRは親PRがmasterへ入った後、baseをmasterへ変更して品質ゲートを再実行する。

制度の意味を意図的に変更してpolicy集計が変わる場合は、数値を黙って許容せず、
`phase1_bank_exp/policy_check_baseline.json`と変更理由を同じPRに含める。
寿命・生存率はbaseline判定へ追加せず、観察値のままとする。

ローカルで同じ確認を行うコマンド:

```bash
python3 tools/automated_review.py
python3 tools/check_dashboard_javascript.py
python3 tools/check_policy_baseline.py
cd phase1_bank_exp
python3 -m unittest discover -v
```
