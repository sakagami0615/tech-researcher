"""週次の ② の出力の検査（D-07 の D1〜D4・D6(b)、data-model.md 4.3、report-templates.md 2.4）。

日次（validate/ids.py・sources.py・text.py・quote.py）と同じ検査を、週次の4つのセクション（テーマ・
重要な技術的変化・比較・今後注目すべきテーマ）の項目に行う。違反した項目は除外し、検査の分類
（reports の exclusion_reasons と同じ名前）と理由を残す。出力全体の形（D6(a)）は
agent/weekly_output.py の parse_weekly_output で先に確かめる（違反は全体の不合格）。

検査の順：ID の存在（invalid_id）→ 出典（invalid_source）→ 製品名の照合（unknown_product）→
D2（forbidden_link）→ D3（forbidden_html）→ 文字数（too_long）→ D4（long_quote）→ 重複
（duplicate_id）→ 行がなくなった比較（empty_comparison）。重複は最後に、ほかの検査を通った
項目の中で判定し、先に出た項目を残す。重複を禁じるのは同じセクションの中だけ（別のセクションに
同じ項目を載せてよい。D-07）。比較表は、行（`item_id`）ごとに検査し、残った行がなければ比較
（領域）ごと除外する。
"""

import re
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace

from tech_researcher.agent.weekly_output import (
    ComparisonEntry,
    ComparisonRow,
    TechChangeEntry,
    ThemeEntry,
    WatchEntry,
    WeeklyOutput,
)
from tech_researcher.collect.aggregate import candidate_links
from tech_researcher.collect.records import Item
from tech_researcher.collect.urls import normalize_url
from tech_researcher.config import QuoteLimits
from tech_researcher.validate.quote import (
    Prepared,
    meta_terms_by_id,
    quote_sources,
    quote_violation,
)
from tech_researcher.validate.sources import item_urls, normalized_urls
from tech_researcher.validate.text import LIMIT_FACTOR, has_html, has_link

MIN_PRODUCT_NAME = 2  # 日本語の製品名の最短の長さ（1文字はどのタイトルにも含まれうる）
# 日本語（ひらがな・カタカナ・漢字）。語の境界にしない（「楽天AIが公開」の AI を通す）
_JAPANESE = re.compile("[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")

Entry = ThemeEntry | TechChangeEntry | ComparisonEntry | ComparisonRow | WatchEntry

# 文字数の上限（プロンプトで示す目安。report-templates.md 2.3・2.4）。
# 除外するのは、その2倍を超えたとき。
# テンプレートに数字がない欄（変化の見出し・領域名・製品名・注目テーマ名）は、近い欄に合わせた値
LIMITS: dict[tuple[str, str], int] = {
    ("theme", "name"): 30,
    ("theme", "change"): 200,
    ("theme", "comparison"): 120,
    ("tech_change", "headline"): 40,
    ("tech_change", "description"): 120,
    ("comparison", "domain"): 30,
    ("comparison", "summary"): 150,
    ("comparison_row", "product_name"): 40,
    ("comparison_row", "feature"): 80,
    ("comparison_row", "use_case"): 60,
    ("watch", "name"): 30,
    ("watch", "reason"): 120,
}
# 検査の分類（検査の順）。reports の verification・exclusion_reasons と同じ名前
CATEGORIES = (
    "invalid_id",
    "invalid_source",
    "unknown_product",
    "forbidden_link",
    "forbidden_html",
    "too_long",
    "long_quote",
    "duplicate_id",
    "empty_comparison",
)


@dataclass(frozen=True)
class WeeklyExcluded:
    """除外した項目。section は theme・tech_change・comparison・comparison_row・watch。

    除外したテーマの AI の theme_id は entry に残る（payload に入れる。3.4）。
    """

    section: str
    category: str
    entry: Entry
    reason: str


@dataclass(frozen=True)
class WeeklyChecked:
    """検査の結果。output は残した項目（元の順）。verification は検査の分類 → passed・failed。"""

    output: WeeklyOutput
    excluded: tuple[WeeklyExcluded, ...]
    verification: Mapping[str, str]

    @property
    def published(self) -> int:
        """掲載する項目の数（テーマ・技術的変化・比較・注目テーマ。比較は領域の数）。"""
        out = self.output
        return len(out.themes) + len(out.tech_changes) + len(out.comparisons) + len(out.watch)


