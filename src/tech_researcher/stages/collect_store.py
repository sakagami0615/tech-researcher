"""① の保存：取得結果を source_fetches に、収集項目を items に（data-model.md 2.4・3.4・3.7）。

日次と週次の ① が使う。決まり：

- 取得結果の状態：失敗は failed（分類は収集処理が付けたもの。付けていなければ fetch_failed）、
  1件以上なら ok、0件なら no_results。取得しなかった情報源（週次の Star の測定など）は skipped
- GitHub の新着・急上昇（github_repos）は、候補集めと Star の測定を1つの情報源として残す。
  1件でも測れていれば ok（Search が上限で止まった・一部の問い合わせが失敗したなどの部分的な失敗は
  ログに残す）。測れたものがなく失敗があれば failed、候補がなければ no_results。件数は測った
  リポジトリの数で、期間内に残った件数は0（急上昇・新規の判定は #37）
- 項目の行：代表のタイトル・抜粋・公開日時は、時刻まで分かる公開日時を持つ出典、なければ日付
  だけの公開日時を持つ出典、なければ最初の出典から取る（期間の判定と同じ順。period_filter）。
  タイトル・抜粋が空なら、ほかの出典の最初の空でない値を使う。投稿日時は出典の中で最も早いもの
- 掲載の根拠とする出来事：判定の窓に入る出来事のうち、published・released → new_repo →
  attention_spike → posted の順で選び、同じ順位なら早いもの
- kind：リポジトリ（github の値を持つ、または URL がリポジトリのトップ）→ repo、
  リリース（出来事 released か、GitHub のリリースのページ）→ release、GitHub の Issue・PR などの
  活動のページと HN の投稿のページ → post、ほか → article
"""

import sqlite3
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from tech_researcher.collect.github_candidates import ACTIVITY_PATHS, repo_from_url
from tech_researcher.collect.period_filter import event_in_window, published_source
from tech_researcher.collect.records import Event, Item, Record, SourceResult
from tech_researcher.collect.sources import GITHUB_REPOS_SOURCE_ID
from tech_researcher.db.items import ItemRow, upsert_items
from tech_researcher.db.runs import SourceFetch, record_source_fetch
from tech_researcher.periods import Target

FETCH_FAILED = "fetch_failed"  # 収集処理が分類を付けなかった失敗
# 掲載の根拠とする出来事の順位（小さいほど先。data-model.md 2.4）
INCLUSION_ORDER = {"published": 0, "released": 0, "new_repo": 1, "attention_spike": 2, "posted": 3}
_HN_HOST = "news.ycombinator.com"


@dataclass(frozen=True)
class GitHubFetch:
    """候補集めと Star の測定（github_repos）の結果のうち、保存とログに使うもの。

    measured は測った（同じ対象日の記録を使ったものを含む）リポジトリの数。
    error・error_category は最初の失敗（Search を先に見る）。skipped は測らなかった（週次）。
    """

    measured: int
    attempts: int
    error: str | None = None
    error_category: str | None = None
    skipped: bool = False

    @property
    def failed(self) -> bool:
        return self.error is not None and self.measured == 0

    def to_fetch(self, fetched_at: datetime) -> SourceFetch:
        if self.skipped:
            status, category = "skipped", None
        elif self.failed:
            status, category = "failed", self.error_category or FETCH_FAILED
        else:
            status, category = ("ok" if self.measured else "no_results"), None
        return SourceFetch(
            GITHUB_REPOS_SOURCE_ID, status, self.attempts, self.measured, 0, fetched_at, category
        )


def source_fetch(result: SourceResult, fetched_at: datetime, in_period: int) -> SourceFetch:
    """取得結果を source_fetches の1行にする。

    in_period は期間内に残った項目に入ったレコードの数。
    """
    count = len(result.records)
    if result.error is not None:
        status, category = "failed", result.error_category or FETCH_FAILED
    else:
        status, category = ("ok" if count else "no_results"), None
    kept = min(in_period, count)
    return SourceFetch(result.source_id, status, result.attempts, count, kept, fetched_at, category)


def in_period_counts(items: Iterable[Item]) -> Counter[str]:
    """期間内に残った項目に入ったレコードの数を、情報源ごとに数える。"""
    return Counter(r.source_id for item in items for r in item.sources)


def save_collect(
    conn: sqlite3.Connection,
    run_id: str,
    target: Target,
    fetches: Sequence[SourceFetch],
    items: Sequence[Item],
) -> list[str]:
    """取得結果と収集項目を書き、項目の ID（i…）を items の順で返す。

    runs の開始の行（start_run）は先に書いておく。items は日次だけ書く（週次の items は、日次の
    items と合わせて作る週次の材料の Issue（#44）で書く。それまでは書かない）。
    """
    for fetch in fetches:
        record_source_fetch(conn, run_id, fetch)
    if target.cadence != "daily":
        return []
    return upsert_items(conn, run_id, [item_row(item, target) for item in items])


def item_row(item: Item, target: Target) -> ItemRow:
    """収集項目を items の1行にする（期間内に残った項目だけを渡す）。"""
    rep = representative(item)
    primary = next((r.primary_text for r in item.sources if r.primary_text is not None), None)
    posted = [r.posted_at for r in item.sources if r.posted_at is not None]
    return ItemRow(
        cadence=target.cadence,
        target=target.key,
        item=item,
        kind=item_kind(item),
        title=rep.title or _first(r.title for r in item.sources) or "",
        published_precision=rep.published_precision,
        inclusion=inclusion_event(item, target),
        excerpt=rep.excerpt or _first(r.excerpt for r in item.sources),
        published_at=rep.published_at,
        posted_at=min(posted) if posted else None,
        primary_text=primary,
    )


def representative(item: Item) -> Record:
    """代表の出典（時刻まで分かる公開日時 → 日付だけの公開日時 → 最初の出典）。"""
    return published_source(item) or item.sources[0]


def inclusion_event(item: Item, target: Target) -> Event:
    """掲載の根拠とする出来事。判定の窓に入る出来事がなければ ValueError。"""
    events = [e for e in item.events if event_in_window(item, e, target)]
    if not events:
        raise ValueError(f"判定の窓に入る出来事がない: {item.url}")
    return min(events, key=lambda e: (INCLUSION_ORDER[e.type], e.at))


def item_kind(item: Item) -> str:
    repo = repo_from_url(item.url)
    segments = urlsplit(item.url).path.split("/")
    if item.github is not None or (repo is not None and len(segments) == 3):
        return "repo"
    if any(e.type == "released" for e in item.events) or (
        repo is not None and segments[3] == "releases"
    ):
        return "release"
    if (repo is not None and segments[3] in ACTIVITY_PATHS) or urlsplit(
        item.url
    ).hostname == _HN_HOST:
        return "post"
    return "article"


def _first(values: Iterable[str | None]) -> str | None:
    return next((v for v in values if v), None)
