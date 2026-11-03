import json
from datetime import UTC, date, datetime, timedelta

import pytest

from tech_researcher.collect.records import Attention
from tech_researcher.db.feed_snapshots import (
    SnapshotEntry,
    fetch_recorded,
    load_feed_snapshot,
    record_feed_snapshot,
)
from tech_researcher.db.runs import SourceFetch, record_source_fetch, start_run
from tech_researcher.db.schema import open_db
from tech_researcher.periods import Target, daily_target

SOURCE = "rss:zenn-trend"
T = date(2026, 10, 23)
FETCHED = datetime(2026, 10, 23, 21, 0, 5, tzinfo=UTC)  # JST 10-24 6:00:05


@pytest.fixture
def conn(tmp_path):
    conn = open_db(tmp_path / "tech-researcher.sqlite3")
    yield conn
    conn.close()


def entry(url, rank=1, fetched_at=FETCHED, attention=()):
    return SnapshotEntry(url, rank, attention, fetched_at)


def rows(conn):
    return conn.execute(
        "SELECT source_id, target, canonical_url, rank, attention_json, fetched_at"
        " FROM feed_snapshots ORDER BY rank"
    ).fetchall()


def test_records_all_entries(conn):
    bookmarks = (Attention("hatena_bookmarks", 120.0, FETCHED),)
    entries = [
        entry("https://a.example/1", 1, attention=bookmarks),
        entry("https://a.example/2", 2),
    ]
    assert record_feed_snapshot(conn, SOURCE, T, entries) is True
    assert rows(conn) == [
        (
            SOURCE,
            "2026-10-23",
            "https://a.example/1",
            1,
            json.dumps(
                [
                    {
                        "metric": "hatena_bookmarks",
                        "value": 120.0,
                        "measured_at": "2026-10-23T21:00:05Z",
                    }
                ]
            ).replace(" ", ""),
            "2026-10-23T21:00:05Z",
        ),
        (SOURCE, "2026-10-23", "https://a.example/2", 2, "[]", "2026-10-23T21:00:05Z"),
    ]
    assert load_feed_snapshot(conn, SOURCE, T) == {
        "https://a.example/1": FETCHED,
        "https://a.example/2": FETCHED,
    }


def test_keeps_first_successful_fetch_of_the_day(conn):
    """同じ（情報源、対象日）の再実行は書かない。遅い時刻の取得で 6:00 の記録を崩さない（3.7）。"""
    record_feed_snapshot(conn, SOURCE, T, [entry("https://a.example/1")])
    later = FETCHED + timedelta(hours=3)
    assert not record_feed_snapshot(conn, SOURCE, T, [entry("https://a.example/9", 1, later)])
    assert load_feed_snapshot(conn, SOURCE, T) == {"https://a.example/1": FETCHED}
    # ほかの情報源・ほかの対象日は別の記録
    assert record_feed_snapshot(conn, "rss:qiita-popular", T, [entry("https://a.example/9")])
    assert record_feed_snapshot(conn, SOURCE, T + timedelta(days=1), [entry("https://a.example/9")])


def test_empty_feed_leaves_no_rows(conn):
    assert record_feed_snapshot(conn, SOURCE, T, []) is False
    assert rows(conn) == []
    assert load_feed_snapshot(conn, SOURCE, T) == {}


def test_rank_starts_at_one():
    with pytest.raises(ValueError, match="rank"):
        entry("https://a.example/1", rank=0)


def fetch(status):
    return SourceFetch(
        source_id=SOURCE,
        status=status,
        attempts=1,
        record_count=0,
        in_period_count=0,
        fetched_at=FETCHED,
        error_category="http_5xx" if status == "failed" else None,
    )


def daily_run(conn, run_id, target_day, status):
    start_run(conn, run_id, daily_target(target_day + timedelta(days=1), 1), FETCHED)
    record_source_fetch(conn, run_id, fetch(status))


@pytest.mark.parametrize(
    ("status", "expected"),
    [("ok", True), ("no_results", True), ("failed", False), ("skipped", False)],
)
def test_fetch_recorded_distinguishes_empty_success_from_failure(conn, status, expected):
    daily_run(conn, "20261024-060000-abcd", T, status)
    assert fetch_recorded(conn, SOURCE, T) is expected


def test_fetch_recorded_needs_a_daily_run_of_that_day_and_source(conn):
    assert not fetch_recorded(conn, SOURCE, T)  # 運用の初日
    daily_run(conn, "20261023-060000-abcd", T - timedelta(days=1), "ok")
    # 週次の実行の取得は数えない（対象のキーが同じ日付でも）
    weekly = Target("weekly", T, daily_target(T + timedelta(days=1), 1).period)
    start_run(conn, "20261026-070000-abcd", weekly, FETCHED)
    record_source_fetch(conn, "20261026-070000-abcd", fetch("ok"))
    assert not fetch_recorded(conn, SOURCE, T)
    assert not fetch_recorded(conn, "rss:other", T - timedelta(days=1))


def test_fetch_recorded_counts_any_successful_run_and_observations(conn):
    daily_run(conn, "20261024-060000-abcd", T, "failed")
    daily_run(conn, "obs-20261024-060000-abcd", T, "ok")
    assert fetch_recorded(conn, SOURCE, T)
