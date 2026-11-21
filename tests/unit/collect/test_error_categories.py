"""RSS・Qiita・last30days-skill の失敗に、理由の分類が付くこと（#40。末尾の表示の元）。"""

import subprocess
from datetime import date

from tech_researcher.collect.http import FetchError, Response
from tech_researcher.collect.last30days import collect_last30days
from tech_researcher.collect.qiita import collect_qiita
from tech_researcher.collect.rss import collect_feed
from tech_researcher.periods import daily_target
from tests.unit.collect.test_last30days import CONFIG, PERIOD, ROOT, FakeRun, done
from tests.unit.collect.test_qiita import QIITA
from tests.unit.collect.test_rss import FEED

TARGET = daily_target(date(2026, 11, 3), 1)


def failing(error):
    def fetch(url, limits):
        raise error

    return fetch


def returning(body):
    def fetch(url, limits):
        return Response(url, 200, body, 1)

    return fetch


def test_rss_fetch_failure_has_the_http_category(limits):
    result = collect_feed(FEED, limits, fetch_url=failing(FetchError("HTTP 503", 3, 503)))
    assert result.error_category == "http_5xx"
    result = collect_feed(FEED, limits, fetch_url=failing(FetchError("遅い", 3, None)))
    assert result.error_category == "network"


def test_rss_unreadable_body_is_an_invalid_response(limits):
    result = collect_feed(FEED, limits, fetch_url=returning(b"<html>error</html>"))
    assert result.error is not None and result.error_category == "invalid_response"


def test_qiita_failures_have_categories(limits):
    result = collect_qiita(
        QIITA, TARGET.period, limits, fetch_url=failing(FetchError("HTTP 429", 3, 429))
    )
    assert result.error_category == "rate_limited"
    result = collect_qiita(QIITA, TARGET.period, limits, fetch_url=returning(b"{}"))
    assert result.error_category == "invalid_response"


def test_last30days_timeout_exit_code_and_contract_have_categories():
    timeout = subprocess.TimeoutExpired("x", 1)
    results = collect_last30days(
        CONFIG, PERIOD, ROOT, github_token="t", run=FakeRun(*[timeout] * 10)
    )
    assert {r.error_category for r in results} == {"timeout"}
    results = collect_last30days(
        CONFIG, PERIOD, ROOT, github_token="t", run=FakeRun(*[done(returncode=1)] * 10)
    )
    assert {r.error_category for r in results} == {"exit_code"}
    results = collect_last30days(
        CONFIG, PERIOD, ROOT, github_token="t", run=FakeRun(done(stdout="not json"))
    )
    assert {r.error_category for r in results} == {"invalid_response"}
