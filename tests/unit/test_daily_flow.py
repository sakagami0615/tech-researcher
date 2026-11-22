"""日次の一巡（D-04・D-22、US-E2-06・US-E8-04）。認証情報も実コンテナも使わない。

統括スクリプトから、① 収集 → ② 評価・要約 → ③ 検証・検査（基本の検証・秘匿情報の検査・本文の生成）
までを本物のコードでつなぎ、外との境界だけを偽にする。

- compose の起動：worker の段階の CLI（cli.main）を同じプロセスで動かす
- HTTP：フィードの取得を、最小の RSS（tests/fixtures/minimal-feed.xml）を返す実装にする
- AI：agent/invoke.py の call_agent を、② の入力を受けて構造化出力を返す実装にする
  （製品には偽の AI のモードを作らない）

ジョブは統括スクリプトが fork した子プロセスで動くので、偽の実装が受け取ったものはファイルに残す。
"""

import contextlib
import io
import json
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path

import pytest

from tech_researcher import cli, failures, observe, orchestrator
from tech_researcher.agent import invoke
from tech_researcher.collect.http import FetchError, Response
from tech_researcher.collect.rss import collect_feed
from tech_researcher.db.feed_snapshots import fetch_recorded
from tech_researcher.db.history import featured_urls, shown_urls
from tech_researcher.db.runs import SourceFetch, finish_run, record_source_fetch, start_run
from tech_researcher.db.schema import open_db
from tech_researcher.orchestrator import StageResult
from tech_researcher.periods import JST, daily_target
from tech_researcher.publish.github_issues import IssueLookup, IssueRef, ListedIssue, parse_marker
from tech_researcher.publish.github_write import CreatedIssue
from tech_researcher.publish.post import Poster
from tech_researcher.runfiles import db_path, new_run_id
from tech_researcher.stages.collect import Collectors, run_collect

ROOT = Path(__file__).parents[2]
FEED = (ROOT / "tests" / "fixtures" / "minimal-feed.xml").read_bytes()
EMPTY_FEED = b'<?xml version="1.0"?><rss version="2.0"><channel><title>E</title></channel></rss>'
LIMITS = (ROOT / "config" / "tech-researcher.toml").read_text(encoding="utf-8").split("[limits]")[1]
STARTED = datetime(2026, 10, 24, 6, 0, tzinfo=JST)  # 対象日は 2026-10-23（金）
TOKEN = "ghp" + "_" + "z" * 36  # テスト用のダミー。実行時に組み立てる
ISSUE_TOKEN = "fake-issue-credential-9876"  # Issue 用トークンの代わり（トークンの形ではない）
ISSUE_REPO = "tester/tr-sandbox"


class FakeGitHub:
    """GitHub の代わり。作った Issue をファイルに残し（ジョブは fork した子プロセスで動くため）、
    重複判定の読み取り（番号・ラベル）に答える。"""

    def __init__(self, path: Path):
        self._path = path
        self.errors: list[Exception] = []  # 次の作成で順に投げる（子プロセスに引き継がれる）

    @property
    def created(self) -> list[dict]:
        if not self._path.exists():
            return []
        rows = [json.loads(x) for x in self._path.read_text().splitlines()]
        return [{**r, "labels": tuple(r["labels"])} for r in rows]

    def writer(self, token, limits, clock=None):
        github = self

        class Writer:
            deadline = None

            def create_issue(self, repo, title, body, labels):
                if github.errors:
                    raise github.errors.pop(0)
                number = 100 + len(github.created)
                row = {"repo": repo, "title": title, "body": body, "labels": labels}
                with github._path.open("a") as f:
                    f.write(json.dumps({**row, "number": number}, ensure_ascii=False) + "\n")
                return CreatedIssue(IssueRef(repo, number))

        return Writer()

    def reader(self, token, limits, **kw):
        github = self

        class Reader:
            def get_issue(self, repo, number):
                found = any(c["number"] == number and c["repo"] == repo for c in github.created)
                ref = IssueRef(repo, number) if found else None
                return IssueLookup(ref, status=None if found else 404)

            def list_report_issues(self, repo, cadence):
                for c in github.created:
                    if "type:report" in c["labels"] and f"cadence:{cadence}" in c["labels"]:
                        yield ListedIssue(c["number"], parse_marker(c["body"]))

        return Reader()


def entry(item_id, rank, **kw):
    return {
        "item_ids": [item_id],
        "primary_item_id": item_id,
        "primary_link_id": None,
        "category": "ai",
        "rank": rank,
        "depth": "full",
        "headline": f"見出し{rank}",
        "points": [f"要点{rank}"],
        "importance": f"理由{rank}",
        "period_event": None,
        **kw,
    }


def select_all(data: dict) -> dict:
    """入力の全項目を1件ずつ載せる。候補リンクがあれば一次情報にする。"""
    return {
        "items": [
            entry(
                item["id"],
                rank,
                primary_item_id=None if item["links"] else item["id"],
                primary_link_id=item["links"][0]["id"] if item["links"] else None,
            )
            for rank, item in enumerate(data["items"], start=1)
        ]
    }


def investigation(data: dict) -> dict:
    """④ の出力の例（検査を通る形）。根拠は入力の最初の行。"""
    lines = data["lines"]
    return {
        "event": "処理が失敗した",
        "cause": "原因の推定",
        "confidence": "medium",
        "investigation": ["ログを確認した"],
        "countermeasures": [{"target": "code", "text": "コードを確認する"}],
        "evidence_line_ids": [lines[0]["id"]] if lines else [],
    }


class Host:
    def __init__(self, tmp_path: Path):
        self.repo = tmp_path / "repo"
        self.data = tmp_path / "data"
        self.record = tmp_path / "record"
        self.secrets = tmp_path / "secrets"
        self.environ = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "TR_DATA_DIR": str(self.data),
            "TR_SECRETS_DIR": str(self.secrets),
        }
        # worker の env_file（worker.env）から compose が渡す秘匿値（D-05）の代わり
        self.worker_env = {
            "TR_DATA_DIR": str(self.data),
            "GITHUB_TOKEN": TOKEN,
            "GITHUB_ISSUE_TOKEN": ISSUE_TOKEN,
            "GITHUB_ISSUE_REPO": ISSUE_REPO,
        }
        self.github = FakeGitHub(tmp_path / "github.jsonl")
        self.feed = FEED
        self.ai = select_all
        self.investigator = investigation  # ④ の偽の AI

    def write_config(self, limits: str = LIMITS, kind: str = "new") -> None:
        (self.repo / "config" / "tech-researcher.toml").write_text(
            "[daily]\nperiod_days = 1\n[weekly]\nperiod_days = 7\n"
            '[[feeds]]\nid = "example"\ndisplay_name = "Example"\n'
            f'url = "https://blog.example.com/feed.xml"\nkind = "{kind}"\nenabled = true\n'
            f"[limits]{limits}",
            encoding="utf-8",
        )

    def fake_fetch(self, url, limits):
        assert url == "https://blog.example.com/feed.xml"
        if self.feed is None:  # 再試行を使い切った失敗
            raise FetchError("HTTP 503", 3, 503)
        return Response(url, 200, self.feed, 1)

    def fake_compose_run(self, compose, service, args, timeout_s):
        """`docker compose run worker …` の代わりに、worker の CLI をそのまま動かす。"""
        self._append("compose", {"service": service, "args": list(args)})
        assert service == "worker"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(args, self.worker_env)
        return StageResult(code, out.getvalue(), err.getvalue())

    def fake_call_agent(self, body, timeout_s, **_):
        assert timeout_s > 0  # 段階の上限を渡している
        data = json.loads(body)
        self._append("agent", data)
        if data["task"] == "investigate":
            return invoke.AgentResult(self.investigator(data))
        return invoke.AgentResult(self.ai(data))

    def calls(self, kind: str) -> list:
        path = self.record / f"{kind}.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def _append(self, kind: str, value: object) -> None:
        with (self.record / f"{kind}.jsonl").open("a") as f:
            f.write(json.dumps(value, ensure_ascii=False) + "\n")

    def run(self, *argv: str) -> tuple[int, dict, str]:
        code = orchestrator.main(["daily", *argv], self.environ)
        return code, *self._summary()

    def observe(self) -> tuple[int, dict]:
        code = observe.main([], self.environ)
        return code, self._summary()[0]

    def runs(self) -> list[tuple]:
        with contextlib.closing(open_db(db_path(self.data))) as conn:
            return conn.execute(
                "SELECT run_id, cadence, target, status, failed_stage, error_category,"
                " finished_at IS NOT NULL FROM runs"
            ).fetchall()

    def _summary(self) -> tuple[dict, str]:
        out = self.capfd.readouterr().out
        lines = out.splitlines()
        assert len(lines) == 1, lines  # 標準出力は要約の1行だけ
        return json.loads(lines[0]), out


