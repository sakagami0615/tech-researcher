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
        self.bodies = []  # POST の本文（GET は None）

    def __call__(self, url, limits, *, headers, sleep, follow_redirects, data=None):
        assert follow_redirects is False
        self.calls.append((url, limits.http_retry_waits_s, dict(headers)))
        self.bodies.append(data)
        self.sleep = sleep
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


def test_search_budget(limits):
    clock = Clock()
    api = client(replace(limits, github_search_calls=1), FakeFetch({}), clock=clock)
    api.search("repositories", {"q": "x"})
    with pytest.raises(GitHubError) as e:
        api.search("repositories", {"q": "y"})
    assert e.value.category == github_api.BUDGET
    assert clock.sleeps == []  # 上限に達していれば間隔を待たない


def test_invalid_json_is_an_error(limits):
    fetch = FakeFetch(Response("https://api.github.com/a", 200, b"<html>", 1))
    api = client(limits, fetch)
    with pytest.raises(GitHubError) as e:
        api.get("/a")
    assert e.value.category == github_api.INVALID_RESPONSE
    assert api.rest_calls == 1


def test_too_deeply_nested_json_is_an_error(limits):
    api = client(limits, FakeFetch(Response("https://api.github.com/a", 200, b"[" * 100_000, 1)))
    with pytest.raises(GitHubError) as e:
        api.get("/a")
    assert e.value.category == github_api.INVALID_RESPONSE


def redirect(location, status=301):
    return FetchError(f"HTTP {status}", 1, status, {"Location": location})


def test_redirect_inside_api_is_followed_and_counted(limits):
    fetch = FakeFetch(
        redirect("/repositories/1"), redirect("https://api.github.com/r/2", 307), {"ok": 1}
    )
    api = client(limits, fetch)
    assert api.get("/repos/old/name") == {"ok": 1}
    assert [c[0] for c in fetch.calls] == [
        "https://api.github.com/repos/old/name",
        "https://api.github.com/repositories/1",
        "https://api.github.com/r/2",
    ]
    assert fetch.calls[2][2]["Authorization"] == f"Bearer {TOKEN}"
    assert api.rest_calls == 3


def test_redirect_stops_at_budget(limits):
    fetch = FakeFetch(redirect("/a2"), {"never": 1})
    api = client(replace(limits, github_rest_calls=1), fetch)
    with pytest.raises(GitHubError) as e:
        api.get("/a")
    assert e.value.category == github_api.BUDGET
    assert len(fetch.calls) == 1 and api.rest_calls == 1


def test_failed_request_after_redirect_counts_all(limits):
    fetch = FakeFetch(redirect("/a2"), FetchError("HTTP 404", 1, 404))
    api = client(limits, fetch)
    with pytest.raises(GitHubError) as e:
        api.get("/a")
    assert (e.value.category, api.rest_calls) == (github_api.NOT_FOUND, 2)


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/x",
        "//evil.example/x",
        "http://api.github.com/x",
        "https://api.github.com.evil/x",
        "",
    ],
)
def test_redirect_outside_api_is_refused(limits, location):
    fetch = FakeFetch(redirect(location))
    with pytest.raises(GitHubError) as e:
        client(limits, fetch).get("/a")
    assert e.value.category == github_api.REDIRECT
    assert len(fetch.calls) == 1


@pytest.mark.parametrize(
    "path, location",
    [
        ("/a", "/x y"),  # パスに空白
        ("/a", "/x?q=a b"),  # クエリに空白
        ("/a", "/x?q=\u00e9"),  # クエリに ASCII 以外
        ("/a", "/search/repositories?q=x"),  # REST から Search へ
        ("/search/repositories", "/repos/a/b"),  # Search から REST へ
    ],
)
def test_unusable_redirect_is_refused(limits, path, location):
    fetch = FakeFetch(redirect(location))
    api = client(limits, fetch)
    with pytest.raises(GitHubError) as e:
        api.search("repositories", {}) if path.startswith("/search/") else api.get(path)
    assert e.value.category == github_api.REDIRECT
    assert len(fetch.calls) == 1


def test_search_redirect_waits_for_interval(limits):
    clock = Clock()
    fetch = FakeFetch(redirect("/search/repositories?q=y"), {})
    api = client(replace(limits, github_search_per_minute=25), fetch, clock=clock)
    api.search("repositories", {"q": "x"})
    assert clock.sleeps == [pytest.approx(2.4)]
    assert api.search_calls == 2


def test_too_many_redirects(limits):
    fetch = FakeFetch(*[redirect(f"/r{i}") for i in range(github_api.MAX_REDIRECTS + 1)])
    with pytest.raises(GitHubError) as e:
        client(limits, fetch).get("/a")
    assert e.value.category == github_api.REDIRECT
    assert len(fetch.calls) == github_api.MAX_REDIRECTS + 1


