"""Issue と実行の対応：issues（data-model.md 3.4・3.7、US-E5-03・US-E4-03）。

作った Issue の番号と、Projects に登録する処理を済ませたかだけを持つ。GitHub 側の状態
（Open/Closed、Projects のステータス、コメント）は保存しない。
ドライランの実行 ID は受け付けない（D-22）。
"""

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from tech_researcher.db.runs import CADENCES, check_run_target, check_writable_run_id, utc_text
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
    replaces: str | None = None,
) -> None:
    """（実行、種別）の Issue の記録を書く。同じ（実行、種別）の行があれば更新する。

    created は repo・number が要り、Projects の登録待ち（pending）で始める。それ以外は持たない。
    作った Issue（created）の行は、止めた・失敗した記録に戻さない（作った後の再試行で
    上書きしないため）。同じ番号を書き直したときは、作成時刻と Projects の登録の状態を保つ。
    番号が変わったときは、Projects の登録を pending に戻し、試行回数・失敗の記録も消す。

    レポート Issue（report）の created で、同じ（cadence, target）の作成済みの記録が別の実行に
    あるときは、replaces で扱いを指定する（なければ一意制約のエラー。report-templates.md 4.3）。
    - "transfer"：転送先の番号（301）。行の run_id は掲載した実行のまま、番号だけ置き換える
    - "recreate"：410・404 の後に、この実行で作り直した番号。行の run_id をこの実行に付け替える
      （「載せた」の導出が、本文を投稿した実行の項目を指すようにするため）

    runs の行の cadence・target と一致しなければ書かない。
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
    if replaces not in (None, "transfer", "recreate"):
        raise ValueError(f"知らない replaces {replaces}")
    if replaces and (kind != "report" or state != "created"):
        raise ValueError("replaces は作成済みのレポート Issue だけに使う")
    day, created_at = target.isoformat(), utc_text(at)
    with transaction(conn):
        check_run_target(conn, run_id, cadence, target)
        if replaces and _replace(conn, run_id, cadence, day, repo, number, created_at, replaces):
            return
        conn.execute(
            "INSERT INTO issues (run_id, kind, cadence, target, state, repo, number, created_at,"
            " projects_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (run_id, kind) DO UPDATE SET cadence = excluded.cadence,"
            " target = excluded.target, state = excluded.state, repo = excluded.repo,"
            " number = excluded.number,"
            f" created_at = CASE WHEN {_SAME} THEN issues.created_at ELSE excluded.created_at END,"
            f" projects_state = CASE WHEN excluded.state != 'created' THEN NULL"
            f" WHEN {_SAME} THEN issues.projects_state ELSE 'pending' END,"
            f" projects_attempts = CASE WHEN {_SAME} THEN issues.projects_attempts ELSE 0 END,"
            f" projects_last_error = CASE WHEN {_SAME} THEN issues.projects_last_error"
            " ELSE NULL END,"
            f" projects_last_run_id = CASE WHEN {_SAME} THEN issues.projects_last_run_id"
            " ELSE NULL END"
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


# upsert で、書き直す番号が今の行と同じか（作成済みの行に同じ番号を書き直すとき）
_SAME = (
    "(issues.state = 'created' AND excluded.state = 'created'"
    " AND issues.repo IS excluded.repo AND issues.number IS excluded.number)"
)


def _replace(
    conn: sqlite3.Connection,
    run_id: str,
    cadence: str,
    day: str,
    repo: str | None,
    number: int | None,
    created_at: str,
    replaces: str,
) -> bool:
    """別の実行にある、同じ対象の作成済みレポート Issue の行を置き換える。なければ False。"""
    old = conn.execute(
        "SELECT id, run_id, repo, number FROM issues WHERE kind = 'report' AND state = 'created'"
        " AND cadence = ? AND target = ? AND run_id != ?",
        (cadence, day, run_id),
    ).fetchone()
    if old is None:
        return False
    old_id, old_run, old_repo, old_number = old
    new_run = run_id if replaces == "recreate" else old_run
    if new_run != old_run:
        # この実行の止めた・失敗した記録は、付け替える行に置き換える
        conn.execute(
            "DELETE FROM issues WHERE run_id = ? AND kind = 'report' AND state != 'created'",
            (run_id,),
        )
    if (old_repo, old_number) == (repo, number):
        conn.execute("UPDATE issues SET run_id = ? WHERE id = ?", (new_run, old_id))
    else:
        conn.execute(
            "UPDATE issues SET run_id = ?, repo = ?, number = ?, created_at = ?,"
            " projects_state = 'pending', projects_attempts = 0, projects_last_error = NULL,"
            " projects_last_run_id = NULL WHERE id = ?",
            (new_run, repo, number, created_at, old_id),
        )
    return True


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
        " projects_last_run_id = ? WHERE id = ? AND state = 'created'"
        " AND projects_state = 'pending'",
        (state, run_id, issue_id),
    )
    if cursor.rowcount != 1:
        raise LookupError(f"登録待ちの Issue の記録がない: {issue_id}")


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
