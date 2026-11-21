import sqlite3
import threading
import time
from datetime import UTC, date, datetime

import pytest

from tech_researcher.db import schema
from tech_researcher.db.history import shown_urls
from tech_researcher.db.issues import record_issue
from tech_researcher.db.reports import EntryRow, ReportRow, save_report
from tech_researcher.db.runs import start_run
from tech_researcher.db.schema import (
    BUSY_TIMEOUT_MS,
    SCHEMA_VERSION,
    SchemaError,
    open_db,
    transaction,
)
from tech_researcher.periods import daily_target


def pragma(conn, name):
    return conn.execute(f"PRAGMA {name}").fetchone()[0]


def test_creates_db_with_settings(tmp_path):
    path = tmp_path / "db" / "tech-researcher.sqlite3"
    conn = open_db(path)
    assert path.is_file()
    assert pragma(conn, "user_version") == SCHEMA_VERSION
    assert pragma(conn, "journal_mode") == "wal"
    assert pragma(conn, "foreign_keys") == 1
    assert pragma(conn, "busy_timeout") == BUSY_TIMEOUT_MS
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")}
    assert tables == {
        "runs",
        "source_fetches",
        "items",
        "feed_snapshots",
        "star_snapshots",
        "reports",
        "report_entries",
        "meta",
        "issues",
    }
    conn.close()


def test_reopen_keeps_version_and_data(tmp_path):
    path = tmp_path / "x.sqlite3"
    conn = open_db(path)
    conn.execute(
        "INSERT INTO runs (run_id, cadence, target, period_start, period_end, started_at, status)"
        " VALUES ('20261024-060000-abcd', 'daily', '2026-10-23', 'a', 'b', 'c', 'running')"
    )
    conn.close()
    conn = open_db(path)
    assert pragma(conn, "user_version") == SCHEMA_VERSION
    assert conn.execute("SELECT count(*) FROM runs").fetchone() == (1,)
    conn.close()


def test_upgrades_first_version_with_feed_snapshots(tmp_path, monkeypatch):
    """版1の DB（runs・source_fetches・items だけ）に、版2で feed_snapshots を足す。"""
    path = tmp_path / "x.sqlite3"
    with monkeypatch.context() as m:
        m.setattr(schema, "MIGRATIONS", schema.MIGRATIONS[:1])
        m.setattr(schema, "SCHEMA_VERSION", 1)
        conn = open_db(path)
        conn.execute(
            "INSERT INTO runs (run_id, cadence, target, period_start, period_end, started_at,"
            " status) VALUES ('20261024-060000-abcd', 'daily', '2026-10-23', 'a', 'b', 'c',"
            " 'running')"
        )
        conn.close()
    conn = open_db(path)
    assert pragma(conn, "user_version") == SCHEMA_VERSION
    assert [r[1] for r in conn.execute("PRAGMA table_info(feed_snapshots)")] == [
        "source_id",
        "target",
        "canonical_url",
        "rank",
        "attention_json",
        "fetched_at",
    ]
    assert conn.execute("SELECT count(*) FROM runs").fetchone() == (1,)
    conn.close()


