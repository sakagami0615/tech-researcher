"""GraphQL で Star 数を実際に取る（マーカー network。CI では流さない）。

GraphQL は認証なしでは使えないので、収集用トークン（GITHUB_TOKEN）があるときだけ流す。
問い合わせは1回（GraphQL の 1 ポイント程度）。DB には書かない。
"""

from datetime import datetime
from pathlib import Path

import pytest

from tech_researcher.collect.github_api import GitHubClient, collect_token
from tech_researcher.collect.github_candidates import Candidate
from tech_researcher.collect.github_stars import measure_stars
from tech_researcher.config import load_config
from tech_researcher.periods import JST, daily_target

CONFIG = load_config(Path(__file__).parents[2] / "config" / "tech-researcher.toml")


@pytest.mark.network
def test_star_counts_are_fetched_in_one_query():
    token = collect_token()
    if token is None:
        pytest.skip("GITHUB_TOKEN がない")
    client = GitHubClient(token, CONFIG.limits)
    target = daily_target(datetime.now(JST).date(), CONFIG.daily_period_days)
    candidates = [
        Candidate("anthropics/claude-code", ("watch",)),
        Candidate("openai/codex", ("watch",)),
        Candidate("tech-researcher-missing/no-such-repo", ("watch",)),
        # 名前を変えたリポジトリ（zeit → vercel）。新しい名前で返ることを確かめる
        Candidate("zeit/next.js", ("watch",)),
    ]
    result = measure_stars(
        candidates, client, target, None, write=False,
        batch_size=CONFIG.limits.github_graphql_batch,
    )  # fmt: skip
    assert result.error is None, result.error_category
    assert {s.repo for s in result.snapshots} == {
        "anthropics/claude-code",
        "openai/codex",
        "vercel/next.js",
    }
    assert all(s.stars_total > 0 for s in result.snapshots)
    assert result.missing == ("tech-researcher-missing/no-such-repo",)
    assert client.graphql_calls == 1
    for s in result.snapshots:
        print(f"{s.repo}: {s.stars_total}（作成 {s.created_at.isoformat()}）")
