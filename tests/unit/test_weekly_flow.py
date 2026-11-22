"""週次の成功の経路の一巡（D-04・D-18、data-model.md 3.6・3.7）。認証情報も GitHub も使わない。

統括スクリプトから ① 収集 → ② 分析 → ③ 検証・保存 → ⑤ 投稿までを本物のコードでつなぎ、外との境界
（GitHub・HTTP・AI）だけを偽にする。偽の境界は日次の一巡（test_daily_flow.py）と同じもの。
"""

import contextlib
from datetime import date, datetime

import pytest

from tech_researcher import orchestrator
from tech_researcher.db.history import past_themes
from tech_researcher.db.schema import open_db
from tech_researcher.periods import JST
from tech_researcher.runfiles import db_path, new_run_id
from tests.unit.test_daily_flow import FEED, ISSUE_REPO, Host, host  # noqa: F401

MONDAY = datetime(2026, 10, 26, 7, 0, tzinfo=JST)  # 対象週は 2026-10-26（10-19〜10-25 を調べる）
NEXT_MONDAY = datetime(2026, 11, 2, 7, 0, tzinfo=JST)
# 翌週の分（10-26〜11-01）に公開した2件
NEXT_FEED = FEED.replace(b"Fri, 23 Oct 2026 01:00:00 +0000", b"Tue, 27 Oct 2026 01:00:00 +0000")
NEXT_FEED = NEXT_FEED.replace(
    b"Fri, 23 Oct 2026 20:00:00 +0900", b"Wed, 28 Oct 2026 20:00:00 +0900"
)


def weekly_ai(data: dict) -> dict:
    """新しいテーマを1つ。過去のテーマが渡されたら、それを続くテーマとして1つ足す。"""
    first, second = data["items"][:2]
    themes = [_theme(None, "新しいテーマ", [first["id"]], [])]
    if data["past_themes"]:
        past = data["past_themes"][0]
        past_id = data["past_observations"][0]["id"]
        themes.insert(0, _theme(past["theme_id"], past["name"], [second["id"]], [past_id]))
    return {"themes": themes, "tech_changes": [], "comparisons": [], "watch": []}


def _theme(theme_id, name, item_ids, past_ids):
    return {
        "theme_id": theme_id,
        "name": name,
        "status": "rising" if theme_id is None else "steady",
        "change": "変化",
        "comparison": "比較",
        "item_ids": item_ids,
        "past_ids": past_ids,
    }


@pytest.fixture
def weekly(host, monkeypatch):  # noqa: F811
    host.ai = weekly_ai

    def start(moment):
        monkeypatch.setattr(
            orchestrator, "new_run_id", lambda now, **kwargs: new_run_id(moment, **kwargs)
        )

    start(MONDAY)
    host.start = start
    host.run_weekly = lambda *argv: _run(host, *argv)
    return host


def _run(h, *argv):
    code = orchestrator.main(["weekly", *argv], h.environ)
    return code, h._summary()[0]


def query(h, sql, *args):
    with contextlib.closing(open_db(db_path(h.data))) as conn:
        return conn.execute(sql, args).fetchall()


def test_production_week_saves_numbers_and_posts(weekly):
    code, summary = weekly.run_weekly()

    assert code == 0
    run_id = summary["run_id"]
    assert (summary["cadence"], summary["target"], summary["result"]) == (
        "weekly",
        "2026-10-26",
        "report",
    )
    assert summary["issue_url"] == f"https://github.com/{ISSUE_REPO}/issues/100"
    assert [c["args"][0] for c in weekly.calls("compose")] == ["collect", "verify", "post"]
    [created] = weekly.github.created
    assert created["labels"] == ("type:report", "cadence:weekly")
    assert created["title"].startswith("[週次] 2026-10-26（月）号")
    assert f"cadence=weekly target=2026-10-26 run={run_id}" in created["body"]
    assert "新しいテーマ" in created["body"]
    # reports・report_entries：新しいテーマの番号はコードが振る
    assert query(weekly, "SELECT status, body_markdown FROM reports WHERE run_id = ?", run_id) == [
        ("passed", created["body"])
    ]
    assert query(
        weekly,
        "SELECT section, status, theme_id FROM report_entries WHERE run_id = ?",
        run_id,
    ) == [("theme", "published", "T0001")]
    assert query(weekly, "SELECT status, failed_stage FROM runs WHERE run_id = ?", run_id) == [
        ("succeeded", None)
    ]
    assert query(weekly, "SELECT state, projects_state FROM issues WHERE run_id = ?", run_id) == [
        ("created", "done")
    ]


def test_second_run_for_the_same_week_does_not_post_again(weekly):
    """再試行（別の実行）で Issue は重複せず、既存の Issue として成功になる。"""
    _, first = weekly.run_weekly()
    weekly.start(MONDAY.replace(hour=8))
    code, second = weekly.run_weekly()

    assert code == 0 and first["run_id"] != second["run_id"]
    assert second["result"] == "report" and second["issue_url"] == first["issue_url"]
    assert len(weekly.github.created) == 1
    # 保存は実行ごと。公開したのは最初の実行だけなので、過去のテーマは最初の実行の分だけ
    assert query(weekly, "SELECT count(*) FROM reports WHERE cadence = 'weekly'") == [(2,)]
    with contextlib.closing(open_db(db_path(weekly.data))) as conn:
        themes = past_themes(conn, date(2026, 11, 2), 4)
    assert [t.theme_id for t in themes] == ["T0001"]


def test_next_week_continues_a_published_theme_and_numbers_the_new_one(weekly):
    weekly.run_weekly()
    weekly.start(NEXT_MONDAY)
    weekly.feed = NEXT_FEED
    code, summary = weekly.run_weekly()

    assert code == 0 and summary["result"] == "report"
    # ② には、公開した先週のテーマと観測が渡る
    second = weekly.calls("agent")[-1]
    assert [t["theme_id"] for t in second["past_themes"]] == ["T0001"]
    assert query(
        weekly,
        "SELECT status, theme_id FROM report_entries WHERE run_id = ? ORDER BY entry_no",
        summary["run_id"],
    ) == [("published", "T0001"), ("published", "T0002")]
    assert len(weekly.github.created) == 2


def test_weekly_dry_run_goes_through_verify_without_writing(weekly):
    code, summary = weekly.run_weekly("--dry-run")

    assert code == 0 and summary["result"] == "dry_run"
    assert [c["args"][0] for c in weekly.calls("compose")] == ["collect", "verify"]  # ⑤ は呼ばない
    issue = (weekly.data / "dry-run" / summary["run_id"] / "issue.md").read_text("utf-8")
    assert issue.splitlines()[0].startswith("[週次] 2026-10-26（月）号")
    assert "新しいテーマ" in issue
    assert not (weekly.data / "db").exists()  # DB を作らない（D-22）
    assert weekly.github.created == []


def test_rejected_week_closes_the_run_without_posting(weekly):
    weekly.ai = lambda data: {"themes": []}  # スキーマに合わない出力（D6(a)）
    code, summary = weekly.run_weekly()

    assert code == 1 and summary["result"] == "rejected"
    assert weekly.github.created == []
    assert [c["args"][0] for c in weekly.calls("compose")] == ["collect", "verify", "finish"]
    run_id = summary["run_id"]
    assert query(weekly, "SELECT status FROM runs WHERE run_id = ?", run_id) == [("rejected",)]
    assert query(
        weekly, "SELECT status, rejected_checks FROM reports WHERE run_id = ?", run_id
    ) == [("rejected", '["invalid_output"]')]
