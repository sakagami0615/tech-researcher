from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tech_researcher.collect.popularity import (
    first_appearances,
    judge_feed_entries,
    mark_feed_entries,
    mark_thresholds,
    snapshot_entries,
)
from tech_researcher.collect.records import Attention, Event, Record, SourceResult
from tech_researcher.db.feed_snapshots import SnapshotEntry, load_feed_snapshot
from tech_researcher.db.readonly import open_readonly
from tech_researcher.db.runs import SourceFetch, record_source_fetch, start_run
from tech_researcher.db.schema import open_db
from tech_researcher.periods import daily_target, weekly_target

POPULAR = "rss:zenn-trend"
TARGET = daily_target(date(2026, 10, 24), 1)  # 対象日 10-23。実行は 10-24 6:00（JST）
PREV = TARGET.key - timedelta(days=1)
FETCHED = datetime(2026, 10, 23, 21, 0, 5, tzinfo=UTC)  # JST 10-24 6:00:05
PREV_FETCHED = FETCHED - timedelta(days=1)
OLD = datetime(2026, 10, 1, tzinfo=UTC)


def record(url, source_id=POPULAR, link_url=None, attention=(), posted_at=None, published_at=OLD):
    events = []
    if published_at:
        events.append(Event("published", published_at))
    if posted_at:
        events.append(Event("posted", posted_at))
    return Record(
        source_id=source_id,
        url=url,
        link_url=link_url,
        title="t",
        published_precision="datetime" if published_at else "unknown",
        fetched_at=FETCHED,
        events=tuple(events),
        published_at=published_at,
        posted_at=posted_at,
        attention=tuple(attention),
    )


def spikes(r):
    return [(e.basis, e.at) for e in r.events if e.type == "attention_spike"]


# ---- 取得記録にする全件 ----


def test_snapshot_entries_keep_feed_order_and_use_aggregate_key():
    hatena = record(
        "https://b.hatena.ne.jp/entry/s/a.example/2",
        link_url="https://a.example/2/",
        attention=[Attention("hatena_bookmarks", 50.0, FETCHED)],
    )
    entries = snapshot_entries(
        [record("https://a.example/1?utm_source=x"), hatena, record("https://A.example/1")]
    )
    assert entries == (
        SnapshotEntry("https://a.example/1", 1, (), FETCHED),
        SnapshotEntry("https://a.example/2", 2, hatena.attention, FETCHED),
    )


# ---- 初登場の判定（前日の記録と比べる）----


def test_first_appearances():
    current = {"https://a.example/1": FETCHED, "https://a.example/2": FETCHED}
    assert first_appearances(current, {"https://a.example/1"}) == {"https://a.example/2": FETCHED}
    assert first_appearances(current, set()) == current


def test_mark_feed_entries_adds_event_at_observed_fetch():
    observed = FETCHED - timedelta(minutes=5)
    marked = mark_feed_entries(
        [record("https://a.example/1"), record("https://a.example/2/")],
        {"https://a.example/2": observed},
    )
    assert [spikes(r) for r in marked] == [[], [("feed_entry", observed)]]


# ---- 基準超えの判定（前日の記録を使わない）----

THRESHOLDS = {"hn_points": 100, "hatena_bookmarks": 100}


@pytest.mark.parametrize(
    ("metric", "value", "expected"),
    [
        ("hn_points", 100.0, True),  # 基準以上
        ("hn_points", 99.0, False),
        ("hatena_bookmarks", 250.0, True),
        ("qiita_likes", 1000.0, False),  # 基準を書いていない metric は判定しない
    ],
)
def test_mark_thresholds(metric, value, expected):
    posted = FETCHED - timedelta(hours=10)
    r = record(
        "https://a.example/1", attention=[Attention(metric, value, FETCHED)], posted_at=posted
    )
    (marked,) = mark_thresholds([r], THRESHOLDS)
    # 日時は基準を超えた投稿の日時（公開日時ではない）
    assert spikes(marked) == ([("threshold", posted)] if expected else [])


def test_threshold_uses_date_only_publish_as_post_date():
    """last30days-skill の HN は、投稿の日付を日付だけの公開日時として持つ（data-model.md 2.8）。"""
    day = datetime(2026, 10, 23, tzinfo=UTC)
    r = replace(
        record("https://a.example/1", attention=[Attention("hn_points", 500.0, FETCHED)]),
        published_at=day,
        published_precision="date",
        events=(Event("published", day),),
    )
    assert spikes(mark_thresholds([r], THRESHOLDS)[0]) == [("threshold", day)]


def test_threshold_needs_a_post_time():
    # 時刻まで分かる公開日時（Qiita API の記事など）は投稿の日時として使わない
    r = record("https://a.example/1", attention=[Attention("hn_points", 500.0, FETCHED)])
    assert spikes(mark_thresholds([r], THRESHOLDS)[0]) == []


def test_threshold_without_settings_marks_nothing():
    r = record(
        "https://a.example/1", attention=[Attention("hn_points", 500.0, FETCHED)], posted_at=FETCHED
    )
    assert mark_thresholds([r], {}) == [r]


# ---- 取得記録と判定（DB）----


