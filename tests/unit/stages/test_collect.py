import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tech_researcher.collect.github_api import GitHubClient, collect_token
from tech_researcher.collect.http import FetchError, Response
from tech_researcher.collect.records import Attention, Event, GitHubRepo, Record, SourceResult
from tech_researcher.config import Feed, load_config
from tech_researcher.db.readonly import open_readonly
from tech_researcher.db.runs import SourceFetch, record_source_fetch, start_run
from tech_researcher.db.schema import open_db
from tech_researcher.logging import RunLog
from tech_researcher.periods import daily_target, weekly_target
from tech_researcher.stages.collect import COLLECTED, Collectors, load_collected, run_collect
from tech_researcher.validate.secret_scan import SecretScanner

# 情報源は各テストで足す（ほかの情報源の収集処理が外部に通信しないように）
CONFIG = replace(
    load_config(Path(__file__).parents[3] / "config" / "tech-researcher.toml"),
    feeds=(),
    qiita=None,
    hackernews=None,
    github_search=None,
    github_repos=None,
)
TARGET = daily_target(date(2026, 10, 24), 1)  # 2026-10-23 0:00〜10-24 0:00（JST）
FETCHED = datetime(2026, 10, 23, 21, 0, tzinfo=UTC)
IN = datetime(2026, 10, 23, 3, 0, tzinfo=UTC)  # JST 12:00
OLD = datetime(2026, 10, 21, 3, 0, tzinfo=UTC)
TOKEN = "ghp" + "_" + "x" * 36  # テスト用のダミー。実行時に組み立てる


def feed(feed_id, enabled=True):
    return Feed(feed_id, feed_id, f"https://{feed_id}.example.com/rss", "new", enabled)


def record(source_id, url, published):
    return Record(
        source_id=source_id,
        url=url,
        title="外部のタイトル",
        published_precision="datetime",
        fetched_at=FETCHED,
        events=(Event("published", published),),
        published_at=published,
    )


def fake_collect(f, limits):
    match f.id:
        case "a":
            return SourceResult(
                "rss:a",
                (
                    record("rss:a", "https://a.example.com/1", IN),
                    record("rss:a", "https://a.example.com/old", OLD),
                ),
                1,
            )
        case "b":
            # a と同じ記事（URL の正規化でまとまる）
            return SourceResult("rss:b", (record("rss:b", "https://a.example.com/1/", IN),), 1)
        case "c":
            return SourceResult("rss:c", (), 3, "再試行を使い切った：HTTP 503")
        case "d":
            raise RuntimeError("想定外 " + TOKEN)
    raise AssertionError(f"呼ばれるはずのない情報源 {f.id}")


def run(tmp_path, feeds):
    log = RunLog(
        tmp_path / "log.jsonl", "dry-20261024-060000-abcd", "daily", TARGET.key, SecretScanner()
    )
    config = replace(CONFIG, feeds=feeds)
    return run_collect(config, TARGET, tmp_path, log, collectors=Collectors(feed=fake_collect)), log


def test_writes_items_in_period_and_returns_counts_only(tmp_path):
    feeds = (feed("a"), feed("b"), feed("c"), feed("off", enabled=False))
    summary, log = run(tmp_path, feeds)

    assert summary == {
        "items": 1,
        "sources": 3,
        "failed_sources": ["rss:c"],
        "unjudged_feed_sources": [],
    }
    items = json.loads((tmp_path / COLLECTED).read_text(encoding="utf-8"))
    assert [i["url"] for i in items] == ["https://a.example.com/1"]
    assert [s["source_id"] for s in items[0]["sources"]] == ["rss:a", "rss:b"]
    assert items[0]["events"][0] == {"type": "published", "at": IN.isoformat(), "basis": None}

    lines = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
    assert [(x["event"], x.get("source"), x.get("count")) for x in lines] == [
        ("start", None, None),
        ("source_result", "rss:a", 2),
        ("source_result", "rss:b", 1),
        ("source_result", "rss:c", 0),
        ("end", None, 1),
    ]
    assert lines[3]["error_category"] == "fetch_failed"
    assert lines[3]["attempt"] == 3
    assert "error_category" not in lines[1]


def test_no_enabled_source_writes_empty_result(tmp_path):
    summary, _ = run(tmp_path, (feed("off", enabled=False),))
    assert summary == {
        "items": 0,
        "sources": 0,
        "failed_sources": [],
        "unjudged_feed_sources": [],
    }
    assert json.loads((tmp_path / COLLECTED).read_text(encoding="utf-8")) == []


