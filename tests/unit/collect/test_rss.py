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
    records = parse_feed(FIXTURE, "rss:example", NOW)
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


def test_popular_feed_is_not_supported_yet(limits):
    with pytest.raises(ValueError):
        collect_feed(Feed("z", "Z", "https://z.example/feed", "popular", True), limits)


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
    (record,) = parse_feed(body, "rss:x", NOW)
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
        parse_feed(str(path).encode(), "rss:x", NOW)


def xxe_body(target):
    return (
        f'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x SYSTEM "{target}">]>'
        '<rss version="2.0"><channel><title>t</title>'
        "<item><title>a &x; b</title><link>https://e.example/a</link></item>"
        "</channel></rss>"
    ).encode()


def test_external_file_entity_is_not_resolved(tmp_path):
    records = parse_feed(xxe_body(f"file://{secret_file(tmp_path)}"), "rss:x", NOW)
    assert "SECRET" not in records[0].title


def test_external_http_entity_is_not_fetched(server):
    parse_feed(xxe_body(server.base + "/xxe"), "rss:x", NOW)
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
        records = parse_feed(body, "rss:x", NOW)
    except ValueError:
        return  # 読めない応答として止まる
    assert all(len(r.title) < 1_000_000 for r in records)
