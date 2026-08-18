# 1シミュレーションrunの可視化ダッシュボード(設計書)

2026-08-16起案。実装ステータスは各節に追記する(このファイル自体が進捗メモを兼ねる)。

## Context

Step 13(物々交換・自給制度)完了後、ユーザーがパラメータ調整(`institutions/barter.py`等)を
進める中で、`--policy-check`のテキスト出力だけでは「社会の状態」が追いにくいという課題が出た。
見たいものとして: 制度Stage(銀行・通貨・地域信用・契約執行・物々交換)の推移、財・資源の残高推移、
NPCの信頼グラフ・契約の重み(金額)、NPC関係/イベントのログ、が挙がった。対象データは1本の
シミュレーション(1seed×1方針)を詳しく追う形、成果物はArtifact(HTMLダッシュボード)。

現状、`simulate_policy()`(`offline_simulation.py`)は`CHECKPOINT_TURNS`(60/200/420/960/
1440/1920の6点)でしかスナップショットを取っておらず、1920ターンを追うグラフには粒度が
粗すぎる。NPCのtrust推移や契約の履行/不履行イベントも、現状は最終状態(`npcs`辞書)としてしか
残らず、いつ・いくらの契約がどうなったかのタイムラインが無い。この計測を最小限追加してから、
その結果をHTMLダッシュボードとして描画する。

## 設計

### A. `offline_simulation.py`: `simulate_policy()`へオプトイン形式のtrace収集を追加 — ✅実装済み

`trace: dict = None`というキーワード専用引数を追加した。既存の全呼び出し(--policy-check・
--policy-probe・既存テストすべて)は`trace`を渡さないため、戻り値の辞書の形・RNG消費順序・
実行速度のいずれも変化しない(trace=None時は追加コストゼロ)。

`trace`が辞書として渡された場合、以下のイベントstreamを埋める:

1. `trace["turns"]`: 1ターンごとのスナップショット。既存のcheckpoint記録
   (`trajectory[turn]`)と同じフィールド構成に`"turn"`・`"phase": "turn_end"`を加えたもの。
   二重実装を避けるため、
   スナップショット辞書の組み立てを`_snapshot()`という内側のクロージャへ切り出し、
   checkpoint記録・trace記録の両方から呼ぶ。traceは所得・清算・通常行動・成長を反映した
   ターン終了時に記録し、開始時の不足ペナルティで死亡した場合だけその死亡状態を記録して終了する。
2. `trace["settlements"]`: 契約清算1件ごとに`{turn, counterparty, is_bank_debt, choice_key,
   settle, repay_money, enforcement_stage}`。
3. `trace["npc_introductions"]`: 新規NPC登場ごとに`{turn, name, initial_trust}`。
4. `trace["npc_events"]`: 名前付きNPCの自然死。年齢・原因・死亡で孤児化した契約IDを含む。
5. `trace["character_events"]`: 焦点人物の死亡と後継世代への切替。
6. `trace["population_events"]`: 集落人口の出生・背景死亡・Stage遷移・絶滅。

既存の`agree_log`(各意思決定点で3方針それぞれが何を選んだかの記録)はそのまま「選択タイムライン」
として再利用する——新規の計測は追加しない。

実測確認済み: `trace`ありでもseed=1/cautious/60turnsのbank_trust・currency_confidenceが
既存golden値(`SimulatePolicyFixtureTest`)と完全一致——RNG・数値へ一切影響しないことを確認。
既存662件のテストは無変更で成功。

### B. 新しいランナー関数 `run_visualize_trace()`(`offline_simulation.py`) — ✅実装済み

`run_policy_check`/`run_policy_probe`と同じ並びに追加。引数: `seed, policy_name, turns,
safety_floor, talent, output_path, *, dependencies`。処理:
1. `trace = {"turns": [], "settlements": [], "npc_introductions": [], ...}`を用意し、
   `simulate_policy(..., trace=trace)`を1回呼ぶ(`deps.simulate_policy_fn`経由の
   monkeypatch伝播も維持)。
2. 実行結果から: 方針・seed・才能・死亡ターン・最終資源/特性/制度Stage・
   `institution_trajectories`(5制度のtransition_history)・`npcs`最終状態・`agree_log`・
   `contracts`最終状態・人口/NPC/交易イベントを含むtrace、をまとめたJSONを`output_path`へ書き出す
   (`json.dump(..., ensure_ascii=False, indent=2)`)。戻り値は書き出したdictそのもの。

`PolicySimulationDependencies`への新規フィールド追加は不要だった(既存の`simulate_policy_fn`を
そのまま使う)。

### C. `game.py`: 薄いラッパー + CLI — ✅実装済み

- `run_visualize_trace(seed, policy, turns, safety_floor, talent, output_path)`——他の
  `run_policy_check`等と同じ形の薄い互換ラッパー。
- 新しいargparseフラグ: `--visualize-run`(store_true)、`--visualize-output`
  (デフォルト`visualize_trace.json`)。既存の`--seed`/`--policy`/`--turns`/`--safety-floor`/
  `--talent`をそのまま再利用する(`--policy`が`"none"`のままなら`"cautious"`にフォールバック)。
  `main()`内、`--policy-check`等と同じ並びに`if args.visualize_run: ...; return`を追加。

### D. テスト追加(`test_characterization.py`) — ✅実装済み(8件追加、既存670件全成功)

- `trace=None`(既定)なら戻り値・RNG状態が既存(trace引数を渡さない)呼び出しと完全一致
  ——短いturnsで`state_before/after`のRNGハッシュ比較。
