"""AI の文章の無害化（report-templates.md 0.3）。

D2・D3（validate/text.py）を通った文章だけを渡す。GitHub が本文から通知・参照・リンクを作る
書き方を、本文に差し込む前に無効にする。外部由来のタイトルや設定の名前は、render/common.py の
plain_text を使う（0.3 の別の行）。
"""

import re

_SPACES = re.compile(r"\s+")
# 表示の向きを変える制御文字（LRM・RLM・ALM、LRE〜RLO、LRI〜PDI）。見た目を入れ替えて偽装できる
_BIDI = re.compile("[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
# GitHub が通知・参照にする文字列。インラインコードで囲む（0.3）。先に書いた形を優先する。
# 直前の文字は見ない（`@a@b`・`_@a_` は描画後に別の文字列になる）。SHA だけは単語の途中を除く
# コミット SHA は a〜f を1文字以上含む7〜40桁（数字だけの列は対象外）
_REFERENCE = re.compile(
    r"""(?:
        [A-Za-z0-9-]+/[A-Za-z0-9._-]+@[0-9a-f]{7,40}(?![0-9A-Za-z])  # owner/repo@SHA
      | [A-Za-z0-9-]+/[A-Za-z0-9._-]+\#[0-9]+(?![0-9A-Za-z])         # owner/repo#数字
      | GH-[0-9]+(?![0-9A-Za-z])                                     # GH-数字
      | \#[0-9]+(?![0-9A-Za-z])                                      # #数字
      | @[A-Za-z0-9_-][A-Za-z0-9_-]*(?:/[A-Za-z0-9._-]+)?               # @ユーザー名・@org/team
      | (?<![0-9A-Za-z])(?=[0-9]*[a-f])[0-9a-f]{7,40}(?![0-9A-Za-z])  # コミット SHA
    )""",
    re.VERBOSE | re.IGNORECASE,
)
# 先頭に置くとブロックになる記法（見出し・引用・箇条書き・番号付きリスト・表・脚注定義）
_BLOCK_START = re.compile(r"^(?:\d+(?=[.)])|[#>\-+|=\[])")
# 語の途中の _（snake_case）は強調にならない。値を途中で割らないよう、そのままにする
_EDGE_UNDERSCORE = re.compile(r"(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])")
_ESCAPES = str.maketrans(
    {
        "\\": "\\\\",
        "<": "\\<",
        "|": "\\|",
        "&": "&amp;",
        "$": "\\$",
        "*": "\\*",
        "~": "\\~",
        "@": "＠",
    }
)


def sanitize_text(text: str) -> str:
    """AI の文章を、1行の、通知・参照・ブロック・HTML の効かない文字列にする。

    - 表示の向きを変える制御文字を取る。改行を含む空白を1つの空白にし、前後を取る
      （要点は配列の1要素が1行）
    - バッククォートは `'` にする（AI の囲みと、無害化の囲みが混ざらないように）
    - `\\`・`<`・`|`・`$`・`*`・`~`・`&`・語の途中でない `_` をエスケープする
      （`&#64;` が `@` になるのを防ぐ。`$` は数式、`*_~` は強調で、ラベルをまねさせない）
    - 囲みに当たらずに残った `@` は全角の `＠` にする（`a@.b.com` のメール自動リンクを防ぐ）
    - `@ユーザー名`・`@org/team`・`#数字`・`GH-数字`・`owner/repo#数字`・コミット SHA・
      `owner/repo@SHA` をインラインコードで囲む
    - 先頭のブロック記法（`#`・`>`・`-`・`1.` など）をエスケープする
    """
    text = _SPACES.sub(" ", _BIDI.sub("", text)).strip().replace("`", "'")
    # 語の途中かは、囲む前の連結した文字列で判定する（`ghp_abc` の `_` を割らない）
    edges = {m.start() for m in _EDGE_UNDERSCORE.finditer(text)}
    parts: list[str] = []
    last = 0
    for match in _REFERENCE.finditer(text):
        parts.append(_escape(text, last, match.start(), edges))
        parts.append(f"`{match.group()}`")
        last = match.end()
    parts.append(_escape(text, last, len(text), edges))
    return _BLOCK_START.sub(_escape_block, "".join(parts), count=1)


def _escape(text: str, start: int, end: int, edges: set[int]) -> str:
    """text[start:end] をエスケープする。edges は、エスケープする _ の text 内の位置。"""
    return "".join(
        "\\_" if i in edges else ch.translate(_ESCAPES)
        for i, ch in enumerate(text[start:end], start)
    )


def _escape_block(match: re.Match[str]) -> str:
    marker = match.group()
    return marker + "\\" if marker.isdigit() else "\\" + marker
