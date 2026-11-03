import json
from functools import partial
from pathlib import Path

import pytest

from tech_researcher import cli
from tech_researcher.collect.http import Response
from tech_researcher.collect.rss import collect_feed
from tech_researcher.stages.collect import run_collect

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
    monkeypatch.setattr(cli, "run_collect", partial(run_collect, collect=collect))
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


def test_weekly_target_comes_from_run_id(env, capsys):
    run_id = "dry-20261028-070000-abcd"  # 水曜。対象週は 10-26（月）から
    (Path(env["TR_DATA_DIR"]) / "dry-run" / run_id).mkdir()
    assert cli.main(["collect", "weekly", "--run-id", run_id], env) == 0
    assert json.loads(capsys.readouterr().out)["target"] == "2026-10-26"


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


def test_invalid_config_exits_2(env, capsys):
    Path("config/tech-researcher.toml").write_text("[daily]\nperiod_days = 1\n", encoding="utf-8")
    assert cli.main(["collect", "daily", "--run-id", DRY], env) == 2
    assert "必須の項目がない" in capsys.readouterr().err


@pytest.mark.parametrize("args", [[], ["post"], ["collect", "monthly", "--run-id", DRY]])
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