@pytest.fixture
def host(tmp_path, monkeypatch, capfd) -> Host:
    h = Host(tmp_path)
    h.capfd = capfd
    (h.repo / "config").mkdir(parents=True)
    h.write_config()
    h.data.mkdir()
    h.record.mkdir()
    h.secrets.mkdir(mode=0o700)
    (h.secrets / "worker.env").write_text(f"GITHUB_TOKEN={TOKEN}\n")
    (h.secrets / "worker.env").chmod(0o600)
    (h.secrets / "agent.env").write_text("CLAUDE_CODE_OAUTH_TOKEN=\n")
    (h.secrets / "agent.env").chmod(0o600)
    monkeypatch.chdir(h.repo)
    monkeypatch.setattr(
        orchestrator, "new_run_id", lambda now, **kwargs: new_run_id(STARTED, **kwargs)
    )
    monkeypatch.setattr(orchestrator.Compose, "run", lambda c, *a: h.fake_compose_run(c, *a))
    monkeypatch.setattr(invoke, "call_agent", h.fake_call_agent)
    monkeypatch.setattr(
        cli, "Poster", partial(Poster, make_writer=h.github.writer, make_reader=h.github.reader)
    )
    collect = partial(collect_feed, fetch_url=lambda url, limits, **_: h.fake_fetch(url, limits))
    collectors = Collectors(feed=collect)
    monkeypatch.setattr(cli, "run_collect", partial(run_collect, collectors=collectors))
    return h


def stages_of(log_path: Path) -> list[tuple[str, str]]:
    return [(r["stage"], r["event"]) for r in map(json.loads, log_path.read_text().splitlines())]


def assert_secret_stop(out_dir: Path) -> None:
    """秘匿情報を検出した実行は、公開停止の定型（R 3.6）だけを残す。値も検出の内容も入れない。"""
    issue = (out_dir / "issue.md").read_text(encoding="utf-8")
    assert "（公開停止）" in issue.splitlines()[0]
    assert "公開を停止しました" in issue and "調査結果" not in issue
    assert "Pw0rd" not in issue and TOKEN not in issue and "sk-ant-" not in issue


def test_dry_run_goes_round_and_keeps_results(host):
    code, summary, out = host.run("--dry-run")

    assert code == 0
    run_id = summary["run_id"]
    out_dir = host.data / "dry-run" / run_id
    assert summary == {
        "run_id": run_id,
        "cadence": "daily",
        "target": "2026-10-23",
        "result": "dry_run",
        "failed_stage": None,
        "error_category": None,
        "items": 2,
        "failed_sources": [],
        "partial_sources": [],
        "output_dir": str(out_dir),
        "issue_url": None,
    }
    assert "モデル" not in out  # 収集した文は標準出力に出さない
    assert [c["args"][0] for c in host.calls("compose")] == ["collect", "verify"]  # ⑤ は呼ばない
    assert {p.name for p in out_dir.iterdir()} == {
        "summary.json",
        "collected.json",
        "agent-input.json",
        "agent-output.json",
        "validation.json",
        "secret-scan.json",
        "unjudged.json",
        "unavailable.json",
        "fetches.json",
        "item-ids.json",
        "issue.md",
        "log.jsonl",
    }
    # ② には仮の ID と、untrusted に入れた外部の文を渡した
    [data] = host.calls("agent")
    assert [i["id"] for i in data["items"]] == ["d1", "d2"]
    assert data["items"][0]["untrusted"]["title"] == "新しいモデルの公開"
    assert data["items"][0]["links"] == [
        {"id": "d1-l1", "url": "https://docs.example.com/model", "domain": "docs.example.com"}
    ]
    assert json.loads((out_dir / "agent-output.json").read_text()) == select_all(data)

    issue = (out_dir / "issue.md").read_text(encoding="utf-8")
    title, labels, _, separator, _, identifier, *_ = issue.splitlines()
    assert title == "[日次] 2026-10-23（金）の技術動向"
    assert labels == "type:report, cadence:daily"
    assert separator == "---"
    assert identifier == (
        f"<!-- tech-researcher kind=report cadence=daily target=2026-10-23 run={run_id} -->"
    )
    assert "> **掲載**：2 件" in issue
    assert "#### 1. 見出し1" in issue and "#### 2. 見出し2" in issue
    assert (
        "一次情報 [docs.example.com](https://docs.example.com/model) ／ "
        "[Example（blog.example.com）](https://blog.example.com/model) 公開 2026-10-23"
    ) in issue
    assert "新しいモデルの公開" not in issue  # 外部の記事のタイトルは本文に使わない

    assert json.loads((out_dir / "validation.json").read_text()) == {
        "result": "report",
        "reason": None,
        "entries": 2,
        "excluded": [],
        "counts": {
            "collected": 2,
            "ai_output": 2,
            "excluded": 0,
            "excluded_by_reason": {},
            "published": 2,
        },
        "verification": {
            "invalid_output": "passed",
            "invalid_id": "passed",
            "invalid_source": "passed",
            "missing_period_event": "passed",
            "forbidden_link": "passed",
            "forbidden_html": "passed",
            "too_long": "passed",
            "long_quote": "passed",
            "duplicate_id": "passed",
            "secret_detected": "passed",
        },
        "rejected_checks": [],
    }
    assert json.loads((out_dir / "secret-scan.json").read_text()) == {
        "detected": False,
        "kinds": [],
    }
    assert stages_of(out_dir / "log.jsonl") == [
        ("job", "start"),
        ("collect", "start"),
        ("collect", "source_result"),
        ("collect", "end"),
        ("analyze", "start"),
        ("analyze", "usage"),
        ("analyze", "end"),
        ("verify", "start"),
        ("verify", "end"),
        ("job", "end"),
    ]
    assert not (host.data / "db").exists()  # DB を作らない（D-22）
    assert not (host.data / "work").exists()


def test_zero_items_is_a_report_saying_so(host):
    """正常に0件だった日も、該当なしと書いたレポートになる（US-E2-06）。

    AI は呼ばず、コードが項目のない ② の出力を作り、③ はほかの日と同じように検証する（R 1.5）。
    """
    host.feed = EMPTY_FEED
    code, summary, _ = host.run("--dry-run")

    assert code == 0
    assert (summary["result"], summary["items"]) == ("dry_run", 0)
    assert host.calls("agent") == []
    assert [c["args"][0] for c in host.calls("compose")] == ["collect", "verify"]
    out_dir = Path(summary["output_dir"])
    assert json.loads((out_dir / "agent-output.json").read_text()) == {"items": []}
    assert json.loads((out_dir / "validation.json").read_text())["result"] == "report"
    assert ("analyze", "end") in stages_of(out_dir / "log.jsonl")
    issue = (out_dir / "issue.md").read_text(encoding="utf-8")
    assert "> **掲載**：0 件" in issue
    assert "**該当なし**：" in issue and "（収集 0 件）" in issue
    assert "## 目次" not in issue


def test_unjudged_popular_feed_is_noted_in_the_header(host):
    """DB のない開発機のドライランは、前日の記録がない日として、人気型フィードの判定をしない。"""
    host.write_config(kind="popular")
    code, summary, _ = host.run("--dry-run")

    assert code == 0
    issue = (Path(summary["output_dir"]) / "issue.md").read_text(encoding="utf-8")
    assert (
        "> ℹ️ 人気型フィードへの初登場（Example）は、前日の記録がないため判定していません。" in issue
    )


def test_ai_choosing_nothing_is_a_report_saying_so(host):
    """AI が1件も選ばなかった日は、除外で0件（不合格）と違い、該当なしのレポートになる。"""
    host.ai = lambda data: {"items": []}
    code, summary, _ = host.run("--dry-run")

    assert code == 0
    assert summary["result"] == "dry_run"
    assert len(host.calls("agent")) == 1
    issue = (Path(summary["output_dir"]) / "issue.md").read_text(encoding="utf-8")
    assert "**該当なし**：" in issue and "（収集 2 件）" in issue


