"""Issue の Projects への登録と、前回までの未登録分の登録し直し（⑤。report-templates.md 4.4、
data-model.md 3.7、D-21）。

- 対象：issues の `projects_state = 'pending'` の行（種別・日次・週次を問わない）。今回作った
  （またはすでにあった）Issue を先に、残りを古い順に登録する
- 書き込みの予算：Issue の作成と共有する WriteBudget の残りまで。尽きたら残りは次の実行に回す
- 失敗の扱い：登録の失敗はジョブの成功とは別に記録する（行は pending のまま、試行回数と分類を
  残す）。実行エラーの Issue は作らない（US-E4-03）
- 404：Projects の番号・持ち主の誤りやトークンのスコープの不足でも返るので、そのままは issue_missing
  にしない。Issue 用トークンで Issue を読み直し、410、または 404 で Issue がないと確かめられた
  ときだけ issue_missing にする。Issue があるのに 404 なら、設定の誤りとして pending のまま
- 認証・権限・レート制限・設定の失敗は、残りも同じ理由で失敗するので、そこで止める
"""

import os
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from tech_researcher.collect.http import AUTH, BUDGET, FORBIDDEN, RATE_LIMITED, REDIRECT
from tech_researcher.config import Limits
from tech_researcher.db.issues import (
    IssueRow,
    mark_projects_abandoned,
    mark_projects_done,
    mark_projects_issue_missing,
    move_issue,
    pending_projects,
    record_projects_failure,
)
from tech_researcher.db.runs import CATEGORY
from tech_researcher.logging import RunLog
from tech_researcher.publish.github_issues import (
    IssueReader,
    IssueReadError,
    IssueRef,
    issue_repo,
    issue_token,
)
from tech_researcher.publish.github_projects import (
    CONFIG,
    ProjectsError,
    ProjectsRegistrar,
    projects_config,
)
from tech_researcher.publish.github_write import WriteBudget

NOT_FOUND_WITH_ISSUE = "projects_not_found"  # 404 だが Issue はある（設定の誤り）
TRANSFERRED = "issue_transferred"  # 404 で、Issue は別の場所に転送されている（1件の問題）
UNCHECKED = "projects_not_found_unchecked"  # 404 で、Issue があるか確かめられなかった
INVALID_ISSUE = "invalid_issue"  # 記録の repo・number が使えない形
DB_ERROR = "db_error"
# 残りの Issue も同じ理由で失敗するので、そこで止める分類
MAX_TRANSFER_ATTEMPTS = 3  # 転送先に更新できない・転送先でも登録できない Issue を諦める回数
_STOP = frozenset({AUTH, FORBIDDEN, RATE_LIMITED, CONFIG, BUDGET, REDIRECT, NOT_FOUND_WITH_ISSUE})


@dataclass
class ProjectsOutcome:
    """登録の結果（要約の projects に出す。ジョブの成功・失敗とは別）。"""

    registered: int = 0
    issue_missing: int = 0
    abandoned: int = 0  # 転送先でも登録できず、試行の上限で pending から外した分
    failed: int = 0
    not_attempted: int = 0  # 予算・時間の上限や、止める失敗のため要求しなかった分
    error_categories: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        out: dict = {
            "registered": self.registered,
            "issue_missing": self.issue_missing,
            "failed": self.failed,
            "not_attempted": self.not_attempted,
        }
        if self.abandoned:
            out["abandoned"] = self.abandoned
        if self.error_categories:
            out["error_category"] = self.error_categories[0]
        return out


