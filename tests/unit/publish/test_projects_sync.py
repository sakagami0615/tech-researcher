"""Projects への登録と未登録分の登録し直し（publish/projects_sync.py）。GitHub は偽物。"""

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tech_researcher.collect.http import FetchError, Response
from tech_researcher.config import load_config
from tech_researcher.db import issues as issues_db
from tech_researcher.db.runs import start_run
from tech_researcher.db.schema import open_db
from tech_researcher.logging import RunLog
from tech_researcher.periods import daily_target, weekly_target
from tech_researcher.publish.github_issues import IssueLookup, IssueReadError, IssueRef
from tech_researcher.publish.github_projects import ProjectsError, ProjectsRegistrar
from tech_researcher.publish.github_write import WriteBudget
from tech_researcher.publish.projects_sync import ProjectsSync
from tech_researcher.validate.secret_scan import SecretScanner

CONFIG_PATH = Path(__file__).parents[3] / "config" / "tech-researcher.toml"
REPO = "tester/sandbox"
AT = datetime(2026, 10, 24, 6, 0, tzinfo=UTC)
ISSUE_TOKEN = "fake-issue-credential-9876"
PROJECTS_TOKEN = "fake-projects-credential-1234"
ENVIRON = {
    "GITHUB_ISSUE_TOKEN": ISSUE_TOKEN,
    "GITHUB_ISSUE_REPO": REPO,
    "GITHUB_PROJECTS_TOKEN": PROJECTS_TOKEN,
    "GITHUB_PROJECTS_OWNER": "tester",
    "GITHUB_PROJECTS_NUMBER": "3",
}
RUN = "20261024-060000-abcd"  # 今回の実行
OLD = "20261017-060000-0001"  # 前回までの日次
OLD_WEEKLY = "20261019-070000-0002"  # 前回までの週次
# 実行ごとの対象（レポート Issue は対象ごとに1件なので、実行ごとに変える）
TARGETS = {
    RUN: daily_target(date(2026, 10, 24), 1),
    OLD: daily_target(date(2026, 10, 18), 1),
    OLD_WEEKLY: weekly_target(date(2026, 10, 19), 7),
}


class FakeRegistrar:
    """登録の要求を覚え、番号ごとに用意した失敗を投げる。予算は本物と同じく数える。"""

    def __init__(self, budget, failures=None):
        self.budget = budget
        self.failures = failures or {}
        self.added = []
        self.deadline = None

    def add_issue(self, ref):
        if self.budget.left <= 0:
            raise ProjectsError("上限", "budget")
        self.budget.used += 1
        self.added.append(ref.number)
        if ref.number in self.failures:
            raise self.failures[ref.number]


class FakeReader:
    def __init__(self, existing, error=None):
        self.existing = existing
        self.error = error
        self.calls = []

    def get_issue(self, repo, number):
        self.calls.append((repo, number))
        if self.error:
            raise self.error
        found = number in self.existing
        return IssueLookup(IssueRef(repo, number) if found else None, status=None if found else 404)


class Harness:
    def __init__(self, tmp_path):
        self.conn = open_db(tmp_path / "db.sqlite3")
        self.tmp_path = tmp_path
        limits = load_config(CONFIG_PATH).limits
        self.limits = replace(limits, http_retry_waits_s=(5, 20), github_write_calls=20)
        self.budget = WriteBudget(20)
        self.failures = {}
        self.existing = set()
        self.reader_error = None
        self.readers = []
        self.registrars = []
        self.environ = dict(ENVIRON)
        self.deadline = None
        self.now = 0.0
        scanner = SecretScanner([ISSUE_TOKEN, PROJECTS_TOKEN])
        self.log = RunLog(tmp_path / "log.jsonl", RUN, "daily", date(2026, 10, 23), scanner)
        for run, target in TARGETS.items():
            start_run(self.conn, run, target, AT)

    def add(self, run, kind, number, state="created"):
        target = TARGETS[run]
        issues_db.record_issue(
            self.conn, run, kind, target.cadence, target.key, state, AT, repo=REPO, number=number
        )

    def rows(self):
        return self.conn.execute(
            "SELECT number, projects_state, projects_attempts, projects_last_error,"
            " projects_last_run_id FROM issues ORDER BY id"
        ).fetchall()

    def state(self, number):
        return {r[0]: r for r in self.rows()}[number]

    def make_registrar(self, config, limits, budget, clock=None):
        registrar = FakeRegistrar(budget, self.failures)
        self.registrars.append(registrar)
        return registrar

    def make_reader(self, token, limits, **kw):
        assert token == ISSUE_TOKEN  # 確かめには Issue 用トークンを使う
        reader = FakeReader(self.existing, self.reader_error)
        self.readers.append(reader)
        return reader

    def sync(self, **kw):
        options = {
            "environ": self.environ,
            "make_registrar": self.make_registrar,
            "make_reader": self.make_reader,
            "deadline": self.deadline,
            "clock": lambda: self.now,
            **kw,
        }
        sync = ProjectsSync(self.conn, RUN, self.limits, self.budget, self.log, **options)
        return sync

    def run(self, first=None, **kw):
        return self.sync(**kw).run(first)

    def log_lines(self):
        path = self.tmp_path / "log.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    harness.conn.close()


