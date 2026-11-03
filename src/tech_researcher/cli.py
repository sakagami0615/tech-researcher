"""コマンドの入口（structure.md 2.2）。worker の各段階を1つずつ起動する。

統括スクリプト（#16）が `docker compose run --rm -T worker <段階> …` で呼ぶ（D-04）。段階は
`collect`（①）と `verify`（③、日次だけ）。実行 ID と
実行のフォルダは統括スクリプトが用意し、段階は実行 ID の日付から対象と期間を決める。
標準出力には、コードが作る要約（件数・情報源の ID・結果の種別）だけを JSON で出す。外部由来の文と
例外の中身は出さず、ログ（秘匿情報をマスクする）にだけ残す（structure.md 3.2・7.3）。
相対パスは作業ディレクトリ（ホストではリポジトリ、worker では /app）から解く。

終了コード：0 は段階が終わった（一部の情報源の失敗を含む）、1 は段階の途中の失敗、
2 は起動の誤り（引数・設定・TR_DATA_DIR・実行のフォルダ）。
"""

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from tech_researcher.config import Config, ConfigError, load_config
from tech_researcher.logging import RunLog
from tech_researcher.periods import Target, daily_target, weekly_target
from tech_researcher.runfiles import RunFiles, data_dir
from tech_researcher.stages.collect import run_collect
from tech_researcher.stages.daily import run_verify, write_daily_input
from tech_researcher.validate.secret_scan import SecretScanner, known_secret_values

CONFIG = Path("config/tech-researcher.toml")
_TARGETS = {"daily": daily_target, "weekly": weekly_target}


class _UsageError(Exception):
    pass


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] = os.environ) -> int:
    args = _parser().parse_args(argv)
    try:
        files, config, target = _prepare(args, environ)
    except (_UsageError, ConfigError, ValueError, OSError) as e:
        print(f"tech-researcher: {e}", file=sys.stderr)
        return 2
    scanner = SecretScanner(known_secret_values(environ))
    log = RunLog(files.log, files.run_id, args.cadence, target.key, scanner)
    try:
        summary = _run(args, config, target, files, log, scanner)
    except Exception as e:
        try:
            log.write(args.stage, "failure", error_category=type(e).__name__, exc=e)
        except Exception:
            pass  # ログに書けなくても、例外の中身（外部由来の文を含みうる）は stderr に出さない
        print(f"tech-researcher: {args.stage} が失敗した（ログ：{files.log}）", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "run_id": files.run_id,
                "cadence": args.cadence,
                "target": target.key.isoformat(),
                "stage": args.stage,
                **summary,
            },
            ensure_ascii=False,
        )
    )
    return 0


def _run(
    args: argparse.Namespace,
    config: Config,
    target: Target,
    files: RunFiles,
    log: RunLog,
    scanner: SecretScanner,
) -> dict:
    if args.stage == "verify":
        return run_verify(config, target, files.run_id, files.dir, log, scanner)
    summary = run_collect(config, target, files.dir, log)
    if args.cadence == "daily":  # 週次の ② の入力は、週次の Issue で足す
        write_daily_input(config, target, files.dir)
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tech-researcher")
    stages = parser.add_subparsers(dest="stage", required=True)
    collect = stages.add_parser(
        "collect",
        help="① 収集。実行のフォルダに collected.json と、日次は ② の入力 agent-input.json を書く",
    )
    collect.add_argument("cadence", choices=sorted(_TARGETS))
    collect.add_argument("--run-id", required=True, help="統括スクリプトが作った実行 ID")
    verify = stages.add_parser(
        "verify", help="③ 検証・検査。agent-output.json から本文を作り、結果を実行のフォルダに書く"
    )
    verify.add_argument("cadence", choices=["daily"])  # 週次は週次の Issue で足す
    verify.add_argument("--run-id", required=True, help="統括スクリプトが作った実行 ID")
    return parser


def _prepare(
    args: argparse.Namespace, environ: Mapping[str, str]
) -> tuple[RunFiles, Config, Target]:
    files = RunFiles(data_dir(environ, Path.cwd()), args.run_id)
    if not files.dir.is_dir():
        raise _UsageError(f"実行のフォルダがない（統括スクリプトが作る）: {files.dir}")
    config = load_config(CONFIG)
    days = config.daily_period_days if args.cadence == "daily" else config.weekly_period_days
    return files, config, _TARGETS[args.cadence](files.started_on, days)
