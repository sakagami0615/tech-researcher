"""週次の ① の材料：取り直した分と日次の収集結果を URL でまとめる（data-model.md 3.8、D-17）。

RSS などはフィードに載る期間が短く（D-12）、週次の時点で7日分を取り直せないので、対象日が調査期間に
入る日次の items を合わせる。

- 日次の掲載除外（hold_reason）は持ち込まない。AI に渡さなかった項目も、週次では観測として使う
  （D-13 の「注目が続いていることは、観測を DB に残して週次で扱う」）
- 同じ URL の行は1つにまとめる。出典・出来事・リンクは和集合、注目度は指標ごとに最新の値、
  代表のタイトル・抜粋・公開日時は時刻まで分かる公開日時を持つ行を優先する
- 日次の行は一次情報の本文を持たない（ハッシュだけ）。週次の AI にも本文は渡さない（4.3）
"""

import sqlite3
from collections.abc import Hashable, Iterable, Sequence
from dataclasses import replace
from datetime import date

from tech_researcher.collect.period_filter import window_events
from tech_researcher.collect.records import Attention, Event, GitHubRepo, Item
from tech_researcher.db.items import ItemRow, load_item_rows
from tech_researcher.periods import JST, Target
from tech_researcher.stages.collect_store import INCLUSION_ORDER, item_kind, item_row

_PRECISION_ORDER = ("datetime", "date", "unknown")  # 公開日時が詳しい順


def daily_days(target: Target) -> tuple[date, date]:
    """調査期間に入る日次の対象日：（最初の日、終わりの日。含まない）。JST の日付。"""
    return target.period.start.astimezone(JST).date(), target.period.end.astimezone(JST).date()


def load_daily_rows(conn: sqlite3.Connection, target: Target) -> list[ItemRow]:
    """DB にある、対象日が週次の調査期間に入る日次の items。"""
    first, end = daily_days(target)
    return load_item_rows(conn, "daily", first, end)


def weekly_rows(target: Target, fresh: Sequence[Item], daily: Iterable[ItemRow]) -> list[ItemRow]:
    """取り直した項目（期間で絞った後）と日次の行を、URL でまとめた週次の items の行。

    並びは、取り直した項目の順、続いて日次の行にだけある URL（日次の行の新しい対象日の順）。
    同じ URL の中では、取り直した項目、日次の行（新しい対象日から）の順に重ねる。
    """
    groups: dict[str, list[ItemRow]] = {i.url: [item_row(i, target)] for i in fresh}
    for row in sorted(daily, key=lambda r: r.target, reverse=True):
        groups.setdefault(row.item.url, []).append(row)
    return [_merge(target, rows) for rows in groups.values()]


def _merge(target: Target, rows: list[ItemRow]) -> ItemRow:
    base = min(rows, key=lambda r: _PRECISION_ORDER.index(r.published_precision))
    ordered = [base, *(r for r in rows if r is not base)]
    items = [r.item for r in ordered]
    events = _unique((e for i in items for e in i.events), lambda e: (e.type, _sec(e.at), e.basis))
    sources = _unique((s for i in items for s in i.sources), lambda s: (s.source_id, s.url))
    item = Item(
        url=base.item.url,
        sources=sources,
        events=events,
        attention=_latest_attention(a for i in items for a in i.attention),
        links=_unique((link for i in items for link in i.links), lambda x: x),
        meta_terms=_unique((t for i in items for t in i.meta_terms), lambda x: x),
        github=_merge_repo([r.item.github for r in rows]),
    )
    candidates = [*(r.inclusion for r in ordered), *window_events(item, target)]
    chosen = min(candidates, key=lambda e: (INCLUSION_ORDER[e.type], e.at))
    inclusion = next(e for e in events if _event_key(e) == _event_key(chosen))
    posted = [r.posted_at for r in ordered if r.posted_at is not None]
    return replace(
        base,
        cadence="weekly",
        target=target.key,
        item=item,
        kind=item_kind(item),
        title=base.title or next((r.title for r in ordered if r.title), ""),
        excerpt=next((r.excerpt for r in ordered if r.excerpt), None),
        inclusion=inclusion,
        posted_at=min(posted) if posted else None,
        primary_text=next((r.primary_text for r in ordered if r.primary_text is not None), None),
        hold_reason=None,
    )


def _sec(moment):
    # 保存した値は秒までなので、取り直した値と同じ出来事を、秒までで見分ける
    return moment.replace(microsecond=0)


def _event_key(e: Event) -> tuple:
    return e.type, _sec(e.at), e.basis


def _unique[T](values: Iterable[T], key) -> tuple[T, ...]:
    found: dict[Hashable, T] = {}
    for v in values:
        found.setdefault(key(v), v)
    return tuple(found.values())


def _merge_repo(values: Sequence[GitHubRepo | None]) -> GitHubRepo | None:
    """rows の順（取り直した分、新しい日次の順）で最初の値を使い、言及元は全部の和集合にする。"""
    found = [g for g in values if g is not None]
    if not found:
        return None
    mentions = _unique((u for g in found for u in g.mentioned_by), lambda u: u)
    return replace(found[0], mentioned_by=mentions)


def _latest_attention(values: Iterable[Attention]) -> tuple[Attention, ...]:
    """指標ごとに、測定が最も新しい値。同じ時刻なら先のもの。並びは指標を最初に見た順。"""
    latest: dict[str, Attention] = {}
    for a in values:
        old = latest.get(a.metric)
        if old is None or a.measured_at > old.measured_at:
            latest[a.metric] = a
    return tuple(latest.values())
