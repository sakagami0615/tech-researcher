"""① 収集（D-04。worker で動く）。

設定で有効にした情報源から順に集め、注目の急上昇を判定し（D-13）、まとめて期間で絞り、
実行のフォルダの collected.json に書く。DB の接続を渡されたときは、前日の記録を読み、書くとき
（本番）は人気型フィードの取得記録・Star 数・取得結果（source_fetches）・収集項目（items）を書く
（stages/collect_store.py）。runs の開始と終わりは呼ぶ側（cli.py）が書く。

集める順：RSS・Atom → Qiita → last30days-skill（HN・GitHub Search）→ GitHub の候補集め（Search）
→ Star 数の測定（GraphQL）。last30days-skill と候補集めはどちらも同じ収集用トークンで GitHub の
Search を使うので、並行させずに順に動かす（候補集めの言及の経路は、それより前の情報源のレコードを
使う）。GitHub Releases の接続は #40 で足す。
Star 数の測定の後に、急上昇・新規のリポジトリを判定し（github_spikes）、載せる分の README を取る。
まとめて期間で絞った後、すでに載せた急上昇・新規と前日に載せた記事は、AI に渡さない（collected.json
に入れない）。DB の items には hold_reason を付けて残す（collect/holds.py）。
外部由来の文（タイトル・本文）はファイルにだけ書き、戻り値（標準出力に出す要約）には件数と
情報源の ID だけを入れる（structure.md 3.2）。
前日の記録がなく判定しなかった急上昇は unjudged.json に書き、③ がヘッダの定型文にする。
collected.json は load_collected で収集項目に戻せる（② の入力の用意と ③ の照合に使う）。
"""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tech_researcher.collect.aggregate import aggregate
from tech_researcher.collect.github_api import GitHubClient, GitHubError, collect_token
from tech_researcher.collect.github_candidates import collect_candidates
from tech_researcher.collect.github_releases import fetch_readmes, with_readme
from tech_researcher.collect.github_spikes import judge_star_spikes, repo_url
from tech_researcher.collect.github_stars import measure_stars
from tech_researcher.collect.holds import hold_reasons, strip_featured
from tech_researcher.collect.last30days import collect_last30days
from tech_researcher.collect.period_filter import in_period
from tech_researcher.collect.popularity import judge_feed_entries, mark_thresholds
from tech_researcher.collect.qiita import SOURCE_ID as QIITA_SOURCE_ID
from tech_researcher.collect.qiita import collect_qiita
from tech_researcher.collect.records import (
    Attention,
    Event,
    GitHubRepo,
    Item,
    Record,
    SourceResult,
)
from tech_researcher.collect.rss import collect_feed, feed_source_id
from tech_researcher.collect.sources import source_names
from tech_researcher.config import Config, Feed, Limits, Qiita
from tech_researcher.db.history import featured_urls, shown_urls
from tech_researcher.db.runs import fetch_incomplete, record_source_fetch
from tech_researcher.db.star_snapshots import load_star_snapshot
from tech_researcher.logging import RunLog
from tech_researcher.periods import Period, Target
from tech_researcher.stages.collect_store import (
    GITHUB_REPOS_SOURCE_ID,
    GitHubFetch,
    in_period_counts,
    save_collect,
    source_fetch,
)

COLLECTED = "collected.json"
UNJUDGED = "unjudged.json"  # 前日の記録がなく判定しなかった急上昇（ヘッダの定型文の元。1.6）
FEED_JUDGMENT = "人気型フィードへの初登場"
STAR_JUDGMENT = "GitHub の Star の急増"
# last30days-skill を置いた場所（作業ディレクトリから。worker では /app、ホストではリポジトリ）
LAST30DAYS_ROOT = Path("third_party/last30days")

CollectFeed = Callable[[Feed, Limits], SourceResult]
CollectQiita = Callable[[Qiita, Period, Limits], SourceResult]
# （設定, 対象期間, 収集用トークン）→ 有効な情報源（hackernews・github_search）ごとの結果
CollectLast30Days = Callable[[Config, Period, str | None], list[SourceResult]]


def _last30days(config: Config, period: Period, token: str | None) -> list[SourceResult]:
    return collect_last30days(config, period, LAST30DAYS_ROOT, github_token=token)


