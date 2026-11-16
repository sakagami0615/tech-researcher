import pytest

from tech_researcher.render.sanitize import sanitize_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("普通の文章。Python 3.13 が出た", "普通の文章。Python 3.13 が出た"),
        ("一行目\n二行目\r\n\n三行目 四行目", "一行目 二行目 三行目 四行目"),
        ("  前後の空白  ", "前後の空白"),
        ("a | b", "a \\| b"),
        ("a\\b", "a\\\\b"),
        ("x < y", "x \\< y"),
        ("`code`", "'code'"),
    ],
)
def test_plain_formatting(raw, expected):
    assert sanitize_text(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("# 見出し", "\\# 見出し"),
        ("## 見出し", "\\## 見出し"),
        ("> 引用", "\\> 引用"),
        ("- 項目", "\\- 項目"),
        ("+ 項目", "\\+ 項目"),
        ("* 項目", "\\* 項目"),
        ("1. 項目", "1\\. 項目"),
        ("12) 項目", "12\\) 項目"),
        ("| a | b |", "\\| a \\| b \\|"),  # 表にならない
        ("---", "\\---"),
        ("~~~", "\\~~~"),
        ("[x]: y", "\\[x]: y"),
    ],
)
def test_block_markers_at_the_start_are_escaped(raw, expected):
    assert sanitize_text(raw) == expected


def test_block_markers_in_the_middle_are_kept():
    assert sanitize_text("A - B > C 1. D") == "A - B > C 1. D"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@octocat に通知", "`@octocat` に通知"),
        ("cc @org/team-a へ", "cc `@org/team-a` へ"),
        ("see #123 and #4", "see `#123` and `#4`"),
        ("GH-42 を修正", "`GH-42` を修正"),
        ("gh-42", "`gh-42`"),
        ("owner/repo#99", "`owner/repo#99`"),
        ("owner/repo@abcdef1", "`owner/repo@abcdef1`"),
        (
            "commit abcdef1 と 0123456789abcdef0123456789abcdef01234567",
            "commit `abcdef1` と `0123456789abcdef0123456789abcdef01234567`",
        ),
        ("&#64;octocat", "&amp;`#64`;octocat"),
        ("\\@octocat", "\\\\`@octocat`"),
    ],
)
def test_notifications_and_references_are_code_wrapped(raw, expected):
    assert sanitize_text(raw) == expected


@pytest.mark.parametrize(
    "text",
    [
        "1234567",  # 数字だけの列は SHA ではない
        "abcdef",  # 6桁
        "abcdef0123456789abcdef0123456789abcdef012",  # 41桁
        "mail me@example.com",
        "C# と F#",
        "話題の #タグ",
        "v1.2.3 のリリース",
        "100%",
    ],
)
def test_non_references_are_left_alone(text):
    assert sanitize_text(text) == text


def test_sha_boundary_lengths():
    assert sanitize_text("abcdef1") == "`abcdef1`"
    assert sanitize_text("a" * 40) == f"`{'a' * 40}`"
    assert sanitize_text("a" * 41) == "a" * 41


def test_wrapped_text_is_not_escaped_again():
    assert sanitize_text("#1 | #2") == "`#1` \\| `#2`"


def test_reference_at_the_start_is_not_treated_as_a_block():
    assert sanitize_text("#12 を直した") == "`#12` を直した"
