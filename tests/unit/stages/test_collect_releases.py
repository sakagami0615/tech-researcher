"""GitHub Releases の接続、取れなかった部分の記録と表示、項目の ID（#40）。"""

import json
from dataclasses import replace
from datetime import date

from tech_researcher.collect.records import Event, FailedPart, Record, SourceResult
from tech_researcher.config import GitHubReleases
from tech_researcher.db.items import lookup_item_refs
from tech_researcher.db.readonly import open_readonly
from tech_researcher.db.runs import start_run
from tech_researcher.db.schema import open_db
from tech_researcher.periods import weekly_target
from tech_researcher.stages.collect import (
    COLLECTED,
    ITEM_IDS,
    UNAVAILABLE,
    load_collected,
    load_item_ids,
    run_collect,
    unavailable_notes,
)
from tests.unit.stages.test_collect import (
    CONFIG,
    FETCHED,
    FULL,
    IN,
    PROD,
    TARGET,
    FakeGitHub,
    all_collectors,
    feed,
    previous_day,
    prod_log,
)

RELEASES = GitHubReleases(True, "GitHub Releases", ("a/one", "b/two"))
V1 = "https://github.com/a/one/releases/tag/v1"
V2 = "https://github.com/a/one/releases/tag/v2"


def release_record(url=V1):
    return Record(
        source_id="github_releases",
        url=url,
        title="v1",
        published_precision="datetime",
        fetched_at=FETCHED,
        events=(Event("released", IN),),
        published_at=IN,
    )


def config():
    return replace(CONFIG, feeds=(feed("a"),), github_releases=RELEASES)


def collectors(result):
    def collect(settings, period, client, **_):
        assert settings is RELEASES and period == TARGET.period
        if isinstance(result, Exception):
            raise result
        return result

    return replace(all_collectors([]), releases=collect)


def run(tmp_path, result, conn=None, run_id=PROD):
    if conn is not None:
        start_run(conn, run_id, TARGET, FETCHED)
    return run_collect(
        config(),
        TARGET,
        tmp_path,
        prod_log(tmp_path, run_id),
        collectors=collectors(result),
        db=conn,
        write_db=True if conn is not None else None,
    )


def fetch_row(conn):
    return conn.execute(
        "SELECT status, error_category, attempts, record_count, in_period_count,"
        " failed_parts_json FROM source_fetches WHERE source_id = 'github_releases'"
    ).fetchone()


def test_releases_are_collected_and_saved_as_a_source(tmp_path):
    conn = open_db(tmp_path / "x.sqlite3")
    summary = run(tmp_path, SourceResult("github_releases", (release_record(),), 2), conn)

    assert summary["sources"] == 2 and summary["failed_sources"] == []
    assert V1 in [i.url for i in load_collected(tmp_path / COLLECTED)]
    assert fetch_row(conn) == ("ok", None, 2, 1, 1, None)
    kind = conn.execute("SELECT kind FROM items WHERE canonical_url = ?", (V1,)).fetchone()
    assert kind == ("release",)
    assert json.loads((tmp_path / UNAVAILABLE).read_text()) == []
    conn.close()


def test_failed_parts_are_recorded_and_shown_by_part(tmp_path):
    """一部が取れなくても情報源は失敗にしない。内訳を DB と末尾の表示に残す。"""
    conn = open_db(tmp_path / "x.sqlite3")
    result = SourceResult(
        "github_releases",
        (release_record(),),
        3,
        failed_parts=(FailedPart("b/two", "http_5xx"), FailedPart("a/one", "truncated")),
    )
    summary = run(tmp_path, result, conn)

    assert summary["failed_sources"] == []  # 全失敗の判定（collect_failure）を変えない
    status, _, _, _, _, parts = fetch_row(conn)
    assert status == "ok"
    assert json.loads(parts) == [["b/two", "http_5xx"], ["a/one", "truncated"]]
    assert unavailable_notes(tmp_path, config()) == [
        ("GitHub Releases（b/two）", "HTTP 5xx"),
        ("GitHub Releases（a/one）", "取りこぼしたおそれ"),
    ]
    conn.close()


def test_all_parts_failed_shows_only_the_breakdown(tmp_path):
    conn = open_db(tmp_path / "x.sqlite3")
    result = SourceResult(
        "github_releases",
        (),
        2,
        "すべての追跡先が取れない（timeout）",
        (FailedPart("a/one", "timeout"), FailedPart("b/two", "timeout")),
        "timeout",
    )
    summary = run(tmp_path, result, conn)

    assert summary["failed_sources"] == ["github_releases"]
    assert fetch_row(conn)[:2] == ("failed", "timeout")
    assert unavailable_notes(tmp_path, config()) == [
        ("GitHub Releases（a/one）", "タイムアウト"),
        ("GitHub Releases（b/two）", "タイムアウト"),
    ]
    conn.close()


