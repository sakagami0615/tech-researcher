"""実行の履歴：runs と source_fetches（data-model.md 3.4・3.7、US-E5-02・US-E6-05）。

runs は実行ごとに1行足し、同じ実行の中では同じ行を更新する。source_fetches は
（実行、情報源）ごとに1行で、同じ実行の中の再試行は同じ行を更新する。
ドライランの実行 ID は受け付けない（D-22。表の CHECK でも止める）。
ここの関数は1文ずつ書く（autocommit で、その文だけでコミットされる）。
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from tech_researcher.logging import CADENCES
from tech_researcher.logging import STAGES as LOG_STAGES
from tech_researcher.periods import Target
from tech_researcher.runfiles import DRY_RUN_PREFIX

# running は開始時に書く。observed は D-13 の観測（① だけの実行。実行 ID の先頭は obs-）
FINISHED_STATUSES = frozenset({"succeeded", "rejected", "failed", "observed"})
STAGES = LOG_STAGES - {"job"}  # failed_stage にする段階（D-04 の ① 〜 ⑤）
FETCH_STATUSES = frozenset({"ok", "no_results", "failed", "skipped"})
OBSERVATION_PREFIX = "obs-"


def utc_text(moment: datetime) -> str:
    """瞬間を保存する形（UTC の ISO 8601、秒まで。3.2）にする。"""
    if moment.tzinfo is None:
        raise ValueError("タイムゾーンのない日時は保存しない")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_writable_run_id(run_id: str) -> None:
    if run_id.startswith(DRY_RUN_PREFIX):
        raise ValueError(f"ドライランは DB に書かない: {run_id}")


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
        if self.status in ("ok", "no_results") and self.error_category is not None:
            raise ValueError(f"{self.status} に error_category は持たせない")
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
