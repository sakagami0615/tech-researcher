import pytest

from tech_researcher.agent.daily import DailyEntry
from tech_researcher.validate.ids import check_daily_ids

DATA = {
    "items": [
        {"id": "i1", "links": [{"id": "i1-l1"}]},
        {"id": "i2", "links": []},
        {"id": "i3", "links": [{"id": "i3-l1"}]},
    ]
}


def entry(item_ids, primary_item_id=None, primary_link_id=None, headline="見出し"):
    return DailyEntry(
        tuple(item_ids),
        primary_item_id,
        primary_link_id,
        "ai",
        1,
        "full",
        headline,
        ("要点",),
        "理由",
        None,
    )


def test_valid_entries_are_kept():
    entries = [entry(["i1", "i2"], "i1"), entry(["i3"], primary_link_id="i3-l1")]
    kept, excluded = check_daily_ids(entries, DATA)
    assert kept == entries and excluded == []


@pytest.mark.parametrize(
    "bad, reason",
    [
        (entry([]), "item_ids が空"),
        (entry(["i9"]), "入力にない ID"),
        (entry(["i2"], "i1"), "primary_item_id"),
        (entry(["i2"], primary_link_id="i9-l1"), "primary_link_id"),
        (entry(["i2"], primary_link_id="https://evil.example"), "primary_link_id"),
    ],
)
def test_violating_entry_is_excluded_with_reason(bad, reason):
    good = entry(["i3"])
    kept, excluded = check_daily_ids([bad, good], DATA)
    assert kept == [good]
    assert [e.entry for e in excluded] == [bad] and reason in excluded[0].reason


def test_duplicate_ids_keep_first_entry():
    first, second = entry(["i1"], headline="先"), entry(["i2", "i1"], headline="後")
    kept, excluded = check_daily_ids([first, second], DATA)
    assert kept == [first] and excluded[0].entry == second
    assert "重複" in excluded[0].reason


def test_excluded_entry_does_not_block_later_ids():
    bad, later = entry(["i1", "i9"]), entry(["i1"])
    kept, _ = check_daily_ids([bad, later], DATA)
    assert kept == [later]


def test_link_of_another_item_is_allowed():
    # 仕様は「入力の候補リンクにある」。自項目のリンクに限らない
    kept, _ = check_daily_ids([entry(["i2"], primary_link_id="i1-l1")], DATA)
    assert len(kept) == 1
