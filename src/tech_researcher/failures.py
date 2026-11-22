"""統括スクリプトの失敗の分岐（structure.md 3.3・6.3・7.4、data-model.md 3.4・4.4、
report-templates.md 3章）。

日次の ① 〜 ③ の実行エラー・不合格・秘匿情報の検出（公開停止）のあとに、次を行う。
（公開停止にした本文の runs は、元の結果のまま閉じる。公開停止はログの `post` の
`secret_detected` に残す。issues の `blocked_secret` に残すのは ⑤ で行う。data-model.md 3.4）

1. ④ 失敗時の調査を1回だけ呼ぶ（agent/investigate.py。再試行しない）。呼ばないのは、② がイメージに
   スキルがない・docker を起動できない・利用上限・認証で失敗したとき（AI を利用できない。D-04）と、
   秘匿情報の検出（3.6 の定型は調査結果を載せない）のとき。調査の入力は、この実行のログ・
   ③ の結果・直近の実行の履歴（本番の DB を読み取り専用で開く。ドライランも同じ。開けなければ
   履歴なしで続ける）
2. 本文を作る（render/failure.py）：AI の調査あり・AI を利用できないときの定型（3.5）・公開停止
   （3.6）。作った本文の全体も秘匿情報の検査にかけ、見つかれば公開停止の定型にする
3. 本文を issue.md に、検査を通った ④ の出力を investigation.json に書く（実行のフォルダ。
   ドライランは出力として残り、本番は ⑤ が使う）
4. 本番だけ、runs を worker の `finish` で閉じる（failed・rejected、終了時刻、④ の出力、
   ② が失敗したときのモデル）。DB に書くのは worker だけ（D-04）。ドライランは DB に書かない。
   ① の worker がすでに閉じた実行（取得の全失敗・① の例外）は、worker の finish が終わりの値を
   上書きせず、再試行回数（worker は書かない）と ④ の出力だけを補う

後始末の失敗（④ の失敗、本文を書けない、runs を閉じられない）は実行の結果を変えない。ログに残し、
要約は元の失敗のまま出す。⑤ の投稿（Issue を作ること）はここでは行わない。投稿する種別（本文を
置き換えて公開停止の定型にしたときは、止めた種別も）を Posting で返し、統括スクリプトが ⑤ を呼ぶ。
"""

import json
import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from tech_researcher.agent import investigate, invoke
from tech_researcher.agent.investigate import INVESTIGATION, Failure, InvestigateResult
from tech_researcher.collect.sources import GITHUB_REPOS_SOURCE_ID, source_names
from tech_researcher.config import Config
from tech_researcher.db.readonly import open_readonly
from tech_researcher.db.runs import recent_runs
from tech_researcher.publish.post import REJECTED as KIND_REJECTED
from tech_researcher.publish.post import RUN_ERROR as KIND_RUN_ERROR
from tech_researcher.publish.post import RUN_ERROR_SECRET, is_secret_stop
from tech_researcher.render.common import Report, body_too_long, fetch_reason
from tech_researcher.render.failure import (
    Counts,
    Investigation,
    SourceResult,
    rejected_report,
    run_error_report,
    secret_stop_report,
)
from tech_researcher.stages.collect import FETCHES
from tech_researcher.stages.daily import ISSUE, VALIDATION, issue_text
from tech_researcher.validate.secret_scan import SecretScanner

if TYPE_CHECKING:
    from tech_researcher.orchestrator import Job

RUN_ERROR = "run_error"
REJECTED = "rejected"
SECRET_STOP = "secret_stop"  # 秘匿情報の検出（3.6）
STDERR_TAIL_CHARS = 2000  # 失敗のとき、ログに残す stderr の末尾
WORKER = "worker"

# 不合格となった検証項目の表示名（検査の分類名 → 名前と ID。report-templates.md 3.3）
CHECK_LABELS = {
    "invalid_output": "D6(a) 出力の形式が不正",
    "invalid_id": "D6(b) ID が存在しない",
    "duplicate_id": "D6(b) ID が重複している",
    "invalid_source": "D1 出典が許可集合にない",
    "forbidden_link": "D2 URL・リンクを含む",
    "forbidden_html": "D3 画像・HTML を含む",
    "long_quote": "D4 長い引用",
    "missing_period_event": "期間内の出来事の欠落",
    "too_long": "文字数・要点数の超過",
    "all_excluded": "すべての項目を除外",
    "body_too_long": "本文が 60,000 文字を超えた",
    "secret_detected": "秘匿情報の検査",
}
REQUEST_COUNT_SOURCES = frozenset({GITHUB_REPOS_SOURCE_ID, "github_releases", "qiita"})
_EXIT = re.compile(r"exit_([0-9]+)")