def test_upgrades_version_3_db_with_report_tables(tmp_path, monkeypatch):
    """版3の DB（star_snapshots まで）に、版4で reports・report_entries・issues を足す。

    既存の runs・items の行は残り、新しい表に書ける。
    """
    path = tmp_path / "x.sqlite3"
    run_id = "20261024-060000-abcd"
    with monkeypatch.context() as m:
        m.setattr(schema, "MIGRATIONS", schema.MIGRATIONS[:3])
        m.setattr(schema, "SCHEMA_VERSION", 3)
        conn = open_db(path)
        start_run(
            conn, run_id, daily_target(date(2026, 10, 24), 1), datetime(2026, 10, 23, tzinfo=UTC)
        )
        conn.execute(
            "INSERT INTO items (cadence, target, canonical_url, kind, title, published_precision,"
            " inclusion_event, inclusion_at, events_json, attention_json, sources_json,"
            " links_json, meta_terms_json, first_run_id, last_run_id) VALUES ('daily',"
            " '2026-10-23', 'https://e.com/a', 'article', 't', 'unknown', 'posted', 'a', '[]',"
            " '[]', '[]', '[]', '[]', ?, ?)",
            (run_id, run_id),
        )
        conn.close()
    conn = open_db(path)
    assert pragma(conn, "user_version") == SCHEMA_VERSION
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")}
    assert {"reports", "report_entries", "issues"} <= names
    assert conn.execute("SELECT count(*) FROM runs").fetchone() == (1,)
    (item_id,) = conn.execute("SELECT item_id FROM items").fetchone()
    entry = EntryRow("daily_item", "published", (f"i{item_id}",), display_no=1)
    report = ReportRow("daily", date(2026, 10, 23), "passed", {}, {}, [entry], body_markdown="本文")
    save_report(conn, run_id, report)
    record_issue(
        conn,
        run_id,
        "report",
        "daily",
        date(2026, 10, 23),
        "created",
        datetime(2026, 10, 24, tzinfo=UTC),
        repo="o/r",
        number=1,
    )
    assert shown_urls(conn, date(2026, 10, 23)) == {"https://e.com/a"}
    conn.close()


def test_rejects_newer_db(tmp_path):
    path = tmp_path / "x.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(SchemaError, match="新しい"):
        open_db(path)


