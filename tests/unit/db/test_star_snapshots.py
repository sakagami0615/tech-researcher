import json
from datetime import date, timedelta

import pytest

from tech_researcher.db.schema import open_db
from tech_researcher.db.star_snapshots import recent_repos

T = date(2026, 11, 3)


@pytest.fixture
def conn(tmp_path):
    conn = open_db(tmp_path / "tech-researcher.sqlite3")
    yield conn
    conn.close()


def insert(conn, repo, day, n=0, routes=("search",)):
    conn.execute(
        "INSERT INTO star_snapshots (repo, target, measured_at, stars_total, created_at,"
        " routes_json) VALUES (?, ?, ?, 100, '2026-01-01T00:00:00Z', ?)",
        (repo, day.isoformat(), f"{day.isoformat()}T21:00:0{n}Z", json.dumps(list(routes))),
    )


def test_recent_repos_are_found_by_search_or_mention_in_the_days_before_target(conn):
    insert(conn, "a/old", T - timedelta(days=31))
    insert(conn, "a/edge", T - timedelta(days=30), routes=("mention", "watch"))
    insert(conn, "b/yesterday", T - timedelta(days=1))
    insert(conn, "b/yesterday", T - timedelta(days=2))
    insert(conn, "c/today", T)  # 同じ対象の最初の実行が書いた行は見ない
    insert(conn, "d/watch-only", T - timedelta(days=1), routes=("watch",))
    insert(conn, "e/mixed", T - timedelta(days=5), routes=("search", "watch"))
    assert recent_repos(conn, T, 30) == ("a/edge", "b/yesterday", "e/mixed")


def test_repos_measured_only_by_watch_leave_after_days(conn):
    """Search で見つかった後、監視リストの経路だけで毎日測っても、days 日で外れる。"""
    insert(conn, "a/daily", T - timedelta(days=31))
    for n in range(1, 31):
        insert(conn, "a/daily", T - timedelta(days=n), routes=("watch",))
    assert recent_repos(conn, T, 30) == ()
    assert recent_repos(conn, T - timedelta(days=1), 30) == ("a/daily",)


def test_repo_found_again_by_mention_is_watched_again(conn):
    insert(conn, "a/back", T - timedelta(days=90))
    insert(conn, "a/back", T - timedelta(days=3), routes=("mention",))
    assert recent_repos(conn, T, 30) == ("a/back",)


def test_recent_repos_skips_names_not_in_owner_repo_form(conn):
    insert(conn, "../x", T - timedelta(days=1))
    insert(conn, "a/b", T - timedelta(days=1))
    assert recent_repos(conn, T, 30) == ("a/b",)


def test_recent_repos_empty_db(conn):
    assert recent_repos(conn, T, 30) == ()


def test_star_snapshots_keys(conn):
    insert(conn, "a/b", T)
    with pytest.raises(Exception, match="UNIQUE"):
        insert(conn, "a/b", T, n=1)  # 同じ（リポジトリ、対象日）
    with pytest.raises(Exception, match="UNIQUE"):
        conn.execute(
            "INSERT INTO star_snapshots VALUES ('a/b', '2026-11-04', '2026-11-03T21:00:00Z', 1,"
            " 'x', '[]')"
        )  # 同じ（リポジトリ、測定時刻）


@pytest.mark.parametrize("routes", ["not json", '"search"', "{}"])
def test_routes_json_must_be_an_array(conn, routes):
    with pytest.raises(Exception, match="CHECK"):
        conn.execute(
            "INSERT INTO star_snapshots VALUES ('a/b', '2026-11-02', '2026-11-02T21:00:00Z', 1,"
            " 'x', ?)",
            (routes,),
        )


def test_days_must_be_positive(conn):
    with pytest.raises(ValueError):
        recent_repos(conn, T, 0)
