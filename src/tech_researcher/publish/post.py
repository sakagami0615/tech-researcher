"""⑤ 投稿（D-04・D-05・D-08、report-templates.md 3.6・4.3、structure.md 6.3・6.5。worker で動く）。

実行のフォルダの issue.md（③ か統括スクリプトの失敗の分岐が書いた、タイトル・ラベル・本文）を
Issue として作り、DB に記録する。Issue 用トークンと投稿先は worker.env から環境変数で読む。

種別（kind）は統括スクリプトが渡す：report・rejected・run_error・run_error_secret（data-model.md
3.4 の issues.kind）。流れ：

1. 投稿先が DB の記録と合うかを確かめる（meta。食い違えば何も書かずに止める）
2. issue.md を読み、タイトル・ラベル・本文の形を確かめる（種別とラベルの対応は 4.2）
3. 投稿の直前に、全体を秘匿情報の検査にかける（全種別）。見つかれば Issue を作らず、止めた種別を
   `blocked_secret` で記録し、公開停止の定型（3.6。処理段階は ⑤）を `run_error_secret` として作る。
   その定型でも検出するなら、何も作らず記録だけにする
4. レポート Issue は、作る前に重複判定（publish/dedup.py）を行う。同じ対象の Issue があれば作らず、
   なければ作る（410・404 の後は replaces="recreate" で記録を付け替える）。不合格・実行エラーは
   実行ごとに作り、その実行・種別の行に番号があれば作らない（data-model.md 3.7）
5. ラベルは作成の要求に含める（github_write.py）。作ったら、すぐ record_issue と record_issue_repo
6. 書き込みの失敗は、タイムアウト・通信の失敗・5xx・二次レート制限だけ、再試行（HTTP の再試行の
   上限）する。再試行の前に、レポート Issue は重複判定をやり直す（応答が届かないまま Issue が
   作られていても、ラベルで見つかる）。4xx と再試行を使い切った失敗は、issues に `failed` を記録する
7. 本番の実行がまだ閉じていなければ（レポートの投稿）、runs を閉じる：投稿・既存で succeeded、
   公開停止・失敗で failed（段階は post）

秘匿情報の検査の役割：③ と失敗の分岐（failures.py）は、本文を作る時点で検査して公開停止の定型に
置き換える。⑤ の検査は、その後の最後の砦で、検出の分類だけをログに残す（位置・文字列は残さない）。
二重にならないよう、⑤ が定型を作り直すのは、渡された本文を検出したときだけ。定型が渡されたときは、
定型として投稿する（統括スクリプトが stopped で止めた種別を知らせる）。

ドライランは ⑤ を呼ばない（D-04）。DB に書き込むので、本番の実行だけ。
"""

import sqlite3
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from tech_researcher.config import Config, ConfigError
from tech_researcher.db.issues import KINDS, find_issue, record_issue
from tech_researcher.db.meta import check_issue_repo, record_issue_repo
from tech_researcher.db.runs import CATEGORY, finish_run
from tech_researcher.logging import RunLog
from tech_researcher.periods import Target
from tech_researcher.publish.dedup import Create, Existing, Skipped, check_duplicate_from_env
from tech_researcher.publish.github_issues import (
    IssueReader,
    IssueReadError,
    IssueRef,
    issue_repo,
    issue_token,
    parse_marker,
)
from tech_researcher.publish.github_write import (
    TITLE_MAX,
    CreatedIssue,
    IssueWriteError,
    IssueWriter,
)
from tech_researcher.render.common import Report, body_too_long
from tech_researcher.render.failure import SECRET_NOTICE, secret_stop_report
from tech_researcher.stages.daily import ISSUE, issue_text
from tech_researcher.validate.secret_scan import SecretScanner

REPORT = "report"
REJECTED = "rejected"
RUN_ERROR = "run_error"
RUN_ERROR_SECRET = "run_error_secret"
SEPARATOR = "\n\n---\n\n"  # stages.daily.issue_text の区切り
_LABEL_TYPES = {
    REPORT: "type:report",
    REJECTED: "type:rejected",
    RUN_ERROR: "type:run-error",
    RUN_ERROR_SECRET: "type:run-error",
}

# 結果（要約の result）
CREATED = "created"  # Issue を作った
EXISTING = (
    "existing"  # すでにある（同じ対象のレポート Issue、または同じ実行の Issue）ので作らなかった
)
BLOCKED = "blocked"  # 秘匿情報を検出して公開を止めた（定型を作った、または記録だけ）
FAILED = "failed"  # 作れなかった
RESULTS = frozenset({CREATED, EXISTING, BLOCKED, FAILED})


