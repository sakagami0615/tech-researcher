"""設定ファイルの GitHub Releases と README を実際に取得する（マーカー network。CI では流さない）。

収集用トークンを環境変数 GITHUB_TOKEN で渡せば使う。なければ認証なし（1時間60回まで）で取る。
"""

import logging
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from tech_researcher.collect.aggregate import aggregate
from tech_researcher.collect.github_api import GitHubClient, collect_token
from tech_researcher.collect.github_releases import collect_releases, fetch_readmes
from tech_researcher.collect.period_filter import in_period
from tech_researcher.config import load_config
from tech_researcher.periods import JST, daily_target

CONFIG = load_config(Path(__file__).parents[2] / "config" / "tech-researcher.toml")


def client():
    return GitHubClient(collect_token(), CONFIG.limits)


@pytest.mark.network
@pytest.mark.parametrize("repo", CONFIG.github_releases.repos)
def test_configured_repo_releases_can_be_fetched(repo, caplog):
    releases = replace(CONFIG.github_releases, repos=(repo,))
    today = datetime.now(JST).date()
    # 週次の期間（7日）で、ページの上限に達しないことを確かめる
    weekly = collect_releases(releases, daily_target(today, 7).period, client())
    assert weekly.error is None and weekly.failed_parts == (), "上限に達したら MAX_PAGES を見直す"
    # 30日分を見て、リリースがあり、期間の判定まで通ることを確かめる
    target = daily_target(today, 30)
    with caplog.at_level(logging.WARNING):
        result = collect_releases(releases, target.period, client())
    assert result.error is None
    items = [i for i in aggregate(result.records) if in_period(i, target)]
    assert items, "30日でリリースが0件。追跡先として妥当か確かめる"
    assert any(r.primary_text for r in result.records), "本文のあるリリースがない"


@pytest.mark.network
def test_readme_can_be_fetched():
    result = fetch_readmes(["anthropics/claude-code"], client())
    assert result.errors == {}
    readme = result.readmes["anthropics/claude-code"]
    assert "Claude Code" in readme.text and readme.links
