from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from tech_researcher.collect.aggregate import aggregate
from tech_researcher.collect.records import Event, GitHubRepo, Record, SourceResult
from tech_researcher.db.runs import start_run
from tech_researcher.db.schema import open_db
from tech_researcher.periods import daily_target, weekly_target
from tech_researcher.stages.collect_store import (
    GitHubFetch,
    in_period_counts,
    inclusion_event,
    item_kind,
    item_row,
    save_collect,
    source_fetch,
)

TARGET = daily_target(date(2026, 10, 24), 1)  # 2026-10-23 0:00〜10-24 0:00（JST）
AT = datetime(2026, 10, 23, 21, 0, tzinfo=UTC)
IN = datetime(2026, 10, 23, 3, 0, tzinfo=UTC)  # JST 12:00
LATER = datetime(2026, 10, 23, 5, 0, tzinfo=UTC)
SPIKE = datetime(2026, 10, 23, 22, 0, tzinfo=UTC)  # JST 10-24 7:00（Star の窓の外）
OLD = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)


def rec(url, *, source="rss:a", published=IN, precision="datetime", **kw):
    events = kw.pop("events", (Event("published", published),) if published else ())
    return Record(
        source_id=source,
        url=url,
        title=kw.pop("title", "タイトル"),
        published_precision=precision if published else "unknown",
        fetched_at=AT,
        events=events,
        published_at=published,
        **kw,
    )


def item(*records):
    [result] = aggregate(records)
    return result


@pytest.mark.parametrize(
    ("url", "kw", "kind"),
    [
        ("https://github.com/o/r", {}, "repo"),
        ("https://example.com/x", {"github": GitHubRepo("o/r", OLD, 1)}, "repo"),
        ("https://github.com/o/r/releases/tag/v1", {}, "release"),
        ("https://example.com/r", {"events": (Event("released", IN),)}, "release"),
        ("https://github.com/o/r/issues/1", {}, "post"),
        ("https://github.com/o/r/pull/2", {}, "post"),
        ("https://news.ycombinator.com/item?id=1", {}, "post"),
        ("https://github.com/o/r/blob/main/README.md", {}, "article"),
        ("https://example.com/blog/1", {}, "article"),
    ],
)
def test_item_kind(url, kw, kind):
    assert item_kind(item(rec(url, **kw))) == kind


def test_representative_prefers_datetime_then_fills_blanks():
    """代表は時刻まで分かる公開日時の出典。空のタイトル・抜粋はほかの出典で埋める。"""
    hn = rec(
        "https://example.com/a",
        source="hackernews",
        published=datetime(2026, 10, 23, tzinfo=UTC),
        precision="date",
        title="HN のタイトル",
        excerpt="HN の抜粋",
        posted_at=LATER,
    )
    rss = rec(
        "https://example.com/a", title="", primary_text="本文", primary_text_origin="rss_full"
    )
    hatena = rec(
        "https://b.hatena.ne.jp/entry/s/example.com/a",
        source="rss:hatena",
        published=None,
        link_url="https://example.com/a",
        posted_at=IN,
        events=(Event("posted", IN),),
    )
    row = item_row(item(hn, rss, hatena), TARGET)
    assert (row.published_at, row.published_precision) == (IN, "datetime")
    assert row.title == "HN のタイトル"  # 代表（RSS）のタイトルが空
    assert row.excerpt == "HN の抜粋"
    assert row.posted_at == IN  # 出典の中で最も早い投稿
    assert row.primary_text == "本文"
    assert row.inclusion == Event("published", IN)


def test_unknown_publication_uses_first_source():
    row = item_row(
        item(rec("https://example.com/p", published=None, events=(Event("posted", IN),))),
        TARGET,
    )
    assert (row.published_at, row.published_precision, row.kind) == (None, "unknown", "article")
    assert row.inclusion == Event("posted", IN)


