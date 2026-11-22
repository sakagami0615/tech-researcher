"""レポート Issue の重複判定（report-templates.md 4.3、US-E4-03・US-E5-03）。

⑤ が Issue を作る直前に呼ぶ。作る処理そのものは呼び出し側。判定の結果は3つ。
- Existing：同じ対象の Issue がある。作らない（Projects に未登録なら登録だけを行う）
- Create：ない。作る。replaces は、DB に古い記録があって GitHub で 410・404 だったときだけ
  "recreate"（作った番号を record_issue に渡して、記録を付け替える）。それ以外は None
- Skipped：ドライランで Issue 用トークンか投稿先（`GITHUB_ISSUE_TOKEN`・`GITHUB_ISSUE_REPO`）が
  なく、判定しなかった。ドライランは Issue 用のトークンを要らないため（structure.md 3.6）。
  本番の実行でこれらがなければ、判定の前に IssueReadError で止める

DB に記録した投稿先が設定と違えば、判定を始める前に ConfigError で止める（meta。db/meta.py）。
DB の更新（転送先の番号、ラベルから見つけた Issue の補完、投稿先の記録）は、ドライランでは行わない。
GitHub を読めないとき（4.3 の「それ以外」）は IssueReadError のまま呼び出し側へ出す。
"""

import os
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime

from tech_researcher.config import Limits
from tech_researcher.db.issues import find_report_issue, record_issue
from tech_researcher.db.meta import check_issue_repo, record_issue_repo
from tech_researcher.publish.github_issues import (
    REPO_ENV,
    TOKEN_ENV,
    IssueReader,
    IssueRef,
    issue_repo,
    issue_token,
)


@dataclass(frozen=True)
class Existing:
    """作らない。via は見つけ方：db（DB の番号）・transferred（転送先）・labels（ラベルの探索）。"""

    ref: IssueRef
    via: str


@dataclass(frozen=True)
class Create:
    """作る。作った後は、呼び出し側が record_issue（replaces はこの値）と record_issue_repo
    （投稿先の記録。db/meta.py）を呼ぶ。"""

    replaces: str | None = None


@dataclass(frozen=True)
class Skipped:
    """ドライランで判定しなかった。reason は足りなかった環境変数の名前。"""

    reason: str


def check_duplicate_from_env(
    conn: sqlite3.Connection | None,
    limits: Limits,
    run_id: str,
    cadence: str,
    target: date,
    at: datetime,
    *,
    dry_run: bool = False,
    environ: Mapping[str, str] = os.environ,
    make_reader: Callable[..., IssueReader] = IssueReader,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Existing | Create | Skipped:
    """環境変数から Issue 用トークンと投稿先を読み、check_duplicate を呼ぶ。

    ドライランでどちらかがなければ Skipped。本番ではなければ IssueReadError。
    トークンの形が使えないときは、ドライランでも IssueReadError（値は出さない）。
    """
    if dry_run:
        missing = [n for n in (TOKEN_ENV, REPO_ENV) if not (environ.get(n) or "").strip()]
        if missing:
            return Skipped(missing[0])
    reader = make_reader(issue_token(environ), limits, deadline=deadline, clock=clock)
    return check_duplicate(
        conn, reader, issue_repo(environ), run_id, cadence, target, at, dry_run=dry_run
    )


def check_duplicate(
    conn: sqlite3.Connection | None,
    reader: IssueReader,
    repo: str,
    run_id: str,
    cadence: str,
    target: date,
    at: datetime,
    *,
    dry_run: bool = False,
) -> Existing | Create:
    """repo は投稿先の `owner/repo`。DB の記録は、転送先など別のリポジトリでもその番号を読む。

    conn は、DB がない開発機のドライランだけ None にできる（記録がないものとして判定する）。
    """
    repo = repo.lower()
    if conn is None:
        if not dry_run:
            raise ValueError("DB がないのは、ドライランだけ")
        return _search(None, reader, repo, run_id, cadence, target, at, True, False)
    check_issue_repo(conn, repo)
    stale = False
    row = find_report_issue(conn, cadence, target)
    if row is not None:
        if row.repo is not None and row.number is not None:
            found = reader.get_issue(row.repo, row.number)
            if found.ref is not None and not found.transferred:
                if not dry_run and row.repo.lower() == repo:
                    record_issue_repo(conn, repo)
                return Existing(found.ref, "db")
            if found.ref is not None:
                if not dry_run:
                    _record(conn, run_id, run_id, cadence, target, at, found.ref, "transfer")
                    record_issue_repo(conn, repo)
                return Existing(found.ref, "transferred")
        stale = True  # 410・404。2 で確かめ直す
    return _search(conn, reader, repo, run_id, cadence, target, at, dry_run, stale)


def _search(
    conn: sqlite3.Connection | None,
    reader: IssueReader,
    repo: str,
    run_id: str,
    cadence: str,
    target: date,
    at: datetime,
    dry_run: bool,
    stale: bool,
) -> Existing | Create:
    """4.3 の 2：ラベル付きの Issue から、識別子の target が一致するものを探す。"""
    for listed in reader.list_report_issues(repo, cadence):
        marker = listed.marker
        if marker is None or (marker.cadence, marker.target) != (cadence, target):
            continue
        ref = IssueRef(repo, listed.number)
        if not dry_run and conn is not None:
            # 本文を投稿した実行（識別子の run）がこの対象にあれば、その実行の記録として補う
            owner = marker.run_id if _is_run_of(conn, marker.run_id, cadence, target) else run_id
            _record(conn, run_id, owner, cadence, target, at, ref, "recreate")
            record_issue_repo(conn, repo)
        return Existing(ref, "labels")
    return Create("recreate" if stale else None)


def _is_run_of(conn: sqlite3.Connection, run_id: str, cadence: str, target: date) -> bool:
    row = conn.execute(
        "SELECT 1 FROM runs WHERE run_id = ? AND cadence = ? AND target = ?",
        (run_id, cadence, target.isoformat()),
    ).fetchone()
    return row is not None


def _record(
    conn: sqlite3.Connection,
    current_run: str,
    owner_run: str,
    cadence: str,
    target: date,
    at: datetime,
    ref: IssueRef,
    replaces: str,
) -> None:
    record_issue(
        conn,
        owner_run if replaces == "recreate" else current_run,
        "report",
        cadence,
        target,
        "created",
        at,
        repo=ref.repo,
        number=ref.number,
        replaces=replaces,
    )
