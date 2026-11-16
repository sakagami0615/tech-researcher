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


def test_link_of_another_item_is_excluded():
    bad = entry(["i1"], "i2-l1")
    kept, excluded = check_daily_sources([bad], ITEMS)
    assert kept == [] and [x.entry for x in excluded] == [bad]


def test_bad_url_in_merged_source_is_excluded():
    merged = item("https://aws.example/post")
    record = merged.sources[0]
    bad_record = Record(**{**record.__dict__, "url": "https://u:p@evil.example/"})
    items = {"i1": Item(merged.url, (record, bad_record), (), (), (), (), None)}
    kept, excluded = check_daily_sources([entry(["i1"])], items)
    assert kept == [] and len(excluded) == 1


def test_duplicate_id_is_judged_after_other_checks():
    """period_event 欠落で除外される先の項目が、後の項目の ID を取らない（③ の検査の並び）。"""
    from tech_researcher.stages.daily import _check_entries

    data = {
        "items": [
            {"id": "i1", "needs_period_event": True, "links": []},
            {"id": "i2", "needs_period_event": False, "links": []},
        ]
    }

    def make(ids, period_event):
        return DailyEntry(
            ids, None, None, "ai", 1, "full", "見出し", ("要点",), "理由", period_event
        )

    a, b = make(("i1",), None), make(("i1", "i2"), "出来事")
    kept, excluded = _check_entries([a, b], data, ITEMS)
    assert kept == [b]
    assert [(c, x.entry) for c, x in excluded] == [("missing_period_event", a)]
