"""1回の実行のログ（structure.md 7.3、US-E6-06）。

JSON Lines で1行に1つの出来事を書く。書く前に、すべての文字列を秘匿情報の検出器でマスクする。
例外のスタックトレースは `exception` に残すが、Issue には載せない（US-E6-04）。Issue や ④ の
入力に使う行は without_raw で `exception` を落とし、コードが分類した `error_category` だけを使う。
"""

import json
import traceback
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

from tech_researcher.periods import JST
from tech_researcher.validate.secret_scan import SecretScanner

# job は実行全体の開始・終了に使う。ほかは D-04 の ① 〜 ⑤（data-model.md の failed_stage と同じ）
STAGES = ("job", "collect", "analyze", "verify", "investigate", "post")
EVENTS = ("start", "end", "retry", "failure", "source_result", "excluded")
# ログにだけ残し、Issue には載せない項目
RAW_FIELDS = ("exception",)


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
        """1行を書き、書いた内容（マスク後）を返す。値が None の項目は書かない。"""
        if stage not in STAGES:
            raise ValueError(f"知らない段階: {stage!r}")
        if event not in EVENTS:
            raise ValueError(f"知らない出来事: {event!r}")
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record


def without_raw(record: dict) -> dict:
    """Issue や ④ の入力に使うための、生ログの項目を除いた行。"""
    return {k: v for k, v in record.items() if k not in RAW_FIELDS}