class BadIssue(Exception):
    """issue.md の形が違う（コードの不具合。投稿しない）。"""


def is_secret_stop(report: Report) -> bool:
    """公開停止の定型（report-templates.md 3.6）か。"""
    return report.body.startswith(SECRET_NOTICE)


def parse_issue(text: str) -> Report:
    """issue.md（stages.daily.issue_text の形）を、タイトル・ラベル・本文に戻す。"""
    head, found, body = text.partition(SEPARATOR)
    title, _, labels = head.partition("\n")
    if not found or not title or not labels or "\n" in labels:
        raise BadIssue("issue.md の形が違う")
    return Report(title, body, tuple(labels.split(", ")))


def expected_labels(kind: str, cadence: str) -> frozenset[str]:
    """種別ごとのラベル（D-08、report-templates.md 4.2）。"""
    return frozenset({_LABEL_TYPES[kind], f"cadence:{cadence}"})


def check_report(report: Report, kind: str, target: Target, run_id: str) -> None:
    """投稿する前の形の検査。種別とラベルの対応、タイトルと本文の長さ、レポートの識別子。"""
    if kind not in KINDS:
        raise BadIssue(f"知らない種別 {kind}")
    if len(report.labels) != 2 or set(report.labels) != expected_labels(kind, target.cadence):
        raise BadIssue("種別とラベルが合わない")
    if len(report.title) > TITLE_MAX or body_too_long(report.body):
        raise BadIssue("タイトルか本文が長すぎる")
    marker = parse_marker(report.body)
    if kind == REPORT:
        if marker is None or (marker.cadence, marker.target, marker.run_id) != (
            target.cadence,
            target.key,
            run_id,
        ):
            raise BadIssue("レポートの識別子が実行と合わない")
    elif marker is not None:
        raise BadIssue("レポート以外に識別子がある")


