"""期間での絞り込みと、出来事ごとの判定の窓（data-model.md 2.4・2.5、D-13・D-16）。

収集レコードをまとめた後の収集項目で判定する。公開日時が分かるかどうかは、
項目にまとまったすべての出典を見て決めるため。
"""

from datetime import UTC, timedelta

from tech_researcher.collect.records import Event, Item
from tech_researcher.periods import (
    Period,
    Target,
    in_utc_date_window,
    last30days_window,
    spike_window,
)


def in_period(item: Item, target: Target) -> bool:
    """出来事が1つでもその判定の窓に入っていれば True。"""
    return any(event_in_window(item, e, target) for e in item.events)


def event_in_window(item: Item, event: Event, target: Target) -> bool:
    """出来事がその種類の判定の窓に入るか。

    - 公開・リリース・投稿・注目度の基準超え：対象期間。日付だけの公開と、日付だけの投稿での
      基準超え（last30days-skill の HN）は UTC の日付で判定する
    - 日付だけの公開は、時刻まで分かる公開日時がほかの出典にあれば使わない。HN の投稿の日付を
      公開日時として持つため、古い記事の新しい投稿で残ってしまうのを防ぐ（D-13）
    - 投稿で判定するのは、どの出典にも公開日時がないときだけ。公開が期間より前の記事は、
      期間内に投稿されても残さない（D-13）
    - 初登場：前日の取得と比べて判定した時点で、その対象日の出来事なので、時刻では判定しない。
      取得の時刻（翌日 6:00 の実行）は、窓（対象日 6:00〜翌日 6:00）の終わりより後になるため
    - Star の増分・新しい OSS：対象日 6:00〜翌日 6:00
    """
    period = target.period
    match event.type:
        case "published" if _date_only(item, event):
            return not _published_datetime_known(item) and _in_date_window(event, period)
        case "published" | "released":
            return period.contains(event.at)
        case "posted":
            published_known = any(r.published_at is not None for r in item.sources)
            return not published_known and period.contains(event.at)
        case "attention_spike" if event.basis == "threshold" and _date_only(item, event):
            return _in_date_window(event, period)
        case "attention_spike" if event.basis == "threshold":
            return period.contains(event.at)
        case "attention_spike" if event.basis == "feed_entry":
            return True
        case "attention_spike" | "new_repo":
            return spike_window(target).contains(event.at)
    raise ValueError(f"知らない出来事 {event.type}")


def published_before_period(item: Item, target: Target) -> bool:
    """公開が対象期間より前か（AI の入力の needs_period_event。2.5）。公開日時が不明なら False。"""
    # 時刻まで分かる公開日時を優先する（event_in_window と同じ）
    dated = [r for r in item.sources if r.published_at is not None]
    published = next((r for r in dated if r.published_precision == "datetime"), None) or next(
        iter(dated), None
    )
    if published is None:
        return False
    if published.published_precision == "date":
        days, as_of = last30days_window(target.period)
        return published.published_at.astimezone(UTC).date() < as_of - timedelta(days=days)
    return published.published_at < target.period.start


def _date_only(item: Item, event: Event) -> bool:
    """日付だけの記事（last30days-skill）の日時か。"""
    return any(r.published_precision == "date" and r.published_at == event.at for r in item.sources)


def _published_datetime_known(item: Item) -> bool:
    return any(r.published_precision == "datetime" for r in item.sources)


def _in_date_window(event: Event, period: Period) -> bool:
    return in_utc_date_window(event.at.astimezone(UTC).date(), *last30days_window(period))