def test_production_posts_the_report_and_closes_the_run(host):
    code, summary, _ = host.run()

    assert code == 0
    run_id = summary["run_id"]
    assert not run_id.startswith("dry-")
    assert (summary["result"], summary["failed_stage"], summary["error_category"]) == (
        "report",
        None,
        None,
    )
    assert summary["issue_url"] == f"https://github.com/{ISSUE_REPO}/issues/100"
    assert [c["args"][0] for c in host.calls("compose")] == ["collect", "verify", "post"]
    assert [p.name for p in (host.data / "work").iterdir()] == [".lock"]  # 実行中のファイルは消した
    assert not (host.data / "dry-run").exists()
    # ラベルは作成の要求に含め、本文の1行目は識別子（重複判定が読む）
    [created] = host.github.created
    assert created["labels"] == ("type:report", "cadence:daily")
    assert created["title"] == "[日次] 2026-10-23（金）の技術動向"
    assert created["body"].startswith(
        f"<!-- tech-researcher kind=report cadence=daily target=2026-10-23 run={run_id} -->"
    )
    with contextlib.closing(open_db(db_path(host.data))) as conn:
        assert conn.execute(
            "SELECT run_id, status, finished_at IS NOT NULL FROM runs"
        ).fetchall() == [(run_id, "succeeded", 1)]
        assert conn.execute(
            "SELECT run_id, kind, cadence, target, state, repo, number, projects_state FROM issues"
        ).fetchall() == [
            (run_id, "report", "daily", "2026-10-23", "created", ISSUE_REPO, 100, "pending")
        ]
        assert conn.execute("SELECT key, value FROM meta").fetchall() == [
            ("issue_repo", ISSUE_REPO)
        ]
        assert conn.execute("SELECT source_id, status FROM source_fetches").fetchall() == [
            ("rss:example", "ok")
        ]
        (count,) = conn.execute("SELECT count(*) FROM items").fetchone()
        assert count == summary["items"] > 0
    log = host.data / "logs" / "2026" / "10" / f"{run_id}.jsonl"
    assert ("verify", "end") in stages_of(log)
    assert ("post", "end") in stages_of(log)


def test_second_run_for_the_same_target_does_not_create_a_second_issue(host, monkeypatch):
    """同じ対象の再実行は、重複判定（DB の番号）で作らない。runs は閉じる（M 3.7）。"""
    host.run()
    host.capfd.readouterr()
    monkeypatch.setattr(
        orchestrator,
        "new_run_id",
        lambda now, **kwargs: new_run_id(STARTED + timedelta(hours=1), **kwargs),
    )
    code, summary, _ = host.run()

    assert (code, summary["result"]) == (0, "report")
    assert summary["issue_url"] == f"https://github.com/{ISSUE_REPO}/issues/100"
    assert len(host.github.created) == 1
    assert [r[3] for r in host.runs()] == ["succeeded", "succeeded"]


def test_post_failure_records_failed_and_closes_run_as_failed_post(host):
    from tech_researcher.publish.github_write import IssueWriteError

    host.github.errors = [IssueWriteError("x", "http_4xx", 422)]
    code, summary, _ = host.run()

    assert code == 1
    assert (summary["result"], summary["failed_stage"], summary["error_category"]) == (
        "error",
        "post",
        "http_4xx",
    )
    [run] = host.runs()
    assert run[3:] == ("failed", "post", "http_4xx", 1)
    with contextlib.closing(open_db(db_path(host.data))) as conn:
        assert conn.execute("SELECT kind, state, number FROM issues").fetchall() == [
            ("report", "failed", None)
        ]


def test_invalid_id_is_excluded_and_others_are_reported(host):
    host.ai = lambda data: {"items": [entry("d1", 1), entry("d9", 2)]}
    code, summary, _ = host.run("--dry-run")

    assert code == 0
    out_dir = Path(summary["output_dir"])
    validation = json.loads((out_dir / "validation.json").read_text())
    assert validation["entries"] == 1
    assert validation["excluded"] == [{"item_ids": ["d9"], "reason": "入力にない ID ['d9']"}]
    issue = (out_dir / "issue.md").read_text(encoding="utf-8")
    assert "#### 1. 見出し1" in issue and "見出し2" not in issue  # 除外した項目は書かない
    assert ("verify", "excluded") in stages_of(out_dir / "log.jsonl")


def test_missing_period_event_is_excluded_and_logged(host, monkeypatch):
    """期間より前に公開された項目は period_event が必須。欠けた項目だけを除外する。"""
    monkeypatch.setattr("tech_researcher.agent.daily.published_before_period", lambda i, t: True)
    host.ai = lambda data: {
        "items": [entry("d1", 1, period_event="Star が急増した"), entry("d2", 2)]
    }
    code, summary, _ = host.run("--dry-run")

    assert code == 0
    out_dir = Path(summary["output_dir"])
    validation = json.loads((out_dir / "validation.json").read_text())
    assert validation["entries"] == 1 and [x["item_ids"] for x in validation["excluded"]] == [
        ["d2"]
    ]
    records = [json.loads(x) for x in (out_dir / "log.jsonl").read_text().splitlines()]
    assert [r["error_category"] for r in records if r["event"] == "excluded"] == [
        "missing_period_event"
    ]


@pytest.mark.parametrize(
    ("override", "category"),
    [
        ({"importance": "詳しくは https://example.net/x"}, "forbidden_link"),
        ({"headline": "<!-- 隠す -->見出し"}, "forbidden_html"),
        ({"points": ["要"] * 7}, "too_long"),
    ],
)
def test_text_violations_are_excluded_and_logged(host, override, category):
    """D2・D3・長さの検査が ③ につながっている。違反した項目だけを除外する。"""
    host.ai = lambda data: {"items": [entry("d1", 1, **override), entry("d2", 2)]}
    code, summary, _ = host.run("--dry-run")

    assert code == 0
    out_dir = Path(summary["output_dir"])
    validation = json.loads((out_dir / "validation.json").read_text())
    assert validation["entries"] == 1
    assert [x["item_ids"] for x in validation["excluded"]] == [["d1"]]
    records = [json.loads(x) for x in (out_dir / "log.jsonl").read_text().splitlines()]
    assert [r["error_category"] for r in records if r["event"] == "excluded"] == [category]


def test_unreadable_source_url_is_excluded(host, monkeypatch):
    """出典の URL を許可集合と照らす検査（D1）が、③ につながっている。"""
    monkeypatch.setattr("tech_researcher.validate.sources.normalize_url", lambda u: None)
    host.ai = lambda data: {"items": [entry("d1", 1)]}
    code, summary, _ = host.run("--dry-run")

    assert code == 1 and summary["result"] == "rejected"
    out_dir = Path(summary["output_dir"])
    assert json.loads((out_dir / "validation.json").read_text())["reason"] == "all_excluded"


@pytest.mark.parametrize(
    ("output", "reason"),
    [
        ({"items": [entry("d9", 1)]}, "all_excluded"),
        ({"items": "x"}, "invalid_output"),
        ({"items": [entry("d1", 1, category="security")]}, "invalid_output"),
        (["not", "object"], "invalid_output"),  # 形の誤りは D6(a) で全体を不合格（D-07）
        ({"items": [entry("d1", 1, url="https://evil.example")]}, "invalid_output"),
        ({"items": [{k: v for k, v in entry("d1", 1).items() if k != "depth"}]}, "invalid_output"),
        (None, "invalid_output"),  # 出力がない場合も違反
    ],
)
def test_rejected_output_makes_no_report(host, output, reason):
    host.ai = lambda data: output
    code, summary, _ = host.run("--dry-run")

    assert code == 1
    assert (summary["result"], summary["failed_stage"]) == ("rejected", None)
    out_dir = Path(summary["output_dir"])
    assert json.loads((out_dir / "validation.json").read_text())["reason"] == reason
    # 不合格 Issue の本文を残す。AI の出力の全文も、除外した項目の中身も入れない（R 3.2）
    issue = (out_dir / "issue.md").read_text(encoding="utf-8")
    assert issue.startswith("[不合格][日次] 2026-10-23（金）のレポート（2026-10-24 06:00 実行）\n")
    assert "type:rejected, cadence:daily" in issue
    assert "## 調査結果" in issue and "検証結果の詳細" in issue
    assert "見出し1" not in issue
    # 不合格の後に ② を呼び直さず（再生成しない）、④ を1回だけ呼び、結果を残す
    assert [c["task"] for c in host.calls("agent")] == ["daily", "investigate"]
    assert json.loads((out_dir / "investigation.json").read_text())["confidence"] == "medium"