def http_error(status, **kw):
    return ProjectsError(f"HTTP {status}", kw.get("category", "http_4xx"), status)


# --- 登録する ---


def test_nothing_pending_does_nothing(h):
    assert h.run() is None
    assert h.registrars == []


def test_registers_the_new_issue_first_and_then_older_ones_of_every_kind_and_cadence(h):
    h.add(OLD, "report", 5)
    h.add(OLD_WEEKLY, "report", 6)
    h.add(OLD, "run_error", 7)
    h.add(RUN, "report", 9)

    outcome = h.run(first=IssueRef(REPO, 9))

    assert h.registrars[0].added == [9, 5, 6, 7]
    assert outcome.as_dict() == {
        "registered": 4,
        "issue_missing": 0,
        "failed": 0,
        "not_attempted": 0,
    }
    assert [(r[0], r[1], r[2], r[4]) for r in h.rows()] == [
        (5, "done", 1, RUN),
        (6, "done", 1, RUN),
        (7, "done", 1, RUN),
        (9, "done", 1, RUN),
    ]


def test_done_issues_are_not_registered_again(h):
    h.add(RUN, "report", 9)
    assert h.run().registered == 1
    assert h.run() is None  # 登録待ちがなければ何もしない
    assert h.registrars[0].added == [9]


# --- 失敗はジョブと別に記録し、pending のまま次に回す ---


def test_a_failure_stays_pending_with_the_category_and_the_rest_continue(h):
    h.add(OLD, "report", 5)
    h.add(RUN, "report", 9)
    h.failures[9] = http_error(500, category="http_5xx")

    outcome = h.run(first=IssueRef(REPO, 9))

    assert h.state(9)[1:4] == ("pending", 1, "http_5xx")
    assert h.state(5)[1] == "done"
    assert outcome.as_dict()["failed"] == 1
    assert outcome.as_dict()["error_category"] == "http_5xx"
    assert ("post", "warning", "projects_http_5xx") in [
        (r["stage"], r["event"], r.get("error_category")) for r in h.log_lines()
    ]
    # 次の実行で登録し直せる
    h.failures.clear()
    assert h.run().registered == 1
    assert h.state(9)[1] == "done"


@pytest.mark.parametrize("category", ["auth", "forbidden", "rate_limited"])
def test_auth_and_rate_limit_failures_stop_the_rest(h, category):
    h.add(OLD, "report", 5)
    h.add(RUN, "report", 9)
    h.failures[9] = http_error(401, category=category)

    outcome = h.run(first=IssueRef(REPO, 9))

    assert h.registrars[0].added == [9]
    assert outcome.as_dict()["failed"] == 1
    assert outcome.as_dict()["not_attempted"] == 1
    assert h.state(5)[1:3] == ("pending", 0)


def test_missing_projects_settings_record_one_failure_without_a_request(h):
    h.add(OLD, "report", 5)
    h.add(RUN, "report", 9)
    del h.environ["GITHUB_PROJECTS_NUMBER"]

    outcome = h.run()

    assert h.registrars == []
    assert outcome.as_dict()["error_category"] == "config"
    assert [r[1:4] for r in h.rows()] == [("pending", 1, "config"), ("pending", 0, None)]
    assert outcome.as_dict()["not_attempted"] == 1


def test_projects_failure_is_not_a_run_failure(h):
    h.add(RUN, "report", 9)
    h.failures[9] = http_error(422)
    h.run()
    assert h.conn.execute("SELECT status FROM runs WHERE run_id = ?", (RUN,)).fetchone() == (
        "running",
    )
    assert h.conn.execute("SELECT count(*) FROM issues").fetchone() == (1,)  # 実行エラー Issue なし


# --- 404・410 ---


