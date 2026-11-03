"""GitHub のリポジトリの Star 数の記録：star_snapshots（data-model.md 3.4・3.5、D-13）。

いまは監視リスト（過去に候補になったリポジトリ）を引く問い合わせだけを持つ。
測定の書き込みは、Star 数を取る処理（GraphQL）と一緒に足す。
"""

import sqlite3
from datetime import date, timedelta


def recent_repos(conn: sqlite3.Connection, target: date, days: int) -> tuple[str, ...]:
    """対象日 target の前の days 日（target−days〜target−1）に測ったリポジトリ
    （`owner/repo`、小文字）を、名前の順で返す（3.4 の監視リスト）。

    target の日の行は見ない。同じ対象の再実行・ドライランで、最初の実行が書いた行によって
    監視リストが変わらないようにするため。
    """
    if days < 1:
        raise ValueError(f"days は1以上にする: {days}")
    start = target - timedelta(days=days)
    return tuple(
        repo
        for (repo,) in conn.execute(
            "SELECT DISTINCT repo FROM star_snapshots WHERE target >= ? AND target < ?"
            " ORDER BY repo",
            (start.isoformat(), target.isoformat()),
        )
    )
