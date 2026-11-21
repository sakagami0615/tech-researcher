import json
from contextlib import closing
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path

import pytest

from tech_researcher import cli
from tech_researcher.collect.http import Response
from tech_researcher.collect.rss import collect_feed
from tech_researcher.db.runs import finish_run, start_run
from tech_researcher.db.schema import open_db
from tech_researcher.periods import daily_target
from tech_researcher.stages.collect import Collectors, run_collect

ROOT = Path(__file__).parents[2]
FEED = (ROOT / "tests" / "unit" / "collect" / "fixtures" / "feed.xml").read_bytes()
# 上限値は本物の設定から取る。情報源はテスト用のフィードだけにする
LIMITS = (ROOT / "config" / "tech-researcher.toml").read_text(encoding="utf-8").split("[limits]")[1]
DRY = "dry-20261024-060000-abcd"  # 対象日 2026-10-23
TOKEN = "ghp" + "_" + "x" * 36  # テスト用のダミー。実行時に組み立てる


@pytest.fixture
def env(tmp_path, monkeypatch):
    """作業ディレクトリをリポジトリに見立て、設定とデータ用フォルダを用意する。

    フィードの取得だけを偽の実装に差し替え、解析から書き出しまでは本物を通す。
    """

    def fetch(url, limits):
        assert url == "https://blog.example.com/feed.xml"
        return Response(url, 200, FEED, 1)

    collect = partial(collect_feed, fetch_url=fetch)
    monkeypatch.setattr(
        cli, "run_collect", partial(run_collect, collectors=Collectors(feed=collect))
    )
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "tech-researcher.toml").write_text(
        "[daily]\nperiod_days = 1\n[weekly]\nperiod_days = 7\n"
        '[[feeds]]\nid = "example"\ndisplay_name = "Example"\nurl = "https://blog.example.com/feed.xml"\n'
        'kind = "new"\nenabled = true\n'
        f"[limits]{LIMITS}",
        encoding="utf-8",
    )
    monkeypatch.chdir(repo)
    data = tmp_path / "data"
    (data / "dry-run" / DRY).mkdir(parents=True)
    return {"TR_DATA_DIR": str(data)}


def test_collect_writes_result_and_prints_summary_only(env, capsys):
    assert cli.main(["collect", "daily", "--run-id", DRY], env) == 0

    out = capsys.readouterr().out
    assert json.loads(out) == {
        "run_id": DRY,
        "cadence": "daily",
        "target": "2026-10-23",
        "stage": "collect",
        "items": 2,
        "sources": 1,
        "failed_sources": [],
        "unjudged_feed_sources": [],
        "unjudged_star": False,
        "held_items": 0,
    }
    assert "記事" not in out  # タイトルなどの外部由来の文は出さない
    run_dir = Path(env["TR_DATA_DIR"]) / "dry-run" / DRY
    items = json.loads((run_dir / "collected.json").read_text(encoding="utf-8"))
    assert [i["url"] for i in items] == [
        "https://blog.example.com/full",
        "https://blog.example.com/summary",
    ]
    events = [json.loads(x)["event"] for x in (run_dir / "log.jsonl").read_text().splitlines()]
    assert events == ["start", "source_result", "end"]
    # 日次は ② の入力も書く（項目には並びの順に仮の ID を付ける）
    data = json.loads((run_dir / "agent-input.json").read_text(encoding="utf-8"))
    assert [i["id"] for i in data["items"]] == ["d1", "d2"]


def test_weekly_target_comes_from_run_id(env, capsys):
    run_id = "dry-20261028-070000-abcd"  # 水曜。対象週は 10-26（月）から
    (Path(env["TR_DATA_DIR"]) / "dry-run" / run_id).mkdir()
    assert cli.main(["collect", "weekly", "--run-id", run_id], env) == 0
    assert json.loads(capsys.readouterr().out)["target"] == "2026-10-26"
    assert not (Path(env["TR_DATA_DIR"]) / "dry-run" / run_id / "agent-input.json").exists()


@pytest.mark.parametrize(
    ("args", "environ", "message"),
    [
        (["--run-id", DRY], {}, "TR_DATA_DIR"),
        (["--run-id", "20261024-060000-abcd"], None, "実行のフォルダがない"),
        (["--run-id", "../x"], None, "実行 ID"),
    ],
)
def test_setup_errors_exit_2(env, capsys, args, environ, message):
    assert cli.main(["collect", "daily", *args], env if environ is None else environ) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err


