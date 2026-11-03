"""統括スクリプト（D-04、structure.md 3.2・3.7・6.4）。ホストで動き、`bin/tr-run` から起動する。

`bin/tr-run` が作業ディレクトリをリポジトリにして `python -m tech_researcher.orchestrator` を呼ぶ。
流れ：

1. 引数（`daily`・`weekly` と `--dry-run` だけ）と環境を確かめる。TR_DATA_DIR はリポジトリの外の
   既存のフォルダ、TR_UID・TR_GID は 0（root）以外
2. 種類ごとのロック（flock）を取る。日次・週次で別にし、ドライランは本番と別にする
   （ドライランが本番の実行を止めないように。US-E8-04）。取れなければ何もせずに終わる
3. 実行 ID と対象日を決め、ジョブを子プロセスで動かす。ジョブ全体の上限を超えたら、子プロセスの
   グループとその実行のコンテナを止め、止めたことをログに残す（6.4）
4. ジョブは段階を `docker compose run` で1つずつ起動する。段階ごとの上限を超えたらコンテナを止める
5. 標準出力には、コードが作る定型の要約（JSON 1行）だけを出す。収集した文・AI の出力・例外の
   中身は出さない（例外は秘匿情報をマスクするログにだけ残す）

compose には許可した環境変数だけを渡し、リポジトリの .env も読ませない（3.7）。ホストのシェルに
ANTHROPIC_API_KEY や COMPOSE_FILE などがあっても、compose の変数の展開と設定に使わせない。

日次は ① 収集（worker）→ ② 評価・要約（call_agent）→ ③ 検証・検査（worker）とつなぐ。
② の AI の呼び出し（agent コンテナ。#18）と ⑤ 投稿はまだないので、それまでの本番の日次は ② で
実行エラー（not_implemented）になる。② をつないだ後も、⑤ ができるまでは ③ の後に実行エラーで
終わる。週次は ① の後に終わる（ドライランは ① だけ、本番は not_implemented）。段階が失敗した
ときの ④・⑤ への分岐も、それらの Issue で足す。

終了コード：0 は最後まで動いた（レポート・ドライラン）、1 は実行エラー・不合格（ジョブ全体の上限で
止めたときを含む）、2 は起動の誤り（引数・設定・環境）、3 は同じ種類の実行が動いている。
"""

import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from tech_researcher.agent.daily import InputTooLarge, encode_input
from tech_researcher.collect.sources import source_names
from tech_researcher.config import Config, ConfigError, StageLimits, load_config
from tech_researcher.logging import RunLog
from tech_researcher.periods import JST, Target, daily_target, weekly_target
from tech_researcher.runfiles import RunFiles, data_dir, new_run_id, run_files
from tech_researcher.stages.daily import AGENT_INPUT, AGENT_OUTPUT, REJECTED, SECRET_DETECTED
from tech_researcher.stages.daily import RESULTS as VERIFY_RESULTS
from tech_researcher.validate.secret_scan import SecretScanner, known_secret_values

CONFIG = Path("config/tech-researcher.toml")
COMPOSE_FILE = "compose.yaml"
WORKER = "worker"
# compose run で作るコンテナに付けるラベル。上限で止めるときに、その実行のコンテナだけを消す
RUN_ID_LABEL = "tech-researcher.run-id"
SUMMARY = "summary.json"  # ドライランの出力のフォルダに残す要約（structure.md 7.2）

# compose に渡す環境変数（許可リスト）。PATH・HOME は docker の CLI とプラグインを探すため、
# DOCKER_HOST は Docker のデーモンの指定（rootless など）のため。DOCKER_CONFIG は統括スクリプトが
# 専用のフォルダを指定する（下）。TR_* は compose.yaml の変数の展開に使う。ほかの変数（認証情報、
# COMPOSE_* などの compose の設定）は渡さない。認証情報は compose が env_file から各コンテナにだけ
# 渡す（D-05）。DOCKER_CONTEXT は渡さない（コンテキストは利用者の Docker の設定にあり、専用の
# フォルダからは見えないため。デーモンは DOCKER_HOST で指定する）
COMPOSE_ENV = ("PATH", "HOME", "DOCKER_HOST")
UID_ENV = {"TR_UID": lambda: os.getuid(), "TR_GID": lambda: os.getgid()}
# Docker の CLI の設定のフォルダ（DOCKER_CONFIG）。利用者の ~/.docker/config.json の proxies は
# compose がコンテナの環境変数に入れるので、使わせない。worker にマウントする TR_DATA_DIR の下には
# 置かない（コンテナの中から、次に起動するコンテナの設定を書き換えられないように）
DOCKER_CONFIG_DIR = Path("tech-researcher") / "docker"

