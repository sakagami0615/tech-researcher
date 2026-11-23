"""統括スクリプト（D-04、structure.md 3.2・3.7・6.4）。ホストで動き、`bin/tr-run` から起動する。

`bin/tr-run` が作業ディレクトリをリポジトリにして `python -m tech_researcher.orchestrator` を呼ぶ。
流れ：

1. 引数（`daily`・`weekly` と `--dry-run` だけ）と環境を確かめる。TR_DATA_DIR はリポジトリの外の
   既存のフォルダ、TR_UID・TR_GID は 0（root）以外、TR_SECRETS_DIR は worker.env・agent.env を
   置いたリポジトリの外の非公開のフォルダ（envfiles.py）
2. 種類ごとのロック（flock）を取る。日次・週次で別にし、ドライランは本番と別にする
   （ドライランが本番の実行を止めないように。US-E8-04）。取れなければ何もせずに終わる
3. 実行 ID と対象日を決め、ジョブを子プロセスで動かす。ジョブ全体の上限を超えたら、子プロセスの
   グループとその実行のコンテナを止め、止めたことをログに残す（6.4）
4. ジョブは段階を `docker compose run` で1つずつ起動する。段階ごとの上限を超えたらコンテナを止める
5. 標準出力には、コードが作る定型の要約（JSON 1行）だけを出す。収集した文・AI の出力・例外の
   中身は出さない（例外は秘匿情報をマスクするログにだけ残す）

compose には許可した環境変数だけを渡し、リポジトリの .env も読ませない（3.7）。ホストのシェルに
ANTHROPIC_API_KEY や COMPOSE_FILE などがあっても、compose の変数の展開と設定に使わせない。
worker.env の中身は compose が worker にだけ、agent.env の中身は agent にだけ渡す。統括スクリプトは
秘匿値をマスクのためにだけ読む。

日次は ① 収集（worker）→ ② 評価・要約（agent コンテナ。agent/invoke.py）→ ③ 検証・検査（worker）
とつなぐ。② で使われたモデルの名前は ③ に渡し、本番では worker が runs の model に記録して前回の
本番と比べる（structure.md 3.3）。日次・週次の ① 〜 ③ の失敗（実行エラー・不合格・秘匿情報の検出）は
failures.py が ④ の調査・本文（issue.md）・本番の runs の終わりへつなぎ、本文を ⑤ で投稿する。
レポートが合格したときも ⑤ で投稿する。⑤ は worker の `post` で、Issue 用トークンは worker だけが
持つ（publish/post.py）。投稿した Issue の URL は要約の issue_url に出す。投稿できなかったときは
実行エラー（failed_stage は post）。ドライランは ⑤ を飛ばす。
週次も同じ流れで、② は週次の分析スキル、③ は検査・テーマ番号の確定・保存（weekly_verify.py）、
⑤ は日次と同じ投稿（重複防止・Projects 登録）。失敗の分岐（④・不合格 Issue・実行エラー Issue・
公開停止）も日次と同じ部品（failures.py）を使う。ドライランも ④ を呼んで investigation.json を
残す（DB には書かず、⑤ は飛ばし、不合格の後に ② を呼び直さない）。

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
from importlib import resources
from pathlib import Path

from tech_researcher import failures
from tech_researcher.agent import invoke
from tech_researcher.agent.daily import InputTooLarge, encode_input
from tech_researcher.collect.sources import source_names
from tech_researcher.config import Config, ConfigError, StageLimits, load_config
from tech_researcher.db.runs import CATEGORY
from tech_researcher.envfiles import (
    ENV_FILES,
    SECRETS_DIR_ENV,
    remove_snapshot,
    secret_values,
    secrets_dir,
    snapshot,
    snapshot_label,
)
from tech_researcher.logging import RunLog
from tech_researcher.periods import JST, Target, daily_target, weekly_target
from tech_researcher.publish import post
from tech_researcher.runfiles import RunFiles, data_dir, new_run_id, run_files
from tech_researcher.stages.collect import collect_failure
from tech_researcher.stages.daily import AGENT_INPUT, AGENT_OUTPUT, REJECTED, SECRET_DETECTED
from tech_researcher.stages.daily import RESULTS as VERIFY_RESULTS
from tech_researcher.validate.schema import DAILY_OUTPUT_SCHEMA, WEEKLY_OUTPUT_SCHEMA
from tech_researcher.validate.secret_scan import SecretScanner, known_secret_values

CONFIG = Path("config/tech-researcher.toml")
COMPOSE_FILE = "compose.yaml"
WORKER = "worker"
# compose run で作るコンテナに付けるラベル。上限で止めるときに、その実行のコンテナだけを消す
RUN_ID_LABEL = "tech-researcher.run-id"
SUMMARY = "summary.json"  # ドライランの出力のフォルダに残す要約（structure.md 7.2）
# ② 日次の出力のスキーマ。`--json-schema` に渡し、③ も同じものを検査に使う（structure.md 2.2）
DAILY_SCHEMA = resources.files("tech_researcher") / "schemas" / DAILY_OUTPUT_SCHEMA
WEEKLY_SCHEMA = resources.files("tech_researcher") / "schemas" / WEEKLY_OUTPUT_SCHEMA

# compose に渡す環境変数（許可リスト）。PATH・HOME は docker の CLI とプラグインを探すため、
# DOCKER_HOST は Docker のデーモンの指定（rootless など）のため。DOCKER_CONFIG は統括スクリプトが
# 専用のフォルダを指定する（下）。TR_* は compose.yaml の変数の展開に使う。ほかの変数（認証情報、
# COMPOSE_* などの compose の設定）は渡さない。認証情報は compose が env_file から各コンテナにだけ
# 渡す（D-05）。env_file の場所（TR_SECRETS_DIR）は、確かめたパスを統括スクリプトが渡す。
# DOCKER_CONTEXT は渡さない（コンテキストは利用者の Docker の設定にあり、専用の
# フォルダからは見えないため。デーモンは DOCKER_HOST で指定する）
COMPOSE_ENV = ("PATH", "HOME", "DOCKER_HOST")
UID_ENV = {"TR_UID": lambda: os.getuid(), "TR_GID": lambda: os.getgid()}
# Docker の CLI の設定のフォルダ（DOCKER_CONFIG）。利用者の ~/.docker/config.json の proxies は
# compose がコンテナの環境変数に入れるので、使わせない。worker にマウントする TR_DATA_DIR の下には
# 置かない（コンテナの中から、次に起動するコンテナの設定を書き換えられないように）
DOCKER_CONFIG_DIR = Path("tech-researcher") / "docker"

MINUTE = 60.0  # 設定の上限（分）を秒にする。テストで縮める
RETRY_MIN_MINUTES = 1  # ② の再試行を始めるのに要る、合計の上限の残りの時間（分）
_now = time.monotonic  # ② の再試行の残り時間の計算に使う時計。テストで偽物に差し替える
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

    result：report（レポート）・rejected（不合格）・error（実行エラー）・dry_run（ドライラン）・
    observed（D-13 の観測。observe.py）。
    failed_sources は取得に失敗した情報源、partial_sources は一部だけ取れなかった情報源（取れた分は
    使った部分レポート。US-E6-03）の、設定ファイルの表示名。issue_url は ⑤ で作った
    （すでにあった）Issue の URL（不合格・実行エラー・公開停止の Issue も）。issue_url があれば
    公開済み（result が error でも、runs を閉じられなかっただけのことがある）。
    """

    run_id: str
    cadence: str
    target: str
    result: str
    failed_stage: str | None = None
    error_category: str | None = None
    items: int | None = None
    failed_sources: list[str] = field(default_factory=list)
    partial_sources: list[str] = field(default_factory=list)
    output_dir: str | None = None
    issue_url: str | None = None
    # 失敗の Issue（不合格・実行エラー・公開停止）の投稿が失敗したときの分類。結果と runs は元のまま
    issue_post_failed: str | None = None


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

    @property
    def env(self) -> Mapping[str, str]:
        return self._env

    def run(
        self, service: str, args: Sequence[str], timeout_s: float, stdin: bytes | None = None
    ) -> StageResult:
        """段階を1つ起動して終わりを待つ。timeout_s を超えたら止め、そのコンテナを消す。

        stdin は標準入力に渡すバイト列（② ④ の入力）。None なら何も渡さない。
        """
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
            stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            out, err = proc.communicate(stdin, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            # SIGTERM で docker の CLI は終わるが、コンテナは動き続ける（実機で確認）。ラベルで消す
            proc.terminate()
            try:
                out, err = proc.communicate(timeout=STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                out, err = b"", b""
            return StageResult(None, _text(out), _text(err), not self._remove_quietly())
        if proc.returncode != 0:
            # CLI が異常終了しても（SIGKILL など）、コンテナは残りうる
            return StageResult(proc.returncode, _text(out), _text(err), not self._remove_quietly())
        return StageResult(proc.returncode, _text(out), _text(err))

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


def _text(data: bytes) -> str:
    # ホストのロケールによらず UTF-8 で読む。壊れたバイト列でも止めない（中身は parse が確かめる）
    return data.decode("utf-8", errors="replace")


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
    except (OSError, UsageError) as e:  # ロックのファイル・fork・env_file の写しなど
        return _usage_error(e, environ)


def _usage_error(e: Exception, environ: Mapping[str, str], prog: str = "tr-run") -> int:
    # 誤って秘匿値を設定した変数の値などが、そのまま cron の出力に残らないようにする
    scanner = SecretScanner(known_secret_values(environ))
    print(scanner.mask(f"{prog}: {e}"), file=sys.stderr)
    return EXIT_USAGE


def _parser() -> argparse.ArgumentParser:
    # 過去の期間を指定する引数は作らない（US-E6-05）。略記（--dry など）も受け付けない
    parser = argparse.ArgumentParser(prog="tr-run", allow_abbrev=False)
    parser.add_argument("cadence", choices=sorted(_TARGETS))
    parser.add_argument("--dry-run", action="store_true", help="⑤ 投稿を飛ばす（D-22）")
    return parser


def check_environment(environ: Mapping[str, str], repo: Path) -> tuple[Path, dict[str, str]]:
    """データ用フォルダと、compose に渡す環境変数（許可リスト）を返す。

    worker.env・agent.env の場所・権限と書式もここで確かめる（中身を環境変数として読み込まない。
    D-05）。
    """
    data = data_dir(environ, repo).resolve()  # リポジトリの外であることも確かめる（D-22）
    if not data.is_dir():
        raise UsageError(f"TR_DATA_DIR のフォルダがない（先に作る）: {data}")
    env = {name: environ[name] for name in COMPOSE_ENV if environ.get(name)}
    env["TR_DATA_DIR"] = str(data)
    secrets = secrets_dir(environ, repo, data)
    for name in ENV_FILES:  # 書式もロックの前に確かめる（実行では写しを読み直す）
        secret_values(secrets / name)
    env[SECRETS_DIR_ENV] = str(secrets)
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
    *,
    observation: bool = False,
) -> Job:
    run_id = new_run_id(datetime.now(JST), dry_run=dry_run, observation=observation)
    files = RunFiles(data, run_id)
    days = config.daily_period_days if cadence == "daily" else config.weekly_period_days
    target = _TARGETS[cadence](files.started_on, days)
    # compose にはこの実行の間だけの写しを読ませ、そのエラー文（段階の stderr）に出る値も伏せる
    try:
        copy = snapshot(Path(compose_env[SECRETS_DIR_ENV]), run_id)
    except ValueError as e:
        raise UsageError(str(e)) from None
    try:
        env_secrets = [
            value
            for name in ENV_FILES
            for value in secret_values(copy / name, snapshot_label(name))
        ]
    except ValueError as e:
        remove_snapshot(copy)
        raise UsageError(str(e)) from None
    compose_env = {**compose_env, SECRETS_DIR_ENV: str(copy)}
    scanner = SecretScanner(known_secret_values(environ) + env_secrets)
    return Job(
        repo=repo,
        config=config,
        files=files,
        target=target,
        compose=Compose(repo, compose_env, files.run_id),
        log=RunLog(files.log, files.run_id, cadence, target.key, scanner),
        scanner=scanner,
    )


def supervise(job: Job, run: Callable[[Job], int] | None = None) -> int:
    """ジョブを動かし、終わったら（止めたときも）env_file の写しを消す（envfiles.py）。"""
    try:
        return _supervise(job, run)
    finally:
        remove_snapshot(Path(job.compose.env[SECRETS_DIR_ENV]))


def _supervise(job: Job, run: Callable[[Job], int] | None) -> int:
    """ジョブを子プロセスで動かし、ジョブ全体の上限を超えたら止める（structure.md 6.4）。

    run は子プロセスで動かすジョブの本体（既定は run_job。観測は observe.py の本体）。
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
            code = (run or run_job)(job)
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
        partial = failures.partial_source_names(job.files.dir, names)
        return _fail(job, "collect", category, {"partial_sources": partial})
    fields = {
        "items": collected["items"],
        "failed_sources": collected["failed_sources"],
        "partial_sources": failures.partial_source_names(job.files.dir, names),
    }
    category = collect_failure(collected["sources"], len(collected["failed_sources"]))
    if category is not None:  # 1つも取得できなかった日（US-E6-03・US-E6-04）
        job.log.write("collect", "failure", error_category=category)
        return _fail(job, "collect", category, fields)
    category, models, retries = _analyze(job)
    if category is not None:
        return _fail(job, "analyze", category, fields, models, retries)
    model_args = ["--model", ",".join(models)] if models else []
    verified, category = _worker(job, "verify", job.limits.verify, _verify_summary, model_args)
    if verified is None:
        return _fail(job, "verify", category, fields)
    if verified["result"] == REJECTED:  # 不合格（US-E7-02）。不合格の後に再生成はしない
        outcome = failures.Outcome(failures.REJECTED, category=_validation_reason(job))
        posting = failures.handle(job, outcome, MINUTE)
        return job.summary("rejected", **fields, **_publish_failure(job, posting))
    if verified["result"] == SECRET_DETECTED:  # 本文を公開しない（US-E7-05）
        outcome = failures.Outcome(failures.SECRET_STOP, "verify", SECRET_DETECTED)
        posting = failures.handle(job, outcome, MINUTE)
        return job.summary(
            "error",
            failed_stage="verify",
            error_category=SECRET_DETECTED,
            **fields,
            **_publish_failure(job, posting),
        )
    if job.files.dry_run:  # ドライランは ⑤ だけを飛ばす（D-04）
        return job.summary("dry_run", **fields)
    return _publish_report(job, fields, retries)


def _publish_report(job: Job, fields: Mapping, retries: int) -> Summary:
    """⑤ でレポートを投稿する。worker が runs を閉じる（成功・公開停止・失敗）。"""
    posted, category = _post(job, post.REPORT, retries=retries)
    if posted is None:  # worker が動かなかった・結果を読めなかった。runs が閉じていなければ閉じる
        failures.close_run(
            job, failures.Outcome(failures.RUN_ERROR, "post", category, retries=retries), MINUTE
        )
        return job.summary("error", failed_stage="post", error_category=category, **fields)
    if posted["run_close_failed"]:  # runs を閉じられなかった。元の分類で閉じ直す
        published = posted["result"] in (post.CREATED, post.EXISTING)
        category = "run_close_failed" if published else posted["error_category"]
        failures.close_run(
            job,
            failures.Outcome(failures.RUN_ERROR, "post", category, retries=retries),
            MINUTE,
        )
        if published:  # Issue は公開済み。runs の終わりを書けなかったことを実行エラーにする
            return job.summary(
                "error",
                failed_stage="post",
                error_category="run_close_failed",
                issue_url=posted["issue_url"],
                **fields,
            )
    if posted["result"] in (post.CREATED, post.EXISTING):
        return job.summary("report", issue_url=posted["issue_url"], **fields)
    # 秘匿情報の検出（⑤ で公開停止の定型を作った）か、作れなかった
    return job.summary(
        "error",
        failed_stage="post",
        error_category=posted["error_category"] or "post_failed",
        issue_url=posted["issue_url"],
        issue_post_failed=posted["write_error"],
        **fields,
    )


def _publish_failure(job: Job, posting: failures.Posting | None) -> dict:
    """失敗の Issue（不合格・実行エラー・公開停止）を ⑤ で投稿する。結果の要約は変えず、作った
    Issue の URL だけを足す。投稿できなくても元の結果のまま（ログには worker が残す）。"""
    if posting is None or job.files.dry_run:
        return {}
    posted, category = _post(job, posting.kind, posting.stopped)
    if posted is None:
        return {"issue_post_failed": category}
    out = {"issue_url": posted["issue_url"]}
    if posted["result"] == post.FAILED or posted["issue_url"] is None:
        out["issue_post_failed"] = (
            posted["write_error"] or posted["error_category"] or "post_failed"
        )
    return out


def _post(
    job: Job, kind: str, stopped: str | None = None, retries: int | None = None
) -> tuple[dict | None, str | None]:
    extra = ["--kind", kind]
    if stopped is not None:
        extra += ["--stopped", stopped]
    if retries is not None:
        extra += ["--retry-count", str(retries)]
    return _worker(job, "post", job.limits.post, _post_summary, extra)


def _fail(
    job: Job,
    stage: str,
    category: str | None,
    fields: Mapping | None = None,
    models: tuple[str, ...] = (),
    retries: int = 0,
) -> Summary:
    """日次・週次の ① 〜 ③ の実行エラー：④ の調査・本文・本番の runs の終わりを行い、要約にする。"""
    assert category is not None
    posting = failures.handle(
        job, failures.Outcome(failures.RUN_ERROR, stage, category, models, retries), MINUTE
    )
    fields = {**(fields or {}), **_publish_failure(job, posting)}
    return job.summary("error", failed_stage=stage, error_category=category, **(fields or {}))


def _validation_reason(job: Job) -> str | None:
    """③ の不合格の理由（validation.json の reason。runs の error_category に残す）。"""
    reason = failures.load_validation(job.files.dir).get("reason")
    return reason if isinstance(reason, str) and CATEGORY.fullmatch(reason) else None


def _agent_call(cadence: str) -> tuple[str, object, dict]:
    """② の呼び出し：（スキル、出力のスキーマ、観測が0件のとき AI を呼ばずにコードが作る出力）。"""
    if cadence == "daily":
        return invoke.DAILY_SKILL, DAILY_SCHEMA, {"items": []}
    empty = {"themes": [], "tech_changes": [], "comparisons": [], "watch": []}
    return invoke.WEEKLY_SKILL, WEEKLY_SCHEMA, empty


def _analyze(job: Job) -> tuple[str | None, tuple[str, ...], int]:
    """② を呼び、構造化出力を agent-output.json に保存する。失敗の分類・使われたモデル・行った
    再試行の回数を返す。

    再試行（structure.md 6.3）：出力がなく、分類が一時的な失敗（invoke.TRANSIENT）のときだけ、
    limits.analyze_retries 回まで、再試行を含めた合計の上限（limits の analyze）の残りの時間の中で
    行う。上限の時間の超過・利用上限・認証の失敗・出力がスキーマに合わない（③ の不合格）は
    再試行しない。再試行は ③ で不合格になった後の再生成ではない（US-E7-02）。

    ② が失敗したときも、結果の JSON にモデルが出ていれば返す（失敗の分岐が runs に記録する）。

    structured_output はそのまま保存し、形の検査は ③ の D6(a) で行う（違反は全体の不合格。
    D-04、D-07・US-E7-02）。

    入力は worker が書いた agent-input.json。上限を超えたら AI を呼ばない（D-21）。ファイルの
    大きさを読み込む前に確かめ、読み込んだ後も標準入力に渡すバイト列で確かめる（worker は同じ
    形の JSON で書くので、2つは同じ大きさになる）。日次は収集結果が0件の日も AI を呼ばず、項目の
    ない出力をコードが作る（③ が「該当なし」の本文にする。report-templates.md 1.5）。週次は、
    今週の観測が0件でも、過去のテーマか過去の観測があれば呼ぶ（沈静のテーマを出すため）。どれも
    なければ呼ばず、空の出力をコードが作る。
    AI を呼んだら、成否によらず1回分の使用量・所要時間・モデル・入力量をログ（usage）に残す。
    """
    job.log.write("analyze", "start")
    skill, schema, empty = _agent_call(job.cadence)
    path = job.files.dir / AGENT_INPUT
    limit = job.limits.agent_input_bytes
    try:
        size = path.stat().st_size
        if size > limit:  # 読み込む前に止める
            raise InputTooLarge(f"② の入力が {size} バイトで、上限 {limit} を超えた")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError("② の入力に items の配列がない")
        body = encode_input(data, limit)
    except InputTooLarge as e:
        job.log.write("analyze", "failure", error_category="input_too_large", detail=str(e))
        return "input_too_large", (), 0
    except (OSError, ValueError) as e:  # worker が入力を書けていない・壊れている
        job.log.write("analyze", "failure", error_category="bad_input", exc=e)
        return "bad_input", (), 0
    models: tuple[str, ...] = ()
    retries = 0
    # 週次は、今週の観測が0件でも、過去のテーマ・観測があれば呼ぶ（沈静のテーマを出すため）
    call = bool(data["items"] or data.get("past_themes") or data.get("past_observations"))
    if call:
        began = _now()
        total = job.limits.analyze * MINUTE
        while True:
            started = time.monotonic()
            try:
                result = invoke.call_agent(
                    body,
                    max(total - (_now() - began), 0.0),  # 再試行は合計の上限の残りの中で行う
                    runner=job.compose,
                    skill=skill,
                    schema=schema,
                    max_turns=job.config.limits.agent_max_turns,
                    max_input_bytes=limit,
                )
            except invoke.AgentError as e:
                if e.category not in invoke.NOT_CALLED:  # claude を動かさずに終わった失敗
                    _log_usage(job, data, body, started, e.models, e.usage)
                # 先に切るとトークンの頭が欠けて検出できなくなるので、マスクしてから末尾を残す
                detail = job.scanner.mask(e.detail)[-STDERR_TAIL_CHARS:]
                job.log.write("analyze", "failure", error_category=e.category, detail=detail)
                if e.remove_failed:
                    job.log.write("analyze", "failure", error_category="container_remove_failed")
                if (
                    e.category in invoke.TRANSIENT
                    and not e.remove_failed
                    and retries < job.config.limits.analyze_retries
                    and total - (_now() - began) >= RETRY_MIN_MINUTES * MINUTE
                ):
                    retries += 1
                    job.log.write(
                        "analyze", "retry", error_category=e.category, attempt=retries + 1
                    )
                    continue
                # 結果の JSON にモデルが出ていれば、runs に記録する
                return e.category, e.models, retries
            except OSError as e:  # docker を起動できない
                job.log.write("analyze", "failure", error_category="launch_failed", exc=e)
                return "launch_failed", (), retries
            except Exception as e:
                # AI を呼んだ後の想定外の失敗。値の分からない記録でも、1回の呼び出しとして残す
                _log_usage(job, data, body, started, (), None)
                job.log.write("analyze", "failure", error_category="agent_failed", exc=e)
                return "agent_failed", (), retries
            break
        _log_usage(job, data, body, started, result.models, result.usage)
        output, models = result.output, result.models
        if not models and not job.files.dry_run:
            # 結果の JSON の形が変わったなど。モデルの変化を見逃すので知らせる（structure.md 3.3）
            job.log.write("analyze", "warning", error_category="model_unknown")
    else:
        output = empty
    try:
        _write_output(job, output)  # 形が崩れていれば ③ が D6(a) で全体を不合格にする（D-07）
    except (OSError, TypeError, ValueError) as e:  # 書けない、または JSON にできない値
        job.log.write("analyze", "failure", error_category="output_write_failed", exc=e)
        return "output_write_failed", (), retries
    if call:
        job.log.write("analyze", "end", count=len(data["items"]))
    else:
        job.log.write("analyze", "end", count=0, detail="分析する材料が0件なので AI を呼ばない")
    return None, models, retries


def _log_usage(
    job: Job,
    data: dict,
    body: bytes,
    started: float,
    models: Sequence[str],
    usage: invoke.AgentUsage | None,
) -> None:
    """AI の1回の呼び出しの記録（structure.md 3.3・3.8 の7・8）。値はコードが作る名前と数だけ。

    count は渡した項目の数、input_bytes は標準入力に渡したバイト数、wall_ms はコンテナの起動を
    含めた所要時間。ほかは Claude Code の結果の JSON の値（なければ unknown）。
    """
    wall_ms = round((time.monotonic() - started) * 1000)
    detail = (
        f"model={','.join(models) or 'unknown'} input_bytes={len(body)} wall_ms={wall_ms} "
        + (usage or invoke.AgentUsage()).fields()
    )
    job.log.write("analyze", "usage", count=len(data["items"]), detail=detail)


def _write_output(job: Job, output: object) -> None:
    """② の出力（外部由来の文を含みうる）はファイルにだけ置く。中身の検査は ③ で行う。"""
    partial = job.files.dir / f"{AGENT_OUTPUT}.partial"
    partial.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
    partial.replace(job.files.dir / AGENT_OUTPUT)


def _worker(
    job: Job,
    stage: str,
    minutes: int,
    parse: Callable[[str], dict | None],
    extra: Sequence[str] = (),
) -> tuple[dict | None, str | None]:
    """worker の段階を1つ動かし、要約を parse で読んで返す。失敗なら分類を返す。

    extra は段階の CLI に足す引数（③ に渡す使われたモデルの名前など）。
    """
    args = [stage, job.cadence, "--run-id", job.files.run_id, *extra]
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
    items, sources, failed = data.get("items"), data.get("sources"), data.get("failed_sources")
    if not (_count(items) and _count(sources)):
        return None
    if not isinstance(failed, list) or not all(isinstance(s, str) and s in names for s in failed):
        return None
    if len(set(failed)) != len(failed) or len(failed) > sources:
        return None
    return {"items": items, "sources": sources, "failed_sources": [names[s] for s in failed]}


def _verify_summary(stdout: str) -> dict | None:
    """③ の要約の結果の種別を読む。知らない種別なら None（件数はログと validation.json にある）。"""
    data = _last_json(stdout)
    if data is None or data.get("result") not in VERIFY_RESULTS:
        return None
    return {"result": data["result"]}


_ISSUE_REPO = re.compile(r"[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+")


def _post_summary(stdout: str) -> dict | None:
    """⑤ の要約を読む。形が違えば None。issue_url は repo・number から作る（worker の文字列を
    そのまま出さない）。error_category は分類の名前の形のときだけ使う。"""
    data = _last_json(stdout)
    if data is None or data.get("result") not in post.RESULTS:
        return None
    url = None
    repo, number = data.get("repo"), data.get("number")
    if repo is not None or number is not None:
        if not (
            isinstance(repo, str)
            and _ISSUE_REPO.fullmatch(repo)
            and not {".", ".."} & set(repo.split("/"))
            and _count(number)
            and number > 0
        ):
            return None
        url = f"https://github.com/{repo}/issues/{number}"
    if data["result"] in (post.CREATED, post.EXISTING) and url is None:
        return None  # 作った・あった Issue の番号がない
    category = data.get("error_category")
    if category is not None and not (isinstance(category, str) and CATEGORY.fullmatch(category)):
        return None
    write_error = data.get("write_error")
    if write_error is not None and not (
        isinstance(write_error, str) and CATEGORY.fullmatch(write_error)
    ):
        return None
    return {
        "result": data["result"],
        "error_category": category,
        "issue_url": url,
        "write_error": write_error,
        "run_close_failed": data.get("run_close_failed") is True,
    }


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
