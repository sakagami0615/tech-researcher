"""D6(b) ID の検査（D-07、data-model.md 4.2）。違反した項目は除外し、理由を残す。"""

from collections.abc import Sequence
from dataclasses import dataclass

from tech_researcher.agent.daily import DailyEntry


@dataclass(frozen=True)
class Excluded:
    entry: DailyEntry
    reason: str


def check_daily_ids(
    entries: Sequence[DailyEntry], data: dict
) -> tuple[list[DailyEntry], list[Excluded]]:
    """AI の出力の ID を、AI に渡した入力 data と照らす。

    - item_ids が空でなく、すべて入力にあり、項目間で重複しない（先に出た項目を残す）
    - primary_item_id が自項目の item_ids にある
    - primary_link_id が入力の候補リンクにある（D-07 の D6(b) の文言どおり。自項目のリンクに
      限ることは、項目ごとの出典の集合を見る D1 で確かめる）
    """
    item_ids = {i["id"] for i in data["items"]}
    link_ids = {link["id"] for i in data["items"] for link in i["links"]}
    kept: list[DailyEntry] = []
    excluded: list[Excluded] = []
    used: set[str] = set()
    for entry in entries:
        reason = _violation(entry, item_ids, link_ids, used)
        if reason:
            excluded.append(Excluded(entry, reason))
        else:
            kept.append(entry)
            used.update(entry.item_ids)
    return kept, excluded


def _violation(
    entry: DailyEntry, item_ids: set[str], link_ids: set[str], used: set[str]
) -> str | None:
    ids = entry.item_ids
    if not ids:
        return "item_ids が空"
    if unknown := sorted(set(ids) - item_ids):
        return f"入力にない ID {unknown}"
    if overlap := sorted(used & set(ids)):
        return f"ほかの項目と重複した ID {overlap}"
    if entry.primary_item_id is not None and entry.primary_item_id not in ids:
        return "primary_item_id が自項目の item_ids にない"
    if entry.primary_link_id is not None and entry.primary_link_id not in link_ids:
        return "primary_link_id が入力の候補リンクにない"
    return None