@dataclass(frozen=True)
class _Context:
    item_ids: frozenset[str]
    past_ids: frozenset[str]
    theme_ids: frozenset[str]
    items: Mapping[str, Item]
    titles: Mapping[str, str]  # ② に渡した今週の観測のタイトル
    past_urls: Mapping[str, str]
    allowed: frozenset[str]
    quote_sources: Sequence[Prepared]
    limits: QuoteLimits


def check_weekly(
    output: WeeklyOutput,
    data: dict,
    items: Mapping[str, Item],
    past_urls: Mapping[str, str],
    limits: QuoteLimits,
) -> WeeklyChecked:
    """D1・D2・D3・D4・D6(b)・製品名・文字数の検査。data は ② に渡した入力（build_weekly_input）。

    items は今週の観測（項目 ID → 項目。② の入力の items と同じもの）、past_urls は渡した過去の
    観測の ID → URL（PastObservation の url）。
    """
    ctx = _context(data, items, past_urls, limits)
    state = _State()
    themes = _survivors(state, "theme", output.themes, ctx)
    changes = _survivors(state, "tech_change", output.tech_changes, ctx)
    watch = _survivors(state, "watch", output.watch, ctx)
    comparisons = _comparisons(state, output.comparisons, ctx)
    # 重複は最後に、ほかの検査をすべて通った項目の中で判定する
    themes = state.dedupe("theme", themes, lambda e: e.item_ids, _theme_key)
    changes = state.dedupe("tech_change", changes, lambda e: e.item_ids)
    watch = state.dedupe("watch", watch, lambda e: e.item_ids)
    comparisons = _drop_duplicate_rows(state, comparisons)
    kept = WeeklyOutput(tuple(themes), tuple(changes), tuple(comparisons), tuple(watch))
    return WeeklyChecked(kept, tuple(state.excluded), state.verification())


class _State:
    def __init__(self) -> None:
        self.excluded: list[WeeklyExcluded] = []
        self.failed: set[str] = set()

    def exclude(self, section: str, category: str, entry: Entry, reason: str) -> None:
        self.failed.add(category)
        self.excluded.append(WeeklyExcluded(section, category, entry, reason))

    def verification(self) -> dict[str, str]:
        verification = {"invalid_output": "passed"}  # スキーマ（D6(a)）は、ここに来る前に通っている
        verification.update({c: "failed" if c in self.failed else "passed" for c in CATEGORIES})
        return verification

    def dedupe[E: Entry](
        self,
        section: str,
        entries: Sequence[E],
        ids: Callable[[E], Sequence[str]],
        key: Callable[[E], str | None] = lambda _: None,
    ) -> list[E]:
        """item_ids（と key。テーマの theme_id）が先に出た項目と重なる項目を除外する。

        除外した項目は ID を取らない（後の項目の ID を巻き込まない）。
        """
        kept: list[E] = []
        used: set[str] = set()
        used_keys: set[str] = set()
        for entry in entries:
            if overlap := sorted(used & set(ids(entry))):
                self.exclude(section, "duplicate_id", entry, f"ほかの項目と重複した ID {overlap}")
            elif (k := key(entry)) is not None and k in used_keys:
                self.exclude(section, "duplicate_id", entry, f"ほかのテーマと重複した theme_id {k}")
            else:
                kept.append(entry)
                used.update(ids(entry))
                if k is not None:
                    used_keys.add(k)
        return kept


def _theme_key(theme: ThemeEntry) -> str | None:
    return theme.theme_id


def _survivors[E: Entry](
    state: _State, section: str, entries: Sequence[E], ctx: _Context
) -> list[E]:
    """重複以外の検査を、検査の順に行い、通った項目を返す。違反は最初の1つだけを理由にする。"""
    kept: list[E] = []
    for entry in entries:
        found = _violation(entry, ctx)
        if found:
            state.exclude(section, found[0], entry, found[1])
        else:
            kept.append(entry)
    return kept


def _comparisons(
    state: _State, comparisons: Sequence[ComparisonEntry], ctx: _Context
) -> list[ComparisonEntry]:
    """領域の文章を検査し、通った領域の行を検査する。残った行がない領域は除外する。"""
    kept: list[ComparisonEntry] = []
    for comparison in comparisons:
        found = _violation(comparison, ctx)
        if found:
            state.exclude("comparison", found[0], comparison, found[1])
            continue
        rows = _survivors(state, "comparison_row", comparison.rows, ctx)
        kept.append(replace(comparison, rows=tuple(rows)))
    return kept


