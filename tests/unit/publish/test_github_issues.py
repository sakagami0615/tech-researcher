import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from tech_researcher.collect.github_api import MAX_JSON_DEPTH
from tech_researcher.collect.http import FetchError, Response
from tech_researcher.config import load_config
from tech_researcher.publish import github_issues
from tech_researcher.publish.github_issues import (
    IssueLookup,
    IssueReader,
    IssueReadError,
    IssueRef,
    issue_repo,
    issue_token,
    parse_marker,
)

TOKEN = "ghp_" + "y" * 36  # 形だけの偽のトークン
CONFIG_PATH = Path(__file__).parents[3] / "config" / "tech-researcher.toml"
API = "https://api.github.com"
MARK = (
    "<!-- tech-researcher kind=report cadence=daily target=2026-10-23 run=20261024-060000-a1b2 -->"
)


@pytest.fixture
def limits():
    return replace(load_config(CONFIG_PATH).limits, http_retry_waits_s=(5, 20))


class FakeFetch:
    """順に応答（JSON にする値・Response・例外）を返す。受けた要求を残す。"""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, url, limits, *, headers, follow_redirects, **kw):
        assert follow_redirects is False
        self.kw = kw
        self.calls.append((url, dict(headers)))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, Response):
            return outcome
        return Response(url, 200, json.dumps(outcome).encode(), 1)


def reader(limits, *outcomes):
    fake = FakeFetch(*outcomes)
    return IssueReader(TOKEN, limits, fetch_url=fake), fake


def http_error(status, location=None):
    headers = {"location": location} if location else {}
    return FetchError(f"HTTP {status}", 1, status, headers)


def labels(cadence="daily"):
    return [{"name": "type:report"}, {"name": f"cadence:{cadence}"}]


def item(number, body, **extra):
    return {"number": number, "body": body, "labels": labels(), **extra}


def test_marker_reads_only_the_first_line():
    marker = parse_marker(MARK + "\r\n本文")
    assert (marker.cadence, str(marker.target), marker.run_id) == (
        "daily",
        "2026-10-23",
        "20261024-060000-a1b2",
    )
    assert parse_marker("本文\n" + MARK) is None  # 途中の偽の識別子
    assert parse_marker(" " + MARK) is None
    assert parse_marker(MARK.replace("kind=report", "kind=rejected")) is None
    assert parse_marker(MARK.replace("2026-10-23", "2026-13-45")) is None
    assert parse_marker(None) is None
    assert parse_marker("") is None


def test_token_and_repo_come_from_issue_variables_not_collect_token():
    assert issue_token({"GITHUB_ISSUE_TOKEN": f" {TOKEN}\r"}) == TOKEN
    with pytest.raises(IssueReadError) as e:
        issue_token({"GITHUB_TOKEN": TOKEN})  # 収集用では代用しない
    assert TOKEN not in str(e.value)
    with pytest.raises(IssueReadError) as e:
        issue_token({"GITHUB_ISSUE_TOKEN": "a b" + TOKEN})
    assert TOKEN not in str(e.value)
    assert issue_repo({"GITHUB_ISSUE_REPO": "Owner/Repo"}) == "owner/repo"
    with pytest.raises(IssueReadError):
        issue_repo({"GITHUB_ISSUE_REPO": "owner"})


def test_reader_rejects_bad_token_without_echoing_it(limits):
    with pytest.raises(IssueReadError) as e:
        IssueReader(TOKEN + "\n", limits)
    assert TOKEN not in str(e.value)
    assert TOKEN not in repr(IssueReader(TOKEN, limits))


def test_get_issue_found_with_issue_token_and_no_redirect_following(limits):
    r, fake = reader(limits, {"number": 11})
    assert r.get_issue("o/r", 11) == IssueLookup(IssueRef("o/r", 11))
    url, headers = fake.calls[0]
    assert url == f"{API}/repos/o/r/issues/11"
    assert headers["Authorization"] == f"Bearer {TOKEN}"


