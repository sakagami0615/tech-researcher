import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from tech_researcher.collect.aggregate import aggregate
from tech_researcher.collect.records import Attention, Event, GitHubRepo, Record
from tech_researcher.db.items import ItemRow, item_ref, upsert_items
from tech_researcher.db.runs import start_run
from tech_researcher.db.schema import open_db
from tech_researcher.periods import daily_target

RUN_ID = "20261024-060000-abcd"
RETRY_ID = "20261024-080000-0123"
TARGET = daily_target(date(2026, 10, 24), 1)
STARTED = datetime(2026, 10, 23, 21, 0, tzinfo=UTC)
PUBLISHED = datetime(2026, 10, 23, 3, 0, tzinfo=UTC)
POSTED = datetime(2026, 10, 23, 5, 0, tzinfo=UTC)
# 本文・タイトルに入れる目印。DB のファイルに残っていないことを確かめる
BODY = "一次情報の本文の目印-7c1f9e"
TITLE = "外部のタイトルの目印-52ab"


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "tech-researcher.sqlite3"


@pytest.fixture
def conn(db_path):
    conn = open_db(db_path)
    start_run(conn, RUN_ID, TARGET, STARTED)
    start_run(conn, RETRY_ID, TARGET, STARTED + timedelta(hours=2))
    yield conn
    conn.close()


def records(url="https://example.com/post"):
    published = Event("published", PUBLISHED)
    return (
        Record(
            source_id="rss:a",
            url=url,
            title=TITLE,
            published_precision="datetime",
            fetched_at=STARTED,
            events=(published,),
            excerpt="抜粋",
            primary_text=BODY,
            primary_text_origin="rss_full",
            published_at=PUBLISHED,
            links=("https://docs.example.com/a", "https://docs.example.com/b"),
        ),
        Record(
            source_id="hackernews",
            url="https://news.ycombinator.com/item?id=1",
            link_url=url,
            title="HN のタイトル",
            published_precision="unknown",
            fetched_at=STARTED,
            events=(Event("posted", POSTED),),
            posted_at=POSTED,
            attention=(Attention("hn_points", 120, STARTED),),
            meta_terms=("owner/repo",),
            github=GitHubRepo("owner/repo", PUBLISHED, 1500, 300, ("https://example.com/post",)),
        ),
    )


def row(url="https://example.com/post", **overrides):
    (item,) = aggregate(records(url))
    values = {
        "cadence": "daily",
        "target": TARGET.key,
        "item": item,
        "kind": "article",
        "title": TITLE,
        "published_precision": "datetime",
        "inclusion": item.events[0],
        "excerpt": "抜粋",
        "published_at": PUBLISHED,
        "posted_at": POSTED,
        "primary_text": BODY,
    }
    return ItemRow(**(values | overrides))


def item_rows(conn):
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM items ORDER BY item_id")]
    conn.row_factory = None
    return rows


def test_writes_columns(conn):
    (ref,) = upsert_items(conn, RUN_ID, [row()])
    (saved,) = item_rows(conn)
    assert ref == f"i{saved['item_id']}"
    assert {k: v for k, v in saved.items() if not k.endswith("_json")} == {
        "item_id": saved["item_id"],
        "cadence": "daily",
        "target": "2026-10-23",
        "canonical_url": "https://example.com/post",
        "kind": "article",
        "title": TITLE,
        "excerpt": "抜粋",
        "published_at": "2026-10-23T03:00:00Z",
        "published_precision": "datetime",
        "posted_at": "2026-10-23T05:00:00Z",
        "inclusion_event": "published",
        "inclusion_basis": None,
        "inclusion_at": "2026-10-23T03:00:00Z",
        "primary_text_hash": hashlib.sha256(BODY.encode()).hexdigest(),
        "primary_text_len": len(BODY),
        "hold_reason": None,
        "first_run_id": RUN_ID,
        "last_run_id": RUN_ID,
    }
    assert json.loads(saved["events_json"]) == [
        {"type": "published", "at": "2026-10-23T03:00:00Z", "basis": None},
        {"type": "posted", "at": "2026-10-23T05:00:00Z", "basis": None},
    ]
    assert json.loads(saved["attention_json"]) == [
        {"metric": "hn_points", "value": 120, "measured_at": "2026-10-23T21:00:00Z"}
    ]
    # 重複判定結果：まとめた元のレコード（情報源・URL・投稿日時・取得日時）
    assert json.loads(saved["sources_json"]) == [
        {
            "source_id": "rss:a",
            "url": "https://example.com/post",
            "posted_at": None,
            "fetched_at": "2026-10-23T21:00:00Z",
        },
        {
            "source_id": "hackernews",
            "url": "https://news.ycombinator.com/item?id=1",
            "posted_at": "2026-10-23T05:00:00Z",
            "fetched_at": "2026-10-23T21:00:00Z",
        },
    ]
    # 候補リンクの ID（i…-l1, -l2）の元になるので、順序を保つ
    assert json.loads(saved["links_json"]) == [
        "https://docs.example.com/a",
        "https://docs.example.com/b",
    ]
    assert json.loads(saved["github_json"]) == {
        "full_name": "owner/repo",
        "created_at": "2026-10-23T03:00:00Z",
        "stars_total": 1500,
        "star_delta_24h": 300,
        "mentioned_by": ["https://example.com/post"],
    }
    assert json.loads(saved["meta_terms_json"]) == ["owner/repo"]


