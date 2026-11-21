import base64
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tech_researcher.collect.github_api import GitHubClient, collect_token
from tech_researcher.collect.http import FetchError, Response
from tech_researcher.collect.records import Attention, Event, GitHubRepo, Record, SourceResult
from tech_researcher.config import Feed, load_config
from tech_researcher.db.issues import record_issue
from tech_researcher.db.readonly import open_readonly
from tech_researcher.db.reports import EntryRow, ReportRow, save_report
from tech_researcher.db.runs import SourceFetch, record_source_fetch, start_run
from tech_researcher.db.schema import open_db
from tech_researcher.db.star_snapshots import StarSnapshot, record_star_snapshots
from tech_researcher.logging import RunLog
from tech_researcher.periods import daily_target, weekly_target
from tech_researcher.stages.collect import (
    COLLECTED,
    STAR_JUDGMENT,
    Collectors,
    load_collected,
    run_collect,
    unavailable_notes,
    unjudged_notes,
)
from tech_researcher.validate.secret_scan import SecretScanner

# 情報源は各テストで足す（ほかの情報源の収集処理が外部に通信しないように）
CONFIG = replace(
    load_config(Path(__file__).parents[3] / "config" / "tech-researcher.toml"),
    feeds=(),
    qiita=None,
    hackernews=None,
    github_search=None,
    github_repos=None,
    github_releases=None,
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


def fake_collect(f, limits, **_):
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
        "unjudged_star": False,
        "held_items": 0,
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
        "unjudged_star": False,
        "held_items": 0,
    }
    assert json.loads((tmp_path / COLLECTED).read_text(encoding="utf-8")) == []


def test_unexpected_error_fails_only_that_source(tmp_path):
    summary, log = run(tmp_path, (feed("d"), feed("a")))

    assert summary == {
        "items": 1,
        "sources": 2,
        "failed_sources": ["rss:d"],
        "unjudged_feed_sources": [],
        "unjudged_star": False,
        "held_items": 0,
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

    def popular_collect(f, limits, **_):
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
        collectors=Collectors(feed=lambda f, limits, **_: SourceResult("rss:x", records, 1)),
    )

    items = load_collected(tmp_path / COLLECTED)
    assert [i.sources for i in items] == [(records[0],), (records[1],)]  # 日時・入れ子も元どおり
    assert items[1].github == repo and items[0].links == ("https://docs.example.com/x",)


FULL = load_config(Path(__file__).parents[3] / "config" / "tech-researcher.toml")
PROD = "20261024-060000-abcd"
REPO_CREATED = "2026-10-20T00:00:00Z"


class FakeGitHub:
    """GitHub の REST（Search）と GraphQL の応答の代わり。呼ばれた順を calls に残す。"""

    def __init__(
        self,
        calls,
        search_repos=("new/tool",),
        graphql_error=None,
        stars=None,
        readmes=None,
        created=None,
        graphql_fail=(),
        search_error=None,
    ):
        self.calls = calls
        self.search_repos = search_repos
        self.graphql_error = graphql_error
        self.stars = stars or {}  # リポジトリ → 累計 Star 数（なければ 300）
        self.readmes = readmes or {}  # リポジトリ → README の本文（なければ 404）
        self.graphql_fail = set(graphql_fail)  # この測定の問い合わせが失敗するリポジトリ
        self.search_error = search_error  # Search の失敗の HTTP の状態
        self.created = created or {}  # リポジトリ → 作成日時（なければ REPO_CREATED）

    def fetch(self, url, limits, *, headers, sleep, follow_redirects, data=None, **_):
        if "/readme" in url:
            repo = url.split("/repos/")[1].removesuffix("/readme")
            self.calls.append(f"readme:{repo}")
            if repo not in self.readmes:
                raise FetchError("HTTP 404", 1, 404)
            content = base64.b64encode(self.readmes[repo].encode()).decode()
            body = {"encoding": "base64", "content": content}
            return Response(url, 200, json.dumps(body).encode(), 1)
        if data is None:
            self.calls.append("github_search")
            if self.search_error is not None:
                raise FetchError(f"HTTP {self.search_error}", 1, self.search_error)
            items = [{"full_name": r} for r in self.search_repos]
            body = {"total_count": len(items), "incomplete_results": False, "items": items}
            return Response(url, 200, json.dumps(body).encode(), 1)
        self.calls.append("github_graphql")
        if self.graphql_error is not None:
            raise FetchError("HTTP 401", 1, self.graphql_error)
        variables = json.loads(data)["variables"]
        nodes = {}
        errors = []
        for key, owner in variables.items():
            if key.startswith("o"):
                name = variables["n" + key[1:]]
                if f"{owner}/{name}" in self.graphql_fail:
                    nodes["r" + key[1:]] = None
                    errors.append({"type": "INTERNAL", "path": ["r" + key[1:]]})
                    continue
                nodes["r" + key[1:]] = {
                    "nameWithOwner": f"{owner}/{name}",
                    "stargazerCount": self.stars.get(f"{owner}/{name}", 300),
                    "createdAt": self.created.get(f"{owner}/{name}", REPO_CREATED),
                }
        body = {"data": nodes, **({"errors": errors} if errors else {})}
        return Response(url, 200, json.dumps(body).encode(), 1)

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


