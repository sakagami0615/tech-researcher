"""週次の ② の入力の用意（D-04・D-17。worker で動く）。

① の後に、collected.json（取り直した分と日次の収集結果をまとめた今週の観測）と、DB の比較対象
（比較の窓の過去の観測・過去のテーマ・Star の増分）から、② の入力（agent-input.json）を作る。
DB がない（開発機のドライラン）ときは、比較対象は空にする。DB は読むだけなので、ドライランの
読み取り専用の接続でも使える。
"""

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

from tech_researcher.agent.daily import InputItem
from tech_researcher.agent.weekly import WeeklyInclusion, WeeklyMaterials, build_weekly_input
from tech_researcher.collect.records import Event
from tech_researcher.collect.sources import source_names
from tech_researcher.config import Config
from tech_researcher.db.history import past_themes
from tech_researcher.db.weekly import (
    compare_week_starts,
    past_observations,
    period_star_gains,
    weeks_with_items,
)
from tech_researcher.logging import RunLog
from tech_researcher.periods import Target
from tech_researcher.stages.collect import COLLECTED, INCLUSIONS, load_collected, load_item_ids
from tech_researcher.stages.daily import AGENT_INPUT
from tech_researcher.stages.weekly_items import daily_days


def weekly_materials(config: Config, target: Target, db: sqlite3.Connection | None):
    """DB から引く比較対象。週次の items がない過去の週は、比較の窓から外す（3.8）。"""
    if db is None:
        return WeeklyMaterials()
    weeks = weeks_with_items(db, compare_week_starts(target.key, config.weekly_compare_weeks))
    return WeeklyMaterials(
        weeks,
        past_observations(db, weeks),
        past_themes(db, target.key, config.weekly_compare_weeks),
        period_star_gains(db, *daily_days(target)),
    )


def load_inclusions(run_dir: Path) -> list[WeeklyInclusion]:
    """inclusions.json（collected.json と同じ順の掲載の根拠の出来事と日付）。"""
    raw = json.loads((run_dir / INCLUSIONS).read_text(encoding="utf-8"))
    return [
        WeeklyInclusion(
            Event(r["type"], datetime.fromisoformat(r["at"]), r["basis"]),
            date.fromisoformat(r["day"]),
        )
        for r in raw
    ]


def write_weekly_input(
    config: Config,
    target: Target,
    run_dir: Path,
    db: sqlite3.Connection | None = None,
    log: RunLog | None = None,
) -> None:
    """run_dir/collected.json・item-ids.json・inclusions.json から、agent-input.json を書く。

    log を渡すと、入力の観測の件数（今週・過去）とバイト数を残す（量を測るため）。
    """
    items = load_collected(run_dir / COLLECTED)
    inputs = [InputItem(i, item) for i, item in zip(load_item_ids(run_dir), items, strict=True)]
    data = build_weekly_input(
        target,
        inputs,
        load_inclusions(run_dir),
        source_names(config),
        weekly_materials(config, target, db),
    )
    body = json.dumps(data, ensure_ascii=False)
    path = run_dir / AGENT_INPUT
    partial = path.with_name(f"{path.name}.partial")
    partial.write_text(body, encoding="utf-8")
    partial.replace(path)
    if log is not None:
        log.write(
            "collect",
            "input",
            count=len(data["items"]),
            detail=f"past_observations={len(data['past_observations'])} bytes={len(body.encode())}",
        )
