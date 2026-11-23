---
name: tr-weekly
description: tech-researcher の週次の処理を実行する（運用者が手動で起動する）
disable-model-invocation: true
argument-hint: "[--dry-run]"
allowed-tools: Bash(${CLAUDE_PROJECT_DIR}/bin/tr-run weekly) Bash(${CLAUDE_PROJECT_DIR}/bin/tr-run weekly --dry-run)
---

tech-researcher の週次の処理を、共通の入口 `bin/tr-run` で実行します。

1. 引数（`$ARGUMENTS`）を見て、次のどちらかのコマンドを Bash で1回だけ実行する。
   - 引数が空：`${CLAUDE_PROJECT_DIR}/bin/tr-run weekly`（本番）
   - 引数が `--dry-run`：`${CLAUDE_PROJECT_DIR}/bin/tr-run weekly --dry-run`（ドライラン）
   - それ以外の引数：実行せずに断る。
2. 処理は長いので、Bash の道具がタイムアウトしてバックグラウンドに移ることがある。そのときは、完了を待ってその出力を受け取る（この受け取りのためだけに、バックグラウンドの出力を読んでよい）。コマンドを実行し直さない。
3. 出力された要約（JSON 1行）を、そのまま運用者に報告する。`result`（`report`・`rejected`・`error`・`dry_run`）、`issue_url`、`output_dir`（ドライランの出力先）、`failed_sources`・`partial_sources` があれば、それも一言で添える。失敗のときは `failed_stage`・`error_category` も添える。
4. 要約（JSON）が出ないときは、終了コードと標準エラーの文をそのまま報告する（環境・設定の誤りや、同じ種類の実行が動いているときなど）。

守ること：

- 上のコマンドのほかは実行しない。
- 出力にないファイル（`bin/tr-run`・`src/`・`agent/skills/` の内側のスキル・出力先のフォルダの中身など）は開かない。
- 失敗したときも、原因の調査や再実行はしない。