def _drop_duplicate_rows(
    state: _State, comparisons: Sequence[ComparisonEntry]
) -> list[ComparisonEntry]:
    """比較のセクションの中で、
    同じ item_id の行は先に出た1つだけ残し、行がなくなった領域を除外する。"""
    used: set[str] = set()
    kept: list[ComparisonEntry] = []
    for comparison in comparisons:
        rows = []
        for row in comparison.rows:
            if row.item_id in used:
                reason = f"ほかの行と重複した ID {[row.item_id]}"
                state.exclude("comparison_row", "duplicate_id", row, reason)
            else:
                used.add(row.item_id)
                rows.append(row)
        if rows:
            kept.append(replace(comparison, rows=tuple(rows)))
        else:
            state.exclude("comparison", "empty_comparison", comparison, "掲載できる行がない")
    return kept


def _violation(entry: Entry, ctx: _Context) -> tuple[str, str] | None:
    checks: Sequence[tuple[str, Callable[[], str | None]]] = (
        ("invalid_id", lambda: _invalid_id(entry, ctx)),
        ("invalid_source", lambda: _invalid_source(entry, ctx)),
        ("unknown_product", lambda: _unknown_product(entry, ctx)),
        ("forbidden_link", lambda: _forbidden(entry, ctx, has_link, "URL・リンク")),
        ("forbidden_html", lambda: _forbidden(entry, ctx, has_html, "画像・HTML")),
        ("too_long", lambda: _too_long(entry, ctx)),
        ("long_quote", lambda: _long_quote(entry, ctx)),
    )
    for category, check in checks:
        if reason := check():
            return category, reason
    return None


def _context(
    data: dict, items: Mapping[str, Item], past_urls: Mapping[str, str], limits: QuoteLimits
) -> _Context:
    links = [c.url for i, it in items.items() for c in candidate_links(it, i)]
    allowed = {u for it in items.values() for u in normalized_urls(item_urls(it))}
    allowed |= set(normalized_urls(links)) | set(normalized_urls(past_urls.values()))
    return _Context(
        item_ids=frozenset(i["id"] for i in data["items"]),
        past_ids=frozenset(p["id"] for p in data["past_observations"]),
        theme_ids=frozenset(t["theme_id"] for t in data["past_themes"]),
        items=items,
        titles={i["id"]: i["untrusted"]["title"] or "" for i in data["items"]},
        past_urls=past_urls,
        allowed=frozenset(allowed),
        quote_sources=quote_sources(data, meta_terms_by_id(items)),
        limits=limits,
    )


def _ids(entry: Entry) -> tuple[Sequence[str], Sequence[str]]:
    """項目が選んだ（今週の観測の ID、過去の観測の ID）。"""
    match entry:
        case ThemeEntry():
            return entry.item_ids, entry.past_ids
        case TechChangeEntry() | WatchEntry():
            return entry.item_ids, ()
        case ComparisonRow():
            return (entry.item_id,), ()
        case ComparisonEntry():
            return (), ()  # 領域には ID がない（行が持つ）


def _texts(entry: Entry, ctx: _Context) -> Iterator[tuple[str, str]]:
    """項目の AI の文章を（欄の名前、文章）で返す。

    GitHub のリポジトリの行の製品名は、表示に使わない（名前は owner/repo）ので対象にしない。
    """
    match entry:
        case ThemeEntry():
            yield from (("name", entry.name), ("change", entry.change))
            yield "comparison", entry.comparison
        case TechChangeEntry():
            yield from (("headline", entry.headline), ("description", entry.description))
        case ComparisonEntry():
            yield from (("domain", entry.domain), ("summary", entry.summary))
        case ComparisonRow():
            if entry.product_name is not None and ctx.items[entry.item_id].github is None:
                yield "product_name", entry.product_name
            yield from (("feature", entry.feature), ("use_case", entry.use_case))
        case WatchEntry():
            yield from (("name", entry.name), ("reason", entry.reason))


def _invalid_id(entry: Entry, ctx: _Context) -> str | None:
    """D6(b)：ID が渡したものにある。theme_id は渡した過去のテーマの番号か null。"""
    ids, past = _ids(entry)
    if isinstance(entry, ThemeEntry):
        if not ids and not past:
            return "item_ids と past_ids がどちらも空"
    elif not isinstance(entry, ComparisonEntry) and not ids:
        return "item_ids が空"
    if unknown := sorted(set(ids) - ctx.item_ids):
        return f"入力にない ID {unknown}"
    if unknown := sorted(set(past) - ctx.past_ids):
        return f"渡していない過去の観測の ID {unknown}"
    if isinstance(entry, ThemeEntry) and entry.theme_id is not None:
        if entry.theme_id not in ctx.theme_ids:
            return "渡していない theme_id"
    return None


