"""週次の ② の出力の検査（D-07 の D1〜D4・D6(b)、report-templates.md 2.4）。"""

from datetime import UTC, datetime

import pytest

from tech_researcher.agent.weekly_output import (
    ComparisonEntry,
    ComparisonRow,
    TechChangeEntry,
    ThemeEntry,
    WatchEntry,
    WeeklyOutput,
    assign_theme_ids,
)
from tech_researcher.collect.records import GitHubRepo, Item, Record
from tech_researcher.config import QuoteLimits
from tech_researcher.validate.weekly import CATEGORIES, check_weekly

NOW = datetime(2026, 10, 21, tzinfo=UTC)
LIMITS = QuoteLimits()


def item(url, title="t", excerpt=None, github=None, links=(), meta=()):
    record = Record(
        source_id="rss:a",
        url=url,
        title=title,
        published_precision="datetime",
        fetched_at=NOW,
        published_at=NOW,
        events=(),
        excerpt=excerpt,
    )
    return Item(url, (record,), (), (), tuple(links), tuple(meta), github)


REPO = GitHubRepo("owner/repo", NOW, 100)
ITEMS = {
    "i1": item("https://a.example.com/1", "FooDB 3.0 released"),
    "i2": item("https://a.example.com/2", "Bar tool"),
    "i3": item("https://a.example.com/3", "Baz"),
    "i4": item("https://github.com/owner/repo", "owner/repo", github=REPO),
    "i5": item("https://a.example.com/5", "Qux", links=["https://docs.example.com/q"]),
}
PAST_URLS = {"i90": "https://old.example.com/a", "i91": "https://old.example.com/b"}
DATA = {
    "items": [
        {"id": i, "untrusted": {"title": v.sources[0].title, "excerpt": None}}
        for i, v in ITEMS.items()
    ],
    "past_observations": [
        {"id": "i90", "untrusted": {"title": "past one"}},
        {"id": "i91", "untrusted": {"title": "past two"}},
    ],
    "past_themes": [
        {"theme_id": "T0001", "name": "a", "history": []},
        {"theme_id": "T0002", "name": "b", "history": []},
    ],
}


def theme(theme_id=None, item_ids=("i1",), past_ids=(), name="テーマ", **kw):
    fields = {"status": "rising", "change": "変化", "comparison": "比較", **kw}
    return ThemeEntry(theme_id, name, item_ids=tuple(item_ids), past_ids=tuple(past_ids), **fields)


def change(item_ids=("i1",), headline="見出し", description="説明"):
    return TechChangeEntry(headline, description, tuple(item_ids))


def row(item_id="i4", product_name=None, feature="特徴", use_case="用途"):
    return ComparisonRow(item_id, product_name, feature, use_case)


def comparison(*rows, domain="領域", summary="まとめ"):
    return ComparisonEntry(domain, tuple(rows), summary)


def watch(item_ids=("i1",), name="注目", reason="理由"):
    return WatchEntry(name, reason, tuple(item_ids))


def check(*, themes=(), changes=(), comparisons=(), watches=(), data=DATA, items=ITEMS):
    out = WeeklyOutput(tuple(themes), tuple(changes), tuple(comparisons), tuple(watches))
    return check_weekly(out, data, items, PAST_URLS, LIMITS)


def reasons(result):
    return [(x.section, x.category) for x in result.excluded]


def test_valid_output_is_kept_as_is():
    entries = dict(
        themes=[theme("T0001", past_ids=["i90"]), theme(None, ["i2"])],
        changes=[change()],
        comparisons=[comparison(row("i4"), row("i1", "FooDB"))],
        watches=[watch()],
    )
    result = check(**entries)
    assert result.excluded == () and result.published == 5
    assert result.output.themes == tuple(entries["themes"])
    assert all(v == "passed" for v in result.verification.values())
    assert set(CATEGORIES) <= set(result.verification)


def test_empty_sections_are_allowed():
    result = check()
    assert result.published == 0 and result.excluded == ()


# --- D6(b) ID ---


