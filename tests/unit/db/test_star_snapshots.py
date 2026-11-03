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


def insert(conn, repo, day, n=0):
    conn.execute(
        "INSERT INTO star_snapshots (repo, target, measured_at, stars_total, created_at)"
        " VALUES (?, ?, ?, 100, '2026-01-01T00:00:00Z')",
        (repo, day.isoformat(), f"{day.isoformat()}T21:00:0{n}Z"),
    )


def test_recent_repos_are_first_measured_in_the_days_before_target(conn):
    insert(conn, "a/old", T - timedelta(days=31))
    insert(conn, "a/edge", T - timedelta(days=30))
    insert(conn, "b/yesterday", T - timedelta(days=1))
    insert(conn, "b/yesterday", T - timedelta(days=2))
    insert(conn, "c/today", T)  # 同じ対象の最初の実行が書いた行は見ない
    assert recent_repos(conn, T, 30) == ("a/edge", "b/yesterday")


def test_repos_measured_every_day_leave_after_days(conn):
    """監視リストで毎日測っても、初めて測ってから days 日で外れる。"""
    for n in range(1, 32):
        insert(conn, "a/daily", T - timedelta(days=n))
    assert recent_repos(conn, T, 30) == ()
    assert recent_repos(conn, T - timedelta(days=1), 30) == ("a/daily",)


def test_recent_repos_empty_db(conn):
    assert recent_repos(conn, T, 30) == ()


def test_star_snapshots_keys(conn):
    insert(conn, "a/b", T)
    with pytest.raises(Exception, match="UNIQUE"):
        insert(conn, "a/b", T, n=1)  # 同じ（リポジトリ、対象日）
    with pytest.raises(Exception, match="UNIQUE"):
        conn.execute(
            "INSERT INTO star_snapshots VALUES ('a/b', '2026-11-04', '2026-11-03T21:00:00Z', 1,"
            " 'x')"
        )  # 同じ（リポジトリ、測定時刻）


def test_days_must_be_positive(conn):
    with pytest.raises(ValueError):
        recent_repos(conn, T, 0)
