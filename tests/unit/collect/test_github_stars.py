"""Star 数の測定（GraphQL）と star_snapshots への記録。

フィクスチャ github_graphql_stars.json は、GitHub の GraphQL の応答の形（data の別名ごとの値と、
見つからないリポジトリの NOT_FOUND の誤り）に合わせて手で作ったもの。GraphQL は認証なしでは
使えず、収集用トークンがまだないため、実際の応答は記録していない（tests/network で確かめる）。
"""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tech_researcher.collect import github_api
from tech_researcher.collect.github_api import GitHubClient
from tech_researcher.collect.github_candidates import Candidate
from tech_researcher.collect.github_stars import (
    batch_query,
    fetch_stars,
    measure_stars,
    parse_batch,
)
from tech_researcher.collect.http import FetchError, Response
from tech_researcher.db.readonly import open_readonly
from tech_researcher.db.schema import open_db
from tech_researcher.db.star_snapshots import (
    StarSnapshot,
    load_star_snapshot,
    record_star_snapshots,
)
from tech_researcher.periods import daily_target, weekly_target

FIXTURES = Path(__file__).parent / "fixtures"
STARS = json.loads((FIXTURES / "github_graphql_stars.json").read_text(encoding="utf-8"))
TOKEN = "ghp_" + "x" * 36  # 形だけの偽のトークン
DAILY = daily_target(date(2026, 11, 4), 1)  # 対象日 11-03
WEEKLY = weekly_target(date(2026, 11, 9), 7)
AT = datetime(2026, 11, 3, 21, 0, 5, tzinfo=UTC)


class Clock:
    """測定時刻。呼ぶたびに1秒進む。"""

    def __init__(self, start=AT):
        self.now = start - timedelta(seconds=1)

    def __call__(self):
        self.now += timedelta(seconds=1)
        return self.now


class FakeFetch:
    """順に応答（JSON にする値・例外）を返し、POST された問い合わせを残す。"""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def __call__(self, url, limits, *, headers, sleep, follow_redirects, data=None):
        assert url == "https://api.github.com/graphql"
        assert data is not None
        self.requests.append(json.loads(data))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return Response(url, 200, json.dumps(outcome).encode(), 1)


def node(repo, stars, created="2026-10-01T12:00:00Z"):
    return {"nameWithOwner": repo, "stargazerCount": stars, "createdAt": created}


def answer(*nodes):
    """候補の順に値（node か None）を並べた応答。"""
    return {"data": {f"r{i}": n for i, n in enumerate(nodes)}}


def cand(repo, routes=("search",), mentioned_by=()):
    return Candidate(repo, routes, mentioned_by)


def make_client(limits, fetch, token=TOKEN):
    return GitHubClient(token, limits, fetch_url=fetch, sleep=lambda _: None)


@pytest.fixture
def conn(tmp_path):
    conn = open_db(tmp_path / "tech-researcher.sqlite3")
    yield conn
    conn.close()


def test_fixture_response_is_parsed():
    found, missing, failed = parse_batch(STARS, 3)
    assert found == {
        0: ("anthropics/claude-code", 41234, datetime(2025, 2, 22, 17, 41, 21, tzinfo=UTC)),
        2: ("openai/codex", 35012, datetime(2025, 4, 13, 22, 44, 51, tzinfo=UTC)),
    }
    assert (missing, failed) == ([1], [])


def test_query_uses_variables_and_only_the_star_count():
    query, variables = batch_query(["a/b", "c-d/e.f"])
    assert variables == {"o0": "a", "n0": "b", "o1": "c-d", "n1": "e.f"}
    assert "a/b" not in query and "e.f" not in query  # 名前は文に埋め込まない
    assert "r1: repository(owner: $o1, name: $n1)" in query
    # Star の履歴（Stargazers の一覧）は使わない（D-13）
    assert "stargazers" not in query.replace("stargazerCount", "")


def test_query_refuses_names_not_in_owner_repo_form():
    with pytest.raises(ValueError):
        batch_query(['a/b") { x } evil: repository(owner: "y'])