def test_search_retries_also_wait_for_interval(limits):
    clock = Clock()
    fetch = FakeFetch({})
    api = client(replace(limits, github_search_per_minute=25), fetch, clock=clock)
    api.search("repositories", {"q": "x"})
    fetch.sleep(1)  # http.fetch が Retry-After: 1 で待つとき
    fetch.sleep(30)
    assert clock.sleeps == [pytest.approx(2.4), 30]
    clock.sleeps.clear()
    fetch.outcomes.append({})
    api.get("/a")
    fetch.sleep(1)  # Search 以外は間隔を足さない
    assert clock.sleeps == [1]


def test_secondary_rate_limit_is_found_in_body():
    body = b'{"message": "You have exceeded a secondary rate limit."}'
    error = FetchError("HTTP 403", 1, 403, {"x-ratelimit-remaining": "4000"}, body)
    assert classify(error) == github_api.RATE_LIMITED


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


def test_graphql_posts_query_with_token_and_counts_calls(limits):
    fetch = FakeFetch({"data": {"r0": None}}, attempts=2)
    api = client(limits, fetch)
    assert api.graphql("query($o0: String!) { x }", {"o0": "a"}) == {"data": {"r0": None}}
    url, waits, headers = fetch.calls[0]
    assert url == "https://api.github.com/graphql"
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Content-Type"] == "application/json"
    assert json.loads(fetch.bodies[0]) == {
        "query": "query($o0: String!) { x }",
        "variables": {"o0": "a"},
    }
    assert waits == (5, 20)
    assert (api.graphql_calls, api.rest_calls, api.search_calls) == (2, 0, 0)


def test_graphql_without_token_sends_nothing(limits):
    fetch = FakeFetch()
    with pytest.raises(GitHubError) as e:
        client(limits, fetch, token=None).graphql("{ x }", {})
    assert (e.value.category, e.value.attempts, fetch.calls) == (github_api.AUTH, 0, [])


def test_graphql_budget_counts_retries_and_trims_them(limits):
    fetch = FakeFetch({}, attempts=3)
    api = client(replace(limits, github_graphql_calls=2), fetch)
    api.graphql("{ x }", {})
    assert fetch.calls[0][1] == (5,)  # 残り2回なので再試行は1回まで
    assert api.graphql_calls == 2
    with pytest.raises(GitHubError) as e:
        api.graphql("{ x }", {})
    assert e.value.category == github_api.BUDGET
    assert len(fetch.calls) == 1


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (FetchError("HTTP 401", 1, 401), github_api.AUTH),
        (FetchError("HTTP 403", 1, 403, {"x-ratelimit-remaining": "0"}), github_api.RATE_LIMITED),
        (FetchError("再試行を使い切った：HTTP 502", 3, 502), github_api.HTTP_5XX),
        (
            FetchError("HTTP 301", 1, 301, {"location": "https://api.github.com/x"}),
            github_api.REDIRECT,
        ),
    ],
)
def test_graphql_http_failures_are_classified_and_counted(limits, error, category):
    api = client(limits, FakeFetch(error))
    with pytest.raises(GitHubError) as e:
        api.graphql("{ x }", {})
    assert e.value.category == category
    assert api.graphql_calls == error.attempts
    assert TOKEN not in str(e.value)


def test_graphql_invalid_json_is_an_error(limits):
    fetch = FakeFetch(Response("https://api.github.com/graphql", 200, b"<html>", 1))
    api = client(limits, fetch)
    with pytest.raises(GitHubError) as e:
        api.graphql("{ x }", {})
    assert e.value.category == github_api.INVALID_RESPONSE
    assert api.graphql_calls == 1


def test_graphql_deeply_nested_json_is_an_invalid_response(limits):
    body = b"[" * 100_000 + b"]" * 100_000
    api = client(limits, FakeFetch(Response("https://api.github.com/graphql", 200, body, 1)))
    with pytest.raises(GitHubError) as e:
        api.graphql("{ x }", {})
    assert e.value.category == github_api.INVALID_RESPONSE


def test_json_depth_limit_does_not_depend_on_python_recursion(limits):
    # 釣り合った深い入れ子は、Python の版によっては json が例外にせず読めてしまう
    deep = b"[" * (github_api.MAX_JSON_DEPTH + 1) + b"]" * (github_api.MAX_JSON_DEPTH + 1)
    api = client(limits, FakeFetch(Response("https://api.github.com/a", 200, deep, 1)))
    with pytest.raises(GitHubError) as e:
        api.get("/a")
    assert e.value.category == github_api.INVALID_RESPONSE


def test_json_at_the_depth_limit_is_read():
    ok = "[" * github_api.MAX_JSON_DEPTH + "]" * github_api.MAX_JSON_DEPTH
    assert github_api.load_json(ok) is not None
    assert github_api.load_json('{"a": {"b": [1, {"c": 2}]}}') == {"a": {"b": [1, {"c": 2}]}}
