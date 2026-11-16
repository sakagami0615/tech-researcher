"""期間内の出来事の欠落（US-E7-01 の必須項目、report-templates.md 1.3、data-model.md 4.2）。"""

from collections.abc import Sequence

from tech_researcher.agent.daily import DailyEntry
from tech_researcher.validate.ids import Excluded


def check_period_events(
    entries: Sequence[DailyEntry], data: dict
) -> tuple[list[DailyEntry], list[Excluded]]:
    """`item_ids` のどれかが `needs_period_event` のとき、`period_event` が空でなければならない。

    必要かどうかはコードが公開日と対象期間から判定して入力に入れた値を使う。欠けた項目は除外する。
    entries は D6(b)（validate/ids.py）で ID の存在を確かめたものを渡す。
    """
    needs = {i["id"] for i in data["items"] if i["needs_period_event"]}
    kept: list[DailyEntry] = []
    excluded: list[Excluded] = []
    for entry in entries:
        if needs.intersection(entry.item_ids) and not (entry.period_event or "").strip():
            excluded.append(Excluded(entry, "期間内の出来事が必要なのに period_event がない"))
        else:
            kept.append(entry)
    return kept, excluded
