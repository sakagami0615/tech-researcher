# 構成と運用

ディレクトリ構成、起動の構成、設定ファイル、期間の扱い、再試行と上限、ログ・実行データ・ドライラン、実装の技術・依存・テストを定める（[decisions.md](../decisions.md) の D-04・D-16・D-17・D-19〜D-23）。データの形は [data-model.md](data-model.md)、レポートの本文の形は [report-templates.md](report-templates.md) による。

## 1. 全体像

```
[運用サーバー（ホスト）]
  cron ──→ bin/tr-run daily|weekly ──→ 統括スクリプト（Python・ホストで動く）
                                          │ docker compose run --rm -T worker …  ① 収集 ③ 検証・検査 ⑤ 投稿
                                          │ docker compose run --rm -T agent  …  ② 評価・要約 ④ 失敗時の調査
                                          ▼                                      （claude -p、道具なし、標準入出力、JSON）
  運用者の Claude Code ──→ /tr-daily・/tr-weekly（外側のスキル）──→ bin/tr-run（cron と同じ入口）
  ${TR_DATA_DIR}/    db/・logs/・dry-run/（worker にだけマウントする）
  ${TR_SECRETS_DIR}/ worker.env・agent.env（権限 600、リポジトリの外）
```

- cron は統括スクリプトを直接起動する（`bin/tr-run` 経由）。起動に Claude Code を挟まない（D-04）
- 運用者が手で実行するときは、Claude Code の外側のスキル（`/tr-daily`・`/tr-weekly`）から同じ入口（`bin/tr-run`）を呼ぶ
- ② と ④ は、agent コンテナの Claude Code が内側のスキルで行う。道具を持たせず、入力は標準入力、出力は標準出力の JSON

## 2. ディレクトリ構成（D-19）

### 2.1 満たす要件

- D-01：`third_party/last30days/` に Submodule と sparse-checkout で取り込み、セットアップスクリプトを用意する
- D-02：自作コードは `src/`、第三者コードは `third_party/`。自作コードは `third_party/` を import せず CLI で呼ぶ
- coding.md：日次・週次は別スキル、共通処理は共有モジュール。期間・情報源は設定ファイルで変える
- D-04：統括スクリプトはホスト、worker・agent はコンテナ。agent から worker の env_file を読めない
- D-05：認証情報はリポジトリの外の env_file（権限 600）

### 2.2 構成

```
.
├── AGENTS.md / CLAUDE.md / README.md / LICENSE
├── .gitmodules
├── pyproject.toml / uv.lock          # Python のパッケージの定義と依存の固定（8章）
├── compose.yaml                      # worker・agent の2つのサービス
├── .claude/
│   └── skills/                       # 外側のスキル（運用者が手で起動する）
│       ├── tr-daily/SKILL.md
│       └── tr-weekly/SKILL.md
├── agent/                            # 内側（agent コンテナ）にだけ入れるもの
│   └── skills/
│       ├── tr-daily-evaluate/SKILL.md     # ② 日次の評価・要約
│       ├── tr-weekly-analyze/SKILL.md     # ② 週次の分析
│       └── tr-investigate/SKILL.md        # ④ 失敗時の調査（日次・週次で共通）
├── config/
│   └── tech-researcher.toml          # 設定ファイル（4章）。秘匿情報は書かない
├── docker/
│   ├── worker.Dockerfile
│   └── agent.Dockerfile
├── deploy/
│   ├── crontab.example               # 日次 6:00・週次 月曜 7:00（D-09）
│   └── env/
│       ├── worker.env.example        # 変数名だけ。値は書かない（トークン、投稿先のリポジトリと Projects の指定。D-20）
│       └── agent.env.example
├── bin/
│   ├── setup-third-party.sh          # D-01 のセットアップ（submodule の取得と sparse-checkout、指示ファイルがないことの確認）
│   └── tr-run                        # 実行の唯一の入口（cron と外側のスキルの両方が呼ぶ）
├── src/
│   └── tech_researcher/
│       ├── cli.py                    # 統括と各段階の入口
│       ├── orchestrator.py           # 統括スクリプト（ホストで動く。docker compose run を呼ぶ）
│       ├── config.py                 # 設定ファイルの読み込みと検証
│       ├── periods.py                # JST の期間の計算、last30days-skill の引数への変換（5章）
│       ├── stages/                   # ① ③ ⑤ の各段階（worker で動く）
│       ├── collect/                  # last30days-skill のアダプタ、RSS、Qiita、GitHub Releases、GitHub の急上昇（D-03・D-13）
│       ├── agent/                    # ② ④ の呼び出しの境界（claude の引数・入出力。AI の差し替えの境界）
│       ├── schemas/                  # ② ④ の出力の JSON Schema（--json-schema に渡し、③ の検査にも使う）
│       ├── validate/                 # D1〜D4・D6、無害化、秘匿情報の検査
│       ├── render/                   # Issue 本文のテンプレート（report-templates.md）
│       ├── publish/                  # Issue の作成、重複判定、Projects への登録
│       ├── db/                       # SQLite（data-model.md）
│       └── logging.py                # ログと秘匿情報のマスク（7章）
├── tests/
│   ├── unit/                         # 既定で流す
│   ├── contract/                     # last30days-skill の agent JSON の契約テスト（D-01）
│   ├── llm/                          # 実際の AI を呼ぶ回帰テスト（D-07。既定では流さず、CI でも流さない）
│   └── fixtures/
├── third_party/
│   └── last30days/                   # Submodule（参照と .gitmodules だけをコミットする）
└── docs/
```

置き場所の理由：

