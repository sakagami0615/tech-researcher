"""期間での絞り込み（data-model.md 2.4・2.5、D-13）。

収集レコードをまとめた後の収集項目で判定する。公開日時が分かるかどうかは、
項目にまとまったすべての出典を見て決めるため。
"""

from tech_researcher.collect.records import Item
from tech_researcher.periods import Period


def in_period(item: Item, period: Period) -> bool:
    """出来事が1つでもその判定の窓に入っていれば True。

    投稿で判定するのは、どの出典にも公開日時がないときだけ。公開が期間より前の記事は、
    期間内に投稿されても残さない（D-13）。急上昇などの出来事は、それを作る Issue で足す。
    """
    published_known = any(r.published_at is not None for r in item.sources)
    for event in item.events:
        match event.type:
            case "published" | "released":
                hit = period.contains(event.at)
            case "posted":
                hit = not published_known and period.contains(event.at)
            case _:
                raise ValueError(f"期間の判定をまだ扱えない出来事 {event.type}")
        if hit:
            return True
    return False