def test_unexpected_error_fails_only_that_source(tmp_path):
    summary, log = run(tmp_path, (feed("d"), feed("a")))

    assert summary == {
        "items": 1,
        "sources": 2,
        "failed_sources": ["rss:d"],
        "unjudged_feed_sources": [],
    }
    assert (tmp_path / COLLECTED).exists()
    assert not (tmp_path / f"{COLLECTED}.partial").exists()
    lines = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
    failure, result = lines[1:3]
    assert (failure["event"], failure["source"], failure["error_category"]) == (
        "failure",
        "rss:d",
        "RuntimeError",
    )
    assert TOKEN not in failure["exception"]
    assert (result["event"], result["detail"]) == ("source_result", "RuntimeError")


def test_popularity_is_judged_before_period_filter(tmp_path):
    """人気型フィードは期間で絞る前の全件を記録し、初登場・基準超えの古い記事だけを残す（D-13）。"""
    conn = open_db(tmp_path / "tech-researcher.sqlite3")
    start_run(conn, "20261023-060000-abcd", daily_target(TARGET.key, 1), FETCHED)
    record_source_fetch(conn, "20261023-060000-abcd", SourceFetch("rss:p", "ok", 1, 1, 0, FETCHED))
    conn.execute(
        "INSERT INTO feed_snapshots VALUES"
        " ('rss:p', '2026-10-22', 'https://p.example.com/stay', 1, '[]', '2026-10-22T21:00:00Z')"
    )
    hot = Attention("hatena_bookmarks", 500.0, FETCHED)

    def popular_collect(f, limits):
        match f.id:
            case "p":
                records = (
                    record("rss:p", "https://p.example.com/stay", OLD),  # 前日から載っている
                    record("rss:p", "https://p.example.com/new", OLD),  # 初登場
                )
            case "q":  # 人気型だが前日の記録がない
                records = (
                    record("rss:q", "https://q.example.com/old", OLD),
                    # 古い記事が期間内に投稿され、基準を超えた
                    replace(
                        record("rss:q", "https://q.example.com/hot", OLD),
                        posted_at=IN,
                        attention=(hot,),
                    ),
                )
        return SourceResult(f"rss:{f.id}", records, 1)

    feeds = (replace(feed("p"), kind="popular"), replace(feed("q"), kind="popular"))
    config = replace(CONFIG, feeds=feeds, attention_thresholds={"hatena_bookmarks": 100})
    log = RunLog(
        tmp_path / "log.jsonl", "20261024-060000-abcd", "daily", TARGET.key, SecretScanner()
    )
    start_run(conn, "20261024-060000-abcd", TARGET, FETCHED)
    summary = run_collect(
        config,
        TARGET,
        tmp_path,
        log,
        collectors=Collectors(feed=popular_collect),
        db=conn,
        write_db=True,
    )

    assert summary["items"] == 2
    assert summary["unjudged_feed_sources"] == ["rss:q"]
    items = json.loads((tmp_path / COLLECTED).read_text(encoding="utf-8"))
    assert [(i["url"], [e["basis"] for e in i["events"]]) for i in items] == [
        ("https://p.example.com/new", [None, "feed_entry"]),
        ("https://q.example.com/hot", [None, "threshold"]),
    ]
    snapshots = conn.execute(
        "SELECT source_id, canonical_url FROM feed_snapshots WHERE target = '2026-10-23'"
        " ORDER BY source_id, rank"
    ).fetchall()
    assert snapshots == [
        ("rss:p", "https://p.example.com/stay"),
        ("rss:p", "https://p.example.com/new"),
        ("rss:q", "https://q.example.com/old"),
        ("rss:q", "https://q.example.com/hot"),
    ]
    conn.close()


def test_db_needs_explicit_write_flag(tmp_path):
    """本番で書き忘れると翌日に全件が初登場になるので、db を渡すときは書くかどうかを必ず渡す。"""
    conn = open_db(tmp_path / "tech-researcher.sqlite3")
    log = RunLog(
        tmp_path / "log.jsonl", "20261024-060000-abcd", "daily", TARGET.key, SecretScanner()
    )
    with pytest.raises(ValueError, match="write_db"):
        run_collect(
            CONFIG, TARGET, tmp_path, log, collectors=Collectors(feed=fake_collect), db=conn
        )
    conn.close()


