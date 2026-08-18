# デジタル水槽 アーキテクチャレビュー

2026-08-18時点の全体構造と、次のモデル改善境界を記録する。

## 結論

現在の水槽は単なる可視化ではない。人口、世帯、活動場所、住民座標、活動点の
近接クラスタ、クラスタ系譜、活動共同体会計、組織はcheckpointへ保存され、月次の
数値世界へ戻る正本になっている。「集落があるから活動点を置く」のではなく、活動点の
集合から共同体を導出し、十分に育ったクラスタだけを会計主体へ昇格する境界も成立している。

2026-08-18に、この不足を埋める`activity_economy_state` v1を追加し、同日v2で財別労働配賦へ
進めた。4財のgross背景生産を
共同体の生産年齢人口へ整数配賦し、最大4096人の名前付き標本と残りの匿名cohort、永続活動場所へ
同じ台帳から割り当てる。財の増減は従来のbarter会計だけが行い、活動台帳は二重生産しない。
空間上の通勤、開拓候補、組織の人員継続は、この台帳が更新した同じ住民・世帯・site実績を参照する。
siteは財別実働人数も保存し、生業組織は「所属可能な生産年齢構成員」と「当月実働」を分ける。
組織capacityと翌月の協調余剰は後者だけから計算されるため、看板と所属だけでは生産効果を出さない。

## 現在の正本境界

| 層 | 正本 | 責務 |
|---|---|---|
| 世界制度 | `bank_stage`等 | 銀行・通貨・契約執行の世界共有状態 |
| 活動共同体会計 | `settlements[*]` | 人口、年齢cohort、4財、生産能力、財別生産経験、地域信用、物々交換Stage |
| 人口台帳 | `resident_registry` | 最大4096人の名前付き標本、世帯、残りの匿名人口 |
| 世帯需要台帳 | `household_needs_state` | 年齢cohort由来の財別需要、名前付き世帯への観察配賦、匿名需要 |
| 世帯財台帳 | `household_goods_state` | 共同体4財の内訳として、名前付き世帯・匿名人口・組織・共用分の保有、移住、相続を保存 |
| 活動経済 | `activity_economy_state` | 4財のgross出力、名前付き/匿名worker、site配賦、累計出力 |
| 永続空間 | `spatial_state` v6 | 住居、活動場所、個人アンカー、site別・財別実働、クラスタ系譜 |
| 共同体昇格 | `activity_community_ledger` | 空間クラスタへ人口・財・信用を保存配賦し、次月の会計IDにする |
| 組織 | `organization_state` v3 | 会社・ギルド等の信用、所属可能人口、当月実働、担当財経験、運営余力、既存在庫内の資産claim |
| 観察投影 | dashboard keyframe/particle packet | 正本を1px粒子、密度、統計へ変換する再生成可能データ |
| live輸送cache | `aquarium_stream.json` | keyframe/deltaと100意味frame。数値世界の正本ではない |

重要な保存則は次のとおり。

- 世界人口 = 名前付き生存者 + 匿名人口。
- 集落需要 = 名前付き世帯需要 + 匿名cohort需要。世界需要は出生・死亡でのみ増減し、移住では保存する。
- 共同体の物理在庫 = 名前付き世帯保有 + 匿名人口保有 + 組織claim + 共用分。世帯財は
  共同体在庫の内訳であり、生産・消費・移住・相続で二重計上しない。
- 活動共同体への分裂・合流で人口、年齢cohort、財、carry、交易累計を保存する。
- 財の消費・不足基準は人口需要、在庫上限・背景生産は場所の生活基盤規模へ分離する。
- 財別生産経験は外延量ではない。分裂では水準を複製継承し、合流では人口加重平均する。
- 組織のasset claimは共同体在庫の内数であり、財を追加生成しない。
- 生業組織の実働合計は共同体の活動経済worker合計を越えず、同じ名前付き住民を複数組織の
  当月実働へ重複計上しない。
- 空間座標と活動点はゲーム本体RNGを消費しない。
- particle packetやstreamが壊れてもcheckpointから再生成できる。

## 配信方式

現在はcursor付きHTTP差分取得を維持する。1 revisionは100意味frameを生成し、clientは
最初の100frameで再生を開始する。低水位30で次chunkを要求し、実際の枯渇が2秒続いた場合だけ
末尾frameで停止し、次の100frameで再開する。

WebSocketは計算速度やbuffer不足を改善しない。数秒以下の更新、多数の同時閲覧者、または
双方向操作の即時反映が必要になるまでは追加しない。一方向pushだけが必要ならSSEを先に検討する。
輸送を交換しても100frame queueとCanvas描画は変更しない。

## 現在の主要な構造リスク

1. `offline_simulation.simulate_policy()`が世界更新の多数の境界を一つの大関数で接着している。
   新しい数値制度を安全に追加するには、月次世界更新を段階別の明示的なpipelineへ移す必要がある。
2. 必需財労働はv2で財別の必要人数・実働人数・充足率まで分離し、siteと組織効果まで接続した。
   ただし必要人数の総計は
   共同体人口の30%、財別比率は現在の基礎消費・摩耗量という当面の集約パラメータである。
   世帯需要v1は年齢cohortから正本化し、世帯固有の在庫と共同利用分も共同体在庫の内訳として
   保存した。ただし年齢係数は初期仮値で、技能、設備、労働時間はまだ明示していない。
   次は係数の実測較正と技能・生産性を接続する必要がある。