def test_secret_in_report_is_not_published(host):
    """本文に秘匿情報があれば、本文を残さず実行エラーにする（US-E7-05）。

    ドライランの agent-output.json・collected.json は、AI の出力と外部の文をそのまま持つ
    （structure.md 7.2。運用者が確かめるためのファイルで、公開しない）。ここでは確かめない。
    """
    host.ai = lambda data: {"items": [entry("d1", 1, importance=f"値は {TOKEN}")]}
    code, summary, out = host.run("--dry-run")

    assert code == 1
    assert (summary["result"], summary["failed_stage"], summary["error_category"]) == (
        "error",
        "verify",
        "secret_detected",
    )
    out_dir = Path(summary["output_dir"])
    # 公開停止の定型だけを残す（3.6）。④ は呼ばず、調査結果・実行 ID・日時を載せない
    issue = (out_dir / "issue.md").read_text(encoding="utf-8")
    assert issue.startswith("[実行エラー][日次] 2026-10-23（金）（公開停止）\n")
    assert "秘匿情報の可能性がある文字列を検出したため、Issue の公開を停止しました。" in issue
    assert "調査結果" not in issue and summary["run_id"] not in issue and TOKEN not in issue
    assert [c["task"] for c in host.calls("agent")] == ["daily"]
    assert not (out_dir / "investigation.json").exists()
    assert json.loads((out_dir / "secret-scan.json").read_text()) == {
        "detected": True,
        "kinds": ["known"],
    }
    assert TOKEN not in out
    assert TOKEN not in (out_dir / "log.jsonl").read_text()
    assert TOKEN not in (out_dir / "validation.json").read_text()


def test_too_large_input_does_not_call_ai(host):
    host.write_config(LIMITS.replace("agent_input_bytes = 2_000_000", "agent_input_bytes = 100"))
    code, summary, _ = host.run("--dry-run")

    assert code == 1
    assert (summary["failed_stage"], summary["error_category"]) == ("analyze", "input_too_large")
    # ② は呼ばない。claude を呼ばずに止めた失敗なので ④ は呼べる（調査結果を載せる）
    assert [c["task"] for c in host.calls("agent")] == ["investigate"]
    assert [c["args"][0] for c in host.calls("compose")] == ["collect"]
    issue = (Path(summary["output_dir"]) / "issue.md").read_text(encoding="utf-8")
    assert "AI への入力が上限を超えた" in issue and "## 調査結果" in issue


def test_ai_failure_stops_before_verify(host):
    def broken(data):
        raise RuntimeError(f"外部の文 {TOKEN}")  # 例外の中身は要約・ログに出さない

    host.ai = broken
    code, summary, out = host.run("--dry-run")

    assert code == 1
    assert "外部の文" not in out and TOKEN not in out
    assert TOKEN not in (Path(summary["output_dir"]) / "log.jsonl").read_text(encoding="utf-8")
    assert (summary["failed_stage"], summary["error_category"]) == ("analyze", "agent_failed")
    assert [c["args"][0] for c in host.calls("compose")] == ["collect"]
    assert not (Path(summary["output_dir"]) / "agent-output.json").exists()
    # 想定外の失敗でも、1回の呼び出しの記録（値は unknown）を残す
    log = [
        json.loads(x) for x in (Path(summary["output_dir"]) / "log.jsonl").read_text().splitlines()
    ]
    [usage] = [r for r in log if r["event"] == "usage" and r["stage"] == "analyze"]
    assert usage["detail"].startswith("model=unknown input_bytes=")
    assert "num_turns=unknown" in usage["detail"]


@pytest.mark.parametrize("feed", [FEED, EMPTY_FEED])
def test_output_write_failure_is_analyze_failure(host, monkeypatch, feed):
    """② の出力を書けないときは、AI を呼んだ日も0件の日も ② の失敗にする。"""

    def unwritable(job, output):
        raise OSError("ディスクがいっぱい")

    monkeypatch.setattr(orchestrator, "_write_output", unwritable)
    host.feed = feed
    code, summary, _ = host.run("--dry-run")

    assert code == 1
    assert (summary["failed_stage"], summary["error_category"]) == (
        "analyze",
        "output_write_failed",
    )
    assert [c["args"][0] for c in host.calls("compose")] == ["collect"]


def test_all_sources_failed_is_error_not_empty_report(host):
    """取得の失敗は、正常の0件（該当なしのレポート）と区別する（US-E6-03・US-E6-04）。"""
    host.feed = None
    code, summary, _ = host.run("--dry-run")

    assert code == 1
    assert (summary["result"], summary["failed_stage"], summary["error_category"]) == (
        "error",
        "collect",
        "all_sources_failed",
    )
    assert summary["failed_sources"] == ["Example"]
    assert [c["task"] for c in host.calls("agent")] == ["investigate"]  # ② は呼ばない
    out_dir = Path(summary["output_dir"])
    assert not (out_dir / "agent-input.json").exists()  # ② に進まない
    # 実行エラー Issue の本文（AI の調査あり）。取得結果の表に、失敗した情報源と理由を出す
    issue = (out_dir / "issue.md").read_text(encoding="utf-8")
    assert issue.startswith(
        "[実行エラー][日次] 2026-10-23（金）（① 収集・2026-10-24 06:00 実行）\n"
    )
    assert "| エラーの概要 | すべての情報源の取得に失敗した（HTTP 5xx） |" in issue
    assert "| Example | 失敗 | HTTP 5xx | 2 回 |" in issue
    assert "## 調査結果" in issue and (out_dir / "investigation.json").exists()
    assert "collect failure error_category=all_sources_failed" in (
        json.dumps(host.calls("agent")[0]["lines"], ensure_ascii=False)
    )


def test_production_collect_failure_closes_run_as_failed(host, monkeypatch):
    """① で実行エラーと分かった本番の実行は、running のまま残さず failed で閉じる（M 3.4）。"""
    host.feed = None
    called = []
    monkeypatch.setattr(cli, "write_daily_input", lambda *a: called.append(a))
    code, summary, _ = host.run()
    assert (code, summary["error_category"]) == (1, "all_sources_failed")
    assert called == []  # ② に進まないので、② の入力も作らない
    [run] = host.runs()
    assert run[0] == summary["run_id"]
    assert run[3:] == ("failed", "collect", "all_sources_failed", 1)


