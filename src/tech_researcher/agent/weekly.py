"""週次の ② 評価・要約への入力（data-model.md 4.3、D-06・D-17・D-18）。

今週の観測（週次の items）と、比較の窓の過去の観測・過去のテーマを渡す。外部由来の自由文は
各項目の untrusted の中にだけ入れる。一次情報の本文は渡さない（テーマ単位の分析に日次ほどの深さは
要らず、入力の量を抑えるため）。出力のスキーマと検査は ② の週次の Issue で足す。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from tech_researcher.agent.daily import InputItem
from tech_researcher.collect.period_filter import window_events
from tech_researcher.collect.records import Event, Item
from tech_researcher.collect.urls import normalize_url
from tech_researcher.db.history import PastTheme
from tech_researcher.db.weekly import PastObservation
from tech_researcher.periods import JST, Target
from tech_researcher.stages.collect_store import INCLUSION_ORDER, item_kind, representative

# 日次の増分は日ごとの値で、週次には合わない。期間の合計を github の star_delta_period に入れる
_DAILY_ONLY_METRICS = frozenset({"star_delta_24h"})


@dataclass(frozen=True)
class WeeklyMaterials:
    """週次の入力に加える、DB から引いた比較対象。

    compare_weeks は週次の items がある週（対象週の月曜。新しい順）。ない週は「データなし」として
    外してある。star_gains は `owner/repo`（小文字）→ 期間の Star の増分（日次の増分の合計）。
    """

    compare_weeks: Sequence[date] = ()
    past: Sequence[PastObservation] = ()
    themes: Sequence[PastTheme] = ()
    star_gains: Mapping[str, int] | None = None


def build_weekly_input(
    target: Target,
    inputs: Sequence[InputItem],
    display_names: Mapping[str, str],
    materials: WeeklyMaterials | None = None,
) -> dict:
    """② に渡す入力。display_names は source_id から表示名への対応。"""
    ids_by_url = {i.item.url: i.item_id for i in inputs}
    materials = materials or WeeklyMaterials()
    gains = materials.star_gains or {}
    return {
        "task": "weekly",
        "target": {
            "week": target.key.isoformat(),
            "period": {
                "start": target.period.start.astimezone(JST).isoformat(),
                "end": target.period.end.astimezone(JST).isoformat(),
            },
            "compare_weeks": [w.isoformat() for w in materials.compare_weeks],
        },
        "items": [_item(i, target, display_names, ids_by_url, gains) for i in inputs],
        "past_observations": [
            {
                "id": p.item_id,
                "week": p.week.isoformat(),
                "source": display_names.get(p.source_id, p.source_id),
                "kind": p.kind,
                "untrusted": {"title": p.title},
            }
            for p in materials.past
        ],
        "past_themes": [
            {
                "theme_id": t.theme_id,
                "name": t.name,
                "history": [
                    {"week": week.isoformat(), "status": status, "count": count}
                    for week, status, count in t.history
                ],
            }
            for t in materials.themes
        ],
    }


def _item(
    entry: InputItem,
    target: Target,
    names: Mapping[str, str],
    ids_by_url: Mapping[str, str],
    gains: Mapping[str, int],
) -> dict:
    item, first = entry.item, entry.item.sources[0]
    rep = representative(item)  # items の代表の値と同じ出典（週次の items と食い違わせない）
    event = _event(item, target)
    github = item.github
    return {
        "id": entry.item_id,
        "date": event.at.astimezone(JST).date().isoformat(),
        "source": names.get(first.source_id, first.source_id),
        "kind": item_kind(item),
        "event": {"type": event.type},
        "attention": [
            {"metric": a.metric, "value": a.value}
            for a in item.attention
            if a.metric not in _DAILY_ONLY_METRICS
        ],
        "github": None
        if github is None
        else {
            "full_name": github.full_name,
            "created_at": github.created_at.astimezone(JST).isoformat(),
            "stars_total": github.stars_total,
            "star_delta_period": gains.get(github.full_name.lower()),
            "mentioned_by": list(
                dict.fromkeys(
                    ids_by_url[u] for u in map(_normalized, github.mentioned_by) if u in ids_by_url
                )
            ),
        },
        "untrusted": {"title": rep.title or _title(item), "excerpt": rep.excerpt or _excerpt(item)},
    }


def _event(item: Item, target: Target) -> Event:
    """掲載の根拠とする出来事（items の inclusion と同じ順位。2.4）。判定の窓に入るものから選び、
    なければ（日次の行に由来する項目など）すべての出来事から選ぶ。"""
    events = window_events(item, target) or item.events
    return min(events, key=lambda e: (INCLUSION_ORDER[e.type], e.at))


def _normalized(url: str) -> str:
    return normalize_url(url) or ""


def _title(item: Item) -> str:
    return next((r.title for r in item.sources if r.title), "")


def _excerpt(item: Item) -> str | None:
    return next((r.excerpt for r in item.sources if r.excerpt), None)