- `trace={...}`を渡すと、短い決定的な設定(seed固定・少ないturns)で`turns`の件数が
  「到達した最終ターン数」と一致し、各エントリが期待フィールドを持つこと。
- `settlements`/`npc_introductions`が期待どおりのタイミング・フィールドで記録されること。
- `run_visualize_trace()`が有効なJSONファイルを書き出し、トップレベルキー
  (`policy`/`seed`/`talent`/`death_turn`/`trace`等)を持つこと(turnsは高速化のため小さめ、
  例: 30〜60)。
- 既存テスト(662件)を無変更で成功。

### E. 可視化ダッシュボード(初回、Artifact公開版) — ✅完了(F で配信方式を変更)

seed=1・cautious方針・1920ターン設定(死亡turn=1455で打ち切り)のデータを生成し、
制度Stage・スカラー値・資源・財・特性・NPC/契約・選択タイムラインの7セクション構成で
HTMLダッシュボードを作成した:
   - ヘッダ: 方針・seed・才能・生死/寿命・各制度の最終Stage
   - 制度Stage推移(5本のステップチャート、bank/currency/local_credit/enforcement/barter)
   - 制度スカラー値推移(bank_trust/currency_confidence/community_trust/
     enforcement_capacity/production_capacity)
   - 資源推移(energy/money/peace)
   - 財推移(food/medicine/shelter/tools)
   - 特性推移(dexterity/intellect/skill/health)
   - NPC・契約: NPC一覧(trust・役割・履行/不履行件数)+ 清算イベントのタイムライン
     (`repay_money`の大きさ・履行/不履行で色分け)
   - 選択タイムライン(`agree_log`から実際に選ばれたarchetypeを時系列で可視化、
     1920点そのままだと密すぎるため一定区間でビン集計)

初回はArtifactツールでclaude.aiへ公開したが、**ユーザーの指示によりこの配信経路は停止した**
(下記F節参照)。データをHTMLへインライン埋め込みする設計・チャート構成自体は変更していない。

方針間比較(3方針を同じseedで重ねる)は今回のスコープ外(ユーザーが単一runを選択したため)——
同じtrace機構を使えば後で自然に拡張できる。

### F. ローカル配信への切り替え・再現可能なビルド — ✅完了

2026-08-16、ユーザー指示: 「この公開はストップしてローカルで配信する形にしたい」。
Artifact(claude.ai)公開は以後行わない。加えて、E節の生成手順が**このセッションの
scratchpad(`~/.claude/...`配下、セッション終了で消える一時領域)にしか存在しない**という
問題があった——ユーザー自身が再生成する手段がプロジェクト内に無かった。これを解消し、
`phase1_bank_exp/dashboard/`配下に再現可能なビルド一式を置いた:

```
dashboard/
  template.html        # __DATA_JSON__ プレースホルダ入りのHTML本体(チャート描画コード込み)
  build_dashboard.py   # visualize_trace.json → 軽量化 → template.htmlへ埋め込み → life_ledger.html
  life_ledger.html     # ビルド済み成果物(build_dashboard.pyの出力、gitではなくファイルとして存在)
```

更新経路は2コマンドに固定した:
```
python game.py --visualize-run --seed <N> --policy <cautious|ambitious|family>
python dashboard/build_dashboard.py
```
`build_dashboard.py`は`../visualize_trace.json`を読み、以下を行う(E節で作った軽量化ロジック
——数値の丸め・resources/traitsのフラット化・agree_logの区間ビン集計・NPC集計——をそのまま
移植したもの、二重実装はしていない):
1. JSONを読み込み、ダッシュボード用に整形・軽量化(浮動小数を3桁へ丸め、`agree_log`を
   既定60区間へビン集計、NPCごとの履行/不履行件数を`settlements`から算出、等)。
2. `template.html`の`__DATA_JSON__`を埋め込みJSONへ置換し、`life_ledger.html`として書き出す。
3. 出力サイズを表示する(1920ターン相当で約800KB、外部通信は一切発生しない自己完結ファイル)。

`life_ledger.html`はダブルクリックでそのまま開ける(データが埋め込み済みなのでローカル
HTTPサーバーは不要)。LAN越しに見たい場合だけ`python -m http.server`等で配信すればよい。

### G. 可視化境界の補強とローカル実験コンソール — ✅完了

2026-08-16、静的な閲覧に加えてフォームからパラメータを変え、同じダッシュボードを
再生成できるローカル実験コンソールを追加した。

```
python dashboard/server.py
# http://127.0.0.1:8765/
```

- `server.py`は`127.0.0.1`だけで待ち受け、任意コマンド・任意出力パスを受け取らない。
- 1runごとに`experiment_worker.py`を別プロセスで起動する。フォームから変更した定数は
  worker終了と同時に消え、次のrun・通常CLI・policy-checkへ漏れない。単一スレッド実行のため
  グローバルRNGも並列干渉しない。
- `experiment_parameters.py`が公開項目・型・範囲・Stage境界間の順序をallowlist検証する。
  現時点ではrun条件と、調整中の物々交換・自給制度だけを公開。全制度の定数を無差別に
  公開しない。
- 設定JSONの保存・読込、既定値への復帰、生成HTMLの保存に対応。
- 静的な`build_dashboard.py`経路は残し、フォーム版も同じ`build_dashboard_data()`・
  `template.html`を使うため、描画実装を二重化しない。

同時に、trace schema version=1の検証、契約執行Stage4の動的な縦軸、空の清算・選択run、
`--bins`範囲、JSONのscript終了タグ対策、NPC名のHTML escape、DOCTYPE/charset/viewport、
明示した`--turns 12`が1920へ置換されるCLI曖昧性を修正した。

### H. 人口・世代を持つ記録世界 — ✅オフライン水槽へ実装

