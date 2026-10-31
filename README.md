# tech-researcher

技術情報を集めて、日本語の日次・週次レポートを GitHub Issue に公開するツールです。仕様は [docs/spec/requirements.md](docs/spec/requirements.md) から読めます。

## 開発の準備

[uv](https://docs.astral.sh/uv/) を使います。Python 3.14 は uv が用意します。

```sh
uv sync
```

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
