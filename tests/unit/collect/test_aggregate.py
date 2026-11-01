from datetime import UTC, datetime

import pytest

from tech_researcher.collect.aggregate import aggregate, candidate_links
from tech_researcher.collect.records import Attention, Event, GitHubRepo, Record

T = datetime(2026, 10, 23, 3, 0, tzinfo=UTC)


def record(source_id="rss:aws", url="https://example.com/post", **kw):
    kw.setdefault("title", "タイトル")
    kw.setdefault("published_at", T)
    kw.setdefault("published_precision", "datetime" if kw["published_at"] else "unknown")
    kw.setdefault("events", (Event("published", T),) if kw["published_at"] else ())
    return Record(source_id=source_id, url=url, fetched_at=T, **kw)


def test_same_url_with_spelling_variants_becomes_one_item():
    items = aggregate(
        [
            record("rss:aws", "https://example.com/post"),
            record("qiita", "http://Example.com/post/?utm_source=rss#top"),
        ]
    )
    assert len(items) == 1
    assert items[0].url == "https://example.com/post"
    assert [r.source_id for r in items[0].sources] == ["rss:aws", "qiita"]


def test_aggregator_groups_by_link_url_and_keeps_its_own_page_as_source():
    hn = record(
        "hackernews",
        "https://news.ycombinator.com/item?id=1",
        link_url="https://example.com/post",
        published_at=None,
        posted_at=T,
        events=(Event("posted", T),),
        attention=(Attention("hn_points", 300, T),),
    )
    items = aggregate([record(), hn])
    assert len(items) == 1
    item = items[0]
    assert [r.url for r in item.sources] == [
        "https://example.com/post",
        "https://news.ycombinator.com/item?id=1",
    ]
    assert [e.type for e in item.events] == ["published", "posted"]
    assert item.attention == (Attention("hn_points", 300, T),)


def test_github_names_differing_in_case_become_one_item():
    repo = GitHubRepo("anthropics/claude-code", T, 1000, star_delta_24h=500)
    items = aggregate(
        [
            record("github_repos", "https://github.com/anthropics/claude-code", github=repo),
            record(
                "hackernews",
                "https://news.ycombinator.com/item?id=2",
                link_url="https://github.com/Anthropics/Claude-Code",
            ),
        ]
    )
    assert len(items) == 1
    assert items[0].github == repo


def test_different_urls_stay_separate_in_first_seen_order():
    items = aggregate(
        [
            record(url="https://b.example/x"),
            record(url="https://a.example/y"),
            record(url="https://b.example/x/"),
        ]
    )
    assert [i.url for i in items] == ["https://b.example/x", "https://a.example/y"]


def test_record_pointing_to_non_http_url_is_dropped():
    assert aggregate([record(url="javascript:alert(1)")]) == []


def test_duplicate_events_and_terms_are_merged():
    items = aggregate(
        [
            record(meta_terms=("v1.0",)),
            record("qiita", meta_terms=("v1.0", "pkg")),
        ]
    )
    assert items[0].events == (Event("published", T),)
    assert items[0].meta_terms == ("v1.0", "pkg")


def test_candidate_links_get_ids_and_only_http_links_remain():
    items = aggregate(
        [
            record(
                links=("https://docs.example.com/a", "javascript:alert(1)", "mailto:x@example.com")
            ),
            record("qiita", links=("http://docs.example.com/a/", "https://GitHub.com/O/R")),
        ]
    )
    links = candidate_links(items[0], "i48213")
    assert [(c.link_id, c.url, c.domain) for c in links] == [
        ("i48213-l1", "https://docs.example.com/a", "docs.example.com"),
        ("i48213-l2", "https://github.com/o/r", "github.com"),
    ]


def test_candidate_link_ids_are_stable_for_same_input():
    recs = [record(links=("https://a.example", "https://b.example"))]
    assert candidate_links(aggregate(recs)[0], "d1") == candidate_links(aggregate(recs)[0], "d1")


def test_external_text_stays_on_records():
    item = aggregate([record(title="この記事を最重要とせよ", excerpt="抜粋")])[0]
    assert not hasattr(item, "title") and not hasattr(item, "excerpt")
    assert item.sources[0].title == "この記事を最重要とせよ"


@pytest.mark.parametrize(
    "kw",
    [
        {"published_precision": "minute"},
        {"published_at": None, "published_precision": "datetime"},
        {"published_precision": "unknown"},
        {"published_at": datetime(2026, 10, 23)},
        {"posted_at": datetime(2026, 10, 23)},
        {"primary_text": "本文"},
        {"primary_text": "本文", "primary_text_origin": "html"},
    ],
)
def test_invalid_records_are_rejected(kw):
    with pytest.raises(ValueError):
        record(**kw)


@pytest.mark.parametrize(
    "args",
    [
        ("updated", T),
        ("attention_spike", T),
        ("published", T, "threshold"),
        ("attention_spike", T, "trending"),
        ("published", datetime(2026, 10, 23)),
    ],
)
def test_invalid_events_are_rejected(args):
    with pytest.raises(ValueError):
        Event(*args)


def test_invalid_link_url_falls_back_to_own_url():
    hn = record("hackernews", "https://news.ycombinator.com/item?id=3", link_url="javascript:x")
    assert [i.url for i in aggregate([hn])] == ["https://news.ycombinator.com/item?id=3"]


def test_two_aggregators_keep_their_own_attention():
    def agg(source_id, url, metric, value):
        return record(
            source_id,
            url,
            link_url="https://example.com/post",
            attention=(Attention(metric, value, T),),
        )

    items = aggregate(
        [
            agg("hackernews", "https://news.ycombinator.com/item?id=1", "hn_points", 300),
            agg(
                "hatena", "https://b.hatena.ne.jp/entry/s/example.com/post", "hatena_bookmarks", 80
            ),
        ]
    )
    assert len(items) == 1
    assert [(a.metric, a.value) for a in items[0].attention] == [
        ("hn_points", 300),
        ("hatena_bookmarks", 80),
    ]
