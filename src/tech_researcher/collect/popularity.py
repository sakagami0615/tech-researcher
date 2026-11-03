"""GitHub 以外の注目の急上昇（D-13、data-model.md 2.4・3.4・3.5）。

2つの判定を分けて持ち、どちらも収集レコードに出来事 `attention_spike` を足すだけにする。
残すかどうかは、まとめた後に period_filter が出来事の判定の窓で決める。

- 初登場（basis = feed_entry）：人気型フィードの日次の取得記録（feed_snapshots）を前日の記録と
  比べ、前日になく今日ある記事。前日の取得の記録が丸ごとない情報源は判定せず、記録だけ行う
- 基準超え（basis = threshold）：注目度の値（HN のポイント、はてなのブックマーク数など）が
  設定の基準以上。前日の記録は使わない
"""

import sqlite3
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from tech_researcher.collect.aggregate import record_key
from tech_researcher.collect.records import Event, Record, SourceResult
from tech_researcher.db.feed_snapshots import (
    SnapshotEntry,
    fetch_recorded,
    load_feed_snapshot,
    record_feed_snapshot,
)
from tech_researcher.periods import Target


@dataclass(frozen=True)
class FeedEntryResult:
    """初登場の判定の結果。unjudged は前日の記録がなく判定しなかった人気型フィードの情報源 ID。"""

    records: tuple[Record, ...]
    unjudged: tuple[str, ...]


def judge_feed_entries(
    results: Iterable[SourceResult],
    popular: Collection[str],
    target: Target,
    conn: sqlite3.Connection | None,
    *,
    write: bool,
) -> FeedEntryResult:
    """人気型フィード（popular の情報源 ID）の取得を記録し、初登場の記事に出来事を足す。

    日次だけで行う（週次は日次の結果を使う。data-model.md 3.3）。write が False のとき
    （ドライラン）と conn が None のときは書かない。conn が None なら前日の記録もないとみなす。
    取得に失敗した情報源は、記録も判定もしない（取得できなかった情報源として別に扱われる）。
    """
    records: list[Record] = []
    unjudged: list[str] = []
    for result in results:
        source_id = result.source_id
        if source_id not in popular or target.cadence != "daily" or result.error is not None:
            records.extend(result.records)
            continue
        entries = snapshot_entries(result.records)
        if conn is not None and write:
            record_feed_snapshot(conn, source_id, target.key, entries)
        if conn is None or not fetch_recorded(conn, source_id, target.key - timedelta(days=1)):
            unjudged.append(source_id)
            records.extend(result.records)
            continue
        # 対象日の記録がすでにあれば（同じ対象の再実行・ドライラン）、最初の取得で判定する
        current = load_feed_snapshot(conn, source_id, target.key) or {
            e.url: e.fetched_at for e in entries
        }
        previous = load_feed_snapshot(conn, source_id, target.key - timedelta(days=1))
        records.extend(mark_feed_entries(result.records, first_appearances(current, previous)))
    return FeedEntryResult(tuple(records), tuple(unjudged))


def snapshot_entries(records: Iterable[Record]) -> tuple[SnapshotEntry, ...]:
    """取得記録にする全件（期間で絞る前）。同じ記事が重なれば、先に載っていた方を残す。

    キーは収集項目をまとめるときと同じ（指している先の正規化した URL）。順位はフィードの順。
    """
    entries: dict[str, SnapshotEntry] = {}
    for record in records:
        key = record_key(record)
        if key is not None and key not in entries:
            entries[key] = SnapshotEntry(key, len(entries) + 1, record.attention, record.fetched_at)
    return tuple(entries.values())


def first_appearances(
    current: Mapping[str, datetime], previous: Collection[str]
) -> dict[str, datetime]:
    """今日の記録にあって前日の記録にない記事を、記事の URL → 今日の取得時刻で返す。"""
    return {url: at for url, at in current.items() if url not in previous}


def mark_feed_entries(records: Iterable[Record], appeared: Mapping[str, datetime]) -> list[Record]:
    """初登場の記事のレコードに出来事を足す。日時は初登場を観測した取得の時刻（2.4）。"""
    marked = []
    for record in records:
        at = appeared.get(record_key(record) or "")
        if at is not None:
            record = _with_event(record, Event("attention_spike", at, "feed_entry"))
        marked.append(record)
    return marked


def mark_thresholds(records: Iterable[Record], thresholds: Mapping[str, int]) -> list[Record]:
    """注目度の値が基準以上のレコードに出来事を足す。基準のない metric では判定しない。

    日時は基準を超えた投稿の日時（2.4）。投稿日時のないレコードは判定しない。ただし日付だけの
    公開日時（last30days-skill の HN）は投稿の日付なので使う（data-model.md 2.8）。
    """
    marked = []
    for record in records:
        at = record.posted_at
        if at is None and record.published_precision == "date":
            at = record.published_at
        if at is not None and any(
            a.metric in thresholds and a.value >= thresholds[a.metric] for a in record.attention
        ):
            record = _with_event(record, Event("attention_spike", at, "threshold"))
        marked.append(record)
    return marked


def _with_event(record: Record, event: Event) -> Record:
    if event in record.events:
        return record
    return replace(record, events=(*record.events, event))
