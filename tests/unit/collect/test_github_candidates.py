import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from tech_researcher.collect import github_api
from tech_researcher.collect.github_api import GitHubClient, GitHubError
from tech_researcher.collect.github_candidates import (
    Candidate,
    collect_candidates,
    mentioned_repos,
    parse_search,
    repo_from_url,
    search_query,
)
from tech_researcher.collect.http import FetchError, Response
from tech_researcher.collect.records import Event, Record
from tech_researcher.config import GitHubRepos
from tech_researcher.db.schema import open_db

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH = json.loads((FIXTURES / "github_search.json").read_text(encoding="utf-8"))
T = date(2026, 11, 3)
NOW = datetime(2026, 11, 3, 21, 0, tzinfo=UTC)
SETTINGS = GitHubRepos(
    enabled=True,
    display_name="GitHub",
    search_topics=("llm", "mcp"),
    watch_repos=("anthropics/claude-code",),
    threshold_increase=100,
    report_top_n=10,
    new_repo_days=14,
    new_repo_stars=200,
    search_days=14,
    search_min_stars=200,
)


def fixture_records():
    """ほかの情報源が集めたレコード（フィクスチャ）。収集処理は動かさない。"""
    data = json.loads((FIXTURES / "github_mentions.json").read_text(encoding="utf-8"))
    return [
        Record(
            source_id=r["source_id"],
            url=r["url"],
            title="t",
            published_precision="datetime",
            fetched_at=NOW,
            events=(Event("published", NOW),),
            link_url=r.get("link_url"),
            published_at=NOW,
            links=tuple(r.get("links", ())),
        )
        for r in data
    ]


def page(names, total=None):
    return {
        "total_count": len(names) if total is None else total,
        "incomplete_results": False,
        "items": [{"full_name": n} for n in names],
    }