@pytest.mark.parametrize(
    "bad",
    [
        theme(item_ids=["i99"]),  # 今週の観測にない
        theme(item_ids=["i90"]),  # 過去の ID を item_ids に入れた
        theme(past_ids=["i1"]),  # 今週の ID を past_ids に入れた
        theme(item_ids=[], past_ids=["i77"]),  # 渡していない過去の観測
        theme(item_ids=[], past_ids=[]),  # どちらも空
        theme("T0099"),  # 渡していない theme_id
        theme("t0001"),
        theme("https://evil.example"),
    ],
)
def test_theme_with_bad_id_is_excluded(bad):
    good = theme("T0002", ["i2"])
    result = check(themes=[bad, good])
    assert result.output.themes == (good,)
    assert reasons(result) == [("theme", "invalid_id")]
    assert result.excluded[0].entry == bad  # AI の theme_id も残る（payload に入れる）
    assert result.verification["invalid_id"] == "failed"


def test_theme_without_this_weeks_items_is_kept_if_it_has_past_ids():
    """注目が落ち着いたテーマ：今週の観測がなく、過去の観測だけ。"""
    result = check(themes=[theme("T0001", item_ids=[], past_ids=["i90", "i91"])])
    assert result.published == 1 and result.excluded == ()


def test_known_theme_number_and_null_are_accepted():
    result = check(themes=[theme("T0001"), theme(None, ["i2"]), theme(None, ["i3"])])
    assert result.excluded == () and len(result.output.themes) == 3


def test_duplicate_theme_number_keeps_the_first_theme():
    first, second = theme("T0001", ["i1"], name="先"), theme("T0001", ["i2"], name="後")
    result = check(themes=[first, second])
    assert result.output.themes == (first,)
    assert reasons(result) == [("theme", "duplicate_id")]
    assert "theme_id" in result.excluded[0].reason


def test_several_new_themes_with_null_are_not_duplicates():
    result = check(themes=[theme(None, ["i1"]), theme(None, ["i2"]), theme(None, ["i3"])])
    assert result.excluded == ()


def test_theme_excluded_by_another_check_does_not_take_the_number():
    bad = theme("T0001", ["i1"], name="あ" * 100)  # 文字数で除外
    good = theme("T0001", ["i2"])
    result = check(themes=[bad, good])
    assert result.output.themes == (good,)
    assert reasons(result) == [("theme", "too_long")]


def test_duplicate_item_ids_are_refused_only_inside_a_section():
    a, b = theme(None, ["i1"], name="先"), theme(None, ["i1", "i2"], name="後")
    result = check(themes=[a, b])
    assert result.output.themes == (a,) and reasons(result) == [("theme", "duplicate_id")]
    # 別のセクションに同じ項目を載せてよい
    result = check(themes=[a], changes=[change(["i1"])], watches=[watch(["i1"])])
    assert result.excluded == () and result.published == 3


def test_duplicate_item_ids_in_tech_changes_and_watch_keep_the_first():
    result = check(
        changes=[change(["i1"], "先"), change(["i1", "i2"], "後")],
        watches=[watch(["i3"]), watch(["i3"])],
    )
    assert [c.headline for c in result.output.tech_changes] == ["先"]
    assert len(result.output.watch) == 1
    assert reasons(result) == [("tech_change", "duplicate_id"), ("watch", "duplicate_id")]


def test_past_ids_may_repeat_across_themes():
    result = check(themes=[theme(None, ["i1"], ["i90"]), theme(None, ["i2"], ["i90"])])
    assert result.excluded == ()


@pytest.mark.parametrize(
    "bad",
    [change(["i99"]), change([]), change(["i90"])],
)
def test_tech_change_with_bad_item_ids_is_excluded(bad):
    result = check(changes=[bad, change(["i2"])])
    assert reasons(result) == [("tech_change", "invalid_id")]
    assert len(result.output.tech_changes) == 1


@pytest.mark.parametrize("bad", [watch(["i99"]), watch([]), watch(["i91"])])
def test_watch_with_bad_item_ids_is_excluded(bad):
    result = check(watches=[bad])
    assert reasons(result) == [("watch", "invalid_id")] and result.published == 0