MINUTE = 60.0  # 設定の上限（分）を秒にする。テストで縮める
STOP_GRACE_S = 10.0  # 止めるとき、SIGTERM から SIGKILL までの猶予
DOCKER_TIMEOUT_S = 60.0  # コンテナを探す・消す docker の呼び出しの上限
POLL_S = 0.2  # ジョブの子プロセスの終わりを確かめる間隔
STDERR_TAIL_CHARS = 2000  # 段階の失敗のとき、ログに残す stderr の末尾

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_BUSY = 3
_EXIT_NO_SUMMARY = 70  # 子プロセスが要約を出せずに終わった（親が代わりに出す）

_TARGETS = {"daily": daily_target, "weekly": weekly_target}
_UID = re.compile(r"[0-9]{1,10}")
MAX_ID = 2**31 - 1  # Docker の --user に渡せる UID・GID の上限


class UsageError(Exception):
    pass


class Busy(Exception):
    pass


@dataclass
class Summary:
    """bin/tr-run が標準出力に出す定型の要約（structure.md 3.2）。値はすべてコードが作る。

    result：report（レポート）・rejected（不合格）・error（実行エラー）・dry_run（ドライラン）。
    failed_sources は設定ファイルの表示名。Issue の番号と URL は ⑤ を実装する Issue で足す。
    """

    run_id: str
    cadence: str
    target: str
    result: str
    failed_stage: str | None = None
    error_category: str | None = None
    items: int | None = None
    failed_sources: list[str] = field(default_factory=list)
    output_dir: str | None = None


@dataclass(frozen=True)
class StageResult:
    returncode: int | None  # None は段階の上限で止めた
    stdout: str
    stderr: str
    remove_failed: bool = False  # 残ったコンテナを消せなかった


