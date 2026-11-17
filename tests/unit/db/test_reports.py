import json
import sqlite3
from datetime import UTC, date, datetime

import pytest

from tech_researcher.db import history
from tech_researcher.db.history import featured_urls, next_theme_id, past_themes, shown_urls
from tech_researcher.db.issues import record_issue
from tech_researcher.db.readonly import open_readonly
from tech_researcher.db.reports import EntryRow, ReportRow, save_report
from tech_researcher.db.runs import start_run
from tech_researcher.db.schema import open_db
from tech_researcher.periods import daily_target, weekly_target

DAY = date(2026, 10, 23)
RUN = "20261024-060000-abcd"
RETRY = "20261024-080000-0123"
AT = datetime(2026, 10, 23, 22, 0, tzinfo=UTC)
SPIKE = [{"type": "attention_spike", "at": "2026-10-23T05:00:00Z", "basis": "threshold"}]
PUBLISHED = [{"type": "published", "at": "2026-10-23T05:00:00Z", "basis": None}]


@pytest.fixture
def conn(tmp_path):
    conn = open_db(tmp_path / "tech-researcher.sqlite3")
    start_run(conn, RUN, daily_target(date(2026, 10, 24), 1), AT)
    start_run(conn, RETRY, daily_target(date(2026, 10, 24), 1), AT)
    yield conn
    conn.close()


def add_item(conn, url, events=PUBLISHED, target="2026-10-23", cadence="daily"):
    """items に最小の行を足し、AI に渡す形の ID（i48213 など）を返す。"""
    (item_id,) = conn.execute(
        "INSERT INTO items (cadence, target, canonical_url, kind, title, published_precision,"
        " inclusion_event, inclusion_at, events_json, attention_json, sources_json, links_json,"
        " meta_terms_json, first_run_id, last_run_id) VALUES (?, ?, ?, 'article', 't', 'datetime',"
        " 'published', 'a', ?, '[]', '[]', '[]', '[]', ?, ?) RETURNING item_id",
        (cadence, target, url, json.dumps(events), RUN, RUN),
    ).fetchone()
    return f"i{item_id}"


def daily_entry(*ids, no=1, **kw):
    return EntryRow("daily_item", "published", tuple(ids), display_no=no, **kw)


def passed(*entries, **kw):
    return ReportRow(
        "daily", DAY, "passed", {"collected": 3}, {"D1": "ok"}, entries, body_markdown="本文", **kw
    )


def rows(conn, table, order):
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]
    finally:
        conn.row_factory = None


def test_saves_report_with_published_and_excluded_entries(conn):
    a = add_item(conn, "https://e.com/a")
    excluded = EntryRow(
        "daily_item",
        "excluded",
        ("i999999",),
        payload={"headline": "見出し"},
        exclusion_reasons=("invalid_id", "long_quote"),
    )
    report = passed(
        daily_entry(a, payload={"headline": "日本語"}),
        excluded,
        ai_output='{"items":[]}',
    )
    save_report(conn, RUN, report)
    (r,) = rows(conn, "reports", "run_id")
    assert r == {
        "run_id": RUN,
        "cadence": "daily",
        "target": "2026-10-23",
        "status": "passed",
        "rejected_checks": "[]",
        "counts_json": '{"collected":3}',
        "verification_json": '{"D1":"ok"}',
        "ai_output_json": '{"items":[]}',
        "body_markdown": "本文",
    }
    first, second = rows(conn, "report_entries", "entry_no")
    assert (first["entry_no"], first["status"], first["display_no"]) == (1, "published", 1)
    assert json.loads(first["item_ids_json"]) == [a]
    assert json.loads(first["payload_json"]) == {"headline": "日本語"}
    assert (second["entry_no"], second["status"], second["display_no"]) == (2, "excluded", None)
    assert json.loads(second["exclusion_reasons"]) == ["invalid_id", "long_quote"]
    # 除外した項目は、存在しない ID も、理由のためにそのまま残す
    assert json.loads(second["item_ids_json"]) == ["i999999"]


def test_rejected_report_keeps_checks_and_ai_output(conn):
    report = ReportRow(
        "daily",
        DAY,
        "rejected",
        {},
        {"D6a": "fail"},
        rejected_checks=("invalid_output",),
        ai_output="壊れた出力",
    )
    save_report(conn, RUN, report)
    (r,) = rows(conn, "reports", "run_id")
    assert (r["status"], r["rejected_checks"], r["ai_output_json"], r["body_markdown"]) == (
        "rejected",
        '["invalid_output"]',
        "壊れた出力",
        None,
    )
    assert rows(conn, "report_entries", "entry_no") == []