| 対象 | 置き場所 | 理由 |
|---|---|---|
| 外側のスキル | `.claude/skills/` | Claude Code は、起動したディレクトリからリポジトリのルートまでの `.claude/skills/<name>/SKILL.md` をプロジェクトのスキルとして読む（[Skills](https://code.claude.com/docs/en/skills)、2026-10-25 に確認）。運用者がリポジトリで Claude Code を開けば `/tr-daily` で使える |
| 内側のスキル | `agent/skills/`（`.claude/skills/` の外） | 外側のセッションに内側のスキルを見せないため。内側のスキルを道具のある外側のセッションで誤って起動すると、外部コンテンツを道具のある AI に読ませる形になる。agent のイメージを作るときにコンテナの中へコピーする |
| 出力の JSON Schema | `src/tech_researcher/schemas/` | 統括スクリプトが `--json-schema` に渡し、③ が同じものを検査に使う。agent コンテナに Python のコードを入れずに済む |
| 設定ファイル | `config/` | 秘匿情報を含まないのでコミットする。worker に読み取り専用でマウントする |
| 実行データ（DB・ログ・ドライランの出力） | リポジトリの外の `${TR_DATA_DIR}` | 7章（D-22） |
| 認証情報 | リポジトリの外の `${TR_SECRETS_DIR}` | D-05 |

- agent のイメージに入れるのは、Claude Code 本体と `agent/skills/` だけにする。`src/`・`config/`・`third_party/` は入れない。agent の AI が道具を持たないことに加えて、コンテナの中に読める秘匿情報や収集データを置かないため

### 2.3 実装で決める細部

- `stages/` の中の分け方（モジュール名）
- `compose.yaml` をルートに置くか `deploy/` に置くか
- `bin/tr-run` をシェルスクリプトにするか、`uv run` の入口にするか

## 3. 起動の構成（D-04）

### 3.1 Claude Code について確かめた事実

F1〜F14 は公式ドキュメントで確かめた（2026-10-25）。F15〜F18 は 2026-10-27 に開発機で `claude -p` を実行して確かめた。

| # | 事実 | 出典 |
|---|---|---|
| F1 | `--bare` は OAuth の認証情報を読まず、`CLAUDE_CODE_OAUTH_TOKEN` も読まない。`ANTHROPIC_API_KEY` か `apiKeyHelper` が要る | [Headless](https://code.claude.com/docs/en/headless)、[Authentication](https://code.claude.com/docs/en/authentication) |
| F2 | `claude setup-token` は1年間有効の OAuth トークンを発行する。トークンはどこにも保存されず、`CLAUDE_CODE_OAUTH_TOKEN` に設定して使う。スコープは推論だけ | [Authentication](https://code.claude.com/docs/en/authentication) |
| F3 | 認証の優先順位では、`ANTHROPIC_AUTH_TOKEN`・`ANTHROPIC_API_KEY`・`apiKeyHelper` が `CLAUDE_CODE_OAUTH_TOKEN` より先に使われる。`-p` では `ANTHROPIC_API_KEY` があれば常に使われる | [Authentication](https://code.claude.com/docs/en/authentication) |
| F4 | `--tools ""` で組み込みの道具（Bash・Read・WebFetch・WebSearch などを含む）をすべて外せる。`--tools` は MCP の道具には効かないので、`--disallowedTools "mcp__*"` で外す。道具を外さずに許可だけで絞る方法は使えない（承認の要らない道具は、許可に書かなくても動く） | [CLI reference](https://code.claude.com/docs/en/cli-reference)、[Tools reference](https://code.claude.com/docs/en/tools-reference) |
| F5 | `-p` では、プロンプトの先頭に `/skill-name` を書くと、Claude Code がスキルを展開してから実行する。文の途中に書いた場合は、AI が `Skill` の道具で起動してよいという意味になるだけ | [Headless](https://code.claude.com/docs/en/headless)、[Skills](https://code.claude.com/docs/en/skills) |
| F6 | `--output-format json` と `--json-schema` を合わせると、スキーマに合う出力が標準出力の JSON の `structured_output` に入る。合う出力がないまま終わると `structured_output` がない（`subtype` が `success` でもないことがある）。仕組みは Claude Code が加える `StructuredOutput` の道具で、合わなければ呼び直させ、回数を使い切ると `error_max_structured_output_retries` で終わる。スキーマが不正なら起動の時点でエラーになる | [Headless](https://code.claude.com/docs/en/headless)、[Structured outputs](https://code.claude.com/docs/en/agent-sdk/structured-outputs) |
| F7 | `-p` の標準入力は 10MB が上限で、超えると非0で終わる | [Headless](https://code.claude.com/docs/en/headless) |
| F8 | `-p` は成功で終了コード0、失敗で非0を返す | [Headless](https://code.claude.com/docs/en/headless) |
| F9 | `--no-session-persistence` でセッションをディスクに保存しない。`--strict-mcp-config` で `--mcp-config` 以外の MCP の設定を無視する。`--max-turns` でターン数を制限し、超えるとエラーで終わる | [CLI reference](https://code.claude.com/docs/en/cli-reference) |
| F10 | `CLAUDE_CONFIG_DIR` で設定・セッションの履歴の置き場所（既定 `~/.claude`）を変えられる。一時ファイルは `CLAUDE_CODE_TMPDIR`（既定は OS の一時フォルダ）に書く。`DISABLE_AUTOUPDATER=1` で自動更新を止め、`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` で自動更新・テレメトリ・エラー報告などの通信を止める。`CLAUDE_CODE_MAX_RETRIES` は API の失敗の再試行回数で、既定は10 | [Environment variables](https://code.claude.com/docs/en/env-vars) |
| F11 | Bash の道具の既定のタイムアウトは2分、上限は10分で、達したコマンドはバックグラウンドに移る。スキルの `` !`command` `` は展開の前にコマンドを実行し、2分で打ち切られる | [Tools reference](https://code.claude.com/docs/en/tools-reference)、[Skills](https://code.claude.com/docs/en/skills) |
| F12 | スキルの frontmatter の `disable-model-invocation: true` で、AI が自分でスキルを起動しないようにできる。`allowed-tools` は、そのスキルを起動したターンだけ、列挙した道具を確認なしで使えるようにする。`${CLAUDE_PROJECT_DIR}` を本文と `allowed-tools` の Bash のルールで使える | [Skills](https://code.claude.com/docs/en/skills) |
| F13 | Bash の許可のルールはコマンドの文字列で照合するだけで、セキュリティの境界ではない。読み取り専用のコマンド（`ls`・`cat`・`grep` など）は、許可のルールに書かなくても動く | [Permissions](https://code.claude.com/docs/en/permissions) |
| F14 | ネイティブのインストーラは、バージョン番号を指定してインストールできる | [Setup](https://code.claude.com/docs/en/setup) |
| F15 | `--tools ""` でも、プロンプトの先頭の `/スキル名` で作業ディレクトリの `.claude/skills/` のスキルが展開される。`Skill` の道具は要らない | 2026-10-27 に開発機で実行して確認 |
| F16 | `--tools ""` のとき、初期化の情報に出る道具は `StructuredOutput` だけで、構造化出力が返る | 同上 |
| F17 | 入力に埋め込んだ「ファイルを読め」「URL を取得せよ」に対して、道具の呼び出しは起きない（呼び出せる道具がない） | 同上 |
| F18 | 標準出力の JSON の初期化の情報と `modelUsage` に、使われたモデルの名前が出る | 同上 |

### 3.2 cron と bin/tr-run

- ホストの crontab に、日次 6:00・週次 月曜 7:00（JST、D-09）で `bin/tr-run daily`・`bin/tr-run weekly` を書く。出力は `${TR_DATA_DIR}/logs/cron.log` に足していく
- `bin/tr-run` は、二重起動を防ぐロック（`flock`）と、ジョブ全体の上限時間（6章）を付けて統括スクリプトを起動する。ロックは日次と週次で別にする（月曜は日次と週次の実行が重なりうるが、同じ種類の二重起動だけを防ぐ）
- `bin/tr-run` が受け付ける引数は、`daily`・`weekly` と `--dry-run` だけにする。知らない引数は拒否する。過去の期間を指定する引数は作らない（US-E6-05）
- `bin/tr-run` が標準出力に出すのは、コードが作る定型の要約だけにする：実行 ID・日次か週次か・対象日（週）・結果の種別（レポート・不合格・実行エラー・ドライラン）・Issue の番号と URL・件数・取得できなかった情報源の名前（設定ファイルの名前）・ドライランの出力先。収集したタイトル・本文と AI の出力は出さない
- ホストの時刻帯が JST でない場合の crontab の書き方（UTC に直すか、cron の実装が対応していれば `CRON_TZ` を使うか）は、運用サーバーの cron の実装を確かめて決める。対象期間の計算はホストの時刻帯に依存させず、コードが JST で行う

### 3.3 統括スクリプトから agent の Claude Code を呼ぶ形

```sh
docker compose run --rm -T agent \
  claude -p "/tr-daily-evaluate" \
    --tools "" \
    --disallowedTools "mcp__*" \
    --strict-mcp-config \
    --output-format json \
    --json-schema "<src/tech_researcher/schemas/ の内容>" \
    --no-session-persistence \
    --max-turns <設定ファイルの上限>
# 標準入力：worker が作った ② の入力 JSON（data-model.md 4章）
# 標準出力：結果の JSON（structured_output を統括スクリプトが取り出す）
```

| 引数・設定 | 目的 | 根拠 |
|---|---|---|
| `--bare` を付けない | サブスクリプションのトークンを使うため | F1 |
| `--tools ""`、`--disallowedTools "mcp__*"`、`--strict-mcp-config` | 道具（コマンドの実行・ファイルの読み書き・ネットワーク）をなくす | F4・F9 |
| プロンプトの先頭を `/tr-daily-evaluate` にする | `Skill` の道具を許可せずにスキルの指示を渡す | F5・F15（確認済み） |
| `--output-format json --json-schema` | 構造化出力で受け取る | F6 |
| `--no-session-persistence` | セッションを保存せず、`CLAUDE_CONFIG_DIR` への書き込みを減らす。外部コンテンツを含む会話の記録をディスクに残さない | F9 |
| `--max-turns` | 構造化出力の呼び直しが終わらないときに止める | F6・F9 |
| 環境変数 `CLAUDE_CODE_OAUTH_TOKEN` | agent.env から渡す | F2・D-05 |
| 環境変数 `ANTHROPIC_API_KEY`・`ANTHROPIC_AUTH_TOKEN` を渡さない | 従量課金のキーが先に使われるのを防ぐ。統括スクリプトは、compose に渡す環境変数を許可したものだけに絞る | F3・D-05 |
| 環境変数 `CLAUDE_CONFIG_DIR`・`CLAUDE_CODE_TMPDIR` | 書き込めるのをこの2つの場所だけにする | F10・D-04 |
| 環境変数 `DISABLE_AUTOUPDATER=1`、`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` | イメージに固定した版を実行中に更新させない | F10・F14 |
| agent のイメージで Claude Code の版を固定する | プロンプト・CLI を変えたときに D-07 の回帰テストを流す運用に合わせる | F14・D-07 |

- 統括スクリプトの判定：終了コードが0でない、または `structured_output` がないときは ② の失敗とする（F6・F8）。結果の JSON の `subtype`（`error_max_structured_output_retries` など）はエラーの分類に使い、ログに残す
- Claude Code の中の構造化出力の呼び直し（F6）は、1回の呼び出しの中でスキーマに合わない出力を直させる仕組みで、US-E7-02 の「自動での再生成」（③ で不合格になった後の再生成）には当たらない
- `--model` は付けず、Claude Code の既定のモデルに任せる。使われたモデルの名前を標準出力の JSON（`modelUsage`）から取り出して runs の `model` に記録し（[data-model.md](data-model.md#runs)）、前回の本番の実行から変わっていたらログに警告を出す。ドライランは記録しないので比べない。運用者はそれを見て、AI の回帰テスト（D-07）を流す
- ④ も同じ形で、プロンプトを `/tr-investigate` にし、④ のスキーマを渡す。週次の ② は `/tr-weekly-analyze`

### 3.4 外側のスキル（手動実行用）

```markdown
---
name: tr-daily
description: tech-researcher の日次の処理を実行する（運用者が手動で起動する）
disable-model-invocation: true
argument-hint: "[--dry-run]"
allowed-tools: Bash(${CLAUDE_PROJECT_DIR}/bin/tr-run daily) Bash(${CLAUDE_PROJECT_DIR}/bin/tr-run daily --dry-run)
---
（本文：`${CLAUDE_PROJECT_DIR}/bin/tr-run daily` を、引数が `--dry-run` のときだけそれを付けて1回実行し、出力された要約をそのまま報告する。ほかのコマンドは実行しない。出力にないファイルは開かない）
```

- `disable-model-invocation: true`：運用者が `/tr-daily` と打ったときだけ動かす（F12）
- `allowed-tools`：2つのコマンドだけを確認なしで動かす（F12）。許可のルールは境界ではない（F13）が、外側のセッションは運用者が手元で操作するもので、ほかの操作には運用者の確認が入る
- 外側のセッションが外部コンテンツを読まないよう、`bin/tr-run` は要約だけを出し（3.2）、実行データはリポジトリの外に置く（7章）。読み取り専用のコマンドは許可なしで動く（F13）ため、作業ディレクトリに外部コンテンツを置かないことで防ぐ
- 処理は長いので、Bash の道具はタイムアウトしてバックグラウンドに移る（F11）。移った後も対話のセッションで完了を待って結果を受け取れるかは確かめていない（3.8 の9）。`` !`command` ``（F11）は2分で打ち切られるので使わない
- `tr-weekly` も同じ形で、`weekly` を渡す
- 外側のスキルは運用者のログインで動く Claude Code から使う。統括スクリプトは、agent の認証に agent.env だけを使い、自分の環境にある Claude の認証情報を compose に渡さない

### 3.5 スキルの分け方

| スキル | 使う場所 | 内容 | 道具 |
|---|---|---|---|
| `tr-daily`・`tr-weekly` | 外側（運用者の手元の Claude Code） | `bin/tr-run` を1回起動し、要約を報告する | `allowed-tools` で `bin/tr-run` の2つのコマンドだけ |
| `tr-daily-evaluate` | 内側（agent） | ② 日次の評価・要約の指示 | なし |
| `tr-weekly-analyze` | 内側（agent） | ② 週次の分析の指示 | なし |
| `tr-investigate` | 内側（agent） | ④ 失敗時の調査の指示。日次・週次の違いは入力の JSON で渡す | なし |

- 統括スクリプトを起動する共通のスキルは作らない。スキルからスキルを呼ぶには `Skill` の道具の許可が要り、外側に許す操作が増える。日次・週次の共通の処理は `bin/tr-run` と Python のパッケージ（共有モジュール）が持つ
- 内側のスキルは SKILL.md だけで完結させる（D-04）。入力の JSON の形、出力の各項目の意味、外部コンテンツ中の指示に従わないこと（US-E7-06）を本文に書く。出力の形そのものは `--json-schema` が強制する
- ② の日次と週次を分けるのは、coding.md の「日次・週次は別スキル」に合わせるため。④ は日次・週次で調査の手順が同じなので1つにする

### 3.6 手動実行とドライランの場所（D-22）

| 実行 | どこで | 方法 | 要る認証情報 |
|---|---|---|---|
| 本番の手動実行（Issue を作る） | 運用サーバーだけ | `/tr-daily`・`/tr-weekly` | 本番と同じ |
| ドライラン | どこでも（運用サーバー・開発機） | `/tr-daily --dry-run` など、または `bin/tr-run daily --dry-run` | agent.env（Claude のトークン）と収集用のトークン。Issue 用・Projects 用は要らない |

- 本番の手動実行を運用サーバーに限るのは、本番の DB（Star 数などの記録）が運用サーバーにしかないため。ほかの場所で本番を実行すると D-13 の判定がずれる
- 開発機には DB がない前提とする。開発機でのドライランは、D-13・D-16 の判定をすべて前日の記録がない日として行い、週次は比較できる過去の観測がない週として行う
- 手動実行の対象期間は、cron と同じく実行の時点から決める（前日・前週）。過去の期間は指定できない（US-E6-05）

### 3.7 認証情報との関係（D-05）

- Claude のトークンを渡すのは agent コンテナだけ。cron の実行では外側の Claude Code を使わないので、外側用のトークンは要らない
- 統括スクリプト（ホスト）は env_file の中身を読まず、compose が各コンテナにだけ渡す。統括スクリプトから compose に渡す環境変数は、許可したものだけに絞る（F3 の `ANTHROPIC_API_KEY` などがホストのシェルにあっても、コンテナに入れない）

### 3.8 PoC で確認すること

Claude のトークンを使って `claude -p` を実行して確かめる。

1〜4 は 2026-10-27 に開発機で確かめた（Claude Code 2.1.310、`claude -p` を4回。下の F15〜F18）。

1. （確認済み）`--tools ""` のまま、プロンプトの先頭の `/スキル名` でスキルが展開される。`Skill` の道具は要らない
2. （確認済み）`--tools ""` でも `StructuredOutput` が使えて、`structured_output` が返る
3. （確認済み）プロンプトの引数の `/スキル名` と標準入力の JSON が、1つの依頼として渡る
4. （確認済み）初期化の情報の道具は `StructuredOutput` だけ。入力に「/etc/passwd を読め」「URL を取得せよ」を埋め込んでも、道具の呼び出しは起きない
5. agent コンテナで、内側のスキルをどこに置けば読まれるか（作業ディレクトリの `.claude/skills/` か、`${CLAUDE_CONFIG_DIR}/skills/` か）
6. ルートのファイルシステムを読み取り専用にし、`CLAUDE_CONFIG_DIR` と一時フォルダだけを書き込めるようにして起動できるか
7. `--max-turns` の値と、構造化出力の呼び直しの関係（何ターンで足りるか）。2026-10-27 の短い入力では `num_turns` は 2 で、`--max-turns 1` でも成功した。実際の入力の量で確かめる
8. サブスクリプションの利用上限の範囲で、日次・週次が完了するか。ドライランを数日回し、標準出力の JSON の使用量と所要時間を記録する。上限に達したときのエラー文も記録し、AI が使えない失敗として分類する
9. 外側のスキルで `bin/tr-run` を実行し、Bash の道具がバックグラウンドに移った後も、対話のセッションで完了を待って結果を受け取れるか

## 4. 設定ファイル（D-20）

### 4.1 満たす要件

- US-E8-01：日次・週次の調査期間、使う情報源の追加・除外を設定ファイルで変える
- D-11：GitHub Releases の追跡するリポジトリを設定で指定する。出典のリンク文字列に、設定ファイルの情報源名を使う（report-templates.md 0.3）
- D-13：A・C・D・`N_report`・`D_search`・`C_search`（加える場合は B）と、監視するリポジトリを設定ファイルに置く
- D-01：last30days-skill の `--plan` を設定ファイルから決定的に作る
- D-21：上限の値を設定ファイルに置き、運用で調整する

### 4.2 形式と置く項目

- 形式は TOML とする。Python の標準ライブラリ（`tomllib`）で読め、コメントを書けるため
- 読み込むときにコードで項目と型を確かめ、知らない項目はエラーにする（誤字で設定が効かないのを防ぐ）
- 置くのは、期間・情報源・急上昇のしきい値・last30days-skill の計画・上限値だけにする
- 次はコードの固定値とし、設定ファイルに置かない：「少ない日」のしきい値（report-templates.md 1.5）、文字数の上限、D-07 の N、週次の比較の窓（初期値は過去4週。report-templates.md 2.3）、対象週の識別（その週の月曜。US-E4-03）

### 4.3 項目の例

値は初期値。項目名は実装で決める。

```toml
[daily]
period_days = 1                 # 対象期間の長さ。終わりは実行日の 0:00（JST）、始まりはその N 日前

[weekly]
period_days = 7                 # 終わりは実行日（月曜）の 0:00（JST）。D-17

# 情報源。enabled = false で除外し、ブロックを足して追加する
[sources.hackernews]
enabled = true
display_name = "Hacker News"
collector = "last30days"

[sources.github_search]
enabled = true
display_name = "GitHub"
collector = "last30days"

[last30days]                    # --plan を作る元（D-01）
subqueries = [
  { search_query = "…", ranking_query = "…", sources = ["hackernews", "github"], weight = 1.0 },
]

[sources.github_repos]          # 新着・急上昇（D-13）
enabled = true
display_name = "GitHub"
search_topics = ["llm", "ai-agents"]
watch_repos = ["anthropics/claude-code"]
threshold_increase_A = 100
report_top_N = 10
new_repo_days_D = 14
new_repo_stars_C = 200
search_days_D = 0               # PoC で決める
search_min_stars_C = 0          # PoC で決める

[sources.github_releases]       # REST API で取る（D-11）
enabled = true
display_name = "GitHub Releases"
repos = ["anthropics/claude-code", "openai/codex"]

[[feeds]]                       # RSS（公式ブログ・Zenn・はてなブックマークなど。D-11）
id = "openai"
display_name = "OpenAI"
url = "https://openai.com/news/rss.xml"
kind = "new"                    # new（新着）／popular（人気型。D-13 の初登場の判定の対象）
enabled = true

[sources.qiita]
enabled = true
display_name = "Qiita"
tags = ["LLM", "AWS"]

[limits]                        # 上限（6章。D-21）
# agent_max_turns =           # 値は PoC で決める（3.8 の7）
# ほかの上限も同じく置く
```

### 4.4 実装で決める細部

- 項目名、ファイルの分け方（情報源を別のファイルにするか）

## 5. 期間の扱い

### 5.1 満たす要件

- US-E2-01：日次は前日 0:00〜当日 0:00（JST、終わりを含まない）
- US-E1-01：対象期間内に起きた出来事だけを残す。公開日時が不明なら投稿日時で判定する。last30days-skill の記事は UTC の日付で近似する
- US-E3-01・US-E8-01：週次の調査期間を設定で変えられる
- US-E4-03：対象週の識別は月曜の日付で、調査期間の設定を含まない

### 5.2 日次・週次の期間

- コード（`periods.py`）が JST の対象期間 `[start, end)` を決める
  - 日次（対象日 T）：T 0:00〜T+1 0:00（JST）
  - 週次（対象週の月曜 M、7日）：M−7 0:00〜M 0:00（JST）。前週の月曜 0:00〜日曜 24:00（D-17）
- 自作の収集（RSS・Qiita・GitHub の API）は時刻付きの日時が取れるので、`[start, end)` でそのまま絞る

### 5.3 last30days-skill の期間指定（D-16）

last30days-skill は `--days N --as-of YYYY-MM-DD` で期間を受け取り、粒度は UTC の日付だけ。`--days 1 --as-of D` は UTC の D−1〜D を取り、出力の `published_at` も UTC の日付だけ（D-01）。

1. 対象期間を覆う UTC の日付を渡す：`as_of` は end を UTC にした日付、`days` は `as_of` と start を UTC にした日付の差
   - 日次（対象日 T）：start は T−1 15:00 UTC、end は T 15:00 UTC。`--days 1 --as-of T`（UTC の T−1〜T を取る）
   - 週次（月曜 M、7日）：`--days 7 --as-of M−1`（UTC の M−8〜M−1 を取る）
2. 取得した記事は、UTC の日付がその範囲に入っていれば期間内とみなす。時刻を取り直すことはしない。そのため、対象期間の前後の日の記事も候補に入りうる（日次では約48時間分）
3. 日次では、同じ記事が2日続けて載らないよう、期間内の判定が last30days-skill の UTC の日付だけによる記事のうち、前日の日次レポート（公開されたレポート Issue）に載せた記事を除く。除くのは ① で、AI に渡す前に行う（items の `hold_reason` を `shown_previous_day` にする。data-model.md 3.4・3.5）。観測としては DB に残す
4. 週次も同じ近似で、前後の日の記事が入りうる。前の週と重なる記事を週次で除くことはしない（3 の除外は日次だけに行う）

### 5.4 実装で決める細部

- `period_days` を変えたときの対象日の決め方（終わり − 1日を対象日とする）

## 6. 再試行・実行時間・API 利用量の上限（D-21）

### 6.1 満たす要件

- US-E6-02：一時的な失敗を自動で再試行する
- US-E6-01：API 利用量・実行時間の許容上限を決める。API 費用は月 $0（D-11）
- US-E6-04：再試行回数を Issue に書く（「N 回（上限 N 回）」）
- US-E7-02：不合格のレポートを自動で再生成しない

### 6.2 外部サービスの上限（事実）

| サービス | 上限 | 出典（2026-10-25 に確認） |
|---|---|---|
| GitHub REST API（トークンあり） | 1時間 5,000 回。同時の要求は 100 まで。1分 900 ポイント（GET は1、POST などは5）。作成系は1分 80 回・1時間 500 回 | [Rate limits for the REST API](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api) |
| GitHub Search API | トークンありで1分 30 回、なしで1分 10 回。1回の検索で最大 1,000 件 | [Search](https://docs.github.com/en/rest/search/search) |
| GitHub GraphQL API | 個人のトークンで1時間 5,000 ポイント。`first`・`last` は1〜100 | [GraphQL rate limits](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api) |
| Qiita API v2 | 認証ありで1時間 1,000 回、なしで IP ごとに1時間 60 回 | [Qiita API v2](https://qiita.com/api/v2/docs) |
| `claude -p` の標準入力 | 10MB | 3.1 の F7 |
| Claude Code の API の再試行 | 既定 10 回（`CLAUDE_CODE_MAX_RETRIES`） | 3.1 の F10 |

サブスクリプションの利用上限の数値は確かめていない（3.8 の8で測る）。

### 6.3 再試行

| 対象 | 1回の上限時間 | 再試行（初回を除く） | 再試行する失敗 | 再試行しない失敗 |
|---|---|---|---|---|
| 外部への HTTP 要求（RSS・GitHub・Qiita） | 接続 10 秒・応答 30 秒 | 2 回。待ちは 5 秒、次に 20 秒。`Retry-After` があれば従う（60 秒まで） | タイムアウト、名前解決・接続の失敗、HTTP 429・5xx | 4xx（429 を除く） |
| last30days-skill の CLI の1回 | 300 秒（内部の上限の約171秒より長くする。D-01） | 1 回 | 終了コードが0・3以外、上限時間の超過 | 終了コード 2（`--plan` がない設定の誤り） |
| ② 評価・要約 | 再試行を含めた合計で日次 20 分・週次 40 分 | 1 回（合計の上限の残りの時間の中で行う） | 出力がなく、エラーの分類が過負荷・サーバーエラー・通信の失敗のとき | 利用上限・認証の失敗（すぐに実行エラー。D-05）、上限時間の超過、出力がスキーマに合わない（US-E7-02）。出力がない一時的な失敗の再試行は、③ で不合格になった後の再生成ではないので、US-E7-02 の「自動での再生成」に当たらない |
| ④ 失敗時の調査 | 日次 8 分・週次 10 分 | 0 回 | — | すべて（失敗したら定型の Issue にする。report-templates.md 3.5） |
| ⑤ GitHub への書き込み | 30 秒 | 2 回。再試行の前に重複判定（report-templates.md 4.3）をやり直す | タイムアウト、HTTP 5xx、二次レート制限 | 4xx（403 の二次レート制限を除く） |
| ⑤ Projects への登録 | 30 秒 | 2 回 | 同上 | 同上。使い切ったら記録して次の実行に回す（US-E4-03） |

- 段階（① 〜 ⑤）やジョブ全体の再試行はしない。失敗の後は次の定時の実行から再開する（US-E6-05）
- 実行エラー Issue の「再試行回数」には、失敗した処理での回数を書く

### 6.4 実行時間

段階ごとの上限の合計がジョブ全体の上限に収まるようにする。統括スクリプトは段階ごとの上限で自分で止まるので、② が上限まで動いても ④・⑤ の時間が残る。

| 対象 | 日次 | 週次 | 超えたとき |
|---|---|---|---|
| ① 収集（全体） | 20 分 | 25 分 | 終わった情報源で続ける（部分レポート。US-E6-03）。全部失敗なら実行エラー |
| ① の情報源ごと（① 全体の内数） | 10 分 | 10 分 | その情報源を失敗とする |
| ② 評価・要約（再試行を含めた合計） | 20 分 | 40 分 | 実行エラー |
| ③ 検証・検査 | 2 分 | 3 分 | 実行エラー |
| ④ 失敗時の調査 | 8 分 | 10 分 | 定型の Issue にする（report-templates.md 3.5） |
| ⑤ 投稿 | 5 分 | 5 分 | 実行エラー（Issue を作った後なら、Projects の登録だけを次の実行に回す） |
| 段階の合計 | 55 分 | 83 分 | — |
| ジョブ全体 | 60 分（6:00 に始めて 7:00 まで。月曜の週次 7:00 と重ならない） | 90 分 | `bin/tr-run` が統括スクリプトを止める。止めたことはログに残す |

- `bin/tr-run` に止められた統括スクリプトは実行エラー Issue を作れない。ジョブ全体の上限は、統括スクリプトの不具合で止まらないときの最後の歯止めとし、段階の合計との差（日次 5 分・週次 7 分）をコンテナの起動などの余裕とする

### 6.5 API 利用量（1回の実行あたり）

| 対象 | 上限 | 理由 |
|---|---|---|
| GitHub REST（収集用トークン） | 1,500 回 | 1時間 5,000 回の3割。急上昇の判定、GitHub Releases と一次情報の取得に使う |
| GitHub Search | 1分 25 回の間隔で、合計 100 回 | 1分 30 回の上限より少なく保つ |
| GitHub GraphQL（Star 数） | 1回の問い合わせで最大 100 リポジトリ、50 回まで | D-13 の「まとめて取る」 |
| GitHub の書き込み（Issue 用・Projects 用） | 20 回 | 作成系の上限（1分 80 回）より十分小さい |
| Qiita API（トークンなし） | 30 回 | 1時間 60 回の半分。D-05 に Qiita のトークンはないため、トークンなしで使う |
| ② の入力（標準入力） | 日次 2MB、週次 8MB | 10MB の上限に余裕を持たせる。超えたら実行エラー（② の段階、「入力が上限を超えた」）。決まった規則で削って続けることは、当面しない |
| agent の Claude Code の呼び出し | ② 最大2回（再試行を含む）、④ 最大1回 | サブスクリプションの範囲に収める（D-05） |

- `--max-budget-usd` は使わない。クライアント側の費用の見積もりで判定するもので、サブスクリプションの利用量の上限ではないため（[CLI reference](https://code.claude.com/docs/en/cli-reference)）
- 上限を超えたときは、それ以上の要求を出さず、その情報源を失敗として扱う（部分レポート）
- 各値は設定ファイルの `[limits]` に置き、運用で調整する

### 6.6 実装で決める細部

- 待ち時間のゆらぎ（ジッタ）、並列度
- `CLAUDE_CODE_MAX_RETRIES` を既定から変えるか

## 7. ログ・実行データ・ドライラン

### 7.1 満たす要件

- US-E6-06：ジョブの開始・終了・処理段階・失敗をログに出す。ログに認証情報・秘匿情報を出さない
- US-E7-01：除外した項目をログと履歴に残す
- US-E7-04・report-templates.md 3.2：④ に渡すログの行に ID を付ける
- US-E8-04：ドライランでレポート本文と各結果を確かめられる。Issue を作らず、Projects に登録しない。本番の Issue の作成を妨げない
- D-13：ドライランは Star 数・人気型フィードの取得記録・「載せた」の記録を書き込まない

### 7.2 置き場所（D-22）

- DB・ログ・ドライランの結果は、リポジトリの外のデータ用フォルダ（`${TR_DATA_DIR}`、例：`/srv/tech-researcher`）に置き、worker にだけマウントする。agent にはマウントしない
- 理由：外部コンテンツを含むデータを、道具のある AI（手元の Claude Code）の作業ディレクトリに置かないため。git に入る心配もない

```
${TR_DATA_DIR}/
├── db/tech-researcher.sqlite3
├── logs/
│   ├── cron.log                       # bin/tr-run と cron の出力
│   └── 2026/10/<実行ID>.jsonl          # 1回の実行のログ（統括スクリプトと worker が書く）
├── work/
│   └── <実行ID>/                        # 本番の実行中のファイル。実行が終わったら消す
│       ├── collected.json              # ① の収集結果（一次情報の本文を含む。③ の照合に使う）
│       ├── agent-input.json            # ② ④ の入力
│       └── agent-output.json           # ② ④ の出力
└── dry-run/
    └── <実行ID>/
        ├── summary.json                # bin/tr-run が出す要約と同じもの
        ├── collected.json              # ① の収集結果（③ の照合に使う）
        ├── agent-input.json            # ② の入力
        ├── agent-output.json           # ② の出力（structured_output）
        ├── validation.json             # ③ の結果（合否、除外した項目と理由）
        ├── secret-scan.json            # 秘匿情報の検査の結果（検出したかどうかと、箇所の種類だけ）
        ├── investigation.json          # ④ を実行したときだけ
        ├── issue.md                    # 作るはずだった Issue のタイトル・ラベル・本文（レポート・不合格・実行エラーのいずれか）
        └── log.jsonl                   # その実行のログ
```

- 本番の実行中のファイル（一次情報の本文を含む収集結果、② ④ の入力と出力）は `${TR_DATA_DIR}/work/<実行ID>/` に置き、実行が終わったら成否を問わず消す。一次情報の本文を残さない（D-14・D-18）ため。途中で止まった実行の残りは、次の実行の始めに消す。② の出力のうち残すものは DB（reports の `ai_output_json` など）に入れる
- ドライランの出力には一次情報の本文を含む収集結果が残る。運用者が確かめるための出力で、消すのは運用者が手で行う（7.4）

### 7.3 ログ

- 形式は JSON Lines。1行に、時刻（JST）・実行 ID・日次か週次か・対象日（週）・段階・出来事（開始・終了・再試行・失敗・情報源の結果・項目の除外）・情報源名・エラーの分類・件数を持たせる
- 秘匿情報を出さない：ログに書く前に、(1) 既知の秘匿値（env_file から読んだ値）を伏せ、(2) トークンの形（`ghp_`・`github_pat_`・`sk-ant-` など）を検出して伏せる。(2) は US-E7-05 の秘匿情報の検査と同じ検出器を使う
- 例外のメッセージやスタックトレースは、ログに残しても Issue には書かない（US-E6-04、report-templates.md 3.2）。ログに残す前にも上のとおり伏せる
- ④ の入力では、コードがログの行に ID（`L012` など）を付けて渡す
- ログの保持期間と消し方は決めていない（要件にない。容量を見て決める）

### 7.4 ドライラン（D-22）

- ドライランは本番の DB を読み取り専用で開き、何も書かない。ジョブの履歴も DB に残さず、ドライランの出力のフォルダにだけ残す（US-E5-02 の「ジョブごとの履歴」は本番の実行が対象）。DB の扱いの詳細は [data-model.md](data-model.md#6-ドライラン) 6章
- 新しく集めた記事の番号は、ドライランの間だけの仮の番号にする
- ⑤ だけを飛ばす（D-04）。① 〜 ④ の結果は上のファイルで確かめる
- 実行 ID の先頭に `dry-` を付けて、本番と区別する
- ドライランの出力は自動では消さない。消すのは運用者が手で行う

### 7.5 実装で決める細部

- `cron.log` が大きくなりすぎるのを防ぐ方法

## 8. 実装の技術・依存・テスト（D-23）

### 8.1 技術と道具

| 用途 | 使うもの | 理由 |
|---|---|---|
| 言語 | Python 3.14 | 2026-10-25 時点でサポート中の版は 3.15（2026-10-09 リリース）・3.14（2025-10-07）・3.13・3.12・3.11（[Status of Python versions](https://devguide.python.org/versions/)）。Python には LTS の区別がないので、リリースから1年たった 3.14 を「十分に安定した最新版」として使う。last30days-skill は `requires-python = ">=3.12"` で、満たす。worker・ホスト（統括スクリプト）・last30days-skill を同じ版にそろえる |
| パッケージ・依存の管理 | uv（`pyproject.toml`・`uv.lock`） | ロックファイルで版を固定できる。ホストでも Python の版を uv で用意できる |
| テスト | pytest | D-07 が pytest を前提にしている |
| 整形・静的検査 | ruff | 1つの道具で整形と検査ができる |
| 型検査 | 入れない | 実装を小さく保つ |
| CI | GitHub Actions で単体テストと、記録した出力による契約テストを流す | 手元で流し忘れても、レビューの前に気づける。どちらも外部につながず速い |

### 8.2 新しい依存

| 用途 | 使うもの | 理由 |
|---|---|---|
| JSON Schema の検査（③ の D6(a)） | `jsonschema` | `--json-schema` に渡すスキーマ1つで検査でき、二重に管理しない |
| RSS・Atom の解析 | `feedparser` | 日付の形の揺れ、RSS と Atom の違いを吸収できる。外部の XML を読むので、外部実体を解決しないことを実装の前に確かめる |
| HTTP | 標準ライブラリ（`urllib.request`） | 依存を増やさない。タイムアウト・再試行は自作の小さな層で包む |

- SQLite は標準の `sqlite3`、設定ファイルは標準の `tomllib` で扱う

### 8.3 テスト

| 種類 | 置き場所 | いつ流すか | 内容 |
|---|---|---|---|
| 単体 | `tests/unit/` | 常に（`pytest`、CI でも流す） | 期間の計算、設定の検証、D1〜D4・D6、無害化（report-templates.md 0.3・4.5 のテストデータ）、テンプレートの出力、重複判定（GitHub は偽の実装に差し替える）、秘匿情報の検出 |
| 契約 | `tests/contract/` | 記録した出力のテストは常に（CI でも流す）。実際の CLI を呼ぶものは last30days-skill を更新したとき | agent JSON（`schema_version`）の形の検証（D-01）。記録した出力でのテストは常に流し（CI でも流す）、実際の CLI を呼ぶものはマーカー `network` で分ける |
| AI の回帰 | `tests/llm/` | プロンプト・CLI を変えたとき、または使われたモデルが変わったとき、運用者が手で（マーカー `llm`）。CI では流さない | D-07 の攻撃文のテスト。攻撃文を埋め込んだ入力 JSON（フィクスチャ）を、② ④ と同じ呼び出しの形（`agent/` の境界）で実際の AI に直接渡す |

- `pyproject.toml` の pytest の設定で、`llm` と `network` を既定で外す
- 外部サービス（GitHub・Claude Code）は、`publish/`・`agent/` の境界で偽の実装に差し替えてテストする

### 8.4 実装で決める細部

- ruff の規則の範囲
- worker のベースイメージ（`python:3.14-slim` など）と uv の版
- agent のイメージのベースと Claude Code の版