def test_collected_items_can_be_loaded_back(tmp_path):
    """③ は collected.json から元の収集項目を作り直して照合する（D-04）。"""
    repo = GitHubRepo("o/r", OLD, 120, 30, ("https://x.example.com/1",))
    records = (
        replace(
            record("rss:x", "https://x.example.com/1", IN),
            excerpt="抜粋",
            primary_text="本文",
            primary_text_origin="rss_full",
            links=("https://docs.example.com/x",),
            meta_terms=("o/r",),
        ),
        Record(
            source_id="rss:x",
            url="https://x.example.com/repo",
            title="o/r",
            published_precision="unknown",
            fetched_at=FETCHED,
            events=(Event("attention_spike", IN, "star_growth"),),
            posted_at=IN,
            attention=(Attention("stars", 120.0, FETCHED),),
            github=repo,
        ),
    )
    log = RunLog(
        tmp_path / "log.jsonl", "dry-20261024-060000-abcd", "daily", TARGET.key, SecretScanner()
    )
    config = replace(CONFIG, feeds=(feed("x"),))
    run_collect(
        config,
        TARGET,
        tmp_path,
        log,
        collectors=Collectors(feed=lambda f, limits: SourceResult("rss:x", records, 1)),
    )

    items = load_collected(tmp_path / COLLECTED)
    assert [i.sources for i in items] == [(records[0],), (records[1],)]  # 日時・入れ子も元どおり
    assert items[1].github == repo and items[0].links == ("https://docs.example.com/x",)


FULL = load_config(Path(__file__).parents[3] / "config" / "tech-researcher.toml")
PROD = "20261024-060000-abcd"
REPO_CREATED = "2026-10-20T00:00:00Z"


class FakeGitHub:
    """GitHub の REST（Search）と GraphQL の応答の代わり。呼ばれた順を calls に残す。"""

    def __init__(self, calls, search_repos=("new/tool",), graphql_error=None):
        self.calls = calls
        self.search_repos = search_repos
        self.graphql_error = graphql_error

    def fetch(self, url, limits, *, headers, sleep, follow_redirects, data=None):
        if data is None:
            self.calls.append("github_search")
            items = [{"full_name": r} for r in self.search_repos]
            body = {"total_count": len(items), "incomplete_results": False, "items": items}
            return Response(url, 200, json.dumps(body).encode(), 1)
        self.calls.append("github_graphql")
        if self.graphql_error is not None:
            raise FetchError("HTTP 401", 1, self.graphql_error)
        variables = json.loads(data)["variables"]
        nodes = {}
        for key, owner in variables.items():
            if key.startswith("o"):
                name = variables["n" + key[1:]]
                nodes["r" + key[1:]] = {
                    "nameWithOwner": f"{owner}/{name}",
                    "stargazerCount": 300,
                    "createdAt": REPO_CREATED,
                }
        return Response(url, 200, json.dumps({"data": nodes}).encode(), 1)

    def client(self, token, limits):
        return GitHubClient(token, limits, fetch_url=self.fetch, sleep=lambda s: None)


def all_sources_config():
    github = replace(FULL.github_repos, search_topics=(), watch_repos=())
    return replace(
        CONFIG,
        feeds=(feed("a"), feed("c")),
        qiita=FULL.qiita,
        hackernews=FULL.hackernews,
        github_search=FULL.github_search,
        github_repos=github,
    )


def all_collectors(calls, github=None, token=TOKEN):
    def rss(f, limits):
        calls.append(f"rss:{f.id}")
        return fake_collect(f, limits)

    def qiita(settings, period, limits):
        calls.append("qiita")
        return SourceResult("qiita", (), 1)

    def last30days(config, period, given_token):
        calls.append("last30days")
        assert given_token == token  # 収集用トークンを渡す
        hn = record("hackernews", "https://news.example.com/x", IN)
        # HN の記事が GitHub のリポジトリに言及した（候補の言及の経路）
        hn = replace(hn, links=("https://github.com/Mentioned/Repo",))
        return [
            SourceResult("hackernews", (hn,), 1),
            SourceResult("github_search", (), 2, "終了コード 1", error_category="timeout"),
        ]

    github = github or FakeGitHub(calls)
    return Collectors(
        feed=rss,
        qiita=qiita,
        last30days=last30days,
        github=github.client,
        token=lambda: token,
        now=lambda: FETCHED,
    )


def prod_log(tmp_path, run_id=PROD, target=TARGET):
    return RunLog(tmp_path / "log.jsonl", run_id, target.cadence, target.key, SecretScanner())


