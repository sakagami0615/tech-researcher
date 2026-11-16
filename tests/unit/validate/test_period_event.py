import pytest

from tech_researcher.agent.daily import DailyEntry
from tech_researcher.validate.period_event import check_period_events

DATA = {
    "items": [
        {"id": "i1", "needs_period_event": True},
        {"id": "i2", "needs_period_event": False},
    ]
}


def entry(item_ids, period_event):
    return DailyEntry(
        tuple(item_ids), None, None, "ai", 1, "full", "見出し", ("要点",), "理由", period_event
    )


def test_entry_with_period_event_is_kept():
    entries = [entry(["i1"], "Star が急増した"), entry(["i2"], None), entry(["i2"], "あっても可")]
    kept, excluded = check_period_events(entries, DATA)
    assert kept == entries and excluded == []


@pytest.mark.parametrize("missing", [None, "", "  \n"])
def test_missing_period_event_is_excluded(missing):
    good = entry(["i2"], None)
    bad = entry(["i1"], missing)
    kept, excluded = check_period_events([bad, good], DATA)
    assert kept == [good]
    assert [x.entry for x in excluded] == [bad] and "period_event" in excluded[0].reason


def test_any_item_needing_it_makes_it_required():
    kept, excluded = check_period_events([entry(["i2", "i1"], None)], DATA)
    assert kept == [] and len(excluded) == 1