def test_comparison_row_item_must_be_this_weeks_observation():
    good, bad = row("i4"), row("i99")
    result = check(comparisons=[comparison(good, bad)])
    assert result.output.comparisons[0].rows == (good,)
    assert reasons(result) == [("comparison_row", "invalid_id")]


def test_past_observation_is_not_a_comparison_row():
    result = check(comparisons=[comparison(row("i90"), row("i4"))])
    assert reasons(result) == [("comparison_row", "invalid_id")]


# --- 比較表：重複・空 ---


def test_duplicate_row_item_keeps_the_first_row_across_comparisons():
    a = comparison(row("i4", feature="先"))
    b = comparison(row("i4", feature="後"), row("i2", "Bar"))
    result = check(comparisons=[a, b])
    assert result.output.comparisons[0] == a
    assert [r.item_id for r in result.output.comparisons[1].rows] == ["i2"]
    assert reasons(result) == [("comparison_row", "duplicate_id")]


def test_comparison_without_remaining_rows_is_excluded():
    only_dup = comparison(row("i4"))
    result = check(comparisons=[comparison(row("i4")), only_dup, comparison()])
    assert len(result.output.comparisons) == 1
    assert reasons(result) == [
        ("comparison_row", "duplicate_id"),
        ("comparison", "empty_comparison"),
        ("comparison", "empty_comparison"),
    ]
    assert result.verification["empty_comparison"] == "failed"


def test_excluded_empty_comparison_is_the_one_the_ai_returned():
    original = comparison(row("i4"), row("i99"))  # 行が全部除外される領域
    result = check(comparisons=[comparison(row("i4")), original])
    empty = [x for x in result.excluded if x.category == "empty_comparison"]
    assert [x.entry is original for x in empty] == [True]  # 絞った後の領域ではなく、元のもの


def test_row_excluded_by_another_check_does_not_take_the_id():
    bad = row("i4", feature="あ" * 200)  # 文字数で除外
    result = check(comparisons=[comparison(bad), comparison(row("i4"))])
    assert [r.feature for c in result.output.comparisons for r in c.rows] == ["特徴"]
    assert reasons(result) == [("comparison_row", "too_long"), ("comparison", "empty_comparison")]


# --- 製品名の照合 ---


def test_github_row_needs_no_product_name_and_it_is_not_checked():
    result = check(comparisons=[comparison(row("i4"), row("i4", "Whatever"))])
    assert [r.product_name for r in result.output.comparisons[0].rows] == [None]  # 2行目は重複
    assert [x.category for x in result.excluded] == ["duplicate_id"]
    assert check(comparisons=[comparison(row("i4", "Whatever"))]).excluded == ()
    # 表示に使わないので、URL や長さの検査にもかけない（特徴・用途は対象のまま）
    long_name = "https://x.example " + "あ" * 500
    assert check(comparisons=[comparison(row("i4", long_name))]).excluded == ()
    result = check(comparisons=[comparison(row("i4", "n", feature="https://x.example"))])
    assert reasons(result)[0] == ("comparison_row", "forbidden_link")


@pytest.mark.parametrize(
    "name, ok",
    [
        ("FooDB", True),
        ("foodb", True),  # 大文字・小文字は区別しない
        ("ＦｏｏＤＢ", True),  # 全角
        ("FooDB 3.0", True),
        ("NoSuchDB", False),
        ("Foo DB", False),
        (None, False),  # GitHub 外で製品名がない
        ("", False),
        ("  ", False),
    ],
)
def test_non_github_row_product_name_must_appear_in_the_collected_title(name, ok):
    result = check(comparisons=[comparison(row("i1", name))])
    assert (result.excluded == ()) is ok
    if not ok:
        assert reasons(result)[0] == ("comparison_row", "unknown_product")
        assert result.verification["unknown_product"] == "failed"


