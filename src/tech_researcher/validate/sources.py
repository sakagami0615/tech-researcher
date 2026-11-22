"""D1 出典の許可集合（D-07、data-model.md 4.2）。違反した項目は除外し、理由を残す。"""

from collections.abc import Iterable, Mapping, Sequence

from tech_researcher.agent.daily import DailyEntry
from tech_researcher.collect.aggregate import candidate_links
from tech_researcher.collect.records import Item
from tech_researcher.collect.urls import normalize_url
from tech_researcher.validate.ids import Excluded


def check_daily_sources(
    entries: Sequence[DailyEntry], items: Mapping[str, Item]
) -> tuple[list[DailyEntry], list[Excluded]]:
    """項目の出典 URL が、その実行で収集した項目の URL と候補リンクの集合にあるかを確かめる。

    URL は AI が返した ID からコードが付ける。entries は D6(b)（validate/ids.py）で ID の存在を
    確かめたものを渡す。ID から引いた URL が http・https として読め（読めなければ URL の形の
    不正）、正規化した形が許可集合にあることを見る。違反した項目は除外する。
    """
    links = {c.link_id: c.url for i, it in items.items() for c in candidate_links(it, i)}
    allowed = {u for it in items.values() for u in normalized_urls(item_urls(it))}
    allowed |= set(normalized_urls(links.values()))
    kept: list[DailyEntry] = []
    excluded: list[Excluded] = []
    for entry in entries:
        urls = [u for i in entry.item_ids for u in item_urls(items[i])]
        own = {c.link_id for i in entry.item_ids for c in candidate_links(items[i], i)}
        if entry.primary_link_id is not None:
            # 一次情報のリンクは自項目の候補リンクに限る（別の記事のリンクは出典にしない）
            urls.append(links[entry.primary_link_id] if entry.primary_link_id in own else "")
        if all((normalize_url(u) or "") in allowed for u in urls):
            kept.append(entry)
        else:
            excluded.append(Excluded(entry, "出典の URL が不正か、収集した URL の集合にない"))
    return kept, excluded


def item_urls(item: Item) -> list[str]:
    """項目の URL と、まとめた元の記事の URL。"""
    return [item.url, *(r.url for r in item.sources)]


def normalized_urls(urls: Iterable[str]) -> list[str]:
    return [n for u in urls if (n := normalize_url(u))]
