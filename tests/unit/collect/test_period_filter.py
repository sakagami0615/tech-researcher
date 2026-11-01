from datetime import date, datetime

import pytest

from tech_researcher.collect.aggregate import aggregate
from tech_researcher.collect.period_filter import in_period
from tech_researcher.collect.records import Event, Record
from tech_researcher.periods import JST, daily_target

PERIOD = daily_target(date(2026, 10, 24), 1).period  # JST 10/23 0:00〜10/24 0:00
IN = datetime(2026, 10, 23, 12, 0, tzinfo=JST)
BEFORE = datetime(2026, 10, 22, 23, 59, tzinfo=JST)
AFTER = datetime(2026, 10, 24, 0, 0, tzinfo=JST)


def record(events, published_at=None, url="https://e.example/a", link_url=None):
    return Record(
        source_id="s",
        url=url,
        link_url=link_url,
        title="t",
        published_precision="datetime" if published_at else "unknown",
        fetched_at=AFTER,
        events=tuple(events),
        published_at=published_at,
    )


def item(*records):
    (only,) = aggregate(records)
    return only


@pytest.mark.parametrize("at, expected", [(IN, True), (BEFORE, False), (AFTER, False)])
def test_published_uses_half_open_period(at, expected):
    assert in_period(item(record([Event("published", at)], at)), PERIOD) is expected


def test_released_uses_period():
    assert in_period(item(record([Event("released", IN)], IN)), PERIOD)


def test_posted_counts_when_published_is_unknown():
    assert in_period(item(record([Event("posted", IN)])), PERIOD)


def test_old_article_posted_on_aggregator_is_dropped():
    # RSS で期間より前に公開された記事が、HN に期間内に投稿された（D-13）
    rss = record([Event("published", BEFORE)], BEFORE)
    hn = record(
        [Event("posted", IN)],
        url="https://news.ycombinator.com/item?id=1",
        link_url="https://e.example/a",
    )
    assert not in_period(item(rss, hn), PERIOD)


def test_record_without_events_is_dropped():
    assert not in_period(item(record([])), PERIOD)


def test_unsupported_event_is_an_error():
    with pytest.raises(ValueError):
        in_period(item(record([Event("new_repo", IN)])), PERIOD)