def test_retry_in_same_run_replaces_report_and_entries(conn):
    a, b = add_item(conn, "https://e.com/a"), add_item(conn, "https://e.com/b")
    save_report(conn, RUN, passed(daily_entry(a), daily_entry(b, no=2)))
    save_report(conn, RUN, passed(daily_entry(b)))
    assert len(rows(conn, "reports", "run_id")) == 1
    (entry,) = rows(conn, "report_entries", "entry_no")
    assert json.loads(entry["item_ids_json"]) == [b]


def test_other_run_is_another_row(conn):
    a = add_item(conn, "https://e.com/a")
    save_report(conn, RUN, passed(daily_entry(a)))
    save_report(conn, RETRY, passed(daily_entry(a)))
    assert [r["run_id"] for r in rows(conn, "reports", "run_id")] == [RUN, RETRY]
    assert len(rows(conn, "report_entries", "run_id")) == 2


def test_published_item_must_exist_and_nothing_is_written(conn):
    a = add_item(conn, "https://e.com/a")
    with pytest.raises(LookupError, match="items にない"):
        save_report(conn, RUN, passed(daily_entry(a), daily_entry("i999999", no=2)))
    with pytest.raises(LookupError, match="形でない"):
        save_report(conn, RUN, passed(daily_entry("d1")))  # ドライランの仮の ID
    assert rows(conn, "reports", "run_id") == []


def test_dry_run_id_is_not_written(conn):
    with pytest.raises(ValueError, match="ドライラン"):
        save_report(conn, "dry-20261024-060000-abcd", passed())
    assert rows(conn, "reports", "run_id") == []


def test_report_requires_existing_run(conn):
    with pytest.raises(sqlite3.IntegrityError):
        save_report(conn, "20261024-090000-ffff", passed())


def weekly_report(*entries):
    return ReportRow("weekly", DAY, "passed", {}, {}, entries, body_markdown="b")


@pytest.mark.parametrize(
    "build",
    [
        lambda: EntryRow(
            "daily_item", "published", ("i1",), display_no=1, exclusion_reasons=("x",)
        ),
        lambda: EntryRow("daily_item", "excluded"),
        lambda: EntryRow("daily_item", "excluded", exclusion_reasons=("D6(b)",)),
        lambda: EntryRow("daily_item", "published", ("i1",)),  # display_no がない
        lambda: EntryRow("daily_item", "published", (), display_no=1),
        lambda: EntryRow("theme", "published", ("i1",)),  # theme_id がない
        lambda: EntryRow("theme", "published", ("i1",), theme_id="7"),
        lambda: EntryRow("watch", "published", ("i1",), theme_id="T0001"),
        lambda: EntryRow("other", "excluded", exclusion_reasons=("x",)),
        lambda: ReportRow("daily", DAY, "passed", {}, {}),  # 本文がない
        lambda: ReportRow(  # 不合格（秘匿情報の検出を含む）の本文は保存しない
            "daily",
            DAY,
            "rejected",
            {},
            {},
            rejected_checks=("secret_detected",),
            body_markdown="b",
        ),
        lambda: ReportRow("daily", DAY, "rejected", {}, {}),  # 検査の名前がない
        lambda: ReportRow(
            "daily", DAY, "passed", {}, {}, rejected_checks=("x",), body_markdown="b"
        ),
        lambda: weekly_report(EntryRow("daily_item", "published", ("i1",), display_no=1)),
    ],
)
def test_invalid_rows_are_refused(build):
    with pytest.raises(ValueError):
        build()


def test_display_no_is_unique_in_a_run(conn):
    a, b = add_item(conn, "https://e.com/a"), add_item(conn, "https://e.com/b")
    with pytest.raises(sqlite3.IntegrityError):
        save_report(conn, RUN, passed(daily_entry(a), daily_entry(b)))
    assert rows(conn, "reports", "run_id") == []


def publish(conn, run, day, url, events):
    item = add_item(conn, url, events, target=day.isoformat())
    save_report(
        conn, run, ReportRow("daily", day, "passed", {}, {}, [daily_entry(item)], body_markdown="b")
    )
    record_issue(conn, run, "report", "daily", day, "created", AT, repo="o/r", number=1)
    return item


def test_featured_urls_come_from_created_report_issues_only(conn):
    publish(conn, RUN, DAY, "https://e.com/spike", SPIKE + PUBLISHED)
    publish(conn, RETRY, date(2026, 10, 22), "https://e.com/plain", PUBLISHED)
    assert featured_urls(conn) == {"https://e.com/spike"}