def test_does_not_store_primary_text(conn, db_path):
    """本文は DB に保存しない（D-18）。DB のファイル（WAL を含む）のどこにも残らない。"""
    upsert_items(conn, RUN_ID, [row()])
    conn.close()  # 最後の接続を閉じると WAL が DB に書き戻される
    data = b"".join(p.read_bytes() for p in db_path.parent.iterdir())
    assert BODY.encode() not in data
    assert TITLE.encode() in data  # 確かめ方が正しいことの対照（タイトルは保存する）


def test_upsert_keeps_id(conn):
    """同じ対象の再実行は同じ行を更新し、ID を変えない（3.7、2.6）。"""
    first = upsert_items(conn, RUN_ID, [row(), row("https://example.com/other")])
    again = upsert_items(
        conn,
        RETRY_ID,
        [
            row("https://example.com/new"),
            row(title="新しいタイトル", primary_text=None, hold_reason="already_featured"),
        ],
    )
    assert again[1] == first[0]
    assert again[0] not in first
    saved = {r["canonical_url"]: r for r in item_rows(conn)}
    assert len(saved) == 3
    updated = saved["https://example.com/post"]
    assert (updated["title"], updated["hold_reason"]) == ("新しいタイトル", "already_featured")
    assert (updated["primary_text_hash"], updated["primary_text_len"]) == (None, None)
    assert (updated["first_run_id"], updated["last_run_id"]) == (RUN_ID, RETRY_ID)
    # 今回の実行で更新しなかった行は、last_run_id が前のまま（日次で AI に渡さない）
    assert saved["https://example.com/other"]["last_run_id"] == RUN_ID


def test_same_url_in_other_target_is_another_item(conn):
    (daily,) = upsert_items(conn, RUN_ID, [row()])
    (next_day,) = upsert_items(conn, RUN_ID, [row(target=date(2026, 10, 24))])
    (weekly,) = upsert_items(conn, RUN_ID, [row(cadence="weekly", target=date(2026, 10, 26))])
    assert len({daily, next_day, weekly}) == 3


def test_batch_is_atomic(conn):
    """途中で失敗したら、その回の書き込みはすべて取り消す。"""
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        upsert_items(conn, "20261025-060000-ffff", [row()])  # runs にない実行
    assert item_rows(conn) == []

    def rows():
        yield row()
        raise RuntimeError("収集の途中の失敗")

    with pytest.raises(RuntimeError):
        upsert_items(conn, RUN_ID, rows())
    assert item_rows(conn) == []
    assert not conn.in_transaction


def test_spike_inclusion(conn):
    spike = Event("attention_spike", STARTED, "feed_entry")
    (item,) = aggregate(records())
    item = replace(item, events=(*item.events, spike))
    upsert_items(conn, RUN_ID, [row(item=item, inclusion=spike)])
    (saved,) = item_rows(conn)
    assert (saved["inclusion_event"], saved["inclusion_basis"], saved["inclusion_at"]) == (
        "attention_spike",
        "feed_entry",
        "2026-10-23T21:00:00Z",
    )


def test_dry_run_is_not_written(conn):
    with pytest.raises(ValueError, match="ドライラン"):
        upsert_items(conn, "dry-20261024-060000-abcd", [row()])
    assert item_rows(conn) == []


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"cadence": "monthly"}, "cadence"),
        ({"kind": "video"}, "kind"),
        ({"published_precision": "year"}, "published_precision"),
        ({"published_at": None}, "unknown"),
        ({"published_precision": "unknown"}, "unknown"),
        ({"inclusion": Event("released", PUBLISHED)}, "inclusion"),
    ],
)
def test_row_validation(overrides, message):
    with pytest.raises(ValueError, match=message):
        row(**overrides)


def test_item_ref():
    assert item_ref(48213) == "i48213"
