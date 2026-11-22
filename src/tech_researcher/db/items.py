"""収集項目の保存：items（data-model.md 2.6・2.7・3.4・3.7、D-18）。

（cadence、target、canonical_url）をキーに upsert し、同じ対象の再試行・再実行でも行を増やさず、
項目の ID（主キー）を変えない。一次情報の本文は保存せず、ハッシュと文字数だけを残す。
"""

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from tech_researcher.collect.records import (
    PUBLISHED_PRECISIONS,
    Attention,
    Event,
    GitHubRepo,
    Item,
    Record,
)
from tech_researcher.db.runs import CADENCES, check_writable_run_id, utc_text
from tech_researcher.db.schema import transaction

KINDS = frozenset({"article", "repo", "release", "post"})


@dataclass(frozen=True)
class ItemRow:
    """items の1行にする値。item はまとめた収集項目で、canonical_url は item.url。

    代表のタイトル・抜粋・日時と、掲載の根拠とする出来事（inclusion。item.events の1つ）は、
    呼び出す側が item.sources から選ぶ。primary_text は保存せず、ハッシュと文字数にだけ使う。
    hold_reason は AI に渡さなかった理由（渡したものは None）。
    """

    cadence: str
    target: date
    item: Item
    kind: str
    title: str
    published_precision: str
    inclusion: Event
    excerpt: str | None = None
    published_at: datetime | None = None
    posted_at: datetime | None = None
    primary_text: str | None = None
    hold_reason: str | None = None
    # 出典ごとの公開日時・注目度を持つ行か。False は、それらを保存する前の形式の行（読み戻したとき
    # 出典の公開日時が分からず、期間の判定をやり直せない。保存してある inclusion を使う）
    dated_sources: bool = True

    def __post_init__(self):
        if self.cadence not in CADENCES:
            raise ValueError(f"知らない cadence {self.cadence}")
        if self.kind not in KINDS:
            raise ValueError(f"知らない kind {self.kind}")
        if self.published_precision not in PUBLISHED_PRECISIONS:
            raise ValueError(f"知らない published_precision {self.published_precision}")
        if (self.published_at is None) != (self.published_precision == "unknown"):
            raise ValueError("published_at がないときだけ published_precision は unknown")
        if self.inclusion not in self.item.events:
            raise ValueError("inclusion は item.events の1つにする")


def item_ref(item_id: int) -> str:
    """AI に渡す項目の ID（例：i48213。2.6）。"""
    return f"i{item_id}"


def upsert_items(conn: sqlite3.Connection, run_id: str, rows: Iterable[ItemRow]) -> list[str]:
    """1つのトランザクションで書き、rows の順に項目の ID を返す。

    すでにある行は値を置き換え、item_id と first_run_id はそのまま、last_run_id を今回にする。
    中でトランザクションを始めるので、`transaction` の中からは呼べない。
    """
    check_writable_run_id(run_id)
    with transaction(conn):
        return [item_ref(_upsert(conn, run_id, row)) for row in rows]


def lookup_item_refs(
    conn: sqlite3.Connection, cadence: str, target: date, urls: Iterable[str]
) -> dict[str, str]:
    """すでにある項目の URL → ID（i…）。ドライランが、本番の実行が書いた項目の ID を使うため。

    読むだけなので、読み取り専用の接続でも使える。
    """
    marks = {
        url: item_ref(item_id)
        for url, item_id in conn.execute(
            "SELECT canonical_url, item_id FROM items WHERE cadence = ? AND target = ?",
            (cadence, target.isoformat()),
        )
    }
    return {url: marks[url] for url in urls if url in marks}


_ROW_COLUMNS = (
    "target",
    "canonical_url",
    "kind",
    "title",
    "excerpt",
    "published_at",
    "published_precision",
    "posted_at",
    "inclusion_event",
    "inclusion_basis",
    "inclusion_at",
    "events_json",
    "attention_json",
    "sources_json",
    "links_json",
    "github_json",
    "meta_terms_json",
    "hold_reason",
)


def load_item_rows(conn: sqlite3.Connection, cadence: str, first: date, end: date) -> list[ItemRow]:
    """対象が first 以上 end 未満の行を ItemRow に戻す（週次の材料に日次の収集結果を使う。3.8）。

    対象の順、同じ対象の中では主キーの順。読むだけなので、読み取り専用の接続でも使える。
    保存していないもの（一次情報の本文、出典ごとのタイトル・抜粋）は戻らない。出典は重複判定結果の
    1件ごとに1つのレコードで、タイトル・抜粋は先頭のレコードに戻し、公開日時・精度・注目度は
    出典ごとに戻す。これらを保存する前の形式の行は dated_sources が False で、先頭の出典だけに
    代表の値・項目の注目度を戻す（期間の判定をやり直せないので、保存してある inclusion を使う）。
    """
    rows = conn.execute(
        f"SELECT {', '.join(_ROW_COLUMNS)} FROM items"
        " WHERE cadence = ? AND target >= ? AND target < ? ORDER BY target, item_id",
        (cadence, first.isoformat(), end.isoformat()),
    )
    return [_restore(cadence, dict(zip(_ROW_COLUMNS, row, strict=True))) for row in rows]


