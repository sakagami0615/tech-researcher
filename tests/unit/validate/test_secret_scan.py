import json
import urllib.parse

import pytest

from tech_researcher.validate.secret_scan import SecretScanner, known_secret_values

# テスト用のダミーのトークン。本物と取り違えられないよう、実行時に組み立てる
GHP = "ghp" + "_" + "x" * 36
GHS = "ghs" + "_" + "X0" * 18
PAT = "github" + "_pat_" + "dummy" * 5 + "_" + "0" * 59
ANT = "sk" + "-ant-oat01-" + "dummy-value_" * 4


@pytest.mark.parametrize(
    "token, kind",
    [(GHP, "github_token"), (GHS, "github_token"), (PAT, "github_pat"), (ANT, "anthropic_key")],
)
def test_token_patterns_are_detected_and_masked(token, kind):
    scanner = SecretScanner()
    text = f"Authorization: Bearer {token}\n続き"
    assert [f.kind for f in scanner.find(text)] == [kind]
    assert scanner.mask(text) == f"Authorization: Bearer [MASKED:{kind}]\n続き"


@pytest.mark.parametrize(
    "text, kind",
    [
        ("prefix" + GHP, "github_token"),
        ("%20" + GHP, "github_token"),
        (repr("x\n" + GHP), "github_token"),  # repr の `\n` の n が直前の文字になる
        ("Bearer_" + GHP, "github_token"),
        ("x" + ANT, "anthropic_key"),
        ("token" + PAT, "github_pat"),
    ],
)
def test_tokens_are_detected_regardless_of_preceding_character(text, kind):
    scanner = SecretScanner()
    assert [f.kind for f in scanner.find(text)] == [kind]
    assert GHP not in scanner.mask(text) and ANT not in scanner.mask(text)


@pytest.mark.parametrize(
    "text",
    [
        "ghp" + "_short",  # 短すぎる
        "sk" + "-ant-",
        "github" + "_pat_",
        "通常の本文 https://github.com/example/repo",
    ],
)
def test_ordinary_text_is_not_detected(text):
    scanner = SecretScanner()
    assert not scanner.contains(text) and scanner.mask(text) == text


def test_known_values_are_masked_everywhere_even_if_short():
    secret = "dummy-known-secret-1"
    scanner = SecretScanner([secret, "s3c"])
    text = f"a {secret} b {secret} s3c"
    assert scanner.mask(text) == "a [MASKED:known] b [MASKED:known] [MASKED:known]"


def test_known_values_are_stripped_and_empty_values_ignored():
    scanner = SecretScanner(["dummy-crlf-secret\r\n", "", "  "])
    assert scanner.mask("x dummy-crlf-secret y") == "x [MASKED:known] y"
    assert scanner.mask("ordinary text") == "ordinary text"


def test_self_overlapping_known_value_leaves_no_suffix():
    scanner = SecretScanner(["abcdabcd"])
    assert scanner.mask("abcdabcdabcd!") == "[MASKED:known]!"


@pytest.mark.parametrize(
    "encode",
    [
        lambda v: v,
        lambda v: repr(v),
        lambda v: json.dumps(v),
        lambda v: urllib.parse.quote(v, safe=""),
    ],
)
def test_escaped_forms_of_known_values_are_masked(encode):
    secret = "dummy\\pa\"ss'word/1"
    masked = SecretScanner([secret]).mask(f"value={encode(secret)}")
    assert "[MASKED:known]" in masked and "pa" not in masked.replace("[MASKED:known]", "")


def test_overlapping_findings_are_merged_without_leaking_parts():
    known = "prefix-" + GHP
    scanner = SecretScanner([known])
    assert scanner.mask(f"[{known}]") == "[[MASKED:known]]"
    # 短い既知の値を含む長い既知の値は、長い方で丸ごと伏せる
    scanner = SecretScanner(["dummy-value-a", "dummy-value-a-longer"])
    assert scanner.mask("dummy-value-a-longer!") == "[MASKED:known]!"
    # 既知の値とトークンの形が一部だけ重なっても、合わせて伏せる
    scanner = SecretScanner(["abc-" + GHP[:10]])
    assert scanner.mask("abc-" + GHP + " end") == "[MASKED:known] end"


def test_known_secret_values_selects_secret_like_names():
    environ = {
        "GITHUB_TOKEN": "dummy-collect-token",
        "CLAUDE_CODE_OAUTH_TOKEN": "dummy-oauth-token",
        "TR_PROJECTS_PAT": "dummy-pat",
        "TR_ISSUE_CREDENTIAL": "dummy-cred",
        "TR_ISSUE_REPO": "example/tech-researcher",  # 投稿先は秘匿値ではない（D-20）
        "PATH": "/usr/bin",  # PAT を含むが秘匿値ではない
        "API_KEY": "s3c",  # 短い値も含める
        "HOME": "/home/example",
    }
    assert sorted(known_secret_values(environ)) == [
        "dummy-collect-token",
        "dummy-cred",
        "dummy-oauth-token",
        "dummy-pat",
        "s3c",
    ]


def test_known_secret_values_feed_the_scanner():
    environ = {"GITHUB_TOKEN": "dummy-collect-token\r", "TR_ISSUE_REPO": "example/repo"}
    scanner = SecretScanner(known_secret_values(environ))
    assert scanner.mask("dummy-collect-token example/repo") == "[MASKED:known] example/repo"


def test_concatenated_tokens_leave_no_body():
    second = "ghp" + "_" + "y" * 36
    masked = SecretScanner().mask(GHP + second + " end")
    assert masked == "[MASKED:github_token] end"


def test_form_encoded_known_value_is_masked():
    secret = "dummy pass+word&1"
    text = "q=" + urllib.parse.quote_plus(secret, safe="")
    assert SecretScanner([secret]).mask(text) == "q=[MASKED:known]"


@pytest.mark.parametrize(
    "encode",
    [
        lambda v: repr(json.dumps({"token": v})),
        lambda v: json.dumps(repr(v)),
        lambda v: json.dumps(json.dumps(v)),
        lambda v: repr(repr(v)),
        lambda v: urllib.parse.quote(json.dumps(v), safe=""),
    ],
)
def test_two_level_escaped_known_value_is_masked(encode):
    secret = "dummy\\pa\"ss'word/1"
    masked = SecretScanner([secret]).mask(encode(secret))
    assert "[MASKED:known]" in masked and "pa" not in masked.replace("[MASKED:known]", "")
