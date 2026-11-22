"""週次の材料と比較対象を引く問い合わせ（data-model.md 3.8・4.3、D-17・D-18）。

比較の窓の過去の観測は、過去の週の週次の items（今週と同じ作り方の母集団）から引く。週次の items が
ない週は「データなし」として窓から外す。Star の増分は日次の測定の差を合計する（D-13）。
読むだけで、書かない（読み取り専用の接続でも使える）。
"""

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from tech_researcher.db.items import item_ref
from tech_researcher.db.star_snapshots import StarSnapshot, load_star_snapshot


def compare_week_starts(target: date, weeks: int) -> list[date]:
    """比較の窓の週（対象週の月曜）。target の前の weeks 週を、新しい順に並べる。"""
    return [target - timedelta(weeks=n) for n in range(1, weeks + 1)]


def weeks_with_items(conn: sqlite3.Connection, candidates: list[date]) -> list[date]:
    """週次の items がある週だけを、candidates の順のまま返す（ない週は「データなし」）。"""
    if not candidates:
        return []
    marks = ", ".join("?" * len(candidates))
    found = {
        row[0]
        for row in conn.execute(
            f"SELECT DISTINCT target FROM items WHERE cadence = 'weekly' AND target IN ({marks})",
            [d.isoformat() for d in candidates],
        )
    }
    return [d for d in candidates if d.isoformat() in found]


@dataclass(frozen=True)
class PastObservation:
    """比較の窓の過去の観測（週次の items の1行）。item_id は `i{主キー}`、week は対象週の月曜。"""

    item_id: str
    week: date
    source_id: str
    kind: str
    title: str
    url: str  # items の canonical_url。D1 の許可集合に使う（出典の URL は ID からコードが付ける）


def past_observations(conn: sqlite3.Connection, weeks: list[date]) -> list[PastObservation]:
    """weeks（週次の items がある週）の観測を、新しい週から主キーの順に返す。"""
    if not weeks:
        return []
    marks = ", ".join("?" * len(weeks))
    rows = conn.execute(
        "SELECT item_id, target, json_extract(sources_json, '$[0].source_id'), kind, title,"
        f" canonical_url FROM items WHERE cadence = 'weekly' AND target IN ({marks})"
        " ORDER BY target DESC, item_id",
        [d.isoformat() for d in weeks],
    )
    return [
        PastObservation(item_ref(item_id), date.fromisoformat(week), source_id, kind, title, url)
        for item_id, week, source_id, kind, title, url in rows
    ]


def period_star_gains(conn: sqlite3.Connection, first: date, end: date) -> dict[str, int]:
    """リポジトリ → 期間の Star の増分。日次の増分（対象日の測定 − 前日の測定）を合計する。

    対象日が first 以上 end 未満の日のうち、前日の測定もある日だけを足す（前日の記録がない日は
    記録だけで、増分にしない。D-13）。測定のある日がなければ入らない。
    """
    rows = conn.execute(
        "SELECT a.repo, sum(a.stars_total - b.stars_total) FROM star_snapshots a"
        " JOIN star_snapshots b ON b.repo = a.repo AND b.target = date(a.target, '-1 day')"
        " WHERE a.target >= ? AND a.target < ? GROUP BY a.repo",
        (first.isoformat(), end.isoformat()),
    )
    return dict(rows)


def latest_star_snapshots(
    conn: sqlite3.Connection, first: date, end: date
) -> dict[str, StarSnapshot]:
    """リポジトリ → 対象日が first 以上 end 未満の中で、最後の日の測定（週次の急上昇の判定用）。"""
    latest: dict[str, StarSnapshot] = {}
    day = first
    while day < end:
        latest.update(load_star_snapshot(conn, day))  # 新しい日が前の日を上書きする
        day += timedelta(days=1)
    return latest
