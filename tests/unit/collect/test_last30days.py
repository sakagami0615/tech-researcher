import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

from tech_researcher.collect.last30days import SCRIPT, child_env, collect_last30days
from tech_researcher.config import Last30DaysSource, Subquery, load_config
from tech_researcher.periods import daily_target, weekly_target

BASE = load_config(Path(__file__).parents[3] / "config" / "tech-researcher.toml")
# テストで使う値は明示する。設定ファイルの値を運用で変えても、テストが壊れないように
CONFIG = replace(
    BASE,
    hackernews=Last30DaysSource(True, "Hacker News"),
    github_search=Last30DaysSource(True, "GitHub"),
    subqueries=(
        Subquery("LLM", "LLM news", ("hackernews", "github"), 1.0),
        Subquery("repo", "new repos", ("github",), 0.5),
    ),
    limits=replace(BASE.limits, last30days_timeout_s=300, last30days_retries=1),
)
PERIOD = daily_target(date(2026, 11, 2), 1).period
ROOT = Path("/opt/last30days")
HN = {
    "source": "hackernews",
    "title": "T",
    "url": "https://example.com/a",
    "published_at": "2026-11-01",
}
GH = {"source": "github", "title": "I", "url": "https://github.com/o/r/issues/1"}


def output(status, results=(HN, GH)):
    return json.dumps({"schema_version": "1.4", "source_status": status, "results": list(results)})


OK = output({"hackernews": "ok", "github": "ok"})


