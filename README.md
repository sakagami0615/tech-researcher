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
- 指示ファイル（`SKILL.md`・`AGENTS.md`・`CLAUDE.md` など）や対象外のファイルが残っていると、スクリプトは失敗します
- `third_party/` の中は編集しません。固定する SHA を変えるときは、`git -C third_party/last30days fetch`・`git -C third_party/last30days checkout <SHA>`・`git add third_party/last30days` の順に実行して参照を更新し、`bin/setup-third-party.sh` を流してからコミットします

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
