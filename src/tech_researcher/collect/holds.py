"""AI に渡さない項目の判定（hold_reason。D-13・D-16、data-model.md 3.4・3.5、structure.md 5.3）。

期間で絞った後の収集項目のうち、次の項目を AI に渡さない（収集結果の collected.json に入れない）。
観測としては DB の items に残す（hold_reason に理由を持たせる）。日次だけで行う。

- already_featured（D-13）：掲載の根拠になりうる出来事が急上昇・新規だけで、すでに「載せた」項目。
  期間内の公開・リリースなど、別の出来事を持つ項目は通常どおり渡す
- shown_previous_day（D-16）：期間内の判定が last30days-skill の UTC の日付の近似だけによる項目の
  うち、前日の日次レポートに載せた記事
"""

from collections.abc import Collection, Iterable

from tech_researcher.collect.period_filter import date_approximated, window_events
from tech_researcher.collect.records import Item
from tech_researcher.periods import Target

ALREADY_FEATURED = "already_featured"
SHOWN_PREVIOUS_DAY = "shown_previous_day"
_SPIKE_TYPES = frozenset({"attention_spike", "new_repo"})


def hold_reasons(
    items: Iterable[Item],
    target: Target,
    featured: Collection[str],
    shown_previous_day: Collection[str],
) -> dict[str, str]:
    """項目の URL → 渡さない理由。渡す項目は入れない。

    featured は載せた急上昇・新規の canonical_url、shown_previous_day は前日の日次レポートに
    載せた項目の canonical_url（db.history）。どちらにも当たるときは already_featured を使う。
    """
    holds: dict[str, str] = {}
    if target.cadence != "daily":
        return holds
    for item in items:
        events = window_events(item, target)
        if events and all(e.type in _SPIKE_TYPES for e in events) and item.url in featured:
            holds[item.url] = ALREADY_FEATURED
        elif item.url in shown_previous_day and date_approximated(item, target):
            holds[item.url] = SHOWN_PREVIOUS_DAY
    return holds
