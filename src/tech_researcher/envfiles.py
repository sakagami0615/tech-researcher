"""認証情報の env_file（`${TR_SECRETS_DIR}/worker.env`）の確認（D-05、structure.md 3.7）。

ホスト（統括スクリプト）で動く。

worker.env の中身は、compose が env_file として worker のコンテナにだけ渡す（compose.yaml）。
統括スクリプトは中身を環境変数として読み込まず、compose にも渡さない。ここで行うのは次のことだけ。

- 起動の前に、置き場所と権限を確かめる（誤りなら ValueError。統括スクリプトは終了コード 2 にする）。
  メッセージにはパスも値も入れない
- 1回の実行の間、compose に読ませる写し（`${TR_SECRETS_DIR}/.run-<実行ID>/worker.env`）を作り、
  終わったら消す。実行の途中で worker.env を書き換えても、compose が読む値とマスクに使う値が
  ずれないようにするため
- worker.env の書式を確かめ、ログに書く前のマスクに使う秘匿値を読む。書式は README の案内の
  とおりの簡単な形（`名前=値` を1行に1つ。値に空白・引用符・`$`・`\` を含めない）に限る。
  compose は書式の誤りをその行の値ごとエラー文に出し（実機で確認）、`$` は変数として展開する
  ので、そうなる書き方を起動の前に止める
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
# worker.env の1行。compose が解釈を変えたり（引用符・`$`・`\`・前後の空白）、エラー文に値を
# 出したりする文字を値に含めない
# 読み飛ばす行（空行と、空白・タブで字下げしてもよい `#` のコメント行）。字下げの文字を絞るのは、
# Python の strip が取り除く制御文字（\x1c など）を compose は名前の一部として扱い、行ごとエラー文に
# 出すため
_SKIP = re.compile(r"[ \t]*(?:#.*)?")
_LINE = re.compile(r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>[^\s'\"$\\]*)")


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
    try:
        path = Path(value).resolve()
    except OSError:  # たどれないパス。例外の文にはパスが入るので使わない
        raise ValueError(f"{SECRETS_DIR_ENV} のパスをたどれない（リンクを見直す）") from None
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
    _check_stat(st, label, kind, owner)


def _check_stat(
    st: os.stat_result,
    label: str,
    kind: tuple[Callable[[int], bool], str, tuple[int, ...]],
    owner: int,
) -> None:
    is_kind, kind_name, modes = kind
    if not is_kind(st.st_mode):
        raise ValueError(f"{label} は{kind_name}にする")
    if st.st_uid == 0 or st.st_uid != owner:
        raise ValueError(f"{label} の持ち主は bin/tr-run を動かす人にする（root にしない）")
    current = stat.S_IMODE(st.st_mode)
    if current not in modes:
        # 権限は秘匿情報ではないので、いまの値も出す（GNU の chmod 700 はフォルダの setgid を残す）
        allowed = "・".join(f"{m:o}" for m in modes)
        raise ValueError(
            f"{label} の権限は {allowed} にする（いまは {current:o}。グループとほかの人の権限や"
            f" setuid・setgid を外す。例：chmod 0{modes[0]:04o}）"
        )


def snapshot(secrets: Path, run_id: str) -> Path:
    """worker.env の写しを作り、そのフォルダ（compose に TR_SECRETS_DIR として渡す）を返す。

    写しのフォルダは 700、ファイルは 600。読み書きでシンボリックリンクはたどらない。元のファイルは
    _read_private で読む。作れなければ ValueError（例外の文にはパスが入るので使わない）。
    """
    run_dir = secrets / f"{SNAPSHOT_PREFIX}{run_id}"
    message = f"{SECRETS_DIR_ENV} の {WORKER_ENV} の写しを作れない"
    try:
        run_dir.mkdir(mode=0o700)
    except OSError:  # 同じ名前のフォルダ（ほかの実行の写しかもしれない）は消さない
        raise ValueError(message) from None
    try:
        data = _read_private(secrets / WORKER_ENV)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        with os.fdopen(os.open(run_dir / WORKER_ENV, flags, 0o600), "wb") as f:
            f.write(data)
    except ValueError:  # 確かめ直して違った（理由をそのまま伝える）
        remove_snapshot(run_dir)
        raise
    except OSError:
        remove_snapshot(run_dir)
        raise ValueError(message) from None
    return run_dir


def _read_private(path: Path) -> bytes:
    """worker.env を読む。確かめた後に差し替わっても、リンクはたどらず、FIFO などで待たない。

    開いたものの種類・持ち主・権限を確かめ直してから読む。違えば ValueError、開けなければ OSError。
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        _check_stat(os.fstat(f.fileno()), f"{SECRETS_DIR_ENV} の {WORKER_ENV}", _FILE, os.geteuid())
        return f.read()


def remove_snapshot(run_dir: Path) -> None:
    """写しを消す。消せなくても止めない（フォルダは 700 で、持ち主にしか読めない）。"""
    if run_dir.name.startswith(SNAPSHOT_PREFIX):
        shutil.rmtree(run_dir, ignore_errors=True)


def secret_values(path: Path) -> list[str]:
    """worker.env の書式を確かめ、秘匿値（名前の規則は secret_scan と同じ）を返す。

    書式は `名前=値` を1行に1つ。空行と、空白・タブで字下げしてもよい `#` の行は読み飛ばす。
    違う行があれば ValueError（行の番号だけを出し、パスも値も出さない）。読めなくても ValueError。
    """
    try:
        text = _read_private(path).decode("utf-8")
    except OSError, UnicodeDecodeError:
        raise ValueError(f"{SECRETS_DIR_ENV} の {WORKER_ENV} を読めない（UTF-8 にする）") from None
    if text.startswith("\ufeff"):
        raise ValueError(
            f"{SECRETS_DIR_ENV} の {WORKER_ENV} の先頭の BOM を外す（UTF-8 の BOM なしで保存する）"
        )
    values: list[str] = []
    for number, line in enumerate(text.split("\n"), start=1):
        line = line.removesuffix("\r")
        if _SKIP.fullmatch(line):
            continue
        m = _LINE.fullmatch(line)
        if m is None:
            raise ValueError(
                f"{SECRETS_DIR_ENV} の {WORKER_ENV} の {number} 行目は `名前=値` にする"
                "（値を引用符で囲まず、空白・引用符・$・\\ を含めない）"
            )
        if is_secret_name(m["name"]):
            values.append(m["value"])
    return values