def test_410_is_checked_with_the_reader_like_404(h):
    h.add(OLD, "report", 5)
    h.failures[5] = http_error(410)

    outcome = h.run()

    assert h.readers[0].calls == [(REPO, 5)]
    assert h.state(5)[1] == "issue_missing"
    assert outcome.as_dict()["issue_missing"] == 1
    assert outcome.as_dict()["failed"] == 0


def test_410_while_the_issue_exists_stays_pending_and_stops(h):
    h.add(OLD, "report", 5)
    h.add(RUN, "report", 9)
    h.existing = {5, 9}
    h.failures[5] = http_error(410)

    outcome = h.run()

    assert h.state(5)[1:4] == ("pending", 1, "projects_not_found")
    assert outcome.as_dict()["not_attempted"] == 1


def test_redirect_from_projects_stops_the_rest(h):
    h.add(OLD, "report", 5)
    h.add(RUN, "report", 9)
    h.failures[5] = http_error(301, category="redirect")

    outcome = h.run()

    assert h.registrars[0].added == [5]
    assert h.state(9)[1:3] == ("pending", 0)
    assert outcome.as_dict()["not_attempted"] == 1


def test_a_row_finished_by_another_run_is_not_a_failure(h):
    h.add(RUN, "report", 9)

    def make(config, limits, budget, clock=None):
        registrar = FakeRegistrar(budget)
        add = registrar.add_issue

        def add_and_finish_elsewhere(ref):
            add(ref)  # 登録している間に、ほかの実行が done にした
            h.conn.execute("UPDATE issues SET projects_state = 'done'")

        registrar.add_issue = add_and_finish_elsewhere
        return registrar

    outcome = h.run(make_registrar=make)

    assert outcome.as_dict()["failed"] == 0
    assert "error_category" not in outcome.as_dict()
    assert h.state(9)[1] == "done"


def test_404_with_a_confirmed_missing_issue_is_issue_missing(h):
    h.add(OLD, "report", 5)
    h.failures[5] = http_error(404, category="not_found")

    outcome = h.run()

    assert h.readers[0].calls == [(REPO, 5)]
    assert h.state(5)[1] == "issue_missing"
    assert outcome.as_dict()["issue_missing"] == 1


def test_404_while_the_issue_exists_is_a_config_failure_and_stays_pending(h):
    h.add(OLD, "report", 5)
    h.add(RUN, "report", 9)
    h.existing = {5, 9}
    h.failures[5] = http_error(404, category="not_found")
    h.failures[9] = http_error(404, category="not_found")

    outcome = h.run()

    assert h.state(5)[1:4] == ("pending", 1, "projects_not_found")  # 設定の誤りなので止める
    assert h.state(9)[1:3] == ("pending", 0)
    assert outcome.as_dict()["failed"] == 1
    assert outcome.as_dict()["not_attempted"] == 1
    assert outcome.as_dict()["issue_missing"] == 0


@pytest.mark.parametrize("problem", ["reader_error", "no_issue_token"])
def test_404_that_cannot_be_checked_stays_pending(h, problem):
    h.add(OLD, "report", 5)
    h.failures[5] = http_error(404, category="not_found")
    if problem == "reader_error":
        h.reader_error = IssueReadError("x", "timeout")
    else:
        del h.environ["GITHUB_ISSUE_TOKEN"]

    outcome = h.run()

    assert h.state(5)[1:4] == ("pending", 1, "projects_not_found_unchecked")
    assert outcome.as_dict()["issue_missing"] == 0


# --- 書き込みの予算と時間 ---


def test_the_budget_is_shared_and_the_rest_wait_for_the_next_run(h):
    h.add(OLD, "report", 5)
    h.add(OLD, "rejected", 6)
    h.add(OLD, "run_error", 7)
    h.budget.used = 18  # Issue の作成で使った分。残りは 2

    outcome = h.run()

    assert h.registrars[0].added == [5, 6]
    assert h.budget.left == 0
    assert outcome.as_dict() == {
        "registered": 2,
        "issue_missing": 0,
        "failed": 0,
        "not_attempted": 1,
    }
    assert h.state(7)[1:3] == ("pending", 0)  # 要求していないので試行に数えない


def test_no_request_without_budget(h):
    h.add(RUN, "report", 9)
    h.budget.used = 20
    outcome = h.run()
    assert h.registrars[0].added == []
    assert outcome.as_dict()["not_attempted"] == 1


def test_no_request_after_the_deadline(h):
    h.add(RUN, "report", 9)
    h.deadline = 10.0
    h.now = 10.0
    outcome = h.run()
    assert h.registrars[0].added == []
    assert outcome.as_dict()["not_attempted"] == 1
    assert h.registrars[0].deadline == 10.0


