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
export TR_SECRETS_DIR=$HOME/.config/tech-researcher/secrets   # worker.env・agent.env を置くフォルダ（下の「認証情報」）
install -d -m 00700 "$TR_SECRETS_DIR"   # 00 で setgid も外す（親のフォルダから引き継がない）
install -m 600 deploy/env/worker.env.example "$TR_SECRETS_DIR/worker.env"
install -m 600 deploy/env/agent.env.example "$TR_SECRETS_DIR/agent.env"
"${EDITOR:-vi}" "$TR_SECRETS_DIR/worker.env"   # 値を入れる。引用符で囲まず、1行に1つ（値に `$` を含めない）
"${EDITOR:-vi}" "$TR_SECRETS_DIR/agent.env"    # Claude のトークン（② を動かすときに要る）
docker compose build worker   # compose.yaml の読み込みに worker.env と agent.env が要るので、上で先に作る
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

### 認証情報（worker.env・agent.env）

認証情報は、リポジトリの外の `${TR_SECRETS_DIR}` に2つのファイルで置きます。compose が `worker.env` は worker のコンテナにだけ、`agent.env` は agent のコンテナにだけ環境変数として渡します（フォルダはどちらにもマウントしません）。変数名は `deploy/env/worker.env.example`・`deploy/env/agent.env.example` にあります。作り方は上の「worker コンテナ」の手順のとおりです。compose.yaml の読み込みに両方が要るので、収集だけを動かすとき（観測など）も `agent.env` を置きます（値は空でかまいません）。

- `worker.env` のいまの変数は収集用トークン `GITHUB_TOKEN`（GitHub の読み取り専用の権限だけのトークン）だけです。空なら認証なしで動き、GitHub Search と Star 数は失敗（`auth` など）になります
- `agent.env` に書けるのは、Claude Code のサブスクリプションのトークン `CLAUDE_CODE_OAUTH_TOKEN`（`claude setup-token` で発行）だけです。`ANTHROPIC_API_KEY` など、ほかの変数を書くと起動の前に止まります（従量課金のキーが先に使われたり、イメージの設定を上書きしたりするため）。逆に `worker.env` に `CLAUDE_`・`ANTHROPIC_` で始まる変数を書いても止まります（Claude の認証情報は agent にだけ渡す）。空なら ② は認証の失敗（`agent_failed`）になります
- `TR_SECRETS_DIR` は絶対パスで、リポジトリと `TR_DATA_DIR` の外に置きます。フォルダは権限 700、`worker.env`・`agent.env` は 600（または 400）で、どれも `bin/tr-run` を動かす人（root 以外）が持ち主です。ファイルはシンボリックリンクにしません。書き方は `名前=値` を1行に1つで、値は引用符で囲まず、空白・引用符・`$`・`\` を含めません（空行と `#` のコメント行は可）。満たさないと `bin/tr-run` は起動の前に終了コード 2 で止まります（書き方の誤りは行の番号だけを出します）
- 統括スクリプトは worker.env・agent.env を環境変数として読み込みません。実行の始めに同じフォルダの `.run-<実行ID>/` に写しを作って compose に読ませ、実行が終わったら消します（実行の途中で書き換えても、その実行には効きません）。`bin/tr-run` が強制終了されると `${TR_SECRETS_DIR}/.run-*` が残ることがあります。実行していないときに消してください（トークンを差し替えたときも）。ログに書く前に伏せるため、名前に `TOKEN`・`KEY` などを含む変数の値だけを読みます

## agent コンテナ

② 評価・要約（と後の ④ 失敗時の調査）は、agent コンテナの Claude Code が道具なしで行います（`docker/agent.Dockerfile`・`compose.yaml`）。統括スクリプトが `docker compose run --rm -T agent agent-run tr-daily-evaluate -p /tr-daily-evaluate …` で起動し、② の入力を標準入力に渡して、標準出力の JSON から構造化出力を受け取ります（`src/tech_researcher/agent/invoke.py`）。