def test_measured_snapshots_are_recorded_with_routes(limits, conn):
    fetch = FakeFetch(STARS)
    candidates = [
        cand("anthropics/claude-code", ("search", "watch")),
        cand("gone-owner/gone-repo", ("mention",), ("https://example.com/a",)),
        cand("openai/codex", ("mention",), ("https://news.ycombinator.com/item?id=1",)),
    ]
    result = measure_stars(
        candidates, make_client(limits, fetch), DAILY, conn, write=True, batch_size=100, now=Clock()
    )
    assert result.error is None
    assert [s.repo for s in result.snapshots] == ["anthropics/claude-code", "openai/codex"]
    assert result.missing == ("gone-owner/gone-repo",)
    assert result.unmeasured == ()
    assert result.calls == 1
    assert result.mentioned_by == {"openai/codex": ("https://news.ycombinator.com/item?id=1",)}
    stored = load_star_snapshot(conn, DAILY.key)
    assert stored["anthropics/claude-code"].routes == ("search", "watch")
    assert stored["openai/codex"].stars_total == 35012
    assert stored["openai/codex"].created_at == datetime(2025, 4, 13, 22, 44, 51, tzinfo=UTC)
    assert stored["openai/codex"].measured_at == AT
    assert "gone-owner/gone-repo" not in stored


def test_candidates_are_split_into_batches(limits, conn):
    repos = [f"o/r{i}" for i in range(5)]
    fetch = FakeFetch(
        answer(*(node(r, i) for i, r in enumerate(repos[:2]))),
        answer(*(node(r, i + 2) for i, r in enumerate(repos[2:4]))),
        answer(node(repos[4], 4)),
    )
    result = measure_stars(
        [cand(r) for r in repos], make_client(limits, fetch), DAILY, conn, write=True,
        batch_size=2, now=Clock(),
    )  # fmt: skip
    assert [len(r["variables"]) for r in fetch.requests] == [4, 4, 2]
    assert [s.stars_total for s in result.snapshots] == [0, 1, 2, 3, 4]
    # 問い合わせごとに測定時刻が違っても、（リポジトリ、測定時刻）の一意は守られる
    assert len({s.measured_at for s in result.snapshots}) == 3
    assert len(load_star_snapshot(conn, DAILY.key)) == 5


def test_same_target_remeasured_keeps_and_returns_the_first(limits, conn):
    first = FakeFetch(answer(node("a/b", 10)))
    measure_stars(
        [cand("a/b")], make_client(limits, first), DAILY, conn, write=True, batch_size=100,
        now=Clock(),
    )  # fmt: skip
    again = FakeFetch(answer(node("c/d", 5)))
    result = measure_stars(
        [cand("a/b", ("watch",)), cand("c/d")], make_client(limits, again), DAILY, conn,
        write=True, batch_size=100, now=Clock(AT + timedelta(hours=2)),
    )  # fmt: skip
    by_repo = {s.repo: s for s in result.snapshots}
    assert (by_repo["a/b"].stars_total, by_repo["a/b"].routes) == (10, ("search",))
    assert by_repo["a/b"].measured_at == AT
    assert by_repo["c/d"].stars_total == 5
    assert load_star_snapshot(conn, DAILY.key)["a/b"].stars_total == 10


def test_first_day_without_previous_records_is_recorded(limits, conn):
    """前日の記録がない日（運用の初日）も記録する。翌日はその記録と比べられる。"""
    yesterday = DAILY.key - timedelta(days=1)
    assert not load_star_snapshot(conn, yesterday)
    fetch = FakeFetch(answer(node("a/b", 10)))
    result = measure_stars(
        [cand("a/b")], make_client(limits, fetch), DAILY, conn, write=True, batch_size=100,
        now=Clock(),
    )  # fmt: skip
    assert [s.repo for s in result.snapshots] == ["a/b"]
    assert load_star_snapshot(conn, DAILY.key)
    assert load_star_snapshot(conn, yesterday) == {}


