"""DB 全体の情報：meta（data-model.md 3.3）。いまは投稿先のリポジトリだけ。

試験用のリポジトリで動かす実行は、別の `TR_DATA_DIR`（別の DB）を使う（structure.md 3.6）。
DB は投稿先を1つ記録し、設定の `GITHUB_ISSUE_REPO` と食い違えば止める。試験用の記録が本番の DB に
残って、本番のレポートが「作成済み」と判定されるのを防ぐため。
"""

import sqlite3

from tech_researcher.config import ConfigError
from tech_researcher.db.schema import transaction

_ISSUE_REPO = "issue_repo"


def check_issue_repo(conn: sqlite3.Connection, repo: str) -> None:
    """記録した投稿先が repo と違えば ConfigError。記録がなければ何もしない。"""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (_ISSUE_REPO,)).fetchone()
    if row is not None and row[0] != repo.lower():
        raise ConfigError(
            f"DB に記録した投稿先 {row[0]} が、設定の投稿先 {repo.lower()} と違う"
            "（試験用のリポジトリは別の TR_DATA_DIR で動かす）"
        )


def record_issue_repo(conn: sqlite3.Connection, repo: str) -> None:
    """投稿先を記録する。すでに記録があれば書き換えない（食い違いは check_issue_repo が止める）。

    中でトランザクションを始めるので、`transaction` の中からは呼べない。
    """
    with transaction(conn):
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (_ISSUE_REPO, repo.lower())
        )
