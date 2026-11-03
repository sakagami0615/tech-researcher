import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from tech_researcher.db.runs import (
    SourceFetch,
    finish_run,
    record_source_fetch,
    start_run,
    utc_text,
)
from tech_researcher.db.schema import open_db
from tech_researcher.periods import JST, Target, daily_target, weekly_target

RUN_ID = "20261024-060000-abcd"
TARGET = daily_target(date(2026, 10, 24), 1)  # 2026-10-23 0:00〜10-24 0:00（JST）
STARTED = datetime(2026, 10, 23, 21, 0, tzinfo=UTC)  # JST 10-24 6:00
FINISHED = STARTED + timedelta(minutes=30)


@pytest.fixture
def conn(tmp_path):
    conn = open_db(tmp_path / "tech-researcher.sqlite3")
    start_run(conn, RUN_ID, TARGET, STARTED)
    yield conn
    conn.close()


def run_row(conn, run_id=RUN_ID):
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    conn.row_factory = None
    return dict(row)


def test_utc_text():
    assert utc_text(datetime(2026, 10, 24, 6, 0, 1, 500, tzinfo=JST)) == "2026-10-23T21:00:01Z"
    with pytest.raises(ValueError, match="タイムゾーン"):
        utc_text(datetime(2026, 10, 24))


def test_start_records_target_and_period(conn):
    assert run_row(conn) == {
        "run_id": RUN_ID,
        "cadence": "daily",
        "target": "2026-10-23",
        "period_start": "2026-10-22T15:00:00Z",
        "period_end": "2026-10-23T15:00:00Z",
        "started_at": "2026-10-23T21:00:00Z",
        "finished_at": None,
        "status": "running",
        "failed_stage": None,
        "error_category": None,
        "retry_count": None,
        "investigation_json": None,
        "model": None,
    }


def test_runs_are_added_per_run(conn):
    """実行ごとに1行足す（US-E6-05）。同じ実行 ID は2回始めない。"""
    start_run(conn, "20261025-060000-0123", TARGET, STARTED + timedelta(days=1))
    assert conn.execute("SELECT count(*) FROM runs").fetchone() == (2,)
    with pytest.raises(sqlite3.IntegrityError):
        start_run(conn, RUN_ID, TARGET, STARTED)


def test_weekly(conn):
    target = weekly_target(date(2026, 10, 26), 7)
    start_run(conn, "20261026-070000-0123", target, STARTED)
    row = run_row(conn, "20261026-070000-0123")
    assert (row["cadence"], row["target"]) == ("weekly", "2026-10-26")


def test_finish_succeeded(conn):
    finish_run(conn, RUN_ID, "succeeded", FINISHED)
    row = run_row(conn)
    assert (row["status"], row["finished_at"]) == ("succeeded", "2026-10-23T21:30:00Z")


def test_finish_failed_and_retry_overwrites(conn):
    finish_run(
        conn,
        RUN_ID,
        "failed",
        FINISHED,
        failed_stage="analyze",
        error_category="timeout",
        retry_count=1,
    )
    row = run_row(conn)
    assert (row["status"], row["failed_stage"], row["error_category"], row["retry_count"]) == (
        "failed",
        "analyze",
        "timeout",
        1,
    )
    # 同じ実行の中でやり直したら同じ行を上書きする（3.7）
    finish_run(conn, RUN_ID, "rejected", FINISHED + timedelta(minutes=1))
    row = run_row(conn)
    assert (row["status"], row["failed_stage"], row["error_category"]) == ("rejected", None, None)
    assert conn.execute("SELECT count(*) FROM runs").fetchone() == (1,)


def test_observation(conn):
    start_run(conn, "obs-20261024-060000-abcd", TARGET, STARTED)
    finish_run(conn, "obs-20261024-060000-abcd", "observed", FINISHED)
    assert run_row(conn, "obs-20261024-060000-abcd")["status"] == "observed"
    start_run(conn, "obs-20261025-060000-abcd", TARGET, STARTED)
    finish_run(conn, "obs-20261025-060000-abcd", "failed", FINISHED, failed_stage="collect")
    assert run_row(conn, "obs-20261025-060000-abcd")["status"] == "failed"


