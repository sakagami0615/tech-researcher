import json
import socket
import urllib.error
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest

from tech_researcher.collect import github_api
from tech_researcher.collect.github_api import (
    GitHubClient,
    GitHubError,
    classify,
    collect_token,
)
from tech_researcher.collect.http import FetchError, Response, _Retryable

TOKEN = "ghp_" + "x" * 36  # 形だけの偽のトークン


class FakeFetch:
    """順に応答（JSON にする値・Response・例外）を返す。受けた要求を残す。

    本物の fetch と同じく、試行は渡された再試行の回数＋1を超えない。
    """

    def __init__(self, *outcomes, attempts=1):
        self.outcomes = list(outcomes)
        self.attempts = attempts
        self.calls = []

    def __call__(self, url, limits, *, headers):
        self.calls.append((url, limits.http_retry_waits_s, dict(headers)))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, Response):
            return outcome
        attempts = min(self.attempts, len(limits.http_retry_waits_s) + 1)
        return Response(url, 200, json.dumps(outcome).encode(), attempts)


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def client(limits, fetch, token=TOKEN, clock=None):
    clock = clock or Clock()
    return GitHubClient(token, limits, fetch_url=fetch, clock=clock, sleep=clock.sleep)


def test_token_is_read_from_environment_and_stripped():
    assert collect_token({"GITHUB_TOKEN": f"{TOKEN}\r\n"}) == TOKEN
    assert collect_token({"GITHUB_TOKEN": ""}) is None
    assert collect_token({}) is None


@pytest.mark.parametrize("bad", ["tok en", "tok\nen", "tok:en", "tokéñ"])
def test_token_with_unusable_characters_is_rejected_without_the_value(bad):
    with pytest.raises(GitHubError) as e:
        collect_token({"GITHUB_TOKEN": bad})
    assert e.value.category == github_api.AUTH
    assert bad.strip() not in str(e.value)
    with pytest.raises(GitHubError) as e:
        GitHubClient(bad, None)
    assert bad not in str(e.value)


def test_get_sends_token_and_api_headers_to_api_host(limits):
    fetch = FakeFetch({"ok": True})
    api = client(limits, fetch)
    assert api.get("/repos/a/b", {"per_page": 1}) == {"ok": True}
    url, _, headers = fetch.calls[0]
    assert url == "https://api.github.com/repos/a/b?per_page=1"
    assert headers == {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {TOKEN}",
    }
    assert api.rest_calls == 1 and api.search_calls == 0


def test_without_token_no_authorization_header(limits):
    fetch = FakeFetch({})
    api = client(limits, fetch, token=None)
    api.get("/rate_limit")
    assert "Authorization" not in fetch.calls[0][2]
    assert not api.authenticated


def test_repr_does_not_show_token(limits):
    api = client(limits, FakeFetch())
    assert TOKEN not in repr(api)


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.example/x",
        "//evil.example/x",
        "repos/a/b",
        "/repos/a/b?x=1",
        "/a b",
        "/a@b",
        "/a/../b",
        "/a/",
    ],
)
def test_path_cannot_change_host(limits, path):
    fetch = FakeFetch()
    with pytest.raises(ValueError):
        client(limits, fetch).get(path)
    assert fetch.calls == []


def test_search_must_use_search_method(limits):
    with pytest.raises(ValueError):
        client(limits, FakeFetch()).get("/search/repositories")


def test_rest_budget_counts_retries_and_trims_them(limits):
    limits = replace(limits, github_rest_calls=4)
    fetch = FakeFetch({}, {}, attempts=3)
    api = client(limits, fetch)
    api.get("/a")
    assert api.rest_calls == 3
    api.get("/b")  # 残り1回なので再試行はしない
    assert fetch.calls[1][1] == ()
    assert api.rest_calls == 4
    with pytest.raises(GitHubError) as e:
        api.get("/c")
    assert e.value.category == github_api.BUDGET and e.value.attempts == 0
    assert len(fetch.calls) == 2


