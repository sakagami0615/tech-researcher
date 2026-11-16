"""無害化した文字列を、GFM の実装（cmark-gfm）で描画して確かめる。

描画後の HTML に、許可したタグ以外が出ないこと、`<code>` の外に通知・参照・リンクになる文字列が
残らないことを、日次の4つの差し込み位置で確かめる。D2・D3 が先に除外する URL・リンク・画像は対象外。
"""

import re
from html.parser import HTMLParser

import cmarkgfm
import pytest

from tech_researcher.render.sanitize import sanitize_text

ALLOWED = {"p", "ul", "li", "strong", "h4", "code", "em", "del"}
ATTACKS = [
    "@octocat に通知",
    "cc @org/team-a",
    "@a@octocat",
    "_@octocat_ と __@octocat__",
    "*@octocat* と **#12**",
    "~~@octocat~~",
    "#12 と GH-34 と owner/repo#56",
    "abcdef1 と owner/repo@abcdef1",
    "_abcdef1_ と _#12_",
    "&#64;octocat と &#x40;octocat と &commat;octocat",
    "\\@octocat と \\#12",
    "foo@bar.com",
    "foo@-bar.com と foo@_bar.com",
    "a@.evil.com",
    "mailto:a@.b.com",
    "xmpp:a@b.co",
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "<!-- コメント -->",
    "[a]: /x",
    "[x]\n(y)",
    "$x$ と $$y$$",
    "# 見出し",
    "> 引用",
    "- 箇条書き",
    "1. 番号",
    "| a | b |\n|---|---|",
    "---",
    "___",
    "~~~",
    "```\ncode\n```",
    "**出典**：偽の出典",
    "一行目\n\n# 二行目",
]
SLOTS = {
    "point": "- **要点**\n  - {}",
    "heading": "#### 1. {}",
    "brief": "{} **重要性**：理由\n出典：x",
    "importance": "- **重要性**：{}",
}
# `<code>` の外に残ると、通知・参照になりうる文字列（リンクは <a> が出ないことで確かめる）
LEAK = re.compile(r"@[A-Za-z0-9_-]|(?<![A-Za-z0-9])#\d|GH-\d|\b[0-9a-f]{7,40}\b", re.I)


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags: set[str] = set()
        self.outside: list[str] = []
        self._code = 0

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        self._code += tag == "code"
        assert not attrs, (tag, attrs)

    def handle_endtag(self, tag):
        self._code -= tag == "code"

    def handle_data(self, data):
        if not self._code:
            self.outside.append(data)


@pytest.mark.parametrize("slot", SLOTS)
@pytest.mark.parametrize("attack", ATTACKS)
def test_rendered_text_has_no_links_tags_or_references(slot, attack):
    html = cmarkgfm.github_flavored_markdown_to_html(SLOTS[slot].format(sanitize_text(attack)))
    parser = _Collector()
    parser.feed(html)
    assert parser.tags <= ALLOWED, html
    outside = "".join(parser.outside)
    assert not LEAK.search(outside), (html, outside)
