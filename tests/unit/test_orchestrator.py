"""統括スクリプトの検査（structure.md 3.2・3.7・6.4）。

docker を偽のプロセス（PATH の先頭に置いた `docker`）に差し替え、ロックは flock で確かめる。
偽の docker は、受け取った引数と環境変数とロックの状態を calls.jsonl に書き、behavior.json の
指示（ok・fail・bad・sleep）どおりに振る舞う。偽の docker には許可した環境変数しか届かないので、
指示はファイルで渡す。① は ok のとき、日次の ② の入力（1項目）も書く。③ は verify の指示
（既定は report）どおりの結果を返す。
② の AI は、項目なしの出力を返す偽の実装に差し替える。段階を本物でつなぐ一巡は test_daily_flow.py。
"""

import fcntl
import json
import os
import subprocess
import sys
import time
import tomllib
from datetime import date
from pathlib import Path

import pytest

from tech_researcher import envfiles, orchestrator
from tech_researcher.agent import invoke
from tech_researcher.collect.sources import source_names
from tech_researcher.config import parse_config

ROOT = Path(__file__).parents[2]
LIMITS = (ROOT / "config" / "tech-researcher.toml").read_text(encoding="utf-8").split("[limits]")[1]
TOKEN = "ghp" + "_" + "y" * 36  # テスト用のダミー。実行時に組み立てる
# worker.env の偽の秘匿値。トークンの形ではないので、既知の値としてだけ伏せられる
WORKER_SECRET = "fake-issue-credential-0123"
PRODUCT_CALL_AGENT = invoke.call_agent

FAKE_DOCKER = f"""#!{sys.executable}
import fcntl, json, os, sys, time
from pathlib import Path

here = Path(__file__).parent
behavior = json.loads((here / "behavior.json").read_text())
lock = os.open(behavior["lock"], os.O_RDWR | os.O_CREAT)
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    locked = False
    fcntl.flock(lock, fcntl.LOCK_UN)
except BlockingIOError:
    locked = True
with (here / "calls.jsonl").open("a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "env": dict(os.environ), "locked": locked}}) + "\\n")
command = sys.argv[1]
mode = behavior["mode"]
if command == "ps":
    print("cid1" if mode == "sleep" else "")
elif command == "compose" and "verify" in sys.argv:
    verify = behavior.get("verify", "report")
    if verify == "fail":
        sys.exit(1)
    print("not json" if verify == "bad" else json.dumps({{"result": verify}}))
elif command == "compose":
    if mode == "ok":
        stage, cadence, _, run_id = sys.argv[-4:]
        if cadence == "daily" and behavior.get("input", True):
            kind = "dry-run" if run_id.startswith("dry-") else "work"
            run_dir = Path(os.environ["TR_DATA_DIR"]) / kind / run_id
            data = {{"task": "daily", "items": [{{"id": "d1"}}]}}
            (run_dir / "agent-input.json").write_text(json.dumps(data))
        sources = behavior.get("sources", 3)
        print(json.dumps({{"items": 3, "sources": sources, "failed_sources": behavior["failed"]}}))
    elif mode == "fail":
        print("tech-researcher: collect が失敗した " + behavior["secret"], file=sys.stderr)
        sys.exit(1)
    elif mode == "bad":
        print("not json")
    elif mode == "sleep":
        time.sleep(60)
"""


class Host:
    def __init__(self, tmp_path: Path):
        self.repo = tmp_path / "repo"
        self.data = tmp_path / "data"
        self.fake = tmp_path / "fakebin"
        self.secrets = tmp_path / "secrets"
        self.environ = {
            "PATH": f"{self.fake}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "TR_DATA_DIR": str(self.data),
            "TR_SECRETS_DIR": str(self.secrets),
        }

    def behave(self, mode: str, lock: str = "daily", **extra) -> None:
        lock_path = self.data / "locks" / f"{lock}.lock"
        behavior = {"mode": mode, "lock": str(lock_path), "failed": [], "secret": TOKEN, **extra}
        (self.fake / "behavior.json").write_text(json.dumps(behavior))

    def calls(self) -> list[dict]:
        path = self.fake / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def log(self, run_id: str) -> list[dict]:
        if run_id.startswith("dry-"):
            path = self.data / "dry-run" / run_id / "log.jsonl"
        else:
            path = self.data / "logs" / run_id[:4] / run_id[4:6] / f"{run_id}.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def host(tmp_path, monkeypatch) -> Host:
    """作業ディレクトリをリポジトリに見立て、設定・データ用フォルダ・偽の docker を用意する。

    上限（分）は 1 分を 0.05 秒に縮める（日次の ① は 1 秒、ジョブ全体は 3 秒）。
    """
    h = Host(tmp_path)
    (h.repo / "config").mkdir(parents=True)
    (h.repo / "config" / "tech-researcher.toml").write_text(
        "[daily]\nperiod_days = 1\n[weekly]\nperiod_days = 7\n"
        '[sources.qiita]\nenabled = true\ndisplay_name = "Qiita"\ntags = ["LLM"]\n'
        '[[feeds]]\nid = "example"\ndisplay_name = "Example"\nurl = "https://blog.example.com/feed.xml"\n'
        'kind = "new"\nenabled = true\n'
        f"[limits]{LIMITS}",
        encoding="utf-8",
    )
    monkeypatch.chdir(h.repo)
    h.data.mkdir()
    h.fake.mkdir()
    h.secrets.mkdir(mode=0o700)
    worker_env = h.secrets / "worker.env"
    worker_env.write_text(f"GITHUB_TOKEN=fake-collect-token\nTR_ISSUE_TOKEN={WORKER_SECRET}\n")
    worker_env.chmod(0o600)
    docker = h.fake / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    h.behave("ok")
    monkeypatch.setattr(invoke, "call_agent", lambda body, timeout_s: {"items": []})
    monkeypatch.setattr(orchestrator, "MINUTE", 0.05)
    monkeypatch.setattr(orchestrator, "STOP_GRACE_S", 1.0)
    monkeypatch.setattr(orchestrator, "POLL_S", 0.02)
    return h


