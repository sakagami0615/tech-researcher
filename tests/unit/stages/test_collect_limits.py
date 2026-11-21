"""① の時間の上限と、GitHub の要求の上限の共有（#42。structure.md 6.4・6.5）。

時計は偽物（情報源の取得で進める）にして、実時間を待たずに確かめる。
"""

import json
from dataclasses import replace
from datetime import date

from tech_researcher.collect.github_api import GitHubClient
from tech_researcher.collect.http import Response
from tech_researcher.collect.records import SourceResult
from tech_researcher.config import GitHubReleases
from tech_researcher.periods import weekly_target
from tech_researcher.stages import collect as collect_stage
from tech_researcher.stages.collect import FETCHES, Deadlines, run_collect
from tests.unit.stages.test_collect import (
    CONFIG,
    TARGET,
    FakeGitHub,
    all_collectors,
    all_sources_config,
    prod_log,
)

RELEASES = GitHubReleases(True, "GitHub Releases", ("a/one", "b/two", "c/three"))


def limited(config, *, collect=10, per_source=4, wrapup=0, **limits):
    """① の上限（分）と、収集の終わりの余裕（秒）を差し替えた設定。"""
    daily = replace(config.limits.daily, collect=collect, collect_per_source=per_source)
    return replace(
        config,
        limits=replace(config.limits, daily=daily, collect_wrapup_s=wrapup, **limits),
    )


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def fetched_status(tmp_path):
    return {f["source_id"]: (f["status"], f["category"]) for f in load_fetches(tmp_path)}


def load_fetches(tmp_path):
    return json.loads((tmp_path / FETCHES).read_text(encoding="utf-8"))


def test_deadlines_leave_the_wrapup_time_and_cap_each_source():
    clock = Clock(1000.0)
    config = limited(CONFIG, collect=10, per_source=4, wrapup=60)
    d = Deadlines.start(config.limits, TARGET, clock)
    assert d.overall == 1000.0 + 10 * 60 - 60
    assert d.source() == 1000.0 + 4 * 60  # 情報源ごと
    clock.now += 400
    assert d.source() == d.overall  # 全体の上限を超えない
    assert not d.expired()
    clock.now = d.overall
    assert d.expired()


def test_weekly_uses_its_own_limits():
    config = CONFIG
    weekly = weekly_target(date(2026, 10, 26), 7)
    d = Deadlines.start(config.limits, weekly, Clock(0.0))
    assert d.overall == config.limits.weekly.collect * 60 - config.limits.collect_wrapup_s


def test_sources_after_the_overall_limit_fail_as_timeout_and_the_rest_continue(tmp_path):
    """① の上限に達したら、残りの情報源は取らず、終わった情報源の分で続ける（US-E6-03）。"""
    clock = Clock()
    calls = []
    base = all_collectors(calls)

    def slow_feed(f, limits):
        result = base.feed(f, limits)
        clock.now += 11 * 60  # 1つ目で ① の上限（10 分）を使い切る
        return result

    collected = replace(base, feed=slow_feed, clock=clock)
    config = limited(all_sources_config())
    summary = run_collect(config, TARGET, tmp_path, prod_log(tmp_path), collectors=collected)

    assert calls == ["rss:a"]  # 上限の後は何も呼ばない（last30days・GitHub も）
    assert summary["items"] == 1  # 終わった情報源の分で続ける
    assert summary["failed_sources"] == [
        "rss:c",
        "qiita",
        "hackernews",
        "github_search",
        "github_repos",
    ]
    status = fetched_status(tmp_path)
    assert status["rss:a"] == ("ok", None)
    for source in ("rss:c", "qiita", "hackernews", "github_search", "github_repos"):
        assert status[source] == ("failed", "timeout"), source


def test_limit_not_reached_runs_everything(tmp_path):
    clock = Clock()
    calls = []
    collected = replace(all_collectors(calls), clock=clock)
    config = limited(all_sources_config())
    run_collect(config, TARGET, tmp_path, prod_log(tmp_path), collectors=collected)
    assert "last30days" in calls and "qiita" in calls


def test_each_source_gets_its_deadline_inside_the_overall_one(tmp_path):
    clock = Clock(1000.0)
    seen = {}
    releases_config = replace(all_sources_config(), github_releases=RELEASES)

    def qiita(settings, period, limits, *, deadline):
        seen["qiita"] = deadline
        clock.now += 100
        return SourceResult("qiita", (), 1)

    def last30days(config, period, token, *, deadline):
        seen["last30days"] = deadline
        clock.now += 100
        return [SourceResult("hackernews", (), 1), SourceResult("github_search", (), 1)]

    def releases(settings, period, client, *, deadline):
        seen["releases"] = deadline
        return SourceResult("github_releases", (), 1)

    collected = replace(
        all_collectors([]), qiita=qiita, last30days=last30days, releases=releases, clock=clock
    )
    # ① 全体 10 分、情報源ごと 4 分
    config = limited(releases_config, collect=10, per_source=4, wrapup=60)
    run_collect(config, TARGET, tmp_path, prod_log(tmp_path), collectors=collected)

    assert seen["qiita"] == 1000.0 + 4 * 60
    assert seen["last30days"] == 1100.0 + 4 * 60
    # 情報源ごとの上限（1200 + 240 = 1440）は ① 全体の上限（1000 + 540 = 1540）より前
    assert seen["releases"] == 1200.0 + 4 * 60
    # 全体の上限が近いときは、全体の上限で止まる
    clock.now = 1000.0
    seen.clear()
    config = limited(releases_config, collect=2, per_source=4, wrapup=0)
    run_collect(config, TARGET, tmp_path, prod_log(tmp_path), collectors=collected)
    assert seen["qiita"] == 1000.0 + 2 * 60