def test_failed_source_without_breakdown_is_shown_with_a_generic_reason(tmp_path):
    """失敗は status で判定する。一覧にない分類は名前を出さず「取得に失敗」にする。"""
    cfg = replace(CONFIG, feeds=(feed("a"),))
    broken = replace(
        all_collectors([]),
        feed=lambda f, limits, **_: SourceResult("rss:a", (), 3, "x", error_category="KeyError"),
    )
    run_collect(cfg, TARGET, tmp_path, prod_log(tmp_path), collectors=broken)
    assert unavailable_notes(tmp_path, cfg) == [("a", "取得に失敗")]


def test_unexpected_error_in_releases_fails_only_that_source(tmp_path):
    summary = run(tmp_path, RuntimeError("想定外"))
    assert summary["failed_sources"] == ["github_releases"]
    assert summary["sources"] == 2


def test_featured_repo_release_is_passed_without_spike_numbers(tmp_path):
    """載せ済みのリポジトリのリリースは通常どおり渡し、急上昇の出来事・数値は持たせない。"""
    conn = open_db(tmp_path / "x.sqlite3")
    previous_day(conn, hot=[], featured=["a/one"])
    start_run(conn, PROD, TARGET, FETCHED)
    github = FakeGitHub([], search_repos=("a/one",), stars={"a/one": 1900})
    cfg = replace(
        config(), github_repos=replace(FULL.github_repos, search_topics=(), watch_repos=())
    )
    summary = run_collect(
        cfg,
        TARGET,
        tmp_path,
        prod_log(tmp_path),
        collectors=replace(
            all_collectors([], github=github),
            releases=lambda s, p, c, **_: SourceResult("github_releases", (release_record(),), 1),
        ),
        db=conn,
        write_db=True,
    )
    items = {i.url: i for i in load_collected(tmp_path / COLLECTED)}
    assert "https://github.com/a/one" not in items  # 載せ済みの急上昇は渡さない
    release = items[V1]
    assert [e.type for e in release.events] == ["released"]
    assert release.github is None
    assert all(a.metric != "star_delta_24h" for a in release.attention)
    assert summary["held_items"] == 1
    conn.close()


def test_production_item_ids_come_from_items_and_dry_run_reuses_them(tmp_path):
    path = tmp_path / "x.sqlite3"
    conn = open_db(path)
    run(tmp_path, SourceResult("github_releases", (release_record(),), 1), conn)
    rows = dict(conn.execute("SELECT canonical_url, item_id FROM items").fetchall())
    expected = [f"i{rows[i.url]}" for i in load_collected(tmp_path / COLLECTED)]
    assert load_item_ids(tmp_path) == expected
    conn.close()

    # ドライラン：DB にある項目は同じ ID、新しい項目は仮の ID
    dry = tmp_path / "dry"
    dry.mkdir()
    ro = open_readonly(path)
    run_collect(
        config(),
        TARGET,
        dry,
        prod_log(tmp_path, "dry-20261024-060000-abcd"),
        collectors=collectors(
            SourceResult("github_releases", (release_record(), release_record(V2)), 1)
        ),
        db=ro,
        write_db=False,
    )
    urls = [i.url for i in load_collected(dry / COLLECTED)]
    by_url = dict(zip(urls, load_item_ids(dry), strict=True))
    assert by_url[V1] == f"i{rows[V1]}"
    assert by_url[V2] == "d1"
    assert lookup_item_refs(ro, "daily", TARGET.key, ["https://nothing"]) == {}
    ro.close()


def test_weekly_writes_item_ids_but_not_daily_only_files(tmp_path):
    weekly = weekly_target(date(2026, 10, 26), 7)
    run_collect(
        replace(CONFIG, feeds=(feed("a"),)),
        weekly,
        tmp_path,
        prod_log(tmp_path, target=weekly),
        collectors=all_collectors([]),
    )
    assert (tmp_path / ITEM_IDS).exists()
    assert not (tmp_path / UNAVAILABLE).exists()


def test_one_github_client_is_shared_so_the_rest_limit_counts_everything(tmp_path):
    """候補集め・README・Releases で、REST の回数を数えるクライアントを1つにする。"""
    made, used = [], []
    github = FakeGitHub([], search_repos=())

    def make(token, limits):
        client = github.client(token, limits)
        made.append(client)
        return client

    cfg = replace(
        config(), github_repos=replace(FULL.github_repos, search_topics=(), watch_repos=())
    )
    collected = replace(
        all_collectors([], github=github),
        github=make,
        releases=lambda s, p, c, **_: used.append(c) or SourceResult("github_releases", (), 1),
    )
    run_collect(cfg, TARGET, tmp_path, prod_log(tmp_path), collectors=collected)
    assert len(made) == 1 and used == made


def test_classified_failures_of_other_sources_are_shown_with_their_reason(tmp_path):
    cfg = replace(CONFIG, feeds=(feed("a"),))
    broken = replace(
        all_collectors([]),
        feed=lambda f, limits, **_: SourceResult("rss:a", (), 3, "x", error_category="http_5xx"),
    )
    run_collect(cfg, TARGET, tmp_path, prod_log(tmp_path), collectors=broken)
    assert unavailable_notes(tmp_path, cfg) == [("a", "HTTP 5xx")]
