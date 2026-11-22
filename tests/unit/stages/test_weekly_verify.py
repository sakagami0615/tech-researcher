"""週次の ③ の保存（data-model.md 3.4・3.6・3.7、D-18）。認証情報も GitHub も使わない。"""

import json
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest

from tech_researcher.db.history import next_theme_id, past_themes
from tech_researcher.db.issues import record_issue
from tech_researcher.db.readonly import open_readonly
from tech_researcher.db.schema import open_db
from tech_researcher.logging import RunLog
from tech_researcher.stages import weekly_verify
from tech_researcher.stages.collect import ITEM_IDS
from tech_researcher.stages.daily import AGENT_OUTPUT
from tech_researcher.stages.weekly_verify import run_verify_weekly
from tech_researcher.validate.secret_scan import SecretScanner
from tests.unit.test_weekly_materials import CONFIG, STARTED, WEEK, collect, rss

RUN = "20261026-070000-abcd"
A, B, C = ("https://a.example.com/1", "https://b.example.com/2", "https://c.example.com/3")
FRESH = [
    (A, "FooDB 3.0 公開"),
    (B, "Bar という新しいツール"),
    (C, "Baz の話"),
]


@pytest.fixture
def conn(tmp_path):
    conn = open_db(tmp_path / "tech-researcher.sqlite3")
    yield conn
    conn.close()


def theme(item_ids, theme_id=None, name="テーマ", past_ids=()):
    return {
        "theme_id": theme_id,
        "name": name,
        "status": "rising",
        "change": "変化",
        "comparison": "比較",
        "item_ids": list(item_ids),
        "past_ids": list(past_ids),
    }


def output(**sections):
    return {"themes": [], "tech_changes": [], "comparisons": [], "watch": [], **sections}


class Week:
    """今週の観測（3件）を集め、実行のフォルダを用意する。"""

    def __init__(self, tmp_path, conn, name="w", run_id=RUN):
        self.conn, self.run_id = conn, run_id
        records = [rss(url, date(2026, 10, 20), title) for url, title in FRESH]
        _, self.run_dir = collect(tmp_path, name, conn, run_id, records)
        self.ids = json.loads((self.run_dir / ITEM_IDS).read_text("utf-8"))
        self.by_url = dict(
            conn.execute("SELECT canonical_url, 'i' || item_id FROM items WHERE cadence='weekly'")
        )
        self.scanner = SecretScanner()
        self.log = RunLog(self.run_dir / "log.jsonl", run_id, "weekly", WEEK.key, self.scanner)

    def verify(self, out, write=True, db="conn"):
        (self.run_dir / AGENT_OUTPUT).write_text(json.dumps(out, ensure_ascii=False))
        db = self.conn if db == "conn" else db
        return run_verify_weekly(
            CONFIG, WEEK, self.run_id, self.run_dir, self.log, self.scanner, db, write=write
        )

    def validation(self):
        return json.loads((self.run_dir / "validation.json").read_text("utf-8"))


@pytest.fixture
def week(tmp_path, conn):
    return Week(tmp_path, conn)


def rows(conn, run_id=RUN):
    return conn.execute(
        "SELECT entry_no, section, status, exclusion_reasons, theme_id, item_ids_json, payload_json"
        " FROM report_entries WHERE run_id = ? ORDER BY entry_no",
        (run_id,),
    ).fetchall()


def report(conn, run_id=RUN):
    return conn.execute(
        "SELECT status, rejected_checks, counts_json, body_markdown, ai_output_json FROM reports"
        " WHERE run_id = ?",
        (run_id,),
    ).fetchone()


def full_output(week):
    a, b, c = (week.by_url[u] for u in (A, B, C))
    return output(
        themes=[
            theme([a], name="新しいテーマ"),
            theme([b], theme_id="T0099", name="不正な番号"),  # 渡していない番号は除外
            theme([c], name="もう1つの新しいテーマ"),
        ],
        tech_changes=[{"headline": "変化", "description": "説明", "item_ids": [a]}],
        comparisons=[
            {
                "domain": "領域",
                "rows": [
                    {"item_id": a, "product_name": "FooDB", "feature": "特徴", "use_case": "用途"},
                    {
                        "item_id": b,
                        "product_name": "Bar",
                        "feature": "あ" * 200,
                        "use_case": "用途",
                    },
                ],
                "summary": "まとめ",
            }
        ],
        watch=[{"name": "注目", "reason": "理由", "item_ids": [c]}],
    )


def test_saves_report_entries_in_ai_order_with_theme_numbers(week, conn):
    result = week.verify(full_output(week))

    assert result == {"result": "report", "entries": 5, "excluded": 2}
    assert [
        (no, section, status, json.loads(reasons), theme_id)
        for no, section, status, reasons, theme_id, *_ in rows(conn)
    ] == [
        (1, "theme", "published", [], "T0001"),
        (2, "theme", "excluded", ["invalid_id"], None),
        (3, "theme", "published", [], "T0002"),
        (4, "tech_change", "published", [], None),
        (5, "comparison", "published", [], None),
        (6, "comparison", "excluded", ["too_long"], None),
        (7, "watch", "published", [], None),
    ]
    status, checks, counts, body, ai_output = report(conn)
    assert (status, json.loads(checks)) == ("passed", [])
    assert json.loads(counts) == {
        "collected": 3,
        "ai_output": 6,
        "excluded": 1,
        "excluded_by_reason": {"invalid_id": 1},
        "excluded_rows": 1,
        "published": 5,
    }
    assert json.loads(ai_output) == full_output(week)
    assert body == (week.run_dir / "issue.md").read_text("utf-8").split("\n\n---\n\n", 1)[1]
    # 除外したテーマの AI の番号は列に入れず、payload に残す
    payload = json.loads(rows(conn)[1][6])
    assert (payload["ai_theme_id"], payload["exclusion_detail"]) == (
        "T0099",
        "渡していない theme_id",
    )
    assert week.validation()["verification"]["invalid_id"] == "failed"


