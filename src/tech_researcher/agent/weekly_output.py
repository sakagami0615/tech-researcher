"""週次の ② の出力（data-model.md 4.3、D-07・D-18）。

受け取った構造化出力をスキーマで検査してから、セクションごとの項目に分ける。ID と文章の検査は
validate/weekly.py、新しいテーマの番号の付与は assign_theme_ids で行う。
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace

from tech_researcher.validate.schema import WEEKLY_OUTPUT_SCHEMA, check_schema

THEME_STATUSES = ("rising", "steady", "fading")  # 台頭・継続・沈静（report-templates.md 2.4）


@dataclass(frozen=True)
class ThemeEntry:
    """テーマ。theme_id は続くテーマなら過去の番号、新しいテーマなら None（採番の前）。"""

    theme_id: str | None
    name: str
    status: str
    change: str
    comparison: str
    item_ids: tuple[str, ...]
    past_ids: tuple[str, ...]


@dataclass(frozen=True)
class TechChangeEntry:
    headline: str
    description: str
    item_ids: tuple[str, ...]


@dataclass(frozen=True)
class ComparisonRow:
    """比較表の1行。product_name は GitHub 外のときだけ AI が書く（GitHub はコードが付ける）。"""

    item_id: str
    product_name: str | None
    feature: str
    use_case: str


@dataclass(frozen=True)
class ComparisonEntry:
    domain: str
    rows: tuple[ComparisonRow, ...]
    summary: str


@dataclass(frozen=True)
class WatchEntry:
    name: str
    reason: str
    item_ids: tuple[str, ...]


@dataclass(frozen=True)
class WeeklyOutput:
    themes: tuple[ThemeEntry, ...]
    tech_changes: tuple[TechChangeEntry, ...]
    comparisons: tuple[ComparisonEntry, ...]
    watch: tuple[WatchEntry, ...]


def parse_weekly_output(output: object) -> WeeklyOutput:
    """AI の構造化出力を、スキーマで検査してから項目に分ける。合わなければ ValueError（D6(a)）。"""
    check_schema(output, WEEKLY_OUTPUT_SCHEMA)
    assert isinstance(output, dict)  # check_schema が通れば JSON のオブジェクト
    return WeeklyOutput(
        themes=tuple(
            ThemeEntry(
                theme_id=r["theme_id"],
                name=r["name"],
                status=r["status"],
                change=r["change"],
                comparison=r["comparison"],
                item_ids=_unique(r["item_ids"]),
                past_ids=_unique(r["past_ids"]),
            )
            for r in output["themes"]
        ),
        tech_changes=tuple(
            TechChangeEntry(r["headline"], r["description"], _unique(r["item_ids"]))
            for r in output["tech_changes"]
        ),
        comparisons=tuple(
            ComparisonEntry(
                r["domain"],
                tuple(
                    ComparisonRow(w["item_id"], w["product_name"], w["feature"], w["use_case"])
                    for w in r["rows"]
                ),
                r["summary"],
            )
            for r in output["comparisons"]
        ),
        watch=tuple(
            WatchEntry(r["name"], r["reason"], _unique(r["item_ids"])) for r in output["watch"]
        ),
    )


def assign_theme_ids(themes: Sequence[ThemeEntry], first_new_id: str) -> list[ThemeEntry]:
    """theme_id が None の新しいテーマに、first_new_id（db.history.next_theme_id）から番号を振る。

    検査を通ったテーマだけを渡す（除外したテーマに番号を使わない。番号は掲載したテーマだけが持つ）。
    続くテーマの番号はそのまま。新しいテーマには AI の出力の順に、番号を1つずつ増やして振る。
    """
    number = int(first_new_id[1:])
    result = []
    for theme in themes:
        if theme.theme_id is None:
            theme = replace(theme, theme_id=f"T{number:04d}")
            number += 1
        result.append(theme)
    return result


def _unique(ids: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(ids))  # 項目の中の重複は1つにする
