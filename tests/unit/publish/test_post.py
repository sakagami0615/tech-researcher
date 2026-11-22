"""⑤ 投稿（publish/post.py）。GitHub は偽物、DB と issue.md は本物。"""

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tech_researcher.config import load_config
from tech_researcher.db import issues as issues_db
from tech_researcher.db.runs import start_run
from tech_researcher.db.schema import open_db
from tech_researcher.logging import RunLog
from tech_researcher.periods import daily_target
from tech_researcher.publish.github_issues import (
    IssueLookup,
    IssueReadError,
    IssueRef,
    ListedIssue,
    parse_marker,
)
from tech_researcher.publish.github_write import CreatedIssue, IssueWriteError
from tech_researcher.publish.post import (
    BadIssue,
    Poster,
    check_report,
    expected_labels,
    is_secret_stop,
    parse_issue,
)
from tech_researcher.render.common import Report, identifier
from tech_researcher.render.failure import secret_stop_report
from tech_researcher.stages.daily import ISSUE, issue_text
from tech_researcher.validate.secret_scan import SecretScanner

CONFIG_PATH = Path(__file__).parents[3] / "config" / "tech-researcher.toml"
DAY = date(2026, 10, 23)
TARGET = daily_target(date(2026, 10, 24), 1)
RUN = "20261024-060000-abcd"
RETRY = "20261024-080000-0123"
AT = datetime(2026, 10, 24, 6, 0, tzinfo=UTC)
REPO = "tester/sandbox"
ISSUE_TOKEN = "fake-issue-credential-9876"
SECRET = "ghp_" + "a" * 36  # トークンの形のダミー
ENVIRON = {"GITHUB_ISSUE_TOKEN": ISSUE_TOKEN, "GITHUB_ISSUE_REPO": REPO}
LABELS = {
    "report": ("type:report", "cadence:daily"),
    "rejected": ("type:rejected", "cadence:daily"),
    "run_error": ("type:run-error", "cadence:daily"),
    "run_error_secret": ("type:run-error", "cadence:daily"),
}


def report_for(run=RUN, text="本文"):
    return Report(
        "[日次] 2026-10-23（金）の技術動向",
        f"{identifier('daily', DAY, run)}\n{text}\n",
        LABELS["report"],
    )


def other_for(kind, text="本文"):
    return Report(f"[{kind}] 2026-10-23（金）", f"{text}\n", LABELS[kind])


class FakeWriter:
    """作成の要求を覚え、用意した失敗を順に投げる。"""

    def __init__(self, github):
        self.github = github
        self.deadline = None

    def create_issue(self, repo, title, body, labels):
        self.github.attempts.append((title, body, labels))
        if self.github.failures:
            raise self.github.failures.pop(0)
        number = 100 + len(self.github.created)
        self.github.created.append(
            {"repo": repo, "title": title, "body": body, "labels": labels, "number": number}
        )
        return CreatedIssue(IssueRef(repo, number), self.github.missing)


class FakeReader:
    def __init__(self, github):
        self.github = github

    def get_issue(self, repo, number):
        found = any(c["number"] == number and c["repo"] == repo for c in self.github.created)
        return IssueLookup(IssueRef(repo, number) if found else None, status=None if found else 404)

    def viewer_login(self):
        return "bot"

    def list_recent_issues(self, repo, since, creator=None):
        """ラベルによらず、作った Issue を返す（ラベルが外れた Issue も見つかる）。"""
        self.github.recent_args.append((since, creator))
        if self.github.recent_error:
            raise self.github.recent_error
        for c in self.github.created:
            yield ListedIssue(c["number"], parse_marker(c["body"]))

    def list_report_issues(self, repo, cadence):
        if self.github.read_error:
            raise self.github.read_error
        for c in self.github.created:
            if "type:report" in c["labels"] and f"cadence:{cadence}" in c["labels"]:
                yield ListedIssue(c["number"], parse_marker(c["body"]))


class FakeGitHub:
    def __init__(self):
        self.created = []
        self.attempts = []
        self.failures = []
        self.missing = ()
        self.read_error = None
        self.writers = []
        self.token = None
        self.reader_args = []
        self.recent_args = []
        self.recent_error = None
        self.unlabeled = False  # 作った Issue のラベルを外して、ラベルの探索から見えなくする

    def make_writer(self, token, limits, clock=None):
        self.token = token
        writer = FakeWriter(self)
        self.writers.append(writer)
        return writer

    def make_reader(self, token, limits, **kw):
        self.reader_args.append(kw)
        return FakeReader(self)


@pytest.fixture
def github():
    return FakeGitHub()


@pytest.fixture
def conn(tmp_path):
    conn = open_db(tmp_path / "tech-researcher.sqlite3")
    for run in (RUN, RETRY):
        start_run(conn, run, TARGET, AT)
    yield conn
    conn.close()


