"""RSS・Qiita・last30days-skill の失敗に、理由の分類が付くこと（#40。末尾の表示の元）。"""

import gzip
import subprocess
from datetime import date

from tech_researcher.collect import http as tr_http
from tech_researcher.collect.http import FetchError, Response, classify
from tech_researcher.collect.last30days import collect_last30days
from tech_researcher.collect.qiita import collect_qiita
from tech_researcher.collect.rss import collect_feed
from tech_researcher.periods import daily_target
from tests.unit.collect.test_http import FakeResponse, run
from tests.unit.collect.test_last30days import CONFIG, PERIOD, ROOT, FakeRun, done, output
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


def test_unreadable_responses_are_invalid_responses(limits, monkeypatch):
    """応答の大きさの超過と、gzip を展開できない失敗は、状態コードがなくても分類が付く。"""
    monkeypatch.setattr(tr_http, "MAX_BODY_BYTES", 3)
    too_large, _, _ = run(limits, b"1234")
    assert classify(too_large) == "invalid_response"
    broken, _, _ = run(limits, FakeResponse(b"not gzip", "gzip"))
    assert classify(broken) == "invalid_response"
    truncated, _, _ = run(limits, FakeResponse(gzip.compress(b"a" * 100)[:10], "gzip"))
    assert classify(truncated) == "invalid_response"


def test_redirect_status_is_classified_as_a_redirect():
    assert classify(FetchError("HTTP 302", 1, 302)) == "redirect"
    assert classify(FetchError("HTTP 404", 1, 404)) == "not_found"


def test_last30days_unknown_state_is_invalid_but_known_failure_has_no_category():
    def states(**status):
        out = output(status, [])
        run = FakeRun(done(3, out))
        return {
            r.source_id: r.error_category
            for r in collect_last30days(CONFIG, PERIOD, ROOT, github_token="t", run=run)
        }

    got = states(hackernews="rate-limited", github="brand-new-state")
    assert got == {"hackernews": None, "github_search": "invalid_response"}
    assert states(hackernews="ok")["github_search"] == "invalid_response"  # 状態が出力にない