def summary_of(capfd) -> dict:
    """標準出力が要約の JSON 1行だけであることを確かめて返す。"""
    lines = capfd.readouterr().out.splitlines()
    assert len(lines) == 1, lines
    return json.loads(lines[0])


# --- 引数と環境 ---


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["monthly"],
        ["daily", "weekly"],
        ["daily", "--since", "2026-10-01"],  # 過去の期間を指定する引数はない（US-E6-05）
        ["daily", "--date=2026-10-01"],
        ["daily", "--dry"],  # 略記も受け付けない
        ["daily", "--run-id", "20261024-060000-abcd"],
    ],
)
def test_rejects_unknown_arguments(host, argv, capfd):
    with pytest.raises(SystemExit) as e:
        orchestrator.main(argv, host.environ)
    assert e.value.code == 2
    assert capfd.readouterr().out == ""
    assert host.calls() == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"TR_DATA_DIR": None}, "設定されていない"),
        ({"TR_DATA_DIR": "data"}, "絶対パス"),
        ({"TR_DATA_DIR": "{repo}/data"}, "リポジトリの外"),
        ({"TR_DATA_DIR": "{tmp}/missing"}, "フォルダがない"),
        ({"TR_UID": "0"}, "TR_UID は 1〜"),
        ({"TR_GID": "00"}, "TR_GID は 1〜"),
        ({"TR_UID": "2147483648"}, "TR_UID"),
        ({"TR_GID": "4294967296"}, "TR_GID"),
        ({"TR_UID": "-1"}, "TR_UID"),
        ({"TR_GID": "1000:0"}, "TR_GID"),
    ],
)
def test_rejects_bad_environment(host, change, message, capfd, tmp_path):
    environ = dict(host.environ)
    for name, value in change.items():
        if value is None:
            environ.pop(name)
        else:
            environ[name] = value.format(repo=host.repo, tmp=tmp_path)
    assert orchestrator.main(["daily", "--dry-run"], environ) == 2
    out, err = capfd.readouterr()
    assert out == ""
    assert message in err
    assert host.calls() == []
    assert not (host.data / "locks").exists()


def test_startup_error_does_not_show_secret(host, capfd):
    # 誤って TR_UID にトークンを入れても、cron の出力に値を残さない
    environ = {**host.environ, "GITHUB_TOKEN": TOKEN, "TR_UID": TOKEN}
    assert orchestrator.main(["daily", "--dry-run"], environ) == 2
    assert TOKEN not in capfd.readouterr().err


def test_uid_is_normalized(host, capfd):
    environ = {**host.environ, "TR_UID": "01001", "TR_GID": "2147483647"}
    assert orchestrator.main(["daily", "--dry-run"], environ) == 0
    summary_of(capfd)
    env = host.calls()[0]["env"]
    assert (env["TR_UID"], env["TR_GID"]) == ("1001", "2147483647")


def test_uid_defaults_to_runner(host, capfd, monkeypatch):
    monkeypatch.setattr(orchestrator.os, "getuid", lambda: 1234)
    monkeypatch.setattr(orchestrator.os, "getgid", lambda: 5678)
    assert orchestrator.main(["daily", "--dry-run"], host.environ) == 0
    summary_of(capfd)
    env = host.calls()[0]["env"]
    assert (env["TR_UID"], env["TR_GID"]) == ("1234", "5678")


def test_root_runner_is_rejected_without_explicit_ids(host, capfd, monkeypatch):
    monkeypatch.setattr(orchestrator.os, "getuid", lambda: 0)
    assert orchestrator.main(["daily", "--dry-run"], host.environ) == 2
    assert "TR_UID" in capfd.readouterr().err
    assert host.calls() == []


def test_docker_config_is_private_and_own(host, capfd, tmp_path):
    xdg = tmp_path / "xdg"
    assert (
        orchestrator.main(["daily", "--dry-run"], {**host.environ, "XDG_CONFIG_HOME": str(xdg)})
        == 0
    )
    summary_of(capfd)
    path = xdg / "tech-researcher" / "docker"
    assert host.calls()[0]["env"]["DOCKER_CONFIG"] == str(path)
    assert path.stat().st_mode & 0o777 == 0o700
    assert not path.is_relative_to(host.data)  # worker にマウントするフォルダの外


def test_rejects_missing_home(host, capfd):
    environ = {k: v for k, v in host.environ.items() if k != "HOME"}
    assert orchestrator.main(["daily", "--dry-run"], environ) == 2
    assert "HOME" in capfd.readouterr().err


def test_rejects_bad_config(host, capfd):
    (host.repo / "config" / "tech-researcher.toml").write_text("[daily]\nperiod_days = 0\n")
    assert orchestrator.main(["daily", "--dry-run"], host.environ) == 2
    assert "必須の項目がない" in capfd.readouterr().err
    assert host.calls() == []


