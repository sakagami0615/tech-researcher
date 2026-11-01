"""last30days-skill の agent JSON の契約テスト（D-01・D-23、structure.md 8.3、data-model.md 2.8）。

記録した出力のテストは常に流す（CI でも流す）。実際の CLI を呼ぶテストはマーカー network で分け、
last30days-skill の固定 SHA を更新したときに手で流す（bin/setup-third-party.sh の後に）。
"""

import json
import subprocess
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tech_researcher.collect.aggregate import aggregate
from tech_researcher.collect.last30days import (
    SCRIPT,
    ContractError,
    collect_last30days,
    parse_agent_json,
)
from tech_researcher.collect.period_filter import in_period
from tech_researcher.config import Last30DaysSource, Subquery, load_config
from tech_researcher.periods import JST, daily_target

REPO = Path(__file__).parents[2]
LAST30DAYS = REPO / "third_party" / "last30days"
# 情報源と計画は明示する。設定ファイルの値を運用で変えても、テストが壊れないように
CONFIG = replace(
    load_config(REPO / "config" / "tech-researcher.toml"),
    hackernews=Last30DaysSource(True, "Hacker News"),
    github_search=Last30DaysSource(True, "GitHub"),
    subqueries=(Subquery("LLM", "LLM news", ("hackernews", "github"), 1.0),),
)
NOW = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)
TARGET = daily_target(date(2026, 11, 2), 1)  # 対象日 11/01。UTC の 10/31・11/01 が期間内


def collect(data):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, json.dumps(data), "")

    period = TARGET.period
    return collect_last30days(
        CONFIG, period, LAST30DAYS, github_token="t", run=run, now=lambda: NOW
    )


def test_recorded_output_becomes_records(agent_json):
    hn, gh = collect(agent_json)
    assert (hn.source_id, hn.error, gh.source_id, gh.error) == (
        "hackernews",
        None,
        "github_search",
        None,
    )
    article, ask = hn.records
    assert article.url == "https://blog.example.com/model" and article.link_url is None
    assert article.published_at == datetime(2026, 10, 31, tzinfo=UTC)
    assert article.published_precision == "date" and article.fetched_at == NOW
    assert [e.type for e in article.events] == ["published"]
    assert article.excerpt is None  # タイトルと同じ要約は捨てる
    assert {(a.metric, a.value) for a in article.attention} == {
        ("hn_points", 120),
        ("hn_comments", 45),
    }
    assert ask.excerpt == "Top comment excerpt"
    (issue,) = gh.records
    assert issue.meta_terms == ("example/agent",)
    assert {a.metric for a in issue.attention} == {"github_reactions", "github_comments"}


def test_records_pass_period_filter(agent_json):
    # UTC の日付だけの記事は、UTC の T-1・T を期間内とみなす（D-16）
    agent_json["results"][1]["published_at"] = "2026-10-29"
    records = [r for result in collect(agent_json) for r in result.records]
    kept = [i.url for i in aggregate(records) if in_period(i, TARGET)]
    assert kept == ["https://blog.example.com/model", "https://github.com/example/agent/issues/7"]


def test_missing_optional_fields_are_accepted(agent_json):
    for key in ("published_at", "summary", "engagement", "cluster"):
        del agent_json["results"][0][key]
    record = collect(agent_json)[0].records[0]
    assert record.published_precision == "unknown" and record.events == ()
    assert record.excerpt is None and record.attention == ()


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(schema_version="1.5"),
        lambda d: d.update(comparison=True),
        lambda d: d.pop("results"),
        lambda d: d.update(source_status=["hackernews"]),
        lambda d: d.update(source_status={"hackernews": 1}),
        lambda d: d["results"][0].pop("url"),
        lambda d: d["results"][0].update(title=None),
        lambda d: d["results"][0].update(published_at="2026-10-31T01:00:00Z"),
        lambda d: d["results"][0].update(published_at="2026-02-30"),
        lambda d: d["results"][0].update(engagement=[1]),
        lambda d: d["results"][0].update(summary=1),
    ],
)
def test_mismatched_output_is_rejected(agent_json, change):
    change(agent_json)
    with pytest.raises(ContractError):
        parse_agent_json(json.dumps(agent_json))
    # 収集としては、両方の情報源の失敗にする（再試行しない）
    assert all(r.error and r.attempts == 1 and not r.records for r in collect(agent_json))


def test_empty_title_is_accepted(agent_json):
    agent_json["results"][1]["title"] = ""
    assert collect(agent_json)[0].records[1].title == ""


def test_top_level_must_be_an_object():
    with pytest.raises(ContractError, match="オブジェクト"):
        parse_agent_json("[]")


def test_comparison_shape_is_rejected():
    with pytest.raises(ContractError, match="比較"):
        parse_agent_json(json.dumps({"comparison": True, "entities": ["a", "b"]}))


@pytest.mark.network
def test_real_cli_output_matches_contract():
    """実際の CLI で HN だけを集め、出力が契約に合うことを確かめる。

    GitHub Search は収集用トークンが要るので、ここでは集めない。
    """
    if not (LAST30DAYS / SCRIPT).is_file():
        pytest.fail("bin/setup-third-party.sh を実行してから流す")
    config = replace(CONFIG, github_search=None)
    target = daily_target(datetime.now(JST).date(), 1)
    (result,) = collect_last30days(config, target.period, LAST30DAYS)
    assert result.error is None, result.error
    assert all(r.published_precision == "date" for r in result.records)
