"""週次の成功の経路の一巡（D-04・D-18、data-model.md 3.6・3.7）。認証情報も GitHub も使わない。

統括スクリプトから ① 収集 → ② 分析 → ③ 検証・保存 → ⑤ 投稿までを本物のコードでつなぎ、外との境界
（GitHub・HTTP・AI）だけを偽にする。偽の境界は日次の一巡（test_daily_flow.py）と同じもの。
"""

import contextlib
import json
from datetime import date, datetime

import pytest

from tech_researcher import orchestrator
from tech_researcher.agent import invoke
from tech_researcher.db.history import past_themes
from tech_researcher.db.schema import open_db
from tech_researcher.periods import JST
from tech_researcher.runfiles import db_path, new_run_id
from tests.unit.test_daily_flow import EMPTY_FEED, FEED, ISSUE_REPO, Host, host  # noqa: F401

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


def _theme_ai(data):
    return {"themes": []}  # スキーマに合わない出力（D6(a)）


def test_rejected_week_posts_a_rejected_issue_and_closes_the_run(weekly):
    weekly.ai = _theme_ai
    code, summary = weekly.run_weekly()

    assert code == 1 and summary["result"] == "rejected"
    run_id = summary["run_id"]
    # 不合格の後に ② を呼び直さず、④ を1回だけ呼ぶ。runs を閉じてから、不合格 Issue を投稿する
    assert [c["task"] for c in weekly.calls("agent")] == ["weekly", "investigate"]
    assert [c["args"][0] for c in weekly.calls("compose")] == [
        "collect",
        "verify",
        "finish",
        "post",
    ]
    [created] = weekly.github.created
    assert created["labels"] == ("type:rejected", "cadence:weekly")
    assert created["title"].startswith("[不合格][週次] 2026-10-26（月）号のレポート")
    assert "## 調査結果" in created["body"]
    assert summary["issue_url"] == f"https://github.com/{ISSUE_REPO}/issues/100"
    assert query(
        weekly, "SELECT status, rejected_checks FROM reports WHERE run_id = ?", run_id
    ) == [("rejected", '["invalid_output"]')]
    [(status, error_category, investigation)] = query(
        weekly,
        "SELECT status, error_category, investigation_json FROM runs WHERE run_id = ?",
        run_id,
    )
    assert (status, error_category) == ("rejected", "invalid_output")
    assert json.loads(investigation)["confidence"] == "medium"
    assert query(weekly, "SELECT kind, state FROM issues WHERE run_id = ?", run_id) == [
        ("rejected", "created")
    ]


def test_weekly_dry_run_rejection_calls_investigation_but_writes_nothing(weekly):
    """ドライランは ④ を呼んで結果を残し、DB に書かず、⑤ を実行せず、再生成もしない。"""
    weekly.ai = _theme_ai
    code, summary = weekly.run_weekly("--dry-run")

    assert code == 1 and summary["result"] == "rejected" and summary["issue_url"] is None
    assert [c["task"] for c in weekly.calls("agent")] == ["weekly", "investigate"]
    assert [c["args"][0] for c in weekly.calls("compose")] == ["collect", "verify"]
    out = weekly.data / "dry-run" / summary["run_id"]
    assert json.loads((out / "investigation.json").read_text("utf-8"))["confidence"] == "medium"
    issue = (out / "issue.md").read_text("utf-8")
    assert issue.splitlines()[0].startswith("[不合格][週次] 2026-10-26（月）号のレポート")
    assert not (weekly.data / "db").exists()  # DB を作らない（D-22）
    assert weekly.github.created == []


def test_all_sources_failed_week_posts_a_run_error_issue(weekly):
    weekly.feed = None
    code, summary = weekly.run_weekly()

    assert code == 1
    assert (summary["result"], summary["failed_stage"], summary["error_category"]) == (
        "error",
        "collect",
        "all_sources_failed",
    )
    assert summary["failed_sources"] == ["Example"]
    assert [c["task"] for c in weekly.calls("agent")] == ["investigate"]  # ② は呼ばない
    assert [c["args"][0] for c in weekly.calls("compose")] == ["collect", "finish", "post"]
    [created] = weekly.github.created
    assert created["labels"] == ("type:run-error", "cadence:weekly")
    assert created["title"].startswith("[実行エラー][週次] 2026-10-26（月）号（① 収集")
    assert "## 調査結果" in created["body"]
    assert query(
        weekly,
        "SELECT status, failed_stage, error_category FROM runs WHERE run_id = ?",
        summary["run_id"],
    ) == [("failed", "collect", "all_sources_failed")]


def test_weekly_dry_run_collect_failure_calls_investigation_without_writing(weekly):
    weekly.feed = None
    code, summary = weekly.run_weekly("--dry-run")

    assert code == 1 and summary["failed_stage"] == "collect"
    assert [c["args"][0] for c in weekly.calls("compose")] == ["collect"]
    out = weekly.data / "dry-run" / summary["run_id"]
    assert (out / "investigation.json").exists() and (out / "issue.md").exists()
    assert not (weekly.data / "db").exists()
    assert weekly.github.created == []