def test_dry_run_does_not_write_but_uses_the_recorded_values(limits, tmp_path):
    path = tmp_path / "tech-researcher.sqlite3"
    rw = open_db(path)
    created = datetime(2026, 10, 1, tzinfo=UTC)
    record_star_snapshots(rw, DAILY.key, [StarSnapshot("a/b", 10, created, AT, ("search",))])
    rw.close()
    ro = open_readonly(path)
    try:
        fetch = FakeFetch(answer(node("c/d", 5)))
        result = measure_stars(
            [cand("a/b"), cand("c/d")], make_client(limits, fetch), DAILY, ro, write=False,
            batch_size=100, now=Clock(AT + timedelta(hours=2)),
        )  # fmt: skip
        assert [(s.repo, s.stars_total) for s in result.snapshots] == [("a/b", 10), ("c/d", 5)]
        assert set(load_star_snapshot(ro, DAILY.key)) == {"a/b"}
    finally:
        ro.close()


def test_without_db_nothing_is_written(limits):
    fetch = FakeFetch(answer(node("a/b", 10)))
    result = measure_stars(
        [cand("a/b")], make_client(limits, fetch), DAILY, None, write=True, batch_size=100,
        now=Clock(),
    )  # fmt: skip
    assert [s.stars_total for s in result.snapshots] == [10]


def test_weekly_does_not_measure_or_write(limits, conn):
    fetch = FakeFetch()
    result = measure_stars(
        [cand("a/b")], make_client(limits, fetch), WEEKLY, conn, write=True, batch_size=100
    )
    assert result.snapshots == () and result.calls == 0
    assert fetch.requests == []
    assert conn.execute("SELECT COUNT(*) FROM star_snapshots").fetchone() == (0,)


def test_no_candidates_sends_nothing(limits, conn):
    fetch = FakeFetch()
    result = measure_stars([], make_client(limits, fetch), DAILY, conn, write=True, batch_size=100)
    assert (result.snapshots, result.error, fetch.requests) == ((), None, [])


@pytest.mark.parametrize("token", [None, "no-client"])
def test_without_token_nothing_is_requested(limits, conn, token):
    fetch = FakeFetch()
    client = None if token == "no-client" else make_client(limits, fetch, token=None)
    result = measure_stars(
        [cand("a/b"), cand("c/d")], client, DAILY, conn, write=True, batch_size=1
    )
    assert result.error_category == github_api.AUTH
    assert result.unmeasured == ("a/b", "c/d")
    assert fetch.requests == []
    assert not load_star_snapshot(conn, DAILY.key)


def test_failed_batch_keeps_the_others(limits, conn):
    """部分失敗：失敗した問い合わせの分だけ測れず、ほかの問い合わせの分は記録する。"""
    fetch = FakeFetch(
        answer(node("a/a", 1)),
        FetchError("再試行を使い切った：HTTP 502", 3, 502),
        answer(node("c/c", 3)),
    )
    result = measure_stars(
        [cand("a/a"), cand("b/b"), cand("c/c")], make_client(limits, fetch), DAILY, conn,
        write=True, batch_size=1, now=Clock(),
    )  # fmt: skip
    assert result.error_category == github_api.HTTP_5XX
    assert result.unmeasured == ("b/b",)
    assert result.calls == 5
    assert set(load_star_snapshot(conn, DAILY.key)) == {"a/a", "c/c"}