@dataclass(frozen=True)
class Collectors:
    """各情報源の収集処理（テストで偽物に差し替える）。token は収集用トークンを読む。"""

    feed: CollectFeed = collect_feed
    qiita: CollectQiita = collect_qiita
    last30days: CollectLast30Days = _last30days
    github: Callable[[str | None, Limits], GitHubClient] = GitHubClient
    token: Callable[[], str | None] = collect_token
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


DEFAULT_COLLECTORS = Collectors()


@dataclass(frozen=True)
class GitHubOutcome:
    """候補集め・Star 数の測定・急上昇の判定の結果。records は急上昇・新規のレコード。"""

    fetch: GitHubFetch
    at: datetime
    records: tuple[Record, ...] = ()
    star_unjudged: bool = False


def collect_failure(sources: int, failed: int) -> str | None:
    """① の要約から実行エラーの分類を決める（US-E6-03・US-E6-04）。続けられるなら None。

    1つも取得できなかった日は、0件のレポートにせず実行エラーにする。有効な情報源がない
    （設定の誤り）ときも同じに扱う。
    """
    if failed < sources:
        return None
    return "no_sources" if sources == 0 else "all_sources_failed"


def run_collect(
    config: Config,
    target: Target,
    run_dir: Path,
    log: RunLog,
    *,
    collectors: Collectors = DEFAULT_COLLECTORS,
    db: sqlite3.Connection | None = None,
    write_db: bool | None = None,
) -> dict:
    """集めて run_dir/collected.json に書き、要約を返す。

    1つの情報源の失敗では止めず、失敗した情報源の ID を要約に入れる（US-E6-04）。
    前日の記録がなく初登場を判定しなかった人気型フィードの ID も要約に入れる（report-templates.md
    1.6 のヘッダの元）。sources は取得しようとした情報源の数（取得しなかった skipped を除く）。
    db は前日の記録を読む接続で、write_db が True なら記録と結果を書く（ドライランは False。
    D-22）。db を渡すときは write_db も必ず渡す（書き忘れると、翌日に全件が初登場になるため）。
    書くときは、log.run_id の runs の行を先に書いておく（source_fetches・items が参照する）。
    run_dir は統括スクリプトが作る（実行の終わりに消すのも統括スクリプト）。
    """
    if db is not None and write_db is None:
        raise ValueError("db を渡すときは write_db も渡す")
    write = bool(write_db) and db is not None
    featured, shown = _history(db, target)
    log.write("collect", "start")
    limits = config.limits
    fetched: list[tuple[SourceResult, datetime]] = []

    def keep(*pairs: tuple[SourceResult, datetime]) -> None:
        # 取得を終えた情報源から書く（① が上限で止められても、それまでの取得の記録を残す）。
        # 期間内に残った件数は、最後にまとめて書き直す（同じ行の upsert）
        fetched.extend(pairs)
        if write:
            assert db is not None
            for result, at in pairs:
                record_source_fetch(db, log.run_id, source_fetch(result, at, 0))

    feeds = [f for f in config.feeds if f.enabled]
    for f in feeds:
        keep(
            _collect_one(feed_source_id(f), lambda f=f: collectors.feed(f, limits), log, collectors)
        )
    if config.qiita is not None and config.qiita.enabled:
        qiita = config.qiita
        keep(
            _collect_one(
                QIITA_SOURCE_ID,
                lambda: collectors.qiita(qiita, target.period, limits),
                log,
                collectors,
            )
        )
    # GitHub を使う情報源があるときだけ読む（使わないのに形の誤りで止めないように）
    uses_github = any(
        s is not None and s.enabled
        for s in (config.hackernews, config.github_search, config.github_repos)
    )
    token = _token(collectors, log) if uses_github else None
    keep(*_collect_last30days(config, target, token, log, collectors))
    results = [r for r, _ in fetched]
    github = _collect_github(config, target, results, token, db, write, featured, log, collectors)
    if github is not None and write:
        assert db is not None
        record_source_fetch(db, log.run_id, github.fetch.to_fetch(github.at))
    popular = {feed_source_id(f) for f in feeds if f.kind == "popular"}
    judged = judge_feed_entries(results, popular, target, db, write=write)
    records = mark_thresholds(judged.records, config.attention_thresholds)
    if github is not None:
        records += github.records
    observed = [i for i in aggregate(records) if in_period(i, target)]
    holds = hold_reasons(observed, target, featured, shown)
    # すでに載せたリポジトリが別の出来事（リリースなど）で渡るときは、急上昇・新規の出来事と
    # 増分を外す（再び急上昇として載せないため。DB の観測には元の値を残す）
    items = [strip_featured(i) if i.url in featured else i for i in observed if i.url not in holds]
    if write:
        assert db is not None
        counts = in_period_counts(observed)
        fetches = [source_fetch(r, at, counts[r.source_id]) for r, at in fetched]
        if github is not None:
            fetches.append(github.fetch.to_fetch(github.at, counts[GITHUB_REPOS_SOURCE_ID]))
        save_collect(db, log.run_id, target, fetches, observed, holds)
    # 途中で止まっても壊れたファイルを残さないよう、一時ファイルに書いてから置き換える
    partial = run_dir / f"{COLLECTED}.partial"
    partial.write_text(
        json.dumps([asdict(i) for i in items], ensure_ascii=False, default=_json_value),
        encoding="utf-8",
    )
    partial.replace(run_dir / COLLECTED)
    unjudged_star = github is not None and github.star_unjudged
    if target.cadence == "daily":
        _write_unjudged(run_dir, judged.unjudged, unjudged_star)
    log.write("collect", "end", count=len(items))
    failed = [r.source_id for r in results if r.error]
    sources = len(results)
    if github is not None and not github.fetch.skipped:
        sources += 1
        if github.fetch.failed:
            failed.append(GITHUB_REPOS_SOURCE_ID)
    return {
        "items": len(items),
        "sources": sources,
        "failed_sources": failed,
        "unjudged_feed_sources": list(judged.unjudged),
        "unjudged_star": unjudged_star,
        "held_items": len(holds),
    }