def all_collectors(calls, github=None, token=TOKEN, hn_record=None):
    def rss(f, limits, **_):
        calls.append(f"rss:{f.id}")
        return fake_collect(f, limits)

    def qiita(settings, period, limits, **_):
        calls.append("qiita")
        return SourceResult("qiita", (), 1)

    def last30days(config, period, given_token, **_):
        calls.append("last30days")
        assert given_token == token  # 収集用トークンを渡す
        hn = hn_record or record("hackernews", "https://news.example.com/x", IN)
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
    github = FakeGitHub(calls, readmes={"mentioned/repo": "# Repo\nhttps://docs.example.com/r"})
    summary = run_collect(
        all_sources_config(),
        TARGET,
        tmp_path,
        prod_log(tmp_path),
        collectors=all_collectors(calls, github=github),
        db=conn,
        write_db=True,
    )

    # last30days-skill と GitHub の Search を並行させず、候補集めは last30days-skill の後。
    # README は Star 数の測定の後に、新規として残したリポジトリについて取る
    assert calls == [
        "rss:a",
        "rss:c",
        "qiita",
        "last30days",
        "github_search",
        "github_graphql",
        "readme:mentioned/repo",
        "readme:new/tool",
    ]
    assert summary == {
        "items": 4,
        "sources": 6,
        "failed_sources": ["rss:c", "github_search"],
        "unjudged_feed_sources": [],
        "unjudged_star": True,  # 前日の測定が1件もない
        "held_items": 0,
    }
    fetches = conn.execute(
        "SELECT source_id, status, error_category, attempts, record_count, in_period_count,"
        " fetched_at FROM source_fetches WHERE run_id = ? ORDER BY rowid",
        (PROD,),
    ).fetchall()
    # 情報源ごとの取得結果（実行エラー Issue の表の元）。ok の行の測り残しの印は失敗の理由にしない
    saved = json.loads((tmp_path / "fetches.json").read_text(encoding="utf-8"))
    assert [(f["source_id"], f["status"], f["category"], f["attempts"]) for f in saved] == [
        ("rss:a", "ok", None, 1),
        ("rss:c", "failed", "fetch_failed", 3),
        ("qiita", "no_results", None, 1),
        ("hackernews", "ok", None, 1),
        ("github_search", "failed", "timeout", 2),
        ("github_repos", "ok", None, 4),
    ]
    at = "2026-10-23T21:00:00Z"
    assert fetches == [
        ("rss:a", "ok", None, 1, 2, 1, at),  # 古い記事は期間で落ちた
        ("rss:c", "failed", "fetch_failed", 3, 0, 0, at),  # 収集処理が分類を付けていない
        ("qiita", "no_results", None, 1, 0, 0, at),
        ("hackernews", "ok", None, 1, 1, 1, at),
        ("github_search", "failed", "timeout", 2, 0, 0, at),
        # Search 1回・GraphQL 1回・README 2回。2件を測り、2件とも新しい OSS として残った
        ("github_repos", "ok", None, 4, 2, 2, at),
    ]
    items = conn.execute(
        "SELECT canonical_url, kind, title, inclusion_event, first_run_id, last_run_id"
        " FROM items ORDER BY item_id"
    ).fetchall()
    assert items == [
        ("https://a.example.com/1", "article", "外部のタイトル", "published", PROD, PROD),
        ("https://news.example.com/x", "article", "外部のタイトル", "published", PROD, PROD),
        ("https://github.com/mentioned/repo", "repo", "mentioned/repo", "new_repo", PROD, PROD),
        ("https://github.com/new/tool", "repo", "new/tool", "new_repo", PROD, PROD),
    ]
    collected = {i.url: i for i in load_collected(tmp_path / COLLECTED)}
    repo = collected["https://github.com/mentioned/repo"]
    assert repo.sources[0].primary_text_origin == "github_readme"  # README は一次情報の本文
    assert "https://docs.example.com/r" in repo.links
    assert collected["https://github.com/new/tool"].sources[0].primary_text is None  # README なし
    assert repo.github.mentioned_by == ("https://news.example.com/x",)
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
    assert summary["items"] == 4  # 記事2件と、新しい OSS 2件
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
    # 測定に失敗した日は、失敗を「取得できなかった情報源」に出す。前日の記録がないための
    # 「判定していません」はヘッダに出さない（理由が測定の失敗になるため）
    assert summary["unjudged_star"] is False
    assert unjudged_notes(tmp_path, config) == []
    assert unavailable_notes(tmp_path, config) == [("GitHub（新着・急上昇）", "認証エラー")]
    fetches = json.loads((tmp_path / "fetches.json").read_text(encoding="utf-8"))
    assert [(f["source_id"], f["status"], f["category"]) for f in fetches][-1] == (
        "github_repos",
        "failed",
        "auth",
    )


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
    def broken(config, period, token, **_):
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

    def stopped(settings, period, limits, **_):
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


