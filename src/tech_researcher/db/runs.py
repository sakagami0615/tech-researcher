"""実行の履歴：runs と source_fetches（data-model.md 3.4・3.7、US-E5-02・US-E6-05）。

runs は実行ごとに1行足し、同じ実行の中では同じ行を更新する。source_fetches は
（実行、情報源）ごとに1行で、同じ実行の中の再試行は同じ行を更新する。
ドライランの実行 ID は受け付けない（D-22。表の CHECK でも止める）。
ここの関数は1文ずつ書く（autocommit で、その文だけでコミットされる）。
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime

from tech_researcher.logging import CADENCES
from tech_researcher.logging import STAGES as LOG_STAGES
from tech_researcher.periods import Target
from tech_researcher.runfiles import DRY_RUN_PREFIX, OBSERVATION_PREFIX

# running は開始時に書く。observed は D-13 の観測（① だけの実行。実行 ID の先頭は obs-）
FINISHED_STATUSES = frozenset({"succeeded", "rejected", "failed", "observed"})
STAGES = LOG_STAGES - {"job"}  # failed_stage にする段階（D-04 の ① 〜 ⑤）
FETCH_STATUSES = frozenset({"ok", "no_results", "failed", "skipped"})


def utc_text(moment: datetime) -> str:
    """瞬間を保存する形（UTC の ISO 8601、秒まで。3.2）にする。"""
    if moment.tzinfo is None:
        raise ValueError("タイムゾーンのない日時は保存しない")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_writable_run_id(run_id: str) -> None:
    if run_id.startswith(DRY_RUN_PREFIX):
        raise ValueError(f"ドライランは DB に書かない: {run_id}")


def check_run_target(conn: sqlite3.Connection, run_id: str, cadence: str, target: date) -> None:
    """runs の行の cadence・target が、書こうとする値と一致することを確かめる。

    別の対象の実行に、レポートや Issue の記録を結び付けないため。トランザクションの中で呼ぶ。
    """
    row = conn.execute("SELECT cadence, target FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise LookupError(f"runs に開始の記録がない: {run_id}")
    if row != (cadence, target.isoformat()):
        raise ValueError(f"実行 {run_id} の対象 {row} と違う: {(cadence, target.isoformat())}")


def start_run(conn: sqlite3.Connection, run_id: str, target: Target, started_at: datetime) -> None:
    """実行の始まりを `running` で記録する。調査期間は、その実行で使った値を残す。"""
    check_writable_run_id(run_id)
    if target.cadence not in CADENCES:
        raise ValueError(f"知らない cadence {target.cadence}")
    conn.execute(
        "INSERT INTO runs (run_id, cadence, target, period_start, period_end, started_at, status)"
        " VALUES (?, ?, ?, ?, ?, ?, 'running')",
        (
            run_id,
            target.cadence,
            target.key.isoformat(),
            utc_text(target.period.start),
            utc_text(target.period.end),
            utc_text(started_at),
        ),
    )


def finish_run(
    conn: sqlite3.Connection,
    run_id: str,
    status: str,
    finished_at: datetime,
    *,
    failed_stage: str | None = None,
    error_category: str | None = None,
    retry_count: int | None = None,
) -> None:
    """実行の終わりを記録する。failed_stage は失敗（failed）のときだけ、必ず持たせる。

    同じ実行の中でやり直したときは、同じ行を上書きする。
    """
    check_writable_run_id(run_id)
    if status not in FINISHED_STATUSES:
        raise ValueError(f"終わりの状態にできない {status}")
    # 観測はレポートを作らないので、終わりは observed か failed だけ
    observation = run_id.startswith(OBSERVATION_PREFIX)
    if (observation and status not in ("observed", "failed")) or (
        not observation and status == "observed"
    ):
        raise ValueError(f"observed は観測（{OBSERVATION_PREFIX}）の実行だけが使う: {run_id}")
    if (status == "failed") != (failed_stage is not None):
        raise ValueError("failed_stage は failed のときだけ必要")
    if failed_stage is not None and failed_stage not in STAGES:
        raise ValueError(f"知らない段階 {failed_stage}")
    cursor = conn.execute(
        "UPDATE runs SET status = ?, finished_at = ?, failed_stage = ?, error_category = ?,"
        " retry_count = ? WHERE run_id = ?",
        (status, utc_text(finished_at), failed_stage, error_category, retry_count, run_id),
    )
    if cursor.rowcount != 1:
        raise LookupError(f"runs に開始の記録がない: {run_id}")


def record_model(conn: sqlite3.Connection, run_id: str, model: str) -> str | None:
    """② で使われたモデルの名前を記録し、前回の本番の実行で記録した名前を返す（structure.md 3.3）。

    前回は、この実行より前に始まった、model を持つ実行のうち最も新しいもの（日次・週次を問わない。
    どちらも Claude Code の既定のモデルを使うため）。なければ None。同じ実行の中のやり直しは
    同じ行を上書きする。
    """
    check_writable_run_id(run_id)
    if not model:
        raise ValueError("モデルの名前が空")
    previous = conn.execute(
        "SELECT model FROM runs WHERE model IS NOT NULL AND run_id != ?"
        " AND started_at <= (SELECT started_at FROM runs WHERE run_id = ?)"
        " ORDER BY started_at DESC, run_id DESC LIMIT 1",
        (run_id, run_id),
    ).fetchone()
    cursor = conn.execute("UPDATE runs SET model = ? WHERE run_id = ?", (model, run_id))
    if cursor.rowcount != 1:
        raise LookupError(f"runs に開始の記録がない: {run_id}")
    return None if previous is None else previous[0]


@dataclass(frozen=True)
class SourceFetch:
    """1回の実行での1情報源の取得結果。attempts は初回を含めた試行回数。"""

    source_id: str
    status: str
    attempts: int
    record_count: int
    in_period_count: int
    fetched_at: datetime
    error_category: str | None = None

    def __post_init__(self):
        if self.status not in FETCH_STATUSES:
            raise ValueError(f"知らない取得の状態 {self.status}")
        # skipped（取得しなかった）は理由を持っても持たなくてもよい
        if self.status == "failed" and self.error_category is None:
            raise ValueError("failed には error_category が要る")
        if self.status == "no_results" and self.error_category is not None:
            raise ValueError("no_results に error_category は持たせない")
        # ok が error_category を持つのは、一部だけ取れなかったとき（PARTIAL の印）
        if not 0 <= self.in_period_count <= self.record_count:
            raise ValueError("件数は 0 ≦ 期間内に残った件数 ≦ 取得件数 にする")
        if self.attempts < 0:
            raise ValueError("試行回数は 0 以上にする")


def record_source_fetch(conn: sqlite3.Connection, run_id: str, fetch: SourceFetch) -> None:
    """取得結果を書く。同じ（実行、情報源）の行があれば置き換える（3.7）。"""
    check_writable_run_id(run_id)
    conn.execute(
        "INSERT INTO source_fetches (run_id, source_id, status, error_category, attempts,"
        " record_count, in_period_count, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (run_id, source_id) DO UPDATE SET status = excluded.status,"
        " error_category = excluded.error_category, attempts = excluded.attempts,"
        " record_count = excluded.record_count, in_period_count = excluded.in_period_count,"
        " fetched_at = excluded.fetched_at",
        (
            run_id,
            fetch.source_id,
            fetch.status,
            fetch.error_category,
            fetch.attempts,
            fetch.record_count,
            fetch.in_period_count,
            utc_text(fetch.fetched_at),
        ),
    )


def fetch_incomplete(conn: sqlite3.Connection, source_id: str, target: date) -> bool:
    """対象日 target の日次の実行のうち最後の取得（skipped を除く）が、一部だけの成功か失敗か。

    状態が `failed`、または `ok` で error_category を持つ（一部だけ取れなかった）とき True。
    取得の記録がなければ False。再実行で、最初の実行が測り残した分を判定に入れるかの判断に使う
    （data-model.md 3.4）。
    """
    row = conn.execute(
        "SELECT f.status, f.error_category FROM source_fetches AS f"
        " JOIN runs AS r ON r.run_id = f.run_id"
        " WHERE r.cadence = 'daily' AND r.target = ? AND f.source_id = ? AND f.status != 'skipped'"
        " ORDER BY f.fetched_at DESC, f.rowid DESC LIMIT 1",
        (target.isoformat(), source_id),
    ).fetchone()
    return row is not None and (row[0] == "failed" or row[1] is not None)


@dataclass(frozen=True)
class RunHistory:
    """④ の入力の履歴の行の材料（data-model.md 4.4）。値は DB にあるコードの分類と数だけ。

    fetches は取得の状態ごとの情報源の数、failed_sources は失敗した（情報源 ID、分類）。
    rejected_checks は reports の不合格となった検査の名前（レポートを保存していなければ空）。
    """

    run_id: str
    cadence: str
    target: str
    started_at: datetime
    status: str
    failed_stage: str | None
    error_category: str | None
    retry_count: int | None
    rejected_checks: tuple[str, ...]
    fetches: dict[str, int]
    failed_sources: tuple[tuple[str, str | None], ...]


def recent_runs(conn: sqlite3.Connection, since: datetime, run_id: str) -> list[RunHistory]:
    """since 以降に始まった、run_id 以外の実行の履歴（古い順）。④ の入力の履歴に使う。"""
    rows = conn.execute(
        "SELECT r.run_id, r.cadence, r.target, r.started_at, r.status, r.failed_stage,"
        " r.error_category, r.retry_count, p.rejected_checks FROM runs AS r"
        " LEFT JOIN reports AS p ON p.run_id = r.run_id"
        " WHERE r.started_at >= ? AND r.run_id != ? ORDER BY r.started_at, r.run_id",
        (utc_text(since), run_id),
    ).fetchall()
    history = []
    for rid, cadence, target, started, status, stage, category, retries, checks in rows:
        fetches = conn.execute(
            "SELECT source_id, status, error_category FROM source_fetches WHERE run_id = ?"
            " ORDER BY source_id",
            (rid,),
        ).fetchall()
        counts: dict[str, int] = {}
        for _, fetch_status, _ in fetches:
            counts[fetch_status] = counts.get(fetch_status, 0) + 1
        history.append(
            RunHistory(
                run_id=rid,
                cadence=cadence,
                target=target,
                started_at=datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC),
                status=status,
                failed_stage=stage,
                error_category=category,
                retry_count=retries,
                rejected_checks=tuple(json.loads(checks)) if checks else (),
                fetches=counts,
                failed_sources=tuple((s, c) for s, st, c in fetches if st == "failed"),
            )
        )
    return history
