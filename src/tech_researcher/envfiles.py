"""認証情報の env_file（`${TR_SECRETS_DIR}/worker.env`）の確認（D-05、structure.md 3.7）。

ホスト（統括スクリプト）で動く。

worker.env の中身は、compose が env_file として worker のコンテナにだけ渡す（compose.yaml）。
統括スクリプトは中身を環境変数として読み込まず、compose にも渡さない。ここで行うのは次の2つだけ。

- 起動の前に、置き場所と権限を確かめる（誤りなら ValueError。統括スクリプトは終了コード 2 にする）。
  メッセージにはパスも値も入れない
- 1回の実行の間、compose に読ませる写し（`${TR_SECRETS_DIR}/.run-<実行ID>/worker.env`）を作り、
  終わったら消す。実行の途中で worker.env を書き換えても、compose が読む値とマスクに使う値が
  ずれないようにするため
- ログに書く前のマスクに使う秘匿値を、その写しから読む。compose は env_file の書式の誤りを、
  その行の値ごとエラー文に出す（実機で確認）ので、そのエラー文を伏せるため
"""

import os
import re
import shutil
import stat
from collections.abc import Callable, Mapping
from pathlib import Path

from tech_researcher.validate.secret_scan import is_secret_name

SECRETS_DIR_ENV = "TR_SECRETS_DIR"
WORKER_ENV = "worker.env"
SNAPSHOT_PREFIX = ".run-"  # 1回の実行の間だけ置く写しのフォルダ
# `名前=値`・`名前: 値`（前に export が付いてもよい）。名前の文字は広めに取る（伏せる側に倒す）
_LINE = re.compile(r"\s*(?:export\s+)?(?P<name>[^\s=:#]+)\s*[=:](?P<value>.*)")


def secrets_dir(
    environ: Mapping[str, str],
    repo: Path,
    data: Path,
) -> Path:
    """`TR_SECRETS_DIR` を確かめ、解決したパスを返す。

    絶対パスで、リポジトリとデータ用フォルダ（worker にマウントする）の外にある、実在のフォルダ。
    フォルダと worker.env は、bin/tr-run を動かす人（root 以外）の持ち主で、グループとほかの人の
    権限がない（フォルダは 700、worker.env は 600 か 400）。worker.env はリンクにしない。
    """
    value = environ.get(SECRETS_DIR_ENV, "")
    if not value:
        raise ValueError(f"{SECRETS_DIR_ENV} が設定されていない（{WORKER_ENV} を置くフォルダ）")
    if not Path(value).is_absolute():
        raise ValueError(f"{SECRETS_DIR_ENV} は絶対パスにする")
    path = Path(value).resolve()
    if path.is_relative_to(repo.resolve()):
        raise ValueError(f"{SECRETS_DIR_ENV} はリポジトリの外にする")
    if path.is_relative_to(data.resolve()):
        raise ValueError(
            f"{SECRETS_DIR_ENV} は TR_DATA_DIR の外にする（worker にマウントするため）"
        )
    owner = os.geteuid()
    _check_private(path, f"{SECRETS_DIR_ENV} のフォルダ", _DIR, owner)
    _check_private(path / WORKER_ENV, f"{SECRETS_DIR_ENV} の {WORKER_ENV}", _FILE, owner)
    return path


# 種類の判定、種類の説明、認める権限（setuid なども含めて、この値だけ）。フォルダは compose が
# 読めて写しを作れる 700、worker.env は compose が読める 600 か、読み取りだけの 400
_DIR = (stat.S_ISDIR, "フォルダ", (0o700,))
_FILE = (stat.S_ISREG, "通常のファイル（シンボリックリンクにしない）", (0o600, 0o400))


def _check_private(
    path: Path, label: str, kind: tuple[Callable[[int], bool], str, tuple[int, ...]], owner: int
) -> None:
    is_kind, kind_name, modes = kind
    try:
        st = path.lstat()  # シンボリックリンクはたどらない（別の場所のファイルを渡さない）
    except FileNotFoundError:
        raise ValueError(f"{label} がない（先に作る）") from None
    except OSError:  # 親のフォルダをたどれないなど。例外の文にはパスが入るので使わない
        raise ValueError(f"{label} を確かめられない（親のフォルダの権限を見直す）") from None
    if not is_kind(st.st_mode):
        raise ValueError(f"{label} は{kind_name}にする")
    if st.st_uid == 0 or st.st_uid != owner:
        raise ValueError(f"{label} の持ち主は bin/tr-run を動かす人にする（root にしない）")
    if stat.S_IMODE(st.st_mode) not in modes:
        allowed = "・".join(f"{m:o}" for m in modes)
        raise ValueError(f"{label} の権限は {allowed} にする（グループとほかの人の権限を外す）")


def snapshot(secrets: Path, run_id: str) -> Path:
    """worker.env の写しを作り、そのフォルダ（compose に TR_SECRETS_DIR として渡す）を返す。

    写しのフォルダは 700、ファイルは 600。読み書きでシンボリックリンクはたどらない。
    作れなければ ValueError（例外の文にはパスが入るので使わない）。
    """
    run_dir = secrets / f"{SNAPSHOT_PREFIX}{run_id}"
    message = f"{SECRETS_DIR_ENV} の {WORKER_ENV} の写しを作れない"
    try:
        run_dir.mkdir(mode=0o700)
    except OSError:  # 同じ名前のフォルダ（ほかの実行の写しかもしれない）は消さない
        raise ValueError(message) from None
    try:
        src = os.open(secrets / WORKER_ENV, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(src, "rb") as f:
            data = f.read()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        with os.fdopen(os.open(run_dir / WORKER_ENV, flags, 0o600), "wb") as f:
            f.write(data)
    except OSError:
        remove_snapshot(run_dir)
        raise ValueError(message) from None
    return run_dir


def remove_snapshot(run_dir: Path) -> None:
    """写しを消す。消せなくても止めない（フォルダは 700 で、持ち主にしか読めない）。"""
    if run_dir.name.startswith(SNAPSHOT_PREFIX):
        shutil.rmtree(run_dir, ignore_errors=True)


def secret_values(path: Path) -> list[str]:
    """worker.env の秘匿値（名前の規則は secret_scan と同じ）。ログのマスクにだけ使う。

    compose の書式を厳密にはまねず、伏せる側に倒す。`名前=値`（compose が受け付ける `名前: 値`
    も）の行ごとに、値そのものと、前後の引用符を外したものと、行末のコメント（` #` 以降）を
    外したものを返す。書式の誤った行（閉じていない引用符など）も読む。
    compose がその値をエラー文に出すため。
    読めなければ ValueError（例外の文にはパスが入るので使わない）。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise ValueError(f"{SECRETS_DIR_ENV} の {WORKER_ENV} を読めない") from None
    values: list[str] = []
    for line in text.splitlines():
        m = _LINE.match(line)
        if not m or not is_secret_name(m["name"]):
            continue
        value = m["value"].strip()
        values += [value, value.strip("\"'"), value.split(" #")[0].strip().strip("\"'")]
    return values