@pytest.mark.parametrize(
    ("run_id", "status", "kwargs", "message"),
    [
        (RUN_ID, "running", {}, "終わりの状態"),
        (RUN_ID, "done", {}, "終わりの状態"),
        (RUN_ID, "observed", {}, "observed"),
        ("obs-20261024-060000-abcd", "succeeded", {}, "observed"),
        (RUN_ID, "failed", {}, "failed_stage"),
        (RUN_ID, "succeeded", {"failed_stage": "post"}, "failed_stage"),
        (RUN_ID, "failed", {"failed_stage": "deploy"}, "知らない段階"),
        ("dry-20261024-060000-abcd", "succeeded", {}, "ドライラン"),
    ],
)
def test_finish_rejects(conn, run_id, status, kwargs, message):
    with pytest.raises(ValueError, match=message):
        finish_run(conn, run_id, status, FINISHED, **kwargs)
    assert run_row(conn)["status"] == "running"


def test_finish_without_start(conn):
    with pytest.raises(LookupError):
        finish_run(conn, "20261025-060000-0123", "succeeded", FINISHED)


def test_dry_run_is_not_written(conn):
    with pytest.raises(ValueError, match="ドライラン"):
        start_run(conn, "dry-20261024-060000-abcd", TARGET, STARTED)
    with pytest.raises(ValueError, match="ドライラン"):
        record_source_fetch(conn, "dry-20261024-060000-abcd", fetch())
    assert conn.execute("SELECT count(*) FROM runs").fetchone() == (1,)


def test_rejects_unknown_cadence(conn):
    target = Target("monthly", TARGET.key, TARGET.period)
    with pytest.raises(ValueError, match="cadence"):
        start_run(conn, "20261025-060000-0123", target, STARTED)


def fetch(**overrides):
    values = {
        "source_id": "rss:a",
        "status": "ok",
        "attempts": 1,
        "record_count": 10,
        "in_period_count": 3,
        "fetched_at": STARTED,
    }
    return SourceFetch(**(values | overrides))


def fetch_rows(conn):
    return conn.execute(
        "SELECT run_id, source_id, status, error_category, attempts, record_count,"
        " in_period_count, fetched_at FROM source_fetches ORDER BY source_id"
    ).fetchall()


def test_source_fetch_retry_updates_same_row(conn):
    empty = {"record_count": 0, "in_period_count": 0}
    record_source_fetch(conn, RUN_ID, fetch(status="failed", error_category="http_5xx", **empty))
    record_source_fetch(conn, RUN_ID, fetch(source_id="rss:b", status="no_results", **empty))
    record_source_fetch(conn, RUN_ID, fetch(attempts=3, fetched_at=FINISHED))
    assert fetch_rows(conn) == [
        (RUN_ID, "rss:a", "ok", None, 3, 10, 3, "2026-10-23T21:30:00Z"),
        (RUN_ID, "rss:b", "no_results", None, 1, 0, 0, "2026-10-23T21:00:00Z"),
    ]


def test_source_fetch_needs_run(conn):
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        record_source_fetch(conn, "20261025-060000-0123", fetch())


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "broken"},
        {"status": "failed"},
        {"status": "ok", "error_category": "timeout"},
        {"in_period_count": 11},
        {"in_period_count": -1},
        {"attempts": -1},
    ],
)
def test_source_fetch_validation(overrides):
    with pytest.raises(ValueError):
        fetch(**overrides)


def test_skipped_may_have_reason():
    assert fetch(status="skipped", error_category="rate_limited").status == "skipped"
    assert fetch(status="skipped").error_category is None


def test_aware_times_only(conn):
    with pytest.raises(ValueError, match="タイムゾーン"):
        start_run(conn, "20261025-060000-0123", TARGET, datetime(2026, 10, 24))