def test_product_name_is_checked_against_the_title_given_to_the_ai():
    """AI に渡したタイトルだけと照合する（まとめた別の記事のタイトルは AI が見ていない）。"""
    merged = Item(
        "https://a.example.com/m",
        (
            item("https://a.example.com/m", "First title").sources[0],
            item("https://b.example.com/m", "Second: BarDB news").sources[0],
        ),
        (),
        (),
        (),
        (),
        None,
    )
    items = {**ITEMS, "i6": merged}
    data = {**DATA, "items": [*DATA["items"], {"id": "i6", "untrusted": {"title": "First title"}}]}
    ok = check(comparisons=[comparison(row("i6", "First"))], data=data, items=items)
    assert ok.excluded == ()
    bad = check(comparisons=[comparison(row("i6", "BarDB"))], data=data, items=items)
    assert reasons(bad)[0] == ("comparison_row", "unknown_product")


@pytest.mark.parametrize(
    "name, ok",
    [
        ("pkgtool", True),  # タイトルは版のタグだけ。repo の名前の部分と一致
        ("PkgTool", True),
        ("example", False),  # owner の部分は見ない
        ("example/pkgtool", False),  # repo の名前の部分だけと照合する
        ("pkg", False),  # repo の名前の一部の語では通さない
        ("tool", False),
        ("other", False),
    ],
)
def test_release_row_product_name_may_match_the_repo_name_part(name, ok):
    items = {
        **ITEMS,
        "i7": item("https://github.com/example/pkgtool/releases/tag/v5.0.0", "v5.0.0"),
    }
    entry = {
        "id": "i7",
        "untrusted": {"title": "v5.0.0", "excerpt": None},
        "repo": "example/pkgtool",
    }
    data = {**DATA, "items": [*DATA["items"], entry]}
    result = check(comparisons=[comparison(row("i7", name))], data=data, items=items)
    assert (result.excluded == ()) is ok


@pytest.mark.parametrize(
    "repo, name, ok",
    [
        ("someone/pkg-tool", "pkg-tool", True),
        ("someone/pkg-tool", "Pkg Tool", True),  # 区切りは同じとみなす
        ("someone/pkg_tool", "pkg.tool", True),
        ("someone/claude-proxy", "Claude", False),  # 別製品の名前の語だけでは通さない
        ("openai/openai-python", "Python", False),
        ("someone/pkgtool-cli", "pkgtool", False),
        ("someone/pkgtool", "-pkgtool-", False),  # 前後の記号は残る
    ],
)
def test_release_row_needs_the_whole_repo_name(repo, name, ok):
    items = {**ITEMS, "i7": item("https://github.com/x/y/releases/tag/v1", "v1")}
    entry = {"id": "i7", "untrusted": {"title": "v1", "excerpt": None}, "repo": repo}
    data = {**DATA, "items": [*DATA["items"], entry]}
    result = check(comparisons=[comparison(row("i7", name))], data=data, items=items)
    assert (result.excluded == ()) is ok


def test_repo_name_is_ignored_for_items_without_repo():
    """repo を持たない項目（リリース以外、URL から取れないリリース）は、タイトルだけと照合する。"""
    items = {**ITEMS, "i7": item("https://a.example.com/r/v5", "v5.0.0")}
    entry = {"id": "i7", "untrusted": {"title": "v5.0.0", "excerpt": None}, "repo": None}
    data = {**DATA, "items": [*DATA["items"], entry]}
    result = check(comparisons=[comparison(row("i7", "pkgtool"))], data=data, items=items)
    assert reasons(result)[0] == ("comparison_row", "unknown_product")


# --- D1 出典 ---


def test_past_observation_url_is_in_the_allowed_set_but_unreadable_url_is_not():
    result = check(themes=[theme(None, [], ["i90"])])
    assert result.excluded == ()
    bad_urls = {"i90": "https://user:pw@evil.example/x", "i91": "ftp://x.example/a"}
    out = WeeklyOutput((theme(None, [], ["i90"]), theme("T0002", [], ["i91"])), (), (), ())
    result = check_weekly(out, DATA, ITEMS, bad_urls, LIMITS)
    assert reasons(result) == [("theme", "invalid_source")] * 2


