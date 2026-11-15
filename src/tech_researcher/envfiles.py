"""認証情報の env_file（`${TR_SECRETS_DIR}/worker.env`）の確認（D-05、structure.md 3.7）。

ホスト（統括スクリプト）で動く。

worker.env の中身は、compose が env_file として worker のコンテナにだけ渡す（compose.yaml）。
統括スクリプトは中身を環境変数として読み込まず、compose にも渡さない。ここで行うのは次の2つだけ。

- 起動の前に、置き場所と権限を確かめる（誤りなら ValueError。統括スクリプトは終了コード 2 にする）。
  メッセージにはパスも値も入れない
- ログに書く前のマスクに使う秘匿値を読む。compose は env_file の書式の誤りを、その行の値ごと
  エラー文に出す（実機で確認）ので、そのエラー文を伏せるため
"""

import os
import stat
from collections.abc import Callable, Mapping
from pathlib import Path

from tech_researcher.validate.secret_scan import is_secret_name

SECRETS_DIR_ENV = "TR_SECRETS_DIR"
WORKER_ENV = "worker.env"
_EXPORT = "export "


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


# 種類の判定、種類の説明、求める権限、持ち主に要る権限（compose が読めるように）
_DIR = (stat.S_ISDIR, "フォルダ", "700", stat.S_IRUSR | stat.S_IXUSR)
_FILE = (stat.S_ISREG, "通常のファイル（シンボリックリンクにしない）", "600", stat.S_IRUSR)


def _check_private(
    path: Path, label: str, kind: tuple[Callable[[int], bool], str, str, int], owner: int
) -> None:
    is_kind, kind_name, mode, needed = kind
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
    if st.st_mode & 0o077 or st.st_mode & needed != needed:
        raise ValueError(f"{label} の権限は {mode} にする（グループとほかの人の権限を外す）")


def secret_values(path: Path) -> list[str]:
    """worker.env の秘匿値（名前の規則は secret_scan と同じ）。ログのマスクにだけ使う。

    compose の書式を厳密にはまねず、伏せる側に倒す。`名前=値` の行ごとに、値そのものと、
    前後の引用符を外したものと、行末のコメント（` #` 以降）を外したものを返す。書式の誤った行
    （閉じていない引用符など）も読む。compose がその値をエラー文に出すため。
    読めなければ ValueError（例外の文にはパスが入るので使わない）。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise ValueError(f"{SECRETS_DIR_ENV} の {WORKER_ENV} を読めない") from None
    values: list[str] = []
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        name = name.strip().removeprefix(_EXPORT).strip()
        if not sep or not is_secret_name(name):
            continue
        value = value.strip()
        values += [value, value.strip("\"'"), value.split(" #")[0].strip().strip("\"'")]
    return values