def test_readmes_stop_at_the_overall_limit(tmp_path, monkeypatch):
    """README の取得（急上昇・新規）も ① 全体の上限につなぐ（#37 からの引き継ぎ）。"""
    clock = Clock(1000.0)
    given = {}
    real = collect_stage.fetch_readmes

    def spy(names, client, **kwargs):
        given.update(kwargs)
        return real(names, client, **kwargs)

    monkeypatch.setattr(collect_stage, "fetch_readmes", spy)
    collected = replace(all_collectors([]), clock=clock)
    config = limited(all_sources_config(), collect=10, per_source=4, wrapup=60)
    run_collect(config, TARGET, tmp_path, prod_log(tmp_path), collectors=collected)
    # github_repos の情報源ごとの上限（4 分）。① 全体の上限（9 分）より前
    assert given == {"deadline": 1000.0 + 4 * 60, "clock": clock}


def test_readmes_after_the_limit_are_skipped_but_items_stay(tmp_path):
    clock = Clock()
    calls = []

    class Slow(FakeGitHub):
        def fetch(self, url, limits, **kwargs):
            if "/readme" in url:
                clock.now += 5 * 60  # 1件ごとに 5 分かかる
            return super().fetch(url, limits, **kwargs)

    github = Slow(calls, readmes={"mentioned/repo": "# R", "new/tool": "# T"})
    collected = replace(all_collectors(calls, github=github), clock=clock)
    config = limited(all_sources_config(), collect=4, per_source=4, wrapup=0)
    summary = run_collect(config, TARGET, tmp_path, prod_log(tmp_path), collectors=collected)

    # 1件目の README で 5 分かかり ① の上限（4 分）を過ぎるので、2件目は要求を出さない
    assert [c for c in calls if c.startswith("readme:")] == ["readme:mentioned/repo"]
    assert summary["items"] == 4  # README がなくても項目は載る


def test_rest_budget_is_shared_between_github_sources(tmp_path):
    """README と Releases は同じクライアントの上限を使う。上限に達したら要求を出さない。"""
    calls = []
    github = FakeGitHub(calls)
    made = []

    def make(token, limits):
        client = GitHubClient(token, limits, fetch_url=github_fetch, sleep=lambda s: None)
        made.append(client)
        return client

    def github_fetch(url, limits, **kwargs):
        if "/releases" in url:
            calls.append("releases:" + url.split("/repos/")[1].split("/releases")[0])
            return Response(url, 200, b"[]", 1)
        return github.fetch(url, limits, **kwargs)

    # 上限 4：README が 2 回（mentioned/repo と new/tool）、Releases が 2 回で使い切る
    config = limited(replace(all_sources_config(), github_releases=RELEASES), github_rest_calls=4)
    collected = replace(all_collectors(calls, github=github), github=make)
    summary = run_collect(config, TARGET, tmp_path, prod_log(tmp_path), collectors=collected)

    assert len(made) == 1  # 1つのクライアントを共有する
    rest = [c for c in calls if c.startswith(("readme:", "releases:"))]
    assert len(rest) == 4  # 上限を超えて要求を出さない
    assert [c for c in rest if c.startswith("releases:")] == ["releases:a/one", "releases:b/two"]
    assert made[0].rest_calls == 4
    # 上限に達した後の追跡先は、取れなかった部分（budget）になる。取れた分は使う
    entry = next(f for f in load_fetches(tmp_path) if f["source_id"] == "github_releases")
    assert entry["status"] != "failed"
    assert entry["parts"] == [{"name": "c/three", "category": "budget"}]
    assert "github_releases" not in summary["failed_sources"]


def test_github_is_not_started_without_time_for_its_searches(tmp_path):
    """Search を使い切る時間（4 分）がなければ、始めずに timeout の失敗にする。"""
    clock = Clock()
    calls = []
    base = all_collectors(calls)

    def slow_feed(f, limits):
        if clock.now == 1000.0:  # 最初の feed だけ。全体の上限（10 分）まで残り 3 分になる
            clock.now += 7 * 60
        return base.feed(f, limits)

    collected = replace(base, feed=slow_feed, clock=clock)
    config = limited(all_sources_config(), collect=10, per_source=10)
    summary = run_collect(config, TARGET, tmp_path, prod_log(tmp_path), collectors=collected)
    assert "github_repos" in summary["failed_sources"]
    assert "github_search" not in calls and "github_graphql" not in calls
    assert fetched_status(tmp_path)["github_repos"] == ("failed", "timeout")
    assert clock.now < 1000.0 + 10 * 60  # 上限は過ぎていない（Search の時間だけが足りない）
