import fcntl
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tech_researcher import runfiles
from tech_researcher.runfiles import (
    RunFiles,
    data_dir,
    db_path,
    new_run_id,
    run_files,
)

PROD = "20261024-060000-abcd"
DRY = "dry-20261024-060000-abcd"
NOW = datetime(2026, 10, 23, 21, 0, 5, tzinfo=UTC)  # JST 2026-10-24 06:00:05


def test_data_dir_must_be_absolute_and_outside_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "data"
    assert data_dir({"TR_DATA_DIR": str(outside)}, repo) == outside
    with pytest.raises(ValueError, match="設定されていない"):
        data_dir({}, repo)
    with pytest.raises(ValueError, match="設定されていない"):
        data_dir({"TR_DATA_DIR": ""}, repo)
    with pytest.raises(ValueError, match="絶対パス"):
        data_dir({"TR_DATA_DIR": "data"}, repo)
    for inside in (repo, repo / "data", repo / ".." / "repo" / "x"):
        with pytest.raises(ValueError, match="リポジトリの外"):
            data_dir({"TR_DATA_DIR": str(inside)}, repo)


def test_data_dir_symlink_into_repo_is_rejected(tmp_path):
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(repo / "data")
    with pytest.raises(ValueError, match="リポジトリの外"):
        data_dir({"TR_DATA_DIR": str(link)}, repo)


def test_new_run_id():
    assert new_run_id(NOW, dry_run=False, token=lambda: "a1b2") == "20261024-060005-a1b2"
    assert new_run_id(NOW, dry_run=True, token=lambda: "a1b2") == "dry-20261024-060005-a1b2"
    assert RunFiles(Path("/data"), new_run_id(NOW, dry_run=True)).dry_run
    assert not RunFiles(Path("/data"), new_run_id(NOW, dry_run=False)).dry_run
    with pytest.raises(ValueError, match="タイムゾーン"):
        new_run_id(datetime(2026, 10, 24, 6), dry_run=False)
    with pytest.raises(ValueError, match="実行 ID"):
        new_run_id(NOW, dry_run=False, token=lambda: "../x")


@pytest.mark.parametrize(
    "run_id",
    ["", "20261024-060000", "obs-20261024-060000-abcd", "20261024-060000-ABCD", "../x", "dry-"],
)
def test_rejects_malformed_run_id(tmp_path, run_id):
    with pytest.raises(ValueError, match="実行 ID"):
        RunFiles(tmp_path, run_id)


def test_paths(tmp_path):
    prod = RunFiles(tmp_path, PROD)
    assert not prod.dry_run
    assert prod.dir == tmp_path / "work" / PROD
    assert prod.log == tmp_path / "logs" / "2026" / "10" / f"{PROD}.jsonl"
    assert prod.db == db_path(tmp_path) == tmp_path / "db" / "tech-researcher.sqlite3"
    dry = RunFiles(tmp_path, DRY)
    assert dry.dry_run
    assert dry.dir == tmp_path / "dry-run" / DRY
    assert dry.log == dry.dir / "log.jsonl"
    assert dry.db == prod.db  # ドライランも本番の DB を（読み取り専用で）読む


def test_production_files_are_removed_on_success(tmp_path):
    with run_files(tmp_path, PROD) as files:
        (files.dir / "collected.json").write_text("{}")
        assert files.dir.is_dir()
    assert not files.dir.exists()


def test_production_files_are_removed_on_failure(tmp_path):
    with pytest.raises(RuntimeError, match="失敗"):
        with run_files(tmp_path, PROD) as files:
            (files.dir / "collected.json").write_text("{}")
            raise RuntimeError("失敗")
    assert not files.dir.exists()


def test_production_files_are_removed_on_interrupt(tmp_path):
    with pytest.raises(KeyboardInterrupt):
        with run_files(tmp_path, PROD) as files:
            raise KeyboardInterrupt
    assert not files.dir.exists()