def test_all_sources_run_in_order_and_are_saved(tmp_path):
    """実装済みの情報源を順に集め、取得結果・収集項目・Star 数を DB に書く（D-13・M 3.4）。"""
    conn = open_db(tmp_path / "tech-researcher.sqlite3")
    start_run(conn, PROD, TARGET, FETCHED)
    calls = []
    summary = run_collect(
        all_sources_config(),
        TARGET,
        tmp_path,
        prod_log(tmp_path),
        collectors=all_collectors(calls),
        db=conn,
        write_db=True,
    )

    # last30days-skill と GitHub の Search を並行させず、候補集めは last30days-skill の後
    assert calls == [
        "rss:a",
        "rss:c",
        "qiita",
        "last30days",
        "github_search",
        "github_graphql",
    ]
    assert summary == {
        "items": 2,
        "sources": 6,
        "failed_sources": ["rss:c", "github_search"],
        "unjudged_feed_sources": [],
    }
    fetches = conn.execute(
        "SELECT source_id, status, error_category, attempts, record_count, in_period_count,"
        " fetched_at FROM source_fetches WHERE run_id = ? ORDER BY rowid",
        (PROD,),
    ).fetchall()
    at = "2026-10-23T21:00:00Z"
    assert fetches == [
        ("rss:a", "ok", None, 1, 2, 1, at),  # 古い記事は期間で落ちた
        ("rss:c", "failed", "fetch_failed", 3, 0, 0, at),  # 収集処理が分類を付けていない
        ("qiita", "no_results", None, 1, 0, 0, at),
        ("hackernews", "ok", None, 1, 1, 1, at),
        ("github_search", "failed", "timeout", 2, 0, 0, at),
        ("github_repos", "ok", None, 2, 2, 0, at),  # Search 1回・GraphQL 1回、2件を測った
    ]
    items = conn.execute(
        "SELECT canonical_url, kind, title, inclusion_event, first_run_id, last_run_id"
        " FROM items ORDER BY item_id"
    ).fetchall()
    assert items == [
        ("https://a.example.com/1", "article", "外部のタイトル", "published", PROD, PROD),
        ("https://news.example.com/x", "article", "外部のタイトル", "published", PROD, PROD),
    ]
    stars = conn.execute(
        "SELECT repo, target, stars_total, routes_json FROM star_snapshots ORDER BY repo"
    ).fetchall()
    assert stars == [
        ("mentioned/repo", "2026-10-23", 300, '["mention"]'),
        ("new/tool", "2026-10-23", 300, '["search"]'),
    ]
    conn.close()


def test_rerun_of_same_target_keeps_item_ids(tmp_path):
    """同じ対象の再実行は items を upsert し、ID と最初の実行を変えない（M 3.7）。"""
    conn = open_db(tmp_path / "tech-researcher.sqlite3")
    config = replace(CONFIG, feeds=(feed("a"),))
    for run_id in (PROD, "20261024-070000-beef"):
        start_run(conn, run_id, TARGET, FETCHED)
        run_collect(
            config,
            TARGET,
            tmp_path,
            prod_log(tmp_path, run_id),
            collectors=Collectors(feed=fake_collect),
            db=conn,
            write_db=True,
        )
    assert conn.execute("SELECT item_id, first_run_id, last_run_id FROM items").fetchall() == [
        (1, PROD, "20261024-070000-beef")
    ]
    assert conn.execute("SELECT count(*) FROM source_fetches").fetchone() == (2,)
    conn.close()


def test_dry_run_reads_db_but_writes_nothing(tmp_path):
    """ドライランは読み取り専用の接続を受け取り、記録も結果も書かない（D-22）。"""
    path = tmp_path / "tech-researcher.sqlite3"
    open_db(path).close()
    conn = open_readonly(path)
    calls = []
    summary = run_collect(
        all_sources_config(),
        TARGET,
        tmp_path,
        prod_log(tmp_path, "dry-20261024-060000-abcd"),
        collectors=all_collectors(calls),
        db=conn,
        write_db=False,
    )
    conn.close()
    assert summary["items"] == 2
    assert "github_graphql" in calls  # 測るが書かない
    conn = open_db(path)
    for table in ("runs", "source_fetches", "items", "star_snapshots", "feed_snapshots"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,), table
    conn.close()