2026-08-17、可視化runだけは`simulate_policy(continue_world=True)`を使うようにした。
通常のpolicy-checkと一人生涯モデルは既定Falseのままなので、既存baselineは変わらない。

- `settlements`は人口制度の集落別状態マップ。初期実装は`home`1集落だが、単一人口値には
  していない。
- `trace["turns"]`へpopulation / reproductive_population / population_stage /
  generation / character_age / character_alive / world_extinctを追加。
- `trace["population_events"]`へ出生・背景死亡・人口Stage・局所/世界絶滅を追加。
- `trace["character_events"]`へ個人死亡と次世代への焦点移動を追加。個人死亡時のopen契約は
  defaultedではなくorphanedとなり、信用ペナルティを発生させない。
- HTMLは世界の経過年を横軸にし、人口チャート、人口Stage、世代交代を表示する。

schema versionは1のまま、全フィールドを後方互換な追加項目としている。旧traceは`.get()`と
フィールド存在判定で従来どおり表示できる。この静的run経路自体は引き続き記録世界の再生に使う。

### I. 永続保存・増分実行・実時間時計 — ✅デジタル水槽へ実装

2026-08-17、静的runを置き換えず、その上に永続水槽を追加した。

- `simulate_policy(..., continue_world=True)`は`resume_state`を返す。乱数状態、制度、財、
  人口、契約、NPC、方針計測、世代を含み、JSON往復後に続きを計算できる。
- 世代交代をまたぐ1720か月について、一括実行と900+820か月の分割実行が、最終結果・
  RNG状態・turn/清算/NPC/人物/人口の全traceで完全一致するテストを持つ。
- `dashboard/aquarium_worker.py`は区間ごとに新規プロセスで同じparameter setを適用し、
  checkpointから増分計算する。`aquarium_runtime.py`はworld JSONと最新HTMLをatomic replaceで
  保存し、一時停止・再開・手動進行と、閲覧者に依存しない実時間時計を管理する。
- `/api/aquarium/status|start|pause|resume|advance`は既存の同一オリジン制約下に置く。
  観察HTMLは更新時に現在月を開き、通常の単発runは従来どおり先頭を開く。
- 詳細trace・反実仮想log・終了契約と、画面へ渡す疎遠NPCは既定2400か月の観察窓へ
  制限する。NPC自体は疎遠後も現行の価格modifierの抽選母集団に残るため、再開checkpoint
  からは削らない。累積件数とID通し番号も残す。実際に終了契約と表示用NPCが削減される
  300か月時点の圧縮checkpointと非圧縮checkpointから同じ30か月を進め、将来trace・
  主要状態・RNGが一致するテストで境界を固定している。
- NASではアプリ本体をread-onlyのまま維持し、`./state:/state`だけを書込可能な永続volumeとする。
- 名前付きNPCは集落人口から抽出した観察対象として、世界RNGを消費しない決定論的な年齢・
  寿命を持つ。自然死したNPCは関係候補から外れ、未清算契約は信用判断外の`orphaned`へ移る。
  集落の背景死亡には既にその人物が含まれるため、人口をもう一度減らす二重計上はしない。
- 新規世界は3集落を持ち、人口・必需財・生産力・物々交換Stage・共同体健康を集落別に保存する。
  生活圧力差による移住は人口・再生産人口の総数を保存し、`population_migrated`へ記録する。
  局所絶滅後も他集落が残れば焦点を移し、集落別人口線・移住・局所絶滅を観察画面へ表示する。
  旧schema 1の単一集落checkpointは新しい住民を追加せず、1集落のままローカル状態だけ補う。
- 集落ネットワークschema 2では地域信用も集落別台帳へ移した。携行可能な食料・医薬品・
  道具だけを信用Stageに応じて融通し、住居・生産力は移さない。`trade_events`へ送受集落・
  財・量・信用容量を記録し、checkpointには累計交易量/件数と集落別送受量を保存する。
  交易前後の世界総量保存、孤立時停止、JSONを挟む分割再開との完全一致をテストする。

### J. ピクセル水槽と名前付き生業台帳 — ✅初期実装

2026-08-17、縦長の統計画面を既定表示にせず、世界を抽象化した水槽面を追加した。

- `institutions/residents.py`の台帳schema 3は世帯ごとの主生業を持つ。さらに
  `institutions/activity_economy.py`が、barter upkeepの4財gross背景生産を
  生産年齢人口・名前付き標本・匿名cohort・永続siteへ毎月保存配賦する。
  必需財部門の必要労働力は共同体人口の30%とし、実働は生産年齢人口との小さい方とする。
  v2では必要総数を基礎消費・摩耗量で4財へ整数配賦し、人手不足時は現在不足している財を
  優先する。財別実働人数の合計を増やさず、各財の充足率だけ対応するgross背景生産へ掛ける。
  余剰の生産年齢人口は他活動に残る。
  財の増減はbarter upkeepが正本で、活動台帳は追加生産を起こさない。
- 2026-08-18、住民・世帯台帳をschema 4へ上げ、4財の累積活動件数を分類して保持する。
  活動共同体は別に0〜100の財別生産経験を持つ。当月実働で翌月経験を学習し、無稼働時は忘却、
  生産倍率は最大1.2とする。分裂時は同じ水準を継承し、合流時は人口加重平均するため、財在庫の
  加算保存と強度値の継承を混同しない。trace/APIは世界人口加重値と共同体別値の双方を公開する。
