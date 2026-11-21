"""コマンドの入口（structure.md 2.2）。worker の各段階を1つずつ起動する。

統括スクリプト（#16）が `docker compose run --rm -T worker <段階> …` で呼ぶ（D-04）。段階は
`collect`（①）と `verify`（③、日次だけ）。実行 ID と
実行のフォルダは統括スクリプトが用意し、段階は実行 ID の日付から対象と期間を決める。
① は本番の DB を開き、本番の実行では runs の開始を書いてから、取得結果と収集項目を書く。
ドライランは DB を読み取り専用で開き、何も書かない（D-22）。① で実行エラーと分かった本番の実行
（すべての情報源の取得に失敗した日と、① の例外）は、runs を failed と終了時刻で閉じる。D-13 の観測の
実行（実行 ID の先頭が obs-。日次の ① だけ）は、それ以外なら observed で閉じる。通常の日次・週次の
② 以降の終わりは、その失敗の分岐と ⑤ を実装する Issue で書く。③ は、本番では統括スクリプトから
渡された ② のモデルの名前を runs の model に記録し、前回の本番から変わっていればログで知らせる
（structure.md 3.3。ドライランは記録せず比べない）。本番の ③ は、結果（レポート案・掲載と除外の
項目・検証結果）を reports・report_entries に保存する（ドライランは書かない）。
標準出力には、コードが作る要約（件数・情報源の ID・結果の種別）だけを JSON で出す。外部由来の文と
例外の中身は出さず、ログ（秘匿情報をマスクする）にだけ残す（structure.md 3.2・7.3）。
相対パスは作業ディレクトリ（ホストではリポジトリ、worker では /app）から解く。

終了コード：0 は段階が終わった（一部の情報源の失敗を含む）、1 は段階の途中の失敗、
2 は起動の誤り（引数・設定・TR_DATA_DIR・実行のフォルダ）。③ は不合格・秘匿情報の検出のときも
0 で終わり、要約の result で結果を返す。
"""

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing, nullcontext, suppress
from datetime import UTC, datetime
from pathlib import Path

from tech_researcher.agent.invoke import split_models
from tech_researcher.config import Config, ConfigError, load_config
from tech_researcher.db.readonly import open_readonly
from tech_researcher.db.runs import finish_run, record_model, start_run
from tech_researcher.db.schema import open_db
from tech_researcher.logging import RunLog
from tech_researcher.periods import Target, daily_target, weekly_target
from tech_researcher.runfiles import RunFiles, data_dir
from tech_researcher.stages.collect import collect_failure, run_collect
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
        if files.dry_run:
            return run_verify(config, target, files.run_id, files.dir, log, scanner)
        if args.model is not None:
            _record_model(files, args.model, log)
        with closing(open_db(files.db)) as db:  # 本番は結果を DB に保存する
            return run_verify(config, target, files.run_id, files.dir, log, scanner, db)
    conn = open_readonly(files.db) if files.dry_run else open_db(files.db)

    def daily_input() -> None:
        if args.cadence == "daily" and not files.observation:  # 週次の ② の入力は週次の Issue で
            write_daily_input(config, target, files.dir)

    with closing(conn) if conn is not None else nullcontext():
        if files.dry_run:
            summary = run_collect(config, target, files.dir, log, db=conn, write_db=False)
            # 本番と同じく、実行エラーの日は ② に進まないので ② の入力を作らない
            if collect_failure(summary["sources"], len(summary["failed_sources"])) is None:
                daily_input()
            return summary
        assert conn is not None
        start_run(conn, files.run_id, target, files.started_at)
        return _collect(config, target, files, log, conn, daily_input)


def _collect(
    config: Config,
    target: Target,
    files: RunFiles,
    log: RunLog,
    conn: sqlite3.Connection,
    then: Callable[[], None],
) -> dict:
    """本番の ①。① で実行エラーと分かったら、runs を failed（collect）で閉じる。

    then は ① の続きの処理（日次の ② の入力を書く）で、その失敗も ① の例外として扱う。
    1つも取得できなかった日（collect_failure）と、① の例外が実行エラーになる（統括スクリプトの
    判定と同じ）。観測（obs-）は ① で終わるので、それ以外なら observed で閉じる。通常の日次・週次は
    ② 以降に進むので、ここでは閉じない（② 以降の失敗と成功の終わりは、その分岐と ⑤ の Issue で
    書く）。
    """
    try:
        summary = run_collect(config, target, files.dir, log, db=conn, write_db=True)
        category = collect_failure(summary["sources"], len(summary["failed_sources"]))
        if category is None:  # 実行エラーの日は ② に進まないので、② の入力も作らない
            then()
    except Exception as e:
        with suppress(Exception):  # 閉じられなくても、元の失敗を伝える（running のまま残る）
            _finish(conn, files.run_id, type(e).__name__)
        raise
    if category is not None or files.observation:
        _finish(conn, files.run_id, category)
    return summary


def _finish(conn: sqlite3.Connection, run_id: str, category: str | None) -> None:
    """① で終わる実行を閉じる。category があれば failed（collect）、なければ observed（観測）。"""
    finish_run(
        conn,
        run_id,
        "observed" if category is None else "failed",
        datetime.now(UTC),
        failed_stage=None if category is None else "collect",
        error_category=category,
    )


def _record_model(files: RunFiles, model: str, log: RunLog) -> None:
    """本番の ② で使われたモデルを runs に記録し、前回の本番から変わっていればログで知らせる。

    運用者はその警告を見て、AI の回帰テスト（D-07）を流す。記録は補助の機能なので、失敗しても
    ③ は止めず、警告（model_record_failed）を残して検査を続ける。
    """
    try:
        with closing(open_db(files.db)) as conn:
            previous = record_model(conn, files.run_id, model)
    except Exception as e:  # DB の誤り（SchemaError を含む）・行がないなど。中身はマスクしてログへ
        log.write("analyze", "warning", error_category="model_record_failed", exc=e)
        return
    if previous is not None and previous != model:
        log.write(
            "analyze",
            "warning",
            error_category="model_changed",
            detail=f"前回の本番 {previous} から {model} に変わった。AI の回帰テスト（D-07）を流す",
        )


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
    verify.add_argument(
        "--model", help="② で使われたモデルの名前（`,` でつなぐ）。本番では runs に記録する"
    )
    return parser


def _prepare(
    args: argparse.Namespace, environ: Mapping[str, str]
) -> tuple[RunFiles, Config, Target]:
    files = RunFiles(data_dir(environ, Path.cwd()), args.run_id)
    if getattr(args, "model", None) is not None:
        split_models(args.model)  # 形が違えば起動の誤り（ログと DB に外部の文を入れない）
    if files.observation and (args.stage, args.cadence) != ("collect", "daily"):
        raise _UsageError("観測（obs-）の実行は日次の ① だけ")
    if not files.dir.is_dir():
        raise _UsageError(f"実行のフォルダがない（統括スクリプトが作る）: {files.dir}")
    config = load_config(CONFIG)
    days = config.daily_period_days if args.cadence == "daily" else config.weekly_period_days
    return files, config, _TARGETS[args.cadence](files.started_on, days)
