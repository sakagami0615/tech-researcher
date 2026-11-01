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
    "text",
    [
        "ghp" + "_short",  # 短すぎる
        "my" + GHP,  # 識別子の一部
        "sk" + "-ant-",
        "github" + "_pat_",
        "通常の本文 https://github.com/example/repo",
    ],
)
def test_ordinary_text_is_not_detected(text):
    scanner = SecretScanner()
    assert not scanner.contains(text) and scanner.mask(text) == text


def test_known_values_are_masked_everywhere():
    secret = "dummy-known-secret-1"
    scanner = SecretScanner([secret, "short"])
    text = f"a {secret} b {secret} short"
    assert scanner.mask(text) == "a [MASKED:known] b [MASKED:known] short"


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
        "TR_ISSUE_REPO": "example/tech-researcher",  # 投稿先は秘匿値ではない（D-20）
        "API_KEY": "short",  # 短い値は除く
        "HOME": "/home/example",
    }
    assert sorted(known_secret_values(environ)) == ["dummy-collect-token", "dummy-oauth-token"]
