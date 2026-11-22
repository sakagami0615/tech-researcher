"""週次の材料と比較対象（data-model.md 3.8・4.3、D-13・D-17・D-18）。"""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tech_researcher.agent.daily import InputItem
from tech_researcher.agent.weekly import WeeklyInclusion, WeeklyMaterials, build_weekly_input
from tech_researcher.collect.aggregate import aggregate
from tech_researcher.collect.records import Attention, Event, GitHubRepo, Record, SourceResult
from tech_researcher.config import Feed, load_config
from tech_researcher.db.history import PastTheme
from tech_researcher.db.issues import record_issue
from tech_researcher.db.items import load_item_rows, upsert_items
from tech_researcher.db.readonly import open_readonly
from tech_researcher.db.reports import EntryRow, ReportRow, save_report
from tech_researcher.db.runs import start_run
from tech_researcher.db.schema import open_db
from tech_researcher.db.weekly import (
    compare_week_starts,
    past_observations,
    period_star_gains,
    weeks_with_items,
)
from tech_researcher.logging import RunLog
from tech_researcher.periods import daily_target, weekly_target
from tech_researcher.stages.collect import COLLECTED, ITEM_IDS, Collectors, run_collect
from tech_researcher.stages.collect_store import item_row
from tech_researcher.stages.weekly import write_weekly_input
from tech_researcher.stages.weekly_items import load_daily_rows, weekly_rows, weekly_rows_dated
from tech_researcher.validate.secret_scan import SecretScanner

CONFIG = replace(
    load_config(Path(__file__).parents[2] / "config" / "tech-researcher.toml"),
    feeds=(Feed("a", "A", "https://a.example.com/rss", "new", True),),
    qiita=None,
    hackernews=None,
    github_search=None,
    github_repos=None,
    github_releases=None,
)
WEEK = weekly_target(date(2026, 10, 26), 7)  # 10-19 0:00〜10-26 0:00（JST）
STARTED = datetime(2026, 10, 25, 22, 0, tzinfo=UTC)
X = "https://x.example.com/post"
Y = "https://y.example.com/post"
HN = "https://news.ycombinator.com/item?id=1"


def noon(day: date) -> datetime:
    """その日の JST 12:00。"""
    return datetime(day.year, day.month, day.day, 3, 0, tzinfo=UTC)


def rss(url, day, title, source_id="rss:a"):
    published = noon(day)
    return Record(
        source_id=source_id,
        url=url,
        title=title,
        published_precision="datetime",
        fetched_at=STARTED,
        events=(Event("published", published),),
        excerpt=f"{title}の抜粋",
        published_at=published,
    )


def hn(day, points, measured):
    posted = noon(day)
    return Record(
        source_id="hackernews",
        url=HN,
        link_url=X,
        title="HN のタイトル",
        published_precision="unknown",
        fetched_at=measured,
        events=(Event("posted", posted),),
        posted_at=posted,
        attention=(Attention("hn_points", points, measured),),
    )


def item_of(*records):
    (item,) = aggregate(records)
    return item


def published(day):
    """項目の掲載の根拠を、公開の出来事（JST の正午）にした入力。"""
    return WeeklyInclusion(Event("published", noon(day)), day)


@pytest.fixture
def conn(tmp_path):
    conn = open_db(tmp_path / "tech-researcher.sqlite3")
    yield conn
    conn.close()


def store_daily(conn, day, item, hold=None):
    target = daily_target(day + timedelta(days=1), 1)
    run_id = f"2026{day:%m%d}-060000-d{item.url[8]}"
    start_run(conn, run_id, target, STARTED)
    return upsert_items(conn, run_id, [item_row(item, target, hold)])[0]


def store_weekly(conn, monday, url, title):
    target = weekly_target(monday, 7)
    run_id = f"2026{monday:%m%d}-070000-w{url[8]}"
    start_run(conn, run_id, target, STARTED)
    item = item_of(rss(url, monday - timedelta(days=5), title))
    return upsert_items(conn, run_id, [item_row(item, target)])[0]


