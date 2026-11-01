"""収集レコードを収集項目にまとめ、候補リンクに ID を付ける（data-model.md 2.2・2.6、D-06）。"""

from collections.abc import Iterable
from urllib.parse import urlsplit

from tech_researcher.collect.records import CandidateLink, Item, Record
from tech_researcher.collect.urls import normalize_url


def aggregate(records: Iterable[Record]) -> list[Item]:
    """指している先の URL を正規化してまとめる（US-E1-03）。

    順序は各 URL を最初に見たレコードの順で、同じ入力なら同じ結果になる。
    link_url が正規化できなければ url でまとめる。どちらもできないレコードは捨てる。
    """
    groups: dict[str, list[Record]] = {}
    for record in records:
        key = normalize_url(record.link_url or "") or normalize_url(record.url)
        if key is not None:
            groups.setdefault(key, []).append(record)
    return [_merge(url, group) for url, group in groups.items()]


def _merge(url: str, group: list[Record]) -> Item:
    return Item(
        url=url,
        sources=tuple(group),
        events=_unique(e for r in group for e in r.events),
        attention=_unique(a for r in group for a in r.attention),
        links=_unique(
            link for r in group for link in map(normalize_url, r.links) if link is not None
        ),
        meta_terms=_unique(t for r in group for t in r.meta_terms),
        github=next((r.github for r in group if r.github is not None), None),
    )


def _unique[T](values: Iterable[T]) -> tuple[T, ...]:
    return tuple(dict.fromkeys(values))


def candidate_links(item: Item, item_id: str) -> tuple[CandidateLink, ...]:
    """候補リンクに `{項目 ID}-l{連番}` の ID を付ける（例：i48213-l2）。連番は1から。"""
    return tuple(
        CandidateLink(f"{item_id}-l{n}", url, urlsplit(url).hostname or "")
        for n, url in enumerate(item.links, start=1)
    )