def test_this_weeks_item_with_unreadable_url_is_excluded():
    items = {**ITEMS, "i1": item("https://user:pw@evil.example/x")}
    result = check(themes=[theme(None, ["i1"]), theme(None, ["i2"])], items=items)
    assert reasons(result) == [("theme", "invalid_source")]
    assert [t.item_ids for t in result.output.themes] == [("i2",)]


# --- D2・D3 ---


@pytest.mark.parametrize(
    "text, category",
    [
        ("https://evil.example/x", "forbidden_link"),
        ("see www.evil.example", "forbidden_link"),
        ("[a](b)", "forbidden_link"),
        ("<http://x>", "forbidden_link"),
        ("![alt]", "forbidden_html"),
        ("<img src=x>", "forbidden_html"),
        ("<!-- c -->", "forbidden_html"),
        ("<b>bold</b>", "forbidden_html"),
    ],
)
def test_links_and_html_are_excluded_in_every_text_field(text, category):
    entries = {
        "theme": dict(themes=[theme(None, name=text), theme(None, ["i2"], change=text)]),
        "tech_change": dict(changes=[change(headline=text), change(["i2"], description=text)]),
        "watch": dict(watches=[watch(name=text), watch(["i2"], reason=text)]),
    }
    for section, kwargs in entries.items():
        result = check(**kwargs)
        assert reasons(result) == [(section, category)] * 2


def test_comparison_texts_are_checked_at_both_levels():
    link = "https://evil.example"
    result = check(
        comparisons=[
            comparison(row("i4"), domain=link),  # 領域ごと除外
            comparison(row("i4", feature=link)),  # 行だけ除外 → 行がなくなり領域も除外
            comparison(row("i1", f"FooDB {link}"), summary="ok"),  # product_name も対象
            comparison(row("i2", "Bar", use_case="<b>x</b>")),
            comparison(row("i3", "Baz"), summary="[a](b)"),
        ]
    )
    assert result.output.comparisons == ()
    assert [(x.section, x.category) for x in result.excluded] == [
        ("comparison", "forbidden_link"),
        ("comparison_row", "forbidden_link"),
        ("comparison_row", "unknown_product"),  # 製品名の照合が先
        ("comparison_row", "forbidden_html"),
        ("comparison", "forbidden_link"),
        ("comparison", "empty_comparison"),
        ("comparison", "empty_comparison"),
        ("comparison", "empty_comparison"),
    ]


def test_reason_does_not_contain_the_text():
    result = check(themes=[theme(None, name="https://secret.example/token")])
    assert "secret" not in result.excluded[0].reason


# --- 文字数 ---


@pytest.mark.parametrize(
    "make, over, edge",
    [
        (lambda n: theme(None, name="あ" * n), 61, 60),
        (lambda n: theme(None, change="あ" * n), 401, 400),
        (lambda n: theme(None, comparison="あ" * n), 241, 240),
        (lambda n: change(headline="あ" * n), 81, 80),
        (lambda n: change(description="あ" * n), 241, 240),
        (lambda n: watch(name="あ" * n), 61, 60),
        (lambda n: watch(reason="あ" * n), 241, 240),
    ],
)
def test_text_longer_than_twice_the_guide_is_excluded(make, over, edge):
    entry_ok, entry_bad = make(edge), make(over)
    kwargs = {ThemeEntry: "themes", TechChangeEntry: "changes", WatchEntry: "watches"}[
        type(entry_ok)
    ]
    assert check(**{kwargs: [entry_ok]}).excluded == ()  # ちょうど2倍までは残す
    result = check(**{kwargs: [entry_bad]})
    assert [x.category for x in result.excluded] == ["too_long"]


@pytest.mark.parametrize(
    "domain, summary, feature, use_case, product, ok",
    [
        ("あ" * 60, "あ" * 300, "あ" * 160, "あ" * 120, None, True),
        ("あ" * 61, "ok", "ok", "ok", None, False),
        ("ok", "あ" * 301, "ok", "ok", None, False),
        ("ok", "ok", "あ" * 161, "ok", None, False),
        ("ok", "ok", "ok", "あ" * 121, None, False),
    ],
)
def test_comparison_text_limits(domain, summary, feature, use_case, product, ok):
    entry = comparison(row("i4", product, feature, use_case), domain=domain, summary=summary)
    result = check(comparisons=[entry])
    assert (result.excluded == ()) is ok
    assert {x.category for x in result.excluded} <= {"too_long", "empty_comparison"}