def test_weekly_ai_failure_closes_the_run_with_the_model_and_posts(weekly):
    def broken(data):
        raise invoke.AgentError("agent_failed", models=("claude-x",))

    weekly.ai = broken
    code, summary = weekly.run_weekly()

    assert (code, summary["failed_stage"], summary["error_category"]) == (
        1,
        "analyze",
        "agent_failed",
    )
    assert [c["task"] for c in weekly.calls("agent")] == ["weekly", "investigate"]
    assert query(
        weekly,
        "SELECT status, failed_stage, error_category, model FROM runs WHERE run_id = ?",
        summary["run_id"],
    ) == [("failed", "analyze", "agent_failed", "claude-x")]
    [created] = weekly.github.created
    assert created["labels"] == ("type:run-error", "cadence:weekly")


@pytest.mark.parametrize("category", ["usage_limit", "missing_skill"])
def test_weekly_ai_unavailable_skips_investigation_and_uses_the_template(weekly, category):
    def broken(data):
        raise invoke.AgentError(category)

    weekly.ai = broken
    code, summary = weekly.run_weekly("--dry-run")

    assert (code, summary["error_category"]) == (1, category)
    assert [c["task"] for c in weekly.calls("agent")] == ["weekly"]  # ④ を呼ばない
    out = weekly.data / "dry-run" / summary["run_id"]
    assert not (out / "investigation.json").exists()
    issue = (out / "issue.md").read_text("utf-8")
    assert "AI による調査結果を掲載できませんでした。" in issue and "## 調査結果" not in issue


def test_weekly_secret_stop_closes_the_run_and_posts_the_fixed_issue(weekly):
    secret = weekly.worker_env["GITHUB_ISSUE_TOKEN"]

    def leaking(data):
        output = weekly_ai(data)
        output["themes"][0]["change"] = f"値は {secret} です"
        return output

    weekly.ai = leaking
    code, summary = weekly.run_weekly()

    assert code == 1
    assert (summary["result"], summary["failed_stage"], summary["error_category"]) == (
        "error",
        "verify",
        "secret_detected",
    )
    assert [c["task"] for c in weekly.calls("agent")] == ["weekly"]  # ④ は呼ばない
    assert [c["args"][0] for c in weekly.calls("compose")] == [
        "collect",
        "verify",
        "finish",
        "post",
    ]
    assert query(
        weekly,
        "SELECT status, failed_stage, error_category FROM runs WHERE run_id = ?",
        summary["run_id"],
    ) == [("failed", "verify", "secret_detected")]
    [created] = weekly.github.created
    assert created["title"] == "[実行エラー][週次] 2026-10-26（月）号（公開停止）"
    assert secret not in created["body"] and "公開を停止しました" in created["body"]
    print(query(weekly, "SELECT kind, state FROM issues WHERE run_id = ?", summary["run_id"]))


def test_weekly_dry_run_secret_stop_writes_only_the_fixed_issue(weekly):
    secret = weekly.worker_env["GITHUB_ISSUE_TOKEN"]
    weekly.ai = lambda data: {
        **weekly_ai(data),
        "watch": [{"item_id": data["items"][0]["id"], "reason": secret}],
    }
    code, summary = weekly.run_weekly("--dry-run")

    assert code == 1 and summary["error_category"] == "secret_detected"
    out = weekly.data / "dry-run" / summary["run_id"]
    assert "（公開停止）" in (out / "issue.md").read_text("utf-8").splitlines()[0]
    assert not (out / "investigation.json").exists()
    assert [c["args"][0] for c in weekly.calls("compose")] == ["collect", "verify"]
    assert not (weekly.data / "db").exists()


def test_week_without_observations_still_asks_the_ai_about_past_themes(weekly):
    """今週の観測が0件でも、過去のテーマがあれば ② を呼ぶ（沈静したテーマを出す）。"""
    weekly.run_weekly()
    weekly.start(NEXT_MONDAY)
    weekly.feed = EMPTY_FEED

    def fading(data):
        past, observation = data["past_themes"][0], data["past_observations"][0]
        theme = _theme(past["theme_id"], past["name"], [], [observation["id"]])
        return {
            "themes": [{**theme, "status": "fading"}],
            "tech_changes": [],
            "comparisons": [],
            "watch": [],
        }

    weekly.ai = fading
    code, summary = weekly.run_weekly()

    assert code == 0 and summary["result"] == "report"
    assert weekly.calls("agent")[-1]["items"] == []
    assert query(
        weekly,
        "SELECT status, theme_id FROM report_entries WHERE run_id = ?",
        summary["run_id"],
    ) == [("published", "T0001")]


def test_week_with_nothing_at_all_does_not_call_the_ai(weekly):
    weekly.feed = EMPTY_FEED
    code, summary = weekly.run_weekly()

    assert code == 0 and summary["result"] == "report"
    assert weekly.calls("agent") == []
