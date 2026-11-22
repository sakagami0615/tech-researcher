"""週次の ② の出力の読み取りと、新しいテーマの番号の付与（data-model.md 3.6・4.3、D-18）。"""

import sqlite3

import pytest

from tech_researcher.agent.weekly_output import (
    THEME_STATUSES,
    ThemeEntry,
    assign_theme_ids,
    parse_weekly_output,
)
from tech_researcher.db.history import next_theme_id
from tech_researcher.db.schema import open_db

OUTPUT = {
    "themes": [
        {
            "theme_id": "T0007",
            "name": "テーマ",
            "status": "rising",
            "change": "変化",
            "comparison": "比較",
            "item_ids": ["i1", "i1", "i2"],
            "past_ids": ["i9"],
        },
        {
            "theme_id": None,
            "name": "新しいテーマ",
            "status": "steady",
            "change": "変化",
            "comparison": "比較",
            "item_ids": [],
            "past_ids": ["i9", "i9"],
        },
    ],
    "tech_changes": [{"headline": "見出し", "description": "説明", "item_ids": ["i3"]}],
    "comparisons": [
        {
            "domain": "領域",
            "rows": [
                {"item_id": "i4", "product_name": None, "feature": "特徴", "use_case": "用途"},
                {"item_id": "i5", "product_name": "Foo", "feature": "特徴", "use_case": "用途"},
            ],
            "summary": "まとめ",
        }
    ],
    "watch": [{"name": "注目", "reason": "理由", "item_ids": ["i6"]}],
}


def theme(theme_id, name="t"):
    return ThemeEntry(theme_id, name, "rising", "変化", "比較", ("i1",), ())


def test_output_is_split_by_section_and_duplicates_in_an_entry_are_merged():
    out = parse_weekly_output(OUTPUT)
    assert [t.theme_id for t in out.themes] == ["T0007", None]
    assert out.themes[0].item_ids == ("i1", "i2")  # 項目の中の重複は1つにする
    assert out.themes[1].item_ids == () and out.themes[1].past_ids == ("i9",)
    assert out.tech_changes[0].headline == "見出し"
    assert [r.product_name for r in out.comparisons[0].rows] == [None, "Foo"]
    assert out.watch[0].item_ids == ("i6",)


def test_empty_sections_are_allowed():
    out = parse_weekly_output({"themes": [], "tech_changes": [], "comparisons": [], "watch": []})
    assert out.themes == out.tech_changes == out.comparisons == out.watch == ()


@pytest.mark.parametrize("output", [None, {}, {**OUTPUT, "extra": 1}])
def test_output_not_matching_schema_raises(output):
    with pytest.raises(ValueError):
        parse_weekly_output(output)


def test_theme_status_enum_matches_the_schema():
    bad = {**OUTPUT, "themes": [{**OUTPUT["themes"][0], "status": THEME_STATUSES[0] + "x"}]}
    with pytest.raises(ValueError):
        parse_weekly_output(bad)


def test_new_themes_get_consecutive_numbers_in_output_order():
    themes = [theme(None, "a"), theme("T0007", "b"), theme(None, "c")]
    result = assign_theme_ids(themes, "T0012")
    assert [t.theme_id for t in result] == ["T0012", "T0007", "T0013"]
    assert [t.name for t in result] == ["a", "b", "c"]


def test_no_new_theme_keeps_numbers_and_input_is_not_changed():
    themes = [theme("T0001")]
    assert assign_theme_ids(themes, "T0005") == themes
    new = [theme(None)]
    assign_theme_ids(new, "T0001")
    assert new[0].theme_id is None


def test_numbering_starts_from_next_theme_id(tmp_path):
    conn: sqlite3.Connection = open_db(tmp_path / "db.sqlite")
    first = next_theme_id(conn)
    assert first == "T0001"
    result = assign_theme_ids([theme(None), theme(None)], first)
    assert [t.theme_id for t in result] == ["T0001", "T0002"]
