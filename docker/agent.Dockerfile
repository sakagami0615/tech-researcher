# agent：② 評価・要約 ④ 失敗時の調査で Claude Code を動かすコンテナ（D-04、structure.md 2.2・3.3）。
# 統括スクリプトが `docker compose run --rm -T agent claude -p …` で起動する（agent/invoke.py）。
# イメージに入れるのは Claude Code 本体だけ。src/・config/・third_party/ と実行データは入れない。
# 内側のスキル（agent/skills/）は、それを作る Issue（#22）で /app/.claude/skills/ にコピーする。

# Claude Code の版を固定する（D-07 の回帰テストを、版を変えたときに流すため）。npm の stable の版。
# npm の登録の整合性（integrity）で検証したパッケージから、ネイティブの実行ファイルだけを取り出す。
# 第三者のインストールスクリプトは動かさない（--ignore-scripts。その機種向けのパッケージは入る）
FROM node:26.13.1-trixie-slim AS install
ARG CLAUDE_CODE_VERSION=2.1.311
RUN npm install --global --omit=dev --ignore-scripts --no-audit --no-fund \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && cp /usr/local/lib/node_modules/@anthropic-ai/claude-code/node_modules/@anthropic-ai/claude-code-linux-*/claude \
        /usr/local/bin/claude \
    && test "$(/usr/local/bin/claude --version)" = "${CLAUDE_CODE_VERSION} (Claude Code)"

# 実行用のイメージには Node.js と npm を入れない（実行ファイルは単体で動く）
FROM debian:13.7-slim
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=install /usr/local/bin/claude /usr/local/bin/claude
# 書き込めるのは CLAUDE_CONFIG_DIR と一時フォルダだけ（compose.yaml で、ルートを読み取り専用にし、
# この2つを tmpfs にする）。自動更新と、自動更新・テレメトリ・エラー報告などの通信を止める（F10）。
# agent.env では上書きさせない（統括スクリプトが agent.env の変数名を確かめる。envfiles.py）
ENV HOME=/tmp/home \
    CLAUDE_CONFIG_DIR=/claude \
    CLAUDE_CODE_TMPDIR=/tmp \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
WORKDIR /app
# root で動かさない。データ用フォルダをマウントしないので、運用者の UID に合わせる必要もない
USER 65534:65534
