# NAS配備

Life Gameの実験コンソールをSynology NAS上の独立コンテナとして動かす。

- LAN URL: `http://192.168.0.22:8016/`
- 外部公開なし。Cloudflare Tunnelの対象にしない
- 実験runはLLMを呼ばないため、PC/Ollamaに非依存
- Python標準ライブラリのみ。既存運用と同じ`python:3.12-alpine`を使う
- 非root・read-only・capability全削除・`no-new-privileges`
- DSMカーネルとの互換性のためCPU CFS/PIDs cgroup制限は使わない
  (実験run自体は実質単一コア処理)
- 単発実験と水槽更新は子プロセスで隔離し、水槽更新同士はlockで直列化する
- 永続水槽は`./state:/state`へcheckpointと最新HTMLを原子的に保存する
- 配備時は旧コンテナを停止し、実行コード・compose・checkpointを
  `./backups/YYYYmmdd-HHMMSS/`へ退避してから更新する
- 空間state v1〜v3は初回更新時にv4へ自動移行する。保存世界の再初期化は不要

## 配置形式

NAS上に次の形を保つ。Composeの`./app:/app:ro`はこの相対配置を
前提にしている。

```text
/volume1/docker/life-game/
  docker-compose.yml
  state/                 # UID/GID 65534が書込可能な永続水槽領域
  app/
    *.py
    institutions/
    dashboard/
      reports/
        barter_sweep_*.html
```

`events.jsonl`、`visualize_trace.json`、`__pycache__`、テスト結果は実行に不要。
単発実験は従来どおり非永続だが、デジタル水槽だけは`state/`へ保存する。
`state/aquarium_stream.json`は最新keyframeと100論理フレームのatomic snapshotで、
保存worldから再生成可能だが、配備時はcheckpoint/HTMLと一緒に退避する。
`state/aquarium_performance.json`はworker開始からworld commitまでの直近実測と
安全な更新周期を持つ小さなsidecarで、巨大なcheckpointの二重保存を避ける。
重い一括走査はPCで実行し、`dashboard/reports/`に生成されたHTMLだけをNASの
`/volume1/docker/life-game/app/dashboard/reports/`へ転送する。既存の`./app:/app:ro`
マウント内なので追加volumeは不要。LANでは`/sweeps`から参照できる。

```sh
mkdir -p /volume1/docker/life-game/app/dashboard/reports
mkdir -p /volume1/docker/life-game/state
sudo chown 65534:65534 /volume1/docker/life-game/state
sudo chmod 700 /volume1/docker/life-game/state
```

## 反映時の手順

Windows側にSSH秘密鍵と`%USERPROFILE%\.ssh\config`の`Host nas`設定がある場合、
PowerShellから次のスクリプトでアプリ更新・HTML転送・再起動をまとめて行える。

```powershell
Set-Location "C:\ClaudeProjects\ai-lab\life-game\phase1_bank_exp"
powershell -ExecutionPolicy Bypass -File ".\deploy\nas\deploy-sweep-reports.ps1"
```

Codex等の内部PTYから実行すると`sudo`の入力欄がユーザー画面に出ないため、
その場合は公開鍵だけで可能なステージングと、手元端末での配置を分ける。

```powershell
powershell -ExecutionPolicy Bypass -File ".\deploy\nas\deploy-sweep-reports.ps1" -StageOnly
ssh -t nas "sh '.life-game-deploy/install-staged.sh'"
```

2行目はユーザー自身の見えるPowerShellで実行するため、NASの`sudo`パスワード
プロンプトへ通常どおり入力できる。パスワードをCodexやログへ渡さない。

スクリプトは配置後、最大45秒間コンテナhealthを確認する。表示される
`Pre-deploy runtime/checkpoint backup:`のパスは、次の配備が正常に動くことを
確認するまで控えておく。バックアップは自動削除しない。

転送後の`sudo`入力を中断した場合、ファイルはNAS側のステージ領域に残る。
再転送せず配置と再起動だけを行うには次を実行する。

```powershell
powershell -ExecutionPolicy Bypass -File ".\deploy\nas\deploy-sweep-reports.ps1" -InstallOnly
```

SSH別名を使わずIPを直接指定する場合:

```powershell
powershell -ExecutionPolicy Bypass -File ".\deploy\nas\deploy-sweep-reports.ps1" `
  -SshTarget "NASユーザー名@192.168.0.22" `
  -IdentityFile "$env:USERPROFILE\.ssh\id_ed25519"
```

公開鍵の指定がSSH configの`Host nas`にだけ設定されている場合は、既定の
`-SshTarget nas`を使う。IP指定時はWindows OpenSSHが対応する鍵を選べるよう、
configに同じHost/IdentityFile設定を用意しておく。

NAS管理リポジトリを原本とする既存ルールに従い、実際の転送・起動は
明示的なdeploy段階で行う。NAS上では概ね次を実行する。

```sh
cd /volume1/docker/life-game
sudo /usr/local/bin/docker compose config
sudo /usr/local/bin/docker compose up -d
sudo /usr/local/bin/docker compose ps
```

確認:

```sh
curl -fsS http://127.0.0.1:8016/api/schema >/dev/null
```

その後、LAN内の別端末から`http://192.168.0.22:8016/`を開き、
フォームから単発runを再生成するか、「デジタル水槽」で永続世界を開始する。
水槽はブラウザを閉じてもコンテナ内の時計で進み、コンテナ再起動後も`state/`の
checkpointから同じ乱数列・制度・財・人口・契約・世代を継続する。

旧版へ戻す場合はコンテナを停止し、表示されたバックアップディレクトリの
`docker-compose.yml`、`app/`、`aquarium_world.json`、`aquarium.html`、
`aquarium_stream.json`、`aquarium_performance.json`をそれぞれ
元の場所へ戻してから`docker compose up -d`する。新コードでv4へ更新する前の
checkpointが退避されるため、空間stateを旧コードへ逆変換する必要はない。

## 初回実測(2026-08-16)

DS918+上で`cautious / seed=1 / health / 1920ターン`を2回実行し、
`19.32秒 / 19.19秒`。応答HTMLは約1.08MB。トップ画面の取得は約3ms。
表示自体は軽く、待ち時間はオフラインシミュレーション中に限られる。

## 大人口水槽の見込み(2026-08-18)

現行world workerを手元PCで120か月実測すると、250人は3.17秒/最大RSS約29MiB、
100万人は27.99秒/約92MiB、1000万人は28.00秒/約92MiBだった。4096人を超える
人口は匿名cohortなので、100万から1000万へ増やしても計算量・メモリはほぼ増えない。
T120の100万人worldを1か月だけ再開した場合は1.68秒だった。描画・pixel混色は閲覧端末の
ブラウザ側で行い、NASは月次計算・checkpoint・HTML生成だけを担当する。

DS918+では単コア性能差を見込み、水槽の未計測時安全下限を`30秒ごとに1か月`とする。
1更新後は直近worker時間と指数移動平均の大きい方へ50%の余裕を加え、UI/APIの選択下限と
稼働時計を自動調整する。初期値は開始12か月・履歴2400か月を推奨する。
シミュレーション本体は実質単一コアなので、DSM全体のCPU使用率が15〜25%でも停止ではない。
重いparameter sweepは引き続きPC/GPUで実行し、NASへはHTML reportだけを配る。

## 撤去

```sh
cd /volume1/docker/life-game
sudo /usr/local/bin/docker compose down
```

永続volumeやDBは無い。配置フォルダの削除は別操作とし、必要な場合だけ
ユーザー確認後に行う。
