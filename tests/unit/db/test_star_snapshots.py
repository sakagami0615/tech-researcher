import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from tech_researcher.db import star_snapshots
from tech_researcher.db.readonly import open_readonly
from tech_researcher.db.schema import open_db, transaction
from tech_researcher.db.star_snapshots import (
    StarSnapshot,
    load_star_snapshot,
    recent_repos,
    record_star_snapshots,
)

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


AT = datetime(2026, 11, 3, 21, 0, 5, tzinfo=UTC)  # 11-04 6:00 JST の実行（対象日 11-03）
CREATED = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def snap(repo, stars, at=AT, routes=("search",)):
    return StarSnapshot(repo, stars, CREATED, at, routes)


def test_snapshots_are_recorded_and_loaded(conn):
    assert record_star_snapshots(conn, T, [snap("a/b", 10, routes=("search", "watch"))]) == 1
    row = conn.execute("SELECT * FROM star_snapshots").fetchone()
    assert tuple(row) == (
        "a/b",
        "2026-11-03",
        "2026-11-03T21:00:05Z",
        10,
        "2026-10-01T12:00:00Z",
        '["search", "watch"]',
    )
    assert load_star_snapshot(conn, T) == {"a/b": snap("a/b", 10, routes=("search", "watch"))}
    assert load_star_snapshot(conn, T)


def test_remeasuring_the_same_target_keeps_the_first_measurement(conn):
    record_star_snapshots(conn, T, [snap("a/b", 10)])
    later = AT + timedelta(hours=3)
    assert record_star_snapshots(conn, T, [snap("a/b", 99, later), snap("c/d", 5, later)]) == 1
    loaded = load_star_snapshot(conn, T)
    assert (loaded["a/b"].stars_total, loaded["a/b"].measured_at) == (10, AT)
    assert loaded["c/d"].stars_total == 5


def test_same_repo_and_measured_at_on_another_target_is_ignored(conn):
    """（リポジトリ、測定時刻）でも一意にする（D-13）。"""
    record_star_snapshots(conn, T, [snap("a/b", 10)])
    assert record_star_snapshots(conn, T + timedelta(days=1), [snap("a/b", 11)]) == 0
    assert load_star_snapshot(conn, T + timedelta(days=1)) == {}
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO star_snapshots VALUES ('a/b', '2026-11-04', '2026-11-03T21:00:05Z',"
            " 1, '2026-10-01T12:00:00Z', '[]')"
        )


def test_previous_day_without_records(conn):
    """前日の記録がない日（運用の初日など）は、前日の測定が空になる（3.5 の丸ごとない日）。"""
    record_star_snapshots(conn, T, [snap("a/b", 10)])
    yesterday = T - timedelta(days=1)
    assert load_star_snapshot(conn, yesterday) == {}
    assert not load_star_snapshot(conn, yesterday)


def test_previous_day_without_a_repo(conn):
    """前日に記録があっても、初めて見るリポジトリの前日の行はない。"""
    yesterday = T - timedelta(days=1)
    record_star_snapshots(conn, yesterday, [snap("a/b", 8, AT - timedelta(days=1))])
    record_star_snapshots(conn, T, [snap("a/b", 10), snap("new/repo", 3)])
    previous = load_star_snapshot(conn, yesterday)
    assert load_star_snapshot(conn, yesterday)
    assert "new/repo" not in previous
    assert load_star_snapshot(conn, T)["a/b"].stars_total - previous["a/b"].stars_total == 2


def test_broken_rows_are_not_loaded(conn):
    insert(conn, "../x", T)
    insert(conn, "a/b", T, routes=("trending",))
    insert(conn, "c/d", T, n=1)
    assert list(load_star_snapshot(conn, T)) == ["c/d"]


def test_failed_write_leaves_nothing(conn):
    class Broken:
        def __iter__(self):
            yield snap("a/b", 1)
            raise RuntimeError("途中で失敗")

    with pytest.raises(RuntimeError):
        record_star_snapshots(conn, T, Broken())
    assert not load_star_snapshot(conn, T)


def test_cannot_be_called_inside_a_transaction(conn):
    with pytest.raises(sqlite3.OperationalError), transaction(conn):
        record_star_snapshots(conn, T, [snap("a/b", 1)])


def test_readonly_db_cannot_be_written(tmp_path):
    path = tmp_path / "tech-researcher.sqlite3"
    open_db(path).close()
    ro = open_readonly(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            record_star_snapshots(ro, T, [snap("a/b", 1)])
    finally:
        ro.close()


@pytest.mark.parametrize(
    "make",
    [
        lambda: snap("A/B", 1),
        lambda: snap("a/b", -1),
        lambda: snap("a/b", 1, routes=()),
        lambda: snap("a/b", 1, routes=("watch", "search")),
        lambda: snap("a/b", 1, routes=("search", "search")),
        lambda: snap("a/b", 1, routes=("trending",)),
        lambda: StarSnapshot("a/b", 1, datetime(2026, 1, 1), AT, ("search",)),
        lambda: StarSnapshot("a/b", 1, CREATED, datetime(2026, 1, 1), ("search",)),
    ],
)
def test_invalid_snapshots_are_refused(make):
    with pytest.raises(ValueError):
        record_star_snapshots(None, T, [make()])


def test_constraint_violations_other_than_uniqueness_are_not_ignored(conn, monkeypatch):
    """CHECK の違反（routes_json が配列でない）は黙って捨てずに失敗にする。"""
    monkeypatch.setattr(star_snapshots, "json", SimpleNamespace(dumps=lambda _: "{}"))
    with pytest.raises(sqlite3.IntegrityError):
        record_star_snapshots(conn, T, [snap("a/b", 1)])
    assert load_star_snapshot(conn, T) == {}