def test_failed_requests_are_counted(limits):
    fetch = FakeFetch(FetchError("再試行を使い切った：HTTP 503", 3, 503))
    api = client(limits, fetch)
    with pytest.raises(GitHubError) as e:
        api.get("/a")
    assert (e.value.category, e.value.attempts, e.value.status) == (github_api.HTTP_5XX, 3, 503)
    assert api.rest_calls == 3


def test_search_is_spaced_by_per_minute_limit(limits):
    limits = replace(limits, github_search_per_minute=25)
    clock = Clock()
    fetch = FakeFetch({}, {}, {})
    api = client(limits, fetch, clock=clock)
    api.search("repositories", {"q": "x"})
    clock.now += 1.0
    api.search("repositories", {"q": "y"})
    api.search("repositories", {"q": "z"})
    assert clock.sleeps == [pytest.approx(1.4), pytest.approx(2.4)]
    assert api.search_calls == 3 and api.rest_calls == 0
    assert urlsplit(fetch.calls[0][0]).path == "/search/repositories"
    assert parse_qs(urlsplit(fetch.calls[0][0]).query) == {"q": ["x"]}


def test_unauthenticated_search_is_spaced_for_ten_per_minute(limits):
    clock = Clock()
    api = client(replace(limits, github_search_per_minute=25), FakeFetch({}, {}), None, clock)
    api.search("repositories", {"q": "x"})
    api.search("repositories", {"q": "y"})
    assert clock.sleeps == [pytest.approx(6.0)]


def test_search_budget(limits):
    api = client(replace(limits, github_search_calls=1), FakeFetch({}))
    api.search("repositories", {"q": "x"})
    with pytest.raises(GitHubError) as e:
        api.search("repositories", {"q": "y"})
    assert e.value.category == github_api.BUDGET


def test_invalid_json_is_an_error(limits):
    fetch = FakeFetch(Response("u", 200, b"<html>", 1))
    api = client(limits, fetch)
    with pytest.raises(GitHubError) as e:
        api.get("/a")
    assert e.value.category == github_api.INVALID_RESPONSE
    assert api.rest_calls == 1


@pytest.mark.parametrize(
    "status, headers, category",
    [
        (401, {}, github_api.AUTH),
        (403, {"X-RateLimit-Remaining": "0"}, github_api.RATE_LIMITED),
        (403, {"retry-after": "60"}, github_api.RATE_LIMITED),
        (403, {"x-ratelimit-remaining": "12"}, github_api.FORBIDDEN),
        (429, {}, github_api.RATE_LIMITED),
        (404, {}, github_api.NOT_FOUND),
        (422, {}, github_api.HTTP_4XX),
        (502, {}, github_api.HTTP_5XX),
    ],
)
def test_http_errors_are_classified(status, headers, category):
    assert classify(FetchError(f"HTTP {status}", 1, status, headers)) == category


def _chained(reason):
    """http.fetch が再試行を使い切ったときの例外の連なりを作る。"""
    try:
        try:
            try:
                raise urllib.error.URLError(reason)
            except urllib.error.URLError as e:
                raise _Retryable(str(e)) from e
        except _Retryable as e:
            raise FetchError("再試行を使い切った", 3) from e
    except FetchError as e:
        return e


@pytest.mark.parametrize(
    "reason, category",
    [
        (TimeoutError("timed out"), github_api.TIMEOUT),
        (socket.gaierror(-2, "Name or service not known"), github_api.DNS),
        (ConnectionResetError(), github_api.NETWORK),
    ],
)
def test_network_errors_are_classified(reason, category):
    assert classify(_chained(reason)) == category


def test_error_message_has_no_token(limits):
    fetch = FakeFetch(FetchError("HTTP 401", 1, 401))
    with pytest.raises(GitHubError) as e:
        client(limits, fetch).get("/user")
    assert TOKEN not in str(e.value) and TOKEN not in repr(e.value)
    assert e.value.category == github_api.AUTH
