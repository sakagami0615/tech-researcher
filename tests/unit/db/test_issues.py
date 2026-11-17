import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from tech_researcher.db import issues
from tech_researcher.db.issues import (
    find_issue,
    find_report_issue,
    mark_projects_done,
    mark_projects_issue_missing,
    pending_projects,
    record_issue,
    record_projects_failure,
)
from tech_researcher.db.runs import start_run
from tech_researcher.db.schema import open_db
from tech_researcher.periods import daily_target

DAY = date(2026, 10, 23)
RUN = "20261024-060000-abcd"
RETRY = "20261024-080000-0123"
NEXT = "20261025-060000-ffff"
AT = datetime(2026, 10, 23, 22, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path):
    conn = open_db(tmp_path / "tech-researcher.sqlite3")
    for run in (RUN, RETRY, NEXT):
        start_run(conn, run, daily_target(date(2026, 10, 24), 1), AT)
    yield conn
    conn.close()


def created(conn, run=RUN, number=11, kind="report", replaces=None):
    record_issue(
        conn, run, kind, "daily", DAY, "created", AT, repo="o/r", number=number, replaces=replaces
    )


def count(conn):
    return conn.execute("SELECT count(*) FROM issues").fetchone()[0]


def test_created_issue_starts_pending_for_projects(conn):
    created(conn)
    row = find_report_issue(conn, "daily", DAY)
    assert (row.run_id, row.repo, row.number, row.target) == (RUN, "o/r", 11, DAY)
    assert (row.projects_state, row.projects_attempts) == ("pending", 0)
    assert find_report_issue(conn, "weekly", DAY) is None


def test_retry_with_same_number_keeps_one_row_and_projects_state(conn):
    created(conn)
    (row,) = pending_projects(conn)
    mark_projects_done(conn, row.id, RUN)
    created(conn)
    assert count(conn) == 1
    assert find_report_issue(conn, "daily", DAY).projects_state == "done"


def test_second_report_issue_for_same_target_is_refused_by_index(conn):
    created(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO issues (run_id, kind, cadence, target, state, repo, number, created_at)"
            f" VALUES ('{RETRY}', 'report', 'daily', '2026-10-23', 'created', 'o/r', 12, 'x')"
        )


def test_record_in_other_run_replaces_number_and_requeues_projects(conn):
    """転送先・作り直した番号で更新する（4.3）。行を増やさず、掲載した実行との対応も保つ。"""
    created(conn)
    (row,) = pending_projects(conn)
    mark_projects_done(conn, row.id, RUN)
    created(conn, RETRY, number=11, replaces="transfer")  # 番号が同じなら登録の状態を保つ
    assert find_report_issue(conn, "daily", DAY).projects_state == "done"
    created(conn, RETRY, number=99, replaces="transfer")
    row = find_report_issue(conn, "daily", DAY)
    assert (row.run_id, row.number, row.projects_state) == (RUN, 99, "pending")
    assert count(conn) == 1
    assert find_issue(conn, RETRY, "report") is None


def test_failed_then_created_in_same_run(conn):
    record_issue(conn, RUN, "report", "daily", DAY, "failed", AT)
    assert find_report_issue(conn, "daily", DAY) is None
    assert find_issue(conn, RUN, "report").projects_state is None
    created(conn)
    row = find_report_issue(conn, "daily", DAY)
    assert (row.number, row.projects_state) == (11, "pending")


def test_created_is_not_downgraded_by_retry(conn):
    created(conn)
    record_issue(conn, RUN, "report", "daily", DAY, "failed", AT)
    assert find_report_issue(conn, "daily", DAY).state == "created"


def test_blocked_secret_and_run_error_share_a_run(conn):
    record_issue(conn, RUN, "report", "daily", DAY, "blocked_secret", AT)
    created(conn, kind="run_error_secret", number=5)
    assert find_issue(conn, RUN, "report").state == "blocked_secret"
    assert find_issue(conn, RUN, "run_error_secret").number == 5
    # 同じ実行の再試行は、番号があれば作らない（呼ぶ側が find_issue で確かめる）
    created(conn, kind="run_error_secret", number=5)
    assert count(conn) == 2
    assert find_report_issue(conn, "daily", DAY) is None


