"""D-13 の観測の入口（一時的。日次の運用を始めるときに消す。#48）。

D-13 の「PoC での調整のしかた」の1：レポートを出さずに、① 収集だけを毎朝 6:00（JST）に動かし、
Star 数と人気型フィードの取得記録をためる。`bin/tr-run` は使わず、運用サーバーの cron から
`bin/tr-observe` で起動する。新しい製品用の引数は足さない（引数は受け付けない）。

- ロックは日次と同じ（`${TR_DATA_DIR}/locks/daily.lock`）。観測と日次を同時に動かさない
- 実行 ID の先頭は `obs-`。worker の ① が runs を開き、終わりに `observed`（すべての情報源の取得に
  失敗した日と、① が失敗したときは `failed`）と終了時刻を書く（cli.py）。取得結果（取得の失敗も）
  は source_fetches に、収集項目は items に残す
- 環境の確かめ方・compose に渡す環境変数・上限時間・止め方・要約の出し方は、統括スクリプト
  （orchestrator.py）と同じものを使う。上限で止めたときなど、worker が runs を閉じられなかった
  実行は running のまま残る（data-model.md 3.4。後片付けはしない）

終了コード：0 は観測を終えた、1 は実行エラー、2 は起動の誤り、3 は日次か観測が動いている。
"""

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from tech_researcher.collect.sources import source_names
from tech_researcher.config import ConfigError, load_config
from tech_researcher.orchestrator import (
    CONFIG,
    EXIT_BUSY,
    EXIT_FAILED,
    EXIT_OK,
    Busy,
    Job,
    Summary,
    UsageError,
    _emit,
    _log_quietly,
    _new_job,
    _stage_summary,
    _usage_error,
    _worker,
    check_environment,
    lock,
    supervise,
)
from tech_researcher.runfiles import run_files
from tech_researcher.stages.collect import collect_failure

OBSERVED = "observed"
LOCK = "daily"  # 日次と同じロック（D-13）


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] = os.environ) -> int:
    argparse.ArgumentParser(prog="tr-observe", allow_abbrev=False).parse_args(argv)
    repo = Path.cwd()
    try:
        data, compose_env = check_environment(environ, repo)
        config = load_config(repo / CONFIG)
    except (UsageError, ConfigError, ValueError, OSError) as e:
        return _usage_error(e, environ)
    try:
        with lock(data / "locks" / f"{LOCK}.lock"):
            job = _new_job(
                repo, data, config, "daily", False, compose_env, environ, observation=True
            )
            return supervise(job, run_observation)
    except Busy:
        print(f"tr-observe: 日次か観測（{LOCK}）が動いている", file=sys.stderr)
        return EXIT_BUSY
    except OSError as e:
        return _usage_error(e, environ)


def run_observation(job: Job) -> int:
    """① だけを動かし、要約を出して終了コードを返す（子プロセスで動く）。"""
    try:
        with run_files(job.files.data_dir, job.files.run_id):
            job.log.write("job", "start")
            summary = _observe(job)
            job.log.write("job", "end")
    except Exception as e:
        _log_quietly(job, "job", "failure", error_category=type(e).__name__, exc=e)
        summary = job.summary("error", error_category="internal_error")
    _emit(job, summary)
    return EXIT_OK if summary.result == OBSERVED else EXIT_FAILED


def _observe(job: Job) -> Summary:
    names = source_names(job.config)
    collected, category = _worker(
        job, "collect", job.limits.collect, lambda out: _stage_summary(out, names)
    )
    if collected is None:
        return job.summary("error", failed_stage="collect", error_category=category)
    fields = {"items": collected["items"], "failed_sources": collected["failed_sources"]}
    category = collect_failure(collected["sources"], len(collected["failed_sources"]))
    if category is not None:
        return job.summary("error", failed_stage="collect", error_category=category, **fields)
    return job.summary(OBSERVED, **fields)


if __name__ == "__main__":
    sys.exit(main())