def test_leftovers_of_stopped_runs_are_removed_at_start(tmp_path):
    work = tmp_path / "work"
    stale = work / "20261023-060000-0001"
    (stale / "sub").mkdir(parents=True)
    (stale / "collected.json").write_text("本文")
    (stale / ".lock").write_text("")  # ロックのファイルだけが残った（持ち主のプロセスはいない）
    no_lock = work / "20261022-060000-0002"
    no_lock.mkdir()
    with run_files(tmp_path, PROD) as files:
        assert sorted(p.name for p in work.iterdir() if p.is_dir()) == [PROD]
        assert files.dir.is_dir()


def test_running_run_is_not_removed(tmp_path):
    other = "20261024-070000-0003"
    with run_files(tmp_path, other) as running:
        (running.dir / "collected.json").write_text("{}")
        with run_files(tmp_path, PROD):
            assert (running.dir / "collected.json").exists()
        assert (running.dir / "collected.json").exists()
    assert not running.dir.exists()


def test_lock_held_by_another_process_is_respected(tmp_path):
    """別のプロセスがロックを持つフォルダは消さない。"""
    running = tmp_path / "work" / "20261024-070000-0004"
    running.mkdir(parents=True)
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK, str(running / ".lock")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    try:
        assert holder.stdout.readline() == b"locked\n"
        with run_files(tmp_path, PROD):
            assert running.is_dir()
    finally:
        holder.communicate(b"")
    with run_files(tmp_path, PROD):  # 持ち主が終われば消す
        assert not running.exists()


_HOLD_LOCK = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)
fcntl.flock(fd, fcntl.LOCK_EX)
print("locked", flush=True)
sys.stdin.read()
"""


def test_same_run_id_is_an_error(tmp_path):
    with run_files(tmp_path, PROD) as files:
        (files.dir / "collected.json").write_text("{}")
        with pytest.raises(FileExistsError):
            with run_files(tmp_path, PROD):
                pass
        assert (
            files.dir / "collected.json"
        ).exists()  # 失敗した側は、ほかの実行のフォルダを消さない


def test_failure_before_yield_removes_the_folder(tmp_path, monkeypatch):
    """フォルダを作った後、渡す前に失敗しても残さない。"""
    open_lock = runfiles._open_lock

    def failing(path):
        if path.parent.name == PROD:
            raise OSError("ロックを開けない")
        return open_lock(path)

    monkeypatch.setattr(runfiles, "_open_lock", failing)
    with pytest.raises(OSError, match="ロックを開けない"):
        with run_files(tmp_path, PROD):
            pass
    assert not (tmp_path / "work" / PROD).exists()


def test_removal_at_end_waits_for_other_starts(tmp_path):
    """終わりの削除も work/.lock を持って行い、ほかの実行の開始時の片付けと重ねない。"""
    context = run_files(tmp_path, PROD)
    files = context.__enter__()
    fd = os.open(tmp_path / "work" / ".lock", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)  # ほかの実行が開始の途中である状態
    ending = threading.Thread(target=context.__exit__, args=(None, None, None))
    ending.start()
    ending.join(0.2)
    assert ending.is_alive()
    assert files.dir.is_dir()
    os.close(fd)
    ending.join()
    assert not files.dir.exists()


def test_symlink_in_work_is_not_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("")
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "link").symlink_to(outside)
    with run_files(tmp_path, PROD):
        pass
    assert (outside / "keep").exists()


def test_dry_run_files_are_kept(tmp_path):
    with run_files(tmp_path, DRY) as files:
        (files.dir / "issue.md").write_text("# 本文")
    assert (files.dir / "issue.md").read_text() == "# 本文"
    with pytest.raises(RuntimeError):
        with run_files(tmp_path, "dry-20261024-070000-ffff") as failed:
            (failed.dir / "log.jsonl").write_text("{}")
            raise RuntimeError
    assert (failed.dir / "log.jsonl").exists()


def test_dry_run_does_not_touch_work(tmp_path):
    stale = tmp_path / "work" / "20261023-060000-0001"
    stale.mkdir(parents=True)
    with run_files(tmp_path, DRY):
        pass
    assert stale.is_dir()
    assert sorted(p.name for p in Path(tmp_path).iterdir()) == ["dry-run", "work"]