OLD_CREATED = "2025-01-01T00:00:00Z"


def previous_day(conn, hot, featured):
    """前日（対象日の前日 10-22）の Star の測定と、10-21 の日次レポートに載せた急上昇の項目。"""
    day = date(2026, 10, 22)
    at = datetime(2026, 10, 21, 21, 0, tzinfo=UTC)
    created = datetime(2025, 1, 1, tzinfo=UTC)
    record_star_snapshots(
        conn, day, [StarSnapshot(r, 1000, created, at, ("search",)) for r in (*hot, *featured)]
    )
    run = "20261022-060000-aaaa"
    start_run(conn, run, daily_target(date(2026, 10, 22), 1), FETCHED)
    events = [{"type": "attention_spike", "at": "2026-10-21T21:00:00Z", "basis": "star_growth"}]
    ids = []
    for repo in featured:
        (item_id,) = conn.execute(
            "INSERT INTO items (cadence, target, canonical_url, kind, title, published_precision,"
            " inclusion_event, inclusion_at, events_json, attention_json, sources_json,"
            " links_json, meta_terms_json, first_run_id, last_run_id) VALUES ('daily',"
            " '2026-10-21', ?, 'repo', 't', 'datetime', 'attention_spike', 'a', ?, '[]', '[]',"
            " '[]', '[]', ?, ?) RETURNING item_id",
            (f"https://github.com/{repo}", json.dumps(events), run, run),
        ).fetchone()
        ids.append(f"i{item_id}")
    if not ids:
        return
    save_report(
        conn,
        run,
        ReportRow(
            "daily",
            date(2026, 10, 21),
            "passed",
            {},
            {},
            [EntryRow("daily_item", "published", tuple(ids), display_no=1)],
            body_markdown="b",
        ),
    )
    record_issue(
        conn, run, "report", "daily", date(2026, 10, 21), "created", FETCHED, repo="o/r", number=1
    )


