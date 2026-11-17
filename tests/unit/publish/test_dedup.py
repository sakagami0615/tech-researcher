from datetime import UTC, date, datetime

import pytest

from tech_researcher.db.issues import find_report_issue, pending_projects, record_issue
from tech_researcher.db.runs import start_run
from tech_researcher.db.schema import open_db
from tech_researcher.periods import daily_target
from tech_researcher.publish.dedup import Create, Existing, check_duplicate
from tech_researcher.publish.github_issues import (
    IssueLookup,
    IssueReadError,
    IssueRef,
    ListedIssue,
    Marker,
)

DAY = date(2026, 10, 23)
RUN = "20261024-060000-abcd"
RETRY = "20261024-080000-0123"
AT = datetime(2026, 10, 23, 22, 0, tzinfo=UTC)
REPO = "o/r"


class FakeReader:
    def __init__(self, lookups=None, listed=(), list_error=None):
        self.lookups = lookups or {}
        self.listed = list(listed)
        self.list_error = list_error
        self.gets = []
        self.lists = 0

    def get_issue(self, repo, number):
        self.gets.append((repo, number))
        out = self.lookups[number]
        if isinstance(out, Exception):
            raise out
        return out

    def list_report_issues(self, repo, cadence):
        self.lists += 1
        if self.list_error:
            raise self.list_error
        yield from self.listed


def listed(number, run=RUN, target=DAY, cadence="daily"):
    return ListedIssue(number, Marker(cadence, target, run))


@pytest.fixture
def conn(tmp_path):
    conn = open_db(tmp_path / "tech-researcher.sqlite3")
    for run in (RUN, RETRY):
        start_run(conn, run, daily_target(date(2026, 10, 24), 1), AT)
    yield conn
    conn.close()


def check(conn, reader, run=RETRY, **kw):
    return check_duplicate(conn, reader, REPO, run, "daily", DAY, AT, **kw)


def seed(conn, number=11, repo=REPO, run=RUN):
    record_issue(conn, run, "report", "daily", DAY, "created", AT, repo=repo, number=number)


def test_db_record_exists_issue_found_does_not_create_or_list(conn):
    seed(conn)
    reader = FakeReader({11: IssueLookup(IssueRef(REPO, 11))})
    assert check(conn, reader) == Existing(IssueRef(REPO, 11), "db")
    assert (reader.gets, reader.lists) == ([(REPO, 11)], 0)
    assert find_report_issue(conn, "daily", DAY).number == 11  # DB は変えない


def test_existing_issue_with_pending_projects_stays_pending_for_caller(conn):
    seed(conn)
    reader = FakeReader({11: IssueLookup(IssueRef(REPO, 11))})
    check(conn, reader)
    assert [r.number for r in pending_projects(conn)] == [11]


def test_301_updates_db_to_transfer_target_keeping_run(conn):
    seed(conn)
    reader = FakeReader({11: IssueLookup(IssueRef("new/repo", 7), transferred=True)})
    assert check(conn, reader) == Existing(IssueRef("new/repo", 7), "transferred")
    row = find_report_issue(conn, "daily", DAY)
    assert (row.run_id, row.repo, row.number) == (RUN, "new/repo", 7)
    assert reader.lists == 0


@pytest.mark.parametrize("status", [404, 410])
def test_gone_falls_through_to_labels_and_recreates_when_not_found(conn, status):
    seed(conn)
    reader = FakeReader({11: IssueLookup(None, status=status)})
    assert check(conn, reader) == Create("recreate")
    assert reader.lists == 1
    assert find_report_issue(conn, "daily", DAY).number == 11  # 作るのは呼び出し側


def test_gone_then_labels_finds_issue_and_replaces_record(conn):
    seed(conn)
    reader = FakeReader({11: IssueLookup(None, status=410)}, [listed(30, run=RUN)])
    assert check(conn, reader) == Existing(IssueRef(REPO, 30), "labels")
    row = find_report_issue(conn, "daily", DAY)
    assert (row.run_id, row.number, row.projects_state) == (RUN, 30, "pending")


def test_no_record_labels_finds_issue_supplements_db_with_identifier_run(conn):
    reader = FakeReader(listed=[listed(5, target=date(2026, 10, 22)), listed(30, run=RUN)])
    assert check(conn, reader) == Existing(IssueRef(REPO, 30), "labels")
    row = find_report_issue(conn, "daily", DAY)
    assert (row.run_id, row.number, row.projects_state) == (RUN, 30, "pending")
    assert reader.gets == []