def test_loaded_daily_row_keeps_what_the_weekly_material_needs(conn):
    item = item_of(
        rss(X, date(2026, 10, 20), "日次のタイトル"), hn(date(2026, 10, 20), 100, STARTED)
    )
    store_daily(conn, date(2026, 10, 20), item, "shown_previous_day")
    (row,) = load_item_rows(conn, "daily", date(2026, 10, 19), date(2026, 10, 26))
    assert (row.cadence, row.target, row.kind, row.hold_reason) == (
        "daily",
        date(2026, 10, 20),
        "article",
        "shown_previous_day",
    )
    assert (row.title, row.excerpt, row.published_precision) == (
        "日次のタイトル",
        "日次のタイトルの抜粋",
        "datetime",
    )
    assert [s.source_id for s in row.item.sources] == ["rss:a", "hackernews"]
    assert row.item.events == item.events
    assert row.item.attention == item.attention
    assert row.inclusion == item.events[0]
    assert load_item_rows(conn, "daily", date(2026, 10, 21), date(2026, 10, 26)) == []


def test_weekly_rows_merge_fresh_and_daily_by_url_without_daily_holds(conn):
    day20, day22 = date(2026, 10, 20), date(2026, 10, 22)
    store_daily(conn, day20, item_of(rss(X, day20, "日次の X"), hn(day20, 100, noon(day20))))
    store_daily(conn, day22, item_of(rss(Y, day22, "日次の Y")), "already_featured")
    # 範囲の外（前の週の日曜と、対象週の月曜）の日次は使わない
    store_daily(
        conn,
        date(2026, 10, 18),
        item_of(rss("https://old.example.com/a", date(2026, 10, 18), "旧")),
    )
    store_daily(
        conn,
        date(2026, 10, 26),
        item_of(rss("https://new.example.com/a", date(2026, 10, 26), "新")),
    )

    daily = load_daily_rows(conn, WEEK)
    assert [r.item.url for r in daily] == [X, Y]  # 範囲の外は引かない
    fresh = [item_of(rss(X, day20, "取り直した X"), hn(day20, 250, noon(date(2026, 10, 24))))]
    rows = weekly_rows(WEEK, fresh, daily)

    assert [r.item.url for r in rows] == [X, Y]
    x, y = rows
    assert (x.cadence, x.target, y.cadence, y.target) == ("weekly", WEEK.key, "weekly", WEEK.key)
    assert x.title == "取り直した X"
    assert [(s.source_id, s.url) for s in x.item.sources] == [("rss:a", X), ("hackernews", HN)]
    assert x.item.attention == (Attention("hn_points", 250, noon(date(2026, 10, 24))),)
    assert [e.type for e in x.item.events] == ["published", "posted"]
    assert x.inclusion.type == "published"
    assert y.hold_reason is None  # 日次の掲載除外は持ち込まない。観測としては入る
    assert (y.title, y.kind) == ("日次の Y", "article")


def test_daily_only_row_becomes_weekly_row_with_the_same_event(conn):
    day = date(2026, 10, 22)
    item = item_of(rss(Y, day, "日次の Y"))
    store_daily(conn, day, item)
    (row,) = weekly_rows(WEEK, [], load_daily_rows(conn, WEEK))
    assert row.item.events == item.events and row.inclusion == item.events[0]
    assert row.published_at == noon(day)


def collectors(records):
    return Collectors(feed=lambda f, limits, **_: SourceResult("rss:a", tuple(records), 1))


def collect(tmp_path, name, conn, run_id, records, write=True):
    run_dir = tmp_path / name
    run_dir.mkdir()
    if write:
        start_run(conn, run_id, WEEK, STARTED)
    log = RunLog(run_dir / "log.jsonl", run_id, "weekly", WEEK.key, SecretScanner())
    summary = run_collect(
        CONFIG, WEEK, run_dir, log, collectors=collectors(records), db=conn, write_db=write
    )
    return summary, run_dir


def weekly_count(conn):
    return conn.execute("SELECT count(*) FROM items WHERE cadence = 'weekly'").fetchone()[0]