def test_compose_gets_only_allowed_environment(host, capfd):
    environ = {
        **host.environ,
        "ANTHROPIC_API_KEY": "sk-ant-" + "a" * 30,
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat" + "b" * 30,
        "GITHUB_TOKEN": TOKEN,
        "COMPOSE_FILE": "/tmp/evil.yaml",
        "COMPOSE_PROJECT_NAME": "evil",
        "TR_SECRETS_DIR": str(host.secrets) + "/",  # 確かめて解決したパスの下の写しを渡す
        "DOCKER_HOST": "unix:///run/user/1000/docker.sock",
        "DOCKER_CONTEXT": "desktop-linux",  # 専用の設定のフォルダからは見えないので渡さない
        "DOCKER_CONFIG": "/home/someone/.docker",  # 利用者の設定（proxies など）は使わない
        "TR_UID": "1001",
        "TR_GID": "1002",
    }
    assert orchestrator.main(["daily", "--dry-run"], environ) == 0
    summary = summary_of(capfd)
    run_id = summary["run_id"]
    calls = host.calls()
    assert [c["argv"][-4] for c in calls] == ["collect", "verify"]  # ① と ③
    assert all(c["env"] == calls[0]["env"] for c in calls)
    call = calls[0]
    env = call["env"]
    env.pop("LC_CTYPE", None)  # 偽の docker（Python）がロケールの強制で足すことがある
    assert env == {
        "PATH": host.environ["PATH"],
        "HOME": host.environ["HOME"],
        "DOCKER_HOST": "unix:///run/user/1000/docker.sock",
        "DOCKER_CONFIG": str(Path(host.environ["HOME"]) / ".config" / "tech-researcher" / "docker"),
        "TR_DATA_DIR": str(host.data),
        # この実行の間だけの worker.env の写しの場所だけ。中身は compose が読む
        "TR_SECRETS_DIR": str(host.secrets / f".run-{run_id}"),
        "TR_UID": "1001",
        "TR_GID": "1002",
    }
    assert call["argv"] == [
        "compose",
        "--project-directory",
        str(host.repo),
        "--file",
        str(host.repo / "compose.yaml"),
        "--env-file",
        os.devnull,
        "run",
        "--rm",
        "-T",
        "--label",
        f"tech-researcher.run-id={run_id}",
        "worker",
        "collect",
        "daily",
        "--run-id",
        run_id,
    ]


def test_data_dir_is_passed_resolved(host, capfd, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(host.data)
    assert (
        orchestrator.main(["daily", "--dry-run"], {**host.environ, "TR_DATA_DIR": str(link)}) == 0
    )
    summary_of(capfd)
    assert host.calls()[0]["env"]["TR_DATA_DIR"] == str(host.data)


# --- worker.env（TR_SECRETS_DIR。D-05・structure.md 3.7） ---


def _no_dir(h):
    h.secrets.chmod(0o700)
    for f in h.secrets.iterdir():
        f.unlink()
    h.secrets.rmdir()


def _loop(h):
    loop = h.secrets.parent / "loop"
    loop.symlink_to(loop)
    h.environ["TR_SECRETS_DIR"] = str(loop / "secrets")


def _link_file(h):
    real = h.secrets / "real.env"
    (h.secrets / "worker.env").rename(real)
    (h.secrets / "worker.env").symlink_to(real)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda h: h.environ.pop("TR_SECRETS_DIR"), "TR_SECRETS_DIR が設定されていない"),
        (lambda h: h.environ.update(TR_SECRETS_DIR="secrets"), "絶対パス"),
        (lambda h: h.environ.update(TR_SECRETS_DIR=str(h.repo / "s")), "リポジトリの外"),
        (lambda h: h.environ.update(TR_SECRETS_DIR=str(h.data / "s")), "TR_DATA_DIR の外"),
        (lambda h: h.environ.update(TR_SECRETS_DIR=str(h.data)), "TR_DATA_DIR の外"),
        (_no_dir, "フォルダ がない"),
        (_loop, "確かめられない"),  # リンクのループもパスを出さずに止める
        (lambda h: (h.secrets / "worker.env").unlink(), "worker.env がない"),
        (lambda h: h.secrets.chmod(0o750), "フォルダ の権限は 700"),
        (lambda h: h.secrets.chmod(0o701), "フォルダ の権限は 700"),
        (lambda h: (h.secrets / "worker.env").chmod(0o640), "worker.env の権限は 600・400"),
        (lambda h: (h.secrets / "worker.env").chmod(0o604), "worker.env の権限は 600・400"),
        # 持ち主が読めなければ compose も読めない
        (lambda h: (h.secrets / "worker.env").chmod(0o000), "worker.env の権限は 600・400"),
        (lambda h: (h.secrets / "worker.env").chmod(0o200), "worker.env の権限は 600・400"),
        (lambda h: h.secrets.chmod(0o600), "フォルダ の権限は 700"),
        (lambda h: h.secrets.chmod(0o500), "フォルダ の権限は 700"),  # 写しを作れない
        # GNU の chmod 700 は setgid を残すので、いまの権限と直し方の例を出す
        (lambda h: h.secrets.chmod(0o2700), "いまは 2700。"),
        (lambda h: h.secrets.chmod(0o2700), "例：chmod 00700"),
        (lambda h: (h.secrets / "worker.env").chmod(0o640), "例：chmod 00600"),
        (lambda h: (h.secrets / "worker.env").chmod(0o700), "worker.env の権限は 600・400"),
        (lambda h: (h.secrets / "worker.env").chmod(0o500), "worker.env の権限は 600・400"),
        (lambda h: (h.secrets / "worker.env").chmod(0o4600), "worker.env の権限は 600・400"),
        (_link_file, "シンボリックリンクにしない"),
    ],
)
def test_rejects_bad_secrets_dir(host, capfd, change, message):
    change(host)
    assert orchestrator.main(["daily", "--dry-run"], host.environ) == 2
    out, err = capfd.readouterr()
    assert out == ""
    assert message in err
    assert WORKER_SECRET not in err
    assert str(host.secrets) not in err  # パスも出さない
    if host.environ.get("TR_SECRETS_DIR"):
        assert host.environ["TR_SECRETS_DIR"] not in err
    assert host.calls() == []
    assert not (host.data / "locks").exists()  # 起動の前に止まる
    if host.secrets.exists():
        host.secrets.chmod(0o700)  # tmp_path を片付けられるように