@pytest.mark.parametrize(
    "args", [["collect", "weekly"], ["verify", "daily"]], ids=["weekly", "verify"]
)
def test_observation_is_daily_collect_only(env, capsys, args):
    """観測（obs-）は日次の ① だけ（D-13）。"""
    run_id = "obs-20261024-060000-abcd"
    (Path(env["TR_DATA_DIR"]) / "work" / run_id).mkdir(parents=True)
    assert cli.main([*args, "--run-id", run_id], env) == 2
    assert "観測" in capsys.readouterr().err
    assert not (Path(env["TR_DATA_DIR"]) / "db").exists()


def test_dry_run_opens_existing_db_read_only(env, capsys):
    """ドライランは本番の DB を読み取り専用で開き、何も書かない（D-22）。"""
    path = Path(env["TR_DATA_DIR"]) / "db" / "tech-researcher.sqlite3"
    open_db(path).close()
    assert cli.main(["collect", "daily", "--run-id", DRY], env) == 0
    with closing(open_db(path)) as conn:
        assert conn.execute("SELECT count(*) FROM runs").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM items").fetchone() == (0,)


def test_invalid_config_exits_2(env, capsys):
    Path("config/tech-researcher.toml").write_text("[daily]\nperiod_days = 1\n", encoding="utf-8")
    assert cli.main(["collect", "daily", "--run-id", DRY], env) == 2
    assert "必須の項目がない" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["post"],
        ["collect", "monthly", "--run-id", DRY],
        ["verify", "weekly", "--run-id", DRY],  # 週次の ③ はまだない
    ],
)
def test_unknown_arguments_are_rejected(env, args):
    with pytest.raises(SystemExit) as e:
        cli.main(args, env)
    assert e.value.code == 2


