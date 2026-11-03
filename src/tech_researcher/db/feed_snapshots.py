"""人気型フィードの取得記録：feed_snapshots（data-model.md 3.4・3.5・3.7、D-13）。

日次の取得ごとに、期間で絞る前のフィードの全件を（情報源、対象日、記事の URL）で残す。
同じ（情報源、対象日）の行がすでにあれば書かず、最初に成功した取得を残す。再実行が遅い時刻に
動いても、6:00 の取得どうしで比べる判定が崩れないため。
フィードが空だった取得は行を残さない。「取得した」ことは source_fetches の状態（`no_results`）で
分かるので、前日の記録の有無は source_fetches で判定する（取得の失敗と空の成功を区別する）。
"""

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from tech_researcher.collect.records import Attention
from tech_researcher.db.runs import utc_text
from tech_researcher.db.schema import transaction


@dataclass(frozen=True)
class SnapshotEntry:
    """フィードに載っていた1件。url は正規化した記事の URL、rank はフィードの中の順位（1から）。"""

    url: str
    rank: int
    attention: tuple[Attention, ...]
    fetched_at: datetime

    def __post_init__(self):
        if self.rank < 1:
            raise ValueError("rank は1以上にする")


def record_feed_snapshot(
    conn: sqlite3.Connection, source_id: str, target: date, entries: Iterable[SnapshotEntry]
) -> bool:
    """取得記録を書く。同じ（情報源、対象日）の行がすでにあれば何も書かずに False を返す（3.7）。

    中でトランザクションを始めるので、`transaction` の中からは呼べない。
    """
    rows = [
        (
            source_id,
            target.isoformat(),
            e.url,
            e.rank,
            json.dumps(
                [
                    {"metric": a.metric, "value": a.value, "measured_at": utc_text(a.measured_at)}
                    for a in e.attention
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            utc_text(e.fetched_at),
        )
        for e in entries
    ]
    with transaction(conn):
        if load_feed_snapshot(conn, source_id, target):
            return False
        conn.executemany(
            "INSERT INTO feed_snapshots (source_id, target, canonical_url, rank, attention_json,"
            " fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
    return bool(rows)


def load_feed_snapshot(
    conn: sqlite3.Connection, source_id: str, target: date
) -> dict[str, datetime]:
    """その対象日の取得記録を、記事の URL → 取得時刻で返す。記録がなければ空。"""
    return {
        url: datetime.fromisoformat(fetched_at)
        for url, fetched_at in conn.execute(
            "SELECT canonical_url, fetched_at FROM feed_snapshots"
            " WHERE source_id = ? AND target = ?",
            (source_id, target.isoformat()),
        )
    }


def fetch_recorded(conn: sqlite3.Connection, source_id: str, target: date) -> bool:
    """対象日の日次の実行で、その情報源の取得に成功した記録（`ok`・`no_results`）があるか（3.5）。

    観測（obs-）の実行も日次の実行として数える。
    """
    row = conn.execute(
        "SELECT 1 FROM source_fetches AS f JOIN runs AS r ON r.run_id = f.run_id"
        " WHERE r.cadence = 'daily' AND r.target = ? AND f.source_id = ?"
        " AND f.status IN ('ok', 'no_results') LIMIT 1",
        (target.isoformat(), source_id),
    ).fetchone()
    return row is not None
