from datetime import UTC, datetime

from tech_researcher.agent.daily import DailyEntry
from tech_researcher.collect.records import Item, Record
from tech_researcher.validate.sources import check_daily_sources

NOW = datetime(2026, 10, 23, tzinfo=UTC)


def item(url, links=()):
    record = Record(
        source_id="rss:aws",
        url=url,
        title="t",
        published_precision="datetime",
        fetched_at=NOW,
        published_at=NOW,
        events=(),
    )
    return Item(url, (record,), (), (), tuple(links), (), None)


def entry(item_ids, primary_link_id=None):
    return DailyEntry(
        tuple(item_ids), None, primary_link_id, "ai", 1, "full", "見出し", (), "理由", None
    )


ITEMS = {
    "i1": item("https://aws.example/post", ["https://docs.example.com/a"]),
    "i2": item("https://blog.example.com/x", ["javascript:alert(1)", "ftp://files.example/a"]),
}


def test_urls_in_the_collected_set_are_kept():
    entries = [entry(["i1"], "i1-l1"), entry(["i2"])]
    kept, excluded = check_daily_sources(entries, ITEMS)
    assert kept == entries and excluded == []


def test_candidate_link_with_bad_shape_is_excluded():
    good = entry(["i1"], "i1-l1")
    for link_id in ("i2-l1", "i2-l2"):  # http・https 以外
        bad = entry(["i2"], link_id)
        kept, excluded = check_daily_sources([bad, good], ITEMS)
        assert kept == [good]
        assert [x.entry for x in excluded] == [bad] and "URL" in excluded[0].reason


def test_item_with_unreadable_url_is_excluded():
    items = {"i1": item("https://user:pw@evil.example/x"), "i2": ITEMS["i2"]}
    kept, excluded = check_daily_sources([entry(["i1"]), entry(["i2"])], items)
    assert [e.item_ids for e in kept] == [("i2",)]
    assert len(excluded) == 1
