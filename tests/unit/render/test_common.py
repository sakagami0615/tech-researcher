from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from tech_researcher.collect.aggregate import aggregate
from tech_researcher.collect.records import Event, Record
from tech_researcher.render.common import (
    day_label,
    identifier,
    plain_text,
    repo_code,
    source_line,
)

IN = datetime(2026, 10, 23, 0, 0, tzinfo=UTC)  # JST 9:00
NAMES = {"rss:blog": "公式ブログ", "hackernews": "Hacker News", "zenn": "Zenn"}


def record(source_id="rss:blog", url="https://example.com/post", published_at=IN, **kw):
    return Record(
        source_id=source_id,
        url=url,
        title=kw.pop("title", "外部のタイトル [罠](https://evil.example)"),
        published_precision=kw.pop("precision", "datetime" if published_at else "unknown"),
        fetched_at=IN,
        published_at=published_at,
        events=(Event("published", published_at or IN),),
        **kw,
    )


def hn(link_url="https://example.com/post"):
    return record(
        "hackernews",
        "https://news.ycombinator.com/item?id=1",
        published_at=None,
        link_url=link_url,
        posted_at=datetime(2026, 10, 23, 16, 0, tzinfo=UTC),  # JST 10/24 1:00
    )


def items(*records):
    return {f"i{n}": item for n, item in enumerate(aggregate(records), start=1)}


def test_day_label_has_weekday():
    assert day_label(date(2026, 10, 23)) == "2026-10-23（金）"


def test_identifier_is_first_line_comment():
    line = identifier("daily", date(2026, 10, 23), "20261024-060000-a1b2")
    assert line == (
        "<!-- tech-researcher kind=report cadence=daily target=2026-10-23 "
        "run=20261024-060000-a1b2 -->"
    )


@pytest.mark.parametrize("run_id", ["a--b", "a b", "x-->", "", "a_b"])
def test_identifier_rejects_unsafe_run_id(run_id):
    with pytest.raises(ValueError):
        identifier("daily", date(2026, 10, 23), run_id)


@pytest.mark.parametrize("cadence", ["daily -->", "a--b", ""])
def test_identifier_rejects_unsafe_cadence(cadence):
    with pytest.raises(ValueError):
        identifier(cadence, date(2026, 10, 23), "r1")


def test_link_text_is_source_name_and_domain_only():
    line = source_line(["i1"], items(record()), NAMES)
    assert line == "[公式ブログ（example.com）](https://example.com/post) 公開 2026-10-23"
    assert "罠" not in line


def test_posted_date_when_published_is_unknown():
    # HN だけの項目は、公開日が不明なので投稿日（JST）を書く
    line = source_line(["i1"], items(hn()), NAMES)
    assert line.endswith(
        "[Hacker News（news.ycombinator.com）](https://news.ycombinator.com/item?id=1) "
        "投稿 2026-10-24"
    )


def test_article_of_aggregator_only_item_is_listed_without_primary():
    # HN だけが指す記事は、一次情報が特定できなくても記事そのものの URL を出典に出す
    line = source_line(["i1"], items(hn()), NAMES)
    assert line.startswith("[example.com](https://example.com/post) ／ [Hacker News")


def test_aggregator_with_unusable_url_still_has_article():
    bad = replace(hn(), url="https://news.ycombinator.com/a b")
    assert source_line(["i1"], items(bad), NAMES) == "[example.com](https://example.com/post)"


def test_no_sources_is_an_error():
    with pytest.raises(ValueError):
        source_line([], {}, NAMES)


def test_date_only_published_uses_utc_date():
    published = datetime(2026, 10, 23, tzinfo=UTC)
    line = source_line(["i1"], items(record(published_at=published, precision="date")), NAMES)
    assert line.endswith("公開 2026-10-23")