- 2026-08-19、住民・世帯台帳をschema 5へ上げ、世帯の移住回数・直前共同体・最終移住月・
  理由を保持する。前月末の世帯不足が3か月以上続き、行先で10%以上の改善が見込める場合、
  既存の移住人数枠内でその世帯を優先する。`residents_migrated`は長期不足による人数と世帯IDを
  持ち、同じ個人pixelの補間移動と世帯表の履歴へ投影する。
- 同日、空間分割に依存しない財会計へ補正した。4財は加算可能な物量、
  `provisioning_scale`は加算可能な生活基盤規模である。上限・背景生産はこのscale、
  不足基準・基礎消費は人口・年齢cohort由来の財別需要scaleへ比例する。分裂・合流前後で
  物量、充足率、世界総フローを保存する。
  `production_capacity`はscale加重の強度、財別生産経験は人口加重の強度として別々に扱う。
  trace/APIは生の物量に加えて世界・共同体別の供給scale、需要scale、`goods_coverage`を公開する。
- schema 1/2の台帳は住民ID・氏名・世帯・累計値を保ったままschema 3へ昇格する。
  台帳を初期化し直し、水槽で観察中の人物が入れ替わる移行は行わない。
- 水槽canvasは暗い粉体画面で、住民一人ずつを1px座標へ投影する。同じ整数pixelへ
  複数人が重なった場合は全員の生業色を一走査で平均混色し、人数の対数に応じて明度を
  上げる。全住民は省略せず、色と明度で構成・密度を残す。色は世帯の主生業、明るさは当月の
  担い手である。選択枠は別レイヤーなので住民本体は1pxから変化しない。
- 2026-08-18、大人口向けに粉体rasterizerをstructure-of-arraysへ変更した。
  `Uint32Array`の人数、`Float64Array`のRGB和、`Uint8Array`の活動flag、
  `Int32Array`のpixel別住民linked listで
  `O(住民数 + 占有pixel数)`に集約する。永続座標は月単位の正史に固定したまま、名前付き住民は
  当月の`activity_economy_state`に配賦された担い手だけが住まいと活動場所の間を往復する。
  他の住民は住居側の個人アンカーへ留まり、活動点の移動・開拓で住居の点群を一緒に動かさない。
  新規世界の最大4096人は個体として動的層へ置くが、移動episodeを持たない人へ架空の通勤を
  合成しない。それを超える旧traceでは等間隔の4096人だけを動的層へ置く。匿名人口は
  最新世界ではsite別の匿名worker配賦、旧traceでは`population_weight`を使い、offscreen Canvasの密度面へ全員分を混色し、
  その上へ最大384粒の代表流動を重ねる。個体解像では代表流動の代わりに、画面内の実匿名粒子から
  最大4096人を静的面から取り出してhome↔activity運動させる。したがって各frameの運動量は
  総人口でなく遠景`4096 + 384`、近景`4096 + 4096`を上限とする。同一点の動的な色も
  固定密度のRGB和と合成し、クリック時だけlinked listを走査する。
  scene初回構築を除いた継続frame時間のEMAが14/28/70/180msを超えた場合は、人口・色・粒子を
  間引かず更新周期だけを33→50→100→250→500msへ変更する。負荷が下がれば逆方向へ
  自動復帰し、reduced-motion環境では250ms周期とする。
  永続住民は`DATA.residents`のobjectをコピーせず、`spatial_state.residents`も追加の
  ID Map/座標Mapへ複製せず直接参照する。これによりscene構築時の大規模な一時object生成を避ける。
  最新月はさらに`dashboard/particle_packet.py`で住民row index・site index・uint16座標・
  activity・flags・activity mode・出発/帰宅位相を17 byte/人のSoAへ量子化し、base64で
  埋め込む。同じ座標を
  `spatial_state.residents`のobjectとして二重送信せず、JS側はTypedArrayから直接rasterizeする。
  10万人の合成fixtureでは座標部分が14.43MiB→1.78MiB、scene構築が約50ms・追加heap約0.9MiB。
  packetの無い旧trace、観察中の過去月は既存keyframe経路を維持する。
  新規buildの過去履歴は`dashboard/spatial_history.py`が完全keyframe列をID別upsert/removeへ
  可逆変換する。JSは要求された前後frameだけを復元し、完全state cacheは4件のLRUに制限する。
  1920か月fixtureでは2.37MiB→0.48MiBで、T960/T1920の人数・位置復元を実行確認済み。
  `dashboard/event_density.py`は出生・死亡・移住・生業を月/共同体/生業別へ集約し、
  人口1万人超では重複する個人observer eventだけを初期HTMLから外す。流量の合計と残高、
  不足対応回数は保持し、出生/死亡のpulse・匿名移住flowとして水槽へ投影する。
  名前付きの`resident_born`は記録済み`parent_ids`だけを使い、親粒子から子粒子への移動光と
  子の誕生波紋を出生月に重ねる。匿名出生へ架空の親IDは割り当てず共同体pulseのままとし、
  個別の世代flowは1か月最大128件へ決定的に制限する。
  `dashboard/particle_cohorts.py`は全観察月の数値人口からその月の名前付き生存人口を引いた差を
  匿名cohort履歴として送る。出生・死亡・移住を増分適用し、同じ構成が続く月はrun-length frameを
  再利用する。最新月だけは名前付きpacket人口と厳密に再照合する。ブラウザは二分探索で再生月を
  選び、一人ずつのID/座標/linked listを作らず、全景では画面画素数以下の
  決定論的セルへ人数とRGB和を一括加算する。拡大時は活動場所ごとの人口パッチとviewportの
  交差人口を固定格子で推定し、描画slotを画面内人口へ再配分する。したがって十分な倍率では
  匿名人口も1人/pxへほどける一方、反復数は常に`min(画面内匿名人口, 利用可能画素数)`で
  頭打ちになる。そのため100万人の合成fixtureでも人口総数と混色を正確に保存する。匿名pixelの
  tooltipは共同体・生業・重なり人数を表示し、個人詳細がないことを個人IDで偽装しない。
  Nodeを使える環境では25万の名前付き粒子と100万の匿名粒子を通す回帰benchmarkで、粒子保存・
  色合成・有界セル数を検証する。Python側でも100万人×1920か月の不変人口が1 frameへ圧縮される。
  Python住民台帳も4096人を上限とする名前付き観察標本と、活動共同体別の匿名人口層へ
  cohort階層化した。出生・死亡・移住は匿名層を定数時間で更新し、空間の代表活動点へ
  `population_weight`として配るため、数値人口・集落人口・活動共同体人口・描画人口が一致する。
  100万人の正規世界は名前付き4096人のまま開始・再開でき、初期総人口は実験コンソールから
  1〜1000万人で指定できる。既定250人は全員名前付きの従来状態と完全一致する。
