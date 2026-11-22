"""週次レポート Issue のタイトルと本文（report-templates.md 2章）。

AI の部品（ThemeEntry など）を、コードが枠・観測数・出典・定型文と組み合わせて本文にする（0.2）。
観測数（今週・前週・過去N週平均）は、AI が選んだ `item_ids`・`past_ids` をコードが週ごとに数える
（2.2）。AI のテキストは sanitize_text で無害化する（0.3）。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from tech_researcher.agent.weekly_output import (
    ComparisonEntry,
    ComparisonRow,
    TechChangeEntry,
    ThemeEntry,
    WatchEntry,
    WeeklyOutput,
)
from tech_researcher.collect.records import Item
from tech_researcher.collect.urls import normalize_url
from tech_researcher.db.weekly import PastObservation
from tech_researcher.periods import JST, Target
from tech_researcher.render.common import (
    Report,
    day_label,
    identifier,
    repo_code,
    source_line,
    source_link,
    unavailable_section,
)
from tech_researcher.render.sanitize import sanitize_text

LABELS = ("type:report", "cadence:weekly")
FOOTER = (
    "このレポートは tech-researcher が自動生成しました。"
    "見逃し・誤検出はこの Issue にコメントしてください。"
)
NO_ITEMS = "今週は該当なし"
NO_PAST = "比較できる過去の観測なし"
MAX_SOURCES = 5  # テーマの主な出典の上限（2.3）
# 状態の表示（AI の出力値はコードが変換する。2.4）と、表・本文での並び順
STATUS_LABELS = {"rising": "🔺台頭", "steady": "➡️継続", "fading": "🔻沈静"}
STATUS_SECTIONS = {
    "rising": "台頭したテーマ",
    "steady": "継続して注目されているテーマ",
    "fading": "注目が落ち着いたテーマ",
}
_DASH = "—"


def theme_display_key(theme: ThemeEntry) -> int:
    """テーマの並び（状態の固定順）。sorted は安定なので、同じ状態では AI の出力の順を保つ。"""
    return list(STATUS_LABELS).index(theme.status)


@dataclass(frozen=True)
class PastWindow:
    """比較の窓。weeks は週次の items がある過去の週（対象週の月曜。新しい順。ない週は外してある）、
    size は設定の窓の長さ（見出しの「過去N週平均」の N）、observations は渡した過去の観測。"""

    weeks: Sequence[date]
    size: int
    observations: Sequence[PastObservation] = ()


@dataclass(frozen=True)
class ThemeCounts:
    """テーマの観測数。last_week は前週が窓にない（データなし）とき、average は窓が空なら None。"""

    this_week: int
    last_week: int | None
    average: float | None


def count_observations(theme: ThemeEntry, target: Target, window: PastWindow) -> ThemeCounts:
    """今週は `item_ids`、前週と過去N週平均は `past_ids` を、観測の週ごとに数える。

    平均は、窓に入った（週次の items がある）週の数で割る。データなしの週は分母に入れない。
    """
    weeks = {o.item_id: o.week for o in window.observations}
    per_week = [weeks[i] for i in theme.past_ids if weeks.get(i) in window.weeks]
    previous = target.key - timedelta(weeks=1)
    return ThemeCounts(
        len(theme.item_ids),
        per_week.count(previous) if previous in window.weeks else None,
        len(per_week) / len(window.weeks) if window.weeks else None,
    )


def render_weekly(
    target: Target,
    run_id: str,
    output: WeeklyOutput,
    items: Mapping[str, Item],
    names: Mapping[str, str],
    window: PastWindow,
    observed: int,
    unavailable: Sequence[tuple[str, str]] = (),
) -> Report:
    """検証を通った出力から週次のレポート Issue を作る。

    - output は検査（check_weekly）を通った項目。新しいテーマの番号は振っても振らなくてもよい
    - items は今週の観測（項目 ID → 項目）、names は source_id から表示名への対応
    - observed は今週の観測の件数、unavailable は取得できなかった情報源の (表示名, 理由の分類)
    """
    themes = sorted(output.themes, key=theme_display_key)
    counts = [count_observations(t, target, window) for t in themes]
    sections = [
        _header(target, run_id, window, observed),
        _theme_table(themes, counts, window),
        *_theme_sections(themes, counts, items, names, window),
        _changes(output.tech_changes, items, names),
        _comparisons(output.comparisons, items, names),
        _watch(output.watch, items, names),
        f"---\n{FOOTER}",
    ]
    if section := unavailable_section(unavailable):
        sections.append(section)
    return Report(_title(target), "\n\n".join(sections) + "\n", LABELS)


def _title(target: Target) -> str:
    """`[週次] 2026-10-26（月）号 技術トレンド（10/19〜10/25）`。年をまたぐ期間は年を付ける。"""
    first, last = _period_days(target)
    fmt = "%Y/%m/%d" if first.year != last.year else "%m/%d"
    return f"[週次] {day_label(target.key)}号 技術トレンド（{first:{fmt}}〜{last:{fmt}}）"


def _period_days(target: Target) -> tuple[date, date]:
    """調査期間の最初の日と最後の日（JST。期間は終わりを含まない）。"""
    start = target.period.start.astimezone(JST).date()
    end = target.period.end.astimezone(JST).date()
    return start, end - timedelta(days=1)


def _header(target: Target, run_id: str, window: PastWindow, observed: int) -> str:
    first, last = _period_days(target)
    return "\n".join(
        [
            identifier("weekly", target.key, run_id),
            f"> **調査期間**：{day_label(first)}〜 {day_label(last)}（JST）",
            f"> **比較に使った過去の観測**：{_past_range(target, window)}",
            f"> **今週の観測**：{observed:,} 件",
        ]
    )


def _past_range(target: Target, window: PastWindow) -> str:
    """窓の最初の日〜最後の日（N 週分）。週の月曜 W の観測期間は W−期間の長さ〜W−1 日。"""
    if not window.weeks:
        return "なし"
    days = (target.period.end - target.period.start).days
    start = min(window.weeks) - timedelta(days=days)
    end = max(window.weeks) - timedelta(days=1)
    return f"{start.isoformat()} 〜 {end.isoformat()}（{len(window.weeks)} 週分）"


def _theme_table(
    themes: Sequence[ThemeEntry], counts: Sequence[ThemeCounts], window: PastWindow
) -> str:
    if not themes:
        return f"## テーマの動き\n\n{NO_ITEMS}"
    lines = [
        "## テーマの動き",
        "",
        f"| テーマ | 状態 | 今週 | 前週 | 過去{window.size}週平均 |",
        "|---|---|---|---|---|",
    ]
    for theme, c in zip(themes, counts, strict=True):
        lines.append(
            f"| {sanitize_text(theme.name)} | {STATUS_LABELS[theme.status]} | {c.this_week} |"
            f" {_last_week(c)} | {_average(c)} |"
        )
    return "\n".join(lines)


def _last_week(c: ThemeCounts) -> str:
    return _DASH if c.last_week is None else str(c.last_week)


def _average(c: ThemeCounts) -> str:
    return _DASH if c.average is None else f"{c.average:.1f}"


def _theme_sections(
    themes: Sequence[ThemeEntry],
    counts: Sequence[ThemeCounts],
    items: Mapping[str, Item],
    names: Mapping[str, str],
    window: PastWindow,
) -> list[str]:
    sections = []
    for status, heading in STATUS_SECTIONS.items():
        group = [(t, c) for t, c in zip(themes, counts, strict=True) if t.status == status]
        body = [_theme(t, c, items, names, window) for t, c in group] or [NO_ITEMS]
        sections.append("\n\n".join([f"## {heading}", *body]))
    return sections


def _theme(
    theme: ThemeEntry,
    counts: ThemeCounts,
    items: Mapping[str, Item],
    names: Mapping[str, str],
    window: PastWindow,
) -> str:
    if window.weeks:
        label = f"過去{window.size}週平均"
        numbers = (
            f"今週 {counts.this_week} 件／前週 {_last_week_text(counts)}／"
            f"{label} {_average(counts)} 件"
        )
        comparison = f"{sanitize_text(theme.comparison)}（{numbers}）"
    else:
        comparison = NO_PAST  # 比べる過去がないので、AI の比較の文は使わない
    source_label = "主な出典（最後に観測した記事）" if theme.status == "fading" else "主な出典"
    return "\n".join(
        [
            f"### {sanitize_text(theme.name)}",
            "",
            sanitize_text(theme.change),
            "",
            f"- **過去の観測との比較**：{comparison}",
            f"- **{source_label}**：{_theme_sources(theme, items, names, window)}",
        ]
    )


def _last_week_text(c: ThemeCounts) -> str:
    return _DASH if c.last_week is None else f"{c.last_week} 件"


def _theme_sources(
    theme: ThemeEntry, items: Mapping[str, Item], names: Mapping[str, str], window: PastWindow
) -> str:
    """AI が選んだ今週の観測の順に最大5件。今週の観測がなければ、最後に観測した過去の記事。"""
    if theme.item_ids:
        return source_line(theme.item_ids, items, names, limit=MAX_SOURCES)
    return _past_sources(theme.past_ids, names, window)


def _past_sources(past_ids: Sequence[str], names: Mapping[str, str], window: PastWindow) -> str:
    """過去の観測の出典。新しい週から最大5件。日付は持たない（週だけが分かる）。"""
    found = {o.item_id: o for o in window.observations}
    chosen = sorted((found[i] for i in past_ids), key=lambda o: o.week, reverse=True)
    links = []
    for o in chosen:
        if (url := normalize_url(o.url)) is not None:
            links.append(source_link(url, names.get(o.source_id, o.source_id)))
        if len(links) == MAX_SOURCES:
            break
    return " ／ ".join(links)


def _changes(
    changes: Sequence[TechChangeEntry], items: Mapping[str, Item], names: Mapping[str, str]
) -> str:
    lines = [
        f"- **{sanitize_text(c.headline)}**：{sanitize_text(c.description)}\n"
        f"  出典：{source_line(c.item_ids, items, names)}"
        for c in changes
    ]
    return "## 重要な技術的変化\n\n" + ("\n".join(lines) or NO_ITEMS)


def _comparisons(
    comparisons: Sequence[ComparisonEntry], items: Mapping[str, Item], names: Mapping[str, str]
) -> str:
    bodies = [b for c in comparisons if (b := _comparison(c, items, names)) is not None]
    return "## OSS・ツールの比較\n\n" + ("\n\n".join(bodies) or NO_ITEMS)


def _comparison(
    comparison: ComparisonEntry, items: Mapping[str, Item], names: Mapping[str, str]
) -> str | None:
    rows = [r for row in comparison.rows if (r := _row(row, items, names)) is not None]
    if not rows:
        return None
    return "\n".join(
        [
            f"### {sanitize_text(comparison.domain)}",
            "",
            "| 名前 | 特徴 | 向いている用途 | 出典 |",
            "|---|---|---|---|",
            *rows,
            "",
            sanitize_text(comparison.summary),
        ]
    )


def _row(row: ComparisonRow, items: Mapping[str, Item], names: Mapping[str, str]) -> str | None:
    """GitHub のリポジトリは `owner/repo`（コードが付ける）、それ以外は AI の製品名（2.4）。
    名前を決められない行（形の合わない owner/repo、製品名なし）は載せない。"""
    item = items[row.item_id]
    try:
        if item.github is not None:
            name = repo_code(item.github.full_name)
        elif row.product_name:
            name = sanitize_text(row.product_name)
        else:
            return None
    except ValueError:
        return None
    sources = source_line([row.item_id], items, names)
    return f"| {name} | {sanitize_text(row.feature)} | {sanitize_text(row.use_case)} | {sources} |"


def _watch(watch: Sequence[WatchEntry], items: Mapping[str, Item], names: Mapping[str, str]) -> str:
    lines = [
        f"- **{sanitize_text(w.name)}**：{sanitize_text(w.reason)}\n"
        f"  出典：{source_line(w.item_ids, items, names)}"
        for w in watch
    ]
    return "## 今後注目すべきテーマ\n\n" + ("\n".join(lines) or NO_ITEMS)