class Compose:
    """1回の実行の `docker compose run`。許可した環境変数だけを渡す。"""

    def __init__(self, repo: Path, env: Mapping[str, str], run_id: str):
        self._repo = repo
        self._env = dict(env)
        self._label = f"{RUN_ID_LABEL}={run_id}"

    def run(self, service: str, args: Sequence[str], timeout_s: float) -> StageResult:
        """段階を1つ起動して終わりを待つ。timeout_s を超えたら止め、そのコンテナを消す。"""
        command = [
            "docker",
            "compose",
            "--project-directory",
            str(self._repo),
            "--file",
            str(self._repo / COMPOSE_FILE),
            "--env-file",  # リポジトリの .env を読ませない
            os.devnull,
            "run",
            "--rm",
            "-T",
            "--label",
            self._label,
            service,
            *args,
        ]
        proc = subprocess.Popen(
            command,
            cwd=self._repo,
            env=self._env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            out, err = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            # SIGTERM で docker の CLI は終わるが、コンテナは動き続ける（実機で確認）。ラベルで消す
            proc.terminate()
            try:
                out, err = proc.communicate(timeout=STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                out, err = "", ""
            return StageResult(None, out, err, not self._remove_quietly())
        if proc.returncode != 0:
            # CLI が異常終了しても（SIGKILL など）、コンテナは残りうる
            return StageResult(proc.returncode, out, err, not self._remove_quietly())
        return StageResult(proc.returncode, out, err)

    def _remove_quietly(self) -> bool:
        """消せたら True。消せなくても段階の結果（上限・終了コード）を返すため、止めない。"""
        try:
            self.remove_containers()
        except OSError, subprocess.SubprocessError:
            return False
        return True

    def remove_containers(self) -> None:
        """この実行のコンテナを消す（動いていれば止める）。"""
        found = subprocess.run(
            ["docker", "ps", "--all", "--quiet", "--filter", f"label={self._label}"],
            env=self._env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=DOCKER_TIMEOUT_S,
            check=True,
        )
        ids = found.stdout.split()
        if ids:
            subprocess.run(
                ["docker", "rm", "--force", *ids],
                env=self._env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=DOCKER_TIMEOUT_S,
                check=True,
            )


@dataclass(frozen=True)
class Job:
    """1回の実行。親（ロックとジョブ全体の上限）と子（段階の起動）の両方が使う。"""

    repo: Path
    config: Config
    files: RunFiles
    target: Target
    compose: Compose
    log: RunLog
    scanner: SecretScanner

    @property
    def cadence(self) -> str:
        return self.target.cadence

    @property
    def limits(self) -> StageLimits:
        return getattr(self.config.limits, self.cadence)

    def summary(self, result: str, **fields) -> Summary:
        return Summary(
            run_id=self.files.run_id,
            cadence=self.cadence,
            target=self.target.key.isoformat(),
            result=result,
            output_dir=str(self.files.dir) if self.files.dry_run else None,
            **fields,
        )


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] = os.environ) -> int:
    args = _parser().parse_args(argv)
    repo = Path.cwd()
    try:
        data, compose_env = check_environment(environ, repo)
        config = load_config(repo / CONFIG)
    except (UsageError, ConfigError, ValueError, OSError) as e:
        return _usage_error(e, environ)
    name = f"{args.cadence}-dry-run" if args.dry_run else args.cadence
    try:
        with lock(data / "locks" / f"{name}.lock"):
            job = _new_job(repo, data, config, args.cadence, args.dry_run, compose_env, environ)
            return supervise(job)
    except Busy:
        print(f"tr-run: 同じ種類の実行（{name}）が動いている", file=sys.stderr)
        return EXIT_BUSY
    except OSError as e:  # ロックのファイル・fork など
        return _usage_error(e, environ)


def _usage_error(e: Exception, environ: Mapping[str, str]) -> int:
    # 誤って秘匿値を設定した変数の値などが、そのまま cron の出力に残らないようにする
    scanner = SecretScanner(known_secret_values(environ))
    print(scanner.mask(f"tr-run: {e}"), file=sys.stderr)
    return EXIT_USAGE


def _parser() -> argparse.ArgumentParser:
    # 過去の期間を指定する引数は作らない（US-E6-05）。略記（--dry など）も受け付けない
    parser = argparse.ArgumentParser(prog="tr-run", allow_abbrev=False)
    parser.add_argument("cadence", choices=sorted(_TARGETS))
    parser.add_argument("--dry-run", action="store_true", help="⑤ 投稿を飛ばす（D-22）")
    return parser


def check_environment(environ: Mapping[str, str], repo: Path) -> tuple[Path, dict[str, str]]:
    """データ用フォルダと、compose に渡す環境変数（許可リスト）を返す。"""
    data = data_dir(environ, repo).resolve()  # リポジトリの外であることも確かめる（D-22）
    if not data.is_dir():
        raise UsageError(f"TR_DATA_DIR のフォルダがない（先に作る）: {data}")
    env = {name: environ[name] for name in COMPOSE_ENV if environ.get(name)}
    env["TR_DATA_DIR"] = str(data)
    env["DOCKER_CONFIG"] = str(docker_config(environ))
    for name, current in UID_ENV.items():
        # 未設定なら bin/tr-run を動かしている人の ID（統括スクリプトが作るフォルダの持ち主と同じ）
        value = environ.get(name) or str(current())
        # root で動かしたときや、sudo の下の $(id -u) などで 0 になると、コンテナが root で動く
        if not _UID.fullmatch(value) or not 0 < int(value) <= MAX_ID:
            raise UsageError(f"{name} は 1〜{MAX_ID} の数にする（0 は root。root で動かさない）")
        env[name] = str(int(value))
    return data, env


