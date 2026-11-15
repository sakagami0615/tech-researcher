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
from datetime import datetime
from functools import partial
from pathlib import Path

import pytest

from tech_researcher import cli, observe, orchestrator
from tech_researcher.agent import invoke
from tech_researcher.collect.http import FetchError, Response
from tech_researcher.collect.rss import collect_feed
from tech_researcher.db.feed_snapshots import fetch_recorded
from tech_researcher.db.schema import open_db
from tech_researcher.orchestrator import StageResult
from tech_researcher.periods import JST
from tech_researcher.runfiles import db_path, new_run_id
from tech_researcher.stages.collect import Collectors, run_collect

ROOT = Path(__file__).parents[2]
FEED = (ROOT / "tests" / "fixtures" / "minimal-feed.xml").read_bytes()
EMPTY_FEED = b'<?xml version="1.0"?><rss version="2.0"><channel><title>E</title></channel></rss>'
LIMITS = (ROOT / "config" / "tech-researcher.toml").read_text(encoding="utf-8").split("[limits]")[1]
STARTED = datetime(2026, 10, 24, 6, 0, tzinfo=JST)  # 対象日は 2026-10-23（金）
TOKEN = "ghp" + "_" + "z" * 36  # テスト用のダミー。実行時に組み立てる


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


class Host:
    def __init__(self, tmp_path: Path):
        self.repo = tmp_path / "repo"
        self.data = tmp_path / "data"
        self.record = tmp_path / "record"
        self.environ = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "TR_DATA_DIR": str(self.data),
        }
        # worker の env_file から渡る秘匿値（D-05）の代わり
        self.worker_env = {"TR_DATA_DIR": str(self.data), "GITHUB_TOKEN": TOKEN}
        self.feed = FEED
        self.ai = select_all

    def write_config(self, limits: str = LIMITS) -> None:
        (self.repo / "config" / "tech-researcher.toml").write_text(
            "[daily]\nperiod_days = 1\n[weekly]\nperiod_days = 7\n"
            '[[feeds]]\nid = "example"\ndisplay_name = "Example"\n'
            'url = "https://blog.example.com/feed.xml"\nkind = "new"\nenabled = true\n'
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

    def fake_call_agent(self, body, timeout_s):
        assert timeout_s > 0  # 段階の上限を渡している
        data = json.loads(body)
        self._append("agent", data)
        return self.ai(data)

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
    monkeypatch.chdir(h.repo)
    monkeypatch.setattr(
        orchestrator, "new_run_id", lambda now, **kwargs: new_run_id(STARTED, **kwargs)
    )
    monkeypatch.setattr(orchestrator.Compose, "run", lambda c, *a: h.fake_compose_run(c, *a))
    monkeypatch.setattr(invoke, "call_agent", h.fake_call_agent)
    collect = partial(collect_feed, fetch_url=lambda url, limits: h.fake_fetch(url, limits))
    collectors = Collectors(feed=collect)
    monkeypatch.setattr(cli, "run_collect", partial(run_collect, collectors=collectors))
    return h


def stages_of(log_path: Path) -> list[tuple[str, str]]:
    return [(r["stage"], r["event"]) for r in map(json.loads, log_path.read_text().splitlines())]


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
        "output_dir": str(out_dir),
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


def test_ai_choosing_nothing_is_a_report_saying_so(host):
    """AI が1件も選ばなかった日は、除外で0件（不合格）と違い、該当なしのレポートになる。"""
    host.ai = lambda data: {"items": []}
    code, summary, _ = host.run("--dry-run")

    assert code == 0
    assert summary["result"] == "dry_run"
    assert len(host.calls("agent")) == 1
    issue = (Path(summary["output_dir"]) / "issue.md").read_text(encoding="utf-8")
    assert "**該当なし**：" in issue and "（収集 2 件）" in issue


def test_production_saves_collect_but_does_not_post(host):
    code, summary, _ = host.run()

    assert code == 1
    assert not summary["run_id"].startswith("dry-")
    assert (summary["result"], summary["failed_stage"], summary["error_category"]) == (
        "error",
        "post",
        "not_implemented",
    )
    assert [c["args"][0] for c in host.calls("compose")] == ["collect", "verify"]
    assert [p.name for p in (host.data / "work").iterdir()] == [".lock"]  # 実行中のファイルは消した
    assert not (host.data / "dry-run").exists()
    # ① は runs の開始・取得結果・収集項目を書く。終わりは ⑤ で書くので running のまま
    with contextlib.closing(open_db(db_path(host.data))) as conn:
        assert conn.execute("SELECT run_id, status, finished_at FROM runs").fetchall() == [
            (summary["run_id"], "running", None)
        ]
        assert conn.execute("SELECT source_id, status FROM source_fetches").fetchall() == [
            ("rss:example", "ok")
        ]
        (count,) = conn.execute("SELECT count(*) FROM items").fetchone()
        assert count == summary["items"] > 0
    log = host.data / "logs" / "2026" / "10" / f"{summary['run_id']}.jsonl"
    assert ("verify", "end") in stages_of(log)
    assert ("post", "failure") in stages_of(log)


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


@pytest.mark.parametrize(
    ("output", "reason"),
    [
        ({"items": [entry("d9", 1)]}, "all_excluded"),
        ({"items": "x"}, "invalid_output"),
        ({"items": [entry("d1", 1, category="security")]}, "invalid_output"),
        (["not", "object"], "invalid_output"),  # 形の誤りは D6(a) で全体を不合格（D-07）
    ],
)
def test_rejected_output_makes_no_report(host, output, reason):
    host.ai = lambda data: output
    code, summary, _ = host.run("--dry-run")

    assert code == 1
    assert (summary["result"], summary["failed_stage"]) == ("rejected", None)
    out_dir = Path(summary["output_dir"])
    assert json.loads((out_dir / "validation.json").read_text())["reason"] == reason
    assert not (out_dir / "issue.md").exists()


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
    assert not (out_dir / "issue.md").exists()
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
    assert host.calls("agent") == []
    assert [c["args"][0] for c in host.calls("compose")] == ["collect"]


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
    assert host.calls("agent") == []
    assert not (Path(summary["output_dir"]) / "issue.md").exists()
    assert not (Path(summary["output_dir"]) / "agent-input.json").exists()  # ② に進まない


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


def test_secret_in_ai_ids_is_masked_in_results(host):
    host.ai = lambda data: {"items": [entry("d1", 1), entry(TOKEN, 2)]}
    code, summary, _ = host.run("--dry-run")

    assert code == 0
    out_dir = Path(summary["output_dir"])
    for name in ("validation.json", "log.jsonl", "issue.md"):
        assert TOKEN not in (out_dir / name).read_text(encoding="utf-8"), name
    assert "[MASKED:known]" in (out_dir / "validation.json").read_text(encoding="utf-8")


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
        ).fetchall() == [("rss:example", "failed", "fetch_failed", 3)]


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