# エラーの概要（コードが分類した短い文。report-templates.md 3.2）。例外のメッセージは入れない
_COLLECT_SUMMARIES = {
    "all_sources_failed": "すべての情報源の取得に失敗した",
    "no_sources": "有効な情報源がない（設定の誤り）",
}
_STAGE_SUMMARIES = {
    "timeout": "段階の上限の時間を超えた",
    "bad_output": "段階の結果を読めなかった",
    "launch_failed": "コンテナを起動できなかった",
    "missing_skill": "AI のイメージにスキルがない（イメージの作り直しが必要）",
    "input_too_large": "AI への入力が上限を超えた",
    "bad_input": "AI への入力を読めなかった",
    "bad_schema": "出力のスキーマを読めなかった",
    "agent_failed": "AI の呼び出しに失敗した",
    "overloaded": "AI のサービスが過負荷だった（再試行しても回復しなかった）",
    "server_error": "AI のサービスでサーバーエラーが起きた（再試行しても回復しなかった）",
    "network": "AI のサービスへの通信に失敗した（再試行しても回復しなかった）",
    "usage_limit": "AI の利用上限に達した",
    "auth": "AI の認証に失敗した",
    "output_write_failed": "AI の出力を保存できなかった",
}


@dataclass(frozen=True)
class Outcome:
    """失敗の分岐に渡す結果。kind は RUN_ERROR・REJECTED・SECRET_STOP。

    stage は実行エラー・公開停止で失敗した段階（STAGES のキー）、category は失敗の分類（コードが
    決めた名前。不合格は ③ の分類）。models は ② が失敗したとき結果の JSON に出ていたモデル。
    retries は ② が失敗したときに行った再試行の回数（Issue の「再試行回数」。6.3）。
    """

    kind: str
    stage: str | None = None
    category: str | None = None
    models: tuple[str, ...] = ()
    retries: int = 0


@dataclass(frozen=True)
class Posting:
    """⑤ に渡す投稿の指示。kind は issue.md の種別（issues.kind）。stopped は、本文を公開停止の
    定型に置き換えたときの、止めた種別（⑤ が blocked_secret で記録する）。"""

    kind: str
    stopped: str | None = None


def handle(job: Job, outcome: Outcome, minute: float) -> Posting | None:
    """失敗の後始末を行う（モジュールの説明）。例外は出さない。minute は設定の上限（分）の秒数。

    本文（issue.md）を書けたときは、⑤ で投稿するもの（Posting）を返す。書けなければ None。
    """
    posting = None
    try:
        posting = _write_issue(job, outcome, minute)
    except Exception as e:
        _quiet(job, "investigate", "failure", error_category=type(e).__name__, exc=e)
    if not job.files.dry_run:
        close_run(job, outcome, minute)
    return posting


def close_run(job: Job, outcome: Outcome, minute: float) -> None:
    """本番の runs を閉じる。失敗はログに残して続ける（すでに閉じていれば値を変えない）。"""
    try:
        _close_run(job, outcome, minute)
    except Exception as e:
        _quiet(job, "job", "failure", error_category="run_close_failed", exc=e)


def _write_issue(job: Job, outcome: Outcome, minute: float) -> Posting:
    """④ の調査・本文を作り、investigation.json・issue.md を書く（先に ④ の出力を残す）。"""
    report, result = _report(job, outcome, minute)
    if result.output is not None:
        _write(job.files.dir / INVESTIGATION, json.dumps(result.output, ensure_ascii=False))
    _write(job.files.dir / ISSUE, issue_text(report))
    kind = KIND_REJECTED if outcome.kind == REJECTED else KIND_RUN_ERROR
    if outcome.kind == SECRET_STOP:  # ③ が止めたレポート
        return Posting(RUN_ERROR_SECRET, "report")
    if is_secret_stop(report):  # 本文の検査で置き換えた（_report）
        return Posting(RUN_ERROR_SECRET, kind)
    return Posting(kind)


def partial_source_names(run_dir: Path, names: Mapping[str, str]) -> list[str]:
    """一部だけ取れなかった（取れなかった部分がある）情報源の表示名。

    全失敗の判定に使う failed_sources には入れない（US-E6-03 の部分レポート）。
    """
    return [
        names.get(e["source_id"], e["source_id"])
        for e in load_fetches(run_dir)
        if e["parts"] and e["status"] != "failed"  # すべて取れなかったときは failed（全失敗）
    ]


