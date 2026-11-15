# tech-researcher

技術情報を集めて、日本語の日次・週次レポートを GitHub Issue に公開するツールです。仕様は [docs/spec/requirements.md](docs/spec/requirements.md) から読めます。

## 開発の準備

[uv](https://docs.astral.sh/uv/) を使います。Python 3.14 は uv が用意します。

```sh
uv sync
```

## last30days-skill の取得

収集に使う [last30days-skill](https://github.com/mvanhorn/last30days-skill) は、`third_party/last30days/` に Git Submodule として固定の SHA で取り込みます。リポジトリにあるのは参照だけです。clone の後に次を実行すると、収集スクリプト（`skills/last30days/scripts/`）と `LICENSE` だけが置かれます。

```sh
bin/setup-third-party.sh
```

- clone に `--recurse-submodules` を付けないでください。last30days-skill の全ファイル（AI への指示ファイルを含む）が置かれます
- `git pull` などで参照の SHA が変わったときも、`bin/setup-third-party.sh` をもう一度実行します（`git pull` は submodule を更新しません）
- 指示ファイル（`SKILL.md`・`AGENTS.md`・`CLAUDE.md` など）や対象外のファイルが残っていると、スクリプトは失敗します
- worker のイメージには、`third_party/last30days/` の `LICENSE`（MIT の著作権表示と許諾表示）と `skills/last30days/scripts/` を、同じ配置のままコピーします（`bin/setup-third-party.sh` で置いた作業ツリーをそのままコピーすれば足ります）。アダプタ（`src/tech_researcher/collect/last30days.py`）は、そのディレクトリを受け取って `skills/last30days/scripts/last30days.py` を呼びます
- 固定する SHA を変えたときは、契約テストを実際の CLI でも流します（`uv run pytest -m network tests/contract`。GitHub Search は収集用トークンが要るので、HN だけを確かめます）
- `third_party/` の中は編集しません。固定する SHA を変えるときは、`git -C third_party/last30days fetch`・`git -C third_party/last30days checkout <SHA>`・`git add third_party/last30days` の順に実行して参照を更新し、`bin/setup-third-party.sh` を流してからコミットします。スクリプトは参照（index の SHA）に合わせて取り直すので、`git add` の前に流すと元の SHA に戻ります

## worker コンテナ

収集などのコードの段階は worker コンテナで動きます（`docker/worker.Dockerfile`・`compose.yaml`）。ビルドの前に `bin/setup-third-party.sh` を実行してください（イメージに last30days-skill をコピーするため）。

```sh
export TR_DATA_DIR=/srv/tech-researcher   # データ用フォルダ。リポジトリの外の絶対パスで、先に作っておく
export TR_UID=$(id -u) TR_GID=$(id -g)    # データ用フォルダの持ち主（省略時は 1000）。0（root）にしない
docker compose build worker
```

- `config/` は読み取り専用で、`${TR_DATA_DIR}` はコンテナの `/data` にマウントします。ほかの場所（ルートのファイルシステム）は読み取り専用で、書けるのは `/data` と `/tmp` だけです
- root では動かさず、権限（capabilities）をすべて外します。Docker のソケットは渡しません
- 段階は統括スクリプトが1つずつ起動します。実行 ID と実行のフォルダは統括スクリプトが用意します。手で試すときは、ドライランのフォルダを作ってから呼びます。対象日は実行 ID の日付から決まります

```sh
run_id="dry-$(TZ=Asia/Tokyo date +%Y%m%d-%H%M%S)-0000"
mkdir -p "$TR_DATA_DIR/dry-run/$run_id"
docker compose run --rm -T worker collect daily --run-id "$run_id"
```

標準出力には件数と失敗した情報源の ID だけが出ます。日次は ② の入力 `agent-input.json` も書きます。エラーのときに出るログのパスは、コンテナの中のパス（`/data/…`）です。収集結果は `$TR_DATA_DIR/dry-run/$run_id/collected.json`、ログは同じフォルダの `log.jsonl` にあります。

## 実行（bin/tr-run）

cron と手動実行は、同じ入口 `bin/tr-run` を使います。ホストに uv と Docker（Compose）が要ります。上の `TR_DATA_DIR` を設定してから実行します。`TR_UID`・`TR_GID` を省略すると、`bin/tr-run` を動かしている人の ID を使います（root では動かしません）。

```sh
bin/tr-run daily --dry-run   # 日次のドライラン。weekly で週次
```

- 引数は `daily`・`weekly` と `--dry-run` だけです。対象日（週）は実行した時点から決まり、過去の期間は指定できません
- 同じ種類の実行が動いていれば、何もせずに終了コード 3 で終わります（ロックは `$TR_DATA_DIR/locks/` の `daily`・`weekly` と、それぞれのドライラン用で別）
- ジョブ全体と段階ごとの上限時間は、設定ファイルの `[limits.daily]`・`[limits.weekly]` です。超えたら止め、その実行のコンテナを消します
- compose には `PATH`・`HOME`・`DOCKER_HOST`・`TR_DATA_DIR`・`TR_UID`・`TR_GID` と、専用の `DOCKER_CONFIG` だけを渡します。リポジトリの `.env` は読みません
- `DOCKER_CONFIG` は `${XDG_CONFIG_HOME:-$HOME/.config}/tech-researcher/docker`（権限 700）です。`~/.docker/config.json` の `proxies` などはコンテナに入りません。このため Docker のコンテキスト（`DOCKER_CONTEXT`・`docker context use`）は使えず、デーモンは `DOCKER_HOST` で指定します。compose のプラグインは、`~/.docker/cli-plugins` ではなく、システムの場所（`/usr/libexec/docker/cli-plugins` など）に入れておきます
- 標準出力には、実行 ID・種類・対象日・結果の種別・件数・取得できなかった情報源の名前・ドライランの出力先だけを JSON 1行で出します。ドライランでは同じものを出力のフォルダの `summary.json` に残します
- 日次は ① 収集 → ② 評価・要約 → ③ 検証・検査（`verify`）とつなぎ、ドライランでは ③ の結果（`validation.json`・`secret-scan.json`・作るはずの Issue の `issue.md`）を出力のフォルダに残します。ただし ② の AI の呼び出し（agent コンテナ）はまだないので、いまは日次の実行（ドライランを含む）が ② で実行エラー（`not_implemented`）になります。収集結果が0件の日は AI を呼ばずに ③ に進むので、ドライランは「該当なし」の本文を残して終わります。すべての情報源の取得に失敗した日は ① で実行エラー（`all_sources_failed`）、有効な情報源がないときは `no_sources` になります。不合格の日の本文と秘匿情報の検査の結果は、まだ残しません（#38 で足します）。⑤ 投稿もまだないので、本番は ③ まで進んだ日（いまは0件の日、② をつないだ後はすべての日）も ③ の後に実行エラー（`post`／`not_implemented`）で終わります
- ① は、本番では DB（`$TR_DATA_DIR/db/tech-researcher.sqlite3`）を開き、runs の開始・取得結果（source_fetches）・収集項目（items。日次だけ）・Star 数と人気型フィードの記録（日次だけ）を書きます。① で実行エラーになった本番の実行（すべての情報源の取得に失敗した日など）は runs を `failed` で閉じます。② 以降の終わりは、それぞれの失敗の分岐と ⑤ で書くので、それまで本番の runs は `running` のまま残ります。ドライランは DB を読み取り専用で開き、何も書きません（DB がなければ前日の記録がないものとして動きます）
- 週次は ① 収集だけです。ドライランは ① の後に終わり、本番は ① の後に実行エラー（`not_implemented`）で終わります

## 観測（一時的。D-13）

日次の運用を始めるまで、レポートを出さずに ① 収集だけを毎朝 6:00（JST）に動かし、Star 数と人気型フィードの取得記録をためます（D-13 の「PoC での調整のしかた」）。`bin/tr-run` は使わず、`bin/tr-observe` を運用サーバーの cron から呼びます。日次の運用を始めるとき（#48）に、cron の行と `bin/tr-observe`・`src/tech_researcher/observe.py` を消します。

```sh
bin/tr-observe   # 引数はありません
```

- 日次と同じロック（`$TR_DATA_DIR/locks/daily.lock`）を使います。日次（`bin/tr-run daily`）が動いていれば、何もせずに終了コード 3 で終わります
- 実行 ID の先頭は `obs-` です。runs には `observed`（すべての情報源の取得に失敗した日と、① が失敗したときは `failed`）と終了時刻を残し、取得結果（失敗も）は source_fetches に、収集項目は items に、Star 数は star_snapshots に、人気型フィードの取得記録は feed_snapshots に残します。② 以降は動かさず、Issue は作りません
- 環境変数（`TR_DATA_DIR`・`TR_UID`・`TR_GID`）、上限時間、標準出力の要約（`result` は `observed`）は `bin/tr-run` と同じです。worker は収集用トークンを環境変数 `GITHUB_TOKEN` から読みます。トークンがないと、GitHub Search（last30days-skill と候補集め）と Star 数の測定は失敗（`auth` など）として source_fetches に残り、ほかの情報源の観測は続けます。worker.env（`${TR_SECRETS_DIR}`）を compose から worker に渡す配線はまだないので、トークンを使った観測は、その配線を足してから始めます

crontab の例（cron の `PATH` に uv と docker が入るようにします。ホストの時刻帯が JST のとき。JST でなければ、cron の実装に合わせて `CRON_TZ=Asia/Tokyo` を書くか、時刻を UTC に直します。リポジトリとデータ用フォルダのパスは運用サーバーに合わせます）：

```
0 6 * * * cd /srv/tech-researcher-repo && TR_DATA_DIR=/srv/tech-researcher bin/tr-observe >> /srv/tech-researcher/logs/cron.log 2>&1
```

観測中の確認（DB は `$TR_DATA_DIR/db/tech-researcher.sqlite3`）：

```sh
sqlite3 -readonly "$TR_DATA_DIR/db/tech-researcher.sqlite3" \
  "SELECT run_id, status, error_category, finished_at FROM runs WHERE run_id LIKE 'obs-%' ORDER BY run_id DESC LIMIT 7;"
sqlite3 -readonly "$TR_DATA_DIR/db/tech-researcher.sqlite3" \
  "SELECT source_id, status, error_category, attempts, record_count FROM source_fetches WHERE run_id = (SELECT max(run_id) FROM runs WHERE run_id LIKE 'obs-%');"
```

- ① の例外で閉じた runs の `error_category` は例外の型の名前（`RuntimeError` など）です。標準出力の要約では、同じ失敗が worker の終了コード（`exit_1`）で出ます
- 収集用トークンの形が誤っているとき（使えない文字を含むなど）は、トークンがないものとして扱います。ログの最初に `auth` の失敗が1行残ります
- 観測と同じ時刻にドライラン（`bin/tr-run daily --dry-run`）を動かさないでください。ロックが別なので同時に動けますが、同じ収集用トークンで GitHub Search の毎分の回数を取り合います
- `status` が `running` のまま残った観測は、上限時間で止められたなど、途中で終わった実行です。ログ（`$TR_DATA_DIR/logs/YYYY/MM/<実行ID>.jsonl`）で原因を確かめます
- 同じ対象日にもう一度動かしても、Star 数と人気型フィードの記録は最初のものを残し、items は同じ行を更新します

## テストと検査

```sh
uv run pytest              # 単体テストと契約テスト（実際の AI と外部への通信を使うものは除く）
uv run ruff format --check # 整形の確認
uv run ruff check          # 静的検査
```

実際の AI を使う回帰テストと、外部と通信するテストは、マーカーを指定して手で流します。

```sh
uv run pytest -m llm
uv run pytest -m network
```