def _history(db: sqlite3.Connection | None, target: Target) -> tuple[set[str], set[str]]:
    """掲載の履歴（D-13・D-16）：（載せた急上昇・新規の URL、前日の日次レポートに載せた URL）。

    日次だけで読む。DB がなければ（開発機のドライラン）、どちらもないものとして扱う。
    featured は対象日より前の日次レポートだけを数える（同じ対象の再実行・ドライランで、その日に
    載せた項目で自分を除外しないため）。
    """
    if db is None or target.cadence != "daily":
        return set(), set()
    return featured_urls(db, target.key), shown_urls(db, target.key - timedelta(days=1))


def _write_unjudged(run_dir: Path, feeds: tuple[str, ...], star: bool) -> None:
    text = json.dumps({"feed_sources": list(feeds), "github_stars": star})
    partial = run_dir / f"{UNJUDGED}.partial"
    partial.write_text(text, encoding="utf-8")
    partial.replace(run_dir / UNJUDGED)


def unjudged_notes(run_dir: Path, config: Config) -> list[tuple[str, list[str]]]:
    """ヘッダの定型文の元：（判定の名前、記録がなかった情報源の表示名）。なければ空。"""
    path = run_dir / UNJUDGED
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    names = source_names(config)
    notes: list[tuple[str, list[str]]] = []
    if data["feed_sources"]:
        notes.append((FEED_JUDGMENT, [names.get(s, s) for s in data["feed_sources"]]))
    if data["github_stars"]:
        notes.append((STAR_JUDGMENT, []))
    return notes


def _token(collectors: Collectors, log: RunLog) -> str | None:
    """収集用トークン。形が誤っていれば、ないものとして続ける（GitHub を使う情報源だけが auth の
    失敗になる。US-E6-04）。値はログに出さない（GitHubError のメッセージに値は入らない）。"""
    try:
        return collectors.token()
    except GitHubError as e:
        log.write("collect", "failure", error_category=e.category, detail=str(e))
        return None


def _collect_one(
    source_id: str, collect: Callable[[], SourceResult], log: RunLog, collectors: Collectors
) -> tuple[SourceResult, datetime]:
    """1つの情報源を集めてログに残す。想定外の例外もその情報源の失敗にする（US-E6-04）。"""
    try:
        result = collect()
    except Exception as e:
        result = _unexpected(source_id, e, log)
    _log_result(result, log)
    return result, collectors.now()


