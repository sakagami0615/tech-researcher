import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from tech_researcher.collect.records import Event, Record, SourceResult
from tech_researcher.config import Feed, load_config
from tech_researcher.logging import RunLog
from tech_researcher.periods import daily_target
from tech_researcher.stages.collect import COLLECTED, run_collect
from tech_researcher.validate.secret_scan import SecretScanner

CONFIG = load_config(Path(__file__).parents[3] / "config" / "tech-researcher.toml")
TARGET = daily_target(date(2026, 10, 24), 1)  # 2026-10-23 0:00〜10-24 0:00（JST）
FETCHED = datetime(2026, 10, 23, 21, 0, tzinfo=UTC)
IN = datetime(2026, 10, 23, 3, 0, tzinfo=UTC)  # JST 12:00
OLD = datetime(2026, 10, 21, 3, 0, tzinfo=UTC)
TOKEN = "ghp" + "_" + "x" * 36  # テスト用のダミー。実行時に組み立てる


def feed(feed_id, enabled=True):
    return Feed(feed_id, feed_id, f"https://{feed_id}.example.com/rss", "new", enabled)


def record(source_id, url, published):
    return Record(
        source_id=source_id,
        url=url,
        title="外部のタイトル",
        published_precision="datetime",
        fetched_at=FETCHED,
        events=(Event("published", published),),
        published_at=published,
    )


def fake_collect(f, limits):
    match f.id:
        case "a":
            return SourceResult(
                "rss:a",
                (
                    record("rss:a", "https://a.example.com/1", IN),
                    record("rss:a", "https://a.example.com/old", OLD),
                ),
                1,
            )
        case "b":
            # a と同じ記事（URL の正規化でまとまる）
            return SourceResult("rss:b", (record("rss:b", "https://a.example.com/1/", IN),), 1)
        case "c":
            return SourceResult("rss:c", (), 3, "再試行を使い切った：HTTP 503")
        case "d":
            raise RuntimeError("想定外 " + TOKEN)
    raise AssertionError(f"呼ばれるはずのない情報源 {f.id}")


def run(tmp_path, feeds):
    log = RunLog(
        tmp_path / "log.jsonl", "dry-20261024-060000-abcd", "daily", TARGET.key, SecretScanner()
    )
    config = replace(CONFIG, feeds=feeds)
    return run_collect(config, TARGET, tmp_path, log, collect=fake_collect), log


def test_writes_items_in_period_and_returns_counts_only(tmp_path):
    feeds = (feed("a"), feed("b"), feed("c"), feed("off", enabled=False))
    summary, log = run(tmp_path, feeds)

    assert summary == {"items": 1, "sources": 3, "failed_sources": ["rss:c"]}
    items = json.loads((tmp_path / COLLECTED).read_text(encoding="utf-8"))
    assert [i["url"] for i in items] == ["https://a.example.com/1"]
    assert [s["source_id"] for s in items[0]["sources"]] == ["rss:a", "rss:b"]
    assert items[0]["events"][0] == {"type": "published", "at": IN.isoformat(), "basis": None}

    lines = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
    assert [(x["event"], x.get("source"), x.get("count")) for x in lines] == [
        ("start", None, None),
        ("source_result", "rss:a", 2),
        ("source_result", "rss:b", 1),
        ("source_result", "rss:c", 0),
        ("end", None, 1),
    ]
    assert lines[3]["error_category"] == "fetch_failed"
    assert lines[3]["attempt"] == 3
    assert "error_category" not in lines[1]


def test_no_enabled_source_writes_empty_result(tmp_path):
    summary, _ = run(tmp_path, (feed("off", enabled=False),))
    assert summary == {"items": 0, "sources": 0, "failed_sources": []}
    assert json.loads((tmp_path / COLLECTED).read_text(encoding="utf-8")) == []


def test_unexpected_error_fails_only_that_source(tmp_path):
    summary, log = run(tmp_path, (feed("d"), feed("a")))

    assert summary == {"items": 1, "sources": 2, "failed_sources": ["rss:d"]}
    assert (tmp_path / COLLECTED).exists()
    assert not (tmp_path / f"{COLLECTED}.partial").exists()
    lines = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
    failure, result = lines[1:3]
    assert (failure["event"], failure["source"], failure["error_category"]) == (
        "failure",
        "rss:d",
        "RuntimeError",
    )
    assert TOKEN not in failure["exception"]
    assert (result["event"], result["detail"]) == ("source_result", "RuntimeError")