- 永続空間には1〜32倍の意味的ズームを持つ表示カメラを追加した。ホイール操作はカーソル下の世界座標を不変に
  保って倍率を変え、ドラッグは正規化世界座標上の中心を移す。既定倍率1では従来のpixel座標と
  完全一致する。匿名人口は全景では全員を人数加重集約し、拡大時には活動場所の空間パッチから
  viewport内人口を再計算する。最大32倍では密度が十分なら1人/pxまで分離し、HUDは世界総人口・
  画面内人口・匿名1px最大人数を別に表示する。画面外の名前付き/匿名粒子は端へclampせずcullする。
  活動点・共同体重心・交易/移住flow・cluster event・親子線も同じ
  camera変換を参照するので、遠景の混色密度から近景の個体へ連続的に移れる。
  cameraはsandboxed iframeから親`app.html`へ`postMessage`し、自動更新後に復元する。単発runや
  新規水槽の開始時だけ全景へ戻す。3個体の4倍zoomで中央1個体だけが残るfixtureと、100万匿名
  人口を複数の重み付き活動場所へ正確に配り、4倍で画面内人口だけに絞り、32倍で最大1人/pxへ
  分解するfixtureで、座標anchor・culling・人口会計・描画量上限を固定した。個体解像に達した
  匿名点は、各活動場所の人口を世界座標上の暗黙的な楕円格子へ整数配分する。隣接viewportへ
  パンしたテストでは共通点のkey/座標が完全一致し、カメラ移動による全点再抽選を防いでいる。
  個体解像時の最大4096点はこの格子の実点を等間隔抽出し、静的rasterから除外してから
  住まい―活動場所間を表示専用位相で往復する。動的点をクリックしても匿名cohort・生業・
  活動場所へ到達でき、静的点との色合成と人口保存を同じfixtureで確認する。
- live個体詳細は`GET /api/aquarium/resident?id=...`でcheckpointから1件だけ読む。`allow-scripts`
  のみのiframeから直接fetchせず、親`app.html`が取得してpostMessageで返す。選択前の遠景描画と
  名前・親・世帯などの詳細読込を分離し、将来のresident catalog省略に備える。
- 世帯ごとの活動場所を先に作り、新しい活動場所は既存活動場所の近傍へ決定論的に
  付着させる。当月の実活動者だけが住まいと活動場所の間を連続的に往復する。制度・会計上の
  settlement所属は活動場所どうしの結びつきと交易端点にだけ利用し、集落矩形・境界・
  整列グリッドは描かない。近接する活動点の連結成分を表示上の密度塊として数えるため、
  「集落があるから活動する」のではなく「活動が集まるから集落に見える」関係になる。
- `institutions/spatial.py`は活動場所・住まい・住民位置を画面pixelではない0..1の
  正規化座標でcheckpointへ保存する。初期核数と座標は活動世帯数・世帯ID・world seedから
  決まり、settlement IDやゲーム本体RNGを使わない。出生・死亡・世帯分割・移住後に月1回
  同期し、近接活動点の連結成分を`clusters`として導出する。前月との活動点の重なりが
  最大の成分へ同じIDを継承し、分裂した子には親ID付きの新IDを与え、合流時は主系譜へ
  吸収する。現役・終了を含む`cluster_lineages`と、`cluster_formed` / `cluster_split` /
  `cluster_merged` / `cluster_dissolved`を`cluster_events`へ保存するため、集落は単月の
  見た目ではなく時間を持つ空間状態になった。観察窓の住民台帳圧縮時は閉鎖済み空間
  レコードも同じ境界で圧縮する。開拓・前集落境界と住居/活動episode分離に加え、月次の
  活動経済を活動点へ配賦する正本はstate v6である。v6はsite別のworker総数だけでなく
  財別実働人数も保存する。v1〜v5を読んだ場合は通し番号・最終開拓月・会計境界・安全な
  住居episode・活動労働者数・財別生産量・財別実働人数を安全な既定値で補い、
  保存世界をリセットせず昇格する。
- 月次生業を実際に担った世帯は、stable hashだけで既存密度の外へ活動点を伸ばせる。
  一つの孤立点を直ちに制度上の集落へせず、同じ母共同体から来た活動点が3点かつ人口8人へ
  達するまでは`provisional`な前集落として物理空間だけに置く。人口・財・信用は
  `accounting_parent_id`の母共同体へ保存され、閾値到達時だけ新しい活動共同体会計へ昇格する。
  `pioneering_started` / `pioneering_settled` / `pioneering_community_formed`は水槽上で1pxの
  流れ・疎な十字点・波紋として見える。開拓の選択・移動・昇格は本体RNGを消費しない。
