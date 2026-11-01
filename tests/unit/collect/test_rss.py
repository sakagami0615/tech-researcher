from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tech_researcher.collect.http import FetchError, Response
from tech_researcher.collect.rss import collect_feed, parse_feed
from tech_researcher.config import Feed

FIXTURE = (Path(__file__).parent / "fixtures" / "feed.xml").read_bytes()
NOW = datetime(2026, 10, 24, 6, 0, tzinfo=UTC)
FEED = Feed("example", "Example", "https://blog.example.com/feed", "new", True)


def fake_fetch(body=FIXTURE, attempts=1):
    def fetch(url, limits):
        assert url == FEED.url
        return Response(url, 200, body, attempts)

    return fetch


def test_entries_become_records(limits):
    records = parse_feed(FIXTURE, FEED, NOW)
    assert [r.url for r in records] == [
        "https://blog.example.com/full",
        "https://blog.example.com/summary",
        "https://blog.example.com/old",
        "https://blog.example.com/undated",
    ]
    full, summary, _, undated = records
    assert full.source_id == "rss:example" and full.fetched_at == NOW
    assert full.published_at == datetime(2026, 10, 23, 1, 0, tzinfo=UTC)
    assert full.primary_text_origin == "rss_full" and "本文。" in full.primary_text
    assert full.excerpt == "要約です"
    assert full.links == ("https://docs.example.com/spec",)  # feedparser が javascript: を消す
    assert summary.primary_text is None and summary.excerpt.startswith("要約。")
    assert summary.published_at == datetime(2026, 10, 23, 11, 0, tzinfo=UTC)
    assert summary.links == ("https://github.com/O/R",)
    assert undated.published_precision == "unknown" and undated.events == ()


def test_collect_returns_all_entries(limits):
    # 期間での絞り込みは、ほかの情報源とまとめた後に行う
    result = collect_feed(FEED, limits, fetch_url=fake_fetch(attempts=2))
    assert result.source_id == "rss:example" and result.error is None
    assert result.attempts == 2 and len(result.records) == 4


def test_empty_result_is_a_success(limits, caplog):
    empty = b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>'
    with caplog.at_level("INFO"):
        result = collect_feed(FEED, limits, fetch_url=fake_fetch(empty))
    assert result.records == () and result.error is None
    assert "0件" in caplog.text


def test_fetch_failure_is_recorded(limits):
    def failing(url, limits):
        raise FetchError("HTTP 404", 1, 404)

    result = collect_feed(FEED, limits, fetch_url=failing)
    assert result.records == () and result.error == "HTTP 404" and result.attempts == 1


def test_popular_feed_is_collected_like_new_one(limits):
    # 初登場の判定（D-13）は別の Issue。ここでは新着型と同じレコードになる
    result = collect_feed(replace(FEED, kind="popular"), limits, fetch_url=fake_fetch())
    assert result.error is None and len(result.records) == 4


def rss_item(content="", description=""):
    return (
        '<?xml version="1.0"?><rss version="2.0" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><title>t</title>'
        "<item><title>a</title><link>https://e.example/a</link>"
        f"<pubDate>Fri, 23 Oct 2026 01:00:00 +0000</pubDate>{description}{content}"
        "</item></channel></rss>"
    ).encode()


def test_summary_can_be_full_text():
    # AWS What's New・Google Cloud は content がなく、description に全文が入る
    body = rss_item(description="<description>全文。</description>")
    (record,) = parse_feed(body, replace(FEED, full_text="summary"), NOW)
    assert record.primary_text == "全文。" and record.primary_text_origin == "rss_full"
    assert record.excerpt is None  # 全文を抜粋として DB に残さない（D-14）


def test_feed_without_full_text_keeps_only_excerpt():
    # Qiita の人気は content も先頭の抜粋。Google AI は summary が画像の説明文
    body = rss_item(
        content="<content:encoded>記事の説明</content:encoded>",
        description="<description>画像の説明</description>",
    )
    (record,) = parse_feed(body, replace(FEED, full_text="none"), NOW)
    assert record.primary_text is None and record.primary_text_origin is None
    assert record.excerpt == "画像の説明"
    (record,) = parse_feed(body, replace(FEED, full_text="none", excerpt="content"), NOW)
    assert record.primary_text is None and record.excerpt == "記事の説明"


def test_title_only_feed():
    (record,) = parse_feed(rss_item(), replace(FEED, full_text="none"), NOW)
    assert record.primary_text is None and record.excerpt is None and record.title == "a"


def test_hatena_entry_date_is_posted_time():
    hatena = Feed("hatena", "はてな", "https://b.hatena.ne.jp/hotentry/it.rss", "popular", True)
    hatena = replace(hatena, full_text="none", entry_date="posted")
    body = (Path(__file__).parent / "fixtures" / "hatena.rdf").read_bytes()
    (record,) = parse_feed(body, hatena, NOW)
    posted = datetime(2026, 10, 30, 10, 18, 57, tzinfo=UTC)
    assert record.url == "https://tech.example.jp/post" and record.title == "記事のタイトル"
    assert record.published_at is None and record.published_precision == "unknown"
    assert record.posted_at == posted
    assert [(e.type, e.at) for e in record.events] == [("posted", posted)]
    assert record.excerpt == "記事の説明。" and record.primary_text is None
    assert record.links == ()  # はてなの埋め込み用の HTML（content）のリンクは拾わない
    assert [(a.metric, a.value, a.measured_at) for a in record.attention] == [
        ("hatena_bookmarks", 355.0, NOW)
    ]


