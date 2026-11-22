import functools
import io
import json
import urllib.error
from dataclasses import replace
from email.message import Message
from pathlib import Path

import pytest

from tech_researcher.collect.http import FetchError, Response, fetch
from tech_researcher.config import load_config
from tech_researcher.publish.github_issues import IssueRef
from tech_researcher.publish.github_projects import (
    NUMBER_ENV,
    OWNER_ENV,
    TOKEN_ENV,
    ProjectsConfig,
    ProjectsError,
    ProjectsRegistrar,
    ProjectsTarget,
    projects_config,
)
from tech_researcher.publish.github_write import IssueWriter, WriteBudget

TOKEN = "ghp_" + "p" * 36  # 形だけの偽のトークン
CONFIG_PATH = Path(__file__).parents[3] / "config" / "tech-researcher.toml"
CONFIG = ProjectsConfig(TOKEN, ProjectsTarget("octocat", 3))
ISSUE = IssueRef("owner/repo", 7)
ENV = {TOKEN_ENV: TOKEN, OWNER_ENV: "octocat", NUMBER_ENV: "3"}


@pytest.fixture
def limits():
    return replace(
        load_config(CONFIG_PATH).limits, http_retry_waits_s=(5, 20), github_write_calls=4
    )


class FakeFetch:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, url, limits, *, headers, follow_redirects, data, **kw):
        self.calls.append(
            {"url": url, "limits": limits, "headers": dict(headers), "data": data, "kw": kw}
        )
        assert follow_redirects is False
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def item(status=201, attempts=1, body=None):
    body = {"id": 99, "content_type": "Issue"} if body is None else body
    return Response("u", status, json.dumps(body).encode(), attempts)


def registrar(limits, *outcomes, budget=None):
    fake = FakeFetch(*outcomes)
    budget = budget or WriteBudget(limits.github_write_calls)
    return ProjectsRegistrar(CONFIG, limits, budget, fetch_url=fake), fake, budget


def error(status, **headers):
    return FetchError(f"HTTP {status}", 1, status, headers)


# --- worker.env の読み込み（トークンと指定を組で読む） ---


def test_config_reads_token_and_target_together():
    config = projects_config(ENV)
    assert config.target == ProjectsTarget("octocat", 3)
    assert config.token == TOKEN
    assert TOKEN not in repr(config)


@pytest.mark.parametrize("name", [TOKEN_ENV, OWNER_ENV, NUMBER_ENV])
def test_config_needs_every_variable(name):
    env = {k: v for k, v in ENV.items() if k != name}
    with pytest.raises(ProjectsError) as raised:
        projects_config(env)
    assert (raised.value.category, name in str(raised.value)) == ("config", True)
    with pytest.raises(ProjectsError):
        projects_config({**ENV, name: "  "})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (TOKEN_ENV, "bad token\n"),
        (OWNER_ENV, "a/b"),
        (OWNER_ENV, "-x"),
        (OWNER_ENV, "../x"),
        (NUMBER_ENV, "0"),
        (NUMBER_ENV, "-1"),
        (NUMBER_ENV, "3a"),
        (NUMBER_ENV, "１２"),
    ],
)
def test_config_rejects_unusable_values(name, value):
    with pytest.raises(ProjectsError) as raised:
        projects_config({**ENV, name: value})
    assert raised.value.category == "config"
    assert value.strip() not in str(raised.value)


# --- 登録の要求 ---


def test_adds_only_the_issue_with_the_documented_request(limits):
    r, fake, budget = registrar(limits, item())
    r.add_issue(ISSUE)

    [call] = fake.calls  # 登録以外の要求（本文・状態の変更など）は出さない
    assert call["url"] == "https://api.github.com/users/octocat/projectsV2/3/items"
    assert json.loads(call["data"]) == {
        "type": "Issue",
        "owner": "owner",
        "repo": "repo",
        "number": 7,
    }
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert budget.used == 1
    assert TOKEN not in repr(r)


def test_existing_item_response_is_accepted(limits):
    r, _, _ = registrar(limits, item(status=200))
    r.add_issue(ISSUE)


@pytest.mark.parametrize(
    "response",
    [
        item(status=204, body={"id": 1}),
        item(body={"no": "id"}),
        item(body={"id": True}),
        item(body=[1]),
        Response("u", 201, b"not json", 1),
    ],
)
def test_unreadable_response_is_invalid(limits, response):
    r, _, _ = registrar(limits, response)
    with pytest.raises(ProjectsError) as raised:
        r.add_issue(ISSUE)
    assert raised.value.category == "invalid_response"


@pytest.mark.parametrize("ref", [IssueRef("bad", 1), IssueRef("owner/repo", 0)])
def test_rejects_unusable_issue_ref(limits, ref):
    r, fake, _ = registrar(limits)
    with pytest.raises(ValueError):
        r.add_issue(ref)
    assert fake.calls == []


