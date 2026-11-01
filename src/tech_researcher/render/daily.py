"""日次レポート Issue のタイトルと本文（report-templates.md 1章）。

AI の部品（DailyEntry）を、コードが枠・件数・出典・定型文と組み合わせて本文にする（0.2）。
AI のテキストの無害化（0.3）と本文の文字数の検査は、後の Issue で足す。
"""

from collections.abc import Mapping, Sequence

from tech_researcher.agent.daily import CATEGORIES, DailyEntry
from tech_researcher.collect.period_filter import published_before_period
from tech_researcher.collect.records import Item
from tech_researcher.periods import JST, Target
from tech_researcher.render.common import Report, day_label, identifier, source_line

CATEGORY_NAMES = dict(
    zip(
        CATEGORIES,
        (
            "生成AI・LLM・AIエージェント",
            "開発者ツール",
            "クラウド・インフラ",
            "OSS",
            "その他の新テーマ",
        ),
        strict=True,
    )
)
FEW_ITEMS = 5  # 「少ない日」のしきい値（1.5）。PoC で調整する
LABELS = ("type:report", "cadence:daily")
FOOTER = (
    "このレポートは tech-researcher が自動生成しました。見逃し・誤検出はこの Issue に"
    "コメントしてください（例：「項目2 は誤検出」「○○の発表が載っていない」）。"
)


def render_daily(
    target: Target,
    run_id: str,
    entries: Sequence[DailyEntry],
    items: Mapping[str, Item],
    names: Mapping[str, str],
    collected: int,
    unavailable: Sequence[tuple[str, str]] = (),
    unjudged: Sequence[str] = (),
) -> Report:
    """検証を通った項目からレポート Issue を作る。

    - items は AI に渡した項目 ID から収集項目への対応、names は source_id から表示名への対応
    - collected は収集件数（0件の日の定型文に書く）
    - unavailable は取得できなかった情報源の (表示名, 理由の分類)
    - unjudged は前日の記録がなく判定しなかった急上昇の判定の名前（1.6）
    """
    ordered = sorted(entries, key=lambda e: (CATEGORIES.index(e.category), e.rank))
    sections = [_header(target, run_id, len(ordered), unjudged)]
    if ordered:
        sections += [_toc(ordered), *_categories(ordered, target, items, names)]
    else:
        sections.append(
            "**該当なし**：対象期間に、掲載基準を満たす重要な技術情報はありませんでした"
            f"（収集 {collected} 件）。ジョブは正常に完了しています。"
        )
    sections.append(f"---\n{FOOTER}")
    if unavailable:
        lines = "\n".join(f"- {name}：{reason}" for name, reason in unavailable)
        sections.append(f"## 取得できなかった情報源\n\n{lines}")
    title = f"[日次] {day_label(target.key)}の技術動向"
    return Report(title, "\n\n".join(sections) + "\n", LABELS)


def _header(target: Target, run_id: str, count: int, unjudged: Sequence[str]) -> str:
    start, end = (t.astimezone(JST) for t in (target.period.start, target.period.end))
    lines = [
        identifier("daily", target.key, run_id),
        f"> **対象期間**：{day_label(start.date())}{start:%H:%M} 〜 "
        f"{day_label(end.date())}{end:%H:%M}（JST）",
        f"> **掲載**：{count} 件",
    ]
    if 0 < count < FEW_ITEMS:
        lines.append(f"> ℹ️ 重要な情報が少ない日でした（掲載 {count} 件）。")
    lines += [f"> ℹ️ {name}は、前日の記録がないため判定していません。" for name in unjudged]
    return "\n".join(lines)


def _toc(entries: Sequence[DailyEntry]) -> str:
    counts = [sum(e.category == c for e in entries) for c in CATEGORIES]
    lines = [f"- {CATEGORY_NAMES[c]}（{n} 件）" for c, n in zip(CATEGORIES, counts, strict=True)]
    return "## 目次\n\n" + "\n".join(lines)


def _categories(
    entries: Sequence[DailyEntry],
    target: Target,
    items: Mapping[str, Item],
    names: Mapping[str, str],
) -> list[str]:
    """カテゴリの固定順に見出しと項目を並べる。項目番号はカテゴリをまたいだ通し番号（1.2）。"""
    sections = []
    for category in CATEGORIES:
        group = [(n, e) for n, e in enumerate(entries, start=1) if e.category == category]
        if group:  # 0件のカテゴリは見出しごと省く
            sections.append(f"## {CATEGORY_NAMES[category]}（{len(group)} 件）")
            sections += [_entry(n, e, target, items, names) for n, e in group]
    return sections


def _entry(
    number: int,
    entry: DailyEntry,
    target: Target,
    items: Mapping[str, Item],
    names: Mapping[str, str],
) -> str:
    sources = source_line(
        entry.item_ids, items, names, entry.primary_item_id, entry.primary_link_id
    )
    event = _period_event(entry, target, items)
    heading = f"#### {number}. {entry.headline}"
    if entry.depth == "full":
        lines = ["- **要点**", *(f"  - {p}" for p in entry.points)]
        lines.append(f"- **重要性**：{entry.importance}")
        if event is not None:
            lines.append(f"- **期間内の出来事**：{event}")
        lines.append(f"- **出典**：{sources}")
    else:
        lines = [f"{' '.join(entry.points)} **重要性**：{entry.importance}"]
        if event is not None:
            lines.append(f"期間内の出来事：{event}")
        lines.append(f"出典：{sources}")
    return f"{heading}\n\n" + "\n".join(lines)


def _period_event(entry: DailyEntry, target: Target, items: Mapping[str, Item]) -> str | None:
    """期間前に公開された項目だけに出す「期間内の出来事」と、急上昇の数値（1.3）。"""
    group = [items[i] for i in entry.item_ids]
    if not any(published_before_period(item, target) for item in group):
        return None
    if entry.period_event is None:  # ③ で除外しているはずの項目（US-E7-01 の必須項目）
        raise ValueError(f"{entry.item_ids} に必要な期間内の出来事がない")
    delta = next(
        (
            i.github.star_delta_24h
            for i in group
            if i.github and i.github.star_delta_24h is not None
        ),
        None,
    )
    return (
        entry.period_event if delta is None else f"{entry.period_event}（Star {delta:+,}／24時間）"
    )