class Env:
    def __init__(self, tmp_path, conn, github):
        self.tmp_path = tmp_path
        self.conn = conn
        self.github = github
        config = load_config(CONFIG_PATH)
        self.config = replace(
            config, limits=replace(config.limits, http_retry_waits_s=(5, 20), github_write_calls=20)
        )
        self.scanner = SecretScanner([ISSUE_TOKEN])
        self.sleeps = []
        self.environ = dict(ENVIRON)
        self.clock = lambda: 0.0

    def write(self, report, run=RUN):
        run_dir = self.tmp_path / run
        run_dir.mkdir(exist_ok=True)
        (run_dir / ISSUE).write_text(issue_text(report), encoding="utf-8")

    def log_lines(self, run=RUN):
        path = self.tmp_path / run / "log.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def poster(self, run=RUN, **kw):
        run_dir = self.tmp_path / run
        run_dir.mkdir(exist_ok=True)
        log = RunLog(run_dir / "log.jsonl", run, "daily", DAY, self.scanner)
        options = {
            "environ": self.environ,
            "now": AT,
            "make_writer": self.github.make_writer,
            "make_reader": self.github.make_reader,
            "sleep": self.sleeps.append,
            "clock": self.clock,
            **kw,
        }
        return Poster(self.config, TARGET, run, run_dir, log, self.scanner, self.conn, **options)

    def post(self, kind, report=None, run=RUN, stopped=None, **kw):
        self.write(report or (report_for(run) if kind == "report" else other_for(kind)), run)
        return self.poster(run, **kw).run(kind, stopped)

    def issue_rows(self):
        return self.conn.execute(
            "SELECT run_id, kind, state, repo, number, projects_state FROM issues ORDER BY id"
        ).fetchall()

    def run_row(self, run=RUN):
        return self.conn.execute(
            "SELECT status, failed_stage, error_category, retry_count FROM runs WHERE run_id = ?",
            (run,),
        ).fetchone()


@pytest.fixture
def env(tmp_path, conn, github):
    return Env(tmp_path, conn, github)


# --- 作る・記録する ---


def test_report_is_created_with_labels_recorded_and_run_closed(env, github):
    result = env.post("report", retry_count=1)

    assert result == {"result": "created", "kind": "report", "repo": REPO, "number": 100}
    [created] = github.created
    assert created["labels"] == LABELS["report"]  # ラベルは作成の要求そのものに含める
    assert github.token == ISSUE_TOKEN
    assert env.issue_rows() == [(RUN, "report", "created", REPO, 100, "pending")]
    assert env.conn.execute("SELECT value FROM meta WHERE key = 'issue_repo'").fetchone() == (REPO,)
    assert env.run_row() == ("succeeded", None, None, 1)
    stages = [(r["stage"], r["event"]) for r in env.log_lines()]
    assert stages == [("post", "start"), ("post", "end")]


@pytest.mark.parametrize("kind", ["rejected", "run_error"])
def test_failure_issue_is_created_without_closing_a_closed_run(env, github, kind):
    from tech_researcher.db.runs import finish_run

    finish_run(env.conn, RUN, "rejected", AT, error_category="x", retry_count=0)
    result = env.post(kind)

    assert result["result"] == "created"
    assert github.created[0]["labels"] == LABELS[kind]
    assert env.issue_rows() == [(RUN, kind, "created", REPO, 100, "pending")]
    assert env.run_row() == ("rejected", None, "x", 0)  # 閉じた実行は変えない


def test_failure_issue_is_not_created_twice_in_the_same_run(env, github):
    env.post("run_error")
    again = env.post("run_error")

    assert again["result"] == "existing" and again["number"] == 100
    assert len(github.created) == 1


# --- 重複判定（4.3）---


def test_existing_report_for_the_same_target_is_not_created_again(env, github):
    env.post("report")
    result = env.post("report", run=RETRY)

    assert (result["result"], result["number"]) == ("existing", 100)
    assert len(github.created) == 1
    assert env.run_row(RETRY)[0] == "succeeded"
    assert env.issue_rows() == [(RUN, "report", "created", REPO, 100, "pending")]


def test_stale_record_recreates_and_moves_the_row_to_this_run(env, github):
    """410・404 で 2 でも見つからなければ作り直し、記録の run_id を付け替える（replaces）。"""
    issues_db.record_issue(
        env.conn, RUN, "report", "daily", DAY, "created", AT, repo=REPO, number=11
    )
    result = env.post("report", run=RETRY)

    assert (result["result"], result["number"]) == ("created", 100)
    assert env.issue_rows() == [(RETRY, "report", "created", REPO, 100, "pending")]


def test_existing_found_by_label_is_recorded_without_creating(env, github):
    """DB に記録がなくても、ラベルと識別子で見つかれば作らない（作成の直後に落ちた後の再試行）。"""
    github.created.append(
        {
            "repo": REPO,
            "title": "t",
            "body": report_for(RUN).body,
            "labels": LABELS["report"],
            "number": 5,
        }
    )
    result = env.post("report", run=RETRY)

    assert (result["result"], result["number"]) == ("existing", 5)
    assert github.attempts == []
    assert env.issue_rows() == [(RUN, "report", "created", REPO, 5, "pending")]


