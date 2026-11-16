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
    """AI の出力の ID を、AI に渡した入力 data と照らす（項目ごとの検査）。

    - item_ids が空でなく、すべて入力にある
    - primary_item_id が自項目の item_ids にある
    - primary_link_id が入力の候補リンクにある（D-07 の D6(b) の文言どおり。自項目のリンクに
      限ることは、D1（validate/sources.py）で確かめる）

    項目間の重複は check_daily_duplicates で、ほかの検査をすべて通った項目の中で判定する。
    """
    item_ids = {i["id"] for i in data["items"]}
    link_ids = {link["id"] for i in data["items"] for link in i["links"]}
    kept: list[DailyEntry] = []
    excluded: list[Excluded] = []
    for entry in entries:
        reason = _violation(entry, item_ids, link_ids)
        if reason:
            excluded.append(Excluded(entry, reason))
        else:
            kept.append(entry)
    return kept, excluded


def check_daily_duplicates(
    entries: Sequence[DailyEntry],
) -> tuple[list[DailyEntry], list[Excluded]]:
    """item_ids が項目間で重複しない（先に出た項目を残す）。

    ほかの検査で除外された項目に ID を取られないよう、検査の最後に、残った項目だけで行う。
    """
    kept: list[DailyEntry] = []
    excluded: list[Excluded] = []
    used: set[str] = set()
    for entry in entries:
        if overlap := sorted(used & set(entry.item_ids)):
            excluded.append(Excluded(entry, f"ほかの項目と重複した ID {overlap}"))
        else:
            kept.append(entry)
            used.update(entry.item_ids)
    return kept, excluded


def _violation(entry: DailyEntry, item_ids: set[str], link_ids: set[str]) -> str | None:
    ids = entry.item_ids
    if not ids:
        return "item_ids が空"
    if unknown := sorted(set(ids) - item_ids):
        return f"入力にない ID {unknown}"
    if entry.primary_item_id is not None and entry.primary_item_id not in ids:
        return "primary_item_id が自項目の item_ids にない"
    if entry.primary_link_id is not None and entry.primary_link_id not in link_ids:
        return "primary_link_id が入力の候補リンクにない"
    return None