def load_fetches(run_dir: Path) -> list[dict]:
    """① が書いた情報源ごとの取得結果（fetches.json）。なければ・形が違えば空。"""
    try:
        data = json.loads((run_dir / FETCHES).read_text(encoding="utf-8"))
        return [
            {
                "source_id": str(e["source_id"]),
                "status": str(e["status"]),
                "category": e["category"] if isinstance(e["category"], str) else None,
                "attempts": int(e["attempts"]),
                "parts": [
                    {"name": str(p["name"]), "category": str(p["category"])} for p in e["parts"]
                ],
            }
            for e in data
        ]
    except OSError, ValueError, TypeError, KeyError:
        return []


def error_summary(stage: str, category: str, fetches: Sequence[Mapping]) -> str:
    """エラーの概要の文（コードが分類した短い文）。"""
    if stage == "collect" and category in _COLLECT_SUMMARIES:
        text = _COLLECT_SUMMARIES[category]
        reasons = {fetch_reason(f["category"]) for f in fetches if f["status"] == "failed"}
        if category == "all_sources_failed" and len(reasons) == 1:
            return f"{text}（{next(iter(reasons))}）"  # 理由が1つに決まるときだけ添える
        return text
    if category == "secret_detected":
        return "秘匿情報の可能性がある文字列を検出した"
    if found := _EXIT.fullmatch(category):
        return f"段階が異常終了した（終了コード {found.group(1)}）"
    return _STAGE_SUMMARIES.get(category, "想定外のエラーで失敗した")


def source_results(fetches: Sequence[Mapping], names: Mapping[str, str]) -> list[SourceResult]:
    """実行エラー Issue の「情報源ごとの取得結果」。内訳のある情報源は部分ごとの行にする。

    再試行回数は _retry_count。
    """
    rows = []
    for f in fetches:
        name = names.get(f["source_id"], f["source_id"])
        retries = _retry_count(f)
        if f["parts"]:
            rows += [
                SourceResult(f"{name}（{p['name']}）", False, fetch_reason(p["category"]), retries)
                for p in f["parts"]
            ]
        elif f["status"] == "failed":
            rows.append(SourceResult(name, False, fetch_reason(f["category"]), retries))
        else:
            rows.append(SourceResult(name, True, None, retries))
    return rows


def _retry_count(fetch: Mapping) -> int:
    """情報源の再試行回数（試行回数から初回を引いた数）。github_repos・github_releases・qiita の
    試行回数は要求の総数（ページ・リポジトリごと）で、再試行ではないので 0 とする。"""
    if fetch["source_id"] in REQUEST_COUNT_SOURCES:
        return 0
    return max(fetch["attempts"] - 1, 0)


def retries(
    config: Config, stage: str, fetches: Sequence[Mapping], analyzed: int = 0
) -> tuple[int, int]:
    """失敗した処理での（再試行回数、上限）。① は情報源の再試行（失敗した情報源の試行回数の
    最大から初回を引いた数。上限で切る）、② は行った再試行の回数（analyzed）。"""
    limits = config.limits
    if stage == "collect":
        limit = max(len(limits.http_retry_waits_s), limits.last30days_retries)
        used = max(
            (_retry_count(f) for f in fetches if f["status"] == "failed"),
            default=0,
        )
        return min(max(used, 0), limit), limit
    if stage == "analyze":
        return min(max(analyzed, 0), limits.analyze_retries), limits.analyze_retries
    return 0, 0