def test_dedup_read_failure_is_a_failed_post(env, github):
    github.read_error = IssueReadError("x", "http_5xx", 503)
    result = env.post("report")

    assert (result["result"], result["error_category"]) == ("failed", "dedup_http_5xx")
    assert github.attempts == []
    assert env.run_row() == ("failed", "post", "dedup_http_5xx", None)
    assert env.issue_rows() == [(RUN, "report", "failed", None, None, None)]


# --- 再試行・上限 ---


def test_transient_failure_is_retried_after_waiting_and_redoing_the_dedup(env, github):
    github.failures = [_error("dns", True)]  # 送る前の失敗だけ再試行する
    result = env.post("report")

    assert (result["result"], result["number"]) == ("created", 100)
    assert len(github.attempts) == 2
    assert env.sleeps == [5]
    events = [(r["event"], r.get("error_category")) for r in env.log_lines()]
    assert ("retry", "dns") in events


def test_lost_response_is_found_by_the_dedup_before_retrying(env, github):
    """応答が届かないまま Issue が作られていても、再試行の前の重複判定（ラベル）で見つかる。"""

    class Lost(FakeWriter):
        def create_issue(self, repo, title, body, labels):
            github.created.append(
                {"repo": repo, "title": title, "body": body, "labels": labels, "number": 50}
            )
            raise IssueWriteError("x", "timeout")

    github.make_writer = lambda token, limits, clock=None: Lost(github)
    result = env.post("report")

    assert (result["result"], result["number"]) == ("existing", 50)
    assert len(github.created) == 1  # 2件目を作らない
    assert env.run_row()[0] == "succeeded"


def test_retries_are_limited_and_exhausted_failure_is_recorded(env, github):
    github.failures = [_error("dns", True) for _ in range(5)]
    result = env.post("report")

    assert (result["result"], result["error_category"]) == ("failed", "dns")
    assert len(github.attempts) == 3  # 初回 + 再試行2回（http_retry_waits_s の数）
    assert env.sleeps == [5, 20]
    assert env.issue_rows() == [(RUN, "report", "failed", None, None, None)]  # 送っていない
    assert env.run_row() == ("failed", "post", "dns", None)


def test_client_error_is_not_retried(env, github):
    github.failures = [IssueWriteError("x", "http_4xx", 422)]
    result = env.post("report")

    assert (result["result"], result["error_category"]) == ("failed", "http_4xx")
    assert len(github.attempts) == 1 and env.sleeps == []


def test_retry_after_is_honored_up_to_the_cap(env, github):
    cap = env.config.limits.retry_after_max_s
    github.failures = [
        IssueWriteError("x", "rate_limited", 403, retry_after=cap + 100),
        IssueWriteError("x", "rate_limited", 403, retry_after=3),
    ]
    assert env.post("report")["result"] == "created"
    assert env.sleeps == [cap, 3]


def test_no_retry_when_the_wait_would_pass_the_deadline(env, github):
    github.failures = [IssueWriteError("x", "http_5xx", 503)]
    result = env.post("report", deadline=3.0)  # clock は 0 で、待ち 5 秒は上限を超える

    assert (result["result"], result["error_category"]) == ("failed", "http_5xx")
    assert len(github.attempts) == 1 and env.sleeps == []


def test_write_budget_exhausted_is_a_failed_post(env, github):
    github.failures = [IssueWriteError("x", "budget")]
    result = env.post("report")

    assert (result["result"], result["error_category"]) == ("failed", "budget")
    assert len(github.attempts) == 1


def test_failed_record_never_replaces_a_created_issue(env, github):
    env.post("run_error")
    github.failures = [IssueWriteError("x", "http_4xx", 422)]
    again = env.poster().run("run_error")  # 作成済みなので何も呼ばない

    assert again["result"] == "existing"
    assert env.issue_rows() == [(RUN, "run_error", "created", REPO, 100, "pending")]