def _unexpected(source_id: str, e: Exception, log: RunLog) -> SourceResult:
    # 例外の中身（外部由来の文を含みうる）は、マスクするログの exception にだけ残す
    name = type(e).__name__
    log.write("collect", "failure", source=source_id, error_category=name, exc=e)
    return SourceResult(source_id, (), 0, name, error_category=name)


def _log_result(result: SourceResult, log: RunLog) -> None:
    category = (result.error_category or "fetch_failed") if result.error else None
    log.write(
        "collect",
        "source_result",
        source=result.source_id,
        error_category=category,
        count=len(result.records),
        attempt=result.attempts,
        detail=result.error,
    )


def _collect_last30days(
    config: Config, target: Target, token: str | None, log: RunLog, collectors: Collectors
) -> list[tuple[SourceResult, datetime]]:
    """HN・GitHub Search を1回の last30days-skill の実行で集める。"""
    enabled = [
        sid
        for sid, source in (
            ("hackernews", config.hackernews),
            ("github_search", config.github_search),
        )
        if source is not None and source.enabled
    ]
    if not enabled:
        return []
    try:
        results = collectors.last30days(config, target.period, token)
    except Exception as e:
        name = type(e).__name__
        log.write("collect", "failure", source=",".join(enabled), error_category=name, exc=e)
        results = [SourceResult(sid, (), 0, name, error_category=name) for sid in enabled]
    at = collectors.now()
    for result in results:
        _log_result(result, log)
    return [(r, at) for r in results]


def _collect_github(
    config: Config,
    target: Target,
    results: list[SourceResult],
    token: str | None,
    db: sqlite3.Connection | None,
    write: bool,
    featured: set[str],
    log: RunLog,
    collectors: Collectors,
) -> GitHubOutcome | None:
    """GitHub の候補を集め、Star 数を測って急上昇・新規を判定する（D-13）。無効なら None。

    日次だけで行う（Star 数は日次だけ測る。週次は日次の増分を合計する。data-model.md 3.4）。
    言及の経路は、取得に成功した情報源のレコードから拾う。
    """
    settings = config.github_repos
    if settings is None or not settings.enabled:
        return None
    records: tuple[Record, ...] = ()
    star_unjudged = False
    if target.cadence != "daily":
        github = GitHubFetch(0, 0, skipped=True)
    else:
        try:
            client = collectors.github(token, config.limits)
            stored = load_star_snapshot(db, target.key) if db else {}
            # 最初の実行が測り残した日は、再実行で測れた分も判定に入れる
            resumed = bool(stored) and fetch_incomplete(db, GITHUB_REPOS_SOURCE_ID, target.key)
            mentions = [rec for r in results if r.error is None for rec in r.records]
            found = collect_candidates(settings, target.key, mentions, client, db)
            stars = measure_stars(
                found.candidates,
                client,
                target,
                db,
                write=write,
                batch_size=config.limits.github_graphql_batch,
            )
            previous = load_star_snapshot(db, target.key - timedelta(days=1)) if db else {}
            # 同じ対象日の記録があれば（再実行・ドライラン）、その記録だけで判定する。再実行で候補が
            # 変わっても、判定の対象が変わらないようにするため（新しい候補は測って記録するだけ）
            rows = stored if stored and not resumed else {**stored, **_by_repo(stars.snapshots)}
            judged = replace(stars, snapshots=tuple(rows[r] for r in sorted(rows)))
            spikes = judge_star_spikes(settings, target, judged, previous, featured)
            star_unjudged = spikes.star_unjudged
            # 最初の失敗は Search を先に見る
            failure = found if found.error is not None else stars
            # 印（測り残し）は、この対象日の判定に測り残しがあるかだけを表す。記録だけで判定した
            # 再実行（resumed でない）は、新しい候補や Search の失敗があっても印を付けない
            partial = not (stored and not resumed) and (
                failure.error is not None or bool(stars.unmeasured)
            )

            def fetch(readme_calls: int) -> GitHubFetch:
                return GitHubFetch(
                    len(judged.snapshots),
                    found.search_attempts + stars.calls + readme_calls,
                    failure.error,
                    failure.error_category,
                    partial=partial,
                )

            if write:  # README の取得中に止められても、測り残しの印を残す
                assert db is not None
                record_source_fetch(db, log.run_id, fetch(0).to_fetch(collectors.now()))
            records, readme_calls = _with_readmes(spikes.records, client, featured, log)
            github = fetch(readme_calls)
        except Exception as e:
            name = type(e).__name__
            source = GITHUB_REPOS_SOURCE_ID
            log.write("collect", "failure", source=source, error_category=name, exc=e)
            github = GitHubFetch(0, 0, name, name)
    at = collectors.now()
    category = None
    if github.error is not None:
        category = github.error_category or "fetch_failed"
    log.write(
        "collect",
        "source_result",
        source=GITHUB_REPOS_SOURCE_ID,
        error_category=category,
        count=github.measured,
        attempt=github.attempts,
        detail="skipped" if github.skipped else github.error,
    )
    return GitHubOutcome(github, at, records, star_unjudged)