def test_long_product_name_is_excluded_by_length():
    title = "X" * 81 + " tail"
    items = {**ITEMS, "i1": item("https://a.example.com/1", title)}
    data = {**DATA, "items": [{"id": "i1", "untrusted": {"title": title}}]}
    result = check(comparisons=[comparison(row("i1", "X" * 81))], data=data, items=items)
    assert reasons(result)[0] == ("comparison_row", "too_long")


# --- D4 長い引用 ---


def test_quote_of_a_collected_title_or_excerpt_is_excluded():
    long_text = "This is a very long sentence copied word for word from a source " * 4
    items = {**ITEMS, "i2": item("https://a.example.com/2", "Bar", excerpt=long_text)}
    data = {
        **DATA,
        "items": [
            {"id": "i1", "untrusted": {"title": "FooDB 3.0 released", "excerpt": None}},
            {"id": "i2", "untrusted": {"title": "Bar", "excerpt": long_text}},
        ],
    }
    copied = theme(None, ["i1"], change=long_text[:300])  # 別の項目の抜粋でも見つける
    result = check(
        themes=[copied, theme(None, ["i2"], comparison=long_text[:230])],
        data=data,
        items=items,
    )
    assert reasons(result) == [("theme", "long_quote")] * 2


def test_quote_of_a_past_observation_title_is_excluded():
    long_title = "A very long title of a past article that the summary copies as it is " * 4
    data = {**DATA, "past_observations": [{"id": "i90", "untrusted": {"title": long_title}}]}
    result = check(watches=[watch(reason=long_title[:230])], data=data)
    assert reasons(result) == [("watch", "long_quote")]


def test_meta_terms_of_the_item_are_not_counted():
    term = "a very long product name that appears many times " * 3
    items = {**ITEMS, "i1": item("https://a.example.com/1", term, meta=[term.strip()])}
    data = {**DATA, "items": [{"id": "i1", "untrusted": {"title": term, "excerpt": None}}]}
    result = check(themes=[theme(None, ["i1"], change=term.strip())], data=data, items=items)
    assert result.excluded == ()


def test_short_overlap_with_a_title_is_allowed():
    result = check(themes=[theme(None, change="FooDB 3.0 released でテーマが動いた")])
    assert result.excluded == ()


# --- 検査の順・まとめ ---


def test_first_failing_check_gives_the_category():
    both = theme("T0099", name="https://evil.example")  # ID と URL の両方に違反
    assert reasons(check(themes=[both])) == [("theme", "invalid_id")]


def test_verification_marks_only_the_failed_checks():
    result = check(watches=[watch(name="https://x.example")])
    failed = {k for k, v in result.verification.items() if v == "failed"}
    assert failed == {"forbidden_link"}
    assert result.verification["invalid_output"] == "passed"


def test_everything_excluded_leaves_nothing_published():
    result = check(themes=[theme("T0099")], changes=[change(["i99"])], watches=[watch([])])
    assert result.published == 0 and len(result.excluded) == 3


def test_unreadable_url_excludes_rows_changes_and_watch_and_empties_the_comparison():
    items = {**ITEMS, "i3": item("https://user:pw@evil.example/x", "Baz")}
    result = check(
        changes=[change(["i3"]), change(["i2"])],
        watches=[watch(["i3"])],
        comparisons=[comparison(row("i3", "Baz")), comparison(row("i4"))],
        items=items,
    )
    assert reasons(result) == [
        ("tech_change", "invalid_source"),
        ("watch", "invalid_source"),
        ("comparison_row", "invalid_source"),
        ("comparison", "empty_comparison"),
    ]
    assert len(result.output.tech_changes) == 1 and len(result.output.comparisons) == 1


