import json
from dataclasses import replace
from datetime import UTC, date, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from tech_researcher.collect.aggregate import aggregate
from tech_researcher.collect.http import FetchError, Response
from tech_researcher.collect.period_filter import in_period
from tech_researcher.collect.qiita import collect_qiita, parse_items, search_query
from tech_researcher.config import Qiita
from tech_researcher.periods import JST, Period, daily_target, weekly_target

QIITA = Qiita(True, "Qiita", ("LLM", "生成AI"))
TARGET = daily_target(date(2026, 11, 3), 1)  # 期間は 11/2 0:00〜11/3 0:00（JST）
NOW = datetime(2026, 11, 2, 21, 0, tzinfo=UTC)


def item(n, created_at="2026-11-02T12:00:00+09:00", **extra):
    return {
        "id": f"id{n}",
        "url": f"https://qiita.com/u/items/{n}",
        "title": f"記事{n}",
        "created_at": created_at,
        "likes_count": n,
        "body": "本文",
        "rendered_body": "",
        "tags": [{"name": "LLM", "versions": []}],
        **extra,
    }


class FakeApi:
    """ページごとの応答を返す。受けた要求の URL と、渡された再試行の待ちを残す。"""

    def __init__(self, pages, attempts=1):
        self.pages = pages
        self.attempts = attempts
        self.calls = []

    def __call__(self, url, limits):
        self.calls.append((url, limits.http_retry_waits_s))
        # 本物の fetch と同じく、試行は渡された再試行の回数＋1を超えない
        attempts = min(self.attempts, len(limits.http_retry_waits_s) + 1)
        page = int(parse_qs(urlsplit(url).query)["page"][0])
        body = self.pages[page - 1] if page <= len(self.pages) else []
        if isinstance(body, Exception):
            raise body
        return Response(url, 200, json.dumps(body).encode(), attempts)

    def queries(self):
        return [parse_qs(urlsplit(url).query) for url, _ in self.calls]


def test_query_uses_tags_and_jst_dates():
    assert search_query(QIITA.tags, TARGET.period) == (
        "tag:LLM,生成AI created:>=2026-11-02 created:<2026-11-03"
    )
    # 終わりが 0:00 でなければ、その日も含める
    period = Period(datetime(2026, 11, 1, 6, tzinfo=JST), datetime(2026, 11, 2, 6, tzinfo=JST))
    assert search_query(("AWS",), period).endswith("created:>=2026-11-01 created:<2026-11-03")
    weekly = weekly_target(date(2026, 11, 4), 7).period  # 10/26 0:00〜11/2 0:00
    assert search_query(("AWS",), weekly).endswith("created:>=2026-10-26 created:<2026-11-02")


def test_items_become_records():
    body = json.dumps(
        [
            item(
                3,
                created_at="2026-11-02T09:30:00+09:00",
                body="# 見出し\n\n" + "あ" * 300,
                rendered_body='<p><a href="https://openai.com/news/x">x</a></p>',
            )
        ]
    ).encode()
    (record,) = parse_items(body, NOW)
    assert record.source_id == "qiita" and record.url == "https://qiita.com/u/items/3"
    assert record.title == "記事3" and record.fetched_at == NOW
    assert record.published_at == datetime(2026, 11, 2, 0, 30, tzinfo=UTC)
    assert record.published_precision == "datetime"
    assert [(e.type, e.at) for e in record.events] == [("published", record.published_at)]
    assert [(a.metric, a.value) for a in record.attention] == [("qiita_likes", 3.0)]
    assert record.primary_text is None  # 一次情報ではない
    assert record.excerpt.startswith("# 見出し あ") and len(record.excerpt) == 200
    assert record.links == ("https://openai.com/news/x",)


def test_collect_sends_query_without_token(limits):
    api = FakeApi([[item(1), item(2)]])
    result = collect_qiita(QIITA, TARGET.period, limits, fetch_url=api)
    assert result.error is None and result.attempts == 1
    assert [r.url for r in result.records] == [
        "https://qiita.com/u/items/1",
        "https://qiita.com/u/items/2",
    ]
    (query,) = api.queries()
    assert query == {
        "page": ["1"],
        "per_page": ["100"],
        "query": ["tag:LLM,生成AI created:>=2026-11-02 created:<2026-11-03"],
    }
    assert api.calls[0][0].startswith("https://qiita.com/api/v2/items?")


def test_pages_are_followed_until_a_short_page(limits):
    full = [item(n) for n in range(100)]
    api = FakeApi([full, [item(99), item(100)]], attempts=2)  # 重なった記事は1件にする
    result = collect_qiita(QIITA, TARGET.period, limits, fetch_url=api)
    assert result.error is None and len(result.records) == 101
    assert [q["page"] for q in api.queries()] == [["1"], ["2"]]
    assert result.attempts == 4


