"""D2・D3 と文字数・要点数の検査（D-07、report-templates.md 0.3・1.3、data-model.md 4.2・4.5）。

対象は AI が書いた文章（見出し・要点・重要性の理由・期間内の出来事）。違反した項目は除外する。
判定の値（文字数の上限など）は D-20 に従いコードの固定値とし、PoC で調整する。
"""

import re
from collections.abc import Callable, Iterator, Sequence

from tech_researcher.agent.daily import DailyEntry
from tech_researcher.validate.ids import Excluded

# D2：裸の URL も GitHub が自動でリンクにするので、スキームだけでも違反にする（`www.` も同じ）
_LINK = re.compile(r"(?:https?|ftp)://|(?<![A-Za-z0-9])www\.|\]\(|<http", re.IGNORECASE)
# D3：画像、`<img`・`<a `、HTML コメント・宣言・処理命令、開始タグ・終了タグ（`<tag …>`・`</tag>`）
_HTML = re.compile(r"!\[|<img\b|<a\s|<!|<\?|</?[A-Za-z][^<>]*>", re.IGNORECASE)

# 文字数の上限（プロンプトで示す目安。report-templates.md 1.3）。除外するのは、その2倍を超えたとき
LIMIT_FACTOR = 2
HEADLINE_LIMIT = 40
POINT_LIMIT = 80
IMPORTANCE_LIMIT = {"full": 100, "brief": 60}
PERIOD_EVENT_LIMIT = 60
# 要点の数の目安（最大3つ）。除外するのは、その2倍（6つ）を超えたとき（data-model.md 4.5）
POINTS_LIMIT = 3


def entry_texts(entry: DailyEntry) -> Iterator[tuple[str, str]]:
    """項目の AI の文章を（項目名、文章）で返す。"""
    yield "headline", entry.headline
    for point in entry.points:
        yield "points", point
    yield "importance", entry.importance
    if entry.period_event is not None:
        yield "period_event", entry.period_event


def check_daily_links(entries: Sequence[DailyEntry]) -> tuple[list[DailyEntry], list[Excluded]]:
    """D2：URL・`www.`・Markdown リンク・`<http` を含む項目を除外する。"""
    return _check(entries, lambda e: _find(e, _LINK, "URL・リンク"))


def check_daily_html(entries: Sequence[DailyEntry]) -> tuple[list[DailyEntry], list[Excluded]]:
    """D3：画像・HTML・HTML コメントを含む項目を除外する。"""
    return _check(entries, lambda e: _find(e, _HTML, "画像・HTML"))


def check_daily_lengths(entries: Sequence[DailyEntry]) -> tuple[list[DailyEntry], list[Excluded]]:
    """文字数が目安の2倍を超える文、または要点が6つを超える項目を除外する。"""
    return _check(entries, _too_long)


def _check(
    entries: Sequence[DailyEntry], violation: Callable[[DailyEntry], str | None]
) -> tuple[list[DailyEntry], list[Excluded]]:
    kept: list[DailyEntry] = []
    excluded: list[Excluded] = []
    for entry in entries:
        if reason := violation(entry):
            excluded.append(Excluded(entry, reason))
        else:
            kept.append(entry)
    return kept, excluded


def _find(entry: DailyEntry, pattern: re.Pattern[str], label: str) -> str | None:
    for name, text in entry_texts(entry):
        if pattern.search(text):
            return f"{name} に{label}を含む"  # 文章そのものは理由に残さない
    return None


def _too_long(entry: DailyEntry) -> str | None:
    max_points = POINTS_LIMIT * LIMIT_FACTOR
    if len(entry.points) > max_points:
        return f"要点が {len(entry.points)} 個で、{max_points} 個を超える"
    limits = {
        "headline": HEADLINE_LIMIT,
        "points": POINT_LIMIT,
        "importance": IMPORTANCE_LIMIT[entry.depth],
        "period_event": PERIOD_EVENT_LIMIT,
    }
    for name, text in entry_texts(entry):
        size, cap = len(text.strip()), limits[name] * LIMIT_FACTOR
        if size > cap:
            return f"{name} が {size} 文字で、{cap} 文字を超える"
    return None