# --- 本物の ProjectsRegistrar と偽の HTTP ---


def test_real_registrar_counts_every_attempt_against_the_shared_budget(h):
    calls = []

    def fetch_url(url, limits, *, headers, follow_redirects, data, **kw):
        calls.append((url, json.loads(data), limits.http_retry_waits_s))
        if len(calls) == 1:
            return Response(url, 201, b'{"id": 1}', 2)  # 1回の再試行を含めて2回
        raise FetchError("HTTP 404", 1, 404)

    def make(config, limits, budget, clock=None):
        return ProjectsRegistrar(config, limits, budget, fetch_url=fetch_url, clock=clock)

    h.add(OLD, "report", 5)
    h.add(RUN, "report", 9)
    h.budget.used = 17

    outcome = h.run(first=IssueRef(REPO, 9), make_registrar=make)

    assert calls[0][0].endswith("/users/tester/projectsV2/3/items")
    assert calls[0][1] == {"type": "Issue", "owner": "tester", "repo": "sandbox", "number": 9}
    assert h.state(9)[1] == "done"
    assert h.budget.used == 17 + 2 + 1
    # 2件目は 404 で、Issue がないと確かめられたので issue_missing
    assert h.state(5)[1] == "issue_missing"
    assert outcome.as_dict()["registered"] == 1


def test_unusable_issue_record_is_a_failure_without_a_request(h):
    h.add(RUN, "report", 9)
    h.conn.execute("UPDATE issues SET repo = 'bad repo' WHERE number = 9")

    def make(config, limits, budget, clock=None):
        return ProjectsRegistrar(config, limits, budget, fetch_url=None, clock=clock)

    outcome = h.run(make_registrar=make)

    assert h.state(9)[1:4] == ("pending", 1, "invalid_issue")
    assert outcome.as_dict()["failed"] == 1


def test_issue_read_rate_limit_stops_the_rest(h):
    h.add(OLD, "report", 5)
    h.add(RUN, "report", 9)
    h.failures[5] = http_error(404, category="not_found")
    h.failures[9] = http_error(404, category="not_found")
    h.reader_error = IssueReadError("x", "rate_limited")

    outcome = h.run()

    assert h.registrars[0].added == [5]
    assert h.state(5)[1:4] == ("pending", 1, "projects_not_found_unchecked")
    assert h.state(9)[1:3] == ("pending", 0)
    assert outcome.as_dict()["not_attempted"] == 1


def moved_row(h, run, kind="report"):
    return h.conn.execute(
        "SELECT repo, number, projects_state, projects_attempts FROM issues"
        " WHERE run_id = ? AND kind = ?",
        (run, kind),
    ).fetchone()


def test_a_transferred_issue_is_updated_to_its_destination_and_registered(h):
    h.add(OLD, "report", 5)
    h.add(RUN, "report", 9)
    h.failures[5] = http_error(404, category="not_found")
    h.make_reader = lambda token, limits, **kw: TransferredReader()

    outcome = h.run(first=IssueRef(REPO, 9))

    assert moved_row(h, OLD) == ("tester/moved", 1005, "done", 1)
    assert h.registrars[0].added == [9, 5, 1005]
    assert outcome.as_dict()["registered"] == 2
    assert outcome.as_dict()["failed"] == 0


def test_many_transfers_do_not_starve_the_later_rows(h):
    h.add(OLD, "report", 5)
    h.add(OLD, "rejected", 6)
    h.add(OLD, "run_error", 7)
    h.add(OLD_WEEKLY, "report", 8)
    h.add(RUN, "report", 9)
    for number in (5, 6, 7):
        h.failures[number] = http_error(404, category="not_found")
    h.make_reader = lambda token, limits, **kw: TransferredReader()

    outcome = h.run()

    assert [r[1] for r in h.rows()] == ["done"] * 5
    assert outcome.as_dict()["registered"] == 5


def test_a_transfer_that_cannot_be_registered_is_abandoned_after_three_attempts(h):
    h.add(OLD, "report", 5)
    h.add(RUN, "report", 9)
    h.failures[5] = http_error(404, category="not_found")
    h.failures[1005] = http_error(404, category="not_found")  # 転送先でも登録できない
    h.make_reader = lambda token, limits, **kw: TransferredReader(fixed=1005)

    for expected in ("pending", "pending", "abandoned"):
        outcome = h.run()
        assert moved_row(h, OLD)[2] == expected

    assert moved_row(h, OLD)[3] == 3  # 外すときは試行回数を増やさない
    assert outcome.as_dict()["abandoned"] == 1
    assert h.run() is None  # 外れた行は、もう試さない


