"""実行 ID と実行データの置き場所（structure.md 7.2・7.4、D-22）。

DB・ログ・実行中のファイル・ドライランの出力は、リポジトリの外の `${TR_DATA_DIR}` に置く。
本番の実行中のファイル（一次情報の本文を含む）は成否を問わず実行の終わりに消し、途中で
止まった実行の残りは次の本番の実行の始めに消す（D-14・D-18）。ドライランの出力は消さない。
"""

import fcntl
import os
import re
import secrets
import shutil
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from tech_researcher.periods import JST

DATA_DIR_ENV = "TR_DATA_DIR"
DRY_RUN_PREFIX = "dry-"
# `YYYYMMDD-HHMMSS-xxxx`（JST の開始時刻と4桁の16進数。data-model.md 3.2）。ドライランは先頭に dry-
_RUN_ID = re.compile(r"(dry-)?(\d{4})(\d{2})\d{2}-\d{6}-[0-9a-f]{4}")
_LOCK = ".lock"


def data_dir(environ: Mapping[str, str], repo_root: Path) -> Path:
    """`TR_DATA_DIR` を読む。絶対パスで、リポジトリの外であることを確かめる（D-22）。"""
    value = environ.get(DATA_DIR_ENV, "")
    if not value:
        raise ValueError(f"{DATA_DIR_ENV} が設定されていない")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{DATA_DIR_ENV} は絶対パスにする: {value!r}")
    if path.resolve().is_relative_to(repo_root.resolve()):
        raise ValueError(f"{DATA_DIR_ENV} はリポジトリの外にする: {value!r}")
    return path


def new_run_id(
    now: datetime, *, dry_run: bool, token: Callable[[], str] = lambda: secrets.token_hex(2)
) -> str:
    """実行 ID。now は開始時刻（タイムゾーン付き）で、JST に直して使う。"""
    if now.tzinfo is None:
        raise ValueError("タイムゾーンのない日時から実行 ID は作れない")
    prefix = DRY_RUN_PREFIX if dry_run else ""
    run_id = f"{prefix}{now.astimezone(JST):%Y%m%d-%H%M%S}-{token()}"
    _check_run_id(run_id)
    return run_id


@dataclass(frozen=True)
class RunFiles:
    """1回の実行のファイルの置き場所。

    - dir：本番は `work/<実行ID>/`（実行の終わりに消す）、ドライランは `dry-run/<実行ID>/`（残す）
    - log：本番は `logs/YYYY/MM/<実行ID>.jsonl`。ドライランのログは出力のフォルダにだけ置く（7.4）
    """

    data_dir: Path
    run_id: str

    def __post_init__(self) -> None:
        _check_run_id(self.run_id)  # パスの部品にするので、形を確かめてから使う

    @property
    def dry_run(self) -> bool:
        return self.run_id.startswith(DRY_RUN_PREFIX)

    @property
    def db(self) -> Path:
        return db_path(self.data_dir)

    @property
    def dir(self) -> Path:
        return self.data_dir / ("dry-run" if self.dry_run else "work") / self.run_id

    @property
    def log(self) -> Path:
        if self.dry_run:
            return self.dir / "log.jsonl"
        match = _RUN_ID.fullmatch(self.run_id)
        assert match is not None
        year, month = match.group(2, 3)
        return self.data_dir / "logs" / year / month / f"{self.run_id}.jsonl"


def db_path(data_dir: Path) -> Path:
    return data_dir / "db" / "tech-researcher.sqlite3"


@contextmanager
def run_files(data_dir: Path, run_id: str) -> Iterator[RunFiles]:
    """実行のフォルダを作って渡す。本番は抜けるとき（例外のときも）にフォルダを消す。

    本番は、作る前に、ほかの実行が使っていない `work/` の残りを消す。使っているかは、各実行が
    持ち続けるファイルのロック（flock）で見分ける。プロセスが落ちればロックは外れるので、
    途中で止まった実行の残りだけが消える。同じ実行 ID のフォルダがすでにあれば失敗にする。
    """
    files = RunFiles(data_dir, run_id)
    if files.dry_run:
        files.dir.mkdir(parents=True)
        yield files
        return
    work = files.dir.parent
    work.mkdir(parents=True, exist_ok=True)
    created = False
    held: int | None = None
    try:
        # work/ の中を消す・作る操作は、すべて work/.lock を持って行い、ほかの実行と重ねない
        with _locked(work / _LOCK):
            for entry in work.iterdir():
                if entry.is_dir(follow_symlinks=False):
                    _remove_if_stale(entry)
            files.dir.mkdir()
            created = True
            held = _open_lock(files.dir / _LOCK)
            fcntl.flock(held, fcntl.LOCK_EX)
        yield files
    finally:
        try:
            if created:
                with _locked(work / _LOCK):
                    shutil.rmtree(files.dir)
        finally:
            if held is not None:
                os.close(held)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    fd = _open_lock(path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _remove_if_stale(path: Path) -> None:
    """ロックを取れた（持ち主のプロセスがもういない）フォルダだけを、ロックを持ったまま消す。"""
    lock = _open_lock(path / _LOCK)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return  # 実行中
        shutil.rmtree(path)
    finally:
        os.close(lock)


def _open_lock(path: Path) -> int:
    return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)


def _check_run_id(run_id: str) -> None:
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError(f"実行 ID の形でない: {run_id!r}")