def test_spike_and_featured_exclusion_with_history(tmp_path):
    """前日比の急上昇を判定し、すでに載せたものは AI に渡さず、観測として残す（D-13）。"""
    conn = open_db(tmp_path / "tech-researcher.sqlite3")
    previous_day(conn, hot=["hot/repo", "flat/repo"], featured=["done/repo"])
    start_run(conn, PROD, TARGET, FETCHED)
    calls = []
    repos = ("hot/repo", "flat/repo", "done/repo")
    github = FakeGitHub(
        calls,
        search_repos=repos,
        stars={"hot/repo": 1300, "flat/repo": 1010, "done/repo": 1900},
        created={r: OLD_CREATED for r in repos},
        readmes={"hot/repo": "# hot", "done/repo": "# done"},
    )
    summary = run_collect(
        all_sources_config(),
        TARGET,
        tmp_path,
        prod_log(tmp_path),
        collectors=all_collectors(calls, github=github),
        db=conn,
        write_db=True,
    )
    assert summary["unjudged_star"] is False  # 前日の測定がある
    assert summary["held_items"] == 1
    # README は載せるものだけ（載せた done/repo は取らない）
    assert [c for c in calls if c.startswith("readme:")] == [
        "readme:hot/repo",
        "readme:mentioned/repo",
    ]
    collected = {i.url: i for i in load_collected(tmp_path / COLLECTED)}
    assert "https://github.com/hot/repo" in collected
    assert "https://github.com/done/repo" not in collected
    assert "https://github.com/flat/repo" not in collected  # 増分 10 は基準に届かない
    hot = collected["https://github.com/hot/repo"]
    assert hot.github.star_delta_24h == 300
    assert [(e.type, e.basis) for e in hot.events] == [("attention_spike", "star_growth")]
    rows = conn.execute(
        "SELECT canonical_url, inclusion_event, hold_reason FROM items"
        " WHERE canonical_url LIKE 'https://github.com/%' AND target = '2026-10-23'"
        " ORDER BY canonical_url"
    ).fetchall()
    assert ("https://github.com/done/repo", "attention_spike", "already_featured") in rows
    assert ("https://github.com/hot/repo", "attention_spike", None) in rows
    stars = conn.execute(
        "SELECT stars_total FROM star_snapshots WHERE repo = 'done/repo' AND target = '2026-10-23'"
    ).fetchall()
    assert stars == [(1900,)]  # 除外しても測定は残る
    conn.close()


def test_dry_run_excludes_with_history_from_a_readonly_connection(tmp_path):
    """ドライランは読み取り専用の履歴で除外を判定し、何も書かない（D-22）。"""
    path = tmp_path / "tech-researcher.sqlite3"
    conn = open_db(path)
    previous_day(conn, hot=["hot/repo"], featured=["done/repo"])
    conn.close()
    ro = open_readonly(path)
    repos = ("hot/repo", "done/repo")
    github = FakeGitHub(
        [],
        search_repos=repos,
        stars={"hot/repo": 1300, "done/repo": 1900},
        created={r: OLD_CREATED for r in repos},
    )
    summary = run_collect(
        all_sources_config(),
        TARGET,
        tmp_path,
        prod_log(tmp_path, "dry-20261024-060000-abcd"),
        collectors=all_collectors([], github=github),
        db=ro,
        write_db=False,
    )
    ro.close()
    assert summary["held_items"] == 1 and summary["unjudged_star"] is False
    urls = {i.url for i in load_collected(tmp_path / COLLECTED)}
    assert "https://github.com/hot/repo" in urls and "https://github.com/done/repo" not in urls
    conn = open_db(path)
    assert conn.execute("SELECT count(*) FROM items WHERE target = '2026-10-23'").fetchone() == (0,)
    conn.close()


