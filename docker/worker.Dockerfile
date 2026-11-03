# worker：① 収集 ③ 検証・検査 ⑤ 投稿を動かすコンテナ（D-04、structure.md 2章・3章）。
# 統括スクリプトが `docker compose run --rm -T worker <段階> …` で起動する。
# 設定（config/）と実行データ（${TR_DATA_DIR}）はイメージに入れず、compose.yaml でマウントする。
# ビルドの前に bin/setup-third-party.sh で last30days-skill を置く（D-01）。

FROM ghcr.io/astral-sh/uv:0.12.0 AS uv

FROM python:3.14.10-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /build
# 依存だけを先に入れ、コードの変更で依存の層を作り直さないようにする
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ src/
RUN uv sync --frozen --no-dev --no-editable

# 実行用のイメージには uv とビルドの作業場所を入れない
FROM python:3.14.10-slim
COPY --from=build /app/.venv /app/.venv
# last30days-skill は LICENSE と収集スクリプトだけを、同じ配置のままコピーする（D-01・D-02）
COPY third_party/last30days/LICENSE /app/third_party/last30days/LICENSE
COPY third_party/last30days/skills/last30days/scripts/ /app/third_party/last30days/skills/last30days/scripts/
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
# root で動かさない。データ用フォルダに書けるよう、compose.yaml で運用者の UID・GID に上書きする
USER 65534:65534
ENTRYPOINT ["tech-researcher"]