def test_rejected_issues_are_per_run(conn):
    created(conn, RUN, number=1, kind="rejected")
    created(conn, RETRY, number=2, kind="rejected")
    assert count(conn) == 2
    assert find_report_issue(conn, "daily", DAY) is None


def test_projects_failure_then_done_is_recorded(conn):
    created(conn)
    (row,) = pending_projects(conn)
    record_projects_failure(conn, row.id, RETRY, "rate_limited")
    (row,) = pending_projects(conn)
    assert (row.projects_attempts, row.projects_last_error, row.projects_last_run_id) == (
        1,
        "rate_limited",
        RETRY,
    )
    mark_projects_done(conn, row.id, NEXT)
    assert pending_projects(conn) == []
    row = find_report_issue(conn, "daily", DAY)
    assert (row.projects_state, row.projects_attempts, row.projects_last_run_id) == (
        "done",
        2,
        NEXT,
    )
    with pytest.raises(LookupError):
        record_projects_failure(conn, row.id, NEXT, "x")  # 済みの Issue は失敗にしない


def test_missing_issue_is_recorded_without_recreating(conn):
    created(conn)
    (row,) = pending_projects(conn)
    mark_projects_issue_missing(conn, row.id, NEXT)
    assert pending_projects(conn) == []
    assert find_report_issue(conn, "daily", DAY).projects_state == "issue_missing"
    with pytest.raises(LookupError):
        mark_projects_done(conn, 9999, NEXT)


def test_pending_includes_every_kind_oldest_first(conn):
    created(conn, RUN, kind="report", number=1)
    created(conn, RETRY, kind="run_error", number=2)
    assert [r.number for r in pending_projects(conn)] == [1, 2]


@pytest.mark.parametrize(
    ("kind", "state", "extra"),
    [
        ("report", "created", {}),
        ("report", "failed", {"repo": "o/r", "number": 1}),
        ("report", "created", {"repo": "o/r"}),
        ("report", "unknown", {}),
        ("other", "failed", {}),
    ],
)
def test_invalid_issue_is_refused(conn, kind, state, extra):
    with pytest.raises(ValueError):
        record_issue(conn, RUN, kind, "daily", DAY, state, AT, **extra)


def test_dry_run_id_is_not_written(conn):
    with pytest.raises(ValueError, match="ドライラン"):
        record_issue(conn, "dry-x", "report", "daily", DAY, "failed", AT)


def test_no_github_state_is_stored(conn):
    columns = {r[1] for r in conn.execute("PRAGMA table_info(issues)")}
    assert columns == {
        "id",
        "run_id",
        "kind",
        "cadence",
        "target",
        "state",
        "repo",
        "number",
        "created_at",
        "projects_state",
        "projects_attempts",
        "projects_last_error",
        "projects_last_run_id",
    }
    assert issues.STATES == {"created", "blocked_secret", "failed"}


def test_same_run_new_number_requeues_and_keeps_one_row(conn):
    created(conn)
    (row,) = pending_projects(conn)
    mark_projects_issue_missing(conn, row.id, RUN)
    later = AT + timedelta(hours=1)
    record_issue(conn, RUN, "report", "daily", DAY, "created", later, repo="o/r", number=12)
    row = find_report_issue(conn, "daily", DAY)
    assert (row.number, row.projects_state, count(conn)) == (12, "pending", 1)


def test_rewriting_same_number_keeps_created_at(conn):
    created(conn)
    first = find_report_issue(conn, "daily", DAY).created_at
    for run, hours in ((RETRY, 1), (RUN, 2)):
        at = AT + timedelta(hours=hours)
        record_issue(
            conn,
            run,
            "report",
            "daily",
            DAY,
            "created",
            at,
            repo="o/r",
            number=11,
            replaces="transfer" if run == RETRY else None,
        )
    assert find_report_issue(conn, "daily", DAY).created_at == first


