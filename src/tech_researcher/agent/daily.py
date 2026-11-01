"""日次の ② 評価・要約への入力と、その出力（data-model.md 4.1・4.2、D-06・D-18）。

外部由来の自由文は各項目の untrusted の中にだけ入れ、出典は ID で受け取る。
AI の呼び出しは evaluate（入力の dict を受け、構造化出力の dict を返す関数）に閉じ、
テストでは偽の実装に差し替える。正式なスキーマの検査は後の Issue で足す。
"""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from tech_researcher.collect.aggregate import candidate_links
from tech_researcher.collect.period_filter import event_in_window, published_before_period
from tech_researcher.collect.records import Attention, Event, Item, Record
from tech_researcher.collect.urls import normalize_url
from tech_researcher.periods import JST, Target, spike_window

CATEGORIES = ("ai", "devtools", "cloud", "oss", "other")  # report-templates.md 1.2 の固定順
DEPTHS = ("full", "brief")

# 掲載の根拠とする出来事の優先順位（2.4）
_INCLUSION_ORDER = ("published", "released", "new_repo", "attention_spike", "posted")

Evaluate = Callable[[dict], dict]


class InputTooLarge(Exception):
    pass


@dataclass(frozen=True)
class InputItem:
    """AI に渡す1項目。item_id は items の主キーから作る ID（ドライランは仮の ID）。"""

    item_id: str
    item: Item


@dataclass(frozen=True)
class DailyEntry:
    """AI が返したレポートの1項目。文章の部品と、出典の ID を持つ。"""

    item_ids: tuple[str, ...]
    primary_item_id: str | None
    primary_link_id: str | None
    category: str
    rank: int
    depth: str
    headline: str
    points: tuple[str, ...]
    importance: str
    period_event: str | None


def inclusion_event(item: Item, target: Target) -> Event:
    """掲載の根拠とする出来事。判定の窓に入る出来事のうち、優先順位が最も高いもの（2.4）。"""
    events = [e for e in item.events if event_in_window(item, e, target)]
    if not events:
        raise ValueError(f"{item.url} に判定の窓に入る出来事がない")
    return min(events, key=lambda e: _INCLUSION_ORDER.index(e.type))


def build_daily_input(
    target: Target, inputs: Sequence[InputItem], display_names: Mapping[str, str]
) -> dict:
    """② に渡す入力。display_names は source_id から表示名への対応。"""
    ids_by_url = {i.item.url: i.item_id for i in inputs}
    window = spike_window(target)
    return {
        "task": "daily",
        "target": {
            "date": target.key.isoformat(),
            "period": _span(target.period.start, target.period.end),
            "spike_window": _span(window.start, window.end),
        },
        "categories": list(CATEGORIES),
        "items": [_input_item(i, target, display_names, ids_by_url) for i in inputs],
    }


def encode_input(data: dict, max_bytes: int) -> bytes:
    """標準入力に渡す JSON。上限を超えたら AI を呼ばずに実行エラーにする（D-21）。"""
    body = json.dumps(data, ensure_ascii=False).encode()
    if len(body) > max_bytes:
        raise InputTooLarge(f"② の入力が {len(body)} バイトで、上限 {max_bytes} を超えた")
    return body


def _input_item(
    entry: InputItem, target: Target, names: Mapping[str, str], ids_by_url: dict[str, str]
) -> dict:
    item, (first, *others) = entry.item, entry.item.sources
    event = inclusion_event(item, target)
    published = next((r for r in item.sources if r.published_at), None)
    github = item.github
    return {
        "id": entry.item_id,
        "source": _name(names, first),
        "kind": _kind(item, event),
        "published": _published(published),
        "published_precision": published.published_precision if published else "unknown",
        "event": {"type": event.type, "at": _iso(event.at)},
        "needs_period_event": published_before_period(item, target),
        "attention": _attention(first.attention),
        "other_sources": [
            {
                "source": _name(names, r),
                "posted": _iso(r.posted_at),
                "attention": _attention(r.attention),
            }
            for r in others
        ],
        "links": [
            {"id": link.link_id, "url": link.url, "domain": link.domain}
            for link in candidate_links(item, entry.item_id)
        ],
        "github": None
        if github is None
        else {
            "full_name": github.full_name,
            "created_at": _iso(github.created_at),
            "stars_total": github.stars_total,
            "star_delta_24h": github.star_delta_24h,
            "mentioned_by": _mention_ids(github.mentioned_by, ids_by_url),
        },
        "untrusted": {
            "title": first.title,
            "excerpt": first.excerpt,
            "primary_text": next((r.primary_text for r in item.sources if r.primary_text), None),
        },
    }


def _name(names: Mapping[str, str], record: Record) -> str:
    try:
        return names[record.source_id]
    except KeyError:
        raise ValueError(f"情報源 {record.source_id} の表示名が設定にない") from None


def _published(record: Record | None) -> str | None:
    if record is None:
        return None
    if record.published_precision == "date":
        return record.published_at.astimezone(UTC).date().isoformat()  # 時刻はないので日付だけ
    return _iso(record.published_at)


def _mention_ids(urls: Sequence[str], ids_by_url: dict[str, str]) -> list[str]:
    ids = (ids_by_url.get(normalize_url(u) or "") for u in urls)
    return list(dict.fromkeys(i for i in ids if i))


def _kind(item: Item, event: Event) -> str:
    """items の kind（data-model.md 3.4）。リポジトリ、掲載の根拠がリリースか投稿か、で分ける。"""
    if item.github is not None:
        return "repo"
    if event.type == "released":
        return "release"
    if event.type == "posted":
        return "post"
    return "article"


def _attention(values: Sequence[Attention]) -> list[dict]:
    return [{"metric": a.metric, "value": a.value} for a in values]


def _span(start: datetime, end: datetime) -> dict:
    return {"start": _iso(start), "end": _iso(end)}


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(JST).isoformat()


def evaluate_daily(evaluate: Evaluate, data: dict) -> list[DailyEntry]:
    """AI を呼び、出力を項目に分ける。形が崩れていれば ValueError（D6(a) の最小の形）。"""
    output = evaluate(data)
    if not isinstance(output, dict) or not isinstance(output.get("items"), list):
        raise ValueError("出力に items の配列がない")
    return [_entry(raw) for raw in output["items"]]


def _entry(raw: object) -> DailyEntry:
    if not isinstance(raw, dict):
        raise ValueError("items の要素がオブジェクトでない")
    entry = DailyEntry(
        item_ids=tuple(dict.fromkeys(_strings(raw, "item_ids"))),  # 項目の中の重複は1つにする
        primary_item_id=_optional_str(raw, "primary_item_id"),
        primary_link_id=_optional_str(raw, "primary_link_id"),
        category=_str(raw, "category"),
        rank=raw.get("rank"),
        depth=_str(raw, "depth"),
        headline=_str(raw, "headline"),
        points=tuple(_strings(raw, "points")),
        importance=_str(raw, "importance"),
        period_event=_optional_str(raw, "period_event"),
    )
    if entry.category not in CATEGORIES or entry.depth not in DEPTHS:
        raise ValueError("category か depth が決めた値でない")
    if type(entry.rank) is not int:
        raise ValueError("rank が整数でない")
    return entry


def _str(raw: dict, key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} が文字列でない")
    return value


def _optional_str(raw: dict, key: str) -> str | None:
    return None if raw.get(key) is None else _str(raw, key)


def _strings(raw: dict, key: str) -> list[str]:
    value = raw.get(key)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{key} が文字列の配列でない")
    return value