def test_unresolvable_secrets_dir_is_rejected_without_path(host, capfd, monkeypatch):
    real = envfiles.Path.resolve

    def fail(self, strict=False):
        if self == host.secrets:  # TR_SECRETS_DIR だけをたどれなくする
            raise OSError(40, "Too many levels of symbolic links", str(self))
        return real(self, strict)

    monkeypatch.setattr(envfiles.Path, "resolve", fail)
    assert orchestrator.main(["daily", "--dry-run"], host.environ) == 2
    err = capfd.readouterr().err
    assert "パスをたどれない" in err
    assert str(host.secrets) not in err
    assert host.calls() == []


def test_rejects_worker_env_compose_would_misread_before_start(host, capfd):
    # compose は書式の誤りを値ごとエラー文に出すので、起動の前に行の番号だけを出して止める
    (host.secrets / "worker.env").write_text(f'GITHUB_TOKEN=x\nTR_ISSUE_TOKEN="{WORKER_SECRET}\n')
    assert orchestrator.main(["daily", "--dry-run"], host.environ) == 2
    out, err = capfd.readouterr()
    assert out == ""
    assert "worker.env の 2 行目" in err
    assert WORKER_SECRET not in err
    assert host.calls() == []
    assert not (host.data / "locks").exists()


def test_rejects_secrets_owned_by_someone_else(host, capfd, monkeypatch):
    other = os.geteuid() + 1
    monkeypatch.setattr(envfiles.os, "geteuid", lambda: other)
    assert orchestrator.main(["daily", "--dry-run"], host.environ) == 2
    assert "持ち主は bin/tr-run を動かす人" in capfd.readouterr().err
    assert host.calls() == []


def test_read_only_worker_env_is_accepted(host, capfd):
    (host.secrets / "worker.env").chmod(0o400)
    assert orchestrator.main(["daily", "--dry-run"], host.environ) == 0
    summary_of(capfd)


def test_worker_env_values_are_masked_in_stage_errors(host, capfd):
    # compose は env_file の書式の誤りを、その行の値ごとエラー文に出す（実機で確認）
    host.behave("fail", lock="daily-dry-run", secret=f'unterminated quoted value "{WORKER_SECRET}')
    assert orchestrator.main(["daily", "--dry-run"], host.environ) == 1
    out, err = capfd.readouterr()
    summary = json.loads(out)
    log_text = (Path(summary["output_dir"]) / "log.jsonl").read_text()
    assert WORKER_SECRET not in out + err + log_text
    assert "[MASKED:known]" in log_text


def test_compose_reads_snapshot_of_worker_env_during_run(host, capfd, monkeypatch, tmp_path):
    # 実行の途中で worker.env を書き換えても、compose が読む値はマスクに使う値と同じ（写し）
    record = tmp_path / "snapshot.json"

    def run(self, service, args, timeout_s):  # ジョブの子プロセスで動く
        (host.secrets / "worker.env").write_text("TR_ISSUE_TOKEN=fake-rewritten-credential\n")
        copy = Path(self.env["TR_SECRETS_DIR"])
        text = (copy / "worker.env").read_text()
        modes = [copy.stat().st_mode & 0o777, (copy / "worker.env").stat().st_mode & 0o777]
        record.write_text(json.dumps({"copy": str(copy), "modes": modes}))
        return orchestrator.StageResult(1, "", f"failed to read: {text}")

    monkeypatch.setattr(orchestrator.Compose, "run", run)
    assert orchestrator.main(["daily", "--dry-run"], host.environ) == 1
    summary = json.loads(capfd.readouterr().out)
    log_text = (Path(summary["output_dir"]) / "log.jsonl").read_text()
    assert WORKER_SECRET not in log_text
    assert "fake-rewritten" not in log_text  # 写しには書き換える前の値しかない
    assert "[MASKED:known]" in log_text
    seen = json.loads(record.read_text())
    assert Path(seen["copy"]) == host.secrets / f".run-{summary['run_id']}"
    assert seen["modes"] == [0o700, 0o600]
    assert [p.name for p in host.secrets.iterdir()] == ["worker.env"]  # 終わったら写しを消す


def test_snapshot_failure_stops_before_start(host, capfd, monkeypatch):
    def fail(path):
        raise ValueError("TR_SECRETS_DIR の worker.env を読めない")

    monkeypatch.setattr(orchestrator, "secret_values", fail)
    assert orchestrator.main(["daily", "--dry-run"], host.environ) == 2
    out, err = capfd.readouterr()
    assert out == ""
    assert "worker.env を読めない" in err
    assert host.calls() == []
    assert [p.name for p in host.secrets.iterdir()] == ["worker.env"]  # 写しを残さない