def test_created_is_not_downgraded_to_blocked_secret(conn):
    created(conn)
    record_issue(conn, RUN, "report", "daily", DAY, "blocked_secret", AT)
    assert find_report_issue(conn, "daily", DAY).state == "created"


def test_projects_marks_need_pending_and_writable_run(conn):
    created(conn)
    (row,) = pending_projects(conn)
    mark_projects_issue_missing(conn, row.id, RUN)
    with pytest.raises(LookupError):
        mark_projects_done(conn, row.id, NEXT)  # 済んだ行を書き換えない
    for call in (mark_projects_done, mark_projects_issue_missing):
        with pytest.raises(ValueError, match="ドライラン"):
            call(conn, row.id, "dry-x")
    with pytest.raises(ValueError, match="ドライラン"):
        record_projects_failure(conn, row.id, "dry-x", "e")


def test_recreate_moves_the_row_to_the_new_run_and_resets_projects(conn):
    created(conn)
    (row,) = pending_projects(conn)
    record_projects_failure(conn, row.id, RUN, "rate_limited")
    record_issue(conn, RETRY, "report", "daily", DAY, "failed", AT)  # この実行の先の失敗の記録
    created(conn, RETRY, number=50, replaces="recreate")
    row = find_report_issue(conn, "daily", DAY)
    assert (row.run_id, row.number, row.projects_state) == (RETRY, 50, "pending")
    assert (row.projects_attempts, row.projects_last_error, row.projects_last_run_id) == (
        0,
        None,
        None,
    )
    assert count(conn) == 1


def test_transfer_keeps_created_at_and_recreate_renews_it(conn):
    created(conn)
    first = find_report_issue(conn, "daily", DAY).created_at
    later = AT + timedelta(hours=3)
    record_issue(
        conn,
        RETRY,
        "report",
        "daily",
        DAY,
        "created",
        later,
        repo="o/r",
        number=40,
        replaces="transfer",
    )
    row = find_report_issue(conn, "daily", DAY)
    assert (row.number, row.created_at, row.run_id) == (40, first, RUN)
    record_issue(
        conn,
        RETRY,
        "report",
        "daily",
        DAY,
        "created",
        later,
        repo="o/r",
        number=41,
        replaces="recreate",
    )
    row = find_report_issue(conn, "daily", DAY)
    assert (row.number, row.created_at, row.run_id) == (41, "2026-10-24T01:00:00Z", RETRY)


def test_changing_the_number_in_the_same_run_clears_projects_records(conn):
    created(conn)
    (row,) = pending_projects(conn)
    record_projects_failure(conn, row.id, RUN, "rate_limited")
    created(conn, number=12)
    row = find_report_issue(conn, "daily", DAY)
    assert (row.number, row.projects_attempts, row.projects_last_error) == (12, 0, None)
    created(conn, number=12)  # 同じ番号の書き直しでは消さない
    record_projects_failure(conn, row.id, RUN, "x")
    created(conn, number=12)
    assert find_report_issue(conn, "daily", DAY).projects_attempts == 1


def test_replaces_is_checked(conn):
    with pytest.raises(ValueError):
        created(conn, replaces="other")
    with pytest.raises(ValueError):
        record_issue(conn, RUN, "report", "daily", DAY, "failed", AT, replaces="transfer")


def test_issue_must_match_the_run_target(conn):
    with pytest.raises(ValueError, match="と違う"):
        record_issue(conn, RUN, "report", "daily", date(2026, 10, 22), "failed", AT)
    with pytest.raises(ValueError, match="と違う"):
        record_issue(conn, RUN, "report", "weekly", DAY, "failed", AT)
    with pytest.raises(LookupError):
        record_issue(conn, "20261024-090000-ffff", "report", "daily", DAY, "failed", AT)
    assert count(conn) == 0