@pytest.fixture
def conn(tmp_path):
    conn = open_db(tmp_path / "tech-researcher.sqlite3")
    yield conn
    conn.close()


def previous_day(conn, status="ok", urls=()):
    """前日（対象日 T−1）の日次の実行の取得結果と取得記録を書く。"""
    run_id = "20261023-060000-abcd"
    start_run(conn, run_id, daily_target(TARGET.key, 1), PREV_FETCHED)
    record_source_fetch(
        conn,
        run_id,
        SourceFetch(
            source_id=POPULAR,
            status=status,
            attempts=1,
            record_count=len(urls),
            in_period_count=0,
            fetched_at=PREV_FETCHED,
            error_category="http_5xx" if status == "failed" else None,
        ),
    )
    if urls:
        conn.executemany(
            "INSERT INTO feed_snapshots VALUES (?, ?, ?, ?, '[]', ?)",
            [
                (POPULAR, PREV.isoformat(), u, n, "2026-10-22T21:00:05Z")
                for n, u in enumerate(urls, 1)
            ],
        )


TODAY = (record("https://a.example/stay"), record("https://a.example/new"))


def judge(conn, records=TODAY, *, target=TARGET, write=True, error=None, popular=(POPULAR,)):
    result = SourceResult(POPULAR, () if error else tuple(records), 1, error)
    return judge_feed_entries([result], set(popular), target, conn, write=write)


def test_judges_new_entries_against_previous_day(conn):
    previous_day(conn, urls=["https://a.example/stay", "https://a.example/gone"])
    result = judge(conn)
    assert [spikes(r) for r in result.records] == [[], [("feed_entry", FETCHED)]]
    assert result.unjudged == ()
    # 期間で絞る前の全件を、今日の記録として残す
    assert load_feed_snapshot(conn, POPULAR, TARGET.key) == {
        "https://a.example/stay": FETCHED,
        "https://a.example/new": FETCHED,
    }


def test_previous_empty_success_judges_everything_as_new(conn):
    """前日のフィードが空でも取得には成功している（no_results）ので判定する。"""
    previous_day(conn, status="no_results")
    result = judge(conn)
    assert [spikes(r) for r in result.records] == [[("feed_entry", FETCHED)]] * 2
    assert result.unjudged == ()


@pytest.mark.parametrize("status", ["failed", None])
def test_without_previous_record_only_records(conn, status):
    """前日の取得が失敗した日と運用の初日は判定せず、記録だけ行う（D-13）。"""
    if status:
        previous_day(conn, status=status)
    result = judge(conn)
    assert result.records == TODAY
    assert result.unjudged == (POPULAR,)
    assert set(load_feed_snapshot(conn, POPULAR, TARGET.key)) == {
        "https://a.example/stay",
        "https://a.example/new",
    }


def test_rerun_judges_with_first_fetch_of_the_day(conn):
    """同じ対象日の再実行は、最初の取得の記録で判定し、記録も書き換えない。"""
    previous_day(conn, urls=["https://a.example/stay"])
    judge(conn, records=TODAY[:1])
    later = [
        record("https://a.example/stay"),
        record("https://a.example/late"),  # 最初の取得の後に載った
    ]
    result = judge(conn, records=later)
    assert [spikes(r) for r in result.records] == [[], []]
    assert set(load_feed_snapshot(conn, POPULAR, TARGET.key)) == {"https://a.example/stay"}


def readonly(conn):
    (path,) = [row[2] for row in conn.execute("PRAGMA database_list")]
    return open_readonly(Path(path))


def test_dry_run_reads_but_does_not_write(conn):
    """ドライランは本番と同じ読み取り専用の接続で、前日の記録を読んで判定する（D-22）。"""
    previous_day(conn, urls=["https://a.example/stay"])
    ro = readonly(conn)
    result = judge(ro, write=False)
    ro.close()
    assert [spikes(r) for r in result.records] == [[], [("feed_entry", FETCHED)]]
    assert load_feed_snapshot(conn, POPULAR, TARGET.key) == {}


def test_dry_run_after_production_uses_recorded_fetch(conn):
    previous_day(conn, urls=["https://a.example/stay"])
    judge(conn, records=TODAY[:1])  # 本番の実行
    ro = readonly(conn)
    result = judge(ro, write=False)
    ro.close()
    assert [spikes(r) for r in result.records] == [[], []]


def test_without_db_nothing_is_judged():
    result = judge(None)
    assert result.records == TODAY
    assert result.unjudged == (POPULAR,)


def test_failed_fetch_is_neither_recorded_nor_unjudged(conn):
    previous_day(conn, urls=["https://a.example/stay"])
    result = judge(conn, error="再試行を使い切った")
    assert result == type(result)((), ())
    assert load_feed_snapshot(conn, POPULAR, TARGET.key) == {}


def test_weekly_and_new_feeds_are_not_judged(conn):
    previous_day(conn, status="no_results")
    weekly = judge(conn, target=weekly_target(date(2026, 10, 26), 7))
    assert weekly.records == TODAY
    assert weekly.unjudged == ()
    new_feed = judge(conn, popular=())
    assert new_feed.records == TODAY
    assert new_feed.unjudged == ()
    assert conn.execute("SELECT count(*) FROM feed_snapshots").fetchone() == (0,)