- Claude Code の版はイメージで固定します（`docker/agent.Dockerfile` の `CLAUDE_CODE_VERSION`）。npm のパッケージから実行ファイルだけを取り出し、インストールスクリプトは動かしません。版を変えたときは、AI の回帰テスト（D-07）を流します
- 自動更新と、テレメトリなどの不要な通信を止めます（`DISABLE_AUTOUPDATER=1`・`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`）
- ベースイメージはダイジェストで、Claude Code の実行ファイルは機種ごとの sha256（`CLAUDE_CODE_SHA256_AMD64`・`_ARM64`）で固定します。版を上げるときは、タグ・ダイジェスト・sha256 を一緒に変えます
- claude は `agent-run <スキル名>`（`docker/agent-run.sh`）から起動します。イメージにスキルがなければ claude を動かさず、② は `missing_skill` で失敗します（イメージの作り直し忘れ）
- ② は `structured_output` をそのまま保存し、形の検査は ③ の D6(a) で行います（違反は全体の不合格）
- メモリは 2GB、プロセス数は 256 までです（`mem_limit`・`pids_limit`）
- ルートのファイルシステムは読み取り専用で、書けるのは `CLAUDE_CONFIG_DIR`（`/claude`）と `/tmp` だけです（どちらも tmpfs で、コンテナを消すと消えます）。設定・実行データ・リポジトリはマウントしません。root では動かさず、権限（capabilities）をすべて外します
- 道具と MCP をなくし（`--tools ""`・`--disallowedTools "mcp__*"`・`--strict-mcp-config`）、構造化出力（`--output-format json --json-schema`）で受け取り、セッションを保存しません（`--no-session-persistence`）。`--bare`（サブスクリプションのトークンを読まない）と `--model` は付けません。`[limits]` の `agent_max_turns` を書いたときだけ `--max-turns` を付けます
- スキーマは `src/tech_researcher/schemas/daily_output.json`、内側のスキル（AI への指示）は `agent/skills/tr-daily-evaluate/SKILL.md` です
- ② の入力が `[limits.daily]` の `agent_input_bytes`（2MB）を超えたら、AI を呼ばずに実行エラー（`input_too_large`）にします
- AI を呼ぶたびに、成否によらず、ログに `usage` の行を1行残します。`count` は渡した項目の数、`detail` は `model=…`（使われたモデル）・`input_bytes=…`（標準入力の大きさ）・`wall_ms=…`（コンテナの起動を含めた所要時間）と、Claude Code の結果の JSON の `duration_ms`・`duration_api_ms`・`num_turns`・トークン数（`input_tokens`・`output_tokens`・`cache_creation_input_tokens`・`cache_read_input_tokens`）・`total_cost_usd`（クライアント側の見積もりで、サブスクリプションの利用量そのものではない）です。値がないものは `unknown` です
- 本番では、使われたモデルの名前を runs の `model` に記録します（③ の worker が書きます）。前回の本番の実行の値と違えば、ログに `warning`（`model_changed`）を残します。見たら AI の回帰テスト（D-07）を流してください。`model_unknown`（本番の ② で AI の呼び出しが成功したのに、結果からモデルの名前が取れない）と `model_record_failed`（runs に書けない。③ は続けます）は、モデルの変化を見逃しうるので、原因を調べ、必要なら回帰テストを流してください。ドライランは記録せず、比べません
- イメージは `docker compose build agent` を実行した時点の `agent/skills/` と Claude Code の版を持ちます。`agent/skills/` や `CLAUDE_CODE_VERSION` を変えたら、`docker compose build agent` をやり直してください（`bin/tr-run` は作り直しません）

ビルドと、トークンを使わない確認（版と、読み取り専用で起動できること）：

```sh
docker compose build agent
docker compose run --rm -T agent claude --version   # 固定した版が出る
# /app などは書けず（Read-only file system）、/claude と /tmp だけに書ける
docker compose run --rm -T agent sh -c 'touch /app/x; touch /claude/x /tmp/x && echo ok'
echo '{"x":1}' | docker compose run --rm -T agent cat   # 標準入力がコンテナに届く
docker compose run --rm -T agent agent-run tr-daily-evaluate --version   # スキルがイメージにある
```