def test_weekly_collect_saves_merged_items_idempotently_and_writes_no_snapshots(tmp_path, conn):
    day20, day22 = date(2026, 10, 20), date(2026, 10, 22)
    store_daily(conn, day20, item_of(rss(X, day20, "日次の X")))
    store_daily(conn, day22, item_of(rss(Y, day22, "日次の Y")), "already_featured")
    fresh = [rss(X, day20, "取り直した X"), rss("https://f.example.com/a", date(2026, 10, 21), "F")]

    summary, run_dir = collect(tmp_path, "one", conn, "20261026-070000-abcd", fresh)

    urls = [X, "https://f.example.com/a", Y]
    assert summary["items"] == 3
    collected = json.loads((run_dir / COLLECTED).read_text("utf-8"))
    assert [i["url"] for i in collected] == urls
    ids = json.loads((run_dir / ITEM_IDS).read_text("utf-8"))
    saved = dict(
        conn.execute("SELECT canonical_url, 'i' || item_id FROM items WHERE cadence='weekly'")
    )
    assert ids == [saved[u] for u in urls]
    assert conn.execute(
        "SELECT DISTINCT target, hold_reason FROM items WHERE cadence='weekly'"
    ).fetchall() == [("2026-10-26", None)]
    for table in ("star_snapshots", "feed_snapshots"):  # 週次はスナップショットを書かない
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)

    # 再実行：行も ID も増えない
    _, rerun = collect(tmp_path, "two", conn, "20261026-080000-abcd", fresh)
    assert json.loads((rerun / ITEM_IDS).read_text("utf-8")) == ids
    assert weekly_count(conn) == 3

    # ドライラン：書かない。DB にある項目は本番の ID、新しい項目は仮の ID
    ro = open_readonly(tmp_path / "tech-researcher.sqlite3")
    extra = [*fresh, rss("https://g.example.com/a", date(2026, 10, 23), "G")]
    _, dry = collect(tmp_path, "dry", ro, "dry-20261026-070000-abcd", extra, write=False)
    ro.close()
    dry_ids = json.loads((dry / ITEM_IDS).read_text("utf-8"))
    assert dry_ids == [ids[0], ids[1], "d1", ids[2]]
    assert weekly_count(conn) == 3


def test_compare_weeks_drop_weeks_without_weekly_items(conn):
    assert compare_week_starts(WEEK.key, 4) == [
        date(2026, 10, 19),
        date(2026, 10, 12),
        date(2026, 10, 5),
        date(2026, 9, 28),
    ]
    store_weekly(conn, date(2026, 10, 19), X, "先週の X")
    store_weekly(conn, date(2026, 10, 5), Y, "三週前の Y")
    store_weekly(conn, WEEK.key, "https://z.example.com/a", "今週の Z")  # 今週は窓に入らない
    weeks = weeks_with_items(conn, compare_week_starts(WEEK.key, 4))
    assert weeks == [date(2026, 10, 19), date(2026, 10, 5)]
    observations = past_observations(conn, weeks)
    assert [(o.week, o.source_id, o.kind, o.title) for o in observations] == [
        (date(2026, 10, 19), "rss:a", "article", "先週の X"),
        (date(2026, 10, 5), "rss:a", "article", "三週前の Y"),
    ]
    assert all(o.item_id.startswith("i") for o in observations)
    assert past_observations(conn, []) == []


def test_star_gains_sum_daily_increments_and_skip_days_without_the_day_before(conn):
    def snapshot(repo, day, stars):
        conn.execute(
            "INSERT INTO star_snapshots (repo, target, measured_at, stars_total, created_at,"
            " routes_json) VALUES (?, ?, ?, ?, '2026-01-01T00:00:00Z', '[\"search\"]')",
            (repo, day.isoformat(), f"{day.isoformat()}T21:00:00Z", stars),
        )

    for day, stars in [(18, 1000), (19, 1100), (20, 1150), (22, 1400), (23, 1500), (26, 9999)]:
        snapshot("a/repo", date(2026, 10, day), stars)
    snapshot("b/new", date(2026, 10, 21), 50)  # 前日の記録がない日は記録だけ
    gains = period_star_gains(conn, date(2026, 10, 19), date(2026, 10, 26))
    # 19: +100、20: +50、22 は前日（21）がない、23: +100。26 は期間の外
    assert gains == {"a/repo": 250}


def test_build_weekly_input_shape_and_untrusted_text():
    day = date(2026, 10, 21)
    repo = GitHubRepo("o/repo", noon(day), 5000, 40, (X,))
    page = replace(
        rss("https://github.com/o/repo", day, "リポジトリ"),
        github=repo,
        attention=(
            Attention("star_delta_24h", 40, noon(day)),
            Attention("hn_points", 9, noon(day)),
        ),
    )
    inputs = [
        InputItem("i1", item_of(rss(X, day, "記事"))),
        InputItem("i2", item_of(page)),
    ]
    past = [PastTheme("T0007", "テーマ", ((date(2026, 10, 19), "rising", 6),))]
    data = build_weekly_input(
        WEEK,
        inputs,
        [published(day), published(day)],
        {"rss:a": "A"},
        WeeklyMaterials([date(2026, 10, 19)], [], past, {"o/repo": 321}),
    )
    assert data["task"] == "weekly"
    assert data["target"] == {
        "week": "2026-10-26",
        "period": {"start": "2026-10-19T00:00:00+09:00", "end": "2026-10-26T00:00:00+09:00"},
        "compare_weeks": ["2026-10-19"],
    }
    first, second = data["items"]
    assert first == {
        "id": "i1",
        "date": "2026-10-21",
        "source": "A",
        "kind": "article",
        "event": {"type": "published"},
        "attention": [],
        "github": None,
        "untrusted": {"title": "記事", "excerpt": "記事の抜粋"},
    }
    assert second["kind"] == "repo"
    assert second["attention"] == [{"metric": "hn_points", "value": 9}]  # 日次の増分は入れない
    assert second["github"]["star_delta_period"] == 321
    assert second["github"]["mentioned_by"] == ["i1"]
    assert data["past_themes"] == [
        {
            "theme_id": "T0007",
            "name": "テーマ",
            "history": [{"week": "2026-10-19", "status": "rising", "count": 6}],
        }
    ]
    assert "primary_text" not in json.dumps(data)