- 最新月は`spatial_state`を正本として描画し、過去月は12か月ごとの軽量な
  `spatial_keyframes`間を補間する。空間履歴を持たない旧traceだけ、同じ住民IDから作る
  従来の表示専用座標へフォールバックする。2026-08-18、
  `institutions/activity_communities.py`の保存則付き台帳を観察投影から数値会計境界へ昇格した。
  前月accountの人口・財・生産能力・信用・交易累計・人口carryを、住民が実際に属する
  活動クラスタへ配賦し、直後に`settlements`と住民・世帯所属へ反映する。したがって次月の
  出生・交易・移住は固定地名ではなくcluster lineage IDを持つ活動共同体を主体に進む。
  加算可能値は世界合計を保存し、信用と健康は人口加重平均、Stageと不足は構成元の最悪値を
  採る。分裂時は人口比配賦、合流時は加算し、無人の土地在庫は消去せず
  `unassigned_accounts`へ人口0のreserveとして残す。台帳・checkpoint・月次trace・水槽UIは
  同じ共同体IDを参照する。
- keyframe v7は、v6までのクラスタ継続ID・成立月・親ID・構成活動点・`provisional`・
  `accounting_parent_id`、活動点ごとの最終活動月、総/名前付き/匿名労働者数、財別生産量に加え、
  財別実働人数を保持する。表示側は旧v1〜v6の短い活動点・クラスタ行も安全な既定値で後方互換に読み、
  住民行にはactivity modeと出発/帰宅位相を追加する。水槽は当月に起きた
  集落変化を1pxの点で作る波紋として重ね、住民tooltipから現在の活動クラスタIDを参照できる。
  既存クラスタ間を1世帯が移るだけの境界変化はsplit+mergeへ二重計上せず、真に新しい
  lineageが生まれた場合だけsplit、古いlineageが終了した場合だけmergeとする。
- 同月の`intersettlement_trade`は活動密度の重心間を流れる財の1px粒子、
  `residents_migrated`は該当住民自身の移動としてアニメーションする。観察月を過去へ
  戻した場合は移住イベントを逆にたどって当時の所属を復元する。住民を選んだときだけ、
  同月に生存する親との線を別レイヤーで表示する。出生・死亡・親ID・世代・後継者選択は
  同じ住民台帳/observer traceにあり、時間再生で点群の世代交代を追える。
- 水槽面はiframeの全viewportを使い、親コンソールは左の設定drawer、全画面APIは右下の操作として
  重ねる。親が1秒ごとにサーバーの`next_tick_at`をpostMessageし、月内の朝/日中/帰宅/夜位相を
  次の月次更新へ同期する。上部メニューから「水槽/世界/住民/統計/履歴」を切り替え、自動更新時は
  スクロール位置・カメラ・選択中の面と住民を親iframeが保持する。世界タブは独立会計へ昇格済みの
  活動共同体だけでなく、空間上に先に生まれた`provisional`前集落も二重計上しない別行で表示する。
  既存の統計グラフは削除しない。

現在の生業記録は背景生産の「担い手」展開であり、個人財布、個人別在庫、行動選択、
住民間関係の力学まではまだ持たない。それらは数値世界側の次の境界とする。

### K. 100論理フレームのストリーミングバッファ — 第3縦断完了

リアルタイム水槽は、動画プレイヤーと同様に約100フレームをring bufferへ保持する。
第1縦断では`dashboard/aquarium_stream.py`と`GET /api/aquarium/frames`を追加し、月次更新ごとの
iframe/HTML全体再読込を廃止した。最新keyframeと連番付き論理frameをatomicな
`aquarium_stream.json`へ保存し、同じcanvasへ継続適用する。

ここで配信する「フレーム」は完成bitmapではなく、1px水槽を再構成できる意味フレームとする:

```text
frame_chunk = {
  stream_id, base_revision, first_sequence, fps, server_time, tick_seconds,
  keyframe?, keyframe_delta?,
  frames: [{sequence, turn, month_phase, signal_mask}]
}
```

- clientは連番を検証し、受信済みframeを最大300件まで保持する。初回の1chunk=100frameで
  再生を開始し、low watermark 30で次chunkを要求する。欠番・revision不一致・長時間の
  background停止時は次のkeyframeから再同期する。
- 再生headは受信headより一定時間遅らせ、短いNAS処理遅延やLAN jitterをbufferで吸収する。
  buffer不足時だけ緩やかに減速し、追いつくために社会時間を飛ばさない。
- 100枚のRGBA画像や全人口の絶対座標を毎回送らない。住居アンカー、当月のactivity episode、
  出発/帰宅位相、交易・移住・出生pulseは一度送れば中間位置を決定論的に展開できる。
  名前付き最大4096人はTypedArrayのdelta、匿名人口は保存則付き密度patch/代表流動として送る。
- client workerで100論理フレームを先に展開し、main threadは現在フレームをcanvasへ合成するだけにする。
  ズーム/パン時は同じ世界座標frameを再投影し、サーバーに画面解像度別動画を要求しない。
- 一時停止・過去再生はlive ringとは分離し、既存の月次`spatial_history`を使う。liveへ戻る際は
  最新keyframeを取得し直す。
- transportは後からSSE/WebSocket/HTTP streamingを選べるようframe schemaを先に固定する。
  現在はcursor付きHTTP差分取得を標準とする。WebSocketは計算能力やbuffer不足を解決しないため、
  数秒以下の更新、多数の同時閲覧者、双方向操作の即時反映が必要になるまで導入しない。
  一方向pushだけが必要ならSSEを先に検討し、HTML丸ごとの差し替えは行わない。