def test_snapshot_is_removed_after_stopped_run(host, capfd):
    host.behave("sleep", lock="daily-dry-run")
    assert orchestrator.main(["daily", "--dry-run"], host.environ) == 1
    summary_of(capfd)
    assert [p.name for p in host.secrets.iterdir()] == ["worker.env"]


def test_compose_passes_worker_env_only_to_worker():
    # worker.env は worker の env_file だけ。フォルダはどのサービスにもマウントしない
    text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    uses = [line.strip() for line in lines if "TR_SECRETS_DIR" in line]
    assert len(uses) == 1
    assert uses[0].startswith("- path: ${TR_SECRETS_DIR:?")
    assert uses[0].endswith("}/worker.env")
    top = [i for i, line in enumerate(lines) if line and not line[0].isspace()]
    start = next(i for i in top if lines[i] == "services:")
    end = next((i for i in top if i > start), len(lines))
    section = lines[start + 1 : end]
    services = [line.strip() for line in section if line.startswith("  ") and line[2] != " "]
    assert services == ["worker:"]  # ほかのサービス（#18 の agent）を足したら、ここも見直す
    assert sum("env_file:" in line for line in lines) == 1


# --- 要約 ---


def test_dry_run_prints_summary_only_and_keeps_it(host, capfd):
    host.behave("ok", lock="daily-dry-run", failed=["rss:example", "qiita"])
    assert orchestrator.main(["daily", "--dry-run"], host.environ) == 0

    summary = summary_of(capfd)
    run_id = summary["run_id"]
    assert run_id.startswith("dry-")
    out_dir = host.data / "dry-run" / run_id
    assert summary == {
        "run_id": run_id,
        "cadence": "daily",
        "target": summary["target"],
        "result": "dry_run",
        "failed_stage": None,
        "error_category": None,
        "items": 3,
        "failed_sources": ["Example", "Qiita"],  # 設定ファイルの表示名にする
        "output_dir": str(out_dir),
    }
    assert json.loads((out_dir / "summary.json").read_text(encoding="utf-8")) == summary
    assert [(r["stage"], r["event"]) for r in host.log(run_id)] == [
        ("job", "start"),
        ("analyze", "start"),
        ("analyze", "end"),
        ("job", "end"),
    ]
    assert json.loads((out_dir / "agent-output.json").read_text()) == {"items": []}
    assert all(c["locked"] for c in host.calls())  # 段階の間はロックを持っている


def test_weekly_target_is_monday(host, capfd):
    host.behave("ok", lock="weekly-dry-run")
    assert orchestrator.main(["weekly", "--dry-run"], host.environ) == 0
    summary = summary_of(capfd)
    target = summary["target"]
    assert summary["cadence"] == "weekly"
    assert date.fromisoformat(target).weekday() == 0
    assert host.calls()[0]["argv"][-3:] == ["weekly", "--run-id", summary["run_id"]]


def test_production_ends_as_error_until_later_stages_exist(host, capfd):
    host.behave("ok", lock="daily")
    assert orchestrator.main(["daily"], host.environ) == 1

    summary = summary_of(capfd)
    run_id = summary["run_id"]
    assert not run_id.startswith("dry-")
    assert summary["result"] == "error"
    assert summary["failed_stage"] == "post"  # ⑤ はまだない
    assert summary["error_category"] == "not_implemented"
    assert summary["items"] == 3
    assert summary["output_dir"] is None
    assert list((host.data / "work").iterdir()) == [host.data / "work" / ".lock"]  # 消した
    assert ("post", "failure") in [(r["stage"], r["event"]) for r in host.log(run_id)]
    assert host.calls()[0]["locked"]


@pytest.mark.parametrize("dry_run", [False, True])
def test_daily_stops_at_analyze_until_agent_exists(host, capfd, monkeypatch, dry_run):
    """製品の ② はまだ AI を呼べない（#18）。③ を起動せずに実行エラーにする。"""
    monkeypatch.setattr(invoke, "call_agent", PRODUCT_CALL_AGENT)  # 偽の AI を外す
    host.behave("ok", lock="daily-dry-run" if dry_run else "daily")
    assert orchestrator.main(["daily", *(["--dry-run"] if dry_run else [])], host.environ) == 1
    summary = summary_of(capfd)
    assert (summary["result"], summary["failed_stage"], summary["error_category"]) == (
        "error",
        "analyze",
        "not_implemented",
    )
    assert [c["argv"][-4] for c in host.calls()] == ["collect"]


@pytest.mark.parametrize(
    ("verify", "result", "stage", "category"),
    [
        ("rejected", "rejected", None, None),
        ("secret_detected", "error", "verify", "secret_detected"),
        ("fail", "error", "verify", "exit_1"),
        ("bad", "error", "verify", "bad_output"),
    ],
)
def test_verify_result_decides_summary(host, capfd, verify, result, stage, category):
    host.behave("ok", lock="daily-dry-run", verify=verify)
    assert orchestrator.main(["daily", "--dry-run"], host.environ) == 1
    summary = summary_of(capfd)
    assert (summary["result"], summary["failed_stage"], summary["error_category"]) == (
        result,
        stage,
        category,
    )
    assert summary["output_dir"] is not None


