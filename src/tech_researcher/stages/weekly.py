"""週次の ② の入力の用意（D-04・D-17。worker で動く）。

① の後に、collected.json（取り直した分と日次の収集結果をまとめた今週の観測）と、DB の比較対象
（比較の窓の過去の観測・過去のテーマ・Star の増分）から、② の入力（agent-input.json）を作る。
DB がない（開発機のドライラン）ときは、比較対象は空にする。DB は読むだけなので、ドライランの
読み取り専用の接続でも使える。
"""

import json
import sqlite3
from pathlib import Path

from tech_researcher.agent.daily import InputItem
from tech_researcher.agent.weekly import WeeklyMaterials, build_weekly_input
from tech_researcher.collect.sources import source_names
from tech_researcher.config import Config
from tech_researcher.db.history import past_themes
from tech_researcher.db.weekly import (
    compare_week_starts,
    past_observations,
    period_star_gains,
    weeks_with_items,
)
from tech_researcher.periods import Target
from tech_researcher.stages.collect import COLLECTED, load_collected, load_item_ids
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


def write_weekly_input(
    config: Config, target: Target, run_dir: Path, db: sqlite3.Connection | None = None
) -> None:
    """run_dir/collected.json と item-ids.json から、run_dir/agent-input.json を書く。"""
    items = load_collected(run_dir / COLLECTED)
    inputs = [InputItem(i, item) for i, item in zip(load_item_ids(run_dir), items, strict=True)]
    data = build_weekly_input(
        target, inputs, source_names(config), weekly_materials(config, target, db)
    )
    path = run_dir / AGENT_INPUT
    partial = path.with_name(f"{path.name}.partial")
    partial.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    partial.replace(path)
