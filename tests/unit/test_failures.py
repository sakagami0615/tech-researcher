"""日次の失敗の分岐の部品（failures.py）。一巡の検査は test_daily_flow.py。"""

import json
import tomllib
from pathlib import Path

import pytest

from tech_researcher import failures
from tech_researcher.config import parse_config
from tech_researcher.render.failure import SourceResult

ROOT = Path(__file__).parents[2]
LIMITS = (ROOT / "config" / "tech-researcher.toml").read_text(encoding="utf-8").split("[limits]")[1]
CONFIG = parse_config(
    tomllib.loads(
        "[daily]\nperiod_days = 1\n[weekly]\nperiod_days = 7\n"
        '[[feeds]]\nid = "example"\ndisplay_name = "Example"\n'
        'url = "https://blog.example.com/feed.xml"\nkind = "new"\nenabled = true\n'
        f"[limits]{LIMITS}"
    )
)


def entry(source_id, status="ok", category=None, attempts=1, parts=()):
    return {
        "source_id": source_id,
        "status": status,
        "category": category,
        "attempts": attempts,
        "parts": [{"name": n, "category": c} for n, c in parts],
    }


def test_load_fetches_ignores_missing_and_broken_files(tmp_path):
    assert failures.load_fetches(tmp_path) == []
    (tmp_path / "fetches.json").write_text("{not json")
    assert failures.load_fetches(tmp_path) == []
    (tmp_path / "fetches.json").write_text(json.dumps([{"source_id": "a"}]))
    assert failures.load_fetches(tmp_path) == []  # 形が違う
    good = [entry("a", "failed", "dns", 3, [("x/y", "timeout")])]
    (tmp_path / "fetches.json").write_text(json.dumps(good))
    assert failures.load_fetches(tmp_path) == good


def test_partial_sources_use_display_names_and_skip_whole_failures(tmp_path):
    fetches = [
        entry("github_releases", "ok", None, 2, [("a/b", "timeout")]),
        entry("rss:example", "failed", "dns", 3),
        entry("qiita"),
    ]
    fetches.append(entry("github_releases", "failed", "timeout", 1, [("a/b", "timeout")]))
    (tmp_path / "fetches.json").write_text(json.dumps(fetches))
    names = {"github_releases": "GitHub Releases", "rss:example": "Example", "qiita": "Qiita"}
    assert failures.partial_source_names(tmp_path, names) == ["GitHub Releases"]


@pytest.mark.parametrize(
    ("stage", "category", "expected"),
    [
        ("collect", "all_sources_failed", "すべての情報源の取得に失敗した（名前解決の失敗）"),
        ("collect", "no_sources", "有効な情報源がない（設定の誤り）"),
        ("collect", "exit_1", "段階が異常終了した（終了コード 1）"),
        ("collect", "timeout", "段階の上限の時間を超えた"),
        ("analyze", "agent_failed", "AI の呼び出しに失敗した"),
        ("analyze", "input_too_large", "AI への入力が上限を超えた"),
        ("verify", "bad_output", "段階の結果を読めなかった"),
        ("collect", "RuntimeError", "想定外のエラーで失敗した"),  # 例外の型の名前は本文に出さない
    ],
)
def test_error_summary_is_a_fixed_sentence(stage, category, expected):
    fetches = [entry("a", "failed", "dns", 3)]
    assert failures.error_summary(stage, category, fetches) == expected


def test_error_summary_names_the_reason_only_when_it_is_common():
    mixed = [entry("a", "failed", "dns", 3), entry("b", "failed", "timeout", 3)]
    assert (
        failures.error_summary("collect", "all_sources_failed", mixed)
        == "すべての情報源の取得に失敗した"
    )


def test_source_results_use_parts_and_ignore_the_partial_mark_of_ok_rows():
    names = {"a": "A", "github_repos": "GitHub（新着・急上昇）", "r": "R"}
    rows = failures.source_results(
        [
            entry("a", "failed", "http_5xx", 3),
            entry("github_repos", "ok", None, 30),  # 試行回数は要求の回数なので再試行に数えない
            entry("r", "ok", None, 1, [("x/y", "truncated")]),
            entry("n", "no_results"),
        ],
        names,
    )
    assert rows == [
        SourceResult("A", False, "HTTP 5xx", 2),
        SourceResult("GitHub（新着・急上昇）", True, None, 0),
        SourceResult("R（x/y）", False, "取りこぼしたおそれ", 0),
        SourceResult("n", True, None, 0),
    ]


def test_retries_per_stage():
    fetches = [entry("a", "failed", "dns", 3), entry("github_repos", "failed", "dns", 40)]
    # ① は失敗した情報源の再試行（github_repos は要求の回数なので除く）。上限は設定から
    assert failures.retries(CONFIG, "collect", fetches) == (2, 2)
    assert failures.retries(CONFIG, "collect", [entry("a", "failed", "dns", 9)]) == (2, 2)
    assert failures.retries(CONFIG, "collect", []) == (0, 2)
    # 要求の総数を試行回数にする情報源は、再試行に数えない
    assert failures.retries(CONFIG, "collect", [entry("qiita", "failed", "dns", 3)]) == (0, 2)
    # ② は行った再試行の回数（上限は structure.md 6.3）。③ は再試行しない
    assert failures.retries(CONFIG, "analyze", fetches) == (0, 1)
    assert failures.retries(CONFIG, "analyze", fetches, 1) == (1, 1)
    assert failures.retries(CONFIG, "analyze", fetches, 5) == (1, 1)
    assert failures.retries(CONFIG, "verify", fetches) == (0, 0)


def test_counts_read_the_weekly_comparison_rows_separately():
    counts = failures._counts(
        {
            "counts": {
                "collected": 9,
                "ai_output": 4,
                "excluded_by_reason": {"invalid_id": 1},
                "excluded_rows_by_reason": {"unknown_product": 2, "x": "bad"},
                "published": 3,
            }
        }
    )
    assert counts.excluded == {"D6(b) ID が存在しない": 1}
    assert counts.excluded_rows == {"週次の比較の製品名が収集した項目にない": 2, "x": 0}
    assert failures._counts({"counts": {}}).excluded_rows == {}  # 日次は持たない


def test_shrink_sources_halves_the_table_until_the_body_fits(monkeypatch):
    from tech_researcher.render.common import Report

    monkeypatch.setattr("tech_researcher.render.common.BODY_LIMIT", 100)
    built = []

    def build(keep):
        built.append(keep)
        return Report("t", "x" * (keep * 10 + 20), ())

    report = failures.shrink_sources(40, build)
    assert report is not None and len(report.body) <= 100
    assert built == [20, 10, 5]  # 5 行（70 字）で収まる
    # 0 行でも収まらなければ None（調査結果を載せない定型にする）
    assert failures.shrink_sources(3, lambda keep: Report("t", "x" * 500, ())) is None
