"""週次の ② の入力の用意と、② の出力の ③ 検証・本文の作成（D-04・D-07・D-17。worker で動く）。

① の後に、collected.json（取り直した分と日次の収集結果をまとめた今週の観測）と、DB の比較対象
（比較の窓の過去の観測・過去のテーマ・Star の増分）から、② の入力（agent-input.json）を作る。
DB がない（開発機のドライラン）ときは、比較対象は空にする。DB は読むだけなので、ドライランの
読み取り専用の接続でも使える。
③ は、② に渡したコピーではなく、worker の collected.json から同じ入力を作り直して照合し（D-04）、
検査を通った項目から本文を作る（verify_weekly）。保存・投稿と失敗の分岐は、ここでは行わない。
"""

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path

from tech_researcher.agent.daily import InputItem
from tech_researcher.agent.weekly import WeeklyInclusion, WeeklyMaterials, build_weekly_input
from tech_researcher.agent.weekly_output import WeeklyOutput, assign_theme_ids, parse_weekly_output
from tech_researcher.collect.records import Event, Item
from tech_researcher.collect.sources import source_names
from tech_researcher.config import Config
from tech_researcher.db.history import next_theme_id, past_themes
from tech_researcher.db.weekly import (
    compare_week_starts,
    past_observations,
    period_star_gains,
    weeks_with_items,
)
from tech_researcher.logging import RunLog
from tech_researcher.periods import Target
from tech_researcher.render.common import Report
from tech_researcher.render.weekly import PastWindow, render_weekly
from tech_researcher.stages.collect import (
    COLLECTED,
    INCLUSIONS,
    load_collected,
    load_item_ids,
    unavailable_notes,
)
from tech_researcher.stages.daily import AGENT_INPUT
from tech_researcher.stages.weekly_items import daily_days
from tech_researcher.validate.weekly import WeeklyChecked, check_weekly


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


def _weekly_input(
    config: Config, target: Target, run_dir: Path, db: sqlite3.Connection | None
) -> tuple[dict, dict[str, Item], WeeklyMaterials]:
    """② の入力と、項目 ID → 項目、比較対象。write_weekly_input と ③ が同じものを作る。"""
    items = load_collected(run_dir / COLLECTED)
    ids = load_item_ids(run_dir)
    inputs = [InputItem(i, item) for i, item in zip(ids, items, strict=True)]
    materials = weekly_materials(config, target, db)
    data = build_weekly_input(
        target, inputs, load_inclusions(run_dir), source_names(config), materials
    )
    return data, dict(zip(ids, items, strict=True)), materials


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
    data, _, _ = _weekly_input(config, target, run_dir, db)
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


@dataclass(frozen=True)
class WeeklyVerified:
    """③ の結果。checked は検査の結果（掲載する項目と除外した項目）、report は作った本文。

    checked.output のテーマには、新しいテーマの番号を振ってある。parsed は AI が返した出力
    （checked.excluded の項目は、この中の項目そのもの。保存で AI の出力の順に並べるために使う）、
    observed は今週の観測の件数。
    """

    checked: WeeklyChecked
    report: Report
    parsed: WeeklyOutput
    observed: int


def verify_weekly(
    config: Config,
    target: Target,
    run_id: str,
    run_dir: Path,
    output: object,
    db: sqlite3.Connection | None = None,
) -> WeeklyVerified:
    """② の出力（agent-output.json の中身）を検査し、通った項目から本文を作る。

    出力全体がスキーマに合わなければ ValueError（D6(a)。不合格にするのは呼ぶ側）。
    新しいテーマの番号は、DB の最後の番号（この実行がすでに保存した分を除く）の次から振る
    （DB がなければ T0001 から）。
    """
    data, items, materials = _weekly_input(config, target, run_dir, db)
    parsed = parse_weekly_output(output)
    checked = check_weekly(
        parsed, data, items, {p.item_id: p.url for p in materials.past}, config.quote
    )
    first_new = next_theme_id(db, run_id) if db is not None else "T0001"
    numbered = WeeklyOutput(
        tuple(assign_theme_ids(checked.output.themes, first_new)),
        checked.output.tech_changes,
        checked.output.comparisons,
        checked.output.watch,
    )
    checked = replace(checked, output=numbered)
    report = render_weekly(
        target,
        run_id,
        numbered,
        items,
        source_names(config),
        PastWindow(materials.compare_weeks, config.weekly_compare_weeks, materials.past),
        observed=len(items),
        unavailable=unavailable_notes(run_dir, config),
    )
    return WeeklyVerified(checked, report, parsed, len(items))
