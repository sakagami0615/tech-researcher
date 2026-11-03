"""設定のトピックで GitHub Search を実際に呼ぶ（マーカー network。CI では流さない）。

収集用トークン（GITHUB_TOKEN）があるときだけ流す。トピックの数だけ Search を使うので、
続けて何度も流さない。
"""

from datetime import datetime
from pathlib import Path

import pytest

from tech_researcher.collect.github_api import GitHubClient, collect_token
from tech_researcher.collect.github_candidates import collect_candidates
from tech_researcher.config import load_config
from tech_researcher.periods import JST, daily_target

CONFIG = load_config(Path(__file__).parents[2] / "config" / "tech-researcher.toml")


@pytest.mark.network
def test_configured_topics_can_be_searched():
    token = collect_token()
    if token is None:
        pytest.skip("GITHUB_TOKEN がない")
    client = GitHubClient(token, CONFIG.limits)
    target = daily_target(datetime.now(JST).date(), CONFIG.daily_period_days)
    result = collect_candidates(CONFIG.github_repos, target.key, [], client, None)
    assert result.error is None, result.error_category
    searched = [c for c in result.candidates if "search" in c.routes]
    assert searched, "Search の候補が0件。トピックか検索条件の形が変わっていないか確かめる"
    assert result.search_attempts <= CONFIG.limits.github_search_calls
    print(f"候補 {len(result.candidates)} 件（Search {len(searched)} 件）")
    print(f"Search の要求 {client.search_calls} 回")
