"""AI の文章の無害化（report-templates.md 0.3）。

D2・D3（validate/text.py）を通った文章だけを渡す。GitHub が本文から通知・参照・リンクを作る
書き方を、本文に差し込む前に無効にする。外部由来のタイトルや設定の名前は、render/common.py の
plain_text を使う（0.3 の別の行）。
"""

import re

_SPACES = re.compile(r"\s+")
# GitHub が通知・参照にする文字列。インラインコードで囲む（0.3）。先に書いた形を優先する。
# コミット SHA は a〜f を1文字以上含む7〜40桁（数字だけの列は対象外）
_REFERENCE = re.compile(
    r"""(?<![A-Za-z0-9_])(?:
        [A-Za-z0-9-]+/[A-Za-z0-9._-]+@[0-9a-f]{7,40}(?![0-9A-Za-z])  # owner/repo@SHA
      | [A-Za-z0-9-]+/[A-Za-z0-9._-]+\#[0-9]+(?![0-9A-Za-z])         # owner/repo#数字
      | GH-[0-9]+(?![0-9A-Za-z])                                     # GH-数字
      | \#[0-9]+(?![0-9A-Za-z])                                      # #数字
      | @[A-Za-z0-9][A-Za-z0-9-]*(?:/[A-Za-z0-9._-]+)?               # @ユーザー名・@org/team
      | (?=[0-9]*[a-f])[0-9a-f]{7,40}(?![0-9A-Za-z])                 # コミット SHA
    )""",
    re.VERBOSE | re.IGNORECASE,
)
# 先頭に置くとブロックになる記法（見出し・引用・箇条書き・番号付きリスト・表・区切り線・脚注定義）
_BLOCK_START = re.compile(r"^(?:\d+(?=[.)])|[#>\-+*|~=\[])")
_ESCAPES = str.maketrans({"\\": "\\\\", "<": "\\<", "|": "\\|", "&": "&amp;"})


def sanitize_text(text: str) -> str:
    """AI の文章を、1行の、通知・参照・ブロック・HTML の効かない文字列にする。

    - 改行を含む空白を1つの空白にし、前後を取る（要点は配列の1要素が1行）
    - バッククォートは `'` にする（AI の囲みと、無害化の囲みが混ざらないように）
    - `\\`・`<`・`|`・`&` をエスケープする（`&#64;` のような実体参照が `@` になるのを防ぐ）
    - `@ユーザー名`・`@org/team`・`#数字`・`GH-数字`・`owner/repo#数字`・コミット SHA・
      `owner/repo@SHA` をインラインコードで囲む
    - 先頭のブロック記法（`#`・`>`・`-`・`1.` など）をエスケープする
    """
    text = _SPACES.sub(" ", text).strip().replace("`", "'")
    parts: list[str] = []
    last = 0
    for match in _REFERENCE.finditer(text):
        parts.append(text[last : match.start()].translate(_ESCAPES))
        parts.append(f"`{match.group()}`")
        last = match.end()
    parts.append(text[last:].translate(_ESCAPES))
    return _BLOCK_START.sub(_escape_block, "".join(parts), count=1)


def _escape_block(match: re.Match[str]) -> str:
    marker = match.group()
    return marker + "\\" if marker.isdigit() else "\\" + marker