3. 匿名人口の活動量とsite配賦は正本になったが、匿名層内部の技能・世帯差はまだ集約値である。
4. 世帯別の必要量・在庫・移住・相続までは正本化したが、各世帯が自分の不足・関係・将来見通しから
   行動を選ぶ意思決定と、住民間関係グラフは未実装。
5. 数値checkpointは将来の力学に必要な意味状態を保持するため、無期限運用では意味アーカイブが必要。

## 実装済み: 活動経済台帳 v2

### 目的

既存の背景生産と基礎消費の総量を変えず、その生産がどの共同体・生業・活動場所・
人口層によって担われたかを数値状態として保存する。表示用の架空の動きではなく、
開拓、組織、人員継続率が同じ活動実績を参照できるようにする。

### 状態

`activity_economy_state`をcheckpointへ追加する。

```text
activity_economy_state = {
  version,
  updated_turn,
  communities: {
    community_id: {
      activities: {
        food|medicine|shelter|tools: {
          gross_output,
          named_worker_ids,
          named_household_ids,
          anonymous_worker_count,
          site_allocations: [{site_id, gross_output, worker_count,
                              named_worker_count, anonymous_worker_count}],
          unlocated_gross_output
        }
      },
      productive_population, required_worker_count, active_worker_count,
      required_worker_count_by_good, active_worker_count_by_good,
      provisioning_scale,
      background_production_labor_factor_by_good,
      unassigned_productive_population, background_production_labor_factor
    }
  },
  cumulative_output_by_good
}
```

毎月の詳細だけを正本へ残し、長期履歴は既存の観察窓内traceへ集約する。

### 入力と保存則

- `turn_engine.plan_barter_upkeep()`は従来のnet deltaに加え、基礎消費と背景生産の
  gross内訳を追加で返す。従来キーと計算順序は変えない。
- 共同体人口の30%を必需財部門の当面の必要労働力とし、`active_worker_count =
  min(required_worker_count, productive_population)`とする。この総数を基礎消費・摩耗量の比で
  財別必要人数へ保存配賦し、人手不足時は現在在庫の不足度を優先重みとして財別実働人数を
  上限付きで配る。必要人数・実働人数とも財別合計が総数と一致する。
- 財別の`active / required`だけ対応するgross生産へ掛ける。生産年齢人口0なら全財のgrossも0、
  小規模共同体で必要人数が4業種へ満たない場合は維持できない財が明示的に生じる。必要人数を超える
  生産年齢人口は`unassigned_productive_population`として他活動に残し、匿名層を個体objectへ展開しない。
- food/medicine/shelter/toolsは共同体間で加算できる物量で、別フィールドの
  `provisioning_scale`が「基準共同体何個分の生活基盤か」を表す。在庫上限と背景生産は
  provisioning scale、基礎消費・摩耗と不足基準は人口・年齢cohort由来の財別demand scaleへ
  比例させる。したがって共同体の分裂・合流だけでは
  充足率、世界総消費、世界総生産を変えない。表示用の財状態は生の物量に加えて
  `goods_coverage = quantity / scaled_reference`を併記する。
- `production_capacity`は強度値なので加算せず、分裂時は継承、合流時は
  `provisioning_scale`加重平均とする。これにより`scale × capacity`で表す総生産ポテンシャルを
  保存する。`production_practice_by_good`は人に宿る経験なので、こちらは人口加重平均する。
- 名前付き15〜64歳住民は対応する専門世帯と活動場所へ決定論的に割り当てる。
- 残りは`anonymous_worker_count`とsite別整数配賦として保持する。
- site別`gross_output`と`unlocated_gross_output`の合計は共同体のgross生産量と一致する。
- 労働不足によるgross縮小だけを財の正本へ反映し、活動台帳で同じ生産を二重計上しない。
- 当月grossは月初の財別経験倍率（1.0〜1.2）をさらに掛ける。当月実働による学習はgross確定後に
  更新して翌月から使い、無稼働なら忘却する。個人・世帯の財別件数は履歴、共同体経験だけが因果状態である。

### 世界pipelineでの位置

1. 4財の基礎消費とgross背景生産を計画する。
2. 前月組織の効果を適用する。
3. gross背景生産を活動経済台帳へ配賦する。
4. 名前付き住民・世帯へ当月activity episodeを反映し、翌月用の共同体経験を更新する。
5. 交易、人口変化、移住を確定する。
6. 永続空間を同期し、活動クラスタと共同体会計を更新する。
7. 組織を更新する。

### 検証済み条件

- 財の増減式とゲーム本体RNGは活動配賦から独立している。
- 1720か月の一括実行とJSON保存を挟む分割再開で、結果・全trace・RNGが完全一致する。
- 4財すべてでsite配賦+未定位出力がgross出力と一致する。
- 名前付きworkerと匿名workerの合計が各活動のworker数と一致する。
- 100万人fixtureでも名前付き上限を超える個体objectを作らない。
- 住民候補は月1回だけ共同体別に索引化し、処理量を
  `O(名前付き住民 + 活動場所 + 共同体数)`に保つ。
- 空間上の通勤・開拓・組織member continuityが同じ月次実績を参照する。

## その次

世帯需要と世帯財台帳として、個人財布を100万人へ持たせず、名前付き世帯と匿名cohortへ需要と
共同体在庫の内訳を保存配賦する境界まで実装した。次は世帯自身の不足・生業・保有財に基づく
自律的な対応を、名前付き世帯と匿名cohortの双方へ集約可能な形で接続し、その結果を活動場所・
交換・信用へ戻す。その後、住民間関係グラフと年齢需要係数、技能・生産性を較正する。WebGL、
タイルLOD、SSE/WebSocketは、モデル上の意味状態や実測負荷が必要性を示してから導入する。
