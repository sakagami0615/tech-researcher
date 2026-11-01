"""期間での絞り込みと、出来事ごとの判定の窓（data-model.md 2.4・2.5、D-13・D-16）。

収集レコードをまとめた後の収集項目で判定する。公開日時が分かるかどうかは、
項目にまとまったすべての出典を見て決めるため。
"""

from datetime import UTC, timedelta

from tech_researcher.collect.records import Event, Item
from tech_researcher.periods import Target, in_utc_date_window, last30days_window, spike_window


def in_period(item: Item, target: Target) -> bool:
    """出来事が1つでもその判定の窓に入っていれば True。"""
    return any(event_in_window(item, e, target) for e in item.events)


def event_in_window(item: Item, event: Event, target: Target) -> bool:
    """出来事がその種類の判定の窓に入るか。

    - 公開・リリース・投稿・注目度の基準超え：対象期間。日付だけの公開は UTC の日付で判定する
    - 投稿で判定するのは、どの出典にも公開日時がないときだけ。公開が期間より前の記事は、
      期間内に投稿されても残さない（D-13）
    - 初登場・Star の増分・新しい OSS：対象日 6:00〜翌日 6:00
    """
    period = target.period
    match event.type:
        case "published" if _date_only(item, event):
            return in_utc_date_window(event.at.astimezone(UTC).date(), *last30days_window(period))
        case "published" | "released":
            return period.contains(event.at)
        case "posted":
            published_known = any(r.published_at is not None for r in item.sources)
            return not published_known and period.contains(event.at)
        case "attention_spike" if event.basis == "threshold":
            return period.contains(event.at)
        case "attention_spike" | "new_repo":
            return spike_window(target).contains(event.at)
    raise ValueError(f"知らない出来事 {event.type}")


def published_before_period(item: Item, target: Target) -> bool:
    """公開が対象期間より前か（AI の入力の needs_period_event。2.5）。公開日時が不明なら False。"""
    published = next((r for r in item.sources if r.published_at is not None), None)
    if published is None:
        return False
    if published.published_precision == "date":
        days, as_of = last30days_window(target.period)
        return published.published_at.astimezone(UTC).date() < as_of - timedelta(days=days)
    return published.published_at < target.period.start


def _date_only(item: Item, event: Event) -> bool:
    """日付だけの記事（last30days-skill）の公開か。"""
    return any(r.published_precision == "date" and r.published_at == event.at for r in item.sources)
