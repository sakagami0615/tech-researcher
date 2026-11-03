"""① 収集（D-04。worker で動く）。

設定で有効にした情報源から集め、まとめて期間で絞り、実行のフォルダの collected.json に書く。
いまは RSS・Atom だけを集める。ほかの情報源と DB への保存は、それぞれを実装する Issue で足す。
外部由来の文（タイトル・本文）はファイルにだけ書き、戻り値（標準出力に出す要約）には件数と
情報源の ID だけを入れる（structure.md 3.2）。
"""

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from tech_researcher.collect.aggregate import aggregate
from tech_researcher.collect.period_filter import in_period
from tech_researcher.collect.records import SourceResult
from tech_researcher.collect.rss import collect_feed
from tech_researcher.config import Config, Feed, Limits
from tech_researcher.logging import RunLog
from tech_researcher.periods import Target

COLLECTED = "collected.json"

CollectFeed = Callable[[Feed, Limits], SourceResult]


def run_collect(
    config: Config,
    target: Target,
    run_dir: Path,
    log: RunLog,
    *,
    collect: CollectFeed = collect_feed,
) -> dict:
    """集めて run_dir/collected.json に書き、要約を返す。

    1つの情報源の失敗では止めず、失敗した情報源の ID を要約に入れる（US-E6-04）。
    run_dir は統括スクリプトが作る（実行の終わりに消すのも統括スクリプト）。
    """
    log.write("collect", "start")
    results = [_collect_one(collect, f, config.limits, log) for f in config.feeds if f.enabled]
    records = (r for result in results for r in result.records)
    items = [i for i in aggregate(records) if in_period(i, target)]
    # 途中で止まっても壊れたファイルを残さないよう、一時ファイルに書いてから置き換える
    partial = run_dir / f"{COLLECTED}.partial"
    partial.write_text(
        json.dumps([asdict(i) for i in items], ensure_ascii=False, default=_json_value),
        encoding="utf-8",
    )
    partial.replace(run_dir / COLLECTED)
    log.write("collect", "end", count=len(items))
    return {
        "items": len(items),
        "sources": len(results),
        "failed_sources": [r.source_id for r in results if r.error],
    }


def _collect_one(collect: CollectFeed, feed: Feed, limits: Limits, log: RunLog) -> SourceResult:
    """1つの情報源を集めてログに残す。想定外の例外もその情報源の失敗にする（US-E6-04）。"""
    try:
        result = collect(feed, limits)
    except Exception as e:
        # 例外の中身（外部由来の文を含みうる）は、マスクするログの exception にだけ残す
        result = SourceResult(f"rss:{feed.id}", (), 0, type(e).__name__)
        log.write(
            "collect", "failure", source=result.source_id, error_category=type(e).__name__, exc=e
        )
    log.write(
        "collect",
        "source_result",
        source=result.source_id,
        error_category="fetch_failed" if result.error else None,
        count=len(result.records),
        attempt=result.attempts,
        detail=result.error,
    )
    return result


def _json_value(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"JSON にできない値: {type(value).__name__}")
