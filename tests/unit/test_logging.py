import json
from datetime import UTC, date, datetime

import pytest

from tech_researcher.logging import RunLog, issue_fields
from tech_researcher.validate.secret_scan import SecretScanner

KNOWN = "dummy-known-secret-1"
TRICKY = 'dummy\\known"secret'  # バックスラッシュと引用符を含む既知の値
TOKEN = "ghp" + "_" + "x" * 36  # テスト用のダミー。実行時に組み立てる


@pytest.fixture
def log(tmp_path):
    return RunLog(
        tmp_path / "logs" / "2026" / "10" / "20261024-060000-abcd.jsonl",
        "20261024-060000-abcd",
        "daily",
        date(2026, 10, 23),
        SecretScanner([KNOWN, TRICKY]),
        now=lambda: datetime(2026, 10, 23, 21, 0, 5, tzinfo=UTC),
    )


def lines(log):
    return [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]


def test_writes_json_lines_with_common_fields(log):
    log.write("job", "start")
    log.write("collect", "source_result", source="Zenn", count=12)
    log.write("collect", "retry", source="GitHub", attempt=2, error_category="rate_limited")
    log.write("verify", "excluded", count=1, detail="入力にない ID ['i9']")
    log.write("post", "secret_detected")
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
        ("post", "secret_detected"),
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


def test_exception_chain_is_kept_masked(log):
    try:
        try:
            raise KeyError(TRICKY)  # repr でエスケープされた形で残る
        except KeyError:
            try:
                raise OSError(f"{KNOWN}")  # __context__ として残る
            except OSError as e:
                raise RuntimeError(f"401 with {TOKEN}") from e
    except RuntimeError as e:
        written = log.write("collect", "failure", error_category="auth", exc=e)
    text = log.path.read_text(encoding="utf-8")
    record = lines(log)[0]
    assert record == written
    assert "KeyError" in record["exception"] and "RuntimeError" in record["exception"]
    for leaked in (TOKEN, KNOWN, "known\\", 'known"', "known\\\\"):
        assert leaked not in text and leaked not in record["exception"]
    assert record["exception"].count("[MASKED:") == 3


def test_issue_fields_keep_only_allowed_fields(log):
    try:
        raise RuntimeError("boom")
    except RuntimeError as e:
        record = log.write(
            "collect",
            "failure",
            source="GitHub",
            error_category="auth",
            count=0,
            attempt=3,
            detail="外部由来の文",
            exc=e,
        )
    assert set(issue_fields(record)) == {
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


@pytest.mark.parametrize("stage, event", [("unknown", "start"), ("job", "unknown")])
def test_unknown_stage_or_event_is_rejected(log, stage, event):
    with pytest.raises(ValueError):
        log.write(stage, event)
    assert not log.path.exists()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"detail": {"token": TOKEN}},
        {"detail": [TOKEN]},
        {"source": 1},
        {"error_category": b"auth"},
        {"count": "1"},
        {"count": True},
        {"attempt": 1.0},
        {"exc": "not an exception"},
    ],
)
def test_wrong_types_are_rejected_before_opening_the_file(log, kwargs):
    with pytest.raises(TypeError):
        log.write("collect", "failure", **kwargs)
    assert not log.path.exists()


def test_whole_line_is_masked_again(tmp_path):
    # 実行 ID のように値ごとのマスクを通らない項目にも、1行全体のマスクが効く
    log = RunLog(
        tmp_path / "a.jsonl", f"run-{TOKEN}", "weekly", date(2026, 10, 19), SecretScanner()
    )
    log.write("job", "start")
    assert TOKEN not in log.path.read_text(encoding="utf-8")


def test_unknown_cadence_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        RunLog(tmp_path / "a.jsonl", "obs-1", "observe", date(2026, 10, 23), SecretScanner())