def test_get_issue_follows_301_transfer_to_new_repo(limits):
    loc = f"{API}/repositories/123/issues/7"
    r, fake = reader(
        limits,
        http_error(301, loc),
        {
            "number": 7,
            "html_url": "https://github.com/New/Repo/issues/7",
            "repository_url": f"{API}/repos/new/repo",
        },
    )
    got = r.get_issue("o/r", 11)
    assert got == IssueLookup(IssueRef("new/repo", 7), transferred=True)
    assert fake.calls[1][0] == loc


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/repositories/1/issues/2",
        f"{API}/repositories/1/issues/2/../../x",
        f"{API}/repos/../../issues/2",
        f"{API}/repos/o/r/pulls/2",
        f"{API}/repositories/1/issues/2?x=1",
        None,
    ],
)
def test_get_issue_does_not_follow_301_to_unexpected_places(limits, location):
    r, fake = reader(limits, http_error(301, location))
    with pytest.raises(IssueReadError) as e:
        r.get_issue("o/r", 11)
    assert (e.value.category, e.value.status) == ("redirect", 301)
    assert len(fake.calls) == 1  # 認証のヘッダを別の先へ送らない


def test_get_issue_stops_endless_transfers(limits):
    loc = f"{API}/repositories/1/issues/2"
    r, fake = reader(limits, *[http_error(301, loc)] * 10)
    with pytest.raises(IssueReadError) as e:
        r.get_issue("o/r", 11)
    assert e.value.category == "redirect"
    assert len(fake.calls) == github_issues.MAX_TRANSFERS + 1


@pytest.mark.parametrize("status", [302, 307, 308])
def test_get_issue_other_redirects_are_failures(limits, status):
    r, _ = reader(limits, http_error(status, f"{API}/repositories/1/issues/2"))
    with pytest.raises(IssueReadError) as e:
        r.get_issue("o/r", 11)
    assert e.value.category == "redirect"


def test_get_issue_transfer_with_mismatched_html_url_fails(limits):
    r, _ = reader(
        limits,
        http_error(301, f"{API}/repositories/1/issues/7"),
        {"number": 7, "html_url": "https://github.com/n/r/issues/8"},
    )
    with pytest.raises(IssueReadError) as e:
        r.get_issue("o/r", 11)
    assert e.value.category == "invalid_response"


@pytest.mark.parametrize("status", [404, 410])
def test_get_issue_gone_is_not_an_error(limits, status):
    r, _ = reader(limits, http_error(status))
    assert r.get_issue("o/r", 11) == IssueLookup(None, status=status)


def test_get_issue_gone_after_transfer(limits):
    r, _ = reader(limits, http_error(301, f"{API}/repositories/1/issues/7"), http_error(410))
    assert r.get_issue("o/r", 11) == IssueLookup(None, status=410)


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (http_error(401), "auth"),
        (http_error(403), "forbidden"),
        (http_error(429), "rate_limited"),
        (http_error(500), "http_5xx"),
        (http_error(422), "http_4xx"),
        (FetchError("URLError: boom", 3), "network"),
    ],
)
def test_get_issue_other_failures_raise(limits, error, category):
    r, _ = reader(limits, error)
    with pytest.raises(IssueReadError) as e:
        r.get_issue("o/r", 11)
    assert e.value.category == category
    assert TOKEN not in str(e.value)


def test_get_issue_invalid_json_fails(limits):
    r, _ = reader(limits, Response("u", 200, b"<html>", 1))
    with pytest.raises(IssueReadError) as e:
        r.get_issue("o/r", 11)
    assert e.value.category == "invalid_response"


def test_list_uses_labels_state_all_and_reads_every_page(limits):
    page1 = [item(n, MARK) for n in range(1, 101)]
    page2 = [item(101, "x"), item(102, "y", pull_request={})]
    r, fake = reader(limits, page1, page2)
    got = list(r.list_report_issues("o/r", "daily"))
    assert [g.number for g in got] == list(range(1, 102))  # プルリクエストは除く
    first = urlsplit(fake.calls[0][0])
    assert first.path == "/repos/o/r/issues"
    query = {k: v[0] for k, v in parse_qs(first.query).items()}
    assert query["labels"] == "type:report,cadence:daily"
    assert query["state"] == "all"
    assert query["page"] == "1"
    assert parse_qs(urlsplit(fake.calls[1][0]).query)["page"] == ["2"]
    assert len(fake.calls) == 2