def _report(job: Job, outcome: Outcome, minute: float) -> tuple[Report, InvestigateResult]:
    """本文と ④ の結果。本文の全体の検査で公開停止にしたときは、公開停止の定型にする。"""
    target = job.target
    if outcome.kind == SECRET_STOP:
        assert outcome.stage is not None
        return secret_stop_report(target, outcome.stage), InvestigateResult(None)
    validation = load_validation(job.files.dir)
    fetches = load_fetches(job.files.dir)
    names = source_names(job.config)
    if outcome.kind == REJECTED:
        checks = tuple(validation.get("rejected_checks") or ([outcome.category or "unknown"]))
        failure = Failure(REJECTED, target, rejected_checks=checks)
    else:
        assert outcome.stage is not None and outcome.category is not None
        summary = error_summary(outcome.stage, outcome.category, fetches)
        failure = Failure(
            RUN_ERROR,
            target,
            failed_stage=outcome.stage,
            error_category=summary,
            error_code=outcome.category,
            retry_count=retries(job.config, outcome.stage, fetches, outcome.retries)[0],
        )
    result = _investigate(job, failure, validation, minute)

    def build(investigation: Investigation | None, shown: int | None = None) -> Report:
        if outcome.kind == REJECTED:
            return rejected_report(
                target,
                job.files.run_id,
                job.files.started_at,
                [CHECK_LABELS.get(c, c) for c in failure.rejected_checks or ()],
                _counts(validation),
                [
                    (CHECK_LABELS.get(name, name), state == "passed")
                    for name, state in _verification(validation).items()
                ],
                investigation,
            )
        assert failure.failed_stage is not None and failure.error_category is not None
        return run_error_report(
            target,
            job.files.run_id,
            job.files.started_at,
            failure.failed_stage,
            failure.error_category,
            retries(job.config, failure.failed_stage, fetches, outcome.retries),
            rows[:shown],
            investigation,
            0 if shown is None else len(rows) - shown,
        )

    rows = source_results(fetches, names)
    report = build(result.investigation)
    # 本文の全体も投稿前の検査にかける（report-templates.md 3.2・US-E7-05）。秘匿情報を先に見る
    kinds = _secret_kinds(job.scanner, issue_text(report))
    if kinds:
        _quiet(job, "post", "secret_detected", error_category="secret_detected", detail=kinds)
        return secret_stop_report(target, "post"), InvestigateResult(None, "secret_detected")
    if body_too_long(report.body):
        # まず取得結果の表の後ろを「ほか N 件」に縮めて収める。収まらなければ調査結果を載せずに
        # 3.5 の定型にする（結果は残さない）
        fitted = shrink_sources(len(rows), lambda keep: build(result.investigation, keep))
        if fitted is not None and outcome.kind == RUN_ERROR:
            return fitted, result
        _quiet(job, "investigate", "failure", error_category="body_too_long")
        return build(None), InvestigateResult(None, "body_too_long")
    return report, result


def shrink_sources(count: int, build: Callable[[int], Report]) -> Report | None:
    """表の行数を半分ずつ減らし、本文が上限に収まる最初の本文を返す。収まらなければ None。"""
    keep = count // 2
    while True:
        report = build(keep)
        if not body_too_long(report.body):
            return report
        if keep == 0:
            return None
        keep //= 2


def _investigate(
    job: Job, failure: Failure, validation: Mapping, minute: float
) -> InvestigateResult:
    """④ の入力の行を集めて呼び、ログに残す。失敗は結果の reason に入り、定型の本文になる。"""
    scanner = job.scanner
    lines = investigate.number_lines(
        scanner,
        _safe(job, lambda: investigate.log_lines(job.log.path)),
        investigate.verification_lines(validation) if validation else [],
        _safe(job, lambda: _history(job)),
    )
    _quiet(job, "investigate", "start")
    try:
        result = investigate.investigate(
            failure,
            lines,
            job.limits.investigate * minute,
            runner=job.compose,
            scanner=scanner,
            max_turns=job.config.limits.agent_max_turns,
            max_input_bytes=job.limits.agent_input_bytes,
        )
    except Exception as e:  # investigate は失敗を結果にするが、想定外でも定型にして続ける
        _quiet(job, "investigate", "failure", error_category="agent_failed", exc=e)
        return InvestigateResult(None, "agent_failed")
    _log_result(job, result)
    return result


def _log_result(job: Job, result: InvestigateResult) -> None:
    """④ の結果をログに残す（usage は AI を呼んだとき、成否によらず。structure.md 3.3）。"""
    if result.called:
        usage = result.usage or invoke.AgentUsage()
        detail = (
            f"model={','.join(result.models) or 'unknown'} input_bytes={result.input_bytes} "
            f"wall_ms={result.wall_ms} {usage.fields()}"
        )
        _quiet(job, "investigate", "usage", detail=detail)
    if result.reason is None:
        _quiet(job, "investigate", "end")
        return
    if result.reason == "secret_detected":  # 検出の分類だけをログに残す。AI の文は載せない
        _quiet(
            job,
            "investigate",
            "secret_detected",
            error_category="secret_detected",
            detail=",".join(result.secret_kinds),
        )
    else:
        # 先に切るとトークンの頭が欠けて検出できなくなるので、マスクしてから末尾を残す
        detail = job.scanner.mask(result.detail)[-STDERR_TAIL_CHARS:]
        _quiet(job, "investigate", "failure", error_category=result.reason, detail=detail or None)
    if result.remove_failed:
        _quiet(job, "investigate", "failure", error_category="container_remove_failed")


