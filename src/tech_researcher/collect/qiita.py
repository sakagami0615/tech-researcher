"""Qiita API v2 によるタグ別の新着の収集（D-11・D-21、data-model.md 2.3〜2.5）。

認証なしで `GET /api/v2/items` を使い、設定のタグ（どれかを持つ記事）と作成日で検索する。
検索の日付は JST の日付として効くので、対象期間を覆う日付で問い合わせ、
時刻での絞り込みは、ほかの情報源とまとめた後に period_filter で行う。
要求の回数（再試行を含む）は1回の実行で limits.qiita_calls までとし、
超えそうなら要求を出さずに情報源を失敗にする（structure.md 6.5）。
"""

import json
import logging
import re
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from urllib.parse import urlencode

from tech_researcher.collect.http import FetchError, fetch
from tech_researcher.collect.records import Attention, Event, Record, SourceResult
from tech_researcher.collect.rss import html_links
from tech_researcher.config import Limits, Qiita
from tech_researcher.periods import JST, Period

log = logging.getLogger(__name__)

SOURCE_ID = "qiita"
API_URL = "https://qiita.com/api/v2/items"
PER_PAGE = 100  # API の上限
MAX_PAGE = 100  # API の上限。これより先のページは取れない
EXCERPT_CHARS = 200  # 抜粋にする本文（Markdown）の先頭の文字数


def search_query(tags: tuple[str, ...], period: Period) -> str:
    """タグのどれかを持ち、期間を覆う JST の日付に作成された記事の検索条件。

    `tag:A,B` はタグの OR。終わりが 0:00 でなければ、その日も含める。
    """
    start = period.start.astimezone(JST).date()
    end = period.end.astimezone(JST)
    end_day = end.date() if end.time() == time(0) else end.date() + timedelta(days=1)
    return f"tag:{','.join(tags)} created:>={start.isoformat()} created:<{end_day.isoformat()}"


def collect_qiita(qiita: Qiita, period: Period, limits: Limits, *, fetch_url=fetch) -> SourceResult:
    """対象期間を覆う新着を、ページを送りながらすべて取る。

    どれかのページが失敗するか、要求の上限に達したら、情報源全体を失敗にする（部分レポート）。
    再試行も要求に数え、残りの回数を超えないよう、1ページの再試行の回数を残りに合わせて減らす。
    重なった記事は先に出たものを残す。
    """
    fetched_at = datetime.now(UTC)
    query = search_query(qiita.tags, period)
    records: dict[str, Record] = {}
    attempts = 0
    for page in range(1, MAX_PAGE + 1):
        remaining = limits.qiita_calls - attempts
        if remaining < 1:
            return _failure(attempts, f"要求の上限 {limits.qiita_calls} 回に達した")
        page_limits = replace(limits, http_retry_waits_s=limits.http_retry_waits_s[: remaining - 1])
        url = f"{API_URL}?{urlencode({'page': page, 'per_page': PER_PAGE, 'query': query})}"
        try:
            response = fetch_url(url, page_limits)
        except FetchError as e:
            return _failure(attempts + e.attempts, str(e))
        attempts += response.attempts
        try:
            items = parse_items(response.body, fetched_at)
        except ValueError as e:
            return _failure(attempts, str(e))
        for record in items:
            records.setdefault(record.url, record)
        if len(items) < PER_PAGE:
            break
    else:
        return _failure(attempts, f"ページの上限 {MAX_PAGE} に達した")
    if not records:
        log.info("%s：記事が0件", SOURCE_ID)
    return SourceResult(SOURCE_ID, tuple(records.values()), attempts)


def _failure(attempts: int, message: str) -> SourceResult:
    return SourceResult(SOURCE_ID, (), attempts, message)


def parse_items(body: bytes, fetched_at: datetime) -> list[Record]:
    """API の応答（記事の配列）を収集レコードにする。形が違えば ValueError。

    作成日時を公開日時、いいね数を注目度（qiita_likes）とする。本文は一次情報ではないので
    primary_text には入れず、Markdown の先頭を抜粋にする。候補リンクは描画後の本文から集める。
    API の契約が変わったと見なし、1件でも必須の値（url・created_at）が欠ければ全体を失敗にする。
    """
    try:
        data = json.loads(body)
        if not isinstance(data, list):
            raise TypeError("配列でない")
        return [_record(item, fetched_at) for item in data]
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise ValueError(f"Qiita API の応答として読めない：{type(e).__name__}: {e}") from e


def _record(item: dict, fetched_at: datetime) -> Record:
    published_at = datetime.fromisoformat(item["created_at"])
    if published_at.tzinfo is None:
        raise ValueError("created_at にタイムゾーンがない")
    published_at = published_at.astimezone(UTC)
    url = item["url"]
    if not isinstance(url, str) or not url:
        raise ValueError("url がない")
    likes = item.get("likes_count")
    attention = ()
    if isinstance(likes, int) and not isinstance(likes, bool):
        attention = (Attention("qiita_likes", float(likes), fetched_at),)
    return Record(
        source_id=SOURCE_ID,
        url=url,
        title=item.get("title") or "",
        published_precision="datetime",
        fetched_at=fetched_at,
        events=(Event("published", published_at),),
        excerpt=_excerpt(item.get("body")),
        published_at=published_at,
        attention=attention,
        links=html_links(item.get("rendered_body")),
    )


def _excerpt(body: str | None) -> str | None:
    if not body:
        return None
    text = re.sub(r"\s+", " ", body).strip()
    return text[:EXCERPT_CHARS] or None