class FakeRun:
    """subprocess.run の代わり。outcomes を順に返す（例外なら送出する）。"""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        self.home_was_empty = os.listdir(kwargs["env"]["HOME"]) == []
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def done(returncode=0, stdout=OK, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def collect(run, config=CONFIG, github_token="collect-token"):
    return collect_last30days(config, PERIOD, ROOT, github_token=github_token, run=run)


def test_arguments_state_plan_sources_backend_and_period():
    run = FakeRun(done())
    collect(run)
    ((args, kwargs),) = run.calls
    assert args[:3] == [sys.executable, str(ROOT / SCRIPT), "technology news"]
    assert args[3:8] == ["--emit=json", "--search", "hackernews,github", "--web-backend", "none"]
    plan = json.loads(args[args.index("--plan") + 1])
    assert plan == {
        "intent": "breaking_news",
        "freshness_mode": "strict_recent",
        "cluster_mode": "none",
        "subqueries": [
            {
                "search_query": "LLM",
                "ranking_query": "LLM news",
                "sources": ["hackernews", "github"],
                "weight": 1.0,
            },
            {
                "search_query": "repo",
                "ranking_query": "new repos",
                "sources": ["github"],
                "weight": 0.5,
            },
        ],
    }
    # 日次の対象日 11/01 は、UTC の 10/31〜11/01（D-16）
    assert args[-4:] == ["--days", "1", "--as-of", "2026-11-01"]
    assert kwargs["timeout"] == 300 and kwargs["cwd"] == kwargs["env"]["HOME"]


def test_relative_root_becomes_absolute():
    # 子プロセスは一時ディレクトリで動くので、相対パスのままでは見つからない
    run = FakeRun(done())
    collect_last30days(CONFIG, PERIOD, Path("third_party/last30days"), github_token="t", run=run)
    assert run.calls[0][0][1] == str(Path.cwd() / "third_party" / "last30days" / SCRIPT)


def test_weekly_period():
    run = FakeRun(done())
    period = weekly_target(date(2026, 11, 2), 7).period
    collect_last30days(CONFIG, period, ROOT, github_token="t", run=run)
    assert run.calls[0][0][-4:] == ["--days", "7", "--as-of", "2026-11-01"]


def test_environment_is_an_allow_list(monkeypatch):
    for name in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GH_TOKEN", "GITHUB_TOKEN", "CLAUDECODE"):
        monkeypatch.setenv(name, "secret")
    run = FakeRun(done())
    collect(run)
    env = run.calls[0][1]["env"]
    home = env.pop("HOME")
    assert env == {
        "PATH": os.environ["PATH"],
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LAST30DAYS_CONFIG_DIR": "",
        "LAST30DAYS_LIBRARY_CONTEXT": "off",
        "LAST30DAYS_STRICT_EXIT": "1",
        "GITHUB_TOKEN": "collect-token",
    }
    # 実行ごとの空の HOME（gh の設定や library.db に届かない）。終わったら消す
    assert run.home_was_empty and not Path(home).exists()


def test_only_enabled_sources_are_searched():
    run = FakeRun(done(stdout=output({"hackernews": "ok"}, [HN])))
    (result,) = collect(
        run, replace(CONFIG, github_search=replace(CONFIG.github_search, enabled=False))
    )
    args = run.calls[0][0]
    assert args[args.index("--search") + 1] == "hackernews"
    plan = json.loads(args[args.index("--plan") + 1])
    assert [s["sources"] for s in plan["subqueries"]] == [["hackernews"]]
    assert result.source_id == "hackernews" and len(result.records) == 1


def test_no_enabled_source_does_not_run():
    run = FakeRun()
    assert collect(run, replace(CONFIG, hackernews=None, github_search=None)) == []
    assert run.calls == []


def test_source_without_subquery_fails_without_running():
    run = FakeRun(done(stdout=output({"github": "ok"}, [GH])))
    hn, gh = collect(run, replace(CONFIG, subqueries=CONFIG.subqueries[1:]))
    args = run.calls[0][0]
    assert args[args.index("--search") + 1] == "github"
    assert "計画がない" in hn.error and hn.attempts == 0 and gh.error is None


def test_github_search_without_token_fails_without_running():
    run = FakeRun(done(stdout=output({"hackernews": "ok"}, [HN])))
    hn, gh = collect(run, github_token=None)
    args = run.calls[0][0]
    assert args[args.index("--search") + 1] == "hackernews"
    assert "GITHUB_TOKEN" not in run.calls[0][1]["env"]
    assert hn.error is None and gh.error == "収集用トークンがない" and gh.attempts == 0
    assert "GITHUB_TOKEN" not in child_env("/tmp/h", None)


def test_nothing_runnable_does_not_run():
    run = FakeRun()
    config = replace(CONFIG, github_search=None, subqueries=CONFIG.subqueries[1:])
    (hn,) = collect(run, config)
    assert run.calls == [] and "計画がない" in hn.error


def test_exit_3_uses_partial_result():
    stderr = "[last30days] strict-exit: degraded sources: github"
    run = FakeRun(done(3, output({"hackernews": "ok", "github": "rate-limited"}), stderr))
    hn, gh = collect(run)
    assert hn.error is None and [r.url for r in hn.records] == [HN["url"]]
    assert gh.records == () and "rate-limited" in gh.error and stderr in gh.error
    assert len(run.calls) == 1  # 部分結果は再試行しない


def test_no_results_is_a_success():
    run = FakeRun(done(stdout=output({"hackernews": "no-results", "github": "ok"}, [GH])))
    hn, gh = collect(run)
    assert (hn.error, hn.records, gh.error) == (None, (), None)


def test_exit_3_with_all_sources_failed():
    status = {"hackernews": "timeout", "github": "rate-limited"}
    hn, gh = collect(FakeRun(done(3, output(status, []))))
    assert "timeout" in hn.error and "rate-limited" in gh.error


def test_missing_or_skipped_status_is_a_failure():
    # skipped-unconfigured は last30days-skill では成功扱いだが、頼んだ情報源が集まっていない
    run = FakeRun(done(stdout=output({"hackernews": "skipped-unconfigured"}, [HN])))
    hn, gh = collect(run)
    assert "skipped-unconfigured" in hn.error and not hn.records
    assert "状態が出力にない" in gh.error


def test_timeout_is_retried_once():
    run = FakeRun(subprocess.TimeoutExpired("cmd", 300), done())
    hn, gh = collect(run)
    assert hn.error is None and hn.attempts == gh.attempts == 2


def test_retry_is_used_up():
    run = FakeRun(done(1, "", "Traceback"), subprocess.TimeoutExpired("cmd", 300))
    results = collect(run)
    assert len(run.calls) == 2
    assert all(r.attempts == 2 and "300 秒" in r.error and not r.records for r in results)


def test_exit_1_is_retried():
    run = FakeRun(done(1, "", "boom"), done())
    assert [r.error for r in collect(run)] == [None, None]


def test_exit_2_after_a_retry_stops():
    run = FakeRun(done(1, "", "boom"), done(2, "", "usage"))
    assert all(r.attempts == 2 and "終了コード 2" in r.error for r in collect(run))


def test_mismatch_after_a_retry_counts_attempts():
    run = FakeRun(subprocess.TimeoutExpired("cmd", 300), done(stdout="[]"))
    assert all(r.attempts == 2 and "契約" in r.error for r in collect(run))


def test_exit_2_is_not_retried():
    run = FakeRun(done(2, "", "usage: --plan required"))
    results = collect(run)
    assert len(run.calls) == 1
    assert all(
        r.attempts == 1 and "終了コード 2" in r.error and "usage" in r.error for r in results
    )


def test_schema_mismatch_is_not_retried():
    run = FakeRun(done(stdout='{"schema_version": "2.0"}'))
    results = collect(run)
    assert len(run.calls) == 1 and all("2.0" in r.error for r in results)


def test_empty_stdout_is_a_contract_failure():
    run = FakeRun(done(0, ""))
    assert all("JSON" in r.error for r in collect(run))