def test_list_reads_marker_from_first_line_only(limits):
    r, _ = reader(limits, [item(1, "x\n" + MARK), item(2, MARK), item(3, None)])
    got = list(r.list_report_issues("o/r", "daily"))
    assert [g.marker is not None for g in got] == [False, True, False]


def test_list_failure_and_page_cap(limits, monkeypatch):
    r, _ = reader(limits, [item(1, MARK)], http_error(500))
    with pytest.raises(IssueReadError) as e:
        list(
            IssueReader(TOKEN, limits, fetch_url=FakeFetch(http_error(404))).list_report_issues(
                "o/r", "daily"
            )
        )
    assert e.value.category == "not_found"  # 一覧の404は、番号の404と違い失敗にする
    monkeypatch.setattr(github_issues, "PER_PAGE", 1)
    monkeypatch.setattr(github_issues, "MAX_PAGES", 2)
    r, _ = reader(limits, [item(1, MARK)], [item(2, MARK)], [item(3, MARK)])
    with pytest.raises(IssueReadError) as e:
        list(r.list_report_issues("o/r", "daily"))
    assert e.value.category == "too_many"


def test_list_rejects_malformed_items(limits):
    r, _ = reader(limits, [{"body": MARK, "labels": labels()}])
    with pytest.raises(IssueReadError):
        list(r.list_report_issues("o/r", "daily"))


def test_get_issue_rejects_pull_request_or_other_number(limits):
    r, _ = reader(limits, {"number": 11, "pull_request": {}})
    with pytest.raises(IssueReadError):
        r.get_issue("o/r", 11)
    r, _ = reader(limits, {"number": 12})
    with pytest.raises(IssueReadError):
        r.get_issue("o/r", 11)


def test_list_redirect_is_a_failure(limits):
    r, _ = reader(limits, http_error(301, f"{API}/repositories/1/issues"))
    with pytest.raises(IssueReadError) as e:
        list(r.list_report_issues("o/r", "daily"))
    assert e.value.category == "redirect"


def test_get_issue_follows_relative_location(limits):
    r, fake = reader(
        limits,
        http_error(301, "/repositories/123/issues/7"),
        {
            "number": 7,
            "html_url": "https://github.com/n/r/issues/7",
            "repository_url": f"{API}/repos/n/r",
        },
    )
    assert r.get_issue("o/r", 11).ref == IssueRef("n/r", 7)
    assert fake.calls[1][0] == f"{API}/repositories/123/issues/7"


@pytest.mark.parametrize(
    "bad",
    [
        {"number": 1, "body": MARK},  # labels がない
        {"number": 1, "body": MARK, "labels": "type:report"},
        {"number": 1, "body": MARK, "labels": ["type:report"]},
        {"number": 1, "body": MARK, "labels": [{"name": 3}]},
        {"number": True, "body": MARK, "labels": []},
        {"number": 0, "body": MARK, "labels": []},
    ],
)
def test_list_malformed_items_fail(limits, bad):
    r, _ = reader(limits, [bad])
    with pytest.raises(IssueReadError) as e:
        list(r.list_report_issues("o/r", "daily"))
    assert e.value.category == "invalid_response"


def test_list_ignores_issues_without_both_labels(limits):
    wrong = [
        item(1, MARK, labels=[]),
        item(2, MARK, labels=[{"name": "type:report"}]),
        item(3, MARK, labels=labels("weekly")),
        item(4, MARK, labels=[{"name": "type:rejected"}, {"name": "cadence:daily"}]),
        item(5, MARK),
    ]
    r, _ = reader(limits, wrong)
    assert [g.number for g in r.list_report_issues("o/r", "daily")] == [5]