@pytest.mark.parametrize(
    ("outcome", "category"),
    [
        (FetchError("HTTP 401", 1, 401), github_api.AUTH),
        (FetchError("HTTP 403", 1, 403), github_api.FORBIDDEN),
        (FetchError("HTTP 403", 1, 403, {"x-ratelimit-remaining": "0"}), github_api.RATE_LIMITED),
        ({"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]},
         github_api.RATE_LIMITED),
    ],
)  # fmt: skip
def test_stopping_failures_skip_the_rest(limits, conn, outcome, category):
    fetch = FakeFetch(answer(node("a/a", 1)), outcome)
    result = measure_stars(
        [cand("a/a"), cand("b/b"), cand("c/c"), cand("d/d")], make_client(limits, fetch), DAILY,
        conn, write=True, batch_size=1, now=Clock(),
    )  # fmt: skip
    assert result.error_category == category
    assert len(fetch.requests) == 2
    assert result.unmeasured == ("b/b", "c/c", "d/d")
    assert set(load_star_snapshot(conn, DAILY.key)) == {"a/a"}


def test_budget_limits_the_number_of_queries(limits, conn):
    fetch = FakeFetch(answer(node("a/a", 1)), answer(node("b/b", 2)))
    client = make_client(replace(limits, github_graphql_calls=2), fetch)
    result = measure_stars(
        [cand("a/a"), cand("b/b"), cand("c/c"), cand("d/d")], client, DAILY, conn, write=True,
        batch_size=1, now=Clock(),
    )  # fmt: skip
    assert result.error_category == github_api.BUDGET
    assert (result.calls, len(fetch.requests)) == (2, 2)
    assert result.unmeasured == ("c/c", "d/d")
    assert len(load_star_snapshot(conn, DAILY.key)) == 2


def test_renamed_repos_are_kept_under_the_new_name_and_merged(limits, conn):
    fetch = FakeFetch(answer(node("New/Name", 7), node("new/name", 7)))
    result = measure_stars(
        [cand("new/name", ("watch",)), cand("old/name", ("mention",), ("https://x.example/1",))],
        make_client(limits, fetch), DAILY, conn, write=True, batch_size=100, now=Clock(),
    )  # fmt: skip
    assert [(s.repo, s.routes) for s in result.snapshots] == [("new/name", ("mention", "watch"))]
    assert result.mentioned_by == {"new/name": ("https://x.example/1",)}
    assert list(load_star_snapshot(conn, DAILY.key)) == ["new/name"]


@pytest.mark.parametrize(
    "bad",
    [
        {"nameWithOwner": "a/b", "stargazerCount": "10", "createdAt": "2026-10-01T00:00:00Z"},
        {"nameWithOwner": "a/b", "stargazerCount": True, "createdAt": "2026-10-01T00:00:00Z"},
        {"nameWithOwner": "a/b", "stargazerCount": -1, "createdAt": "2026-10-01T00:00:00Z"},
        {"nameWithOwner": "a/b", "stargazerCount": 1, "createdAt": "2026-10-01T00:00:00"},
        {"nameWithOwner": "a/b", "stargazerCount": 1, "createdAt": "yesterday"},
        {"nameWithOwner": "../b", "stargazerCount": 1, "createdAt": "2026-10-01T00:00:00Z"},
        {"stargazerCount": 1, "createdAt": "2026-10-01T00:00:00Z"},
        {"nameWithOwner": "\u212aube/x", "stargazerCount": 1, "createdAt": "2026-10-01T00:00:00Z"},
        {"nameWithOwner": "a/b", "stargazerCount": 1, "createdAt": "0001-01-01T00:00:00+01:00"},
        {"nameWithOwner": "a/b", "stargazerCount": 1, "createdAt": "9999-12-31T23:00:00-01:00"},
        "a/b",
    ],
)
def test_malformed_entry_is_unmeasured_without_affecting_others(bad):
    found, missing, failed = parse_batch(answer(node("x/y", 1), bad), 2)
    assert (list(found), missing, failed) == ([0], [], [1])


def test_entry_errors_other_than_not_found_are_failures():
    data = {
        "data": {"r0": None, "r1": None, "r2": node("x/y", 1)},
        "errors": [
            {"type": "FORBIDDEN", "path": ["r0"], "message": "blocked"},
            {"type": "NOT_FOUND", "path": ["r1"], "message": "gone"},
        ],
    }
    assert parse_batch(data, 4) == ({2: ("x/y", 1, datetime(2026, 10, 1, 12, tzinfo=UTC))},
                                    [1], [0, 3])  # fmt: skip


@pytest.mark.parametrize(
    ("data", "category"),
    [
        ([], github_api.INVALID_RESPONSE),
        ({"errors": "x"}, github_api.INVALID_RESPONSE),
        ({"data": None}, github_api.INVALID_RESPONSE),
        ({"errors": [{"type": "MAX_NODE_LIMIT_EXCEEDED", "message": "x"}]}, github_api.GRAPHQL),
        ({"data": None, "errors": [{"message": "Something went wrong"}]}, github_api.GRAPHQL),
        ({"data": {}, "errors": [{"type": "RATE_LIMITED", "path": ["other"]}]},
         github_api.RATE_LIMITED),
        ({"errors": [{"message": "You have exceeded a secondary rate limit."}]},
         github_api.RATE_LIMITED),
    ],
)  # fmt: skip
def test_whole_query_errors_fail_the_batch(data, category):
    with pytest.raises(github_api.GitHubError) as e:
        parse_batch(data, 1)
    assert e.value.category == category


def test_error_type_in_message_is_limited_to_a_safe_form():
    data = {"errors": [{"type": "x\nGITHUB_TOKEN=abc", "message": "secret"}]}
    with pytest.raises(github_api.GitHubError) as e:
        parse_batch(data, 1)
    assert str(e.value) == "GraphQL の誤り：UNKNOWN"


def test_fetch_stars_refuses_zero_batch(limits):
    with pytest.raises(ValueError):
        fetch_stars(make_client(limits, FakeFetch()), [cand("a/b")], batch_size=0)


def test_recorded_candidates_are_not_queried_again(limits, conn):
    record_star_snapshots(
        conn,
        DAILY.key,
        [StarSnapshot("a/b", 10, datetime(2026, 10, 1, tzinfo=UTC), AT, ("search",))],
    )
    fetch = FakeFetch(answer(node("c/d", 5)))
    result = measure_stars(
        [cand("a/b", ("mention",), ("https://x.example/1",)), cand("c/d")],
        make_client(limits, fetch), DAILY, conn, write=True, batch_size=100,
        now=Clock(AT + timedelta(hours=2)),
    )  # fmt: skip
    assert [r["variables"] for r in fetch.requests] == [{"o0": "c", "n0": "d"}]
    assert [(s.repo, s.stars_total) for s in result.snapshots] == [("a/b", 10), ("c/d", 5)]
    assert result.mentioned_by == {"a/b": ("https://x.example/1",)}


def test_rerun_that_fails_still_returns_the_recorded_values(limits, conn):
    """再実行がレート制限で止まっても、最初の実行で記録した値は返る（判定が変わらない）。"""
    first = FakeFetch(answer(node("a/a", 1)), FetchError("再試行を使い切った：HTTP 502", 3, 502))
    measure_stars(
        [cand("a/a"), cand("b/b")], make_client(limits, first), DAILY, conn, write=True,
        batch_size=1, now=Clock(),
    )  # fmt: skip
    again = FakeFetch({"errors": [{"type": "RATE_LIMITED", "message": "limit"}]})
    result = measure_stars(
        [cand("a/a"), cand("b/b")], make_client(limits, again), DAILY, conn, write=True,
        batch_size=1, now=Clock(AT + timedelta(hours=1)),
    )  # fmt: skip
    assert len(again.requests) == 1  # 記録のない b/b だけを問い合わせた
    assert [(s.repo, s.stars_total) for s in result.snapshots] == [("a/a", 1)]
    assert (result.unmeasured, result.error_category) == (("b/b",), github_api.RATE_LIMITED)


def test_renamed_repo_already_recorded_returns_the_first_measurement(limits, conn):
    record_star_snapshots(
        conn,
        DAILY.key,
        [StarSnapshot("new/name", 3, datetime(2026, 10, 1, tzinfo=UTC), AT, ("watch",))],
    )
    fetch = FakeFetch(answer(node("new/name", 9)))
    result = measure_stars(
        [cand("old/name", ("mention",))], make_client(limits, fetch), DAILY, conn, write=True,
        batch_size=100, now=Clock(AT + timedelta(hours=1)),
    )  # fmt: skip
    assert [(s.repo, s.stars_total) for s in result.snapshots] == [("new/name", 3)]
    assert load_star_snapshot(conn, DAILY.key)["new/name"].stars_total == 3


def test_all_candidates_recorded_sends_nothing_even_without_client(conn):
    record_star_snapshots(
        conn, DAILY.key, [StarSnapshot("a/b", 3, datetime(2026, 10, 1, tzinfo=UTC), AT, ("watch",))]
    )
    result = measure_stars([cand("a/b")], None, DAILY, conn, write=True, batch_size=100)
    assert ([s.stars_total for s in result.snapshots], result.error) == ([3], None)


@pytest.mark.parametrize(
    "error",
    [
        {"type": "NOT_FOUND", "path": [["r0"]]},
        {"type": "NOT_FOUND", "path": [{"a": 1}]},
        {"type": "NOT_FOUND", "path": [0]},
    ],
)
def test_error_paths_of_other_shapes_fail_the_batch(error):
    with pytest.raises(github_api.GitHubError) as e:
        parse_batch({"data": {"r0": None}, "errors": [error]}, 1)
    assert e.value.category == github_api.GRAPHQL


def test_rate_limited_error_on_an_alias_fails_the_batch():
    data = {"data": {"r0": None}, "errors": [{"type": "RATE_LIMITED", "path": ["r0"]}]}
    with pytest.raises(github_api.GitHubError) as e:
        parse_batch(data, 1)
    assert e.value.category == github_api.RATE_LIMITED


def test_star_count_beyond_graphql_int_is_unmeasured():
    found, _, failed = parse_batch(answer(node("a/b", 2**31)), 1)
    assert (found, failed) == ({}, [0])


def test_renamed_repos_in_separate_queries_are_merged(limits, conn):
    fetch = FakeFetch(answer(node("new/name", 7)), answer(node("new/name", 8)))
    result = measure_stars(
        [cand("new/name", ("watch",)), cand("old/name", ("search",))],
        make_client(limits, fetch), DAILY, conn, write=True, batch_size=1, now=Clock(),
    )  # fmt: skip
    assert [(s.repo, s.routes, s.stars_total) for s in result.snapshots] == [
        ("new/name", ("search", "watch"), 8)
    ]
    assert list(load_star_snapshot(conn, DAILY.key)) == ["new/name"]


def test_invalid_candidates_are_not_queried(limits):
    fetch = FakeFetch(answer(node("a/b", 1)))
    candidates = [cand('x") { evil }'), cand("a/b"), cand("c/d", routes=())]
    result = fetch_stars(make_client(limits, fetch), candidates, batch_size=100, now=Clock())
    assert [r["variables"] for r in fetch.requests] == [{"o0": "a", "n0": "b"}]
    assert [s.repo for s in result.snapshots] == ["a/b"]
    assert result.unmeasured == ('x") { evil }', "c/d")
    assert result.error is None  # 候補の形の誤りは呼ぶ側の不具合で、取得の失敗には数えない


def test_row_written_first_by_another_run_is_returned(limits, conn, monkeypatch):
    """読んでから書くまでに別の実行が書いた行があれば、DB の値を返す。"""
    from tech_researcher.collect import github_stars

    real = github_stars.record_star_snapshots

    def record_after_other_run(c, day, snapshots):
        created = datetime(2026, 10, 1, tzinfo=UTC)
        real(c, day, [StarSnapshot("a/b", 3, created, AT - timedelta(minutes=1), ("watch",))])
        return real(c, day, snapshots)

    monkeypatch.setattr(github_stars, "record_star_snapshots", record_after_other_run)
    fetch = FakeFetch(answer(node("a/b", 9)))
    result = measure_stars(
        [cand("a/b")], make_client(limits, fetch), DAILY, conn, write=True, batch_size=100,
        now=Clock(),
    )  # fmt: skip
    assert [s.stars_total for s in result.snapshots] == [3]


def test_measured_at_is_kept_to_the_second(limits, conn):
    """返す測定時刻は DB に残す精度（秒）にそろえ、DB の値と一致する。"""
    fetch = FakeFetch(answer(node("a/b", 1)))
    result = measure_stars(
        [cand("a/b")], make_client(limits, fetch), DAILY, conn, write=True, batch_size=100,
        now=lambda: AT.replace(microsecond=123456),
    )  # fmt: skip
    assert result.snapshots[0].measured_at == AT
    assert load_star_snapshot(conn, DAILY.key)["a/b"] == result.snapshots[0]