def test_all_sources_failed_is_error_without_ai(host, capfd, monkeypatch):
    """すべての情報源が取得できない日は、0件のレポートにしない（US-E6-03・US-E6-04）。"""
    monkeypatch.setattr(invoke, "call_agent", PRODUCT_CALL_AGENT)
    host.behave("ok", lock="daily-dry-run", failed=["rss:example", "qiita"], sources=2)
    assert orchestrator.main(["daily", "--dry-run"], host.environ) == 1
    summary = summary_of(capfd)
    assert (summary["failed_stage"], summary["error_category"]) == ("collect", "all_sources_failed")
    assert [c["argv"][-4] for c in host.calls()] == ["collect"]  # ② ③ を呼ばない


def test_no_enabled_source_is_error_not_empty_report(host, capfd):
    host.behave("ok", lock="daily-dry-run", sources=0)
    assert orchestrator.main(["daily", "--dry-run"], host.environ) == 1
    summary = summary_of(capfd)
    assert (summary["failed_stage"], summary["error_category"]) == ("collect", "no_sources")
    assert [c["argv"][-4] for c in host.calls()] == ["collect"]


def test_missing_agent_input_is_analyze_failure(host, capfd):
    host.behave("ok", lock="daily-dry-run", input=False)
    assert orchestrator.main(["daily", "--dry-run"], host.environ) == 1
    summary = summary_of(capfd)
    assert (summary["failed_stage"], summary["error_category"]) == ("analyze", "bad_input")


def test_weekly_dry_run_ends_after_collect(host, capfd):
    host.behave("ok", lock="weekly-dry-run")
    assert orchestrator.main(["weekly", "--dry-run"], host.environ) == 0
    assert summary_of(capfd)["result"] == "dry_run"
    assert [c["argv"][-4] for c in host.calls()] == ["collect"]


@pytest.mark.parametrize(("mode", "category"), [("fail", "exit_1"), ("bad", "bad_output")])
def test_stage_failure_is_error_without_leaking_output(host, capfd, mode, category):
    host.behave(mode, lock="daily-dry-run")
    assert orchestrator.main(["daily", "--dry-run"], host.environ) == 1

    out, err = capfd.readouterr()
    assert TOKEN not in out + err
    summary = json.loads(out)
    assert summary["result"] == "error"
    assert summary["failed_stage"] == "collect"
    assert summary["error_category"] == category
    assert summary["items"] is None
    failure = [r for r in host.log(summary["run_id"]) if r["event"] == "failure"]
    assert [(r["stage"], r["error_category"]) for r in failure] == [("collect", category)]
    log_text = (Path(summary["output_dir"]) / "log.jsonl").read_text()
    assert TOKEN not in log_text
    if mode == "fail":
        assert "[MASKED:github_token]" in failure[0]["detail"]
        # 異常終了したときも、残ったコンテナを探して消す
        label = f"label=tech-researcher.run-id={summary['run_id']}"
        assert ["ps", "--all", "--quiet", "--filter", label] in [c["argv"] for c in host.calls()]


def test_unknown_source_id_is_bad_output(host, capfd):
    # 設定にない ID（worker の誤りなど）を要約に素通ししない
    host.behave("ok", lock="daily-dry-run", failed=["rss:example", TOKEN])
    assert orchestrator.main(["daily", "--dry-run"], host.environ) == 1
    out = capfd.readouterr().out
    assert TOKEN not in out
    assert json.loads(out)["error_category"] == "bad_output"


def test_stderr_is_masked_before_it_is_cut(host, capfd):
    # 末尾を切る位置がトークンの途中に来ても、残った部分が漏れない
    tail = "z" * (orchestrator.STDERR_TAIL_CHARS - 30)
    host.behave("fail", lock="daily-dry-run", secret=TOKEN + tail)
    assert orchestrator.main(["daily", "--dry-run"], host.environ) == 1
    summary = summary_of(capfd)
    log_text = (Path(summary["output_dir"]) / "log.jsonl").read_text()
    assert TOKEN[-20:] not in log_text


def test_docker_missing_is_launch_failure(host, capfd):
    (host.fake / "docker").unlink()
    environ = {**host.environ, "PATH": str(host.fake)}
    assert orchestrator.main(["daily", "--dry-run"], environ) == 1
    summary = summary_of(capfd)
    assert (summary["failed_stage"], summary["error_category"]) == ("collect", "launch_failed")


# --- 上限 ---


def test_stage_timeout_stops_and_removes_container(host, capfd):
    host.behave("sleep", lock="daily-dry-run")
    started = time.monotonic()
    assert orchestrator.main(["daily", "--dry-run"], host.environ) == 1
    assert time.monotonic() - started < 10  # ① の上限は 20 分 × 0.05 秒 = 1 秒

    summary = summary_of(capfd)
    assert (summary["failed_stage"], summary["error_category"]) == ("collect", "timeout")
    label = f"label=tech-researcher.run-id={summary['run_id']}"
    argvs = [c["argv"] for c in host.calls()]
    assert ["ps", "--all", "--quiet", "--filter", label] in argvs
    assert ["rm", "--force", "cid1"] in argvs


def _hang(pid_file: Path):
    """compose の代わりに、子プロセスを1つ起動して止まる（ジョブの子プロセスのグループに入る）。"""

    def run(self, service, args, timeout_s):
        # SIGTERM を無視する孫（止めるには SIGKILL が要る）
        proc = subprocess.Popen(["sh", "-c", "trap '' TERM; sleep 60"])
        pid_file.write_text(str(proc.pid))
        time.sleep(60)

    return run


