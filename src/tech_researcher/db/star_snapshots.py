"""GitHub のリポジトリの Star 数の記録：star_snapshots（data-model.md 3.4・3.5、D-13）。

いまは監視リスト（過去に Search・言及で候補になったリポジトリ）を引く問い合わせだけを持つ。
routes_json は、その測定の候補を見つけた経路（search・mention・watch）の JSON の配列。
測定の書き込みは、Star 数を取る処理（GraphQL）と一緒に足す。
"""

import sqlite3
from datetime import date, timedelta

# 監視リストに数える経路。watch だけの行は数えない（数えると、一度候補になったリポジトリが
# 毎日の測定で行を足し続け、監視リストから外れないため）
WATCH_SOURCE_ROUTES = ("search", "mention")


def recent_repos(conn: sqlite3.Connection, target: date, days: int) -> tuple[str, ...]:
    """対象日 target の前の days 日（target−days〜target−1）に、Search か言及の経路で
    候補になって測ったリポジトリ（`owner/repo`、小文字）を、名前の順で返す（3.4 の監視リスト）。

    target の日の行は見ない。同じ対象の再実行・ドライランで、最初の実行が書いた行によって
    監視リストが変わらないようにするため。
    """
    if days < 1:
        raise ValueError(f"days は1以上にする: {days}")
    start = target - timedelta(days=days)
    marks = ", ".join("?" * len(WATCH_SOURCE_ROUTES))
    return tuple(
        repo
        for (repo,) in conn.execute(
            "SELECT DISTINCT s.repo FROM star_snapshots AS s"
            " WHERE s.target >= ? AND s.target < ? AND EXISTS ("
            f"SELECT 1 FROM json_each(s.routes_json) WHERE value IN ({marks}))"
            " ORDER BY s.repo",
            (start.isoformat(), target.isoformat(), *WATCH_SOURCE_ROUTES),
        )
    )