def test_weekly_skips_star_measurement_and_items(tmp_path):
    """週次は Star を測らず（skipped）、items は週次の材料の Issue（#44）まで書かない。"""
    weekly = weekly_target(date(2026, 10, 26), 7)
    conn = open_db(tmp_path / "tech-researcher.sqlite3")
    start_run(conn, PROD, weekly, FETCHED)
    calls = []
    summary = run_collect(
        all_sources_config(),
        weekly,
        tmp_path,
        prod_log(tmp_path, target=weekly),
        collectors=all_collectors(calls),
        db=conn,
        write_db=True,
    )
    assert "github_search" not in calls and "github_graphql" not in calls
    assert summary["sources"] == 5  # 取得しなかった github_repos は数えない
    assert conn.execute(
        "SELECT status, record_count FROM source_fetches WHERE source_id = 'github_repos'"
    ).fetchall() == [("skipped", 0)]
    assert conn.execute("SELECT count(*) FROM items").fetchone() == (0,)
    conn.close()


def test_github_without_measurement_is_a_failed_source(tmp_path):
    """測れたリポジトリがなく失敗があれば、github_repos を取得できなかった情報源にする。"""
    conn = open_db(tmp_path / "tech-researcher.sqlite3")
    start_run(conn, PROD, TARGET, FETCHED)
    calls = []
    config = replace(all_sources_config(), feeds=(feed("a"),), hackernews=None, github_search=None)
    summary = run_collect(
        config,
        TARGET,
        tmp_path,
        prod_log(tmp_path),
        collectors=all_collectors(calls, github=FakeGitHub(calls, graphql_error=401)),
        db=conn,
        write_db=True,
    )
    assert summary["failed_sources"] == ["github_repos"]
    assert conn.execute(
        "SELECT status, error_category, record_count FROM source_fetches"
        " WHERE source_id = 'github_repos'"
    ).fetchall() == [("failed", "auth", 0)]
    conn.close()


def test_github_token_is_read_only_when_a_github_source_is_enabled(tmp_path):
    def no_token():
        raise AssertionError("読まない")

    summary = run_collect(
        replace(CONFIG, feeds=(feed("a"),)),
        TARGET,
        tmp_path,
        prod_log(tmp_path, "dry-20261024-060000-abcd"),
        collectors=Collectors(feed=fake_collect, token=no_token),
    )
    assert summary["sources"] == 1


def test_unexpected_error_in_last30days_fails_its_sources(tmp_path):
    def broken(config, period, token):
        raise RuntimeError("想定外 " + TOKEN)

    config = replace(CONFIG, hackernews=FULL.hackernews, github_search=FULL.github_search)
    log = prod_log(tmp_path, "dry-20261024-060000-abcd")
    summary = run_collect(
        config, TARGET, tmp_path, log, collectors=Collectors(last30days=broken, token=lambda: None)
    )
    assert summary["failed_sources"] == ["hackernews", "github_search"]
    assert TOKEN not in log.path.read_text(encoding="utf-8")


def test_malformed_token_fails_only_github_sources(tmp_path):
    """収集用トークンの形が誤っていても、GitHub を使わない情報源は続ける（US-E6-04）。"""
    calls = []
    bad = "ghp_" + "x" * 36 + "\n改行"
    collectors = replace(
        all_collectors(calls, token=None), token=lambda: collect_token({"GITHUB_TOKEN": bad})
    )
    log = prod_log(tmp_path, "dry-20261024-060000-abcd")
    summary = run_collect(all_sources_config(), TARGET, tmp_path, log, collectors=collectors)
    assert summary["items"] == 2  # RSS・HN（偽物）は続けた
    assert "github_repos" in summary["failed_sources"]
    assert "github_graphql" not in calls  # トークンがないので GraphQL は呼ばない
    assert bad.split("\n")[0] not in log.path.read_text(encoding="utf-8")


def test_fetches_are_recorded_as_each_source_finishes(tmp_path):
    """① が途中で止められても、取得を終えた情報源の記録は残る（取得失敗も履歴に残す）。"""
    conn = open_db(tmp_path / "tech-researcher.sqlite3")
    start_run(conn, PROD, TARGET, FETCHED)

    def stopped(settings, period, limits):
        raise KeyboardInterrupt  # 上限で止められたときの代わり（情報源の失敗にはしない）

    config = replace(CONFIG, feeds=(feed("a"), feed("c")), qiita=FULL.qiita)
    collectors = Collectors(feed=fake_collect, qiita=stopped, now=lambda: FETCHED)
    with pytest.raises(KeyboardInterrupt):
        run_collect(
            config,
            TARGET,
            tmp_path,
            prod_log(tmp_path),
            collectors=collectors,
            db=conn,
            write_db=True,
        )
    assert conn.execute(
        "SELECT source_id, status, in_period_count FROM source_fetches ORDER BY rowid"
    ).fetchall() == [("rss:a", "ok", 0), ("rss:c", "failed", 0)]
    conn.close()
