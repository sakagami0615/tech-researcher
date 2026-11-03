"""GitHub のリポジトリの Star 数の記録：star_snapshots（data-model.md 3.4・3.5、D-13）。

いまは監視リスト（過去に候補になったリポジトリ）を引く問い合わせだけを持つ。
測定の書き込みは、Star 数を取る処理（GraphQL）と一緒に足す。
"""

import sqlite3
from datetime import date, timedelta


def recent_repos(conn: sqlite3.Connection, target: date, days: int) -> tuple[str, ...]:
    """対象日 target の前の days 日（target−days〜target−1）の間に初めて測ったリポジトリ
    （`owner/repo`、小文字）を、名前の順で返す（3.4 の監視リスト）。

    「直近に行があるか」で引くと、監視リストのリポジトリも毎日測って行ができるので、
    一度候補になったリポジトリがいつまでも外れない。そのため初めて測った日で引き、
    候補になってから days 日で外す。Search・言及でまた見つかれば、その経路で候補に入る。
    target の日の行は見ない。同じ対象の再実行・ドライランで、最初の実行が書いた行によって
    監視リストが変わらないようにするため。
    """
    if days < 1:
        raise ValueError(f"days は1以上にする: {days}")
    start = target - timedelta(days=days)
    return tuple(
        repo
        for (repo,) in conn.execute(
            "SELECT repo FROM star_snapshots WHERE target < ? GROUP BY repo"
            " HAVING min(target) >= ? ORDER BY repo",
            (target.isoformat(), start.isoformat()),
        )
    )