@pytest.mark.parametrize(
    ("failure", "category"),
    [
        (error(401), "auth"),
        (error(403), "forbidden"),
        (error(404), "not_found"),
        (error(410), "http_4xx"),
        (error(422), "http_4xx"),
        (error(503), "http_5xx"),
        (error(429), "rate_limited"),
        (error(301, location="https://api.github.com/x"), "redirect"),
        (FetchError("t", 1, None, category="timeout"), "timeout"),
        (FetchError("reset", 1, None), "network"),
    ],
)
def test_failures_are_classified_without_the_token(limits, failure, category):
    r, _, budget = registrar(limits, failure)
    with pytest.raises(ProjectsError) as raised:
        r.add_issue(ISSUE)
    assert raised.value.category == category
    assert raised.value.status == failure.status
    assert TOKEN not in str(raised.value) and "octocat" not in str(raised.value)
    assert budget.used == 1


# --- 予算（Issue と Projects を合わせて 20 回。再試行も数える） ---


def test_budget_is_shared_with_the_issue_writer(limits):
    budget = WriteBudget(2)
    created = {"number": 7, "html_url": "https://github.com/owner/repo/issues/7", "labels": []}
    issues = FakeFetch(Response("u", 201, json.dumps(created).encode(), 1))
    writer = IssueWriter(TOKEN, limits, fetch_url=issues, budget=budget)
    writer.create_issue("owner/repo", "t", "b", ())
    r, fake, _ = registrar(limits, item(), budget=budget)
    r.add_issue(ISSUE)

    assert (budget.used, writer.calls) == (2, 2)
    with pytest.raises(ProjectsError) as raised:  # 上限に達したら要求を出さない
        r.add_issue(ISSUE)
    assert raised.value.category == "budget" and len(fake.calls) == 1


def test_default_budget_is_the_configured_limit():
    assert load_config(CONFIG_PATH).limits.github_write_calls == 20


def test_attempts_of_a_failed_request_are_counted(limits):
    r, _, budget = registrar(limits, FetchError("再試行を使い切った", 3, 503))
    with pytest.raises(ProjectsError):
        r.add_issue(ISSUE)
    assert budget.used == 3


@pytest.mark.parametrize(("left", "waits"), [(4, (5, 20)), (2, (5,)), (1, ())])
def test_retries_fit_in_the_remaining_budget(limits, left, waits):
    budget = WriteBudget(left)
    r, fake, _ = registrar(limits, item(), budget=budget)
    r.add_issue(ISSUE)
    assert fake.calls[0]["limits"].http_retry_waits_s == waits


# --- タイムアウトと再試行（本物の fetch に偽の通信をつなぐ） ---


def _http_error(code):
    return urllib.error.HTTPError("https://api.github.com/x", code, "err", Message(), io.BytesIO())


def _scripted(*outcomes):
    requests = []

    def open_url(request):
        requests.append(request)
        outcome = outcomes[len(requests) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        body = io.BytesIO(json.dumps({"id": 5}).encode())
        body.status = 201
        body.headers = Message()
        body.geturl = lambda: request.full_url
        return body

    return open_url, requests


def real_registrar(limits, *outcomes, deadline=None, clock=None):
    open_url, requests = _scripted(*outcomes)
    waits = []
    fetch_url = functools.partial(fetch, open_url=open_url, sleep=waits.append)
    budget = WriteBudget(limits.github_write_calls)
    kwargs = {} if clock is None else {"clock": clock}
    r = ProjectsRegistrar(CONFIG, limits, budget, fetch_url=fetch_url, **kwargs)
    r.deadline = deadline
    return r, requests, waits, budget


def test_transient_failures_are_retried_with_the_configured_waits(limits):
    r, requests, waits, budget = real_registrar(limits, _http_error(503), _http_error(500), "ok")
    r.add_issue(ISSUE)
    assert (len(requests), waits, budget.used) == (3, [5, 20], 3)


def test_retries_stop_when_exhausted(limits):
    r, requests, _, budget = real_registrar(
        limits, _http_error(503), _http_error(503), _http_error(503)
    )
    with pytest.raises(ProjectsError) as raised:
        r.add_issue(ISSUE)
    assert (raised.value.category, len(requests), budget.used) == ("http_5xx", 3, 3)


def test_client_errors_are_not_retried(limits):
    r, requests, waits, budget = real_registrar(limits, _http_error(422))
    with pytest.raises(ProjectsError):
        r.add_issue(ISSUE)
    assert (len(requests), waits, budget.used) == (1, [], 1)


def test_timeouts_are_retried(limits):
    r, requests, waits, _ = real_registrar(limits, TimeoutError("t"), "ok")
    r.add_issue(ISSUE)
    assert (len(requests), waits) == (2, [5])


def test_past_deadline_sends_nothing(limits):
    r, requests, _, budget = real_registrar(limits, "ok", deadline=10.0, clock=lambda: 11.0)
    with pytest.raises(ProjectsError) as raised:
        r.add_issue(ISSUE)
    assert (raised.value.category, requests, budget.used) == ("timeout", [], 0)
