from datetime import UTC, date, datetime

from tech_researcher.collect.aggregate import aggregate
from tech_researcher.collect.holds import (
    ALREADY_FEATURED,
    SHOWN_PREVIOUS_DAY,
    hold_reasons,
    strip_featured,
)
from tech_researcher.collect.records import Attention, Event, GitHubRepo, Record
from tech_researcher.periods import JST, daily_target, weekly_target

TARGET = daily_target(date(2026, 10, 24), 1)  # JST 10/23 0:00〜10/24 0:00
IN = datetime(2026, 10, 23, 12, 0, tzinfo=JST)
BEFORE = datetime(2026, 10, 1, 12, 0, tzinfo=JST)
FETCHED = datetime(2026, 10, 23, 21, 0, tzinfo=UTC)
URL = "https://e.example/a"


def item(events, url=URL, published_at=None, precision="unknown"):
    record = Record(
        source_id="s",
        url=url,
        title="t",
        published_precision=precision,
        fetched_at=FETCHED,
        events=tuple(events),
        published_at=published_at,
    )
    return aggregate([record])[0]


def holds(items, featured=(), shown=()):
    return hold_reasons(items, TARGET, set(featured), set(shown))


def test_featured_item_with_only_spike_events_is_held():
    spike = Event("attention_spike", IN, "feed_entry")
    assert holds([item([spike])], featured={URL}) == {URL: ALREADY_FEATURED}
    assert holds([item([Event("new_repo", IN)])], featured={URL}) == {URL: ALREADY_FEATURED}


def test_featured_item_with_a_published_event_in_period_is_not_held():
    events = [Event("published", IN), Event("attention_spike", IN, "feed_entry")]
    assert holds([item(events, published_at=IN, precision="datetime")], featured={URL}) == {}


def test_item_not_featured_is_not_held():
    assert holds([item([Event("attention_spike", IN, "feed_entry")])]) == {}


def test_featured_old_article_published_before_period_is_held_for_its_spike_only():
    # 公開は期間より前。窓に入る出来事は急上昇だけ
    events = [Event("published", BEFORE), Event("attention_spike", IN, "feed_entry")]
    assert holds([item(events, published_at=BEFORE, precision="datetime")], featured={URL}) == {
        URL: ALREADY_FEATURED
    }


def test_shown_previous_day_applies_only_to_date_only_judgment():
    date_only = datetime(2026, 10, 22, tzinfo=UTC)  # last30days-skill の UTC の日付
    approx = item([Event("published", date_only)], published_at=date_only, precision="date")
    assert holds([approx], shown={URL}) == {URL: SHOWN_PREVIOUS_DAY}
    assert holds([approx]) == {}  # 前日に載せていなければ渡す
    exact = item([Event("published", IN)], published_at=IN, precision="datetime")
    assert holds([exact], shown={URL}) == {}  # 時刻まで分かる記事は除かない


def test_date_only_item_with_a_precise_event_is_not_held():
    date_only = datetime(2026, 10, 22, tzinfo=UTC)
    rss = Record(
        source_id="rss",
        url=URL,
        title="t",
        published_precision="datetime",
        fetched_at=FETCHED,
        events=(Event("published", IN),),
        published_at=IN,
    )
    hn = Record(
        source_id="hackernews",
        url=URL,
        title="t",
        published_precision="date",
        fetched_at=FETCHED,
        events=(Event("published", date_only),),
        published_at=date_only,
    )
    assert holds(aggregate([rss, hn]), shown={URL}) == {}


def test_weekly_holds_nothing():
    spike = Event("attention_spike", IN, "feed_entry")
    result = hold_reasons([item([spike])], weekly_target(date(2026, 10, 26), 7), {URL}, {URL})
    assert result == {}


def test_strip_featured_removes_spike_events_and_star_delta_but_keeps_others():
    repo = GitHubRepo("a/b", BEFORE, 1000, 300)
    record = Record(
        source_id="github_repos",
        url=URL,
        title="t",
        published_precision="datetime",
        fetched_at=FETCHED,
        events=(Event("released", IN), Event("attention_spike", IN, "star_growth")),
        published_at=IN,
        attention=(Attention("stars_total", 1000, IN), Attention("star_delta_24h", 300, IN)),
        github=repo,
    )
    (stripped,) = [strip_featured(i) for i in aggregate([record])]
    assert [e.type for e in stripped.events] == ["released"]
    assert [e.type for e in stripped.sources[0].events] == ["released"]
    assert [a.metric for a in stripped.attention] == ["stars_total"]
    assert stripped.github.star_delta_24h is None and stripped.github.stars_total == 1000
    assert stripped.sources[0].github.star_delta_24h is None
