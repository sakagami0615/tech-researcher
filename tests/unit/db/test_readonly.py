import sqlite3
from datetime import UTC, date, datetime
from itertools import islice

import pytest

from tech_researcher.db.readonly import open_readonly, tentative_ids
from tech_researcher.db.runs import start_run
from tech_researcher.db.schema import SCHEMA_VERSION, SchemaError, open_db
from tech_researcher.periods import daily_target


def make_db(path):
    conn = open_db(path)
    start_run(
        conn,
        "20261024-060000-abcd",
        daily_target(date(2026, 10, 24), 1),
        datetime(2026, 10, 23, 21, tzinfo=UTC),
    )
    conn.close()


def test_missing_db_is_not_created(tmp_path):
    path = tmp_path / "db" / "tech-researcher.sqlite3"
    assert open_readonly(path) is None
    assert not path.exists()
    assert not path.parent.exists()


def test_reads_existing_db(tmp_path):
    path = tmp_path / "tech-researcher.sqlite3"
    make_db(path)
    conn = open_readonly(path)
    assert conn is not None
    with conn:
        assert conn.execute("SELECT run_id FROM runs").fetchall() == [("20261024-060000-abcd",)]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO runs VALUES ('dry-20261024-060000-abcd')",
        "UPDATE runs SET run_id = 'x'",
        "DELETE FROM runs",
        "CREATE TABLE other (x)",
        "PRAGMA user_version = 3",
    ],
)
def test_rejects_writes(tmp_path, sql):
    path = tmp_path / "tech-researcher.sqlite3"
    make_db(path)
    before = path.read_bytes()
    conn = open_readonly(path)
    assert conn is not None
    with pytest.raises(sqlite3.OperationalError):
        conn.execute(sql)
        conn.commit()
    conn.close()
    assert path.read_bytes() == before


def test_mode_ro_rejects_writes_without_query_only(tmp_path):
    """query_only を外されても、mode=ro なので書けない。"""
    path = tmp_path / "tech-researcher.sqlite3"
    make_db(path)
    conn = open_readonly(path)
    assert conn is not None
    conn.execute("PRAGMA query_only = OFF")
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM runs")
    conn.close()


def test_path_with_uri_characters(tmp_path):
    """? や # を含むパスでも、別のファイルを作らずに同じファイルを開く。"""
    directory = tmp_path / "a b?mode=rwc#x%20"
    directory.mkdir()
    path = directory / "tech-researcher.sqlite3"
    assert open_readonly(path) is None
    make_db(path)
    conn = open_readonly(path)
    assert conn is not None
    assert conn.execute("SELECT count(*) FROM runs").fetchone() == (1,)
    conn.close()
    # 開いた DB の WAL のファイル（-wal・-shm）のほかに、別のファイルを作っていない
    names = sorted(p.name for p in tmp_path.rglob("*"))
    assert names == ["a b?mode=rwc#x%20", path.name, f"{path.name}-shm", f"{path.name}-wal"]


def test_directory_is_not_a_db(tmp_path):
    assert open_readonly(tmp_path) is None


def test_tentative_ids():
    assert list(islice(tentative_ids(), 3)) == ["d1", "d2", "d3"]
    assert next(tentative_ids()) == "d1"  # 実行ごとに1から


def files(directory):
    """DB と WAL の中身。-shm は読み手も印（読んでいる位置）を書く共有メモリなので比べない。"""
    return {p.name: p.read_bytes() for p in directory.iterdir() if not p.name.endswith("-shm")}


def test_reads_wal_db_while_writer_is_open(tmp_path):
    """本番の実行が DB を開いている間（WAL に未反映の書き込みがある）でも、読み取り専用で
    コミット済みの内容を読め、DB にも WAL にも何も書かない（D-22、US-E8-04）。"""
    path = tmp_path / "db" / "tech-researcher.sqlite3"
    make_db(path)
    writer = open_db(path)
    start_run(
        writer,
        "20261025-060000-0123",
        daily_target(date(2026, 10, 25), 1),
        datetime(2026, 10, 24, 21, tzinfo=UTC),
    )
    wal = path.with_name(path.name + "-wal")
    assert wal.stat().st_size > 0  # コミット済みの行が、まだ WAL にだけある
    before = files(path.parent)

    # 本番がトランザクションの途中でも読める。未コミットの行は見えない
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("DELETE FROM runs WHERE run_id = '20261024-060000-abcd'")
    conn = open_readonly(path)
    assert conn is not None
    assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    assert conn.execute("SELECT run_id FROM runs ORDER BY run_id").fetchall() == [
        ("20261024-060000-abcd",),
        ("20261025-060000-0123",),
    ]
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("INSERT INTO runs (run_id) VALUES ('x')")
    conn.execute("PRAGMA query_only = OFF")
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("UPDATE runs SET status = 'failed'")
    conn.close()
    writer.execute("ROLLBACK")
    assert files(path.parent) == before

    # 読み取りの後も、本番はそのまま書ける
    writer.execute("UPDATE runs SET status = 'failed', failed_stage = 'collect'")
    writer.close()
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


def test_reads_wal_db_without_wal_files(tmp_path):
    """本番が閉じた後（-wal・-shm がない）でも読める。SQLite が空の -wal・-shm を作るが、
    DB の中身は変わらず、次に本番が開いたときにそのまま使われる。"""
    path = tmp_path / "tech-researcher.sqlite3"
    make_db(path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]
    before = path.read_bytes()
    conn = open_readonly(path)
    assert conn is not None
    assert conn.execute("SELECT count(*) FROM runs").fetchone() == (1,)
    conn.close()
    assert path.read_bytes() == before
    assert path.with_name(path.name + "-wal").stat().st_size == 0
    writer = open_db(path)
    writer.execute("DELETE FROM runs")
    writer.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]


@pytest.mark.parametrize(("version", "message"), [(0, "古い"), (SCHEMA_VERSION + 1, "新しい")])
def test_rejects_other_schema_version(tmp_path, version, message):
    """読み取り専用では版を上げられないので、版がコードと違えば使わない。"""
    path = tmp_path / "tech-researcher.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {version}")
    conn.close()
    with pytest.raises(SchemaError, match=message):
        open_readonly(path)