def _by_repo(snapshots):
    return {s.repo: s for s in snapshots}


def _with_readmes(
    records: tuple[Record, ...], client: GitHubClient, featured: set[str], log: RunLog
) -> tuple[tuple[Record, ...], int]:
    """載せるリポジトリ（すでに載せたものを除く）の README を、一次情報の本文にする。

    README の失敗は項目の補足が欠けるだけなので、止めずに続ける（失敗の分類は fetch_readmes が
    ログに残す）。（README を足したレコード、使った要求の回数）を返す。
    想定外の例外のときも、それまでに使った要求の回数を返す。測定が失敗した日のヘッダの扱いは
    #41（部分失敗の分岐）で扱う。
    """
    wanted = [
        r for r in records if r.github is not None and repo_url(r.github.full_name) not in featured
    ]
    if not wanted:
        return records, 0
    before = client.rest_calls
    try:
        found = fetch_readmes([r.github.full_name for r in wanted if r.github], client)
    except Exception as e:  # README は補足。想定外でも急上昇の判定は残す
        log.write(
            "collect", "failure", source=GITHUB_REPOS_SOURCE_ID, error_category=type(e).__name__
        )
        return records, client.rest_calls - before
    kept = []
    for record in records:
        readme = found.readmes.get(record.github.full_name) if record.github else None
        kept.append(with_readme(record, readme) if readme is not None else record)
    return tuple(kept), found.attempts


def _json_value(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"JSON にできない値: {type(value).__name__}")


def load_collected(path: Path) -> list[Item]:
    """collected.json を収集項目に戻す（③ が worker 側の元の収集結果と照合するため。D-04）。"""
    return [_item(raw) for raw in json.loads(path.read_text(encoding="utf-8"))]


def _item(raw: dict) -> Item:
    return Item(
        url=raw["url"],
        sources=tuple(_record(r) for r in raw["sources"]),
        events=tuple(_event(e) for e in raw["events"]),
        attention=tuple(_attention(a) for a in raw["attention"]),
        links=tuple(raw["links"]),
        meta_terms=tuple(raw["meta_terms"]),
        github=_github(raw["github"]),
    )


def _record(raw: dict) -> Record:
    return Record(
        **{
            **raw,
            **{k: _time(raw[k]) for k in ("fetched_at", "published_at", "posted_at")},
            "events": tuple(_event(e) for e in raw["events"]),
            "attention": tuple(_attention(a) for a in raw["attention"]),
            "links": tuple(raw["links"]),
            "github": _github(raw["github"]),
            "meta_terms": tuple(raw["meta_terms"]),
        }
    )


def _event(raw: dict) -> Event:
    return Event(raw["type"], _time(raw["at"]), raw["basis"])


def _attention(raw: dict) -> Attention:
    return Attention(raw["metric"], raw["value"], _time(raw["measured_at"]))


def _github(raw: dict | None) -> GitHubRepo | None:
    if raw is None:
        return None
    return GitHubRepo(
        **{
            **raw,
            "created_at": _time(raw["created_at"]),
            "mentioned_by": tuple(raw["mentioned_by"]),
        }
    )


def _time(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)