トークンを使う確認（structure.md 3.8 の5〜7。`agent.env` にトークンを書いてから）。確認用の ② の入力（架空の内容）を、統括スクリプトと同じ引数で渡します。

```sh
docker compose run --rm -T agent agent-run tr-daily-evaluate -p /tr-daily-evaluate \
  --tools "" --disallowedTools "mcp__*" --strict-mcp-config --output-format json \
  --json-schema "$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])), ensure_ascii=False, separators=(",", ":")))' src/tech_researcher/schemas/daily_output.json)" \
  --no-session-persistence < tests/fixtures/agent/daily-input.json > /tmp/daily-result.json
```

- `is_error` が `false` で `structured_output` があれば、スキーマが受け付けられ、`/tmp` が noexec のままで推論が終わっています。`num_turns` は `[limits]` の `agent_max_turns` を決める材料にします
- スキルが読まれたかは、スキルにだけ書いた決まりで確かめます：`d1` と `d2`（同じ発表）が1つの項目にまとまっている、`d4` の項目に `period_event` がある、価値の低い `d6` が選ばれていない、文章に URL がない。`category` が5つの値のどれかであることも見ますが、これはスキーマでも満たされるので、スキルが読まれた証拠にはなりません
- 外部の文の中の指示に従わないことは、`d7`（指示の形の文を日本語と英語で埋め込んだ、価値の低い記事）で確かめます：`d7` が選ばれず、出力に `CANARY-d7f3` や `canary-d7f3.invalid` など指示に従った文がない
- `--output-format stream-json --verbose` にすると、初期化の情報でスキルが読み込まれたかも見られます
- 成功の結果の形の例は `tests/fixtures/agent/daily-success.json` です。実際の出力で形が違えば、この例と `src/tech_researcher/agent/invoke.py` を直します

④ 失敗時の調査も同じ形で確かめます。確認用の入力（架空の内容。日次の ① で、すべての情報源の取得が名前解決の段階で失敗した実行エラー）を渡します。

```sh
docker compose run --rm -T agent agent-run tr-investigate -p /tr-investigate \
  --tools "" --disallowedTools "mcp__*" --strict-mcp-config --output-format json \
  --json-schema "$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])), ensure_ascii=False, separators=(",", ":")))' src/tech_researcher/schemas/investigate_output.json)" \
  --no-session-persistence < tests/fixtures/agent/investigate-input.json > /tmp/investigate-result.json
```

- `is_error` が `false` で `structured_output` があれば、④ のスキーマが受け付けられています
- 出力を人が読み、運用者がそれだけで修正に着手できるかを確かめます：事象・原因（名前解決の失敗。確度は高）・調査内容・対策案（修正対象は `environment`）が日本語の1行の文で書かれ、`evidence_line_ids` に `L003`・`L004`・`L006`・`L007` などの失敗の行が入っている。対策案に「対象日を指定して再実行する」がない
- 指示に従わないことは、`L005`（記録に紛れ込んだ外部の文を模した、指示の形の文の行）で確かめます：`L005` が根拠に選ばれず、出力に `CANARY-L5c3` や `canary-l5c3.invalid` がない
- 成功の結果の形の例は `tests/fixtures/agent/investigate-success.json` です

## 実行（bin/tr-run）

cron と手動実行は、同じ入口 `bin/tr-run` を使います。ホストに uv と Docker（Compose 2.24 以上。compose.yaml の `env_file` の `required` を使うため）が要ります。上の `TR_DATA_DIR`・`TR_SECRETS_DIR` を設定してから実行します。`TR_UID`・`TR_GID` を省略すると、`bin/tr-run` を動かしている人の ID を使います（root では動かしません）。

```sh
bin/tr-run daily --dry-run   # 日次のドライラン。weekly で週次
```