def _invalid_source(entry: Entry, ctx: _Context) -> str | None:
    """D1：ID から付ける出典の URL が、収集した URL と過去の観測の URL の集合にある。"""
    ids, past = _ids(entry)
    urls = [u for i in ids for u in item_urls(ctx.items[i])]
    urls += [ctx.past_urls.get(i, "") for i in past]
    if all((normalize_url(u) or "") in ctx.allowed for u in urls):
        return None
    return "出典の URL が不正か、収集した URL の集合にない"


def _unknown_product(entry: Entry, ctx: _Context) -> str | None:
    """比較表の GitHub 外の行：製品名が、選んだ項目の ② に渡したタイトルに含まれる（2.4）。

    GitHub のリポジトリの行は、名前をコードが owner/repo で付けるので照合しない。
    """
    if not isinstance(entry, ComparisonRow):
        return None
    item = ctx.items[entry.item_id]
    if item.github is not None:
        return None
    name = _fold(entry.product_name or "")
    if not name:
        return "GitHub 外の行に製品名がない"
    if len(name) < MIN_PRODUCT_NAME and not _is_word_char(name):
        return f"製品名が {MIN_PRODUCT_NAME} 文字より短い"
    if _contains_word(_fold(ctx.titles.get(entry.item_id, "")), name):
        return None
    return "製品名が収集タイトルにない"


def _is_word_char(text: str) -> bool:
    """日本語以外の英数字（Unicode）だけでできている（語の境界を見る文字）。"""
    return text.isalnum() and not _JAPANESE.search(text)


def _contains_word(title: str, name: str) -> bool:
    """name が title に語として含まれる（Intel は Intelligence に、DB は FooDB に一致させない）。

    - 前：名前が日本語以外の英数字で始まるなら、直前がそのような英数字でないこと
    - 後：名前が英字で終わるなら後ろに英字が続かないこと（TypeScript5.7 の数字は許す）、
      数字で終わるなら後ろに英数字が続かないこと（GPT-4 は GPT-4o に一致させない）
    - 日本語の文字と記号は境界にしない。記号で終わる名前の後ろは見ない（C# は C#compiler に一致）
    """
    start = title.find(name)
    while start >= 0:
        end = start + len(name)
        if _boundary_before(title, start, name) and _boundary_after(title, end, name):
            return True
        start = title.find(name, start + 1)
    return False


def _boundary_before(title: str, start: int, name: str) -> bool:
    if not _is_word_char(name[0]) or start == 0:
        return True
    return not _is_word_char(title[start - 1])


def _boundary_after(title: str, end: int, name: str) -> bool:
    last = name[-1]
    if not _is_word_char(last) or end >= len(title):
        return True
    following = title[end]
    if last.isalpha():
        return not (_is_word_char(following) and following.isalpha())
    return not _is_word_char(following)


def _fold(text: str) -> str:
    """製品名の照合用：NFKC、大文字・小文字の統一、空白の圧縮だけ（記号は残す）。"""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _forbidden(entry: Entry, ctx: _Context, found: Callable[[str], bool], label: str) -> str | None:
    for name, text in _texts(entry, ctx):
        if found(text):
            return f"{name} に{label}を含む"  # 文章そのものは理由に残さない
    return None


def _too_long(entry: Entry, ctx: _Context) -> str | None:
    for name, text in _texts(entry, ctx):
        size, cap = len(text.strip()), LIMITS[(_kind(entry), name)] * LIMIT_FACTOR
        if size > cap:
            return f"{name} が {size} 文字で、{cap} 文字を超える"
    return None


def _kind(entry: Entry) -> str:
    match entry:
        case ThemeEntry():
            return "theme"
        case TechChangeEntry():
            return "tech_change"
        case ComparisonEntry():
            return "comparison"
        case ComparisonRow():
            return "comparison_row"
        case WatchEntry():
            return "watch"


def _long_quote(entry: Entry, ctx: _Context) -> str | None:
    for _, text in _texts(entry, ctx):
        if reason := quote_violation(text, ctx.quote_sources, ctx.limits):
            return reason
    return None