- 受信queueと描画clockは分離する。第3縦断で、親の`next_tick_at`から得た位相を100frame全体へ
  直接掛ける方式を廃止し、iframe内の独立playout clockが確定frameを順に消費するようにした。
  1論理月は`tick_seconds`で再生し、catch-upで5か月が一度に確定したchunkなら5周期を使って
  T3→T4→T5の順に流す。NAS計算が設定周期を超えて親時計が既に位相1でも、受信直後に末尾へ
  飛ばない。軽いjitterはbufferで吸収し、枯渇時は最後の確定frameを保持する。未再生の社会時間を
  飛ばして追いつく動作は既定にしない。

第1縦断の実装境界:

- 1 revisionにつき100個の`sequence/turn/month_phase`を配信し、clientは連番とrevisionを検証する。
  目標100、low 30、high 150は実装値で、残量は水槽HUDと親コンソールに表示する。
- logical frame v2は、1回の更新で複数月が確定した場合、その月列を100frameへ均等配分する。
  各月は位相0→1を持ち、再生headがT3→T4→T5のように順番に観察状態を進める。未来の月を
  予測生成せず、100か月を超える大きな手動catch-upでは観察窓の直近100か月を再生する。
- 各frameはイベント本文でなく`signal_mask`だけを持ち、活動・交易・移住・出生・組織変化・
  空間系譜・人口変化を示す。イベント本体と空間差分履歴はkeyframeに一度だけ載せるため、
  大量イベントを100枚へ複製しない。v1の`sequence/turn/month_phase`だけのframeも読み続ける。
- keyframeは現在値と直近120か月の追い付きtailだけを持つ。生産年齢・組織・空間差分履歴を含む
  現行1920か月fixtureでは約1.58MB、100frame配列自体は約7.8KBである。完成bitmap・RGBA frameは送らない。
- 通常の月次更新ではiframeを再読込せず、住民選択・カメラ・表示タブ・canvasの残像を保持する。
  新世界開始、旧serverとの混在fallback、明示的な再同期だけがHTML読込境界になる。
- serverの計算中も、atomicに確定済みのstatus/keyframe/住民詳細を更新lock無しで読める。
- revision差分の第2縦断も完了した。初回・別stream・古いcursorには完全keyframe、直前revisionには
  schema付き`keyframe_delta`を返す。deltaはturn upsert、NPC/住民/世帯の追加・削除・フィールドpatch・
  順序、イベントtail、空間履歴patch、変更された現在値だけを持つ。1920か月世界の1か月更新は
  約276KB(完全keyframeの17.5%)で、serverの参照適用結果が次keyframeと完全一致した。
  clientはrevisionを適用前に検証し、不整合ならcursorを捨てて完全keyframeから再同期する。破損した
  `aquarium_stream.json`は再生成可能cacheとして捨て、数値世界の更新を止めない。今後さらに必要なら
  `organization_state`/`spatial_state`を内部patch化し、展開をWeb Workerへ移す。HTTP cursor契約と
  100frame再生queueはそのまま使える。
- 第3縦断では、連続するrevisionのframeを未再生queueへ追記し、完全keyframe/別worldのときだけ
  queueを置き換える。pause/resumeは数値worldやkeyframeを変えない時計操作なのでrevisionを進めず、
  statusだけが対応streamの無い世代を先行表示する不整合も解消した。停止中に読み込んだ画面は最新の
  確定frameで固定し、buffer残量は末尾を未再生1枚として数えず0になる。
- 第4縦断では当初、最初の100frameを保持したまま次の100frameを待ち、受信headを常に1更新先へ
  置く方式を採った。しかしserverも1 revisionごとに100frameしか生成しないため、起動直後と
  枯渇復旧時にもう1 revisionを余計に待つ契約となり、「初回だけ動き、その後は更新時に画面が飛ぶ」
  症状を生んだ。現在は1chunkで開始し、先読みを完全に使い切った状態が2秒続いたときだけ末尾frameで
  停止、次の1chunkで直ちに再開する。未来deltaは数値DATAへ先に統合してもobserver turnを再生headへ
  固定するため、社会時間を飛ばさない。
- server更新ではHTML全体を毎月書かず、streamとworldだけをatomic更新する。信頼済み親子worker間は
  pickle protocol 5、外部APIと永続stateはJSONのままとし、8MiB超のworld全体deepcopyも廃止した。
  各更新はsimulation/compact/dashboard/summary/worker境界の時間をstatusへ記録し、精度・RNG・会計を
  変えずに次のボトルネックを実測できる。1秒ごとのstatus pollは直近commit時に作った小さな公開
  snapshotを返し、数MiB以上へ成長するcheckpoint JSONを毎回読み直さない。server再起動後の初回だけ
  永続worldから復元する。NASではread-onlyのアプリ配置を変えず、`PYTHONPYCACHEPREFIX=/tmp/pycache`で
  bytecodeだけを揮発性tmpfsへ置き、隔離workerごとに同じソースを再解析する固定費も削る。
- 生成能力の安全弁として、serverは通常の1更新についてworker計算からstream/checkpointの
  atomic commit完了までを含むpipeline時間を、worker/commit/orchestrationへ分解して記録する。
  複数tickのcatch-upは固定費を月数で割ると過小評価になるため標本にせず、
  古い短周期を安全下限へ引き上げる最初の実行もcatch-upせず1更新だけで計測する。
  大きい方の1.5倍を切り上げた値を`minimum_tick_seconds`としてstatusへ返し、未計測時は30秒を
  保守値にする。直近値は世界revisionと組にした小さな`aquarium_performance.json`へ保存し、
  計測値だけを更新するために巨大checkpointを再保存しない。UIはそれ未満を選択不能にし、APIも同じ境界で拒否する。既存時計が実測下限を
  下回った場合は次の更新時に自動で引き上げる。これによりring bufferは短いjitterを滑らかにする
  責務に留まり、恒常的な計算不足を未再生月の蓄積で隠さない。