def test_inclusion_event_priority_and_window():
    """公開・リリース → 新しい OSS → 急上昇 → 投稿の順。窓の外の出来事は選ばない。"""
    spike = Event("attention_spike", LATER, "threshold")
    star = Event("attention_spike", SPIKE, "star_growth")  # 前日の記録との比較で判定済み
    stale = Event("attention_spike", OLD, "threshold")  # 窓（対象期間）の外
    posted = Event("posted", IN)
    old = rec(
        "https://example.com/o", published=OLD, events=(Event("published", OLD), posted, spike)
    )
    assert inclusion_event(item(old), TARGET) == spike  # 公開は期間の外
    new = rec("https://example.com/n", events=(posted, Event("published", LATER), spike))
    assert inclusion_event(item(new), TARGET) == Event("published", LATER)
    two = rec("https://example.com/t", events=(Event("published", LATER), Event("released", IN)))
    assert inclusion_event(item(two), TARGET) == Event("released", IN)  # 同じ順位は早い方
    out = rec("https://example.com/x", published=OLD, events=(Event("published", OLD), stale))
    with pytest.raises(ValueError, match="判定の窓"):
        inclusion_event(item(out), TARGET)
    starred = rec("https://example.com/s", published=OLD, events=(Event("published", OLD), star))
    assert inclusion_event(item(starred), TARGET) == star  # 測定の時刻が窓の外でも判定済み


def test_source_fetch_states():
    records = (rec("https://example.com/1"), rec("https://example.com/2"))
    ok = source_fetch(SourceResult("rss:a", records, 2), AT, 1)
    assert (ok.status, ok.error_category, ok.record_count, ok.in_period_count) == ("ok", None, 2, 1)
    empty = source_fetch(SourceResult("rss:a", (), 1), AT, 0)
    assert (empty.status, empty.error_category) == ("no_results", None)
    failed = source_fetch(SourceResult("rss:a", (), 3, "HTTP 503"), AT, 0)
    assert (failed.status, failed.error_category, failed.attempts) == ("failed", "fetch_failed", 3)
    timeout = source_fetch(
        SourceResult("hackernews", (), 2, "上限", error_category="timeout"), AT, 0
    )
    assert timeout.error_category == "timeout"


@pytest.mark.parametrize(
    ("fetch", "expected"),
    [
        (GitHubFetch(3, 2), ("ok", None, 3)),
        (GitHubFetch(0, 1), ("no_results", None, 0)),
        # Search が上限で止まっても、測れた分があれば部分的な成功（ok）
        (GitHubFetch(2, 101, "上限", "budget"), ("ok", None, 2)),
        (GitHubFetch(0, 2, "401", "auth"), ("failed", "auth", 0)),
        (GitHubFetch(0, 0, "x", None), ("failed", "fetch_failed", 0)),
        (GitHubFetch(0, 0, skipped=True), ("skipped", None, 0)),
    ],
)
def test_github_fetch_states(fetch, expected):
    row = fetch.to_fetch(AT)
    assert (row.source_id, row.status, row.error_category, row.record_count) == (
        "github_repos",
        *expected,
    )
    assert row.in_period_count == 0  # 急上昇・新規の項目は #37 で足す
    assert fetch.failed == (expected[0] == "failed")


def test_in_period_counts():
    items = aggregate(
        [
            rec("https://example.com/1"),
            rec("https://example.com/1", source="rss:b"),
            rec("https://example.com/2"),
        ]
    )
    assert in_period_counts(items) == {"rss:a": 2, "rss:b": 1}


def test_save_collect_writes_items_only_for_daily(tmp_path):
    conn = open_db(tmp_path / "db.sqlite3")
    weekly = weekly_target(date(2026, 10, 26), 7)
    start_run(conn, "20261024-060000-abcd", TARGET, AT)
    start_run(conn, "20261026-070000-abcd", weekly, AT)
    items = [item(rec("https://example.com/1"))]
    fetch = source_fetch(SourceResult("rss:a", items[0].sources, 1), AT, 1)
    assert save_collect(conn, "20261024-060000-abcd", TARGET, [fetch], items) == ["i1"]
    assert save_collect(conn, "20261026-070000-abcd", weekly, [fetch], items) == []
    assert conn.execute("SELECT cadence FROM items").fetchall() == [("daily",)]
    assert conn.execute("SELECT count(*) FROM source_fetches").fetchone() == (2,)
    conn.close()


def test_item_row_keeps_hold_reason_empty():
    """掲載済み・前日の記事の除外（hold_reason）は #37 で足す。いまは全項目を渡す扱い。"""
    row = item_row(item(replace(rec("https://example.com/1"), excerpt=None)), TARGET)
    assert row.hold_reason is None and row.excerpt is None