class FakeSearch:
    """クエリの topic ごと・ページごとの応答を返す。受けた検索条件を残す。"""

    def __init__(self, pages):
        self.pages = pages  # topic → [応答または例外, ...]
        self.queries = []

    def __call__(self, url, limits, *, headers, sleep):
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        self.queries.append(query)
        topic = query["q"].split()[0].removeprefix("topic:")
        outcome = self.pages[topic][int(query["page"]) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return Response(url, 200, json.dumps(outcome).encode(), 1)


def client(limits, fake, token="t0ken"):
    return GitHubClient(token, limits, fetch_url=fake, clock=lambda: 0.0, sleep=lambda s: None)


def test_search_query():
    assert search_query("llm", date(2026, 10, 20), 200) == (
        "topic:llm created:>=2026-10-20 stars:>=200"
    )


def test_parse_recorded_search_response():
    assert parse_search(SEARCH) == [
        "ccch1mneyyy/dsh-go",
        "wangzan101/mika",
        "modelflare-ai/anymodelbot",
    ]


@pytest.mark.parametrize(
    "data",
    [[], {"items": []}, {"total_count": 1}, {"total_count": 1, "items": [{"full_name": "x"}]}],
)
def test_parse_search_rejects_unexpected_shape(data):
    with pytest.raises(GitHubError) as e:
        parse_search(data)
    assert e.value.category == github_api.INVALID_RESPONSE


@pytest.mark.parametrize(
    "url, repo",
    [
        ("https://github.com/Owner/Repo", "owner/repo"),
        ("http://www.github.com/owner/repo.git", "owner/repo"),
        ("https://github.com/owner/repo/pull/3?utm_source=x", "owner/repo"),
        ("https://github.com/owner/my.repo_1-x/tree/main", "owner/my.repo_1-x"),
        ("https://github.com/owner", None),
        ("https://github.com/features/actions", None),
        ("https://github.com/topics/llm", None),
        ("https://gist.github.com/owner/abc", None),
        ("https://api.github.com/repos/owner/repo", None),
        ("https://user@github.com/owner/repo", None),
        ("ftp://github.com/owner/repo", None),
        ("https://github.com/-bad/repo", None),
        ("https://github.com/owner/..", None),
        ("", None),
    ],
)
def test_repo_from_url(url, repo):
    assert repo_from_url(url) == repo


def test_mentions_from_fixture_records():
    assert mentioned_repos(fixture_records()) == {
        "example-org/fast-tool": (
            "https://github.blog/news/2026-11-02-example",
            "https://zenn.dev/u/articles/abc",
        ),
        # リポジトリへ直接リンクした投稿は、はてなのエントリーのページを言及元にし、
        # レコード自身もリポジトリの URL（HN）なら言及元に入れない
        "someone/agent-kit": ("https://b.hatena.ne.jp/entry/s/github.com/someone/agent-kit",),
        "infra-team/deployer": ("https://github.com/infra-team/deployer/issues/12",),
    }


def test_collects_all_routes(limits, tmp_path):
    conn = open_db(tmp_path / "db.sqlite3")
    conn.execute(
        "INSERT INTO star_snapshots VALUES ('old/watched', '2026-10-20', '2026-10-19T21:00:00Z',"
        " 5, '2025-01-01T00:00:00Z')"
    )
    fake = FakeSearch(
        {
            "llm": [page(["Someone/Agent-Kit", "new/one"])],
            "mcp": [page(["new/one", "new/two"])],
        }
    )
    result = collect_candidates(SETTINGS, T, fixture_records(), client(limits, fake), conn)
    conn.close()
    assert result.error is None and result.search_attempts == 2
    assert result.candidates == (
        Candidate("anthropics/claude-code", ("watch",)),
        Candidate(
            "example-org/fast-tool",
            ("mention",),
            ("https://github.blog/news/2026-11-02-example", "https://zenn.dev/u/articles/abc"),
        ),
        Candidate(
            "infra-team/deployer",
            ("mention",),
            ("https://github.com/infra-team/deployer/issues/12",),
        ),
        Candidate("new/one", ("search",)),
        Candidate("new/two", ("search",)),
        Candidate("old/watched", ("watch",)),
        Candidate(
            "someone/agent-kit",
            ("search", "mention"),
            ("https://b.hatena.ne.jp/entry/s/github.com/someone/agent-kit",),
        ),
    )
    assert result.candidates[0].url == "https://github.com/anthropics/claude-code"
    assert fake.queries[0] == {
        "q": "topic:llm created:>=2026-10-20 stars:>=200",
        "sort": "stars",
        "order": "desc",
        "per_page": "100",
        "page": "1",
    }


def test_search_pages_until_results_run_out(limits):
    full = [f"o/r{i}" for i in range(100)]
    fake = FakeSearch(
        {
            "llm": [page(full, total=150), page(["o/last"], total=150)],
            "mcp": [page(full[:100], total=100)],  # ちょうど100件なら次のページは取らない
        }
    )
    result = collect_candidates(SETTINGS, T, [], client(limits, fake), None)
    assert [q["page"] for q in fake.queries] == ["1", "2", "1"]
    assert len(result.candidates) == 102  # 100 件と o/last と監視リストの1件


def test_search_stops_at_api_limit_of_1000(limits):
    full = [f"o/r{i}" for i in range(100)]
    fake = FakeSearch({"llm": [page(full, total=5000)] * 10})
    settings = replace(SETTINGS, search_topics=("llm",))
    collect_candidates(settings, T, [], client(limits, fake), None)
    assert len(fake.queries) == 10


def test_search_failure_keeps_other_routes_and_found_repos(limits):
    error = FetchError("HTTP 403", 1, 403, {"x-ratelimit-remaining": "0"})
    fake = FakeSearch({"llm": [page(["new/one"])], "mcp": [error]})
    result = collect_candidates(SETTINGS, T, fixture_records(), client(limits, fake), None)
    assert result.error_category == github_api.RATE_LIMITED
    assert "t0ken" not in result.error
    assert result.search_attempts == 2
    repos = {c.repo: c.routes for c in result.candidates}
    assert repos["new/one"] == ("search",)
    assert repos["anthropics/claude-code"] == ("watch",)
    assert "example-org/fast-tool" in repos


def test_search_budget_failure(limits):
    fake = FakeSearch({"llm": [page(["new/one"])]})
    api = client(replace(limits, github_search_calls=1), fake)
    result = collect_candidates(SETTINGS, T, [], api, None)
    assert result.error_category == github_api.BUDGET
    assert result.search_attempts == 1
    assert [c.repo for c in result.candidates] == ["anthropics/claude-code", "new/one"]


@pytest.mark.parametrize("token", [None, "missing-client"])
def test_without_token_search_is_skipped(limits, token):
    fake = FakeSearch({})
    api = None if token == "missing-client" else client(limits, fake, token=None)
    result = collect_candidates(SETTINGS, T, fixture_records(), api, None)
    assert result.error_category == github_api.AUTH and result.search_attempts == 0
    assert fake.queries == []
    assert "anthropics/claude-code" in {c.repo for c in result.candidates}


def test_empty_topics_mean_no_search(limits):
    fake = FakeSearch({})
    settings = replace(SETTINGS, search_topics=())
    result = collect_candidates(settings, T, [], None, None)
    assert result.error is None and fake.queries == []
    assert [c.repo for c in result.candidates] == ["anthropics/claude-code"]


def test_disabled_source_collects_nothing(limits):
    settings = replace(SETTINGS, enabled=False)
    result = collect_candidates(settings, T, fixture_records(), None, None)
    assert result.candidates == () and result.error is None


def test_failed_topic_is_skipped_and_next_topic_is_searched(limits):
    error = FetchError("再試行を使い切った：HTTP 502", 3, 502)
    for first in ({"message": "x"}, error):
        fake = FakeSearch({"llm": [first], "mcp": [page(["new/two"])]})
        result = collect_candidates(SETTINGS, T, [], client(limits, fake), None)
        assert result.error_category in (github_api.INVALID_RESPONSE, github_api.HTTP_5XX)
        assert "new/two" in {c.repo for c in result.candidates}


@pytest.mark.parametrize(
    "error",
    [
        FetchError("HTTP 403", 1, 403, {"x-ratelimit-remaining": "0"}),
        FetchError("HTTP 401", 1, 401),
    ],
)
def test_rate_limit_or_auth_failure_stops_remaining_topics(limits, error):
    fake = FakeSearch({"llm": [error], "mcp": [page(["new/two"])]})
    result = collect_candidates(SETTINGS, T, [], client(limits, fake), None)
    assert len(fake.queries) == 1
    assert result.error_category in (github_api.RATE_LIMITED, github_api.AUTH)
