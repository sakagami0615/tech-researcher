"""ドライランの DB の扱い（data-model.md 6章、D-22）。

ドライランは本番の DB を読み取り専用で開き、何も書かない。DB がない場所（開発機）では DB を
作らず、前日の記録や過去の観測がないものとして判定し、項目には仮の ID を付ける（structure.md 3.6）。
"""

import sqlite3
from collections.abc import Iterator
from itertools import count
from pathlib import Path


def open_readonly(path: Path) -> sqlite3.Connection | None:
    """読み取り専用で開く。DB がなければ作らずに None を返す。

    `mode=ro` の URI で開くので、ファイルがなくても作られず、書き込みは SQLite が拒む。
    多重防御として `query_only` も有効にする。WAL の DB を開けるかは #26 で確かめる。
    """
    if not path.is_file():
        return None
    # as_uri はパスの ? や # を符号化するので、URI の区切りと取り違えない
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
    except BaseException:
        conn.close()
        raise
    return conn


def tentative_ids() -> Iterator[str]:
    """ドライランで新しく集めた項目の仮の ID（d1, d2, …。data-model.md 2.6）。

    DB の主キーから作る本番の ID（i48213 など）と先頭の文字で区別する。
    """
    return (f"d{n}" for n in count(1))