def _history(job: Job) -> list[str]:
    """直近の実行の履歴の行。本番の DB を読み取り専用で開く（ドライランも）。DB がなければ空。"""
    conn = open_readonly(job.files.db)
    if conn is None:
        return []
    started = job.files.started_at
    with closing(conn):
        runs = recent_runs(
            conn, started - timedelta(days=investigate.HISTORY_DAYS), started, job.files.run_id
        )
    return investigate.history_lines(runs)


def _close_run(job: Job, outcome: Outcome, minute: float) -> None:
    """本番の runs を worker の finish で閉じる（failed・rejected）。失敗はログに残して続ける。"""
    rejected = outcome.kind == REJECTED
    args = [
        "finish",
        job.cadence,
        "--run-id",
        job.files.run_id,
        "--status",
        "rejected" if rejected else "failed",
    ]
    if not rejected:
        assert outcome.stage is not None
        args += ["--failed-stage", outcome.stage]
    if outcome.category:
        args += ["--error-category", outcome.category]
    if outcome.kind in (REJECTED, SECRET_STOP):  # ③ の不合格・公開停止に再試行はない
        args += ["--retry-count", "0"]
    elif outcome.kind == RUN_ERROR and outcome.stage is not None:
        count = retries(job.config, outcome.stage, load_fetches(job.files.dir), outcome.retries)[0]
        if outcome.stage == "post":  # ⑤ の失敗でも、② の再試行の回数は残す
            count = outcome.retries
        args += ["--retry-count", str(count)]
    if outcome.models:
        args += ["--model", ",".join(outcome.models)]
    _run_worker(job, args, minute)


def _run_worker(job: Job, args: Sequence[str], minute: float) -> None:
    """runs に書く worker の段階を起動する。失敗はログに残して続ける。"""
    try:
        # DB に書くだけの短い処理なので、上限は ③ と同じにする
        result = job.compose.run(WORKER, args, job.limits.verify * minute)
    except OSError as e:
        _quiet(job, "job", "failure", error_category="run_close_failed", exc=e)
        return
    if result.returncode != 0:
        detail = job.scanner.mask(result.stderr)[-STDERR_TAIL_CHARS:]
        _quiet(job, "job", "failure", error_category="run_close_failed", detail=detail)
    if result.remove_failed:
        _quiet(job, "job", "failure", error_category="container_remove_failed")


def load_validation(run_dir: Path) -> dict:
    """③ の結果（validation.json）。なければ・形が違えば空。"""
    try:
        data = json.loads((run_dir / VALIDATION).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _counts(validation: Mapping) -> Counts:
    counts = validation.get("counts")
    counts = counts if isinstance(counts, dict) else {}
    by_reason = counts.get("excluded_by_reason")
    by_reason = by_reason if isinstance(by_reason, dict) else {}
    return Counts(
        collected=_int(counts.get("collected")),
        ai_output=_int(counts.get("ai_output")),
        excluded={CHECK_LABELS.get(str(k), str(k)): _int(v) for k, v in by_reason.items()},
        published=_int(counts.get("published")),
    )


def _verification(validation: Mapping) -> dict[str, str]:
    found = validation.get("verification")
    return {str(k): str(v) for k, v in found.items()} if isinstance(found, dict) else {}


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _secret_kinds(scanner: SecretScanner, text: str) -> str:
    return ",".join(sorted({f.kind for f in scanner.find(text)}))


def _write(path: Path, text: str) -> None:
    """途中で止まっても壊れたファイルを残さないよう、一時ファイルに書いてから置き換える。"""
    partial = path.with_name(f"{path.name}.partial")
    partial.write_text(text, encoding="utf-8")
    partial.replace(path)


def _safe(job: Job, read: Callable[[], list[str]]) -> list[str]:
    """④ の入力の行を集める。集められなければ空にして続ける（④ は行がなくても呼べる）。"""
    try:
        return read()
    except Exception as e:
        _quiet(job, "investigate", "warning", error_category="lines_unavailable", exc=e)
        return []


def _quiet(job: Job, stage: str, event: str, **fields) -> None:
    """ログに書けなくても止めない（後始末を優先する）。"""
    with suppress(Exception):
        job.log.write(stage, event, **fields)