def test_write_weekly_input_uses_materials_and_compare_weeks(tmp_path, conn):
    day = date(2026, 10, 20)
    store_daily(conn, day, item_of(rss(X, day, "日次の X")))
    store_weekly(conn, date(2026, 10, 19), Y, "先週の Y")
    store_weekly(conn, date(2026, 10, 5), "https://w.example.com/a", "三週前の W")
    _, run_dir = collect(tmp_path, "one", conn, "20261026-070000-abcd", [])

    write_weekly_input(CONFIG, WEEK, run_dir, conn)

    data = json.loads((run_dir / "agent-input.json").read_text("utf-8"))
    ids = json.loads((run_dir / ITEM_IDS).read_text("utf-8"))
    assert [i["id"] for i in data["items"]] == ids
    assert data["items"][0]["untrusted"]["title"] == "日次の X"
    assert data["target"]["compare_weeks"] == ["2026-10-19", "2026-10-05"]  # 欠測の週は外す
    assert [(p["week"], p["untrusted"]["title"]) for p in data["past_observations"]] == [
        ("2026-10-19", "先週の Y"),
        ("2026-10-05", "三週前の W"),
    ]
    assert data["past_themes"] == []

    # DB がなければ（開発機のドライラン）比較対象は空
    write_weekly_input(CONFIG, WEEK, run_dir, None)
    data = json.loads((run_dir / "agent-input.json").read_text("utf-8"))
    assert (data["past_observations"], data["target"]["compare_weeks"]) == ([], [])


def test_weekly_input_uses_the_same_representative_as_items_and_the_given_inclusion():
    """出典の並びによらず、items の代表の値を渡す。出来事と日付は、渡された根拠をそのまま使う。"""
    day = date(2026, 10, 21)
    spike = Event("attention_spike", noon(day - timedelta(days=1)), "feed_entry")
    early_hn = replace(hn(day - timedelta(days=1), 10, noon(day)), events=(spike,), title="HN")
    item = item_of(early_hn, rss(X, day, "公開日時のある記事"))
    assert item.sources[0].source_id == "hackernews"
    data = build_weekly_input(
        WEEK, [InputItem("i1", item)], [published(day)], {"rss:a": "A", "hackernews": "HN"}
    )
    entry = data["items"][0]
    assert entry["untrusted"] == {
        "title": "公開日時のある記事",
        "excerpt": "公開日時のある記事の抜粋",
    }
    assert (entry["event"]["type"], entry["date"]) == ("published", "2026-10-21")


def test_merged_repo_uses_the_newest_day_and_all_mentions(conn):
    def repo_item(day, stars, mention):
        repo = GitHubRepo("o/repo", noon(day), stars, None, (mention,))
        return item_of(replace(rss("https://github.com/o/repo", day, "repo"), github=repo))

    # 公開日時の精度は同じ。新しい日次の値（Star の累計）を使い、言及元は両方残す
    store_daily(conn, date(2026, 10, 20), repo_item(date(2026, 10, 20), 100, X))
    store_daily(conn, date(2026, 10, 24), repo_item(date(2026, 10, 24), 900, Y))
    (row,) = weekly_rows(WEEK, [], load_daily_rows(conn, WEEK))
    assert row.item.github.stars_total == 900
    assert row.item.github.mentioned_by == (Y, X)


