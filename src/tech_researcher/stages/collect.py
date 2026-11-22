"""① 収集（D-04。worker で動く）。

設定で有効にした情報源から順に集め、注目の急上昇を判定し（D-13）、まとめて期間で絞り、
実行のフォルダの collected.json に書く。DB の接続を渡されたときは、前日の記録を読み、書くとき
（本番）は人気型フィードの取得記録・Star 数・取得結果（source_fetches）・収集項目（items）を書く
（stages/collect_store.py）。runs の開始と終わりは呼ぶ側（cli.py）が書く。

集める順：RSS・Atom → Qiita → last30days-skill（HN・GitHub Search）→ GitHub の候補集め（Search）
→ Star 数の測定（GraphQL）。last30days-skill と候補集めはどちらも同じ収集用トークンで GitHub の
Search を使うので、並行させずに順に動かす（候補集めの言及の経路は、それより前の情報源のレコードを
使う）。GitHub Releases は最後に集める（追跡先のリリースを、候補集めの「言及」にしないため）。
取れなかった追跡先は source_fetches の failed_parts_json に残し、取れなかった情報源と一緒に
unavailable.json に書く（③ が末尾の「取得できなかった情報源」にする）。情報源ごとの取得結果は
fetches.json にも書く（実行エラー Issue の表と、部分的な失敗の情報源の名前の元。統括スクリプトが
読む）。
Star 数の測定の後に、急上昇・新規のリポジトリを判定し（github_spikes）、載せる分の README を取る。
まとめて期間で絞った後、すでに載せた急上昇・新規と前日に載せた記事は、AI に渡さない（collected.json
に入れない）。DB の items には hold_reason を付けて残す（collect/holds.py）。
週次は、取り直した分と DB にある日次の items を URL でまとめ（stages/weekly_items.py）、まとめた分を
collected.json と週次の items にする（日次の掲載除外は持ち込まない。Star・人気型は測らない）。
時間の上限（structure.md 6.4）：① 全体の上限（段階の上限から collect_wrapup_s を引いた時刻）を
過ぎたら、残りの情報源は取得せずに timeout の失敗にして、終わった情報源の分で続ける。情報源ごとの
上限は last30days-skill・Qiita・GitHub Releases に、全体の上限は README の取得に渡す（Deadlines）。
外部由来の文（タイトル・本文）はファイルにだけ書き、戻り値（標準出力に出す要約）には件数と
情報源の ID だけを入れる（structure.md 3.2）。
前日の記録がなく判定しなかった急上昇は unjudged.json に書き、③ がヘッダの定型文にする。
collected.json は load_collected で収集項目に戻せる（② の入力の用意と ③ の照合に使う）。
collected.json の項目と同じ順の ID を item-ids.json に書く。本番は items の主キーから
作る ID（i48213）、ドライランは DB にすでにある項目はその ID、新しい項目は仮の ID（d1, d2, …。
data-model.md 2.6）にする。
"""

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path

from tech_researcher.collect.aggregate import aggregate
from tech_researcher.collect.github_api import GitHubClient, GitHubError, collect_token
from tech_researcher.collect.github_candidates import collect_candidates
from tech_researcher.collect.github_releases import SOURCE_ID as RELEASES_SOURCE_ID
from tech_researcher.collect.github_releases import collect_releases, fetch_readmes, with_readme
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
from tech_researcher.config import Config, Feed, Limits
from tech_researcher.db.history import featured_urls, shown_urls
from tech_researcher.db.items import lookup_item_refs
from tech_researcher.db.readonly import tentative_ids
from tech_researcher.db.runs import fetch_incomplete, record_source_fetch
from tech_researcher.db.star_snapshots import load_star_snapshot
from tech_researcher.logging import RunLog
from tech_researcher.periods import Period, Target
from tech_researcher.render.common import fetch_reason
from tech_researcher.stages.collect_store import (
    GITHUB_REPOS_SOURCE_ID,
    GitHubFetch,
    fetch_entries,
    in_period_counts,
    save_collect,
    source_fetch,
    unavailable_sources,
)
from tech_researcher.stages.weekly_items import load_daily_rows, weekly_rows

OUT_OF_TIME = "収集全体の時間の上限に達した"
COLLECTED = "collected.json"
ITEM_IDS = "item-ids.json"  # collected.json の項目と同じ順の ID（② の入力と ③ の照合で使う）
UNAVAILABLE = "unavailable.json"  # 取得できなかった情報源と、取れなかった部分（末尾の表示の元）
FETCHES = "fetches.json"  # 情報源ごとの取得結果（実行エラー Issue の表と、部分的な失敗の元）
UNJUDGED = "unjudged.json"  # 前日の記録がなく判定しなかった急上昇（ヘッダの定型文の元。1.6）
FEED_JUDGMENT = "人気型フィードへの初登場"
STAR_JUDGMENT = "GitHub の Star の急増"
# last30days-skill を置いた場所（作業ディレクトリから。worker では /app、ホストではリポジトリ）
LAST30DAYS_ROOT = Path("third_party/last30days")