def test_comparison_entry_matches_the_rows_in_the_body(week, conn):
    week.verify(full_output(week))

    published, excluded = rows(conn)[4], rows(conn)[5]
    kept = json.loads(published[6])["rows"]
    assert [r["product_name"] for r in kept] == ["FooDB"]  # 除外した行は含まない
    assert json.loads(published[5]) == [week.by_url[A]]
    assert json.loads(excluded[6])["row"]["product_name"] == "Bar"
    assert json.loads(excluded[5]) == [week.by_url[B]]
    body = report(conn)[3]
    assert "FooDB" in body and "あ" * 200 not in body


def test_retry_in_the_same_run_replaces_rows_and_keeps_the_numbers(week, conn):
    out = full_output(week)
    week.verify(out)
    first = rows(conn)
    week.verify(out)

    assert rows(conn) == first  # 番号は振り直しても同じ（自分の実行の行は数えない）
    assert conn.execute("SELECT count(*) FROM reports").fetchone() == (1,)
    assert next_theme_id(conn) == "T0003"


def test_unpublished_themes_are_not_past_themes_but_their_numbers_are_not_reused(week, conn):
    week.verify(full_output(week))

    # Issue を作っていない（公開していない）週のテーマは、過去のテーマに入れない
    assert past_themes(conn, date(2026, 11, 2), 4) == []
    assert next_theme_id(conn) == "T0003"
    record_issue(conn, RUN, "report", "weekly", WEEK.key, "created", STARTED, repo="o/r", number=5)
    assert [t.theme_id for t in past_themes(conn, date(2026, 11, 2), 4)] == ["T0001", "T0002"]


def test_dry_run_writes_files_but_nothing_to_the_db(week, conn):
    readonly = open_readonly(Path(conn.execute("PRAGMA database_list").fetchone()[2]))
    with closing(readonly):
        result = week.verify(full_output(week), write=False, db=readonly)

    assert result["result"] == "report"
    assert (week.run_dir / "issue.md").is_file()
    assert conn.execute("SELECT count(*) FROM reports").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM report_entries").fetchone() == (0,)
    assert week.validation()["counts"]["published"] == 5


def test_ai_choosing_nothing_is_a_report_saying_so(week, conn):
    result = week.verify(output())

    assert result == {"result": "report", "entries": 0, "excluded": 0}
    assert rows(conn) == []
    assert report(conn)[0] == "passed"
    assert "今週は該当なし" in (week.run_dir / "issue.md").read_text("utf-8")


def test_invalid_output_is_rejected_with_the_original_kept(week, conn):
    result = week.verify({"themes": []})

    assert (result["result"], week.validation()["reason"]) == ("rejected", "invalid_output")
    status, checks, _, body, ai_output = report(conn)
    assert (status, json.loads(checks), body) == ("rejected", ["invalid_output"], None)
    assert json.loads(ai_output) == {"themes": []}
    assert rows(conn) == []
    assert not (week.run_dir / "issue.md").exists()


def test_all_excluded_is_rejected_and_keeps_the_excluded_entries(week, conn):
    out = output(themes=[theme([week.by_url[A]], theme_id="T0099")], watch=[])
    result = week.verify(out)

    assert (result["result"], week.validation()["reason"]) == ("rejected", "all_excluded")
    assert report(conn)[0] == "rejected"
    assert [(r[1], r[2]) for r in rows(conn)] == [("theme", "excluded")]
    assert next_theme_id(conn) == "T0001"  # 除外したテーマの番号は数えない
    assert not (week.run_dir / "issue.md").exists()


def test_secret_in_the_raw_output_keeps_nothing(week, conn):
    token = "ghp" + "_" + "q" * 36
    out = output(watch=[{"name": "注目", "reason": f"鍵 {token}", "item_ids": [week.by_url[C]]}])
    result = week.verify(out)

    assert result["result"] == "secret_detected"
    status, checks, _, body, ai_output = report(conn)
    assert (status, json.loads(checks), body, ai_output) == (
        "rejected",
        ["secret_detected"],
        None,
        None,
    )
    assert rows(conn) == []
    assert token not in json.dumps(week.validation())


def test_body_too_long_is_rejected_but_burns_the_theme_numbers(week, conn, monkeypatch):
    monkeypatch.setattr(weekly_verify, "body_too_long", lambda body: True)
    result = week.verify(full_output(week))

    assert (result["result"], week.validation()["reason"]) == ("rejected", "body_too_long")
    status, checks, counts, body, _ = report(conn)
    assert (status, json.loads(checks), body) == ("rejected", ["body_too_long"], None)
    assert json.loads(counts)["published"] == 0
    assert len(rows(conn)) == 7
    assert next_theme_id(conn) == "T0003"  # 公開していない週でも、番号は再利用しない
    assert not (week.run_dir / "issue.md").exists()
