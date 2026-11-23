---
name: tr-weekly
description: tech-researcher の週次の処理を実行する（運用者が手動で起動する）
disable-model-invocation: true
argument-hint: "[--dry-run]"
allowed-tools: Bash(${CLAUDE_PROJECT_DIR}/bin/tr-run weekly) Bash(${CLAUDE_PROJECT_DIR}/bin/tr-run weekly --dry-run)
---

tech-researcher の週次の処理を、共通の入口 `bin/tr-run` で実行します。

1. 引数（`$ARGUMENTS`）が `--dry-run` のときだけ、次のコマンドを Bash で1回実行する。引数が空なら、`--dry-run` を付けない。

   - 本番：`${CLAUDE_PROJECT_DIR}/bin/tr-run weekly`
   - ドライラン：`${CLAUDE_PROJECT_DIR}/bin/tr-run weekly --dry-run`

2. 処理は長いので、Bash の道具がタイムアウトしてバックグラウンドに移ることがある。そのときは、完了を待ってから出力を受け取る。コマンドを実行し直さない。
3. 出力された要約（JSON 1行）を、そのまま運用者に報告する。`result`（`report`・`rejected`・`error`・`dry_run`）、`issue_url`、`output_dir`（ドライランの出力先）、`failed_sources`・`partial_sources` があれば、それも一言で添える。

守ること：

- 上のコマンドのほかは実行しない。引数が `--dry-run` 以外なら、実行せずに断る。
- 出力にないファイル（`bin/tr-run`・`src/`・`agent/skills/` の内側のスキル・出力先のフォルダの中身など）は開かない。
- 失敗したときも、要約の `result`・`failed_stage`・`error_category` を報告するだけにし、原因の調査や再実行はしない。