def test_list_failure_message_has_no_token(limits):
    r, _ = reader(limits, FetchError(f"HTTP 500 {TOKEN}", 3, 500))
    with pytest.raises(IssueReadError) as e:
        list(r.list_report_issues("o/r", "daily"))
    assert e.value.category == "http_5xx"
    assert TOKEN not in str(e.value)


@pytest.mark.parametrize(
    "data",
    [
        {"number": 8, "html_url": "https://github.com/n/r/issues/8"},  # Location は 7
        {"number": 7, "html_url": "https://github.com/n/r/issues/8"},
        {"number": 7, "html_url": "https://github.com/n/../issues/7"},
        {"number": 7, "html_url": "https://github.com/-n/r/issues/7"},
        {"number": True, "html_url": "https://github.com/n/r/issues/1"},
        {"number": 7, "html_url": "https://github.com/n/r/issues/7", "pull_request": {}},
        {"number": 7},
    ],
)
def test_transfer_response_is_checked(limits, data):
    r, _ = reader(limits, http_error(301, f"{API}/repositories/1/issues/7"), data)
    with pytest.raises(IssueReadError) as e:
        r.get_issue("o/r", 11)
    assert e.value.category == "invalid_response"


def test_transfer_repo_must_match_location_and_repository_url(limits):
    good = {"number": 7, "html_url": "https://github.com/New/Repo/issues/7"}
    r, _ = reader(limits, http_error(301, f"{API}/repos/new/repo/issues/7"), good)
    assert r.get_issue("o/r", 11).ref == IssueRef("new/repo", 7)  # 大文字・小文字は区別しない
    r, _ = reader(
        limits,
        http_error(301, f"{API}/repositories/1/issues/7"),
        {**good, "repository_url": f"{API}/repos/new/repo"},
    )
    assert r.get_issue("o/r", 11).transferred
    for location, extra in [
        (f"{API}/repos/other/repo/issues/7", {}),
        (f"{API}/repositories/1/issues/7", {"repository_url": f"{API}/repos/other/repo"}),
        (f"{API}/repositories/1/issues/7", {"repository_url": 5}),
    ]:
        r, _ = reader(limits, http_error(301, location), {**good, **extra})
        with pytest.raises(IssueReadError) as e:
            r.get_issue("o/r", 11)
        assert e.value.category == "invalid_response"


def test_list_label_names_are_case_insensitive(limits):
    r, _ = reader(
        limits, [item(1, MARK, labels=[{"name": "Type:Report"}, {"name": "CADENCE:daily"}])]
    )
    assert [g.number for g in r.list_report_issues("o/r", "daily")] == [1]


def test_transfer_by_repository_id_requires_repository_url(limits):
    data = {"number": 7, "html_url": "https://github.com/n/r/issues/7"}
    r, _ = reader(limits, http_error(301, f"{API}/repositories/1/issues/7"), data)
    with pytest.raises(IssueReadError) as e:
        r.get_issue("o/r", 11)
    assert e.value.category == "invalid_response"


def test_get_issue_too_deeply_nested_json_is_an_invalid_response(limits):
    depth = MAX_JSON_DEPTH + 1
    body = (b"[" * depth) + (b"]" * depth)
    r, _ = reader(limits, Response(f"{API}/repos/o/r/issues/7", 200, body, 1))
    with pytest.raises(IssueReadError) as e:
        r.get_issue("o/r", 7)
    assert e.value.category == "invalid_response"


def test_reads_pass_the_time_limit_to_fetch(limits):
    fake = FakeFetch({"number": 3})
    r = IssueReader(TOKEN, limits, fetch_url=fake, deadline=50.0, clock=lambda: 1.0)
    r.get_issue("o/r", 3)
    assert fake.kw["deadline"] == 50.0 and fake.kw["clock"]() == 1.0


def test_reads_without_a_time_limit_pass_nothing(limits):
    fake = FakeFetch({"number": 3})
    IssueReader(TOKEN, limits, fetch_url=fake).get_issue("o/r", 3)
    assert fake.kw == {}