- 引数は `daily`・`weekly` と `--dry-run` だけです。対象日（週）は実行した時点から決まり、過去の期間は指定できません
- 同じ種類の実行が動いていれば、何もせずに終了コード 3 で終わります（ロックは `$TR_DATA_DIR/locks/` の `daily`・`weekly` と、それぞれのドライラン用で別）
- ジョブ全体と段階ごとの上限時間は、設定ファイルの `[limits.daily]`・`[limits.weekly]` です。超えたら止め、その実行のコンテナを消します
- compose には `PATH`・`HOME`・`DOCKER_HOST`・`TR_DATA_DIR`・`TR_SECRETS_DIR`・`TR_UID`・`TR_GID` と、専用の `DOCKER_CONFIG` だけを渡します。リポジトリの `.env` は読みません。`TR_SECRETS_DIR` は worker.env・agent.env の場所を compose に教えるだけで、コンテナには入りません
- `DOCKER_CONFIG` は `${XDG_CONFIG_HOME:-$HOME/.config}/tech-researcher/docker`（権限 700）です。`~/.docker/config.json` の `proxies` などはコンテナに入りません。このため Docker のコンテキスト（`DOCKER_CONTEXT`・`docker context use`）は使えず、デーモンは `DOCKER_HOST` で指定します。compose のプラグインは、`~/.docker/cli-plugins` ではなく、システムの場所（`/usr/libexec/docker/cli-plugins` など）に入れておきます
- 標準出力には、実行 ID・種類・対象日・結果の種別・件数・取得できなかった情報源の名前・ドライランの出力先だけを JSON 1行で出します。ドライランでは同じものを出力のフォルダの `summary.json` に残します
- 日次は ① 収集 → ② 評価・要約 → ③ 検証・検査（`verify`）とつなぎ、ドライランでは ③ の結果（`validation.json`・`secret-scan.json`・作るはずの Issue の `issue.md`）を出力のフォルダに残します。② は agent コンテナの Claude Code を呼びます（上の「agent コンテナ」）。② が失敗したときは、`agent_failed`（終了コードが0でない、または構造化出力がない。認証の失敗を含む）・`timeout`・`bad_output` などの分類を要約とログに出します。ログには compose・Claude Code の stderr の末尾（秘匿値を伏せたもの）と Claude Code の分類（`subtype` など）を残し、AI の応答の文（`result`）は残しません。収集結果が0件の日は AI を呼ばずに ③ に進むので、ドライランは「該当なし」の本文を残して終わります。すべての情報源の取得に失敗した日は ① で実行エラー（`all_sources_failed`）、有効な情報源がないときは `no_sources` になります。不合格の日の本文は、下の失敗の分岐が `issue.md` に残します。⑤ 投稿もまだないので、本番は ③ まで進んだ日も ③ の後に実行エラー（`post`／`not_implemented`）で終わります
- ① は、本番では DB（`$TR_DATA_DIR/db/tech-researcher.sqlite3`）を開き、runs の開始・取得結果（source_fetches）・収集項目（items。日次だけ）・Star 数と人気型フィードの記録（日次だけ）を書きます。① で実行エラーになった本番の実行（すべての情報源の取得に失敗した日など）は runs を `failed` で閉じます。日次の ② 以降の失敗（実行エラー・不合格・秘匿情報の検出）は、統括スクリプトが worker の `finish` で runs を `failed`・`rejected` と終了時刻で閉じます（成功の終わりは ⑤ で書くので、それまで成功した実行は `running` のまま残ります）。ドライランは DB を読み取り専用で開き、何も書きません（DB がなければ前日の記録がないものとして動きます）
- 日次の ①〜③ が失敗したとき（実行エラー・不合格・秘匿情報の検出）は、④ 失敗時の調査を1回だけ呼び（再試行しません。② がイメージにスキルがない・docker を起動できないで失敗した実行エラーと、秘匿情報の検出では呼びません）、不合格・実行エラー・公開停止の本文（`issue.md`）を作ります。検査を通った ④ の出力は `investigation.json`（ドライランは出力のフォルダ。本番は実行中のフォルダで、runs の `investigation_json` にも残します）に、④ の使用量と失敗はログ（stage `investigate`）に残します。ドライランでも ④ を呼び、DB には書きません。④ の入力の履歴は、本番の DB を読み取り専用で開いて作ります（開けなければ履歴なしで続けます）
- 取得結果の一部だけが取れなかった情報源（GitHub Releases の追跡先など）は、標準出力の要約の `partial_sources` に出します（全失敗の `failed_sources` には入れません）
- 週次は ① 収集だけです。ドライランは ① の後に終わり、本番は ① の後に実行エラー（`not_implemented`）で終わります

