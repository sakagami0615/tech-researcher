"""日次・週次で共通の本文の部品（report-templates.md 0.3・0.4・4.1）。

出典はコードが ID から URL を引いて書く（D-06）。リンク文字列は設定の情報源名とドメインだけにし、
外部の記事のタイトルは使わない（0.3）。
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date
from urllib.parse import urlsplit

from tech_researcher.collect.aggregate import candidate_links
from tech_researcher.collect.records import Item, Record
from tech_researcher.collect.urls import normalize_url
from tech_researcher.periods import JST

_WEEKDAYS = "月火水木金土日"
_ID_VALUE = re.compile(r"[A-Za-z0-9]+(-[A-Za-z0-9]+)*")  # HTML コメントの中に -- を作らない（4.1）
# GitHub の owner は英数字と -、repo は英数字と . _ -（0.3）
_REPO = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")
# リンク先の中で Markdown のリンクを閉じうる記号。正規化した URL に残るのは ( ) だけ
_LINK_DEST = str.maketrans({"(": "%28", ")": "%29"})
SEPARATOR = " ／ "


@dataclass(frozen=True)
class Report:
    """投稿する Issue の中身。labels は D-08（4.2）。"""

    title: str
    body: str
    labels: tuple[str, ...]


def day_label(day: date) -> str:
    """`YYYY-MM-DD（曜）`（0.4）。"""
    return f"{day.isoformat()}（{_WEEKDAYS[day.weekday()]}）"


def identifier(cadence: str, target: date, run_id: str) -> str:
    """本文の1行目に置く重複防止用の識別子（4.1）。"""
    for value in (cadence, run_id):
        if not _ID_VALUE.fullmatch(value):
            raise ValueError(f"識別子に使えない値 {value!r}")
    return (
        f"<!-- tech-researcher kind=report cadence={cadence} target={target.isoformat()} "
        f"run={run_id} -->"
    )


def repo_code(full_name: str) -> str:
    """`owner/repo` の形だけを許し、インラインコードにする（0.3）。"""
    if not _REPO.fullmatch(full_name) or full_name.split("/")[1] in (".", ".."):
        raise ValueError(f"owner/repo の形でない {full_name!r}")
    return f"`{full_name}`"


@dataclass(frozen=True)
class _Source:
    url: str
    name: str | None  # 情報源名。候補リンクなど、情報源のレコードがない URL は None
    date: str | None


def source_line(
    item_ids: Sequence[str],
    items: Mapping[str, Item],
    names: Mapping[str, str],
    primary_item_id: str | None = None,
    primary_link_id: str | None = None,
) -> str:
    """出典の並び（1.3）。一次情報を先頭に印を付けて置き、残りは item_ids の順に並べる。

    - 一次情報は primary_link_id（候補リンク）を、なければ primary_item_id の項目の URL を使う
    - その URL の収集レコードがあれば、そのレコードを先頭に移す。なければドメインだけで書く
    - names は source_id から表示名への対応。同じ URL の出典は1つにする
    """
    sources: dict[str, _Source] = {}
    for item_id in item_ids:
        for record in items[item_id].sources:
            source = _from_record(record, names)
            if source is not None:
                sources.setdefault(source.url, source)
    primary = _primary_url(items, primary_item_id, primary_link_id)
    marked = []
    if primary is not None:
        first = sources.pop(primary, None) or _Source(primary, None, None)
        marked = [f"一次情報 {_format(first)}"]
    return SEPARATOR.join(marked + [_format(s) for s in sources.values()])


def _primary_url(items: Mapping[str, Item], item_id: str | None, link_id: str | None) -> str | None:
    if link_id is not None:  # 両方あれば候補リンクを優先する（data-model.md 4.2 の初期案）
        links = {
            link.link_id: link.url for i, it in items.items() for link in candidate_links(it, i)
        }
        return links[link_id]
    return None if item_id is None else items[item_id].url


def _from_record(record: Record, names: Mapping[str, str]) -> _Source | None:
    """URL を正規化できないレコードは、安全にリンクにできないので出典に書かない。"""
    url = normalize_url(record.url)
    if url is None:
        return None
    try:
        name = names[record.source_id]
    except KeyError:
        raise ValueError(f"情報源 {record.source_id} の表示名が設定にない") from None
    return _Source(url, name, _date(record))


def _date(record: Record) -> str | None:
    """公開日、なければ投稿日（0.4）。日付だけの公開は UTC の日付（時刻がないため）。"""
    if record.published_at is not None:
        zone = UTC if record.published_precision == "date" else JST
        return f"公開 {record.published_at.astimezone(zone).date().isoformat()}"
    if record.posted_at is not None:
        return f"投稿 {record.posted_at.astimezone(JST).date().isoformat()}"
    return None


def _format(source: _Source) -> str:
    domain = urlsplit(source.url).hostname
    text = domain if source.name is None else f"{source.name}（{domain}）"
    link = f"[{text}]({source.url.translate(_LINK_DEST)})"
    return link if source.date is None else f"{link} {source.date}"
