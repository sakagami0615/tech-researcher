"""RSS・Atom の収集（D-03・D-11・D-14、data-model.md 2.3〜2.7）。

新着型のフィードを取得して、エントリーを収集レコードにする。期間での絞り込みは、
ほかの情報源とまとめた後に period_filter で行う（公開日時を全出典で判断するため）。
人気型（初登場の判定。D-13）とはてなブックマークの扱いは、それを実装する Issue で足す。
"""

import calendar
import io
import logging
from datetime import UTC, datetime
from html.parser import HTMLParser

import feedparser

from tech_researcher.collect.http import FetchError, fetch
from tech_researcher.collect.records import Event, Record, SourceResult
from tech_researcher.config import Feed, Limits

log = logging.getLogger(__name__)


def collect_feed(feed: Feed, limits: Limits, *, fetch_url=fetch) -> SourceResult:
    """フィードを取得する。フィードとして読めない応答（HTML のエラーページなど）は失敗にする。"""
    if feed.kind != "new":
        raise ValueError(f"人気型のフィード {feed.id} はまだ扱えない")
    source_id = f"rss:{feed.id}"
    try:
        response = fetch_url(feed.url, limits)
    except FetchError as e:
        return SourceResult(source_id, (), e.attempts, str(e))
    try:
        records = parse_feed(response.body, source_id, datetime.now(UTC))
    except ValueError as e:
        return SourceResult(source_id, (), response.attempts, str(e))
    if not records:
        log.info("%s：エントリーが0件", source_id)
    return SourceResult(source_id, tuple(records), response.attempts)


def parse_feed(body: bytes, source_id: str, fetched_at: datetime) -> list[Record]:
    """フィードの本文を収集レコードにする。リンクのないエントリーは捨てる。

    本文（content）があれば一次情報の本文とし、要約（summary）は抜粋にする。
    フィードとして読めなければ ValueError。
    feedparser は外部実体を解決しない（テストで確かめている）。
    bytes をそのまま渡すとファイルのパスとして開こうとするので、BytesIO で包む。
    """
    parsed = feedparser.parse(io.BytesIO(body))
    if not parsed.get("version"):
        raise ValueError("フィードとして読めない応答")
    records = []
    for entry in parsed.entries:
        url = entry.get("link")
        if not url:
            continue
        published_at = _entry_time(entry)
        content = _content(entry)
        summary = entry.get("summary") or None
        if content == summary:
            summary = None  # RSS 2.0 は description が summary と content の両方に入ることがある
        records.append(
            Record(
                source_id=source_id,
                url=url,
                title=entry.get("title") or "",
                published_precision="datetime" if published_at else "unknown",
                fetched_at=fetched_at,
                events=(Event("published", published_at),) if published_at else (),
                excerpt=summary,
                primary_text=content,
                primary_text_origin="rss_full" if content else None,
                published_at=published_at,
                links=_links(content, summary),
            )
        )
    return records


def _entry_time(entry) -> datetime | None:
    """公開日時。Atom の updated は更新日時で公開日時ではないので使わない（なければ不明）。"""
    parsed = entry.get("published_parsed")  # feedparser が UTC の struct_time にそろえる
    if parsed is None:
        return None
    return datetime.fromtimestamp(calendar.timegm(parsed), UTC)


def _content(entry) -> str | None:
    parts = [c.get("value", "") for c in entry.get("content", [])]
    return "\n".join(p for p in parts if p) or None


def _links(*texts: str | None) -> tuple[str, ...]:
    """本文・抜粋の <a href> を順に集める。正規化と http(s) の絞り込みはまとめるときに行う。"""
    links: list[str] = []
    for text in texts:
        if text:
            parser = _LinkParser()  # 閉じていないタグが次の文章を飲み込まないよう、文章ごとに作る
            parser.feed(text)
            parser.close()
            links.extend(parser.links)
    return tuple(links)


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