現行のstate v6(固定住居アンカー＋activity episode＋財別活動経済配賦)、particle packet v2、親から渡す停止状態と
`tick_seconds`はこのbuffer方式の入力境界であり、使い捨ての表示ハックにはしない。

### L. 年齢cohortと活動由来組織の空間表示 — 第5縦断完了

人口の数値正本は子ども・生産年齢・高齢者の3整数cohortを持ち、各活動共同体の
`productive_population`を月次frameへ載せる。これは名前付き住民の表示標本から逆算せず、
100万人規模でも集落数に比例する固定長会計である。世界タブは総人口と生産年齢人口を並べ、
水槽HUDにも世界合計を表示する。

活動点の密度と制度状態から成立した会社・ギルド・集会所・宗教共同体・家族工房は、
集落を先に塗る境界ではなく、実際の活動場所の重心へ1pxパターンとして重ねる。会社の信用Stage、
生産年齢構成員数、規模、形態履歴は`organization_state`に保存され、100論理frameのkeyframe契約にも
含まれる。第2縦断では、前月末の組織が生産年齢構成員・capacity・信用に応じて翌月の財・地域信用・
共同体健康へ限定的な協調余剰を加える。第3縦断では、物的資産と区別した0〜12か月の運営余力と
`stable`/`strained`/`dormant`を持たせ、余力低下で余剰が縮小し、休眠時は停止する。
status別件数と運営余力合計は月次snapshotへ載せる。適用量は`organization_effects`と組織台帳の累計へ記録するが、
描画は引き続き数値正本を変更しない投影である。終了済み組織と長い履歴はlive観察窓で圧縮し、
checkpointの正本は中断再開後もone-shot実行と一致させる。

第4縦断では、各組織をその活動点に属する世帯・住民へ接続した。続く活動経済v2接続では、
15〜64歳の所属可能な生産年齢構成員と、siteの財別台帳に記録された当月実働を分離した。
名前付き実働者は住民の`last_activity`と一致するIDだけを追跡し、残りは匿名実働人数として保持する。
4096人を超える匿名人口を個体化せず、100万人規模でも全住民オブジェクトを要求しない。
生業組織のcapacityは共同体の当月必需財workerに占める当該組織の実働比で決まり、所属だけでは
協調余剰を出さない。月次trace・live summaryは所属可能人数、総実働、名前付き実働、人員継続率を保持し、住民詳細APIからは現在の
所属組織・関係種別・労働者かどうかを取得できる。人員と活動点の継続率は翌月の組織運営余力へ影響するが、
Canvas表示の住民粒子や人口正本を変更しない。

第5縦断では、実現した協調余剰の一部を共同体在庫の内数である`asset_claims`として組織へ留保する。
claimを世界の財へ再加算せず、共同体別在庫を上限に縮小し、分裂・合流では活動点系譜に沿って保存する。
現在claim・取得累計・共同体分配累計は月次snapshot/live summaryへ、組織別claimは住民詳細APIの所属組織へ
載せる。これは描画専用の属性ではなく最大15%の組織効果補正を持つ数値状態だが、Canvas上の組織記号を
独立した財粒子として増やすものではない。

## 変更対象ファイル

- `offline_simulation.py`(`simulate_policy()`のtrace引数・`_snapshot`ヘルパー・
  `run_visualize_trace()`新設)——✅完了
- `game.py`(`run_visualize_trace`ラッパー・argparseフラグ2つ・`main()`分岐)——✅完了
- `test_characterization.py`(新規テストクラス、8件)——✅完了
- `dashboard/template.html` / `dashboard/build_dashboard.py` / `dashboard/life_ledger.html`
  ——✅完了(F節)。`life_ledger.html`はビルド成果物なので`build_dashboard.py`実行のたびに
  上書きされる
- `dashboard/app.html` / `dashboard/server.py` / `dashboard/experiment_worker.py` /
  `dashboard/experiment_parameters.py` ——✅完了(G節)
- `institutions/population.py` / `test_population.py` ——✅完了(H節)
- `dashboard/aquarium_worker.py` / `dashboard/aquarium_runtime.py` /
  `test_world_resume.py` / `test_aquarium.py` ——✅完了(I節)
- `dashboard/event_density.py` / `dashboard/particle_cohorts.py` /
  `test_event_density.py` / `test_particle_cohorts.py` ——✅表示の大人口境界まで完了(J節)
- `institutions/residents.py` / `institutions/spatial.py` /
  `institutions/activity_communities.py` ——✅数値側の匿名cohort・代表活動点境界まで完了(J節)

## 検証方法

- `python -m py_compile` 全対象 + `python -m unittest`(件数は実行時の最新値を完了報告に記録)
- `python game.py --help` exit 0
- `python game.py --policy-check` exit 1(既存挙動が変わっていないことの確認)
- `python game.py --visualize-run --seed 1 --policy cautious` を実際に実行しJSON生成を確認
- `python dashboard/build_dashboard.py`を実行し、`life_ledger.html`が正しいサイズ・
  内容で再生成されることを確認
- 生成したHTMLを実際にブラウザで開き、ピクセル水槽の集落・個人粒度切替と
  上部メニュー、従来グラフがデータと整合しているか目視確認
- localhostの実HTTP経路で、水槽開始・最新HTML取得・自動進行・一時停止・12か月手動進行・
  再開を確認