def _gone(pid: int) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_job_timeout_kills_process_group_and_records_it(host, capfd, monkeypatch, tmp_path):
    host.behave("sleep", lock="daily-dry-run")
    pid_file = tmp_path / "sleep.pid"
    monkeypatch.setattr(orchestrator.Compose, "run", _hang(pid_file))
    started = time.monotonic()
    assert orchestrator.main(["daily", "--dry-run"], host.environ) == 1
    assert time.monotonic() - started < 15  # ジョブ全体は 60 分 × 0.05 秒 = 3 秒

    summary = summary_of(capfd)
    assert summary["result"] == "error"
    assert summary["error_category"] == "job_timeout"
    out_dir = Path(summary["output_dir"])
    assert json.loads((out_dir / "summary.json").read_text()) == summary
    records = host.log(summary["run_id"])
    assert ("job", "failure", "job_timeout") in [
        (r["stage"], r["event"], r.get("error_category")) for r in records
    ]
    assert _gone(int(pid_file.read_text()))  # 孫のプロセスも止めた
    assert ["rm", "--force", "cid1"] in [c["argv"] for c in host.calls()]  # コンテナも消した


def test_signal_to_runner_is_forwarded_to_job(host, capfd, monkeypatch, tmp_path):
    host.behave("sleep", lock="daily-dry-run")
    pid_file = tmp_path / "sleep.pid"
    monkeypatch.setattr(orchestrator.Compose, "run", _hang(pid_file))
    monkeypatch.setattr(orchestrator, "MINUTE", 60.0)  # 上限には達しない
    # 子プロセスが段階に入って（親がシグナルを受ける用意ができて）から、別のプロセスで送る
    # （スレッドを使うと fork の警告が出る）
    wait_then_kill = f"while [ ! -s {pid_file} ]; do sleep 0.02; done; kill -TERM {os.getpid()}"
    sender = subprocess.Popen(["sh", "-c", wait_then_kill])
    try:
        assert orchestrator.main(["daily", "--dry-run"], host.environ) == 1
    finally:
        sender.kill()
        sender.wait()
    summary = summary_of(capfd)
    assert summary["error_category"] == "interrupted"
    assert _gone(int(pid_file.read_text()))
    assert ["rm", "--force", "cid1"] in [c["argv"] for c in host.calls()]
    events = [
        (r["stage"], r["event"], r.get("error_category")) for r in host.log(summary["run_id"])
    ]
    assert ("job", "failure", "interrupted") in events


def test_job_timeout_in_production_removes_work_and_releases_lock(
    host, capfd, monkeypatch, tmp_path
):
    host.behave("sleep", lock="daily")
    monkeypatch.setattr(orchestrator.Compose, "run", _hang(tmp_path / "sleep.pid"))
    assert orchestrator.main(["daily"], host.environ) == 1
    assert summary_of(capfd)["error_category"] == "job_timeout"
    # 止められた子プロセスも後片付けをする（一次情報の本文を次の実行まで残さない）
    assert [p.name for p in (host.data / "work").iterdir()] == [".lock"]
    monkeypatch.undo()
    monkeypatch.setattr(orchestrator, "MINUTE", 0.05)
    host.behave("ok", lock="daily")
    assert orchestrator.main(["daily"], host.environ) == 1  # ロックは外れている
    assert summary_of(capfd)["error_category"] == "not_implemented"


def test_container_remove_failure_keeps_stage_result(host, capfd, monkeypatch):
    host.behave("sleep", lock="daily-dry-run")

    def fail(self):
        raise subprocess.CalledProcessError(1, ["docker", "ps"])

    monkeypatch.setattr(orchestrator.Compose, "remove_containers", fail)
    assert orchestrator.main(["daily", "--dry-run"], host.environ) == 1
    summary = summary_of(capfd)
    assert (summary["failed_stage"], summary["error_category"]) == ("collect", "timeout")
    categories = [r.get("error_category") for r in host.log(summary["run_id"])]
    assert "container_remove_failed" in categories


def test_job_that_dies_without_summary_gets_one(host, capfd, monkeypatch):
    host.behave("ok", lock="daily-dry-run")
    monkeypatch.setattr(orchestrator, "run_job", lambda job: os._exit(9))
    assert orchestrator.main(["daily", "--dry-run"], host.environ) == 1
    summary = summary_of(capfd)
    assert (summary["result"], summary["error_category"]) == ("error", "internal_error")


# --- ロック ---