@pytest.mark.parametrize("name", ["Foo|DB", "Foo*DB", "Foo DB"])
def test_product_name_must_match_the_title_with_symbols(name):
    items = {**ITEMS, "i1": item("https://a.example.com/1", "C# FooDB 3.0")}
    data = {**DATA, "items": [{"id": "i1", "untrusted": {"title": "C# FooDB 3.0"}}]}
    result = check(comparisons=[comparison(row("i1", name))], data=data, items=items)
    assert reasons(result)[0] == ("comparison_row", "unknown_product")
    ok = check(comparisons=[comparison(row("i1", "C# FooDB"))], data=data, items=items)
    assert ok.excluded == ()


@pytest.mark.parametrize(
    "title, name, ok",
    [
        ("Intelligence report", "Intel", False),  # 語の途中
        ("Intel releases chip", "Intel", True),
        ("Welcome to FooDB 3", "DB", False),
        ("Welcome to Foo DB 3", "DB", True),
        ("FooDB (beta)", "FooDB", True),  # 前後が記号
        ("Use C# today", "C#", True),
        ("AB", "A", False),  # 英数字の名前は1文字でも照合する（境界で誤一致を防ぐ）
        ("A B", "A", True),
        ("X（旧Twitter）", "X", True),
        ("Xbox 発売", "X", False),
        ("新製品の発表", "新", False),  # 日本語1文字の名前は短すぎる
        ("βIntel launches", "Intel", False),  # 英数字は Unicode で見る
        ("Αintel", "intel", False),
        ("楽天AIが公開", "AI", True),  # 日本語は境界にしない
        ("楽天AIが公開", "楽天AI", True),
        ("TypeScript5.7 released", "TypeScript", True),  # 後ろの数字は許す
        ("Next.js15 released", "Next.js", True),
        ("Next.js15 released", "Next.js15", True),
        ("GPT-4o released", "GPT-4", False),  # 数字で終わる名前の後ろに英字は続かない
        ("GPT-4 released", "GPT-4", True),
        ("TypeScript5D", "TypeScript", False),  # 版の表記の後ろに英字
        ("TypeScript5.7beta", "TypeScript", False),
        ("TypeScript5.7", "TypeScript", True),
        ("Next.js15で公開", "Next.js", True),
        ("Intel8086 chip", "Intel", True),
        ("人々AIが話題", "AI", True),  # 々・〆・〇、互換漢字、拡張漢字も日本語
        ("〇AI〆", "AI", True),
        ("豈AI", "AI", True),
        ("\U00020bb7AI", "AI", True),
        ("Python3 guide", "Python", True),
        ("C#compiler news", "C#", True),  # 記号で終わる名前の後ろは見ない
        ("新製品「サンプルDB」登場", "サンプルDB", True),  # 日本語は部分一致のまま
        ("新製品サンプルDBを公開", "サンプル", True),
        ("x2 pro", "x", True),
    ],
)
def test_product_name_matches_as_a_word(title, name, ok):
    items = {**ITEMS, "i1": item("https://a.example.com/1", title)}
    data = {**DATA, "items": [{"id": "i1", "untrusted": {"title": title}}]}
    result = check(comparisons=[comparison(row("i1", name))], data=data, items=items)
    assert (result.excluded == ()) is ok
    if not ok:
        assert reasons(result)[0] == ("comparison_row", "unknown_product")


def test_product_name_of_exactly_twice_the_guide_is_kept():
    title = "X" * 80
    items = {**ITEMS, "i1": item("https://a.example.com/1", title)}
    data = {**DATA, "items": [{"id": "i1", "untrusted": {"title": title}}]}
    result = check(comparisons=[comparison(row("i1", title))], data=data, items=items)
    assert result.excluded == ()  # 上限 40 の2倍（80）までは残す


def test_excluded_theme_does_not_take_a_number_when_assigning_new_ids():
    bad = theme(None, ["i99"], name="除外")
    result = check(themes=[bad, theme(None, ["i1"], name="先"), theme("T0002", ["i2"])])
    numbered = assign_theme_ids(result.output.themes, "T0010")
    assert [(t.name, t.theme_id) for t in numbered] == [("先", "T0010"), ("テーマ", "T0002")]
    assert result.excluded[0].entry.theme_id is None  # 除外したテーマは番号を持たない
