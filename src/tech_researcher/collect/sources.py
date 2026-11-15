"""情報源 ID と設定ファイルの表示名の対応。

統括スクリプト（要約）と worker（② の入力・本文）が使う。
"""

from tech_researcher.collect.qiita import SOURCE_ID as QIITA_SOURCE_ID
from tech_researcher.collect.rss import feed_source_id
from tech_researcher.config import Config


def source_names(config: Config) -> dict[str, str]:
    """情報源 ID → 設定ファイルの表示名。"""
    names = {feed_source_id(f): f.display_name for f in config.feeds}
    for source_id, source in (
        ("hackernews", config.hackernews),
        ("github_search", config.github_search),
        (QIITA_SOURCE_ID, config.qiita),
    ):
        if source is not None:
            names[source_id] = source.display_name
    return names
