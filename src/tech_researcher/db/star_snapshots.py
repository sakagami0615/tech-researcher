"""GitHub のリポジトリの Star 数の記録：star_snapshots（data-model.md 3.4・3.5、D-13）。

日次の測定ごとに、リポジトリの累計 Star 数を（リポジトリ、対象日）で残す。同じ対象日の行が
すでにあれば書かず、最初の測定を残す（3.7 の `INSERT OR IGNORE`。一意制約の衝突だけを
無視したいので、`ON CONFLICT DO NOTHING` で書く。CHECK・NOT NULL の違反は例外にする）。
再実行が遅い時刻に動いても、6:00 の測定どうしの差が崩れないため。
週次の実行とドライランは書かない（書かないのは呼ぶ側が決める。ドライランは DB を読み取り専用で
開く）。
routes_json は、その測定の候補を見つけた経路（search・mention・watch）の JSON の配列。
監視リスト（過去に Search・言及で候補になったリポジトリ）もここから引く。
"""

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from tech_researcher.config import GITHUB_REPO_NAME
from tech_researcher.db.runs import utc_text
from tech_researcher.db.schema import transaction

# 候補を見つけた経路（D-13）。routes はこの順に並べる
ROUTES = ("search", "mention", "watch")
# 監視リストに数える経路。watch だけの行は数えない（数えると、一度候補になったリポジトリが
# 毎日の測定で行を足し続け、監視リストから外れないため）
WATCH_SOURCE_ROUTES = ("search", "mention")


def recent_repos(conn: sqlite3.Connection, target: date, days: int) -> tuple[str, ...]:
    """対象日 target の前の days 日（target−days〜target−1）に、Search か言及の経路で
    候補になって測ったリポジトリ（`owner/repo`、小文字）を、名前の順で返す（3.4 の監視リスト）。

    target の日の行は見ない。同じ対象の再実行・ドライランで、最初の実行が書いた行によって
    監視リストが変わらないようにするため。
    `owner/repo` の形でない行は返さない（API のパスに使うため）。
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
        if GITHUB_REPO_NAME.fullmatch(repo)
    )


@dataclass(frozen=True)
class StarSnapshot:
    """1リポジトリの1回の測定。repo は `owner/repo`（小文字）、routes は ROUTES の順の経路。"""

    repo: str
    stars_total: int
    created_at: datetime
    measured_at: datetime
    routes: tuple[str, ...]

    def __post_init__(self):
        if not GITHUB_REPO_NAME.fullmatch(self.repo):
            raise ValueError(f"repo が owner/repo（小文字）の形でない: {self.repo!r}")
        if self.created_at.tzinfo is None or self.measured_at.tzinfo is None:
            raise ValueError("タイムゾーンのない日時は持たない")
        if self.stars_total < 0:
            raise ValueError("stars_total は0以上にする")
        if not self.routes or tuple(r for r in ROUTES if r in self.routes) != self.routes:
            raise ValueError(
                f"routes は ROUTES の順の、重ならない1つ以上の経路にする: {self.routes}"
            )


def record_star_snapshots(
    conn: sqlite3.Connection, target: date, snapshots: Iterable[StarSnapshot]
) -> int:
    """対象日 target の測定を書き、書いた行の数を返す。

    同じ（リポジトリ、対象日）か（リポジトリ、測定時刻）の行がすでにあれば、その行は書かない
    （最初の測定を残す。3.7）。中でトランザクションを始めるので、`transaction` の中からは呼べない。
    """
    rows = [
        (
            s.repo,
            target.isoformat(),
            utc_text(s.measured_at),
            s.stars_total,
            utc_text(s.created_at),
            json.dumps(list(s.routes)),
        )
        for s in snapshots
    ]
    with transaction(conn):
        before = conn.total_changes
        conn.executemany(
            "INSERT INTO star_snapshots (repo, target, measured_at, stars_total,"
            " created_at, routes_json) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            rows,
        )
        return conn.total_changes - before


def load_star_snapshot(conn: sqlite3.Connection, target: date) -> dict[str, StarSnapshot]:
    """対象日 target の測定を、リポジトリ → 測定で返す。記録がなければ空（3.5 の T・T−1 の行）。

    形の崩れた行（`owner/repo` の形でない・知らない経路）は返さない。
    """
    found: dict[str, StarSnapshot] = {}
    for repo, measured_at, stars, created_at, routes_json in conn.execute(
        "SELECT repo, measured_at, stars_total, created_at, routes_json FROM star_snapshots"
        " WHERE target = ?",
        (target.isoformat(),),
    ):
        try:
            found[repo] = StarSnapshot(
                repo,
                stars,
                datetime.fromisoformat(created_at),
                datetime.fromisoformat(measured_at),
                tuple(json.loads(routes_json)),
            )
        except TypeError, ValueError:
            continue
    return found