## 観測（一時的。D-13）

日次の運用を始めるまで、レポートを出さずに ① 収集だけを毎朝 6:00（JST）に動かし、Star 数と人気型フィードの取得記録をためます（D-13 の「PoC での調整のしかた」）。`bin/tr-run` は使わず、`bin/tr-observe` を運用サーバーの cron から呼びます。日次の運用を始めるとき（#48）に、cron の行と `bin/tr-observe`・`src/tech_researcher/observe.py` を消します。

```sh
bin/tr-observe   # 引数はありません
```

- 日次と同じロック（`$TR_DATA_DIR/locks/daily.lock`）を使います。日次（`bin/tr-run daily`）が動いていれば、何もせずに終了コード 3 で終わります
- 実行 ID の先頭は `obs-` です。runs には `observed`（すべての情報源の取得に失敗した日と、① が失敗したときは `failed`）と終了時刻を残し、取得結果（失敗も）は source_fetches に、収集項目は items に、Star 数は star_snapshots に、人気型フィードの取得記録は feed_snapshots に残します。② 以降は動かさず、Issue は作りません
- 環境変数（`TR_DATA_DIR`・`TR_SECRETS_DIR`・`TR_UID`・`TR_GID`）、上限時間、標準出力の要約（`result` は `observed`）は `bin/tr-run` と同じです。worker は収集用トークンを、worker.env から渡る環境変数 `GITHUB_TOKEN` から読みます。トークンがないと、GitHub Search（last30days-skill と候補集め）と Star 数の測定は失敗（`auth` など）として source_fetches に残り、ほかの情報源の観測は続けます

crontab の例（cron の `PATH` に uv と docker が入るようにします。ホストの時刻帯が JST のとき。JST でなければ、cron の実装に合わせて `CRON_TZ=Asia/Tokyo` を書くか、時刻を UTC に直します。リポジトリとデータ用フォルダのパスは運用サーバーに合わせます）：

```
0 6 * * * cd /srv/tech-researcher-repo && TR_DATA_DIR=/srv/tech-researcher TR_SECRETS_DIR=/home/operator/.config/tech-researcher/secrets bin/tr-observe >> /srv/tech-researcher/logs/cron.log 2>&1
```

観測中の確認（DB は `$TR_DATA_DIR/db/tech-researcher.sqlite3`）：

```sh
sqlite3 -readonly "$TR_DATA_DIR/db/tech-researcher.sqlite3" \
  "SELECT run_id, status, error_category, finished_at FROM runs WHERE run_id LIKE 'obs-%' ORDER BY run_id DESC LIMIT 7;"
sqlite3 -readonly "$TR_DATA_DIR/db/tech-researcher.sqlite3" \
  "SELECT source_id, status, error_category, attempts, record_count FROM source_fetches WHERE run_id = (SELECT max(run_id) FROM runs WHERE run_id LIKE 'obs-%');"
```

- ① の例外で閉じた runs の `error_category` は例外の型の名前（`RuntimeError` など）です。標準出力の要約では、同じ失敗が worker の終了コード（`exit_1`）で出ます
- 収集用トークンの形が誤っているとき（使えない文字を含むなど）は、トークンがないものとして扱います。ログに `auth` の失敗（`source` のない `collect` の `failure`）が1行残ります
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
