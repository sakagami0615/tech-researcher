"""レポート Issue の重複判定（report-templates.md 4.3、US-E4-03・US-E5-03）。

⑤ が Issue を作る直前に呼ぶ。作る処理そのものは呼び出し側。判定の結果は2つ。
- Existing：同じ対象の Issue がある。作らない（Projects に未登録なら登録だけを行う）
- Create：ない。作る。replaces は、DB に古い記録（410・404・別のリポジトリ）があるとき
  "recreate"（作った番号を record_issue に渡して、記録を付け替える）

DB の更新（転送先の番号、ラベルから見つけた Issue の補完）は、ドライランでは行わない。
GitHub を読めないとき（4.3 の「それ以外」）は IssueReadError のまま呼び出し側へ出す。
"""

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from tech_researcher.db.issues import find_report_issue, record_issue
from tech_researcher.publish.github_issues import IssueReader, IssueRef


@dataclass(frozen=True)
class Existing:
    """作らない。via は見つけ方：db（DB の番号）・transferred（転送先）・labels（ラベルの探索）。"""

    ref: IssueRef
    via: str


@dataclass(frozen=True)
class Create:
    replaces: str | None = None


def check_duplicate(
    conn: sqlite3.Connection,
    reader: IssueReader,
    repo: str,
    run_id: str,
    cadence: str,
    target: date,
    at: datetime,
    *,
    dry_run: bool = False,
) -> Existing | Create:
    """repo は投稿先（小文字の `owner/repo`）。DB の記録が別のリポジトリのものなら使わない。"""
    stale = False
    row = find_report_issue(conn, cadence, target)
    if row is not None:
        if (row.repo or "").lower() == repo and row.number is not None:
            found = reader.get_issue(repo, row.number)
            if found.ref is not None and not found.transferred:
                return Existing(found.ref, "db")
            if found.ref is not None:
                if not dry_run:
                    _record(conn, run_id, run_id, cadence, target, at, found.ref, "transfer")
                return Existing(found.ref, "transferred")
        stale = True  # 410・404、または別のリポジトリの記録。2 で確かめ直す
    for listed in reader.list_report_issues(repo, cadence):
        marker = listed.marker
        if marker is None or (marker.cadence, marker.target) != (cadence, target):
            continue
        ref = IssueRef(repo, listed.number)
        if not dry_run:
            # 本文を投稿した実行（識別子の run）がこの対象にあれば、その実行の記録として補う
            owner = marker.run_id if _is_run_of(conn, marker.run_id, cadence, target) else run_id
            _record(conn, run_id, owner, cadence, target, at, ref, "recreate")
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
