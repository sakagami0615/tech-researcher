import json
from datetime import UTC, date, datetime

import pytest

from tech_researcher.logging import RunLog, without_raw
from tech_researcher.validate.secret_scan import SecretScanner

KNOWN = "dummy-known-secret-1"
TOKEN = "ghp" + "_" + "x" * 36  # テスト用のダミー。実行時に組み立てる


@pytest.fixture
def log(tmp_path):
    return RunLog(
        tmp_path / "logs" / "2026" / "10" / "20261024-060000-abcd.jsonl",
        "20261024-060000-abcd",
        "daily",
        date(2026, 10, 23),
        SecretScanner([KNOWN]),
        now=lambda: datetime(2026, 10, 23, 21, 0, 5, tzinfo=UTC),
    )


def lines(log):
    return [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]


def test_writes_json_lines_with_common_fields(log):
    log.write("job", "start")
    log.write("collect", "source_result", source="Zenn", count=12)
    log.write("collect", "retry", source="GitHub", attempt=2, error_category="rate_limited")
    log.write("verify", "excluded", count=1, detail="入力にない ID ['i9']")
    log.write("job", "end")
    first, *rest = lines(log)
    assert first == {
        "time": "2026-10-24T06:00:05+09:00",
        "run_id": "20261024-060000-abcd",
        "cadence": "daily",
        "target": "2026-10-23",
        "stage": "job",
        "event": "start",
    }
    assert [(r["stage"], r["event"]) for r in rest] == [
        ("collect", "source_result"),
        ("collect", "retry"),
        ("verify", "excluded"),
        ("job", "end"),
    ]
    assert rest[0]["source"] == "Zenn" and rest[0]["count"] == 12
    assert rest[1]["attempt"] == 2 and rest[1]["error_category"] == "rate_limited"
    assert rest[2]["detail"] == "入力にない ID ['i9']"


def test_secrets_are_masked_before_writing(log):
    log.write("collect", "failure", source="GitHub", detail=f"token={TOKEN} key={KNOWN}")
    text = log.path.read_text(encoding="utf-8")
    assert TOKEN not in text and KNOWN not in text
    assert lines(log)[0]["detail"] == "token=[MASKED:github_token] key=[MASKED:known]"


def test_exception_is_kept_masked_and_separated_from_issue_fields(log):
    try:
        raise RuntimeError(f"401 with {TOKEN}")
    except RuntimeError as e:
        written = log.write("collect", "failure", error_category="auth", exc=e)
    record = lines(log)[0]
    assert record == written
    assert "Traceback" in record["exception"] and "RuntimeError" in record["exception"]
    assert TOKEN not in record["exception"] and "[MASKED:github_token]" in record["exception"]
    safe = without_raw(record)
    assert "exception" not in safe and safe["error_category"] == "auth"


@pytest.mark.parametrize("stage, event", [("unknown", "start"), ("job", "unknown")])
def test_unknown_stage_or_event_is_rejected(log, stage, event):
    with pytest.raises(ValueError):
        log.write(stage, event)
    assert not log.path.exists()