def test_old_article_reposted_stays_a_spike_after_restoring_the_daily_row(conn):
    """古い記事（時刻ありの公開）への HN の日付だけの公開・基準超えを、週次の公開にしない。"""
    day = date(2026, 10, 21)
    old = rss(X, date(2026, 9, 1), "古い記事")
    spike = Event("attention_spike", noon(day), "threshold")
    date_only = replace(
        hn(day, 300, noon(day)),
        published_precision="date",
        published_at=datetime(2026, 10, 21, tzinfo=UTC),
        events=(Event("published", datetime(2026, 10, 21, tzinfo=UTC)), spike),
    )
    item = item_of(old, date_only)
    store_daily(conn, day, item)
    ((row, row_day),) = weekly_rows_dated(WEEK, [], load_daily_rows(conn, WEEK))
    assert (row.inclusion.type, row_day) == ("attention_spike", day)
    names = {"rss:a": "A", "hackernews": "HN"}
    inclusion = WeeklyInclusion(row.inclusion, row_day)
    data = build_weekly_input(WEEK, [InputItem("i1", row.item)], [inclusion], names)
    assert data["items"][0]["event"] == {"type": "attention_spike"}


def strip_to_legacy(conn):
    """出典ごとの公開日時・注目度を保存する前の形式の行にする。"""
    rows = conn.execute("SELECT item_id, sources_json FROM items").fetchall()
    for item_id, text in rows:
        old = [
            {
                k: v
                for k, v in s.items()
                if k not in ("published_at", "published_precision", "attention")
            }
            for s in json.loads(text)
        ]
        conn.execute(
            "UPDATE items SET sources_json = ? WHERE item_id = ?", (json.dumps(old), item_id)
        )


def test_legacy_row_keeps_its_stored_inclusion(conn):
    """出典ごとの公開日時がない古い行は、期間の判定をやり直さず、保存した根拠を使う。"""
    day = date(2026, 10, 21)
    old = rss(X, date(2026, 9, 1), "古い記事")
    midnight = datetime(2026, 10, 21, tzinfo=UTC)
    spike = Event("attention_spike", noon(day), "threshold")
    date_only = replace(
        hn(day, 300, noon(day)),
        published_precision="date",
        published_at=midnight,
        events=(Event("published", midnight), spike),
    )
    store_daily(conn, day, item_of(old, date_only))
    strip_to_legacy(conn)
    (loaded,) = load_item_rows(conn, "daily", day, day + timedelta(days=1))
    assert loaded.dated_sources is False
    ((row, _),) = weekly_rows_dated(WEEK, [], [loaded])
    assert row.inclusion.type == "attention_spike"  # 日付だけの公開を、時刻ありの公開にしない


def test_inclusion_day_of_feed_entry_is_the_daily_target_not_the_measured_time(conn):
    day = date(2026, 10, 22)
    seen = Event("attention_spike", noon(day + timedelta(days=1)), "feed_entry")
    record = replace(rss(X, date(2026, 9, 1), "古い記事"), events=(seen,))
    store_daily(conn, day, item_of(record))
    ((row, row_day),) = weekly_rows_dated(WEEK, [], load_daily_rows(conn, WEEK))
    assert row.inclusion.type == "attention_spike"
    assert row_day == day  # 時刻（翌日）ではなく、日次の行の対象日


def test_attention_is_latest_per_source_and_metric(conn):
    day = date(2026, 10, 20)
    first = replace(rss(X, day, "X"), attention=(Attention("feed_rank", 3, noon(day)),))
    second = replace(hn(day, 100, noon(day)), attention=(Attention("feed_rank", 7, noon(day)),))
    store_daily(conn, day, item_of(first, second))
    later = replace(first, attention=(Attention("feed_rank", 1, noon(date(2026, 10, 23))),))
    (row,) = weekly_rows(WEEK, [item_of(later)], load_daily_rows(conn, WEEK))
    # 同じ指標でも出典が違えば両方残し、出典ごとに新しい値だけを使う
    assert sorted(a.value for a in row.item.attention) == [1, 7]


def test_events_differing_only_in_microseconds_are_one_event(conn):
    day = date(2026, 10, 20)
    store_daily(conn, day, item_of(rss(X, day, "X")))
    fresh = rss(X, day, "X")
    shifted = fresh.published_at.replace(microsecond=500)
    fresh = replace(fresh, published_at=shifted, events=(Event("published", shifted),))
    (row,) = weekly_rows(WEEK, [item_of(fresh)], load_daily_rows(conn, WEEK))
    assert [e.type for e in row.item.events] == ["published"]


