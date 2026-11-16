"""Issue と実行の対応：issues（data-model.md 3.4・3.7、US-E5-03・US-E4-03）。

作った Issue の番号と、Projects に登録する処理を済ませたかだけを持つ。GitHub 側の状態
（Open/Closed、Projects のステータス、コメント）は保存しない。
ドライランの実行 ID は受け付けない（D-22）。
"""

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from tech_researcher.db.runs import CADENCES, check_writable_run_id, utc_text
from tech_researcher.db.schema import transaction

KINDS = frozenset({"report", "rejected", "run_error", "run_error_secret"})
STATES = frozenset({"created", "blocked_secret", "failed"})

_COLUMNS = (
    "id, run_id, kind, cadence, target, state, repo, number, created_at, projects_state,"
    " projects_attempts, projects_last_error, projects_last_run_id"
)


@dataclass(frozen=True)
class IssueRow:
    """projects_state は、pending・done・issue_missing（created 以外は None）。"""

    id: int
    run_id: str
    kind: str
    cadence: str
    target: date
    state: str
    repo: str | None
    number: int | None
    created_at: str
    projects_state: str | None
    projects_attempts: int
    projects_last_error: str | None
    projects_last_run_id: str | None


def record_issue(
    conn: sqlite3.Connection,
    run_id: str,
    kind: str,
    cadence: str,
    target: date,
    state: str,
    at: datetime,
    *,
    repo: str | None = None,
    number: int | None = None,
) -> None:
    """（実行、種別）の Issue の記録を書く。同じ（実行、種別）の行があれば更新する。

    created は repo・number が要り、Projects の登録待ち（pending）で始める。それ以外は持たない。
    作った Issue（created）の行は、止めた・失敗した記録に戻さない（作った後の再試行で
    上書きしないため）。同じ番号を書き直したときは Projects の登録の状態を保つ。

    レポート Issue（report）の created は、同じ（cadence, target）の記録が別の実行にあれば、
    その行の番号を置き換える（転送先の番号、410・404 の後に作り直した番号。
    report-templates.md 4.3）。番号が変わったときは Projects の登録を pending に戻す。
    中でトランザクションを始めるので、`transaction` の中からは呼べない。
    """
    check_writable_run_id(run_id)
    if kind not in KINDS:
        raise ValueError(f"知らない Issue の種別 {kind}")
    if cadence not in CADENCES:
        raise ValueError(f"知らない cadence {cadence}")
    if state not in STATES:
        raise ValueError(f"知らない Issue の状態 {state}")
    if (state == "created") != (repo is not None and number is not None):
        raise ValueError("repo・number は created のときだけ、両方を付ける")
    if (repo is None) != (number is None):
        raise ValueError("repo と number は両方付けるか、両方付けない")
    day, created_at = target.isoformat(), utc_text(at)
    with transaction(conn):
        if kind == "report" and state == "created":
            cursor = conn.execute(
                "UPDATE issues SET repo = ?, number = ?, created_at = ?,"
                " projects_state = CASE WHEN repo IS ? AND number IS ? THEN projects_state"
                " ELSE 'pending' END WHERE kind = 'report' AND state = 'created'"
                " AND cadence = ? AND target = ? AND run_id != ?",
                (repo, number, created_at, repo, number, cadence, day, run_id),
            )
            if cursor.rowcount:
                return
        conn.execute(
            "INSERT INTO issues (run_id, kind, cadence, target, state, repo, number, created_at,"
            " projects_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (run_id, kind) DO UPDATE SET cadence = excluded.cadence,"
            " target = excluded.target, state = excluded.state, repo = excluded.repo,"
            " number = excluded.number, created_at = excluded.created_at,"
            " projects_state = CASE WHEN excluded.state != 'created' THEN NULL"
            " WHEN issues.state = 'created' AND issues.repo IS excluded.repo"
            " AND issues.number IS excluded.number THEN issues.projects_state"
            " ELSE 'pending' END"
            " WHERE NOT (issues.state = 'created' AND excluded.state != 'created')",
            (
                run_id,
                kind,
                cadence,
                day,
                state,
                repo,
                number,
                created_at,
                "pending" if state == "created" else None,
            ),
        )


def find_issue(conn: sqlite3.Connection, run_id: str, kind: str) -> IssueRow | None:
    """（実行、種別）の記録。不合格・実行エラー Issue は、番号があれば作らない（3.7）。"""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM issues WHERE run_id = ? AND kind = ?", (run_id, kind)
    ).fetchone()
    return None if row is None else _row(row)


def find_report_issue(conn: sqlite3.Connection, cadence: str, target: date) -> IssueRow | None:
    """同じ対象の作成済みレポート Issue の記録（重複判定の1。report-templates.md 4.3）。"""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM issues WHERE kind = 'report' AND state = 'created'"
        " AND cadence = ? AND target = ?",
        (cadence, target.isoformat()),
    ).fetchone()
    return None if row is None else _row(row)


def pending_projects(conn: sqlite3.Connection) -> list[IssueRow]:
    """Projects に未登録の Issue。種別・日次・週次を問わず、古い順（4.4）。"""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM issues WHERE state = 'created' AND projects_state = 'pending'"
        " ORDER BY id"
    )
    return [_row(r) for r in rows]


def mark_projects_done(conn: sqlite3.Connection, issue_id: int, run_id: str) -> None:
    _finish_projects(conn, issue_id, run_id, "done")


def mark_projects_issue_missing(conn: sqlite3.Connection, issue_id: int, run_id: str) -> None:
    """登録しようとした Issue が見つからない（410・404）。作り直さず、記録だけ残す。"""
    _finish_projects(conn, issue_id, run_id, "issue_missing")


def _finish_projects(conn: sqlite3.Connection, issue_id: int, run_id: str, state: str) -> None:
    check_writable_run_id(run_id)
    cursor = conn.execute(
        "UPDATE issues SET projects_state = ?, projects_attempts = projects_attempts + 1,"
        " projects_last_run_id = ? WHERE id = ? AND state = 'created'",
        (state, run_id, issue_id),
    )
    if cursor.rowcount != 1:
        raise LookupError(f"作成済みの Issue の記録がない: {issue_id}")


def record_projects_failure(
    conn: sqlite3.Connection, issue_id: int, run_id: str, error: str
) -> None:
    """登録の失敗を記録する。pending のまま、次の実行の ⑤ が再び登録する。

    error はエラーの分類などの短い文にする（認証情報を含めない。US-E6-06）。
    """
    check_writable_run_id(run_id)
    cursor = conn.execute(
        "UPDATE issues SET projects_attempts = projects_attempts + 1, projects_last_error = ?,"
        " projects_last_run_id = ? WHERE id = ? AND state = 'created'"
        " AND projects_state = 'pending'",
        (error, run_id, issue_id),
    )
    if cursor.rowcount != 1:
        raise LookupError(f"登録待ちの Issue の記録がない: {issue_id}")


def _row(r: tuple) -> IssueRow:
    return IssueRow(*r[:4], date.fromisoformat(r[4]), *r[5:])