def test_identifier_run_unknown_to_db_uses_current_run(conn):
    reader = FakeReader(listed=[listed(30, run="20250101-000000-ffff")])
    check(conn, reader)
    assert find_report_issue(conn, "daily", DAY).run_id == RETRY


def test_labels_ignore_other_cadence_and_unmarked_issues(conn):
    reader = FakeReader(
        listed=[
            ListedIssue(1, None),
            listed(2, cadence="weekly"),
            listed(3, target=date(2026, 10, 22)),
        ]
    )
    assert check(conn, reader) == Create(None)
    assert find_report_issue(conn, "daily", DAY) is None


def test_no_record_and_nothing_found_creates(conn):
    assert check(conn, FakeReader()) == Create(None)


def test_transferred_record_is_read_from_its_own_repo_on_next_check(conn):
    seed(conn)
    first = FakeReader({11: IssueLookup(IssueRef("new/repo", 7), transferred=True)})
    check(conn, first)
    second = FakeReader({7: IssueLookup(IssueRef("new/repo", 7))})
    assert check(conn, second) == Existing(IssueRef("new/repo", 7), "db")
    assert second.gets == [("new/repo", 7)]
    assert second.lists == 0


def test_repo_argument_is_lowercased(conn):
    reader = FakeReader(listed=[listed(30)])
    assert check_duplicate(conn, reader, "O/R", RETRY, "daily", DAY, AT).ref.repo == "o/r"


def test_weekly_uses_its_own_cadence(conn):
    monday = date(2026, 10, 19)
    reader = FakeReader(listed=[listed(3, cadence="weekly", target=monday), listed(4)])
    got = check_duplicate(conn, reader, REPO, RETRY, "weekly", monday, AT, dry_run=True)
    assert got == Existing(IssueRef(REPO, 3), "labels")


def test_first_matching_issue_is_the_oldest(conn):
    reader = FakeReader(listed=[listed(30), listed(40)])
    assert check(conn, reader, dry_run=True).ref.number == 30


@pytest.mark.parametrize("category", ["auth", "rate_limited", "http_5xx", "redirect"])
def test_other_failures_in_step1_raise_without_creating(conn, category):
    seed(conn)
    reader = FakeReader({11: IssueReadError("x", category)})
    with pytest.raises(IssueReadError):
        check(conn, reader)
    assert reader.lists == 0


def test_failure_listing_raises(conn):
    with pytest.raises(IssueReadError):
        check(conn, FakeReader(list_error=IssueReadError("x", "http_5xx")))


def test_dry_run_reads_but_does_not_write_db(conn):
    reader = FakeReader(listed=[listed(30)])
    assert check(conn, reader, dry_run=True) == Existing(IssueRef(REPO, 30), "labels")
    assert find_report_issue(conn, "daily", DAY) is None
    seed(conn)
    reader = FakeReader({11: IssueLookup(IssueRef("new/repo", 7), transferred=True)})
    assert check(conn, reader, dry_run=True).via == "transferred"
    assert find_report_issue(conn, "daily", DAY).number == 11


def test_retry_after_crash_before_db_record_does_not_duplicate(conn):
    # 1回目：Issue は作れたが DB に記録する前に落ちた。2回目：ラベルから見つけて補う
    reader = FakeReader(listed=[listed(30, run=RUN)])
    assert check(conn, reader).via == "labels"
    assert check(conn, FakeReader({30: IssueLookup(IssueRef(REPO, 30))})).via == "db"
    assert conn.execute("SELECT count(*) FROM issues").fetchone()[0] == 1


def test_transfer_in_same_run_keeps_created_at(conn):
    seed(conn)
    before = find_report_issue(conn, "daily", DAY).created_at
    later = datetime(2026, 10, 24, 5, 0, tzinfo=UTC)
    reader = FakeReader({11: IssueLookup(IssueRef("new/repo", 7), transferred=True)})
    check_duplicate(conn, reader, REPO, RUN, "daily", DAY, later)
    row = find_report_issue(conn, "daily", DAY)
    assert (row.run_id, row.number, row.created_at) == (RUN, 7, before)