def test_record_failure_after_creation_is_reported_with_the_number(env, github, monkeypatch):
    def broken(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr("tech_researcher.publish.post.record_issue", broken)
    result = env.post("report")

    assert (result["result"], result["error_category"], result["number"]) == (
        "failed",
        "issue_record_failed",
        100,
    )


def test_labels_missing_is_logged_as_a_warning(env, github):
    github.missing = ("type:report",)
    result = env.post("report")

    assert result["labels_missing"] is True
    assert ("post", "warning") in [(r["stage"], r["event"]) for r in env.log_lines()]


# --- 秘匿情報（投稿の直前の検査）---


def test_secret_in_the_body_is_not_posted_and_a_stop_notice_is_created(env, github):
    result = env.post("report", report_for(text=f"キーは {SECRET}"))

    assert (result["result"], result["error_category"], result["kind"]) == (
        "blocked",
        "secret_detected",
        "run_error_secret",
    )
    [created] = github.created
    assert "公開停止" in created["title"] and SECRET not in created["body"]
    assert created["labels"] == LABELS["run_error_secret"]
    assert created["body"] == secret_stop_report(TARGET, "post").body
    assert "処理段階 | ⑤ 投稿" in created["body"]
    assert env.issue_rows() == [
        (RUN, "report", "blocked_secret", None, None, None),
        (RUN, "run_error_secret", "created", REPO, 100, "pending"),
    ]
    assert env.run_row() == ("failed", "post", "secret_detected", None)
    text = (env.tmp_path / RUN / "log.jsonl").read_text()
    assert SECRET not in text and "secret_detected" in text


@pytest.mark.parametrize("kind", ["rejected", "run_error"])
def test_secret_in_a_failure_body_is_replaced_by_the_notice(env, github, kind):
    result = env.post(kind, other_for(kind, f"キーは {SECRET}"))

    assert result["result"] == "blocked"
    assert [c["labels"] for c in github.created] == [LABELS["run_error_secret"]]
    assert env.issue_rows() == [
        (RUN, kind, "blocked_secret", None, None, None),
        (RUN, "run_error_secret", "created", REPO, 100, "pending"),
    ]


def test_secret_notice_is_posted_as_is_and_the_stopped_kind_is_recorded(env, github):
    """③ や失敗の分岐が置き換えた定型は、そのまま投稿し、止めた種別（stopped）を記録する。"""
    notice = secret_stop_report(TARGET, "verify")
    result = env.post("run_error_secret", notice, stopped="report")

    assert (result["result"], result["number"]) == ("created", 100)
    assert github.created[0]["body"] == notice.body
    assert env.issue_rows() == [
        (RUN, "report", "blocked_secret", None, None, None),
        (RUN, "run_error_secret", "created", REPO, 100, "pending"),
    ]


def test_secret_in_the_notice_itself_records_only(env, github, monkeypatch):
    """定型でも再検出なら、何も作らず記録だけにする。"""
    notice = secret_stop_report(TARGET, "verify")
    env.scanner = SecretScanner(["公開を停止しました"])  # 定型の文に反応させる
    result = env.post("run_error_secret", notice, stopped="report")

    assert (result["result"], result["error_category"]) == ("blocked", "secret_detected")
    assert github.attempts == []
    assert env.issue_rows() == [
        (RUN, "report", "blocked_secret", None, None, None),
        (RUN, "run_error_secret", "blocked_secret", None, None, None),
    ]
    assert env.run_row()[:3] == ("failed", "post", "secret_detected")


def test_notice_recheck_after_replacing_records_both_and_creates_nothing(env, github):
    env.scanner = SecretScanner(["公開を停止しました"])
    result = env.post("report", report_for(text=f"キーは {SECRET}"))

    assert result["result"] == "blocked" and github.attempts == []
    assert [row[1:3] for row in env.issue_rows()] == [
        ("report", "blocked_secret"),
        ("run_error_secret", "blocked_secret"),
    ]


# --- 準備の失敗 ---


def test_destination_mismatch_stops_before_any_write(env, github):
    from tech_researcher.db.meta import record_issue_repo

    record_issue_repo(env.conn, "other/prod")
    result = env.post("report")

    assert (result["result"], result["error_category"]) == ("failed", "config")
    assert github.attempts == []
    assert [row[1:3] for row in env.issue_rows()] == [("report", "failed")]  # 作れなかった記録
    assert env.run_row() == ("failed", "post", "config", None)


@pytest.mark.parametrize(
    ("change", "category"),
    [
        ({"GITHUB_ISSUE_TOKEN": ""}, "auth"),
        ({"GITHUB_ISSUE_REPO": "not a repo"}, "config"),
        ({"GITHUB_ISSUE_REPO": ""}, "config"),
    ],
)
def test_missing_or_bad_environment_is_a_failed_post(env, github, change, category):
    env.environ.update(change)
    result = env.post("report")

    assert (result["result"], result["error_category"]) == ("failed", category)
    assert github.attempts == []


@pytest.mark.parametrize(
    "report",
    [
        Report("t", report_for().body, ("type:rejected", "cadence:daily")),  # 種別とラベルが違う
        Report("t", "識別子がない\n", LABELS["report"]),
        Report("t", report_for(RETRY).body, LABELS["report"]),  # 別の実行の識別子
        Report("x" * 257, report_for().body, LABELS["report"]),
    ],
)
def test_bad_report_is_not_posted(env, github, report):
    result = env.post("report", report)

    assert (result["result"], result["error_category"]) == ("failed", "bad_issue")
    assert github.attempts == []


def test_missing_or_broken_issue_file_is_a_failed_post(env, github):
    (env.tmp_path / RUN).mkdir()
    assert env.poster().run("report")["error_category"] == "bad_issue"
    (env.tmp_path / RUN / ISSUE).write_text("だけ", encoding="utf-8")
    assert env.poster().run("report")["error_category"] == "bad_issue"


# --- 部品 ---


def test_parse_issue_round_trips_issue_text():
    report = report_for()
    assert parse_issue(issue_text(report)) == report


@pytest.mark.parametrize("text", ["", "t\n\n---\n\nb", "t\na, b\n---\nb", "t\n\n\n---\n\n本文"])
def test_parse_issue_rejects_other_shapes(text):
    with pytest.raises(BadIssue):
        parse_issue(text)


def test_expected_labels_follow_d08():
    assert expected_labels("report", "weekly") == {"type:report", "cadence:weekly"}
    assert expected_labels("run_error_secret", "daily") == expected_labels("run_error", "daily")
    with pytest.raises(BadIssue):
        check_report(report_for(), "unknown", TARGET, RUN)


def test_is_secret_stop_only_for_the_notice():
    assert is_secret_stop(secret_stop_report(TARGET, "verify"))
    assert not is_secret_stop(report_for())


# --- 再試行してよい失敗（種別ごと）---


def _error(category, unsent, **kw):
    return IssueWriteError("x", category, unsent=unsent, **kw)


@pytest.mark.parametrize("kind", ["rejected", "run_error"])
@pytest.mark.parametrize("category", ["timeout", "network", "http_5xx"])
def test_failure_issue_is_not_retried_when_the_response_may_be_lost(env, github, kind, category):
    """識別子がない失敗の Issue は、応答が返らない失敗の後に再試行すると2件作りうる。"""
    github.failures = [_error(category, False)]
    result = env.post(kind)

    assert (result["result"], result["error_category"]) == ("failed", category)
    assert len(github.attempts) == 1 and env.sleeps == []
    assert [row[1:3] for row in env.issue_rows()] == [(kind, "failed")]


@pytest.mark.parametrize("category", ["dns", "timeout", "network"])
def test_failure_issue_is_retried_when_it_was_certainly_not_sent(env, github, category):
    github.failures = [_error(category, True)]
    result = env.post("run_error")

    assert (result["result"], result["number"]) == ("created", 100)
    assert len(github.attempts) == 2 and env.sleeps == [5]


@pytest.mark.parametrize("category", ["timeout", "network", "http_5xx"])
def test_maybe_created_failure_is_retried_when_the_unlabeled_lookup_finds_nothing(
    env, github, category
):
    github.failures = [_error(category, False)]
    result = env.post("report")

    assert (result["result"], result["number"]) == ("created", 100)
    assert len(github.attempts) == 2 and env.sleeps == [5]
    [(since, creator)] = github.recent_args  # ラベルなし・この実行の開始以降・トークンの持ち主
    assert (since, creator) == ("2026-10-24T06:00:00Z", "bot")


def test_maybe_created_failure_finds_an_issue_whose_labels_were_removed(env, github):
    """ラベルが外れた Issue は、ラベルなしの確認で見つけ、再投稿しない。"""

    class Lost(FakeWriter):
        def create_issue(self, repo, title, body, labels):
            github.attempts.append(1)
            github.created.append(
                {"repo": repo, "title": title, "body": body, "labels": (), "number": 70}
            )
            raise _error("timeout", False)

    github.make_writer = lambda token, limits, clock=None: Lost(github)
    result = env.post("report")

    assert (result["result"], result["number"]) == ("existing", 70)
    assert len(github.attempts) == 1 and env.sleeps == []
    assert env.issue_rows() == [(RUN, "report", "created", REPO, 70, "pending")]


def test_maybe_created_failures_to_the_end_are_uncertain(env, github):
    github.failures = [_error("timeout", False) for _ in range(5)]
    result = env.post("report")

    assert (result["result"], result["error_category"]) == ("failed", "timeout")
    assert len(github.attempts) == 3 and env.sleeps == [5, 20]  # 再試行は2回まで
    assert [row[1:3] for row in env.issue_rows()] == [("report", "uncertain")]


def test_invalid_response_is_not_retried_and_is_uncertain(env, github):
    github.failures = [_error("invalid_response", False)]
    result = env.post("report")

    assert (result["result"], result["error_category"]) == ("failed", "invalid_response")
    assert len(github.attempts) == 1
    assert [row[1:3] for row in env.issue_rows()] == [("report", "uncertain")]


def test_unreadable_unlabeled_lookup_gives_up_as_uncertain_without_posting_again(env, github):
    github.failures = [_error("timeout", False)]
    github.recent_error = IssueReadError("x", "http_5xx", 503)
    result = env.post("report")

    assert (result["result"], result["error_category"]) == ("failed", "timeout")
    assert len(github.attempts) == 1
    assert [row[1:3] for row in env.issue_rows()] == [("report", "uncertain")]


def test_unsent_then_maybe_created_then_unsent_ends_uncertain(env, github):
    github.failures = [_error("dns", True), _error("timeout", False), _error("dns", True)]
    result = env.post("report")

    assert (result["result"], result["error_category"]) == ("failed", "dns")
    assert len(github.attempts) == 3
    assert [row[1:3] for row in env.issue_rows()] == [("report", "uncertain")]


def test_final_lookup_sqlite_error_stays_uncertain_with_db_error(env, github, monkeypatch):
    github.failures = [_error("invalid_response", False)]

    def broken(*a, **k):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr("tech_researcher.publish.post.check_recent_from_env", broken)
    result = env.post("report")

    assert (result["result"], result["error_category"]) == ("failed", "db_error")
    assert [row[1:3] for row in env.issue_rows()] == [("report", "uncertain")]


def test_stale_created_row_of_the_same_run_becomes_uncertain(env, github):
    """同じ実行に古い番号（404）の作成済みの行がある。uncertain に直す。"""
    issues_db.record_issue(
        env.conn, RUN, "report", "daily", DAY, "created", AT, repo=REPO, number=11
    )
    github.failures = [_error("invalid_response", False)]
    env.post("report")

    assert env.issue_rows() == [(RUN, "report", "uncertain", None, None, None)]


@pytest.mark.parametrize("later", ["budget", "http_4xx"])
def test_uncertain_even_if_the_last_failure_kind_is_certain(env, github, later):
    """作られたかもしれない失敗の後は、後続の失敗の種類によらず uncertain（POST は出さない）。"""
    github.failures = [_error("timeout", False), _error(later, later == "budget")]
    env.post("report")
    assert [row[1:3] for row in env.issue_rows()] == [("report", "uncertain")]


def test_invalid_response_for_a_report_runs_the_dedup_once(env, github):
    class Odd(FakeWriter):
        def create_issue(self, repo, title, body, labels):
            github.created.append(
                {"repo": repo, "title": title, "body": body, "labels": labels, "number": 60}
            )
            raise IssueWriteError("x", "invalid_response", 201)

    github.make_writer = lambda token, limits, clock=None: Odd(github)
    result = env.post("report")

    assert (result["result"], result["number"]) == ("existing", 60)
    assert env.issue_rows() == [(RUN, "report", "created", REPO, 60, "pending")]  # 記録された


def test_invalid_response_without_a_found_issue_fails(env, github):
    github.failures = [IssueWriteError("x", "invalid_response", 201)]
    result = env.post("report")
    assert (result["result"], result["error_category"]) == ("failed", "invalid_response")


# --- 投稿先の記録（meta）---


def test_existing_issue_is_not_a_success_until_the_destination_is_recorded(
    env, github, monkeypatch
):
    env.post("report")
    calls = []

    def broken(conn, repo):
        calls.append(repo)
        raise OSError("disk")

    monkeypatch.setattr("tech_researcher.publish.post.record_issue_repo", broken)
    monkeypatch.setattr("tech_researcher.publish.dedup.record_issue_repo", broken)
    env.conn.execute("DELETE FROM meta")  # 記録に失敗した後の状態
    result = env.post("report", run=RETRY)

    # 重複判定の中の記録の失敗も、見つけた Issue の番号を結果に載せる
    assert (result["result"], result["error_category"], result["number"]) == (
        "failed",
        "issue_record_failed",
        100,
    )
    assert env.run_row(RETRY)[:3] == ("failed", "post", "issue_record_failed")


def test_destination_is_recorded_when_returning_to_an_existing_issue(env, github):
    env.post("report")
    env.conn.execute("DELETE FROM meta")  # record_issue_repo が失敗した後の状態
    result = env.post("report", run=RETRY)

    assert result["result"] == "existing"
    assert env.conn.execute("SELECT value FROM meta").fetchall() == [(REPO,)]


def test_same_run_failure_issue_also_records_the_destination(env, github):
    env.post("run_error")
    env.conn.execute("DELETE FROM meta")
    assert env.poster().run("run_error")["result"] == "existing"
    assert env.conn.execute("SELECT value FROM meta").fetchall() == [(REPO,)]


# --- 公開停止の定型の投稿に失敗したとき ---


def test_failed_notice_keeps_the_secret_category_and_the_write_error_separately(env, github):
    github.failures = [IssueWriteError("x", "http_4xx", 422)]
    result = env.post("report", report_for(text=f"キーは {SECRET}"))

    assert (result["result"], result["error_category"]) == ("failed", "secret_detected")
    assert result["write_error"] == "http_4xx"
    assert env.run_row()[:3] == ("failed", "post", "secret_detected")


# --- 読み取りの時間の上限 ---


def test_dedup_reader_gets_the_deadline_and_clock(env, github):
    env.post("report", deadline=42.0)
    assert github.reader_args[0]["deadline"] == 42.0
    assert github.reader_args[0]["clock"] is env.clock


# --- 転送された Issue（PR #99 の2回目の確認）---


def test_transferred_issue_keeps_being_found_on_later_runs(env, github):
    """作る → 別のリポジトリへ転送 → 次の実行が 301 をたどる → さらに次の実行、でも毎回成功する。"""
    third = "20261024-090000-4567"
    start_run(env.conn, third, TARGET, AT)
    env.post("report")
    moved = IssueRef("other/dest", 9)

    class Reader(FakeReader):
        def get_issue(self, repo, number):
            if (repo, number) == (REPO, 100):
                return IssueLookup(moved, transferred=True)
            if (repo, number) == (moved.repo, moved.number):
                return IssueLookup(moved)
            return super().get_issue(repo, number)

    github.make_reader = lambda token, limits, **kw: Reader(github)
    second = env.post("report", run=RETRY)
    assert (second["result"], second["repo"], second["number"]) == ("existing", "other/dest", 9)
    later = env.post("report", run=third)
    assert (later["result"], later["repo"], later["number"]) == ("existing", "other/dest", 9)
    assert len(github.created) == 1
    assert env.run_row(third)[0] == "succeeded"


# --- 準備の失敗（PR #99 の2回目の確認）---


def test_stopped_kind_is_recorded_even_when_preparation_fails(env, github):
    env.environ["GITHUB_ISSUE_TOKEN"] = ""
    env.write(other_for("run_error_secret"))
    result = env.poster().run("run_error_secret", "report")

    assert result["error_category"] == "auth"
    assert [row[1:3] for row in env.issue_rows()] == [
        ("report", "blocked_secret"),
        ("run_error_secret", "failed"),
    ]


def test_sqlite_error_while_checking_the_destination_is_a_db_error(env, github, monkeypatch):
    def broken(conn, repo):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr("tech_researcher.publish.post.check_issue_repo", broken)
    result = env.post("report")

    assert (result["result"], result["error_category"]) == ("failed", "db_error")
    assert env.run_row()[:3] == ("failed", "post", "db_error")
    assert [row[1:3] for row in env.issue_rows()] == [("report", "failed")]


# --- 作られたか分からないレポート（uncertain）---


def test_unreadable_creation_without_a_found_issue_is_recorded_as_uncertain(env, github):
    github.failures = [IssueWriteError("x", "invalid_response", 201)]
    result = env.post("report")

    assert (result["result"], result["error_category"]) == ("failed", "invalid_response")
    assert [row[1:3] for row in env.issue_rows()] == [("report", "uncertain")]


def test_next_run_does_not_recreate_when_an_uncertain_record_exists(env, github):
    github.failures = [IssueWriteError("x", "invalid_response", 201)]
    env.post("report")
    result = env.post("report", run=RETRY)

    assert (result["result"], result["error_category"]) == ("failed", "needs_review")
    assert github.created == []  # 自動で作り直さない
    assert env.run_row(RETRY)[:3] == ("failed", "post", "needs_review")
    assert [row[:3] for row in env.issue_rows()] == [
        (RUN, "report", "uncertain"),
        (RETRY, "report", "failed"),
    ]


def test_uncertain_record_does_not_block_when_the_issue_is_found(env, github):
    github.failures = [IssueWriteError("x", "invalid_response", 201)]
    env.post("report")
    github.created.append(  # 運用者がラベルを戻した、など
        {
            "repo": REPO,
            "title": "t",
            "body": report_for(RUN).body,
            "labels": LABELS["report"],
            "number": 8,
        }
    )
    assert env.post("report", run=RETRY)["result"] == "existing"


def test_operator_can_release_by_deleting_the_uncertain_record(env, github):
    github.failures = [IssueWriteError("x", "invalid_response", 201)]
    env.post("report")
    env.conn.execute("DELETE FROM issues WHERE state = 'uncertain'")  # 確かめて、作られていなかった
    assert env.post("report", run=RETRY)["result"] == "created"


# --- PR #99 の3回目の確認 ---


def test_config_change_after_a_failed_record_needs_review_and_writes_no_destination(env, github):
    """meta が空のまま、別のリポジトリの Issue を新しい投稿先に結び付けない。"""
    env.post("report")
    env.conn.execute("DELETE FROM meta")
    env.environ["GITHUB_ISSUE_REPO"] = "other/prod"
    result = env.post("report", run=RETRY)

    assert (result["result"], result["error_category"], result["number"]) == (
        "failed",
        "needs_review",
        100,
    )
    assert env.conn.execute("SELECT value FROM meta").fetchall() == []
    assert env.run_row(RETRY)[:3] == ("failed", "post", "needs_review")


def test_found_in_another_repo_is_a_success_when_meta_already_matches(env, github):
    env.post("report")
    github.make_reader = lambda token, limits, **kw: _MovedReader(github)
    assert env.post("report", run=RETRY)["result"] == "existing"
    assert env.conn.execute("SELECT value FROM meta").fetchall() == [(REPO,)]


class _MovedReader(FakeReader):
    def get_issue(self, repo, number):
        return IssueLookup(IssueRef("other/dest", 9), transferred=True)


def test_read_failure_of_the_last_look_still_records_uncertain(env, github):
    class Odd(FakeWriter):
        def create_issue(self, repo, title, body, labels):
            github.read_error = IssueReadError("x", "http_5xx", 503)  # 最後の確認で読めない
            raise IssueWriteError("x", "invalid_response", 201)

    github.make_writer = lambda token, limits, clock=None: Odd(github)
    result = env.post("report")

    assert (result["result"], result["error_category"]) == ("failed", "invalid_response")
    assert [row[1:3] for row in env.issue_rows()] == [("report", "uncertain")]


def test_rerun_with_the_same_run_id_does_not_slip_past_the_uncertain_record(env, github):
    github.failures = [IssueWriteError("x", "invalid_response", 201)]
    env.post("report")
    result = env.post("report")  # 同じ実行 ID でやり直す

    assert (result["result"], result["error_category"]) == ("failed", "needs_review")
    assert github.created == []
    assert [row[1:3] for row in env.issue_rows()] == [("report", "uncertain")]  # 上書きしない


@pytest.mark.parametrize("category", ["timeout", "network", "http_5xx"])
def test_report_ending_with_an_ambiguous_failure_is_uncertain(env, github, category):
    github.failures = [_error(category, False) for _ in range(3)]
    env.post("report")
    assert [row[1:3] for row in env.issue_rows()] == [("report", "uncertain")]


@pytest.mark.parametrize("category", ["http_4xx", "budget"])
def test_report_ending_with_a_certain_failure_is_failed(env, github, category):
    github.failures = [_error(category, category == "budget")]
    env.post("report")
    assert [row[1:3] for row in env.issue_rows()] == [("report", "failed")]


def test_last_look_finds_an_issue_created_by_the_final_attempt(env, github):
    class Lost(FakeWriter):
        def create_issue(self, repo, title, body, labels):
            github.attempts.append(1)
            github.created.append(
                {"repo": repo, "title": title, "body": body, "labels": labels, "number": 70}
            )
            raise _error("timeout", False)

    github.make_writer = lambda token, limits, clock=None: Lost(github)
    result = env.post("report")
    assert (result["result"], result["number"]) == ("existing", 70)


def test_run_close_failure_is_a_failed_post_that_keeps_the_number(env, github, monkeypatch):
    def broken(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr("tech_researcher.publish.post.finish_run", broken)
    result = env.post("report")

    # 結果と分類は変えず、閉じられなかったことを別に返す
    assert (result["result"], result["number"], result["run_close_failed"]) == (
        "created",
        100,
        True,
    )
    assert "error_category" not in result
    assert env.run_row()[0] == "running"


def test_uncertain_record_is_cleared_when_the_issue_is_found(env, github):
    github.failures = [IssueWriteError("x", "invalid_response", 201)]
    env.post("report")
    github.created.append(
        {
            "repo": REPO,
            "title": "t",
            "body": report_for(RUN).body,
            "labels": LABELS["report"],
            "number": 8,
        }
    )
    assert env.post("report", run=RETRY)["result"] == "existing"
    # 見つけた Issue の行（labels の補い）が、uncertain の行を置き換える
    assert [row[:5] for row in env.issue_rows() if row[0] == RUN] == [
        (RUN, "report", "created", REPO, 8)
    ]


def test_close_failure_after_the_stop_notice_is_run_close_failed(env, github, monkeypatch):
    def broken(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr("tech_researcher.publish.post.finish_run", broken)
    result = env.post("report", report_for(text=f"キーは {SECRET}"))

    assert (result["result"], result["error_category"], result["number"]) == (
        "blocked",
        "secret_detected",  # 分類は検出のまま
        100,
    )
    assert result["run_close_failed"] is True


def test_close_failure_when_the_notice_also_has_a_secret_keeps_the_category(
    env, github, monkeypatch
):
    def broken(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr("tech_researcher.publish.post.finish_run", broken)
    env.scanner = SecretScanner(["公開を停止しました"])
    result = env.post("report", report_for(text=f"キーは {SECRET}"))

    assert (result["result"], result["error_category"]) == ("blocked", "secret_detected")
    assert result["run_close_failed"] is True


def test_found_in_another_repo_follows_the_dedup_decision_not_the_issue_repo(env, github):
    """meta を書くかは dedup の判定（元の記録の repo）に従う。転送先の repo では決めない。"""
    issues_db.record_issue(
        env.conn, RUN, "report", "daily", DAY, "created", AT, repo="old/repo", number=11
    )

    class Reader(FakeReader):
        def get_issue(self, repo, number):
            return IssueLookup(IssueRef(REPO, 7), transferred=True)  # 設定の投稿先へ転送された

    github.make_reader = lambda token, limits, **kw: Reader(github)
    result = env.post("report", run=RETRY)

    assert (result["result"], result["error_category"]) == ("failed", "needs_review")
    assert env.conn.execute("SELECT count(*) FROM meta").fetchone() == (0,)


def test_failing_to_clear_the_uncertain_record_is_not_a_success(env, github, monkeypatch):
    env.post("report")
    monkeypatch.setattr(Poster, "_clear_uncertain", lambda self: False)
    result = env.post("report", run=RETRY)

    assert (result["result"], result["error_category"], result["number"]) == (
        "failed",
        "db_error",
        100,
    )
