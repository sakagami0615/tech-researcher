import copy
import tomllib
from pathlib import Path

import pytest

from tech_researcher.config import ConfigError, load_config, parse_config

CONFIG_PATH = Path(__file__).parents[2] / "config" / "tech-researcher.toml"

SOURCES = {
    "hackernews": {"enabled": True, "display_name": "Hacker News"},
    "github_search": {"enabled": False, "display_name": "GitHub"},
    "github_repos": {
        "enabled": True,
        "display_name": "GitHub",
        "search_topics": ["llm"],
        "watch_repos": ["anthropics/claude-code"],
        "threshold_increase": 100,
        "report_top_n": 10,
        "new_repo_days": 14,
        "new_repo_stars": 200,
        "search_days": 14,
        "search_min_stars": 200,
    },
    "github_releases": {"enabled": True, "display_name": "GitHub Releases", "repos": ["a/b"]},
    "qiita": {"enabled": True, "display_name": "Qiita", "tags": ["LLM"]},
}
FEED = {
    "id": "aws",
    "display_name": "AWS",
    "url": "https://aws.amazon.com/about-aws/whats-new/recent/feed/",
    "kind": "new",
    "enabled": True,
}
LAST30DAYS = {
    "subqueries": [
        {"search_query": "llm", "ranking_query": "llm", "sources": ["hackernews"], "weight": 1.0}
    ]
}


@pytest.fixture
def base():
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)


def full(base):
    data = copy.deepcopy(base)
    data["sources"] = copy.deepcopy(SOURCES)
    data["feeds"] = [dict(FEED)]
    data["last30days"] = copy.deepcopy(LAST30DAYS)
    return data


def test_repository_config_loads():
    config = load_config(CONFIG_PATH)
    assert config.daily_period_days == 1
    assert config.weekly_period_days == 7
    assert [f.id for f in config.feeds] == ["cloudflare"]
    assert config.hackernews.enabled and config.github_search.enabled and config.subqueries
    assert config.limits.http_retry_waits_s == (5, 20)
    assert config.limits.agent_max_turns is None
    assert config.limits.daily.job == 60
    assert config.limits.weekly.agent_input_bytes == 8_000_000


def test_all_sources_parse(base):
    config = parse_config(full(base))
    assert config.hackernews.enabled and not config.github_search.enabled
    assert config.github_repos.watch_repos == ("anthropics/claude-code",)
    assert config.feeds[0].kind == "new"
    assert config.subqueries[0].sources == ("hackernews",)


def test_sources_are_optional(base):
    for name in ("feeds", "sources", "last30days"):
        base.pop(name, None)
    config = parse_config(base)
    assert config.hackernews is None and config.feeds == () and config.subqueries == ()


def test_source_can_be_disabled_or_added_without_code(base):
    data = full(base)
    data["feeds"].append({**FEED, "id": "zenn", "kind": "popular", "enabled": False})
    config = parse_config(data)
    assert [(f.id, f.enabled) for f in config.feeds] == [("aws", True), ("zenn", False)]


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d.update(publish={"repo": "x/y"}), "知らない項目 publish"),
        (lambda d: d["daily"].update(perod_days=1), "知らない項目 perod_days"),
        (lambda d: d["sources"].update(slack={"enabled": True}), "知らない項目 slack"),
        (lambda d: d["sources"]["qiita"].update(token="secret"), "知らない項目 token"),
        (lambda d: d["limits"].update(github_token="secret"), "知らない項目 github_token"),
        (lambda d: d["feeds"][0].update(extra=1), "知らない項目 extra"),
    ],
)
def test_unknown_keys_are_rejected(base, mutate, message):
    data = full(base)
    mutate(data)
    with pytest.raises(ConfigError, match=message):
        parse_config(data)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["daily"].update(period_days="1"),
        lambda d: d["daily"].update(period_days=0),
        lambda d: d["weekly"].update(period_days=True),
        lambda d: d["sources"]["qiita"].update(enabled="yes"),
        lambda d: d["sources"]["qiita"].update(tags="LLM"),
        lambda d: d["sources"]["github_repos"].update(report_top_n=0),
        lambda d: d["limits"].update(http_retry_waits_s=[5, -1]),
        lambda d: d["limits"].update(agent_max_turns=0),
        lambda d: d["feeds"][0].update(kind="hot"),
        lambda d: d["feeds"][0].update(url="http://example.com/feed"),
        lambda d: d["last30days"]["subqueries"][0].update(weight=0.0),
        lambda d: d["last30days"]["subqueries"][0].update(sources=["hackernwes"]),
        lambda d: d["last30days"]["subqueries"][0].update(sources=["github_search"]),
        lambda d: d.pop("limits"),
        lambda d: d["sources"]["qiita"].pop("display_name"),
        lambda d: d["feeds"][0].pop("url"),
        lambda d: d["feeds"][0].update(id=""),
        lambda d: d["sources"]["hackernews"].update(display_name=" "),
        lambda d: d.update(sources=["hackernews"]),
    ],
)
def test_invalid_values_are_rejected(base, mutate):
    data = full(base)
    mutate(data)
    with pytest.raises(ConfigError):
        parse_config(data)


def test_unknown_source_is_rejected_without_other_sources(base):
    data = copy.deepcopy(base)
    data["sources"] = {"hackernws": {"enabled": True, "display_name": "HN"}}
    with pytest.raises(ConfigError, match="知らない項目 hackernws"):
        parse_config(data)


def test_duplicate_feed_ids_are_rejected(base):
    data = full(base)
    data["feeds"].append(dict(FEED))
    with pytest.raises(ConfigError, match="重複"):
        parse_config(data)


def test_stage_limits_must_fit_in_job(base):
    data = full(base)
    data["limits"]["daily"]["analyze_minutes"] = 40  # 20+40+2+8+5 = 75 > 60
    with pytest.raises(ConfigError, match="ジョブ全体"):
        parse_config(data)


def test_broken_toml_is_reported(tmp_path):
    path = tmp_path / "broken.toml"
    path.write_text("[daily\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="TOML"):
        load_config(path)