# deadline は情報源ごとの時間の上限（Collectors の clock の値。structure.md 6.4）
CollectFeed = Callable[[Feed, Limits], SourceResult]
CollectQiita = Callable[..., SourceResult]  # （設定, 対象期間, limits, deadline=）
# （設定, 対象期間, 収集用トークン, deadline=）→ 有効な情報源（hackernews・github_search）ごとの結果
CollectLast30Days = Callable[..., list[SourceResult]]
CollectReleases = Callable[..., SourceResult]  # （設定, 対象期間, クライアント, deadline=）


def _last30days(
    config: Config,
    period: Period,
    token: str | None,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[SourceResult]:
    return collect_last30days(
        config, period, LAST30DAYS_ROOT, github_token=token, deadline=deadline, clock=clock
    )


@dataclass(frozen=True)
class Collectors:
    """各情報源の収集処理（テストで偽物に差し替える）。token は収集用トークンを読む。"""

    feed: CollectFeed = collect_feed
    qiita: CollectQiita = collect_qiita
    last30days: CollectLast30Days = _last30days
    releases: CollectReleases = collect_releases
    github: Callable[[str | None, Limits], GitHubClient] = GitHubClient
    token: Callable[[], str | None] = collect_token
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    # 時間の上限の判定に使う時計（テストで偽物に差し替える）。既定の収集器は自分で time.monotonic
    # を使うので、収集器を差し替えずに clock だけを変えると食い違う
    clock: Callable[[], float] = time.monotonic


DEFAULT_COLLECTORS = Collectors()


@dataclass(frozen=True)
class GitHubOutcome:
    """候補集め・Star 数の測定・急上昇の判定の結果。records は急上昇・新規のレコード。"""

    fetch: GitHubFetch
    at: datetime
    records: tuple[Record, ...] = ()
    star_unjudged: bool = False


MINUTE = 60.0  # 設定の上限（分）を秒にする


@dataclass(frozen=True)
class Deadlines:
    """① の時間の上限（structure.md 6.4）。clock の値で表す。

    overall は ① 全体の上限（段階の上限から collect_wrapup_s を引いた時刻。まとめて書く時間を
    残す）。過ぎたら、残りの情報源は取得せずに timeout の失敗にし、終わった情報源の分で続ける
    （US-E6-03）。source は情報源ごとの上限（① 全体の内数）。
    """

    clock: Callable[[], float]
    overall: float
    per_source_s: float

    @classmethod
    def start(cls, limits: Limits, target: Target, clock: Callable[[], float]) -> Deadlines:
        stage = getattr(limits, target.cadence)
        total = max(stage.collect * MINUTE - limits.collect_wrapup_s, 0.0)
        return cls(clock, clock() + total, stage.collect_per_source * MINUTE)

    def expired(self) -> bool:
        return self.clock() >= self.overall

    def left(self) -> float:
        return self.overall - self.clock()

    def source(self) -> float:
        return min(self.clock() + self.per_source_s, self.overall)


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
    deadlines = Deadlines.start(limits, target, collectors.clock)
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
            _collect_one(
                feed_source_id(f),
                lambda until, f=f: collectors.feed(
                    f, limits, deadline=until, clock=collectors.clock
                ),
                log,
                collectors,
                deadlines,
            )
        )
    if config.qiita is not None and config.qiita.enabled:
        qiita = config.qiita
        keep(
            _collect_one(
                QIITA_SOURCE_ID,
                lambda until: collectors.qiita(
                    qiita, target.period, limits, deadline=until, clock=collectors.clock
                ),
                log,
                collectors,
                deadlines,
            )
        )
    # GitHub を使う情報源があるときだけ読む（使わないのに形の誤りで止めないように）
    uses_github = any(
        s is not None and s.enabled
        for s in (
            config.hackernews,
            config.github_search,
            config.github_repos,
            config.github_releases,
        )
    )
    token = _token(collectors, log) if uses_github else None
    keep(*_collect_last30days(config, target, token, log, collectors, deadlines))
    results = [r for r, _ in fetched]
    # REST の回数の上限（1回の実行あたり）はクライアントごとに数える。候補集め・README・Releases で
    # 1つのクライアントを共有し、上限を超えないようにする
    client = cache(lambda: _github_client(collectors, token, limits, deadlines))
    github = _collect_github(
        config, target, results, client, db, write, featured, log, collectors, deadlines
    )
    if github is not None and write:
        assert db is not None
        record_source_fetch(db, log.run_id, github.fetch.to_fetch(github.at))
    releases = config.github_releases
    if releases is not None and releases.enabled:
        keep(
            _collect_one(
                RELEASES_SOURCE_ID,
                lambda until: collectors.releases(
                    releases, target.period, client(), deadline=until, clock=collectors.clock
                ),
                log,
                collectors,
                deadlines,
            )
        )
        results = [r for r, _ in fetched]
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
    counts = in_period_counts(observed)
    fetches = [source_fetch(r, at, counts[r.source_id]) for r, at in fetched]
    if github is not None:
        fetches.append(github.fetch.to_fetch(github.at, counts[GITHUB_REPOS_SOURCE_ID]))
    rows = None
    stored = observed  # items に書く項目（日次は AI に渡さない項目も含む）
    if target.cadence == "weekly":
        # 週次の材料：取り直した分と、DB にある日次の収集結果を URL でまとめる（3.8）
        rows = weekly_rows(target, items, load_daily_rows(db, target) if db is not None else [])
        items = stored = [r.item for r in rows]
    refs: dict[str, str] = {}
    if write:
        assert db is not None
        saved = save_collect(db, log.run_id, target, fetches, observed, holds, rows)
        refs = dict(zip((i.url for i in stored), saved, strict=True))
    elif db is not None:  # ドライラン：本番の実行が書いた項目の ID
        refs = lookup_item_refs(db, target.cadence, target.key, (i.url for i in items))
    # 途中で止まっても壊れたファイルを残さないよう、一時ファイルに書いてから置き換える
    partial = run_dir / f"{COLLECTED}.partial"
    partial.write_text(
        json.dumps([asdict(i) for i in items], ensure_ascii=False, default=_json_value),
        encoding="utf-8",
    )
    partial.replace(run_dir / COLLECTED)
    # Star 数の測定に失敗した日は、失敗を「取得できなかった情報源」に出すので、ヘッダの
    # 「前日の記録がないため判定していません」は出さない（理由が前日の記録でなく測定の失敗になる）
    unjudged_star = github is not None and github.star_unjudged and not github.fetch.failed
    if target.cadence == "daily":
        _write_unjudged(run_dir, judged.unjudged, unjudged_star)
        _write_json(run_dir / UNAVAILABLE, unavailable_sources(fetches))
        _write_json(run_dir / FETCHES, fetch_entries(fetches))
    _write_item_ids(run_dir, items, refs)
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
    _write_json(run_dir / UNJUDGED, {"feed_sources": list(feeds), "github_stars": star})


