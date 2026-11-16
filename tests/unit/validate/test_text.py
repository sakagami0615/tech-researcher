import pytest

from tech_researcher.agent.daily import DailyEntry
from tech_researcher.validate.text import check_daily_html, check_daily_lengths, check_daily_links


def entry(headline="見出し", points=("要点",), importance="理由", period_event=None, depth="full"):
    return DailyEntry(
        ("i1",), "i1", None, "ai", 1, depth, headline, points, importance, period_event
    )


def only_violation(check, **kw):
    bad = entry(**kw)
    kept, excluded = check([bad])
    return kept == [] and [x.entry for x in excluded] == [bad]


def test_clean_text_is_kept():
    entries = [entry(), entry(period_event="期間内に Star が急増", depth="brief")]
    for check in (check_daily_links, check_daily_html, check_daily_lengths):
        assert check(entries) == (entries, [])


def field_kwargs(field, text):
    return {field: (text,) if field == "points" else text}


FIELDS = ["headline", "points", "importance", "period_event"]


@pytest.mark.parametrize(
    "text",
    [
        "https://example.com",
        "see HTTP://EXAMPLE.COM/x",
        "ftp://example.com",
        "www.example.com",
        "(www.example.com)",
        "[記事](x)",
        "<https://example.com>",
        "<http://x>",
    ],
)
@pytest.mark.parametrize("field", FIELDS)
def test_links_are_excluded_in_every_field(text, field):
    assert only_violation(check_daily_links, **field_kwargs(field, text))


@pytest.mark.parametrize("text", ["www は略称", "awww.x", "[注] 補足", "http の話", "C#"])
def test_ordinary_text_is_not_a_link(text):
    kept, excluded = check_daily_links([entry(headline=text)])
    assert excluded == [] and len(kept) == 1


@pytest.mark.parametrize(
    "text",
    [
        "![alt](x)",
        "![",
        "<img src=x>",
        "<IMG",
        "<a href=x>",
        "<!-- コメント -->",
        "<!--",
        "<!DOCTYPE html>",
        "<?xml?>",
        "<div>",
        "</div>",
        "<script>alert(1)</script>",
        "Result<T, E> を返す",
    ],
)
@pytest.mark.parametrize("field", FIELDS)
def test_html_is_excluded_in_every_field(text, field):
    assert only_violation(check_daily_html, **field_kwargs(field, text))


@pytest.mark.parametrize("text", ["a < b", "x<3", "a > b", "感嘆！[1]", "<3"])
def test_ordinary_text_is_not_html(text):
    kept, _ = check_daily_html([entry(headline=text)])
    assert len(kept) == 1


def test_reason_does_not_repeat_the_text():
    _, excluded = check_daily_links([entry(points=("ok", "https://evil.example"))])
    assert "evil" not in excluded[0].reason and "points" in excluded[0].reason


@pytest.mark.parametrize(
    ("field", "depth", "limit"),
    [
        ("headline", "full", 80),
        ("points", "full", 160),
        ("importance", "full", 200),
        ("importance", "brief", 120),
        ("period_event", "full", 120),
    ],
)
def test_length_boundary_is_twice_the_guideline(field, depth, limit):
    def check(n):
        return check_daily_lengths([entry(depth=depth, **field_kwargs(field, "あ" * n))])

    assert check(limit)[1] == []  # 2倍ちょうどは残す
    assert len(check(limit + 1)[1]) == 1  # 2倍を超えたら除外


def test_length_counts_after_trimming_whitespace():
    assert check_daily_lengths([entry(headline=" " + "あ" * 80 + "\n")])[1] == []


def test_each_point_is_measured_separately():
    assert check_daily_lengths([entry(points=("あ" * 160, "あ" * 160))])[1] == []
    assert only_violation(check_daily_lengths, points=("あ", "あ" * 161))


def test_point_count_boundary():
    assert check_daily_lengths([entry(points=("要",) * 6)])[1] == []
    assert only_violation(check_daily_lengths, points=("要",) * 7)