def test_stage_failure_is_logged_but_not_printed(env, capsys, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError(f"外部の文 {TOKEN}")

    monkeypatch.setattr(cli, "run_collect", broken)
    assert cli.main(["collect", "daily", "--run-id", DRY], env) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "外部の文" not in captured.err and TOKEN not in captured.err
    log = Path(env["TR_DATA_DIR"]) / "dry-run" / DRY / "log.jsonl"
    (line,) = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert (line["event"], line["error_category"]) == ("failure", "RuntimeError")
    assert TOKEN not in line["exception"] and "[MASKED:github_token]" in line["exception"]


def test_missing_config_exits_2(env, capsys):
    Path("config/tech-researcher.toml").unlink()
    assert cli.main(["collect", "daily", "--run-id", DRY], env) == 2
    assert "tech-researcher.toml" in capsys.readouterr().err


def test_production_run_uses_work_dir_and_dated_log(env, capsys):
    run_id = "20261024-060000-abcd"
    data = Path(env["TR_DATA_DIR"])
    (data / "work" / run_id).mkdir(parents=True)
    assert cli.main(["collect", "daily", "--run-id", run_id], env) == 0
    assert json.loads(capsys.readouterr().out)["items"] == 2
    assert (data / "work" / run_id / "collected.json").is_file()
    assert (data / "logs" / "2026" / "10" / f"{run_id}.jsonl").is_file()


def test_failure_when_log_cannot_be_written_prints_no_details(env, capsys, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError(f"外部の文 {TOKEN}")

    def unwritable(*args, **kwargs):
        raise OSError("ディスクがいっぱい")

    monkeypatch.setattr(cli, "run_collect", broken)
    monkeypatch.setattr(cli.RunLog, "write", unwritable)
    assert cli.main(["collect", "daily", "--run-id", DRY], env) == 1
    captured = capsys.readouterr()
    assert "外部の文" not in captured.err and TOKEN not in captured.err


@pytest.mark.parametrize("model", ["", "bad name", "a,,b", "claude\n-x"])
def test_bad_model_name_is_usage_error(env, capsys, model):
    # 統括スクリプトが確かめた名前だけを渡す。形が違えば DB とログに入れずに止める
    assert cli.main(["verify", "daily", "--run-id", DRY, "--model", model], env) == 2
    assert "モデルの名前" in capsys.readouterr().err


def test_verify_failure_is_logged_as_verify(env, capsys):
    # ② の出力がない（統括スクリプトの誤りなど）。段階の途中の失敗にし、③ の失敗として残す
    assert cli.main(["collect", "daily", "--run-id", DRY], env) == 0
    assert cli.main(["verify", "daily", "--run-id", DRY], env) == 1
    captured = capsys.readouterr()
    assert "verify が失敗した" in captured.err
    log = Path(env["TR_DATA_DIR"]) / "dry-run" / DRY / "log.jsonl"
    last = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert (last["stage"], last["event"], last["error_category"]) == (
        "verify",
        "failure",
        "FileNotFoundError",
    )


# finish：本番の失敗した実行を runs で閉じる（failures.py が呼ぶ）

PROD = "20261024-060000-abcd"
INVESTIGATION = {"event": "処理が失敗した", "confidence": "high"}
STARTED = datetime(2026, 10, 24, tzinfo=UTC)


def production_run(env, investigation=INVESTIGATION):
    data = Path(env["TR_DATA_DIR"])
    run_dir = data / "work" / PROD
    run_dir.mkdir(parents=True)
    if investigation is not None:
        (run_dir / "investigation.json").write_text(json.dumps(investigation), encoding="utf-8")
    with closing(open_db(data / "db" / "tech-researcher.sqlite3")) as conn:
        start_run(conn, PROD, daily_target(date(2026, 10, 24), 1), STARTED)
    return data


def run_row(data):
    with closing(open_db(data / "db" / "tech-researcher.sqlite3")) as conn:
        return conn.execute(
            "SELECT status, failed_stage, error_category, retry_count, investigation_json,"
            " model, finished_at IS NOT NULL FROM runs"
        ).fetchone()


def test_finish_closes_failed_run_with_investigation_and_model(env, capsys):
    data = production_run(env)
    args = ["finish", "daily", "--run-id", PROD, "--status", "failed", "--failed-stage", "analyze"]
    args += ["--error-category", "agent_failed", "--retry-count", "0", "--model", "claude-x"]
    assert cli.main(args, env) == 0

    assert json.loads(capsys.readouterr().out)["status"] == "failed"
    status, stage, category, retries, text, model, finished = run_row(data)
    assert (status, stage, category, retries, model, finished) == (
        "failed",
        "analyze",
        "agent_failed",
        0,
        "claude-x",
        1,
    )
    assert json.loads(text) == INVESTIGATION


def test_finish_rejected_without_investigation(env):
    data = production_run(env, investigation=None)
    args = ["finish", "daily", "--run-id", PROD, "--status", "rejected"]
    assert cli.main([*args, "--error-category", "all_excluded"], env) == 0
    assert run_row(data) == ("rejected", None, "all_excluded", None, None, None, 1)


def test_finish_rejects_dry_run_and_bad_arguments(env, capsys):
    args = ["finish", "daily", "--run-id", DRY, "--status", "failed", "--failed-stage", "analyze"]
    assert cli.main(args, env) == 2  # ドライランは DB に書かない
    assert "ドライラン" in capsys.readouterr().err
    for bad in (
        ["--error-category", "bad category"],
        ["--error-category", "a" * 65],
        ["--retry-count", "-1"],
        ["--failed-stage", "nowhere"],
    ):
        with pytest.raises(SystemExit) as e:
            cli.main(["finish", "daily", "--run-id", PROD, "--status", "failed", *bad], env)
        assert e.value.code == 2


def test_finish_keeps_the_values_of_a_run_already_closed_and_adds_investigation(env):
    data = production_run(env)
    with closing(open_db(data / "db" / "tech-researcher.sqlite3")) as conn:
        finish_run(
            conn,
            PROD,
            "failed",
            STARTED,
            failed_stage="collect",
            error_category="all_sources_failed",
            retry_count=2,
        )
    args = ["finish", "daily", "--run-id", PROD, "--status", "failed", "--failed-stage", "analyze"]
    args += ["--error-category", "agent_failed", "--retry-count", "2", "--model", "claude-x"]
    assert cli.main(args, env) == 0
    status, stage, category, retries, text, _, _ = run_row(data)
    # 状態・分類は変えず、再試行回数（① の worker は書かない）と ④ の出力を補う
    assert (status, stage, category, retries) == ("failed", "collect", "all_sources_failed", 2)
    assert json.loads(text) == INVESTIGATION


def test_finish_without_a_start_row_does_nothing(env, capsys):
    (Path(env["TR_DATA_DIR"]) / "work" / PROD).mkdir(parents=True)
    args = ["finish", "daily", "--run-id", PROD, "--status", "failed", "--failed-stage", "collect"]
    assert cli.main(args, env) == 0  # ① が開始の行を書く前に止まった実行。閉じるものがない
    assert json.loads(capsys.readouterr().out)["status"] == "not_started"


def test_finish_for_a_run_not_started_does_not_read_the_investigation(env, capsys):
    run_dir = Path(env["TR_DATA_DIR"]) / "work" / PROD
    run_dir.mkdir(parents=True)
    (run_dir / "investigation.json").write_text("{broken")  # 開始の行がなければ読まない
    args = ["finish", "daily", "--run-id", PROD, "--status", "failed", "--failed-stage", "collect"]
    assert cli.main(args, env) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "not_started"