def test_all_sources_in_item_ids_order_without_primary():
    data = items(record(), hn(), record("zenn", "https://zenn.dev/a/articles/b"))
    line = source_line(["i2", "i1"], data, NAMES)
    assert "一次情報" not in line
    assert [p.split("]")[0] for p in line.split(" ／ ")] == [
        "[Zenn（zenn.dev）",
        "[公式ブログ（example.com）",
        "[Hacker News（news.ycombinator.com）",
    ]


def test_primary_item_comes_first_with_mark():
    data = items(record("zenn", "https://zenn.dev/a/articles/b"), hn(), record())
    # i1 は Zenn、i2 は公式ブログと HN をまとめた項目。一次情報は i2 の記事そのもの
    line = source_line(["i1", "i2"], data, NAMES, primary_item_id="i2")
    parts = line.split(" ／ ")
    assert parts[0].startswith("一次情報 [公式ブログ（example.com）](https://example.com/post)")
    assert parts[1].startswith("[Zenn")
    assert parts[2].startswith("[Hacker News")


def test_primary_item_without_own_record_links_its_url_by_domain():
    # HN だけが指している記事。記事そのもののレコードはないので、ドメインだけで書く
    line = source_line(["i1"], items(hn()), NAMES, primary_item_id="i1")
    assert line.startswith("一次情報 [example.com](https://example.com/post) ／ [Hacker News")


def test_primary_link_comes_first_and_is_not_duplicated():
    blog = record(links=("https://docs.example.org/release(1)", "https://example.com/post"))
    data = items(blog)
    line = source_line(["i1"], data, NAMES, primary_link_id="i1-l1")
    assert line.startswith("一次情報 [docs.example.org](https://docs.example.org/release%281%29)")
    # 候補リンクが既存の出典と同じ URL なら、その出典を先頭に移す
    line = source_line(["i1"], data, NAMES, primary_link_id="i1-l2")
    assert line == "一次情報 [公式ブログ（example.com）](https://example.com/post) 公開 2026-10-23"


def test_primary_link_wins_over_primary_item():
    data = items(record(links=("https://docs.example.org/a",)))
    line = source_line(["i1"], data, NAMES, primary_item_id="i1", primary_link_id="i1-l1")
    assert line.startswith("一次情報 [docs.example.org]")
    assert line.count("一次情報") == 1


def test_unknown_source_name_is_an_error():
    with pytest.raises(ValueError):
        source_line(["i1"], items(record("rss:unknown")), NAMES)


@pytest.mark.parametrize("name", ["example/tiny-agent", "Owner-1/repo.name_2"])
def test_repo_code_wraps_valid_name(name):
    assert repo_code(name) == f"`{name}`"


@pytest.mark.parametrize("name", ["a/b/c", "a`/b", "@user/repo", "owner/repo#1", "/repo", "a/b c"])
def test_repo_code_rejects_other_forms(name):
    with pytest.raises(ValueError):
        repo_code(name)


EVIL = "Feed](https://evil.example) @victim <!--\n# 見出し"


def test_plain_text_neutralizes_markdown_mentions_and_newlines():
    text = plain_text(EVIL)
    assert text == (r"Feed\]\(https\://evil\.example\) ＠victim \<\!\-\- ＃ 見出し")


def test_malicious_source_name_cannot_break_link_label():
    line = source_line(["i1"], items(record()), {"rss:blog": EVIL})
    label = line[: line.index("](https://example.com/post)")]
    assert "](" not in label and "@" not in label and "<!--" not in label and "\n" not in line


@pytest.mark.parametrize(
    "url, dest",
    [
        ("https://example.com/a&num;b", "https://example.com/a&amp;num;b"),
        ("https://example.com/p?a=1&b=2", "https://example.com/p?a=1&amp;b=2"),
        ("https://example.com/p?q=&lt;x&gt;", "https://example.com/p?q=&amp;lt;x&amp;gt;"),
    ],
)
def test_ampersand_in_link_destination_is_escaped(url, dest):
    line = source_line(["i1"], items(record(url=url)), NAMES)
    assert f"]({dest})" in line
