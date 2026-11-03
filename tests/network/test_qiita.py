"""設定のタグで Qiita API の新着を実際に取得する（マーカー network。CI では流さない）。

認証なしは IP ごとに1時間60回なので、続けて何度も流さない。
"""

from datetime import datetime
from pathlib import Path

import pytest

from tech_researcher.collect.aggregate import aggregate
from tech_researcher.collect.period_filter import in_period
from tech_researcher.collect.qiita import collect_qiita
from tech_researcher.config import load_config
from tech_researcher.periods import JST, daily_target

CONFIG = load_config(Path(__file__).parents[2] / "config" / "tech-researcher.toml")


@pytest.mark.network
def test_configured_tags_can_be_fetched():
    target = daily_target(datetime.now(JST).date(), CONFIG.daily_period_days)
    result = collect_qiita(CONFIG.qiita, target.period, CONFIG.limits)
    assert result.error is None
    assert result.records, "前日の新着が0件。タグか検索条件の形が変わっていないか確かめる"
    assert result.attempts <= CONFIG.limits.qiita_calls
    # 日付の条件で取った記事は、すべて期間の判定を通る
    items = aggregate(result.records)
    assert len(items) == len(result.records)
    assert all(in_period(i, target) for i in items)
    print(f"{target.period}: {len(result.records)} 件、要求 {result.attempts} 回")