class Poster:
    """1回の実行の ⑤。依存（DB・ログ・GitHub のクライアント・時計）は引数で受け取る。"""

    def __init__(
        self,
        config: Config,
        target: Target,
        run_id: str,
        run_dir: Path,
        log: RunLog,
        scanner: SecretScanner,
        conn: sqlite3.Connection,
        *,
        environ: Mapping[str, str],
        retry_count: int | None = None,
        now: datetime | None = None,
        make_writer: Callable[..., IssueWriter] = IssueWriter,
        make_reader: Callable[..., IssueReader] = IssueReader,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        deadline: float | None = None,
    ):
        self.config = config
        self.target = target
        self.run_id = run_id
        self.run_dir = run_dir
        self.log = log
        self.scanner = scanner
        self.conn = conn
        self.environ = environ
        self.retry_count = retry_count
        self.now = now or datetime.now(UTC)
        self.make_writer = make_writer
        self.make_reader = make_reader
        self.sleep = sleep
        self.clock = clock
        self.deadline = deadline  # 要求を出してよい時間の上限（clock の値。structure.md 6.4）
        self.repo = ""
        self.writer: IssueWriter | None = None

    def run(self, kind: str, stopped: str | None = None) -> dict:
        """issue.md を投稿して記録し、結果（result・kind・repo・number・error_category）を返す。

        stopped は、すでに公開を止めた種別（③ が秘匿情報を検出したときの report など。kind は定型の
        run_error_secret）。その種別を `blocked_secret` で記録する。想定外の例外は呼び出し側
        （cli）で段階の失敗にする。
        """
        self.log.write("post", "start")
        try:
            self.repo = issue_repo(self.environ)
            self.writer = self.make_writer(
                issue_token(self.environ), self.config.limits, clock=self.clock
            )
            self.writer.deadline = self.deadline
            check_issue_repo(self.conn, self.repo)
        except (IssueReadError, IssueWriteError, ConfigError) as e:
            return self._stop(kind, _category(e, "config"))
        try:
            report = parse_issue((self.run_dir / ISSUE).read_text(encoding="utf-8"))
            check_report(report, kind, self.target, self.run_id)
        except (OSError, UnicodeDecodeError, BadIssue) as e:
            self.log.write("post", "failure", error_category="bad_issue", exc=e)
            return self._stop(kind, "bad_issue")
        if stopped is not None and kind == RUN_ERROR_SECRET:
            self._record_blocked(stopped)
        found = self._secret_kinds(report)
        if found is None:
            return self._post(kind, report)
        # 投稿の直前の検査で検出した。渡された本文は公開しない
        self.log.write("post", "secret_detected", error_category="secret_detected", detail=found)
        self._record_blocked(kind)
        if kind != RUN_ERROR_SECRET:
            stop = secret_stop_report(self.target, "post")
            if self._secret_kinds(stop) is None:
                posted = self._post(RUN_ERROR_SECRET, stop, close=False)
                self._close("failed", "secret_detected")
                if posted["result"] in (CREATED, EXISTING):
                    return {**posted, "result": BLOCKED, "error_category": "secret_detected"}
                # 定型の投稿に失敗した。要約の分類は検出のまま、書き込みの分類は別に残す
                write_error = posted.get("error_category")
                return {**posted, "error_category": "secret_detected", "write_error": write_error}
            self._record_blocked(RUN_ERROR_SECRET)
        # 定型でも検出した。作らず、記録だけにする
        self._close("failed", "secret_detected")
        return _result(BLOCKED, RUN_ERROR_SECRET, error_category="secret_detected")

    def _secret_kinds(self, report: Report) -> str | None:
        """検出した分類（秘匿値そのものは含まない）。なければ None。"""
        kinds = ",".join(sorted({f.kind for f in self.scanner.find(issue_text(report))}))
        return kinds or None

    def _post(self, kind: str, report: Report, *, close: bool = True) -> dict:
        """重複を確かめて Issue を作り、記録して、runs を閉じる（close が True のとき）。"""
        if kind != REPORT:
            row = find_issue(self.conn, self.run_id, kind)
            if row is not None and row.state == "created":  # 同じ実行の再試行。作らない
                ref = IssueRef(str(row.repo), int(row.number or 0))
                return self._existing(kind, ref, close, "db")
        try:
            found = self._create(kind, report)
        except IssueReadError as e:
            return self._write_failed(kind, _category(e, "read_failed", prefix="dedup_"), close)
        except IssueWriteError as e:
            self.log.write("post", "failure", error_category=e.category, detail=str(e))
            return self._write_failed(kind, _category(e, "write_failed"), close)
        except (sqlite3.Error, OSError) as e:
            self.log.write("post", "failure", error_category="db_error", exc=e)
            return self._write_failed(kind, "db_error", close)
        if isinstance(found, Existing):
            return self._existing(kind, found.ref, close, found.via)
        created, replaces = found
        try:
            record_issue(
                self.conn,
                self.run_id,
                kind,
                self.target.cadence,
                self.target.key,
                "created",
                self.now,
                repo=created.ref.repo,
                number=created.ref.number,
                replaces=replaces,
            )
            self._record_repo(created.ref, "created")
        except Exception as e:  # Issue はある。次の重複判定がラベルで見つける（report）
            self.log.write("post", "failure", error_category="issue_record_failed", exc=e)
            if close:
                self._close("failed", "issue_record_failed")
            return _result(FAILED, kind, ref=created.ref, error_category="issue_record_failed")
        if (
            created.missing_labels
        ):  # 権限が足りないなどでラベルが付かなかった（ラベル探索が効かない）
            self.log.write(
                "post",
                "warning",
                error_category="labels_missing",
                count=len(created.missing_labels),
            )
        if close:
            self._close("succeeded", None)
        self.log.write("post", "end", count=1)
        return _result(CREATED, kind, ref=created.ref, labels_missing=bool(created.missing_labels))

    def _existing(self, kind: str, ref: IssueRef, close: bool, via: str) -> dict:
        """すでにある Issue。投稿先の記録（meta）が済んでいることも確かめてから、成功にする。"""
        try:
            self._record_repo(ref, via)
        except Exception as e:  # 記録できないまま成功にしない（次の実行でやり直す）
            self.log.write("post", "failure", error_category="issue_record_failed", exc=e)
            if close:
                self._close("failed", "issue_record_failed")
            return _result(FAILED, kind, ref=ref, error_category="issue_record_failed")
        if close:
            self._close("succeeded", None)
        self.log.write("post", "end", detail=f"existing via {via}")
        return _result(EXISTING, kind, ref=ref)

    def _record_repo(self, ref: IssueRef, via: str) -> None:
        """設定の投稿先を meta に記録し、記録されたことを読み直して確かめる。転送された Issue
        以外で、Issue の repo が設定の投稿先と違えば、記録しない（失敗にする）。"""
        if ref.repo.lower() != self.repo and via != "transferred":
            raise ValueError("Issue の repo が設定の投稿先と違う")
        record_issue_repo(self.conn, self.repo)
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'issue_repo'").fetchone()
        if row is None or row[0] != self.repo:
            raise RuntimeError("投稿先を記録できていない")

    def _create(self, kind: str, report: Report) -> Existing | tuple[CreatedIssue, str | None]:
        """重複判定（レポートだけ）の後に作る。再試行の前には、重複判定をやり直す。"""
        assert self.writer is not None
        limits = self.config.limits
        waits = limits.http_retry_waits_s
        replaces: str | None = None
        for attempt in range(len(waits) + 1):
            if kind == REPORT:
                decision = self._decide()
                if isinstance(decision, Existing):
                    return decision
                replaces = decision.replaces
            try:
                created = self.writer.create_issue(
                    self.repo, report.title, report.body, report.labels
                )
                return created, replaces
            except IssueWriteError as e:
                if kind == REPORT and e.category == "invalid_response":
                    # 応答が読めないが、Issue は作られていることがある。重複判定を1回だけ行う
                    again = self._decide()
                    if isinstance(again, Existing):
                        return again
                    raise
                if not e.retryable or attempt == len(waits):
                    raise
                if kind != REPORT and not e.unsent:
                    raise  # 識別子がなく、再試行で2件作りうる（応答が返らない失敗）
                wait = waits[attempt]
                if e.retry_after is not None:
                    wait = min(e.retry_after, limits.retry_after_max_s)
                if self.deadline is not None and self.clock() + wait >= self.deadline:
                    raise  # 待った後に要求する時間が残らない
                self.log.write("post", "retry", error_category=e.category, attempt=attempt + 2)
                self.sleep(wait)
        raise AssertionError("ここには来ない")

    def _decide(self) -> Existing | Create:
        decision = check_duplicate_from_env(
            self.conn,
            self.config.limits,
            self.run_id,
            self.target.cadence,
            self.target.key,
            self.now,
            environ=self.environ,
            make_reader=self.make_reader,
            deadline=self.deadline,
            clock=self.clock,
        )
        assert not isinstance(decision, Skipped)  # 本番は Skipped にならない
        return decision

    def _write_failed(self, kind: str, category: str, close: bool) -> dict:
        """作れなかった。issues に failed を記録し、runs を failed（post）で閉じる。"""
        self._record_failed(kind)
        if close:
            self._close("failed", category)
        return _result(FAILED, kind, error_category=category)

    def _record_failed(self, kind: str) -> None:
        try:
            record_issue(
                self.conn,
                self.run_id,
                kind,
                self.target.cadence,
                self.target.key,
                "failed",
                self.now,
            )
        except Exception as e:
            self.log.write("post", "failure", error_category="issue_record_failed", exc=e)

    def _stop(self, kind: str, category: str) -> dict:
        """投稿の準備で止まった失敗（設定・トークン・issue.md）。何も作らず、runs だけ閉じる。"""
        self.log.write("post", "failure", error_category=category)
        self._record_failed(kind)
        self._close("failed", category)
        return _result(FAILED, kind, error_category=category)

    def _record_blocked(self, kind: str) -> None:
        """止めた種別を blocked_secret で記録する（作った Issue の行は書き換わらない）。"""
        try:
            record_issue(
                self.conn,
                self.run_id,
                kind,
                self.target.cadence,
                self.target.key,
                "blocked_secret",
                self.now,
            )
        except Exception as e:  # 記録できなくても、公開は止まっている
            self.log.write("post", "failure", error_category="issue_record_failed", exc=e)

    def _close(self, status: str, category: str | None) -> None:
        """まだ閉じていない（running の）実行だけを閉じる。失敗は記録して続ける。"""
        try:
            row = self.conn.execute(
                "SELECT status FROM runs WHERE run_id = ?", (self.run_id,)
            ).fetchone()
            if row is None or row[0] != "running":
                return
            finish_run(
                self.conn,
                self.run_id,
                status,
                datetime.now(UTC),
                failed_stage="post" if status == "failed" else None,
                error_category=category,
                retry_count=self.retry_count,
            )
        except Exception as e:
            self.log.write("post", "failure", error_category="run_close_failed", exc=e)


def _category(error: Exception, fallback: str, prefix: str = "") -> str:
    """ログと DB に入れてよい分類の名前（形に合わなければ fallback）。"""
    value = f"{prefix}{getattr(error, 'category', fallback)}"
    return value if CATEGORY.fullmatch(value) else fallback


def _result(
    result: str,
    kind: str,
    *,
    ref: IssueRef | None = None,
    error_category: str | None = None,
    labels_missing: bool = False,
) -> dict:
    out: dict = {"result": result, "kind": kind}
    if ref is not None:
        out |= {"repo": ref.repo, "number": ref.number}
    if error_category is not None:
        out["error_category"] = error_category
    if labels_missing:
        out["labels_missing"] = True
    return out