def test_atom_updated_can_be_used_as_posted_time():
    # Vercel の Atom は published がなく updated だけ
    body = (
        b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>'
        b"<entry><title>a</title><link href='https://e.example/a'/>"
        b"<updated>2026-10-23T01:00:00Z</updated></entry></feed>"
    )
    (record,) = parse_feed(body, replace(FEED, entry_date="posted"), NOW)
    assert record.published_at is None
    assert record.posted_at == datetime(2026, 10, 23, 1, 0, tzinfo=UTC)


def paged_fetch(pages, attempts=1):
    """URL ごとの本文を返す。値が FetchError ならそれを送出する。"""
    requested = []

    def fetch(url, limits):
        requested.append(url)
        body = pages[url]
        if isinstance(body, FetchError):
            raise body
        return Response(url, 200, body, attempts)

    return fetch, requested


def page_body(*names):
    items = "".join(
        f"<item><title>{n}</title><link>https://e.example/{n}</link>"
        "<pubDate>Fri, 23 Oct 2026 01:00:00 +0000</pubDate></item>"
        for n in names
    )
    return f'<rss version="2.0"><channel><title>t</title>{items}</channel></rss>'.encode()


def test_changelog_pages_are_followed(limits):
    feed = replace(FEED, url="https://github.blog/changelog/feed/", pages=2)
    fetch, requested = paged_fetch(
        {
            feed.url: page_body("a", "b"),
            # 取得の間に新しい記事が出ると、前のページの末尾が次のページの先頭にずれる
            feed.url + "?paged=2": page_body("b", "c"),
        },
        attempts=2,
    )
    result = collect_feed(feed, limits, fetch_url=fetch)
    assert requested == [feed.url, feed.url + "?paged=2"]
    assert [r.url for r in result.records] == [f"https://e.example/{n}" for n in "abc"]
    assert result.error is None and result.attempts == 4


def test_failed_page_fails_the_whole_source(limits):
    feed = replace(FEED, url="https://e.example/feed?x=1", pages=2)
    fetch, _ = paged_fetch(
        {feed.url: page_body("a"), feed.url + "&paged=2": FetchError("HTTP 503", 3, 503)}
    )
    result = collect_feed(feed, limits, fetch_url=fetch)
    assert result.records == () and result.error == "HTTP 503" and result.attempts == 4


@pytest.mark.parametrize(
    "body",
    [b"<html><body>Access denied</body></html>", b"", b"not xml at all"],
)
def test_non_feed_response_is_a_failure(limits, body):
    result = collect_feed(FEED, limits, fetch_url=fake_fetch(body))
    assert result.records == () and result.error == "フィードとして読めない応答"


def test_atom_updated_is_not_used_as_published():
    body = (
        b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>'
        b"<entry><title>a</title><link href='https://e.example/a'/>"
        b"<updated>2026-10-23T01:00:00Z</updated></entry></feed>"
    )
    (record,) = parse_feed(body, FEED, NOW)
    assert record.published_at is None and record.events == ()


def secret_file(tmp_path):
    path = tmp_path / "secret.xml"
    path.write_text(
        '<rss version="2.0"><channel><item><title>SECRET</title>'
        "<link>https://e.example/s</link></item></channel></rss>"
    )
    return path


def test_body_is_not_opened_as_file_path(tmp_path):
    # feedparser は bytes をファイルのパスとして開こうとする。本文として読ませる
    path = secret_file(tmp_path)
    with pytest.raises(ValueError):
        parse_feed(str(path).encode(), FEED, NOW)


def xxe_body(target):
    return (
        f'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x SYSTEM "{target}">]>'
        '<rss version="2.0"><channel><title>t</title>'
        "<item><title>a &x; b</title><link>https://e.example/a</link></item>"
        "</channel></rss>"
    ).encode()


def test_external_file_entity_is_not_resolved(tmp_path):
    records = parse_feed(xxe_body(f"file://{secret_file(tmp_path)}"), FEED, NOW)
    assert "SECRET" not in records[0].title


def test_external_http_entity_is_not_fetched(server):
    parse_feed(xxe_body(server.base + "/xxe"), FEED, NOW)
    assert server.paths == []


def test_entity_expansion_is_bounded():
    entities = '<!ENTITY a "aaaaaaaaaa">' + "".join(
        f'<!ENTITY {n} "{("&" + p + ";") * 10}">'
        for p, n in zip("abcdefgh", "bcdefghi", strict=True)
    )
    body = (
        f'<?xml version="1.0"?><!DOCTYPE rss [{entities}]>'
        '<rss version="2.0"><channel><item><title>&i;</title>'
        "<link>https://e.example/a</link></item></channel></rss>"
    ).encode()
    try:
        records = parse_feed(body, FEED, NOW)
    except ValueError:
        return  # 読めない応答として止まる
    assert all(len(r.title) < 1_000_000 for r in records)
