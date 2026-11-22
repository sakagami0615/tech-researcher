"""週次の材料と比較対象（data-model.md 3.8・4.3、D-13・D-17・D-18）。"""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tech_researcher.agent.daily import InputItem
from tech_researcher.agent.weekly import WeeklyMaterials, build_weekly_input
from tech_researcher.collect.aggregate import aggregate
from tech_researcher.collect.records import Attention, Event, GitHubRepo, Record, SourceResult
from tech_researcher.config import Feed, load_config
from tech_researcher.db.history import PastTheme
from tech_researcher.db.items import load_item_rows, upsert_items
from tech_researcher.db.readonly import open_readonly
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
from tech_researcher.stages.weekly_items import load_daily_rows, weekly_rows
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


def test_weekly_input_uses_the_same_representative_and_inclusion_as_items():
    """出典の並びや出来事の早さによらず、items の代表の値・掲載の根拠と同じものを渡す。"""
    day = date(2026, 10, 21)
    spike = Event("attention_spike", noon(day - timedelta(days=1)), "feed_entry")
    early_hn = replace(hn(day - timedelta(days=1), 10, noon(day)), events=(spike,), title="HN")
    item = item_of(early_hn, rss(X, day, "公開日時のある記事"))
    assert item.sources[0].source_id == "hackernews"
    data = build_weekly_input(WEEK, [InputItem("i1", item)], {"rss:a": "A", "hackernews": "HN"})
    entry = data["items"][0]
    assert entry["untrusted"] == {
        "title": "公開日時のある記事",
        "excerpt": "公開日時のある記事の抜粋",
    }
    assert (entry["event"]["type"], entry["date"]) == ("published", "2026-10-21")