def _hold_lock(path: Path) -> subprocess.Popen:
    """flock(1) でロックを取り、取れるまで待つ。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    # flock が起動する sleep もロックの fd を持つので、グループごと止められるようにする
    proc = subprocess.Popen(["flock", str(path), "sleep", "60"], start_new_session=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        fd = os.open(path, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BlockingIOError:
            return proc
        finally:
            os.close(fd)
        time.sleep(0.02)
    _release(proc)
    raise AssertionError("flock がロックを取れなかった")


def _release(proc: subprocess.Popen) -> None:
    os.killpg(proc.pid, 9)
    proc.wait()


@pytest.mark.parametrize(("argv", "name"), [(["daily"], "daily"), (["weekly"], "weekly")])
def test_same_kind_is_not_started_twice(host, capfd, argv, name):
    holder = _hold_lock(host.data / "locks" / f"{name}.lock")
    try:
        assert orchestrator.main(argv, host.environ) == 3
    finally:
        _release(holder)
    out, err = capfd.readouterr()
    assert out == ""
    assert "動いている" in err
    assert host.calls() == []
    assert not (host.data / "logs").exists()


def test_locks_are_per_kind(host, capfd):
    """日次の本番が動いていても、週次とドライランは動く（ドライランは本番を止めない。US-E8-04）。"""
    holder = _hold_lock(host.data / "locks" / "daily.lock")
    try:
        host.behave("ok", lock="daily-dry-run")
        assert orchestrator.main(["daily", "--dry-run"], host.environ) == 0
        assert summary_of(capfd)["result"] == "dry_run"
        host.behave("ok", lock="weekly")
        assert orchestrator.main(["weekly"], host.environ) == 1  # ② 以降がまだないため
        assert summary_of(capfd)["cadence"] == "weekly"
    finally:
        _release(holder)
    holder = _hold_lock(host.data / "locks" / "daily-dry-run.lock")
    try:
        host.behave("ok", lock="daily")
        assert orchestrator.main(["daily"], host.environ) == 1  # ドライランが動いていても本番は動く
        assert summary_of(capfd)["error_category"] == "not_implemented"
    finally:
        _release(holder)


def test_lock_is_released_after_run(host, capfd):
    assert orchestrator.main(["daily"], host.environ) == 1
    summary_of(capfd)
    assert orchestrator.main(["daily"], host.environ) == 1
    summary_of(capfd)


# --- 部品 ---


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        (
            '{"items": 2, "sources": 2, "failed_sources": ["qiita"]}',
            {"items": 2, "sources": 2, "failed_sources": ["Qiita"]},
        ),
        ('{"items": 2, "sources": 2, "failed_sources": ["rss:gone"]}', None),
        (
            '{"items": 0, "sources": 0, "failed_sources": [], "x": 1}',
            {"items": 0, "sources": 0, "failed_sources": []},
        ),
        # ビルドの進み具合の後に要約（イメージがないときの compose run）
        (
            '#1 DONE 0.0s\n\n{"items": 1, "sources": 1, "failed_sources": []}\n',
            {"items": 1, "sources": 1, "failed_sources": []},
        ),
        ('{"items": 1, "sources": 1, "failed_sources": []}\nnot json\n', None),
        ("", None),
        ("[]", None),
        ('{"items": true, "sources": 1, "failed_sources": []}', None),
        ('{"items": -1, "sources": 1, "failed_sources": []}', None),
        ('{"items": 1, "sources": 1, "failed_sources": [1]}', None),
        ('{"items": 1, "sources": 1}', None),
        ('{"items": 1, "failed_sources": []}', None),
        ('{"items": 1, "sources": 2, "failed_sources": ["qiita", "qiita"]}', None),
        ('{"items": 1, "sources": 1, "failed_sources": ["qiita", "rss:a"]}', None),
    ],
)
def test_stage_summary_shape(stdout, expected):
    assert orchestrator._stage_summary(stdout, {"qiita": "Qiita", "rss:a": "A"}) == expected


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ('{"result": "report", "entries": 2}', {"result": "report"}),
        ('{"result": "rejected"}', {"result": "rejected"}),
        ('{"result": "secret_detected"}', {"result": "secret_detected"}),
        ('{"result": "dry_run"}', None),
        ('{"entries": 2}', None),
        ("not json", None),
    ],
)
def test_verify_summary_shape(stdout, expected):
    assert orchestrator._verify_summary(stdout) == expected


def test_source_names():
    config = parse_config(
        {
            "daily": {"period_days": 1},
            "weekly": {"period_days": 7},
            "sources": {
                "hackernews": {"enabled": True, "display_name": "Hacker News"},
                "zenn": {"topics": ["llm"]},
            },
            "feeds": [
                {
                    "id": "a",
                    "display_name": "A ブログ",
                    "url": "https://a.example.com/feed",
                    "kind": "new",
                    "enabled": False,
                }
            ],
            "limits": parse_limits(),
        }
    )
    assert source_names(config) == {
        "rss:a": "A ブログ",
        "rss:zenn-llm": "Zenn（llm）",
        "hackernews": "Hacker News",
    }


def parse_limits() -> dict:
    return tomllib.loads("[limits]" + LIMITS)["limits"]


# --- bin/tr-run ---


def test_bin_tr_run_calls_orchestrator_from_repo(tmp_path):
    """bin/tr-run は、リポジトリを作業ディレクトリにして統括スクリプトに引数をそのまま渡す。"""
    fake = tmp_path / "bin"
    fake.mkdir()
    record = tmp_path / "uv.json"
    uv = fake / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "called = {'argv': sys.argv[1:], 'cwd': os.getcwd()}\n"
        f"open({str(record)!r}, 'w').write(json.dumps(called))\n"
        "sys.exit(7)\n"
    )
    uv.chmod(0o755)
    link = tmp_path / "tr-run"
    link.symlink_to(ROOT / "bin" / "tr-run")  # 別の場所から呼んでも、リポジトリで動く
    result = subprocess.run(
        [str(link), "daily", "--dry-run"],
        cwd=tmp_path,
        env={"PATH": f"{fake}:/usr/bin:/bin"},
        capture_output=True,
    )
    assert result.returncode == 7
    assert json.loads(record.read_text()) == {
        "argv": [
            "run",
            "--frozen",
            "--no-dev",
            "--quiet",
            "python",
            "-I",
            "-m",
            "tech_researcher.orchestrator",
            "daily",
            "--dry-run",
        ],
        "cwd": str(ROOT),
    }
