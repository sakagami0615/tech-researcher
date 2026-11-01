import sqlite3
from itertools import islice

import pytest

from tech_researcher.db.readonly import open_readonly, tentative_ids


def make_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO runs VALUES ('20261024-060000-abcd')")
    conn.commit()
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
    assert sorted(p.name for p in tmp_path.rglob("*")) == ["a b?mode=rwc#x%20", path.name]


def test_directory_is_not_a_db(tmp_path):
    assert open_readonly(tmp_path) is None


def test_tentative_ids():
    assert list(islice(tentative_ids(), 3)) == ["d1", "d2", "d3"]
    assert next(tentative_ids()) == "d1"  # 実行ごとに1から