def test_production_collect_exception_closes_run_as_failed(host, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("想定外")

    monkeypatch.setattr(cli, "run_collect", broken)
    code, summary, _ = host.run()
    assert (code, summary["failed_stage"], summary["error_category"]) == (1, "collect", "exit_1")
    [run] = host.runs()
    assert run[3:] == ("failed", "collect", "RuntimeError", 1)


def test_production_daily_input_failure_closes_run_as_failed(host, monkeypatch):
    """① の続き（② の入力を書く）の失敗も ① の失敗として閉じる。"""

    def broken(*args, **kwargs):
        raise OSError("書けない")

    monkeypatch.setattr(cli, "write_daily_input", broken)
    code, summary, _ = host.run()
    assert (code, summary["failed_stage"]) == (1, "collect")
    [run] = host.runs()
    assert run[3:] == ("failed", "collect", "OSError", 1)


def test_dry_run_does_not_change_existing_db(host):
    """ドライランは本番の DB を読み取り専用で開き、何も書かない（D-22）。"""
    with contextlib.closing(open_db(db_path(host.data))) as conn:
        before = list(conn.iterdump())

    code, _, _ = host.run("--dry-run")

    assert code == 0
    with contextlib.closing(open_db(db_path(host.data))) as conn:
        assert list(conn.iterdump()) == before


def test_verify_checks_worker_data_not_the_copy_given_to_ai(host):
    """③ は agent に渡したコピーではなく collected.json と照合する（D-04）。"""

    def tamper(data):
        # ② の入力のファイルに、収集していない項目を足す（コピーが書き換えられた場合）
        [path] = (host.data / "dry-run").glob("*/agent-input.json")
        forged = {**data, "items": [*data["items"], {**data["items"][0], "id": "d3"}]}
        path.write_text(json.dumps(forged), encoding="utf-8")
        return {"items": [entry("d1", 1), entry("d3", 2)]}

    host.ai = tamper
    code, summary, _ = host.run("--dry-run")

    assert code == 0
    validation = json.loads((Path(summary["output_dir"]) / "validation.json").read_text())
    assert validation["entries"] == 1
    assert [x["item_ids"] for x in validation["excluded"]] == [["d3"]]


@pytest.mark.parametrize(
    "secret", ["Pw0rd*~x_y", "sk-ant-oat01-_" + "A" * 90], ids=["known", "anthropic_key"]
)
def test_secret_in_raw_ai_output_stops_the_report(host, secret):
    """無害化でエスケープされる記号を含む値も、AI の出力の全文を検査して見つける（US-E7-05）。

    本文では `\\*`・`\\_` に変わって値が割れるので、本文の検査だけでは見逃す。
    """
    host.worker_env["API_SECRET"] = "Pw0rd*~x_y"
    host.ai = lambda data: {
        "items": [entry("d1", 1), entry("d2", 2, points=[f"値は {secret} です"], depth="brief")]
    }
    code, summary, out = host.run("--dry-run")

    assert code == 1
    assert summary["error_category"] == "secret_detected"
    out_dir = Path(summary["output_dir"])
    assert_secret_stop(out_dir)
    assert json.loads((out_dir / "secret-scan.json").read_text())["detected"] is True
    assert secret not in out
    assert secret not in (out_dir / "log.jsonl").read_text()


def test_double_escaped_secret_in_ai_string_is_detected(host):
    """AI が値を2回エスケープして書いても、文字列の値を1つずつ検査して見つける。"""
    secret = 'Pa"x0rd\\z'
    host.worker_env["API_SECRET"] = secret
    twice = json.dumps(json.dumps(secret)[1:-1])[1:-1]
    host.ai = lambda data: {"items": [entry("d1", 1, points=[f"値は {twice} です"])]}
    code, summary, out = host.run("--dry-run")

    assert code == 1 and summary["error_category"] == "secret_detected"
    assert_secret_stop(Path(summary["output_dir"]))


def test_secret_in_ai_ids_is_masked_in_results(host):
    """AI の出力の全文に秘匿情報があれば、ID の中でも全体を止める。結果のファイルには残さない。"""
    host.ai = lambda data: {"items": [entry("d1", 1), entry(TOKEN, 2)]}
    code, summary, out = host.run("--dry-run")

    assert code == 1 and summary["error_category"] == "secret_detected"
    out_dir = Path(summary["output_dir"])
    assert_secret_stop(out_dir)
    assert TOKEN not in out
    for name in ("validation.json", "log.jsonl"):
        assert TOKEN not in (out_dir / name).read_text(encoding="utf-8"), name


# D-13 の観測（observe.py）。bin/tr-run を使わず、日次と同じロックで ① だけを動かす


def test_observation_collects_only_and_closes_run_as_observed(host):
    code, summary = host.observe()

    assert code == 0
    run_id = summary["run_id"]
    assert run_id.startswith("obs-20261024-060000-")
    assert (summary["result"], summary["items"], summary["failed_sources"]) == ("observed", 2, [])
    assert summary["output_dir"] is None
    assert [c["args"] for c in host.calls("compose")] == [["collect", "daily", "--run-id", run_id]]
    assert host.calls("agent") == []  # ② 以降は動かさない
    assert [p.name for p in (host.data / "work").iterdir()] == [".lock"]  # 実行中のファイルは消した
    # env_file の写しも消した
    assert sorted(p.name for p in host.secrets.iterdir()) == ["agent.env", "worker.env"]
    assert host.runs() == [(run_id, "daily", "2026-10-23", "observed", None, None, 1)]
    with contextlib.closing(open_db(db_path(host.data))) as conn:
        assert conn.execute("SELECT source_id, status FROM source_fetches").fetchall() == [
            ("rss:example", "ok")
        ]
        assert conn.execute("SELECT count(*) FROM items").fetchone() == (2,)
    log = host.data / "logs" / "2026" / "10" / f"{run_id}.jsonl"
    assert stages_of(log) == [
        ("job", "start"),
        ("collect", "start"),
        ("collect", "source_result"),
        ("collect", "end"),
        ("job", "end"),
    ]


def test_observation_with_all_sources_failed_is_recorded_as_failed(host):
    """取得の失敗も履歴に残し、1つも取得できなかった日は failed で閉じる。"""
    host.feed = None
    code, summary = host.observe()

    assert code == 1
    assert (summary["result"], summary["failed_stage"], summary["error_category"]) == (
        "error",
        "collect",
        "all_sources_failed",
    )
    assert summary["failed_sources"] == ["Example"]
    [run] = host.runs()
    assert run[3:] == ("failed", "collect", "all_sources_failed", 1)
    with contextlib.closing(open_db(db_path(host.data))) as conn:
        assert conn.execute(
            "SELECT source_id, status, error_category, attempts FROM source_fetches"
        ).fetchall() == [("rss:example", "failed", "http_5xx", 3)]


def test_observation_failure_in_collect_closes_run_as_failed(host, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError(f"外部の文 {TOKEN}")

    monkeypatch.setattr(cli, "run_collect", broken)
    code = observe.main([], host.environ)
    out, err = host.capfd.readouterr()
    summary = json.loads(out)

    assert code == 1
    assert (summary["result"], summary["failed_stage"], summary["error_category"]) == (
        "error",
        "collect",
        "exit_1",
    )
    [run] = host.runs()
    assert run[3:] == ("failed", "collect", "RuntimeError", 1)
    # 例外の中身（外部由来の文・秘匿値）は標準出力・標準エラー・ログに出さない
    log = (host.data / "logs" / "2026" / "10" / f"{summary['run_id']}.jsonl").read_text()
    for text in (out, err, log):
        assert TOKEN not in text
    assert "外部の文" not in out + err


def test_observation_uses_the_daily_lock(host):
    """観測と日次を同時に動かさない（同じロック）。"""
    with orchestrator.lock(host.data / "locks" / "daily.lock"):
        assert observe.main([], host.environ) == orchestrator.EXIT_BUSY
    assert host.calls("compose") == []
    assert "日次か観測" in host.capfd.readouterr().err


def test_observation_startup_error_names_its_entry(host, capfd):
    environ = {k: v for k, v in host.environ.items() if k != "TR_DATA_DIR"}
    assert observe.main([], environ) == 2
    assert capfd.readouterr().err.startswith("tr-observe: ")


def test_observation_takes_no_arguments(host):
    with pytest.raises(SystemExit) as e:
        observe.main(["--dry-run"], host.environ)
    assert e.value.code == 2


def test_observation_counts_as_previous_day_record_for_feeds(host):
    """観測の取得も、翌日の人気型フィードの初登場の判定で前日の記録に数える（M 3.4）。"""
    assert host.observe()[0] == 0
    with contextlib.closing(open_db(db_path(host.data))) as conn:
        assert fetch_recorded(conn, "rss:example", datetime(2026, 10, 23).date())


def test_long_quote_is_excluded_by_the_configured_limit(host):
    host.write_config(LIMITS + "\n[quote]\nmax_en = 5\n")  # しきい値を設定で下げる（D4）
    host.ai = lambda data: {
        "items": [entry("d1", 1, headline="新しいモデルの公開"), entry("d2", 2)]
    }
    code, summary, _ = host.run("--dry-run")

    assert code == 0
    out_dir = Path(summary["output_dir"])
    validation = json.loads((out_dir / "validation.json").read_text())
    assert validation["entries"] == 1
    assert [x["item_ids"] for x in validation["excluded"]] == [["d1"]]
    assert "上限 5" in validation["excluded"][0]["reason"]
    assert ("verify", "excluded") in stages_of(out_dir / "log.jsonl")


# --- ② を製品の call_agent でつなぐ（偽の agent コンテナが Claude Code の結果の JSON を返す） ---

PRODUCT_CALL_AGENT = invoke.call_agent
SUCCESS = json.loads((ROOT / "tests" / "fixtures" / "agent" / "daily-success.json").read_text())
MODEL = "claude-opus-4-5-20251101"


@pytest.fixture
def agent(host, monkeypatch):
    """call_agent は製品のまま。agent のサービスだけを偽にし、受け取った引数を残す。"""
    monkeypatch.setattr(invoke, "call_agent", PRODUCT_CALL_AGENT)
    host.model = MODEL

    def run(compose, service, args, timeout_s, stdin=None):
        if service == "worker":
            return host.fake_compose_run(compose, service, args, timeout_s)
        host._append("agent-args", list(args))
        data = json.loads(stdin)
        result = SUCCESS | {
            "modelUsage": {host.model: SUCCESS["modelUsage"][MODEL]} if host.model else {},
            "structured_output": host.ai(data),
        }
        return StageResult(0, json.dumps(result, ensure_ascii=False), "")

    monkeypatch.setattr(orchestrator.Compose, "run", run)
    return host


def models_of(host) -> list[tuple]:
    with contextlib.closing(open_db(db_path(host.data))) as conn:
        return conn.execute("SELECT run_id, model FROM runs ORDER BY rowid").fetchall()


def log_of(host, run_id: str) -> list[dict]:
    path = host.data / "logs" / "2026" / "10" / f"{run_id}.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()]


def test_product_call_agent_uses_the_skill_and_schema(agent):
    code, summary, _ = agent.run("--dry-run")

    assert (code, summary["result"]) == (0, "dry_run")
    [args] = agent.calls("agent-args")
    assert args[:4] == ["agent-run", "tr-daily-evaluate", "-p", "/tr-daily-evaluate"]
    # 製品のスキーマを1行にして渡す
    schema = json.loads(args[args.index("--json-schema") + 1])
    assert schema == json.loads(orchestrator.DAILY_SCHEMA.read_text(encoding="utf-8"))
    out_dir = Path(summary["output_dir"])
    assert json.loads((out_dir / "validation.json").read_text())["entries"] == 2
    log = [json.loads(x) for x in (out_dir / "log.jsonl").read_text().splitlines()]
    [usage] = [r for r in log if r["event"] == "usage"]
    assert usage["detail"].startswith(f"model={MODEL} input_bytes=")
    assert "duration_ms=84210 " in usage["detail"]
    assert "output_tokens=2310 " in usage["detail"]
    assert not (agent.data / "db").exists()  # ドライランはモデルを DB に書かない


def test_production_records_model_and_warns_when_it_changes(agent):
    code, first, _ = agent.run()
    assert (code, first["failed_stage"]) == (0, None)
    assert models_of(agent) == [(first["run_id"], MODEL)]
    assert not [r for r in log_of(agent, first["run_id"]) if r["event"] == "warning"]

    _, second, _ = agent.run()  # 同じモデル
    assert not [r for r in log_of(agent, second["run_id"]) if r["event"] == "warning"]
    agent.model = "claude-opus-5-20261101"
    _, third, _ = agent.run()
    assert [m for _, m in models_of(agent)] == [MODEL, MODEL, "claude-opus-5-20261101"]
    [warning] = [r for r in log_of(agent, third["run_id"]) if r["event"] == "warning"]
    assert (warning["stage"], warning["error_category"]) == ("analyze", "model_changed")
    assert f"{MODEL} から claude-opus-5-20261101" in warning["detail"]


def test_unknown_model_in_production_is_warned(agent):
    agent.model = None  # 結果の JSON に modelUsage がない
    _, summary, _ = agent.run()
    assert models_of(agent) == [(summary["run_id"], None)]
    warnings = [r for r in log_of(agent, summary["run_id"]) if r["event"] == "warning"]
    assert [r["error_category"] for r in warnings] == ["model_unknown"]


def test_model_record_failure_does_not_stop_verify(agent, monkeypatch):
    def broken(*args, **kwargs):
        raise LookupError("runs に開始の記録がない")

    monkeypatch.setattr(cli, "record_model", broken)
    code, summary, _ = agent.run()
    assert (code, summary["failed_stage"]) == (0, None)  # ③ の後の ⑤ まで進んだ
    log = log_of(agent, summary["run_id"])
    assert ("analyze", "warning", "model_record_failed") in [
        (r["stage"], r["event"], r.get("error_category")) for r in log
    ]
    assert ("verify", "end") in [(r["stage"], r["event"]) for r in log]


def test_dry_run_does_not_record_or_compare_models(agent):
    agent.run()
    agent.model = "claude-opus-5-20261101"
    _, summary, _ = agent.run("--dry-run")
    log = Path(summary["output_dir"]) / "log.jsonl"
    assert ("analyze", "warning") not in stages_of(log)
    assert [m for _, m in models_of(agent)] == [MODEL]


def test_too_large_input_file_is_not_read(host, monkeypatch):
    """② の入力のファイルが上限を超えていれば、読み込まずに止める。"""
    host.write_config(LIMITS.replace("agent_input_bytes = 2_000_000", "agent_input_bytes = 100"))
    real_read = Path.read_text

    def read_text(path, *args, **kwargs):
        assert path.name != "agent-input.json", "上限を超えた入力を読んだ"
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    code, summary, _ = host.run("--dry-run")
    assert (code, summary["error_category"]) == (1, "input_too_large")
    assert [c["task"] for c in host.calls("agent")] == ["investigate"]  # ② は呼ばない


def saved_report(host):
    with contextlib.closing(open_db(db_path(host.data))) as conn:
        report = conn.execute(
            "SELECT run_id, status, rejected_checks, counts_json, verification_json,"
            " ai_output_json, body_markdown FROM reports"
        ).fetchall()
        entries = conn.execute(
            "SELECT entry_no, status, exclusion_reasons, display_no, item_ids_json, payload_json"
            " FROM report_entries ORDER BY entry_no"
        ).fetchall()
        items = conn.execute("SELECT item_id FROM items ORDER BY item_id").fetchall()
    return report, entries, items


def test_production_saves_the_report_with_item_ids_from_items(host):
    """② ③ の項目の ID は items の主キー（i…）。結果は reports・report_entries に残る。"""
    host.ai = lambda data: {"items": [entry(data["items"][0]["id"], 1), entry("i99999", 2)]}
    code, summary, _ = host.run()

    assert code == 0
    [data] = host.calls("agent")
    assert [i["id"] for i in data["items"]] == ["i1", "i2"]
    [row], entries, items = saved_report(host)
    run_id, status, rejected, counts, verification, ai_output, body = row
    assert (run_id, status, json.loads(rejected)) == (summary["run_id"], "passed", [])
    assert json.loads(counts) == {
        "collected": 2,
        "ai_output": 2,
        "excluded": 1,
        "excluded_by_reason": {"invalid_id": 1},
        "published": 1,
    }
    assert json.loads(verification)["invalid_id"] == "failed"
    assert json.loads(verification)["secret_detected"] == "passed"
    assert "見出し1" in ai_output and "> **掲載**：1 件" in body
    assert [(e[0], e[1], json.loads(e[2]), e[3], json.loads(e[4])) for e in entries] == [
        (1, "published", [], 1, ["i1"]),
        (2, "excluded", ["invalid_id"], None, ["i99999"]),
    ]
    assert len(items) == 2


def test_dry_run_item_ids_are_tentative_without_a_db_and_nothing_is_saved(host):
    host.run("--dry-run")
    [data] = host.calls("agent")
    assert [i["id"] for i in data["items"]] == ["d1", "d2"]
    assert not (host.data / "db").exists()


def test_verify_rerun_in_the_same_run_replaces_the_saved_report(host, monkeypatch):
    """同じ実行の ③ のやり直しは、レポートと項目を置き換える（重複しない）。"""
    original = cli.main

    def twice(args, environ):
        code = original(args, environ)
        if args[0] == "verify":  # 同じ入力で ③ をもう一度動かす
            code = original(args, environ)
        return code

    monkeypatch.setattr(cli, "main", twice)
    code, _, _ = host.run()
    assert code == 0
    [row], entries, items = saved_report(host)
    assert row[1] == "passed" and [e[0] for e in entries] == [1, 2]
    assert [i[0] for i in items] == [1, 2]


def test_rejected_output_is_saved_with_the_failed_check(host):
    host.ai = lambda data: {"items": "bad"}
    host.run()
    [row], entries, _ = saved_report(host)
    assert (row[1], json.loads(row[2]), json.loads(row[4])) == (
        "rejected",
        ["invalid_output"],
        {"secret_detected": "passed", "invalid_output": "failed"},
    )
    assert row[6] is None and entries == []


def test_work_files_are_removed_after_a_production_run(host):
    host.run()
    assert [p.name for p in (host.data / "work").iterdir()] == [".lock"]


def test_all_excluded_is_saved_as_rejected_with_reasons(host):
    host.ai = lambda data: {"items": [entry("i98", 1), entry("i99", 2)]}
    host.run()
    [row], entries, _ = saved_report(host)
    assert (row[1], json.loads(row[2])) == ("rejected", ["all_excluded"])
    assert json.loads(row[4])["secret_detected"] == "passed"
    assert row[6] is None and row[5] is not None  # 本文はなく、AI の出力は調査用に残る
    assert [(e[1], json.loads(e[2]), e[3]) for e in entries] == [
        ("excluded", ["invalid_id"], None),
        ("excluded", ["invalid_id"], None),
    ]
    payload = json.loads(entries[0][5])
    assert payload["headline"] == "見出し1" and "exclusion_detail" in payload


def test_display_no_follows_the_report_order(host):
    """本文の通し番号は、カテゴリの固定順・rank 順（AI の出力の順ではない）。"""
    host.ai = lambda data: {
        "items": [
            entry("i1", 1, category="oss"),
            entry("i2", 1, category="ai"),
        ]
    }
    host.run()
    _, entries, _ = saved_report(host)
    assert [(e[0], e[3]) for e in entries] == [(1, 2), (2, 1)]


def test_secret_in_ai_output_saves_neither_output_nor_body(host):
    host.ai = lambda data: {
        "items": [entry("i1", 1, points=["値は " + TOKEN + " です"]), entry("i2", 2)]
    }
    host.run()
    [row], entries, _ = saved_report(host)
    assert (row[1], json.loads(row[2]), row[5], row[6]) == (
        "rejected",
        ["secret_detected"],
        None,
        None,
    )
    assert entries == [] and TOKEN not in str(row)
    assert json.loads(row[3])["ai_output"] == 2  # 保存しなくても、出力の項目の数は残す


def test_excluded_entries_keep_their_own_position_even_when_equal(host):
    """同じ内容の項目が並んでも、除外の理由は AI の出力の順の番号で結び付ける。"""
    host.ai = lambda data: {"items": [entry("i1", 1), entry("i1", 1)]}
    host.run()
    _, entries, _ = saved_report(host)
    assert [(e[0], e[1], json.loads(e[2]), e[3]) for e in entries] == [
        (1, "published", [], 1),
        (2, "excluded", ["duplicate_id"], None),
    ]


def test_secret_found_only_in_the_body_is_not_saved_either(host, monkeypatch):
    """本文を組み立てた後の検出：項目と本文は残さず、件数に AI の出力と除外を数える。"""
    from tech_researcher.validate.secret_scan import SecretScanner

    original = SecretScanner.find
    calls = []

    def find(self, text):
        calls.append(text)
        # 原文の検査（最初の呼び出し群）は通し、本文の検査だけ検出させる
        return (
            original(self, text)
            if "tech-researcher kind=report" not in text
            else [type("F", (), {"kind": "known"})()]
        )

    monkeypatch.setattr(SecretScanner, "find", find)
    host.run()
    [row], entries, _ = saved_report(host)
    assert (row[1], json.loads(row[2]), row[5], row[6]) == (
        "rejected",
        ["secret_detected"],
        None,
        None,
    )
    counts = json.loads(row[3])
    assert (counts["ai_output"], counts["published"]) == (2, 0)
    assert entries == []


# 日次の失敗の分岐（failures.py）：④ の調査・本文・本番の runs の終わり


def retry_count(host) -> int | None:
    with contextlib.closing(open_db(db_path(host.data))) as conn:
        return conn.execute("SELECT retry_count FROM runs").fetchone()[0]


def investigation_json(host) -> str | None:
    with contextlib.closing(open_db(db_path(host.data))) as conn:
        return conn.execute("SELECT investigation_json FROM runs").fetchone()[0]


def test_production_rejected_closes_run_as_rejected_with_investigation(host):
    host.ai = lambda data: {"items": [entry("i99", 1)]}  # 入力にない ID を除外して0件
    code, summary, _ = host.run()

    assert (code, summary["result"]) == (1, "rejected")
    [run] = host.runs()
    assert run[3:] == ("rejected", None, "all_excluded", 1)  # running のまま残さない
    assert json.loads(investigation_json(host))["confidence"] == "medium"
    # ④ は1回だけ。② を呼び直さない
    assert [c["task"] for c in host.calls("agent")] == ["daily", "investigate"]
    assert [c["args"][0] for c in host.calls("compose")] == ["collect", "verify", "finish", "post"]
    [created] = host.github.created  # 不合格の Issue も投稿する
    assert created["labels"] == ("type:rejected", "cadence:daily")
    assert summary["issue_url"] == f"https://github.com/{ISSUE_REPO}/issues/100"
    log = host.data / "logs" / "2026" / "10" / f"{summary['run_id']}.jsonl"
    records = [json.loads(x) for x in log.read_text().splitlines()]
    assert [(r["stage"], r["event"]) for r in records if r["stage"] == "investigate"] == [
        ("investigate", "start"),
        ("investigate", "usage"),
        ("investigate", "end"),
    ]
    [usage] = [r for r in records if r["stage"] == "investigate" and r["event"] == "usage"]
    assert usage["detail"].startswith("model=unknown ")


def test_rejected_issue_body_has_counts_and_checks(host):
    host.ai = lambda data: {"items": [entry("d9", 1), entry("d1", 2, category="security")]}
    code, summary, _ = host.run("--dry-run")

    assert code == 1
    issue = (Path(summary["output_dir"]) / "issue.md").read_text(encoding="utf-8")
    assert "| 不合格となった検証項目 | D6\\(a\\) 出力の形式が不正 |" in issue
    assert "収集 2 件／AI の出力 2 件／除外 0 件／掲載 0 件" in issue
    assert "| D6\\(a\\) 出力の形式が不正 | 不合格 |" in issue


def test_production_agent_failure_closes_run_and_records_model(host):
    def broken(data):
        raise invoke.AgentError("agent_failed", models=("claude-x", "claude-y"))

    host.ai = broken
    code, summary, _ = host.run()

    assert (code, summary["failed_stage"], summary["error_category"]) == (
        1,
        "analyze",
        "agent_failed",
    )
    [run] = host.runs()
    assert run[3:] == ("failed", "analyze", "agent_failed", 1)
    with contextlib.closing(open_db(db_path(host.data))) as conn:
        assert conn.execute("SELECT model, retry_count FROM runs").fetchone() == (
            "claude-x,claude-y",
            0,
        )
    assert investigation_json(host) is not None
    assert [c["task"] for c in host.calls("agent")] == ["daily", "investigate"]
    assert [c["args"][0] for c in host.calls("compose")] == ["collect", "finish", "post"]


@pytest.mark.parametrize("category", ["missing_skill", "launch_failed"])
def test_ai_unusable_failure_skips_investigation_but_keeps_the_template(host, category):
    def broken(data):
        raise invoke.AgentError(category)

    host.ai = broken
    code, summary, _ = host.run("--dry-run")

    assert (code, summary["error_category"]) == (1, category)
    assert [c["task"] for c in host.calls("agent")] == ["daily"]  # ④ を呼ばない
    out_dir = Path(summary["output_dir"])
    assert not (out_dir / "investigation.json").exists()
    issue = (out_dir / "issue.md").read_text(encoding="utf-8")
    assert "AI による調査結果を掲載できませんでした。" in issue and "## 調査結果" not in issue
    assert "実行ID" not in issue  # 3.5：実行ID・日時・付記・フッタは入れない


def test_failed_investigation_falls_back_to_the_template(host):
    def broken(data):
        raise invoke.AgentError("timeout", "stderr の文")

    host.investigator = broken
    host.ai = lambda data: {"items": [entry("d9", 1)]}
    code, summary, _ = host.run("--dry-run")

    assert (code, summary["result"]) == (1, "rejected")
    out_dir = Path(summary["output_dir"])
    assert not (out_dir / "investigation.json").exists()
    assert "AI による調査結果を掲載できませんでした。" in (out_dir / "issue.md").read_text()
    log = [json.loads(x) for x in (out_dir / "log.jsonl").read_text().splitlines()]
    failure = [r for r in log if r["stage"] == "investigate" and r["event"] == "failure"]
    assert [r["error_category"] for r in failure] == ["timeout"]


def test_secret_in_investigation_is_not_published(host):
    def leaky(data):
        return {**investigation(data), "cause": f"値は {TOKEN}"}

    host.investigator = leaky
    host.ai = lambda data: {"items": [entry("d9", 1)]}
    code, summary, out = host.run("--dry-run")

    assert code == 1
    out_dir = Path(summary["output_dir"])
    assert "AI による調査結果を掲載できませんでした。" in (out_dir / "issue.md").read_text()
    assert not (out_dir / "investigation.json").exists()
    log_text = (out_dir / "log.jsonl").read_text()
    assert TOKEN not in out + log_text and "secret_detected" in log_text


def test_production_secret_stop_closes_run_without_investigation(host):
    host.ai = lambda data: {"items": [entry("i1", 1, importance=f"値は {TOKEN}")]}
    code, summary, _ = host.run()

    assert (code, summary["error_category"]) == (1, "secret_detected")
    [run] = host.runs()
    assert run[3:] == ("failed", "verify", "secret_detected", 1)
    assert retry_count(host) == 0
    assert investigation_json(host) is None
    assert [c["task"] for c in host.calls("agent")] == ["daily"]


def test_production_all_sources_failed_keeps_workers_close_and_adds_investigation(host):
    host.feed = None
    code, summary, _ = host.run()

    assert (code, summary["error_category"]) == (1, "all_sources_failed")
    [run] = host.runs()
    assert run[3:] == ("failed", "collect", "all_sources_failed", 1)
    assert json.loads(investigation_json(host))["event"] == "処理が失敗した"
    assert [c["args"][0] for c in host.calls("compose")] == ["collect", "finish", "post"]
    with contextlib.closing(open_db(db_path(host.data))) as conn:
        # ① の worker は再試行回数を書かない。finish が補う（rss の3回の試行は再試行2回）
        assert conn.execute("SELECT retry_count FROM runs").fetchone() == (2,)


def test_dry_run_failure_does_not_write_db_and_reads_history(host):
    """ドライランは失敗でも DB に書かない。本番の履歴は ④ の入力に読むだけ（D-22）。"""
    previous = "20261023-060000-abcd"
    with contextlib.closing(open_db(db_path(host.data))) as conn:
        start_run(conn, previous, daily_target(date(2026, 10, 23), 1), STARTED - timedelta(days=1))
        finish_run(
            conn, previous, "failed", STARTED, failed_stage="collect", error_category="timeout"
        )
        fetch = SourceFetch("github_releases", "ok", 1, 3, 3, STARTED, None, (("a/b", "timeout"),))
        record_source_fetch(conn, previous, fetch)
        before = list(conn.iterdump())
    host.ai = lambda data: {"items": [entry("d9", 1)]}
    code, _, _ = host.run("--dry-run")

    assert code == 1
    with contextlib.closing(open_db(db_path(host.data))) as conn:
        assert list(conn.iterdump()) == before
    lines = host.calls("agent")[-1]["lines"]
    history = [line["text"] for line in lines if line["kind"] == "history"]
    assert len(history) == 1 and "status=failed failed_stage=collect" in history[0]
    assert "partial_sources=github_releases:1" in history[0]  # 一部だけ取れなかった情報源も入れる


def test_production_verify_failure_closes_run_as_failed_verify(host, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("想定外")

    monkeypatch.setattr(cli, "run_verify", broken)
    code, summary, _ = host.run()

    assert (code, summary["failed_stage"], summary["error_category"]) == (1, "verify", "exit_1")
    [run] = host.runs()
    assert run[3:] == ("failed", "verify", "exit_1", 1)  # running のまま残さない


def test_run_close_failure_does_not_change_the_result(host, monkeypatch):
    """runs を閉じられなくても、要約は元の失敗のまま出す（後始末の失敗はログに残す）。"""
    real = host.fake_compose_run

    def run(compose, service, args, timeout_s):
        if args[0] == "finish":
            raise OSError("docker")
        return real(compose, service, args, timeout_s)

    monkeypatch.setattr(host, "fake_compose_run", run)
    host.ai = lambda data: {"items": [entry("i99", 1)]}
    code, summary, _ = host.run()

    assert (code, summary["result"]) == (1, "rejected")
    log = host.data / "logs" / "2026" / "10" / f"{summary['run_id']}.jsonl"
    assert ("job", "failure") in stages_of(log)


def test_body_over_the_limit_is_rejected_not_posted(host, monkeypatch):
    """本文が 60,000 文字を超える日は不合格にする（R 0.3）。掲載を絞って作り直さない。"""
    monkeypatch.setattr("tech_researcher.render.common.BODY_LIMIT", 100)
    code, summary, _ = host.run("--dry-run")

    assert (code, summary["result"]) == (1, "rejected")
    out_dir = Path(summary["output_dir"])
    validation = json.loads((out_dir / "validation.json").read_text())
    assert (validation["reason"], validation["rejected_checks"]) == (
        "body_too_long",
        ["body_too_long"],
    )
    assert validation["counts"]["published"] == 0  # 投稿しないので、掲載の件数は 0
    issue = (out_dir / "issue.md").read_text(encoding="utf-8")
    assert issue.startswith("[不合格]") and "本文が 60,000 文字を超えた" in issue
    assert "見出し1" not in issue
    assert [c["task"] for c in host.calls("agent")] == ["daily", "investigate"]


def test_secret_in_body_is_checked_before_the_length(host, monkeypatch):
    """本文が長すぎる日も、本文全体の秘匿情報の検査を先に行い、公開停止にする。"""
    from tech_researcher.validate.secret_scan import SecretScanner

    monkeypatch.setattr("tech_researcher.render.common.BODY_LIMIT", 100)
    original = SecretScanner.find

    def find(self, text):
        if "tech-researcher kind=report" in text:
            return [type("F", (), {"kind": "known"})()]
        return original(self, text)

    monkeypatch.setattr(SecretScanner, "find", find)
    code, summary, _ = host.run("--dry-run")

    assert (code, summary["result"], summary["error_category"]) == (1, "error", "secret_detected")
    out_dir = Path(summary["output_dir"])
    assert json.loads((out_dir / "validation.json").read_text())["result"] == "secret_detected"
    assert "公開を停止しました" in (out_dir / "issue.md").read_text(encoding="utf-8")


def test_failure_body_over_the_limit_falls_back_to_the_template(host, monkeypatch):
    """不合格・実行エラーの本文も 60,000 文字の上限を守る。超えたら調査結果を載せない定型にする。"""
    monkeypatch.setattr("tech_researcher.render.common.BODY_LIMIT", 400)
    host.feed = None
    code, summary, _ = host.run("--dry-run")

    assert (code, summary["error_category"]) == (1, "all_sources_failed")
    out_dir = Path(summary["output_dir"])
    issue = (out_dir / "issue.md").read_text(encoding="utf-8")
    assert "AI による調査結果を掲載できませんでした。" in issue and "## 調査結果" not in issue
    assert not (out_dir / "investigation.json").exists()  # 載せない結果は残さない
    log = [json.loads(x) for x in (out_dir / "log.jsonl").read_text().splitlines()]
    assert [r["error_category"] for r in log if r.get("error_category") == "body_too_long"]


def test_secret_in_failure_body_keeps_the_original_result_in_runs(host, monkeypatch):
    """本文全体の検査で公開停止にしても、runs は元の結果のまま閉じる。

    公開停止の記録先は issues の blocked_secret（⑤）。それまではログの post の secret_detected。
    """
    monkeypatch.setattr(
        failures,
        "_secret_kinds",
        lambda scanner, text: "known" if "調査結果" in text and "公開を停止" not in text else "",
    )
    host.ai = lambda data: {"items": [entry("i99", 1)]}  # 不合格
    code, summary, _ = host.run()

    assert (code, summary["result"]) == (1, "rejected")  # 要約は元の結果のまま
    [run] = host.runs()
    assert run[3:] == ("rejected", None, "all_excluded", 1)
    assert retry_count(host) == 0
    log = host.data / "logs" / "2026" / "10" / f"{summary['run_id']}.jsonl"
    assert ("post", "secret_detected") in stages_of(log)


def test_production_rejected_records_retry_count_zero(host):
    host.ai = lambda data: {"items": [entry("i99", 1)]}
    host.run()
    assert retry_count(host) == 0  # 不合格に再試行はない


def test_production_agent_failure_records_retry_count_zero(host):
    def broken(data):
        raise invoke.AgentError("agent_failed")

    host.ai = broken
    host.run()
    assert retry_count(host) == 0  # ② の再試行は別の Issue で行う


def test_body_too_long_day_is_saved_as_rejected_with_zero_published(host, monkeypatch):
    monkeypatch.setattr("tech_researcher.render.common.BODY_LIMIT", 100)
    host.run()
    [row], entries, _ = saved_report(host)
    assert (row[1], json.loads(row[2])) == ("rejected", ["body_too_long"])
    assert json.loads(row[3])["published"] == 0
    # Issue を作らないので、項目の状態が published でも「載せた」の導出には入らない（issues がない）
    assert [e[1] for e in entries] == ["published", "published"]
    with contextlib.closing(open_db(db_path(host.data))) as conn:
        assert featured_urls(conn) == set()
        assert shown_urls(conn, date(2026, 10, 23)) == set()
