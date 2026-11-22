import json
from dataclasses import replace
from pathlib import Path

import pytest

from tech_researcher.collect.http import FetchError, Response
from tech_researcher.config import load_config
from tech_researcher.publish.github_write import IssueWriteError, IssueWriter

TOKEN = "ghp_" + "y" * 36  # 形だけの偽のトークン
CONFIG_PATH = Path(__file__).parents[3] / "config" / "tech-researcher.toml"
REPO = "owner/repo"
LABELS = ("type:report", "cadence:daily")


@pytest.fixture
def limits():
    return replace(
        load_config(CONFIG_PATH).limits, http_retry_waits_s=(5, 20), github_write_calls=3
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


def created(number=7, repo=REPO, labels=LABELS, status=201, **extra):
    body = {
        "number": number,
        "html_url": f"https://github.com/{repo}/issues/{number}",
        "labels": [{"name": n} for n in labels],
        **extra,
    }
    return Response("u", status, json.dumps(body).encode(), 1)


def writer(limits, *outcomes):
    fake = FakeFetch(*outcomes)
    return IssueWriter(TOKEN, limits, fetch_url=fake), fake


def error(status, **headers):
    return FetchError(f"HTTP {status}", 1, status, headers)


def test_request_carries_labels_and_never_retries_by_itself(limits):
    w, fake = writer(limits, created())
    result = w.create_issue(REPO, "タイトル", "本文", LABELS)

    assert (result.ref.repo, result.ref.number, result.missing_labels) == (REPO, 7, ())
    [call] = fake.calls
    assert call["url"] == "https://api.github.com/repos/owner/repo/issues"
    assert json.loads(call["data"]) == {
        "title": "タイトル",
        "body": "本文",
        "labels": ["type:report", "cadence:daily"],
    }
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert call["headers"]["Content-Type"] == "application/json"
    # POST は黙って繰り返さない（応答が届かなくても作られていることがある）
    assert call["limits"].http_retry_waits_s == ()
    assert w.calls == 1


def test_labels_dropped_by_github_are_reported(limits):
    w, _ = writer(limits, created(labels=("type:report",)))
    assert w.create_issue(REPO, "t", "b", LABELS).missing_labels == ("cadence:daily",)


def test_label_comparison_ignores_case(limits):
    w, _ = writer(limits, created(labels=("Type:Report", "CADENCE:daily")))
    assert w.create_issue(REPO, "t", "b", LABELS).missing_labels == ()


@pytest.mark.parametrize(
    ("failure", "category", "retryable"),
    [
        (error(422), "http_4xx", False),
        (error(401), "auth", False),
        (error(404), "not_found", False),
        (error(403), "forbidden", False),
        (error(503), "http_5xx", True),
        (error(403, **{"retry-after": "30"}), "rate_limited", True),
        (error(429), "rate_limited", False),  # 待ち時間が示されない
        (error(429, **{"retry-after": "10"}), "rate_limited", True),
        (error(301, location="https://api.github.com/repositories/1/issues"), "redirect", False),
        (FetchError("timed out", 1, None), "network", True),
        (FetchError("t", 1, None, category="timeout"), "timeout", True),
    ],
)
def test_failures_are_classified(limits, failure, category, retryable):
    w, _ = writer(limits, failure)
    with pytest.raises(IssueWriteError) as raised:
        w.create_issue(REPO, "t", "b", LABELS)
    assert (raised.value.category, raised.value.retryable) == (category, retryable)
    assert TOKEN not in str(raised.value)


@pytest.mark.parametrize(
    ("failure", "unsent"),
    [
        (FetchError("t", 0, None, category="timeout"), True),  # 時間の上限のため送らなかった
        (FetchError("再試行を使い切った：gaierror", 1, None), False),
        (FetchError("URLError: ConnectionRefusedError: x", 1, None), True),
        (FetchError("t", 1, None, category="timeout"), False),  # 応答の読み取りのタイムアウト
        (FetchError("reset", 1, None), False),
        (error(503), False),
        (error(429), True),
        (error(403, **{"retry-after": "5"}), True),
        (error(422), False),
    ],
)
def test_unsent_marks_only_failures_where_no_issue_was_created(limits, failure, unsent):
    w, _ = writer(limits, failure)
    with pytest.raises(IssueWriteError) as raised:
        w.create_issue(REPO, "t", "b", LABELS)
    assert raised.value.unsent is unsent


def test_retry_after_is_exposed(limits):
    w, _ = writer(limits, error(403, **{"retry-after": "30"}))
    with pytest.raises(IssueWriteError) as raised:
        w.create_issue(REPO, "t", "b", LABELS)
    assert raised.value.retry_after == 30


def test_write_budget_stops_before_sending(limits):
    w, fake = writer(limits, error(503), error(503), error(503), created())
    for _ in range(3):
        with pytest.raises(IssueWriteError):
            w.create_issue(REPO, "t", "b", LABELS)
    with pytest.raises(IssueWriteError) as raised:
        w.create_issue(REPO, "t", "b", LABELS)
    assert raised.value.category == "budget" and not raised.value.retryable
    assert len(fake.calls) == 3  # 4回目は要求を出さない


@pytest.mark.parametrize(
    "response",
    [
        created(status=200),
        created(number=0),
        created(repo="other/repo"),
        created(pull_request={}),
        Response("u", 201, b"not json", 1),
        Response("u", 201, json.dumps({"number": 7, "html_url": "https://evil/7"}).encode(), 1),
    ],
)
def test_invalid_response_is_not_retried(limits, response):
    w, _ = writer(limits, response)
    with pytest.raises(IssueWriteError) as raised:
        w.create_issue(REPO, "t", "b", LABELS)
    assert (raised.value.category, raised.value.retryable) == ("invalid_response", False)


def test_deadline_is_passed_to_fetch(limits):
    w, fake = writer(limits, created())
    w.deadline = 123.0
    w.create_issue(REPO, "t", "b", LABELS)
    assert fake.calls[0]["kw"]["deadline"] == 123.0


@pytest.mark.parametrize(
    "args",
    [
        ("bad", "t", "b"),
        (REPO, "", "b"),
        (REPO, "x" * 257, "b"),
    ],
)
def test_bad_arguments_are_rejected_before_sending(limits, args):
    w, fake = writer(limits, created())
    with pytest.raises(ValueError):
        w.create_issue(*args, LABELS)
    assert fake.calls == [] and w.calls == 0


def test_token_is_not_in_repr_and_bad_token_is_rejected(limits):
    w, _ = writer(limits)
    assert TOKEN not in repr(w)
    with pytest.raises(IssueWriteError):
        IssueWriter("bad token\n", limits)