def _write_item_ids(run_dir: Path, items: list[Item], refs: dict[str, str]) -> None:
    """collected.json と同じ順の ID。DB にない項目（ドライラン）は、仮の ID を順に付ける。"""
    fresh = tentative_ids()
    _write_json(run_dir / ITEM_IDS, [refs.get(i.url) or next(fresh) for i in items])


def _write_json(path: Path, value: object) -> None:
    # 途中で止まっても壊れたファイルを残さないよう、一時ファイルに書いてから置き換える
    partial = path.with_name(f"{path.name}.partial")
    partial.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    partial.replace(path)


def load_item_ids(run_dir: Path) -> list[str]:
    """item-ids.json（collected.json の項目と同じ順の ID）。"""
    return json.loads((run_dir / ITEM_IDS).read_text(encoding="utf-8"))


def unavailable_notes(run_dir: Path, config: Config) -> list[tuple[str, str]]:
    """末尾の「取得できなかった情報源」の元：（表示名、理由）。なければ空。

    内訳のある情報源は部分ごとに「情報源名（取れなかった部分）」で出す（report-templates.md 1.4）。
    """
    path = run_dir / UNAVAILABLE
    if not path.is_file():
        return []
    names = source_names(config)
    notes: list[tuple[str, str]] = []
    for entry in json.loads(path.read_text(encoding="utf-8")):
        name = names.get(entry["source_id"], entry["source_id"])
        if "parts" in entry:
            notes += [
                (f"{name}（{p['name']}）", fetch_reason(p["category"])) for p in entry["parts"]
            ]
        else:
            notes.append((name, fetch_reason(entry["category"])))
    return notes


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
    source_id: str,
    collect: Callable[[float], SourceResult],
    log: RunLog,
    collectors: Collectors,
    deadlines: Deadlines,
) -> tuple[SourceResult, datetime]:
    """1つの情報源を集めてログに残す。想定外の例外もその情報源の失敗にする（US-E6-04）。

    ① 全体の上限を過ぎていたら、取得せずに timeout の失敗にする。collect には情報源ごとの
    上限（clock の値）を渡す。
    """
    if deadlines.expired():
        result = _out_of_time(source_id, log)
    else:
        try:
            result = collect(deadlines.source())
        except Exception as e:
            result = _unexpected(source_id, e, log)
    _log_result(result, log)
    return result, collectors.now()


