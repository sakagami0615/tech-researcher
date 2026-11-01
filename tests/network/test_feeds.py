"""設定ファイルの RSS を実際に取得する（マーカー network。CI では流さない）。"""

from datetime import datetime
from pathlib import Path

import pytest

from tech_researcher.collect.aggregate import aggregate
from tech_researcher.collect.period_filter import in_period
from tech_researcher.collect.rss import collect_feed
from tech_researcher.config import load_config
from tech_researcher.periods import JST, daily_target

CONFIG = load_config(Path(__file__).parents[2] / "config" / "tech-researcher.toml")


@pytest.mark.network
@pytest.mark.parametrize("feed", [f for f in CONFIG.feeds if f.enabled], ids=lambda f: f.id)
def test_configured_feed_can_be_fetched(feed):
    result = collect_feed(feed, CONFIG.limits)
    assert result.error is None
    assert all(r.published_at for r in result.records), "公開日時のないエントリーがある"
    # 1週間分を見て、期間の判定まで通ることを確かめる
    target = daily_target(datetime.now(JST).date(), 7)
    items = [i for i in aggregate(result.records) if in_period(i, target)]
    assert items, "1週間で0件。フィードの URL か形式が変わっていないか確かめる"