def test_an_unexpected_error_is_recorded_on_the_row_and_stops(h):
    h.add(OLD, "report", 5)
    h.add(RUN, "report", 9)
    h.failures[9] = RuntimeError("想定外")

    outcome = h.run(first=IssueRef(REPO, 9))

    assert h.state(9)[1:4] == ("pending", 1, "unexpected")
    assert h.state(5)[1:3] == ("pending", 0)
    assert outcome.as_dict()["not_attempted"] == 1


class TransferredReader:
    def __init__(self, fixed=None):
        self.fixed = fixed

    def get_issue(self, repo, number):
        moved = self.fixed or number + 1000
        return IssueLookup(IssueRef("tester/moved", moved), transferred=True)


class MovedIssueReader:
    """元の投稿先では転送されたと答え、転送先では Issue があると答える。"""

    def get_issue(self, repo, number):
        if repo == REPO:
            return IssueLookup(IssueRef("tester/moved", number + 1000), transferred=True)
        return IssueLookup(IssueRef(repo, number))


def test_404_at_the_destination_with_an_existing_issue_does_not_stop_the_rest(h):
    h.add(OLD, "report", 5)
    h.add(RUN, "report", 9)
    h.failures[5] = http_error(404, category="not_found")
    h.failures[1005] = http_error(404, category="not_found")
    h.make_reader = lambda token, limits, **kw: MovedIssueReader()

    outcome = h.run()

    assert moved_row(h, OLD) == ("tester/moved", 1005, "pending", 1)
    assert h.state(9)[1] == "done"  # 後ろの行は試される
    assert outcome.as_dict()["not_attempted"] == 0
    assert h.log_lines()[-1]["event"] == "end"


def test_404_for_an_issue_of_another_repository_does_not_stop_the_rest(h):
    h.add(OLD, "report", 5)
    h.add(RUN, "report", 9)
    h.conn.execute("UPDATE issues SET repo = 'tester/elsewhere' WHERE number = 5")
    h.failures[5] = http_error(404, category="not_found")
    h.existing = {5, 9}

    outcome = h.run()

    assert moved_row(h, OLD)[2:] == ("pending", 1)
    assert h.state(9)[1] == "done"
    assert outcome.as_dict()["not_attempted"] == 0


class ToConfiguredRepoReader:
    """元の場所からは、設定の投稿先に転送されたと答え、設定の投稿先では Issue があると答える。"""

    def get_issue(self, repo, number):
        if repo == REPO:
            return IssueLookup(IssueRef(repo, number))
        return IssueLookup(IssueRef(REPO, number + 1000), transferred=True)


def test_404_for_an_issue_moved_into_the_configured_repository_stops_the_rest(h):
    h.add(OLD, "report", 5)
    h.add(RUN, "report", 9)
    h.conn.execute("UPDATE issues SET repo = 'tester/old' WHERE number = 5")
    h.failures[5] = http_error(404, category="not_found")
    h.failures[1005] = http_error(404, category="not_found")
    h.make_reader = lambda token, limits, **kw: ToConfiguredRepoReader()

    outcome = h.run()

    assert moved_row(h, OLD) == (REPO, 1005, "pending", 1)
    assert h.state(9)[1:3] == ("pending", 0)  # 設定の誤りなので、後ろは試さない
    assert outcome.as_dict()["not_attempted"] == 1
    assert outcome.as_dict()["error_category"] == "projects_not_found"


def test_a_chain_of_transfers_is_abandoned_by_the_total_attempts(h):
    h.add(OLD, "report", 5)
    for number in (5, 1005, 2005, 3005):
        h.failures[number] = http_error(404, category="not_found")
    h.make_reader = lambda token, limits, **kw: TransferredReader()  # 毎回、さらに転送される

    for expected in ("pending", "pending", "abandoned"):
        h.run()
        assert moved_row(h, OLD)[2] == expected

    assert moved_row(h, OLD)[3] == 3


def test_unreadable_configured_repository_is_a_config_stop(h):
    h.add(OLD, "report", 5)
    h.add(RUN, "report", 9)
    h.existing = {5, 9}
    h.failures[5] = http_error(404, category="not_found")
    del h.environ["GITHUB_ISSUE_REPO"]

    outcome = h.run()

    assert h.state(5)[1:4] == ("pending", 1, "config")
    assert h.state(9)[1:3] == ("pending", 0)
    assert outcome.as_dict()["not_attempted"] == 1
