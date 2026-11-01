from datetime import UTC, date, datetime

import pytest

from tech_researcher.collect.aggregate import aggregate
from tech_researcher.collect.period_filter import in_period, published_before_period
from tech_researcher.collect.records import Event, Record
from tech_researcher.periods import JST, daily_target

TARGET = daily_target(date(2026, 10, 24), 1)  # JST 10/23 0:00〜10/24 0:00
IN = datetime(2026, 10, 23, 12, 0, tzinfo=JST)
BEFORE = datetime(2026, 10, 22, 23, 59, tzinfo=JST)
AFTER = datetime(2026, 10, 24, 0, 0, tzinfo=JST)


def record(events, published_at=None, url="https://e.example/a", link_url=None, precision=None):
    return Record(
        source_id="s",
        url=url,
        link_url=link_url,
        title="t",
        published_precision=precision or ("datetime" if published_at else "unknown"),
        fetched_at=AFTER,
        events=tuple(events),
        published_at=published_at,
    )


def item(*records):
    (only,) = aggregate(records)
    return only


@pytest.mark.parametrize("at, expected", [(IN, True), (BEFORE, False), (AFTER, False)])
def test_published_uses_half_open_period(at, expected):
    assert in_period(item(record([Event("published", at)], at)), TARGET) is expected


def test_released_uses_period():
    assert in_period(item(record([Event("released", IN)], IN)), TARGET)


def test_posted_counts_when_published_is_unknown():
    assert in_period(item(record([Event("posted", IN)])), TARGET)


def test_old_article_posted_on_aggregator_is_dropped():
    # RSS で期間より前に公開された記事が、HN に期間内に投稿された（D-13）
    rss = record([Event("published", BEFORE)], BEFORE)
    hn = record(
        [Event("posted", IN)],
        url="https://news.ycombinator.com/item?id=1",
        link_url="https://e.example/a",
    )
    assert not in_period(item(rss, hn), TARGET)


def test_record_without_events_is_dropped():
    assert not in_period(item(record([])), TARGET)


def test_threshold_spike_uses_period_and_rescues_old_article():
    events = [Event("published", BEFORE), Event("attention_spike", IN, "threshold")]
    assert in_period(item(record(events, BEFORE)), TARGET)


@pytest.mark.parametrize(
    "event, expected",
    [
        (Event("new_repo", datetime(2026, 10, 23, 6, 0, tzinfo=JST)), True),
        (Event("new_repo", datetime(2026, 10, 24, 5, 59, tzinfo=JST)), True),
        (Event("new_repo", datetime(2026, 10, 23, 5, 59, tzinfo=JST)), False),
        (Event("attention_spike", datetime(2026, 10, 24, 6, 0, tzinfo=JST), "star_growth"), False),
        (Event("attention_spike", datetime(2026, 10, 24, 1, 0, tzinfo=JST), "feed_entry"), True),
    ],
)
def test_detected_events_use_spike_window(event, expected):
    # 対象日 6:00〜翌日 6:00（JST）
    assert in_period(item(record([event])), TARGET) is expected


@pytest.mark.parametrize("day, expected", [(21, False), (22, True), (23, True), (24, False)])
def test_date_only_articles_use_utc_dates(day, expected):
    # 対象日 10/23 は UTC の 10/22・10/23 を窓に入るとみなす（D-16）
    at = datetime(2026, 10, day, tzinfo=UTC)
    assert (
        in_period(item(record([Event("published", at)], at, precision="date")), TARGET) is expected
    )


@pytest.mark.parametrize(
    "published_at, precision, expected",
    [
        (IN, "datetime", False),
        (BEFORE, "datetime", True),
        (datetime(2026, 10, 22, tzinfo=UTC), "date", False),  # UTC の T-1 は窓の中
        (datetime(2026, 10, 21, tzinfo=UTC), "date", True),
        (None, None, False),
    ],
)
def test_published_before_period(published_at, precision, expected):
    events = [Event("published", published_at)] if published_at else [Event("posted", IN)]
    it = item(record(events, published_at, precision=precision))
    assert published_before_period(it, TARGET) is expected