def _restore(cadence: str, r: dict) -> ItemRow:
    published_at = _time(r["published_at"])
    precision = r["published_precision"]
    stored = json.loads(r["sources_json"])
    dated = all("published_at" in s for s in stored)
    attention = tuple(
        Attention(a["metric"], a["value"], datetime.fromisoformat(a["measured_at"]))
        for a in json.loads(r["attention_json"])
    )
    records = tuple(
        Record(
            source_id=s["source_id"],
            url=s["url"],
            title=r["title"] if n == 0 else "",
            published_precision=_source_precision(s, precision if n == 0 else "unknown"),
            fetched_at=datetime.fromisoformat(s["fetched_at"]),
            events=(),
            excerpt=r["excerpt"] if n == 0 else None,
            published_at=_time(s["published_at"])
            if "published_at" in s
            else (published_at if n == 0 else None),
            posted_at=_time(s["posted_at"]),
            attention=_source_attention(s, attention, n),
        )
        for n, s in enumerate(stored)
    )
    github = None if r["github_json"] is None else json.loads(r["github_json"])
    item = Item(
        url=r["canonical_url"],
        sources=records,
        events=tuple(
            Event(e["type"], datetime.fromisoformat(e["at"]), e["basis"])
            for e in json.loads(r["events_json"])
        ),
        attention=attention,
        links=tuple(json.loads(r["links_json"])),
        meta_terms=tuple(json.loads(r["meta_terms_json"])),
        github=None
        if github is None
        else GitHubRepo(
            github["full_name"],
            datetime.fromisoformat(github["created_at"]),
            github["stars_total"],
            github["star_delta_24h"],
            tuple(github["mentioned_by"]),
        ),
    )
    return ItemRow(
        cadence=cadence,
        target=date.fromisoformat(r["target"]),
        item=item,
        kind=r["kind"],
        title=r["title"],
        published_precision=precision,
        inclusion=Event(
            r["inclusion_event"], datetime.fromisoformat(r["inclusion_at"]), r["inclusion_basis"]
        ),
        excerpt=r["excerpt"],
        published_at=published_at,
        posted_at=_time(r["posted_at"]),
        hold_reason=r["hold_reason"],
        dated_sources=dated,
    )


def _source_attention(source: dict, item_level: tuple[Attention, ...], n: int) -> tuple:
    # 出典ごとの注目度を持たない古い行は、項目の注目度を先頭の出典のものとして戻す
    if "attention" in source:
        return tuple(
            Attention(a["metric"], a["value"], datetime.fromisoformat(a["measured_at"]))
            for a in source["attention"]
        )
    return item_level if n == 0 else ()


def _source_precision(source: dict, fallback: str) -> str:
    # 出典ごとの公開日時を持たない古い行は、代表の値を先頭の出典だけに戻す
    return source.get("published_precision", fallback)


def _time(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)


_KEY = ("cadence", "target", "canonical_url")
# 再試行・再実行で置き換える列。first_run_id は最初の値を残す
_VALUES = (
    "kind",
    "title",
    "excerpt",
    "published_at",
    "published_precision",
    "posted_at",
    "inclusion_event",
    "inclusion_basis",
    "inclusion_at",
    "events_json",
    "attention_json",
    "sources_json",
    "links_json",
    "github_json",
    "meta_terms_json",
    "primary_text_hash",
    "primary_text_len",
    "hold_reason",
    "last_run_id",
)
_COLUMNS = (*_KEY, *_VALUES, "first_run_id")
# INSERT OR REPLACE は行を消して入れ直し、item_id が変わるので使わない
_UPSERT = (
    f"INSERT INTO items ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' * len(_COLUMNS))})"
    f" ON CONFLICT ({', '.join(_KEY)}) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in _VALUES)
    + " RETURNING item_id"
)


def _upsert(conn: sqlite3.Connection, run_id: str, row: ItemRow) -> int:
    item = row.item
    text = row.primary_text
    (item_id,) = conn.execute(
        _UPSERT,
        (
            row.cadence,
            row.target.isoformat(),
            item.url,
            row.kind,
            row.title,
            row.excerpt,
            _when(row.published_at),
            row.published_precision,
            _when(row.posted_at),
            row.inclusion.type,
            row.inclusion.basis,
            utc_text(row.inclusion.at),
            _json([_event(e) for e in item.events]),
            _json([_attention(a) for a in item.attention]),
            _json([_source(r) for r in item.sources]),
            _json(list(item.links)),
            None if item.github is None else _json(_github(item.github)),
            _json(list(item.meta_terms)),
            None if text is None else hashlib.sha256(text.encode()).hexdigest(),
            None if text is None else len(text),
            row.hold_reason,
            run_id,  # last_run_id
            run_id,  # first_run_id（新しい行のときだけ使われる）
        ),
    ).fetchone()
    return item_id


def _when(moment: datetime | None) -> str | None:
    return None if moment is None else utc_text(moment)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _event(e: Event) -> dict:
    return {"type": e.type, "at": utc_text(e.at), "basis": e.basis}


def _attention(a: Attention) -> dict:
    return {"metric": a.metric, "value": a.value, "measured_at": utc_text(a.measured_at)}


def _source(r: Record) -> dict:
    """重複判定結果として残す元のレコード（3.4）。外部由来の文（タイトル・本文）は入れない。"""
    return {
        "source_id": r.source_id,
        "url": r.url,
        "posted_at": _when(r.posted_at),
        "fetched_at": utc_text(r.fetched_at),
        "published_at": _when(r.published_at),
        "published_precision": r.published_precision,
        "attention": [_attention(a) for a in r.attention],
    }


def _github(g: GitHubRepo) -> dict:
    return {
        "full_name": g.full_name,
        "created_at": utc_text(g.created_at),
        "stars_total": g.stars_total,
        "star_delta_24h": g.star_delta_24h,
        "mentioned_by": list(g.mentioned_by),
    }
