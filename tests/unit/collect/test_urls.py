import pytest

from tech_researcher.collect.urls import normalize_url


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://example.com/a", "https://example.com/a"),
        ("http://example.com/a", "https://example.com/a"),
        ("HTTPS://Example.COM/a", "https://example.com/a"),
        ("https://example.com/a/", "https://example.com/a"),
        ("https://example.com/", "https://example.com"),
        ("https://example.com/a#section", "https://example.com/a"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "https://example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        ("  https://example.com/a  ", "https://example.com/a"),
        (
            "https://example.com/a?utm_source=x&id=3&UTM_Medium=y&fbclid=z&gclid=w",
            "https://example.com/a?id=3",
        ),
        ("https://example.com/a?b=2&a=1", "https://example.com/a?b=2&a=1"),  # 順序は変えない
        ("https://example.com/Path/Case", "https://example.com/Path/Case"),  # パスの大小は区別
        ("https://example.com./a", "https://example.com/a"),
        ("https://ドメイン.example/a", "https://xn--eckwd4c7c.example/a"),
        ("https://example.com/記事", "https://example.com/%E8%A8%98%E4%BA%8B"),
        ("https://example.com/%E8%A8%98%E4%BA%8B", "https://example.com/%E8%A8%98%E4%BA%8B"),
        ('https://example.com/a"><img', "https://example.com/a%22%3E%3Cimg"),
        ("https://example.com/a\u3000b", "https://example.com/a%E3%80%80b"),  # 全角空白は符号化
        ("https://example.com/a?x", "https://example.com/a?x"),
        ("https://example.com/a?q=a%20b", "https://example.com/a?q=a%20b"),
        ("https://example.com/a?q=1;r=2", "https://example.com/a?q=1;r=2"),
        ("https://example.com/a?utm%5Fsource=x&b=1", "https://example.com/a?b=1"),
        ("https://example.com:80/a", "https://example.com:80/a"),  # https の 80 は別の先
    ],
)
def test_spelling_variants_are_unified(url, expected):
    assert normalize_url(url) == expected


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/Anthropics/Claude-Code", "https://github.com/anthropics/claude-code"),
        (
            "https://GitHub.com/Anthropics/Claude-Code/blob/main/README.md",
            "https://github.com/anthropics/claude-code/blob/main/README.md",
        ),
        ("https://github.com/Anthropics", "https://github.com/anthropics"),
        ("https://www.github.com/A/B", "https://github.com/a/b"),
        ("https://github.com/A/B.git", "https://github.com/a/b"),
        (
            "https://example.com/Anthropics/Claude-Code",
            "https://example.com/Anthropics/Claude-Code",
        ),
    ],
)
def test_github_owner_and_repo_are_lowercased(url, expected):
    assert normalize_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "mailto:a@example.com",
        "ftp://example.com/a",
        "/relative/path",
        "https://",
        "https://example.com:99999/a",
        "",
        "https://example.com/a b",
        "https://example.com/a\x00",
        "https://exa<mple.com/",
        "https://[::1]:8443/a",
        "https://example.com\\@evil.com/",
        "https://user:pass@example.com/",
    ],
)
def test_unsafe_or_non_http_urls_are_rejected(url):
    assert normalize_url(url) is None