def test_unexpected_error_in_github_does_not_duplicate_other_sources(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("想定外")

    monkeypatch.setattr("tech_researcher.stages.collect.measure_stars", boom)
    config = replace(all_sources_config(), feeds=(feed("a"),))
    summary = run_collect(
        config,
        TARGET,
        tmp_path,
        prod_log(tmp_path, "dry-20261024-060000-abcd"),
        collectors=all_collectors([]),
    )
    assert "github_repos" in summary["failed_sources"]
    items = {i.url: i for i in load_collected(tmp_path / COLLECTED)}
    assert len(items["https://a.example.com/1"].sources) == 1  # 出典が二重にならない


def test_rerun_judges_only_repos_recorded_on_the_first_run(tmp_path):
    """同じ対象日の再実行で候補が増えても、判定の対象は最初の記録のままにする。"""
    conn = open_db(tmp_path / "tech-researcher.sqlite3")
    previous_day(conn, hot=["hot/repo"], featured=[])
    seen = []
    for run_id, repos in (
        (PROD, ("hot/repo",)),
        ("20261024-070000-beef", ("hot/repo", "late/new")),
    ):
        start_run(conn, run_id, TARGET, FETCHED)
        github = FakeGitHub(
            [],
            search_repos=repos,
            stars={"hot/repo": 1300, "late/new": 900},
            created={"hot/repo": OLD_CREATED, "late/new": "2026-10-20T00:00:00Z"},
        )
        run_collect(
            all_sources_config(),
            TARGET,
            tmp_path,
            prod_log(tmp_path, run_id),
            collectors=all_collectors([], github=github),
            db=conn,
            write_db=True,
        )
        seen.append(
            sorted(i.url for i in load_collected(tmp_path / COLLECTED) if "github.com" in i.url)
        )
    # 言及の経路の mentioned/repo は初回から候補。late/new だけが再実行で加わった
    assert seen[0] == seen[1]
    assert "https://github.com/hot/repo" in seen[0]
    assert "https://github.com/late/new" not in seen[0]
    # 新しい候補も測って記録する（翌日の前日比に使う）
    assert conn.execute(
        "SELECT count(*) FROM star_snapshots WHERE repo = 'late/new'"
    ).fetchone() == (1,)
    conn.close()


def run_day(conn, tmp_path, run_id, github, collectors=None):
    start_run(conn, run_id, TARGET, FETCHED)
    return run_collect(
        all_sources_config(),
        TARGET,
        tmp_path,
        prod_log(tmp_path, run_id),
        collectors=collectors or all_collectors([], github=github),
        db=conn,
        write_db=True,
    )


def github_urls(tmp_path):
    return sorted(i.url for i in load_collected(tmp_path / COLLECTED) if "github.com" in i.url)


def test_rerun_after_a_partial_first_run_judges_the_repos_measured_later(tmp_path):
    """最初の実行が測り残した日は、再実行で測れた分も判定に入れる。"""
    conn = open_db(tmp_path / "tech-researcher.sqlite3")
    previous_day(conn, hot=["hot/repo", "late/hot"], featured=[])
    stars = {"hot/repo": 1300, "late/hot": 1400}
    created = {"hot/repo": OLD_CREATED, "late/hot": OLD_CREATED}
    first = FakeGitHub([], search_repos=(), stars=stars, created=created, graphql_fail={"late/hot"})
    run_day(conn, tmp_path, PROD, first)
    assert "https://github.com/late/hot" not in github_urls(tmp_path)
    row = conn.execute(
        "SELECT status, error_category, record_count FROM source_fetches"
        " WHERE run_id = ? AND source_id = 'github_repos'",
        (PROD,),
    ).fetchone()
    assert row == ("ok", "partial", 2)  # 測れた分があれば ok。測り残しの印を残す
    second = FakeGitHub([], search_repos=(), stars=stars, created=created)
    run_day(conn, tmp_path, "20261024-070000-beef", second)
    assert "https://github.com/late/hot" in github_urls(tmp_path)  # 今回測れた分も判定する
    row = conn.execute(
        "SELECT status, error_category, record_count FROM source_fetches"
        " WHERE run_id = '20261024-070000-beef' AND source_id = 'github_repos'"
    ).fetchone()
    assert row == ("ok", None, 3)  # 完了した。次の再実行は、この回の記録だけで判定する
    conn.close()


def test_rerun_with_a_failed_search_still_judges_from_the_recorded_day(tmp_path):
    """候補の検索が失敗しても、記録から判定できれば github_repos は失敗にしない。"""
    conn = open_db(tmp_path / "tech-researcher.sqlite3")
    previous_day(conn, hot=["hot/repo"], featured=[])
    kwargs = {"stars": {"hot/repo": 1300}, "created": {"hot/repo": OLD_CREATED}}
    run_day(conn, tmp_path, PROD, FakeGitHub([], search_repos=("hot/repo",), **kwargs))
    first = github_urls(tmp_path)
    summary = run_day(
        conn,
        tmp_path,
        "20261024-070000-beef",
        FakeGitHub([], search_error=401, **kwargs),
    )
    assert github_urls(tmp_path) == first
    assert "github_repos" not in summary["failed_sources"]
    status, count = conn.execute(
        "SELECT status, record_count FROM source_fetches"
        " WHERE run_id = '20261024-070000-beef' AND source_id = 'github_repos'"
    ).fetchone()
    assert (status, count) == ("ok", 2)  # 判定に使った測定の数（hot/repo と mentioned/repo）
    conn.close()


def test_failed_rerun_judged_from_records_does_not_mark_the_day_partial(tmp_path):
    """初回は測り切り、2回目は Search が失敗。3回目に新しい候補を判定に入れない。"""
    conn = open_db(tmp_path / "tech-researcher.sqlite3")
    previous_day(conn, hot=["hot/repo"], featured=[])
    stars = {"hot/repo": 1300, "late/new": 900}
    created = {"hot/repo": OLD_CREATED, "late/new": "2026-10-20T00:00:00Z"}
    first_github = FakeGitHub([], search_repos=("hot/repo",), stars=stars, created=created)
    run_day(conn, tmp_path, PROD, first_github)
    first = github_urls(tmp_path)
    second = FakeGitHub([], search_error=401, stars=stars, created=created)
    run_day(conn, tmp_path, "20261024-070000-beef", second)
    assert conn.execute(
        "SELECT error_category FROM source_fetches WHERE run_id = '20261024-070000-beef'"
        " AND source_id = 'github_repos'"
    ).fetchone() == (None,)  # 記録だけで判定した。印は付けない
    third = FakeGitHub([], search_repos=("hot/repo", "late/new"), stars=stars, created=created)
    run_day(conn, tmp_path, "20261024-080000-cafe", third)
    assert github_urls(tmp_path) == first
    assert "https://github.com/late/new" not in first
    conn.close()


def test_partial_mark_is_written_before_the_readmes_are_fetched(tmp_path, monkeypatch):
    """README の取得中に止められても、測り残しの印が残る。"""
    conn = open_db(tmp_path / "tech-researcher.sqlite3")
    previous_day(conn, hot=["hot/repo"], featured=[])

    def stop(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("tech_researcher.stages.collect._with_readmes", stop)
    github = FakeGitHub([], search_repos=("hot/repo",), graphql_fail={"mentioned/repo"})
    with pytest.raises(KeyboardInterrupt):
        run_day(conn, tmp_path, PROD, github)
    assert conn.execute(
        "SELECT status, error_category FROM source_fetches WHERE source_id = 'github_repos'"
    ).fetchall() == [("ok", "partial")]
    conn.close()


def test_featured_repo_with_another_event_is_passed_without_the_spike(tmp_path):
    """載せ済みのリポジトリが別の出来事で渡るときは、急上昇の出来事と増分を外す。"""
    conn = open_db(tmp_path / "tech-researcher.sqlite3")
    previous_day(conn, hot=[], featured=["done/repo"])
    github = FakeGitHub(
        [],
        search_repos=("done/repo",),
        stars={"done/repo": 1900},
        created={"done/repo": OLD_CREATED},
    )
    collectors = all_collectors([], github=github)
    release = record("rss:a", "https://github.com/done/repo", IN)
    collectors = replace(
        collectors, feed=lambda f, limits, **_: SourceResult("rss:a", (release,), 1)
    )
    summary = run_day(conn, tmp_path, PROD, github, collectors)
    assert summary["held_items"] == 0
    item = {i.url: i for i in load_collected(tmp_path / COLLECTED)}["https://github.com/done/repo"]
    assert [e.type for e in item.events] == ["published"]
    assert all(e.type == "published" for r in item.sources for e in r.events)
    assert item.github.star_delta_24h is None
    assert all(a.metric != "star_delta_24h" for a in item.attention)
    conn.close()


def test_featured_does_not_exclude_the_same_target_on_rerun(tmp_path):
    conn = open_db(tmp_path / "tech-researcher.sqlite3")
    previous_day(conn, hot=[], featured=["done/repo"])
    # 同じ対象日（10-23）の日次レポートに載せたことにして、再実行で自分を除外しないことを確かめる
    conn.execute("UPDATE issues SET target = '2026-10-23'")
    conn.execute("UPDATE items SET target = '2026-10-23'")
    start_run(conn, PROD, TARGET, FETCHED)
    github = FakeGitHub(
        [],
        search_repos=("done/repo",),
        stars={"done/repo": 1900},
        created={"done/repo": OLD_CREATED},
    )
    summary = run_collect(
        all_sources_config(),
        TARGET,
        tmp_path,
        prod_log(tmp_path),
        collectors=all_collectors([], github=github),
        db=conn,
        write_db=True,
    )
    assert summary["held_items"] == 0
    conn.close()


def test_previous_day_article_is_held_only_when_judged_by_utc_date(tmp_path):
    """日付だけの記事は前日に載せたものを除き、時刻付きの記事は除かない（D-16）。"""
    conn = open_db(tmp_path / "tech-researcher.sqlite3")
    run = "20261023-060000-aaaa"
    start_run(conn, run, daily_target(date(2026, 10, 23), 1), FETCHED)
    published = [{"type": "published", "at": "2026-10-22T00:00:00Z", "basis": None}]
    ids = []
    for url in ("https://hn.example.com/approx", "https://a.example.com/1"):
        (item_id,) = conn.execute(
            "INSERT INTO items (cadence, target, canonical_url, kind, title, published_precision,"
            " inclusion_event, inclusion_at, events_json, attention_json, sources_json,"
            " links_json, meta_terms_json, first_run_id, last_run_id) VALUES ('daily',"
            " '2026-10-22', ?, 'article', 't', 'date', 'published', 'a', ?, '[]', '[]',"
            " '[]', '[]', ?, ?) RETURNING item_id",
            (url, json.dumps(published), run, run),
        ).fetchone()
        ids.append(f"i{item_id}")
    entry = EntryRow("daily_item", "published", tuple(ids), display_no=1)
    save_report(
        conn,
        run,
        ReportRow("daily", date(2026, 10, 22), "passed", {}, {}, [entry], body_markdown="b"),
    )
    record_issue(
        conn, run, "report", "daily", date(2026, 10, 22), "created", FETCHED, repo="o/r", number=1
    )
    start_run(conn, PROD, TARGET, FETCHED)
    day = datetime(2026, 10, 22, tzinfo=UTC)  # UTC の日付だけ（D-16）
    approx = replace(
        record("hackernews", "https://hn.example.com/approx", day),
        published_precision="date",
    )
    config = replace(all_sources_config(), github_repos=None, qiita=None)
    config = replace(config, feeds=(feed("a"),))
    summary = run_collect(
        config,
        TARGET,
        tmp_path,
        prod_log(tmp_path),
        collectors=all_collectors([], hn_record=approx),
        db=conn,
        write_db=True,
    )
    assert summary["held_items"] == 1
    urls = {i.url for i in load_collected(tmp_path / COLLECTED)}
    assert urls == {"https://a.example.com/1"}  # 近似の記事は除かれ、時刻付きの記事は残る
    assert conn.execute(
        "SELECT hold_reason FROM items WHERE target = '2026-10-23' AND canonical_url = ?",
        ("https://hn.example.com/approx",),
    ).fetchall() == [("shown_previous_day",)]
    conn.close()


def test_unjudged_notes_for_the_header(tmp_path):
    """前日の記録がなく判定しなかった急上昇は、unjudged.json からヘッダの定型文になる（1.6）。"""
    calls = []
    run_collect(
        all_sources_config(),
        TARGET,
        tmp_path,
        prod_log(tmp_path, "dry-20261024-060000-abcd"),
        collectors=all_collectors(calls),
    )
    assert unjudged_notes(tmp_path, all_sources_config()) == [(STAR_JUDGMENT, [])]
    assert unjudged_notes(tmp_path / "none", all_sources_config()) == []
