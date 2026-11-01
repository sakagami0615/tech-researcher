"""1回の実行のログ（structure.md 7.3、US-E6-06）。

JSON Lines で1行に1つの出来事を書く。書く前に、すべての文字列を秘匿情報の検出器でマスクし、
JSON にした1行全体にもう一度マスクをかける。例外のスタックトレースは `exception` に残すが、
Issue には載せない（US-E6-04）。Issue や ④ の入力に使う行は issue_fields で、外部由来の自由文を
含まない項目だけに絞る（report-templates.md 3.2、data-model.md 4.4）。
"""

import json
import traceback
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

from tech_researcher.periods import JST
from tech_researcher.validate.secret_scan import SecretScanner

CADENCES = frozenset({"daily", "weekly"})
# job は実行全体の開始・終了に使う。ほかは D-04 の ① 〜 ⑤（data-model.md の failed_stage と同じ）
STAGES = frozenset({"job", "collect", "analyze", "verify", "investigate", "post"})
EVENTS = frozenset(
    {"start", "end", "retry", "failure", "source_result", "excluded", "secret_detected"}
)
# Issue や ④ の入力に使ってよい項目（許可リスト）。detail・exception は外部由来の文を含みうる
ISSUE_FIELDS = frozenset(
    {
        "time",
        "run_id",
        "cadence",
        "target",
        "stage",
        "event",
        "source",
        "error_category",
        "count",
        "attempt",
    }
)


class RunLog:
    def __init__(
        self,
        path: Path,
        run_id: str,
        cadence: str,
        target: date,
        scanner: SecretScanner,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(JST),
    ):
        if cadence not in CADENCES:
            raise ValueError(f"知らない種別: {cadence!r}")
        self.path = path
        self.run_id = run_id
        self.cadence = cadence
        self.target = target
        self._scanner = scanner
        self._now = now

    def write(
        self,
        stage: str,
        event: str,
        *,
        source: str | None = None,
        error_category: str | None = None,
        count: int | None = None,
        attempt: int | None = None,
        detail: str | None = None,
        exc: BaseException | None = None,
    ) -> dict:
        """1行を書き、書いた内容（マスク後）を返す。値が None の項目は書かない。

        型が違う値は、ファイルを開く前に TypeError にする（マスクを通らない値を書かないため）。
        """
        if stage not in STAGES:
            raise ValueError(f"知らない段階: {stage!r}")
        if event not in EVENTS:
            raise ValueError(f"知らない出来事: {event!r}")
        for name, value in (
            ("source", source),
            ("error_category", error_category),
            ("detail", detail),
        ):
            _check_type(name, value, str)
        for name, value in (("count", count), ("attempt", attempt)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise TypeError(f"{name} は整数にする: {value!r}")
        _check_type("exc", exc, BaseException)
        record = {
            "time": self._now().astimezone(JST).isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "cadence": self.cadence,
            "target": self.target.isoformat(),
            "stage": stage,
            "event": event,
            "source": source,
            "error_category": error_category,
            "count": count,
            "attempt": attempt,
            "detail": detail,
            "exception": "".join(traceback.format_exception(exc)) if exc is not None else None,
        }
        record = {
            k: self._scanner.mask(v) if isinstance(v, str) else v
            for k, v in record.items()
            if v is not None
        }
        line = self._masked_line(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return json.loads(line)

    def _masked_line(self, record: dict) -> str:
        """1行全体をもう一度マスクし、常に読める JSON を返す。

        数値が既知の値に一致すると `"count": [MASKED:known]` のように壊れるので、そのときは
        数値も文字列にしてマスクし直す。それでも壊れるとき（既知の値が JSON の区切りに
        一致するなど）は、中身を残さない固定の行にする。
        """
        line = self._scanner.mask(json.dumps(record, ensure_ascii=False))
        if _is_json(line):
            return line
        record = {k: self._scanner.mask(str(v)) for k, v in record.items()}
        line = self._scanner.mask(json.dumps(record, ensure_ascii=False))
        return line if _is_json(line) else json.dumps({"masked_line": True})


def issue_fields(record: dict) -> dict:
    """Issue や ④ の入力に使うための、許可した項目だけの行。"""
    return {k: v for k, v in record.items() if k in ISSUE_FIELDS}


def _is_json(line: str) -> bool:
    try:
        json.loads(line)
    except json.JSONDecodeError:
        return False
    return True


def _check_type(name: str, value: object, expected: type) -> None:
    if value is not None and not isinstance(value, expected):
        raise TypeError(f"{name} の型が違う: {type(value).__name__}")