def test_featured_urls_exclude_unpublished_states(conn):
    runs = [f"20261024-1{n}0000-aaaa" for n in range(4)]
    for run in runs:
        start_run(conn, run, daily_target(date(2026, 10, 24), 1), AT)
    # 除外した項目
    item = add_item(conn, "https://e.com/excluded", SPIKE)
    excluded = EntryRow("daily_item", "excluded", (item,), exclusion_reasons=("long_quote",))
    save_report(conn, runs[0], passed(excluded))
    record_issue(conn, runs[0], "report", "daily", DAY, "created", AT, repo="o/r", number=1)
    # 掲載したが、Issue を作れなかった・止めた実行、Issue の記録がない実行
    for run, state in ((runs[1], "failed"), (runs[2], "blocked_secret")):
        item = add_item(conn, f"https://e.com/{state}", SPIKE)
        save_report(conn, run, passed(daily_entry(item)))
        record_issue(conn, run, "report", "daily", DAY, state, AT)
    item = add_item(conn, "https://e.com/no-issue", SPIKE)
    save_report(conn, runs[3], passed(daily_entry(item)))
    assert featured_urls(conn) == set()


def test_featured_urls_ignore_weekly_reports(conn):
    item = add_item(conn, "https://e.com/w", SPIKE, cadence="weekly")
    save_report(conn, RUN, weekly_report(EntryRow("watch", "published", (item,))))
    record_issue(conn, RUN, "report", "weekly", DAY, "created", AT, repo="o/r", number=1)
    assert featured_urls(conn) == set()


def test_shown_urls_is_for_the_given_day(conn):
    publish(conn, RUN, DAY, "https://e.com/yesterday", PUBLISHED)
    publish(conn, RETRY, date(2026, 10, 22), "https://e.com/older", PUBLISHED)
    assert shown_urls(conn, DAY) == {"https://e.com/yesterday"}
    assert shown_urls(conn, date(2026, 10, 21)) == set()


def weekly_run(
    conn, run, week, theme_id, name, status, *, state="created", urls=("https://e.com/x",)
):
    start_run(conn, run, weekly_target(week, 1), AT)
    ids = tuple(add_item(conn, u, target=week.isoformat(), cadence="weekly") for u in urls)
    payload = {"name": name, "status": status}
    entry = EntryRow("theme", "published", ids, theme_id=theme_id, payload=payload)
    report = ReportRow("weekly", week, "passed", {}, {}, [entry], body_markdown="b")
    save_report(conn, run, report)
    if state == "created":
        record_issue(conn, run, "report", "weekly", week, state, AT, repo="o/r", number=2)
    else:
        record_issue(conn, run, "report", "weekly", week, state, AT)


def test_past_themes_use_window_published_issues_and_latest_name(conn):
    weekly_run(conn, "20261012-070000-0001", date(2026, 10, 12), "T0001", "旧名", "rising")
    weekly_run(
        conn,
        "20261019-070000-0002",
        date(2026, 10, 19),
        "T0001",
        "新名",
        "steady",
        urls=("https://e.com/a", "https://e.com/b"),
    )
    weekly_run(
        conn,
        "20261026-070000-0003",
        date(2026, 10, 26),
        "T0002",
        "失敗の週",
        "rising",
        state="failed",
    )
    weekly_run(conn, "20260921-070000-0004", date(2026, 9, 21), "T0009", "窓の外", "rising")
    target = date(2026, 11, 9)
    themes = past_themes(conn, target, 4)
    assert [(t.theme_id, t.name) for t in themes] == [("T0001", "新名")]
    assert themes[0].history == (
        (date(2026, 10, 12), "rising", 1),
        (date(2026, 10, 19), "steady", 2),
    )
    assert past_themes(conn, target, 0) == []


def test_next_theme_id_counts_unpublished_weeks_too(conn):
    assert next_theme_id(conn) == "T0001"
    weekly_run(
        conn, "20261012-070000-0001", date(2026, 10, 12), "T0007", "a", "rising", state="failed"
    )
    assert next_theme_id(conn) == "T0008"


def test_history_join_uses_the_items_primary_key(conn):
    sql = f"EXPLAIN QUERY PLAN SELECT it.canonical_url {history._PUBLISHED_DAILY}"
    plan = " ".join(row[3] for row in conn.execute(sql))
    assert "SCAN it" not in plan
    assert "SEARCH it USING INTEGER PRIMARY KEY" in plan


def test_leading_zero_id_is_not_published(conn):
    a = add_item(conn, "https://e.com/a")
    with pytest.raises(LookupError, match="形でない"):
        save_report(conn, RUN, passed(daily_entry("i0" + a[1:])))


def test_history_queries_work_on_a_readonly_connection(conn, tmp_path):
    publish(conn, RUN, DAY, "https://e.com/spike", SPIKE)
    ro = open_readonly(tmp_path / "tech-researcher.sqlite3")
    assert featured_urls(ro) == {"https://e.com/spike"}
    assert shown_urls(ro, DAY) == {"https://e.com/spike"}
    assert past_themes(ro, date(2026, 11, 9), 4) == []
    assert next_theme_id(ro) == "T0001"
    ro.close()


def test_json_array_columns_are_checked(conn):
    save_report(conn, RUN, passed())
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE reports SET rejected_checks = '{}' WHERE run_id = ?", (RUN,))