def docker_config(environ: Mapping[str, str]) -> Path:
    """このプロジェクト専用の Docker の CLI の設定のフォルダを用意する（権限 700）。

    `${XDG_CONFIG_HOME:-$HOME/.config}/tech-researcher/docker`。公開イメージの取得とビルドには
    設定が要らないので、空のフォルダでよい。
    """
    base = environ.get("XDG_CONFIG_HOME") or (
        str(Path(environ["HOME"]) / ".config") if environ.get("HOME") else ""
    )
    if not base or not Path(base).is_absolute():
        raise UsageError("HOME（または XDG_CONFIG_HOME）を絶対パスで設定する")
    path = Path(base) / DOCKER_CONFIG_DIR
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


@contextmanager
def lock(path: Path) -> Iterator[None]:
    """flock で排他のロックを取る。ほかのプロセスが持っていれば、待たずに Busy にする。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy from None
        yield
    finally:
        os.close(fd)


def _new_job(
    repo: Path,
    data: Path,
    config: Config,
    cadence: str,
    dry_run: bool,
    compose_env: Mapping[str, str],
    environ: Mapping[str, str],
) -> Job:
    files = RunFiles(data, new_run_id(datetime.now(JST), dry_run=dry_run))
    days = config.daily_period_days if cadence == "daily" else config.weekly_period_days
    target = _TARGETS[cadence](files.started_on, days)
    scanner = SecretScanner(known_secret_values(environ))
    return Job(
        repo=repo,
        config=config,
        files=files,
        target=target,
        compose=Compose(repo, compose_env, files.run_id),
        log=RunLog(files.log, files.run_id, cadence, target.key, scanner),
        scanner=scanner,
    )


def supervise(job: Job) -> int:
    """ジョブを子プロセスで動かし、ジョブ全体の上限を超えたら止める（structure.md 6.4）。

    子プロセスは自分のプロセスグループを持ち、止めるときはグループごと止める（compose の CLI も）。
    コンテナは CLI を止めても動き続けるので、ラベルで消す。親が受けた SIGINT・SIGTERM・SIGHUP も
    子プロセスのグループに伝える。子プロセスが要約を出せずに終わったときは、親が代わりに出す。
    """
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:  # 子プロセス
        code = _EXIT_NO_SUMMARY
        try:
            os.setpgid(0, 0)
            # 止められたら SystemExit にし、run_files の後片付け（一次情報の本文を消す）を走らせる
            signal.signal(signal.SIGTERM, _exit_on_signal)
            code = run_job(job)
        except BaseException:
            pass  # 例外の中身（外部由来の文を含みうる）は出さない。親が実行エラーの要約を出す
        finally:
            with suppress(BaseException):
                sys.stdout.flush()
            os._exit(code)
    with suppress(ProcessLookupError, PermissionError):
        os.setpgid(pid, pid)  # 子プロセスが自分で設定する前に止めることになっても、グループで止める
    deadline = [time.monotonic() + job.limits.job * MINUTE]
    interrupted: list[int] = []

    def forward(signum: int, _frame: object) -> None:
        # 中断は上限と同じ流れで止める（すぐに SIGTERM、猶予の後に SIGKILL）
        interrupted.append(signum)
        deadline[0] = 0

    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {s: signal.signal(s, forward) for s in signals}
    try:
        code = _wait(pid, deadline)
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)
    if code in (EXIT_OK, EXIT_FAILED) and not interrupted:
        return code
    # 上限で止めた・シグナルで止めた・子プロセスが要約を出せなかった
    # 子プロセスだけ先に終わっても、グループに残ったもの（SIGTERM を無視した孫など）を止める
    _kill_group(pid, signal.SIGKILL)
    if code in (EXIT_OK, EXIT_FAILED):
        return code  # 止める前に子プロセスが要約を出して終わった
    if interrupted:
        category = "interrupted"
    elif code is None:
        category = "job_timeout"
    else:
        category = "internal_error"
    try:
        job.compose.remove_containers()
    except Exception as e:
        _log_quietly(job, "job", "failure", error_category="container_remove_failed", exc=e)
    _log_quietly(job, "job", "failure", error_category=category)
    _log_quietly(job, "job", "end")
    _emit(job, job.summary("error", error_category=category))
    return EXIT_FAILED


def _exit_on_signal(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


def _wait(pid: int, deadline: list[float]) -> int | None:
    """子プロセスの終了コードを返す。deadline[0]（シグナルの受け取りで早まる）を過ぎたら
    グループを止め、None を返す。"""
    while True:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            return os.waitstatus_to_exitcode(status)
        left = deadline[0] - time.monotonic()
        if left <= 0:
            break
        time.sleep(min(POLL_S, left))
    _kill_group(pid, signal.SIGTERM)
    stop = time.monotonic() + STOP_GRACE_S
    while time.monotonic() < stop:
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done:
            break
        time.sleep(POLL_S)
    else:
        _kill_group(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    _kill_group(pid, signal.SIGKILL)  # グループに残ったもの（compose の CLI など）
    return None


def _kill_group(pgid: int, sig: int) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def run_job(job: Job) -> int:
    """段階を順に起動し、要約を出して終了コードを返す。想定外の例外も実行エラーの要約にする。"""
    try:
        with run_files(job.files.data_dir, job.files.run_id):
            job.log.write("job", "start")
            summary = _run_stages(job)
            job.log.write("job", "end")
    except Exception as e:
        _log_quietly(job, "job", "failure", error_category=type(e).__name__, exc=e)
        summary = job.summary("error", error_category="internal_error")
    _emit(job, summary)
    return EXIT_OK if summary.result in ("report", "dry_run") else EXIT_FAILED


def _run_stages(job: Job) -> Summary:
    names = source_names(job.config)
    collected, category = _worker(
        job, "collect", job.limits.collect, lambda out: _stage_summary(out, names)
    )
    if collected is None:
        return job.summary("error", failed_stage="collect", error_category=category)
    fields = {"items": collected["items"], "failed_sources": collected["failed_sources"]}
    if job.cadence == "weekly":
        # 週次の ② 〜 ⑤ はまだない。本番はレポートを作れないので実行エラーにする
        if job.files.dry_run:
            return job.summary("dry_run", **fields)
        job.log.write("analyze", "failure", error_category="not_implemented")
        return job.summary(
            "error", failed_stage="analyze", error_category="not_implemented", **fields
        )
    category = _analyze(job)
    if category is not None:
        return job.summary("error", failed_stage="analyze", error_category=category, **fields)
    verified, category = _worker(job, "verify", job.limits.verify, _verify_summary)
    if verified is None:
        return job.summary("error", failed_stage="verify", error_category=category, **fields)
    if verified["result"] == REJECTED:  # 不合格（US-E7-02）。④ と不合格 Issue は後の Issue で足す
        return job.summary("rejected", **fields)
    if verified["result"] == SECRET_DETECTED:  # 本文を公開しない（US-E7-05）
        return job.summary("error", failed_stage="verify", error_category=SECRET_DETECTED, **fields)
    if job.files.dry_run:  # ドライランは ⑤ だけを飛ばす（D-04）
        return job.summary("dry_run", **fields)
    # ⑤ 投稿はまだない。本番はレポートを公開できないので実行エラーにする
    job.log.write("post", "failure", error_category="not_implemented")
    return job.summary("error", failed_stage="post", error_category="not_implemented", **fields)


def call_agent(job: Job, body: bytes) -> dict:
    """② の AI を agent コンテナで呼び、構造化出力（structured_output）を返す。

    agent コンテナと Claude Code の呼び出しの境界は #18 で実装する。それまでは呼べないので、
    ② は実行エラー（not_implemented）になる。テストでは偽の実装に差し替える（製品には偽の AI の
    モードを作らない）。
    """
    raise NotImplementedError


def _analyze(job: Job) -> str | None:
    """② を呼び、構造化出力を agent-output.json に保存する。失敗なら分類を返す。

    入力は worker が書いた agent-input.json。上限を超えたら AI を呼ばない（D-21）。
    """
    job.log.write("analyze", "start")
    data = json.loads((job.files.dir / AGENT_INPUT).read_text(encoding="utf-8"))
    try:
        body = encode_input(data, job.limits.agent_input_bytes)
    except InputTooLarge as e:
        job.log.write("analyze", "failure", error_category="input_too_large", detail=str(e))
        return "input_too_large"
    try:
        output = call_agent(job, body)
    except NotImplementedError:
        job.log.write("analyze", "failure", error_category="not_implemented")
        return "not_implemented"
    except Exception as e:
        job.log.write("analyze", "failure", error_category="agent_failed", exc=e)
        return "agent_failed"
    if not isinstance(output, dict):
        job.log.write("analyze", "failure", error_category="bad_output")
        return "bad_output"
    # AI の出力（外部由来の文を含みうる）はファイルにだけ置く。中身の検査は ③ で行う
    partial = job.files.dir / f"{AGENT_OUTPUT}.partial"
    partial.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
    partial.replace(job.files.dir / AGENT_OUTPUT)
    job.log.write("analyze", "end")
    return None


def _worker(
    job: Job, stage: str, minutes: int, parse: Callable[[str], dict | None]
) -> tuple[dict | None, str | None]:
    """worker の段階を1つ動かし、要約を parse で読んで返す。失敗なら分類を返す。"""
    args = [stage, job.cadence, "--run-id", job.files.run_id]
    try:
        result = job.compose.run(WORKER, args, minutes * MINUTE)
    except OSError as e:
        job.log.write(stage, "failure", error_category="launch_failed", exc=e)
        return None, "launch_failed"
    if result.returncode is None:
        category = "timeout"
    elif result.returncode != 0:
        category = f"exit_{result.returncode}"
    else:
        summary = parse(result.stdout)
        if summary is not None:
            return summary, None
        category = "bad_output"
    # 先に切るとトークンの頭が欠けて検出できなくなるので、マスクしてから末尾を残す
    detail = job.scanner.mask(result.stderr)[-STDERR_TAIL_CHARS:]
    job.log.write(stage, "failure", error_category=category, detail=detail)
    if result.remove_failed:
        job.log.write(stage, "failure", error_category="container_remove_failed")
    return None, category


def _last_json(stdout: str) -> dict | None:
    """段階の CLI の要約（cli.py）。最後の行だけを読む。イメージがないと compose run がビルドし、
    その進み具合を標準出力に出すため（実機で確認）。"""
    lines = stdout.strip().splitlines()
    try:
        data = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _stage_summary(stdout: str, names: Mapping[str, str]) -> dict | None:
    """① の要約を読み、情報源 ID を設定ファイルの表示名にする。

    形が違うとき、または設定にない情報源 ID があるときは None（要約に設定の名前以外を出さない）。
    """
    data = _last_json(stdout)
    if data is None:
        return None
    items, failed = data.get("items"), data.get("failed_sources")
    if not _count(items):
        return None
    if not isinstance(failed, list) or not all(isinstance(s, str) and s in names for s in failed):
        return None
    return {"items": items, "failed_sources": [names[s] for s in failed]}


def _verify_summary(stdout: str) -> dict | None:
    """③ の要約（結果の種別と件数）を読む。形が違えば None。"""
    data = _last_json(stdout)
    if data is None or data.get("result") not in VERIFY_RESULTS:
        return None
    if not (_count(data.get("entries")) and _count(data.get("excluded"))):
        return None
    return {k: data[k] for k in ("result", "entries", "excluded")}


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _emit(job: Job, summary: Summary) -> None:
    """要約を標準出力に1行で出す。ドライランは出力のフォルダにも残す。"""
    line = json.dumps(asdict(summary), ensure_ascii=False)
    if job.files.dry_run and job.files.dir.is_dir():
        with suppress(OSError):
            (job.files.dir / SUMMARY).write_text(line + "\n", encoding="utf-8")
    print(line, flush=True)


def _log_quietly(job: Job, stage: str, event: str, **fields) -> None:
    """ログに書けなくても止めない（要約を出すことを優先する）。"""
    with suppress(Exception):
        job.log.write(stage, event, **fields)


if __name__ == "__main__":
    sys.exit(main())