def test_full_last_page_is_followed_by_an_empty_page(limits):
    api = FakeApi([[item(n) for n in range(100)]])
    result = collect_qiita(QIITA, TARGET.period, limits, fetch_url=api)
    assert result.error is None and len(result.records) == 100 and len(api.calls) == 2


def test_requests_stop_at_the_limit(limits):
    full = [item(n) for n in range(100)]
    api = FakeApi([full] * 50)
    result = collect_qiita(QIITA, TARGET.period, replace(limits, qiita_calls=30), fetch_url=api)
    assert result.records == () and "上限 30 回" in result.error
    assert len(api.calls) == 30 and result.attempts == 30


def test_retries_are_cut_to_the_remaining_requests(limits):
    # 再試行も要求に数える。残りが少ないページは、再試行の回数を減らして上限を超えない
    full = [item(n) for n in range(100)]
    api = FakeApi([full] * 10, attempts=1)
    collect_qiita(QIITA, TARGET.period, replace(limits, qiita_calls=3), fetch_url=api)
    assert [waits for _, waits in api.calls] == [(5, 20), (5,), ()]


def test_retries_never_exceed_the_limit(limits):
    # 毎ページ再試行を使い切る（3回）とき、7回の上限は 3＋3＋1 で止まる
    full = [item(n) for n in range(100)]
    api = FakeApi([full] * 10, attempts=3)
    result = collect_qiita(QIITA, TARGET.period, replace(limits, qiita_calls=7), fetch_url=api)
    assert "上限 7 回" in result.error and result.attempts == 7 and len(api.calls) == 3


def test_failed_attempts_are_added_to_the_count(limits):
    full = [item(n) for n in range(100)]
    api = FakeApi([full, FetchError("再試行を使い切った：HTTP 503", 2, 503)])
    result = collect_qiita(QIITA, TARGET.period, replace(limits, qiita_calls=3), fetch_url=api)
    assert result.records == () and "503" in result.error and result.attempts == 3


def test_page_limit_of_the_api_fails_the_source(limits):
    api = FakeApi([[item(n) for n in range(100)]] * 101)
    result = collect_qiita(QIITA, TARGET.period, replace(limits, qiita_calls=200), fetch_url=api)
    assert "ページの上限 100" in result.error and len(api.calls) == 100


def test_zero_items_is_a_success(limits, caplog):
    with caplog.at_level("INFO"):
        result = collect_qiita(QIITA, TARGET.period, limits, fetch_url=FakeApi([[]]))
    assert result.records == () and result.error is None and result.attempts == 1
    assert "0件" in caplog.text


def test_failure_on_a_later_page_fails_the_source(limits):
    # 認証なしの利用制限の超過（403）は、http.fetch が再試行せずに失敗にする
    full = [item(n) for n in range(100)]
    api = FakeApi([full, FetchError("HTTP 403", 1, 403)])
    result = collect_qiita(QIITA, TARGET.period, limits, fetch_url=api)
    assert result.records == () and result.error == "HTTP 403" and result.attempts == 2


@pytest.mark.parametrize(
    "body",
    [
        b"<html>error</html>",
        b'{"message": "not a list"}',
        json.dumps([{"url": "https://qiita.com/u/items/1"}]).encode(),  # created_at がない
        json.dumps([item(1, created_at="2026-11-02T12:00:00")]).encode(),  # タイムゾーンがない
    ],
)
def test_unreadable_response_fails_the_source(limits, body):
    def fetch(url, limits):
        return Response(url, 200, body, 1)

    result = collect_qiita(QIITA, TARGET.period, limits, fetch_url=fetch)
    assert result.records == () and "Qiita API の応答として読めない" in result.error


def test_period_boundaries_are_decided_after_aggregation(limits):
    # API の日付の条件は日単位。時刻での判定は period_filter が [start, end) で行う
    pages = [
        [
            item(1, created_at="2026-11-02T00:00:00+09:00"),  # 始まり（含む）
            item(2, created_at="2026-11-02T23:59:59+09:00"),
            item(3, created_at="2026-11-03T00:00:00+09:00"),  # 終わり（含まない）
            item(4, created_at="2026-11-01T23:59:59+09:00"),
        ]
    ]
    result = collect_qiita(QIITA, TARGET.period, limits, fetch_url=FakeApi(pages))
    kept = [i.url for i in aggregate(result.records) if in_period(i, TARGET)]
    assert kept == ["https://qiita.com/u/items/1", "https://qiita.com/u/items/2"]