def _out_of_time(source_id: str, log: RunLog) -> SourceResult:
    log.write("collect", "failure", source=source_id, error_category="timeout", detail=OUT_OF_TIME)
    return SourceResult(source_id, (), 0, OUT_OF_TIME, error_category="timeout")


def _unexpected(source_id: str, e: Exception, log: RunLog) -> SourceResult:
    # 例外の中身（外部由来の文を含みうる）は、マスクするログの exception にだけ残す
    name = type(e).__name__
    log.write("collect", "failure", source=source_id, error_category=name, exc=e)
    return SourceResult(source_id, (), 0, name, error_category=name)


def _log_result(result: SourceResult, log: RunLog) -> None:
    category = (result.error_category or "fetch_failed") if result.error else None
    detail = result.error
    if detail is None and result.failed_parts:  # 一部だけ取れなかった（名前は設定の値）
        parts = "、".join(f"{p.name}（{p.error_category}）" for p in result.failed_parts)
        detail = f"取れなかった追跡先：{parts}"
    log.write(
        "collect",
        "source_result",
        source=result.source_id,
        error_category=category,
        count=len(result.records),
        attempt=result.attempts,
        detail=detail,
    )


def _collect_last30days(
    config: Config,
    target: Target,
    token: str | None,
    log: RunLog,
    collectors: Collectors,
    deadlines: Deadlines,
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
        if deadlines.expired():
            results = [_out_of_time(sid, log) for sid in enabled]
        else:
            results = collectors.last30days(
                config,
                target.period,
                token,
                deadline=deadlines.source(),
                clock=collectors.clock,
            )
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
    get_client: Callable[[], GitHubClient],
    db: sqlite3.Connection | None,
    write: bool,
    featured: set[str],
    log: RunLog,
    collectors: Collectors,
    deadlines: Deadlines,
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
    until = deadlines.source()  # 情報源ごとの上限（README の取得に渡す）
    if target.cadence != "daily":
        github = GitHubFetch(0, 0, skipped=True)
    elif deadlines.expired() or deadlines.left() < _github_min_seconds(config):
        # 残りの時間が Search の最短の所要時間より短い（途中で止められず ① 全体が落ちるため）。
        # 取得せず失敗にする
        log.write(
            "collect",
            "failure",
            source=GITHUB_REPOS_SOURCE_ID,
            error_category="timeout",
            detail=OUT_OF_TIME,
        )
        github = GitHubFetch(0, 0, OUT_OF_TIME, "timeout")
    else:
        try:
            client = get_client()
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
            records, readme_calls = _with_readmes(
                spikes.records, client, featured, log, until, collectors.clock
            )
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


def _github_client(
    collectors: Collectors, token: str | None, limits: Limits, deadlines: Deadlines
) -> GitHubClient:
    """1回の実行で共有する GitHub のクライアント。① 全体の上限を、すべての要求の時間の上限にする
    （要求の時間を残りまで縮め、残りに収まらない再試行や待ちは始めない）。"""
    client = collectors.github(token, limits)
    client.limit_time(deadlines.overall, deadlines.clock)
    return client


def _github_min_seconds(config: Config) -> float:
    """GitHub の候補集めを始めるのに要る残りの時間（秒）。Search を使い切るまでにかかる時間
    （1分あたりの回数で間隔をあける。途中では止めない）。Search を使わなければ 0 で、上限を
    過ぎていなければ始める。"""
    settings = config.github_repos
    if settings is None or not (settings.search_topics or settings.search_without_topic):
        return 0.0
    limits = config.limits
    return limits.github_search_calls / limits.github_search_per_minute * MINUTE


def _by_repo(snapshots):
    return {s.repo: s for s in snapshots}


def _with_readmes(
    records: tuple[Record, ...],
    client: GitHubClient,
    featured: set[str],
    log: RunLog,
    deadline: float,
    clock: Callable[[], float],
) -> tuple[tuple[Record, ...], int]:
    """載せるリポジトリ（すでに載せたものを除く）の README を、一次情報の本文にする。

    README の失敗は項目の補足が欠けるだけなので、止めずに続ける（失敗の分類は fetch_readmes が
    ログに残す）。（README を足したレコード、使った要求の回数）を返す。
    想定外の例外のときも、それまでに使った要求の回数を返す。
    deadline（github_repos の情報源ごとの上限。① 全体の上限で切る）を過ぎたら、残りの README は
    取らない（README なしで載る）。
    """
    wanted = [
        r for r in records if r.github is not None and repo_url(r.github.full_name) not in featured
    ]
    if not wanted:
        return records, 0
    before = client.rest_calls
    try:
        found = fetch_readmes(
            [r.github.full_name for r in wanted if r.github],
            client,
            deadline=deadline,
            clock=clock,
        )
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