def test_failed_migration_is_rolled_back(tmp_path, monkeypatch):
    """途中で失敗した版は当たらず、前の版のまま残る。"""
    path = tmp_path / "x.sqlite3"
    open_db(path).close()
    broken = "CREATE TABLE added (x INTEGER) STRICT; INSERT INTO added VALUES ('not a number');"
    monkeypatch.setattr(schema, "MIGRATIONS", (*schema.MIGRATIONS, broken))
    monkeypatch.setattr(schema, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
    with pytest.raises(sqlite3.IntegrityError):
        open_db(path)
    conn = sqlite3.connect(path)
    assert pragma(conn, "user_version") == SCHEMA_VERSION
    assert conn.execute("SELECT name FROM sqlite_schema WHERE name = 'added'").fetchone() is None
    conn.close()


def test_pending_versions_are_applied_together(tmp_path, monkeypatch):
    """未適用の版はまとめて当てる。後の版が失敗したら、前の版も当たらない。"""
    path = tmp_path / "x.sqlite3"
    open_db(path).close()
    added = ("CREATE TABLE a (x INTEGER) STRICT;", "INSERT INTO a VALUES ('not a number');")
    monkeypatch.setattr(schema, "MIGRATIONS", (*schema.MIGRATIONS, *added))
    monkeypatch.setattr(schema, "SCHEMA_VERSION", SCHEMA_VERSION + 2)
    with pytest.raises(sqlite3.IntegrityError):
        open_db(path)
    conn = sqlite3.connect(path)
    assert pragma(conn, "user_version") == SCHEMA_VERSION
    assert conn.execute("SELECT name FROM sqlite_schema WHERE name = 'a'").fetchone() is None
    conn.close()


def test_applies_new_versions_in_order(tmp_path, monkeypatch):
    path = tmp_path / "x.sqlite3"
    open_db(path).close()
    added = ("CREATE TABLE a (x INTEGER) STRICT;", "ALTER TABLE a ADD COLUMN y TEXT;")
    monkeypatch.setattr(schema, "MIGRATIONS", (*schema.MIGRATIONS, *added))
    monkeypatch.setattr(schema, "SCHEMA_VERSION", SCHEMA_VERSION + 2)
    conn = open_db(path)
    assert pragma(conn, "user_version") == SCHEMA_VERSION + 2
    assert [r[1] for r in conn.execute("PRAGMA table_info(a)")] == ["x", "y"]
    conn.close()


def test_strict_types_and_foreign_keys(tmp_path):
    conn = open_db(tmp_path / "x.sqlite3")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(
            "INSERT INTO source_fetches VALUES ('20261024-060000-abcd', 'rss:a', 'ok', NULL,"
            " 1, 0, 0, '2026-10-23T21:00:00Z', NULL)"
        )
    with pytest.raises(sqlite3.IntegrityError, match="cannot store"):
        conn.execute(
            "INSERT INTO runs (run_id, cadence, target, period_start, period_end, started_at,"
            " status, retry_count) VALUES ('r', 'daily', 't', 'a', 'b', 'c', 'running', 'x')"
        )
    conn.close()


def test_runs_rejects_dry_run_ids_in_sql(tmp_path):
    """コードの確かめを通らずに書いても、ドライランの実行 ID は表が拒む（D-22）。"""
    conn = open_db(tmp_path / "x.sqlite3")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO runs (run_id, cadence, target, period_start, period_end, started_at,"
            " status) VALUES ('dry-20261024-060000-abcd', 'daily', 't', 'a', 'b', 'c', 'running')"
        )
    conn.close()


def test_transaction_rolls_back_on_error(tmp_path):
    conn = open_db(tmp_path / "x.sqlite3")
    with pytest.raises(RuntimeError), transaction(conn):
        conn.execute(
            "INSERT INTO runs (run_id, cadence, target, period_start, period_end, started_at,"
            " status) VALUES ('r', 'daily', 't', 'a', 'b', 'c', 'running')"
        )
        raise RuntimeError
    assert conn.execute("SELECT count(*) FROM runs").fetchone() == (0,)
    assert not conn.in_transaction
    conn.close()


def test_waits_for_other_writer(tmp_path):
    """ほかの接続が書いている間は busy_timeout まで待ち、終われば書ける（月曜の重なり。3.2）。"""
    path = tmp_path / "x.sqlite3"
    open_db(path).close()
    holder = sqlite3.connect(path, autocommit=True, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    timer = threading.Timer(0.3, lambda: holder.execute("COMMIT"))
    started = time.monotonic()
    timer.start()
    conn = open_db(path)  # 版の確かめでも書き込みのロックを取るので、ここで待つ
    assert time.monotonic() - started >= 0.25
    timer.join()
    with transaction(conn):
        conn.execute(
            "INSERT INTO runs (run_id, cadence, target, period_start, period_end, started_at,"
            " status) VALUES ('r', 'daily', 't', 'a', 'b', 'c', 'running')"
        )
    conn.close()
    holder.close()


def test_gives_up_after_busy_timeout(tmp_path):
    path = tmp_path / "x.sqlite3"
    open_db(path).close()
    holder = sqlite3.connect(path, autocommit=True)
    holder.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        open_db(path, busy_timeout_ms=200)
    assert time.monotonic() - started >= 0.2
    holder.execute("COMMIT")
    holder.close()


def test_upgrades_version_5_db_with_failed_parts_column(tmp_path, monkeypatch):
    """版5の DB の source_fetches の行は、版6で足す failed_parts_json が NULL のまま残る。"""
    path = tmp_path / "x.sqlite3"
    run_id = "20261024-060000-abcd"
    with monkeypatch.context() as m:
        m.setattr(schema, "MIGRATIONS", schema.MIGRATIONS[:5])
        m.setattr(schema, "SCHEMA_VERSION", 5)
        conn = open_db(path)
        start_run(
            conn, run_id, daily_target(date(2026, 10, 24), 1), datetime(2026, 10, 23, tzinfo=UTC)
        )
        conn.execute(
            "INSERT INTO source_fetches VALUES (?, 'rss:a', 'ok', NULL, 1, 2, 1,"
            " '2026-10-23T21:00:00Z')",
            (run_id,),
        )
        conn.close()
    conn = open_db(path)
    assert pragma(conn, "user_version") == SCHEMA_VERSION == 6
    assert conn.execute("SELECT status, failed_parts_json FROM source_fetches").fetchall() == [
        ("ok", None)
    ]
    conn.execute('UPDATE source_fetches SET failed_parts_json = \'[["a/b", "timeout"]]\'')
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE source_fetches SET failed_parts_json = '{}'")
    conn.close()
