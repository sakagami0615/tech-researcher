"""RSS・Atom の収集（D-03・D-11・D-14、data-model.md 2.3〜2.7）。

フィードを取得して、エントリーを収集レコードにする。期間での絞り込みは、
ほかの情報源とまとめた後に period_filter で行う（公開日時を全出典で判断するため）。
人気型も新着型と同じく取得する。初登場の判定（D-13）は、それを実装する Issue で足す。
"""

import calendar
import io
import logging
from datetime import UTC, datetime
from html.parser import HTMLParser

import feedparser

from tech_researcher.collect.http import FetchError, fetch
from tech_researcher.collect.records import Attention, Event, Record, SourceResult
from tech_researcher.config import Feed, Limits

log = logging.getLogger(__name__)


def collect_feed(feed: Feed, limits: Limits, *, fetch_url=fetch) -> SourceResult:
    """フィードを取得する。フィードとして読めない応答（HTML のエラーページなど）は失敗にする。

    pages が2以上なら、続くページも取る（GitHub Changelog。D-11）。どれかのページが失敗したら、
    情報源全体を失敗にする。ページの間で重なったエントリーは1件にする。
    """
    source_id = f"rss:{feed.id}"
    fetched_at = datetime.now(UTC)
    records: dict[str, Record] = {}
    attempts = 0
    for page in range(1, feed.pages + 1):
        try:
            response = fetch_url(_page_url(feed.url, page), limits)
        except FetchError as e:
            return SourceResult(source_id, (), attempts + e.attempts, str(e))
        attempts += response.attempts
        try:
            for record in parse_feed(response.body, feed, fetched_at):
                records.setdefault(record.url, record)
        except ValueError as e:
            return SourceResult(source_id, (), attempts, str(e))
    if not records:
        log.info("%s：エントリーが0件", source_id)
    return SourceResult(source_id, tuple(records.values()), attempts)


def _page_url(url: str, page: int) -> str:
    if page == 1:
        return url
    return f"{url}{'&' if '?' in url else '?'}paged={page}"


def parse_feed(body: bytes, feed: Feed, fetched_at: datetime) -> list[Record]:
    """フィードの本文を収集レコードにする。リンクのないエントリーは捨てる。

    設定の full_text の欄を一次情報の本文、excerpt の欄を抜粋にする（D-14）。
    entry_date が posted なら、エントリーの日時（はてなの dc:date など）を投稿日時とし、
    公開日時は不明とする（D-11・D-13）。
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
        texts = {"content": _content(entry), "summary": entry.get("summary") or None}
        primary = texts.get(feed.full_text)  # none なら本文なし
        excerpt = texts[feed.excerpt]
        if excerpt == primary:
            excerpt = None  # 同じ欄か、RSS 2.0 で description が両方に入ったとき
        published_at = posted_at = None
        if feed.entry_date == "posted":
            # はてなの dc:date・Atom の updated は feedparser が updated に入れる
            posted_at = _time(entry, "published_parsed") or _time(entry, "updated_parsed")
        else:
            # Atom の updated は更新日時で公開日時ではないので使わない（なければ不明）
            published_at = _time(entry, "published_parsed")
        events = []
        if published_at:
            events.append(Event("published", published_at))
        if posted_at:
            events.append(Event("posted", posted_at))
        records.append(
            Record(
                source_id=f"rss:{feed.id}",
                url=url,
                title=entry.get("title") or "",
                published_precision="datetime" if published_at else "unknown",
                fetched_at=fetched_at,
                events=tuple(events),
                excerpt=excerpt,
                primary_text=primary,
                primary_text_origin="rss_full" if primary else None,
                published_at=published_at,
                posted_at=posted_at,
                attention=_attention(entry, fetched_at),
                links=_links(primary, excerpt),
            )
        )
    return records


def _time(entry, key: str) -> datetime | None:
    parsed = entry.get(key)  # feedparser が UTC の struct_time にそろえる
    if parsed is None:
        return None
    return datetime.fromtimestamp(calendar.timegm(parsed), UTC)


def _attention(entry, measured_at: datetime) -> tuple[Attention, ...]:
    """はてなブックマークのブックマーク数（data-model.md 2.4）。読めなければ持たせない。"""
    count = entry.get("hatena_bookmarkcount")
    if count is None or not str(count).strip().isdigit():
        return ()
    return (Attention("hatena_bookmarks", float(int(count)), measured_at),)


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