def test_representative_comes_from_the_row_with_the_most_precise_published_time(conn):
    day = date(2026, 10, 20)
    store_daily(conn, day, item_of(rss(X, day, "公開日時のある行")))
    only_posted = replace(hn(day, 50, noon(day)), link_url=X, title="HN だけの行")
    (row,) = weekly_rows(WEEK, [item_of(only_posted)], load_daily_rows(conn, WEEK))
    assert (row.title, row.published_precision, row.published_at) == (
        "公開日時のある行",
        "datetime",
        noon(day),
    )
    assert row.excerpt == "公開日時のある行の抜粋"


def test_write_weekly_input_with_themes_star_gains_and_readonly_db(tmp_path, conn):
    day = date(2026, 10, 20)
    repo = GitHubRepo("o/repo", noon(day), 1150, 50, ())
    page = replace(rss("https://github.com/o/repo", day, "リポジトリ"), github=repo)
    store_daily(conn, day, item_of(page))
    ref = store_weekly(conn, date(2026, 10, 19), Y, "先週の Y")
    # 先週の週次レポートの公開済みのテーマ
    run_id = "20261019-070000-wy"
    payload = {"name": "先週のテーマ", "status": "rising"}
    entry = EntryRow("theme", "published", (ref,), theme_id="T0007", payload=payload)
    report = ReportRow("weekly", date(2026, 10, 19), "passed", {}, {}, [entry], body_markdown="b")
    save_report(conn, run_id, report)
    record_issue(
        conn, run_id, "report", "weekly", date(2026, 10, 19), "created", STARTED,
        repo="o/r", number=2,
    )  # fmt: skip
    for target_day, stars in [(18, 1000), (19, 1100), (20, 1150), (25, 2000), (26, 5000)]:
        conn.execute(
            "INSERT INTO star_snapshots (repo, target, measured_at, stars_total, created_at,"
            " routes_json) VALUES ('o/repo', ?, ?, ?, '2026-01-01T00:00:00Z', '[\"search\"]')",
            (f"2026-10-{target_day}", f"2026-10-{target_day}T21:00:00Z", stars),
        )
    conn.commit()
    _, run_dir = collect(tmp_path, "one", conn, "20261026-070000-abcd", [])
    conn.commit()
    ro = open_readonly(tmp_path / "tech-researcher.sqlite3")
    log = RunLog(
        run_dir / "log2.jsonl", "20261026-070000-abcd", "weekly", WEEK.key, SecretScanner()
    )

    write_weekly_input(CONFIG, WEEK, run_dir, ro, log)
    ro.close()

    data = json.loads((run_dir / "agent-input.json").read_text("utf-8"))
    (item,) = data["items"]
    # 調査期間（10-19〜10-25）の日次の増分：19 日 +100、20 日 +50（25 日は前日の測定がなく、
    # 26 日は期間の外）
    assert item["github"]["star_delta_period"] == 150
    assert data["past_themes"] == [
        {
            "theme_id": "T0007",
            "name": "先週のテーマ",
            "history": [{"week": "2026-10-19", "status": "rising", "count": 1}],
        }
    ]
    line = json.loads((run_dir / "log2.jsonl").read_text("utf-8").splitlines()[-1])
    assert (line["event"], line["count"]) == ("input", 1)
    assert "past_observations=1" in line["detail"] and "bytes=" in line["detail"]


def test_attention_of_different_posts_of_one_source_are_both_kept(conn):
    day = date(2026, 10, 20)
    thread_a = hn(day, 300, noon(day))
    thread_b = replace(
        hn(day, 50, noon(day)),
        url="https://news.ycombinator.com/item?id=2",
        attention=(Attention("hn_points", 50, noon(day)),),
    )
    store_daily(conn, day, item_of(rss(X, day, "X"), thread_a, thread_b))
    (row,) = weekly_rows(WEEK, [], load_daily_rows(conn, WEEK))
    assert sorted(a.value for a in row.item.attention) == [50, 300]


def test_date_only_published_uses_the_daily_target_as_date(conn):
    """日付だけの公開（0:00 UTC）は、JST の日付が期間より前にならないよう、日次の対象日にする。"""
    day = date(2026, 10, 19)
    midnight = datetime(2026, 10, 18, tzinfo=UTC)  # UTC の日付は窓の中。JST の日付は期間の前日
    record = replace(
        rss(X, day, "日付だけ"),
        published_precision="date",
        published_at=midnight,
        events=(Event("published", midnight),),
    )
    store_daily(conn, day, item_of(record))
    ((row, row_day),) = weekly_rows_dated(WEEK, [], load_daily_rows(conn, WEEK))
    assert (row.inclusion.type, row_day) == ("published", day)