class ProjectsSync:
    """未登録の Issue を Projects に登録する。依存は引数で受け取る（テストで差し替える）。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        limits: Limits,
        budget: WriteBudget,
        log: RunLog,
        *,
        environ: Mapping[str, str] = os.environ,
        make_registrar: Callable[..., ProjectsRegistrar] = ProjectsRegistrar,
        make_reader: Callable[..., IssueReader] = IssueReader,
        deadline: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.conn = conn
        self.run_id = run_id
        self.limits = limits
        self.budget = budget
        self.log = log
        self.environ = environ
        self.make_registrar = make_registrar
        self.make_reader = make_reader
        self.deadline = deadline
        self.clock = clock
        self._reader: IssueReader | None = None

    def run(self, first: IssueRef | None = None) -> ProjectsOutcome | None:
        """pending の Issue を登録する。first の Issue を先にする。対象がなければ None。"""
        try:
            rows = pending_projects(self.conn)
        except sqlite3.Error as e:
            self.log.write("post", "failure", error_category=DB_ERROR, exc=e)
            return ProjectsOutcome(failed=1, error_categories=[DB_ERROR])
        if first is not None:
            rows.sort(key=lambda r: (r.repo, r.number) != (first.repo, first.number))
        if not rows:
            return None
        outcome = ProjectsOutcome()
        try:
            registrar = self.make_registrar(
                projects_config(self.environ), self.limits, self.budget, clock=self.clock
            )
        except ProjectsError as e:
            self._fail(rows[0], e.category, outcome)  # 残りも同じ理由。要求も記録も1件だけ
            outcome.not_attempted += len(rows) - 1
            return outcome
        registrar.deadline = self.deadline
        for index, row in enumerate(rows):
            if self._out_of_time() or self.budget.left <= 0:
                outcome.not_attempted += len(rows) - index
                break
            try:
                category = self._register(registrar, row, outcome)
            except Exception as e:  # 想定外。この行に記録して止める（ジョブには影響させない）
                self.log.write("post", "failure", error_category="projects_unexpected", exc=e)
                category = self._fail(row, "unexpected", outcome)
                outcome.not_attempted += len(rows) - index - 1
                break
            if category in _STOP:
                outcome.not_attempted += len(rows) - index - 1
                break
        self.log.write("post", "end", detail="projects", count=outcome.registered)
        return outcome

    def _out_of_time(self) -> bool:
        return self.deadline is not None and self.clock() >= self.deadline

    def _register(
        self,
        registrar: ProjectsRegistrar,
        row: IssueRow,
        outcome: ProjectsOutcome,
        ref: IssueRef | None = None,
        moved: bool = False,
    ) -> str | None:
        """1件を登録する。失敗の分類（成功・issue_missing なら None）を返す。moved は、転送先に
        更新した後の登録のとき。"""
        ref = ref or IssueRef(str(row.repo), int(row.number or 0))
        try:
            registrar.add_issue(ref)
        except ValueError:
            return self._fail(row, INVALID_ISSUE, outcome)
        except ProjectsError as e:
            if e.status in (404, 410):  # Issue がないとは限らない。読み直して確かめる
                return self._not_found(registrar, row, outcome, ref, moved)
            return self._fail(row, e.category, outcome)
        return self._finish(row, outcome, missing=False)

    def _not_found(
        self,
        registrar: ProjectsRegistrar,
        row: IssueRow,
        outcome: ProjectsOutcome,
        ref: IssueRef,
        moved: bool,
    ) -> str | None:
        gone = self._issue_gone(ref)
        if gone is True:
            return self._finish(row, outcome, missing=True)
        if gone is False:
            # 設定の誤りと言えるのは、設定の投稿先にある Issue のときだけ。転送先・別の投稿先の
            # Issue は、この1件の問題として扱い、止めずに試行の上限で諦める
            if moved or ref.repo.lower() != self._configured_repo():
                return self._give_up_transfer(row, outcome)
            return self._fail(row, NOT_FOUND_WITH_ISSUE, outcome)
        if isinstance(gone, IssueRef):  # 転送されている。記録を転送先に更新して、登録し直す
            if not moved:
                try:
                    move_issue(self.conn, row.id, gone.repo, gone.number)
                except (sqlite3.Error, LookupError) as e:
                    self.log.write("post", "failure", error_category=DB_ERROR, exc=e)
                else:
                    self.log.write("post", "warning", error_category=TRANSFERRED, detail=_name(row))
                    return self._register(registrar, row, outcome, gone, moved=True)
            return self._give_up_transfer(row, outcome)
        self._fail(row, UNCHECKED, outcome)
        # Issue 用トークンの認証・権限・レート制限の失敗は、残りの確かめも同じ理由で失敗する
        return gone if gone in _STOP else UNCHECKED

    def _configured_repo(self) -> str:
        try:
            return issue_repo(self.environ)
        except IssueReadError, ValueError:
            return ""

    def _give_up_transfer(self, row: IssueRow, outcome: ProjectsOutcome) -> str:
        """転送先に更新できない・転送先でも登録できない。この1件だけ pending のまま続け、上限を
        超えたら pending から外す（毎回の予算を使い続けないため）。"""
        self._fail(row, TRANSFERRED, outcome)
        if row.projects_attempts + 1 >= MAX_TRANSFER_ATTEMPTS:
            try:
                mark_projects_abandoned(self.conn, row.id, self.run_id)
                outcome.abandoned += 1
            except (sqlite3.Error, LookupError) as e:
                self.log.write("post", "failure", error_category=DB_ERROR, exc=e)
        return TRANSFERRED

    def _issue_gone(self, ref: IssueRef) -> bool | str | IssueRef:
        """Issue 用トークンで Issue を読み直す。ない（404・410）なら True、あれば False、転送されて
        いれば転送先の IssueRef、確かめられなければ、読み取りの失敗の分類（文字列）。"""
        try:
            if self._reader is None:
                self._reader = self.make_reader(
                    issue_token(self.environ),
                    self.limits,
                    deadline=self.deadline,
                    clock=self.clock,
                )
            found = self._reader.get_issue(ref.repo, ref.number)
            if found.transferred and found.ref is not None:
                return found.ref
            return found.ref is None
        except IssueReadError as e:
            return e.category

    def _finish(self, row: IssueRow, outcome: ProjectsOutcome, *, missing: bool) -> str | None:
        try:
            if missing:
                mark_projects_issue_missing(self.conn, row.id, self.run_id)
            else:
                mark_projects_done(self.conn, row.id, self.run_id)
        except LookupError:  # もう pending でない。ほかの実行（日次・週次の重なり）が済ませた
            self.log.write("post", "warning", error_category="projects_already_done")
            return None
        except sqlite3.Error as e:
            self.log.write("post", "failure", error_category=DB_ERROR, exc=e)
            outcome.failed += 1
            outcome.error_categories.append(DB_ERROR)
            return DB_ERROR
        if missing:
            outcome.issue_missing += 1
            self.log.write("post", "warning", error_category="issue_missing", detail=_name(row))
        else:
            outcome.registered += 1
        return None

    def _fail(self, row: IssueRow, category: str, outcome: ProjectsOutcome) -> str:
        """pending のまま失敗を記録する（ジョブの失敗にはしない）。"""
        category = category if CATEGORY.fullmatch(category) else "unknown"
        outcome.failed += 1
        outcome.error_categories.append(category)
        self.log.write(
            "post",
            "warning",
            error_category=category if category.startswith("projects_") else f"projects_{category}",
            detail=_name(row),
        )
        try:
            record_projects_failure(self.conn, row.id, self.run_id, category)
        except LookupError:  # もう pending でない（ほかの実行が済ませた）
            pass
        except sqlite3.Error as e:
            self.log.write("post", "failure", error_category=DB_ERROR, exc=e)
        return category


def _name(row: IssueRow) -> str:
    return f"{row.repo}#{row.number}"
