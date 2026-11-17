# agent：② 評価・要約 ④ 失敗時の調査で Claude Code を動かすコンテナ（D-04、structure.md 2.2・3.3）。
# 統括スクリプトが `docker compose run --rm -T agent agent-run <スキル名> -p …` で起動する
# （agent/invoke.py）。agent-run はスキルがイメージにあることを確かめてから claude を動かす。
# イメージに入れるのは Claude Code 本体と内側のスキルだけ。src/・config/・third_party/ と実行データは
# 入れない。内側のスキル（agent/skills/）は、作業ディレクトリの .claude/skills/ に
# 置く（プロンプトの先頭の /スキル名 で展開される。F15。コンテナでの読まれ方は 3.8 の5で確かめる）。
#
# ベースイメージはダイジェストで固定する（タグの付け替えで中身が変わらないように）。版を上げるときは
# タグとダイジェストを一緒に変える（docker buildx imagetools inspect <タグ> で調べる）。

# Claude Code の版を固定する（D-07 の回帰テストを、版を変えたときに流すため）。npm の stable の版。
# npm のパッケージから、ネイティブの実行ファイルだけを取り出し、機種ごとの sha256 と照らす
# （npm の登録元が返す integrity に加えて、こちらで固定した値でも確かめる）。第三者の
# インストールスクリプトは動かさない（--ignore-scripts。その機種向けのパッケージは入る）。
# 版を上げるときは、各機種のパッケージ（@anthropic-ai/claude-code-linux-x64・-arm64）の tgz の
# package/claude の sha256 を調べて、下の値を一緒に変える
FROM node:26.13.1-trixie-slim@sha256:e6bedef217677b276c591795e169840edd9096dbf13f29df8ef8d1856b8820bc AS install
ARG CLAUDE_CODE_VERSION=2.1.311
ARG CLAUDE_CODE_SHA256_AMD64=86dc1fcc69498159eaf15f446613e847010f9036bfbbdffd4c49350be8489ada
ARG CLAUDE_CODE_SHA256_ARM64=2e2496c9a8065df93acb0fca2d341f59a6b8777649b2e025ab8c9bcd5eef61f3
ARG TARGETARCH
RUN case "${TARGETARCH}" in \
        amd64) sha="${CLAUDE_CODE_SHA256_AMD64}" ;; \
        arm64) sha="${CLAUDE_CODE_SHA256_ARM64}" ;; \
        *) echo "対応していない機種: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && npm install --global --omit=dev --ignore-scripts --no-audit --no-fund \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && cp /usr/local/lib/node_modules/@anthropic-ai/claude-code/node_modules/@anthropic-ai/claude-code-linux-*/claude \
        /usr/local/bin/claude \
    && echo "${sha}  /usr/local/bin/claude" | sha256sum -c - \
    && test "$(/usr/local/bin/claude --version)" = "${CLAUDE_CODE_VERSION} (Claude Code)"

# 実行用のイメージには Node.js と npm を入れない（実行ファイルは単体で動く）
FROM debian:13.7-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=install /usr/local/bin/claude /usr/local/bin/claude
COPY --chmod=0755 docker/agent-run.sh /usr/local/bin/agent-run
# 書き込めるのは CLAUDE_CONFIG_DIR と一時フォルダだけ（compose.yaml で、ルートを読み取り専用にし、
# この2つを tmpfs にする）。自動更新と、自動更新・テレメトリ・エラー報告などの通信を止める（F10）。
# agent.env では上書きさせない（統括スクリプトが agent.env の変数名を確かめる。envfiles.py）
ENV HOME=/tmp/home \
    CLAUDE_CONFIG_DIR=/claude \
    CLAUDE_CODE_TMPDIR=/tmp \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
WORKDIR /app
COPY agent/skills/ /app/.claude/skills/
# 手元の umask によらず、動かす人（65534）がスキルを読めるようにする
RUN chmod -R a+rX /app/.claude
# root で動かさない。データ用フォルダをマウントしないので、運用者の UID に合わせる必要もない
USER 65534:65534
