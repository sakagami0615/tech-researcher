from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from tech_researcher.collect.github_spikes import is_new_repo, judge_star_spikes
from tech_researcher.collect.github_stars import StarResult
from tech_researcher.config import load_config
from tech_researcher.db.star_snapshots import StarSnapshot
from tech_researcher.periods import daily_target, weekly_target

SETTINGS = replace(
    load_config(Path(__file__).parents[3] / "config" / "tech-researcher.toml").github_repos,
    threshold_increase=100,
    report_top_n=2,
    new_repo_days=14,
    new_repo_stars=200,
    new_repo_top_n=2,
)
TARGET = daily_target(date(2026, 10, 24), 1)  # 対象日 10-23。窓の終わりは 10-24 6:00（JST）
MEASURED = datetime(2026, 10, 23, 21, 0, 30, tzinfo=UTC)  # 10-24 6:00:30（JST）
OLD = datetime(2025, 1, 1, tzinfo=UTC)
NEW = datetime(2026, 10, 20, tzinfo=UTC)


def snap(repo, stars, created=OLD):
    return StarSnapshot(repo, stars, created, MEASURED, ("search",))


def judge(today, yesterday=(), featured=(), mentioned=None, target=TARGET):
    stars = StarResult(tuple(today), mentioned or {})
    previous = {s.repo: s for s in yesterday}
    return judge_star_spikes(SETTINGS, target, stars, previous, featured)


def by_repo(result):
    return {r.github.full_name: r for r in result.records}


def test_existing_repo_needs_the_increase_threshold():
    result = judge(
        [snap("a/up", 1100), snap("a/flat", 1099)], [snap("a/up", 1000), snap("a/flat", 1000)]
    )
    assert list(by_repo(result)) == ["a/up"]
    record = result.records[0]
    (event,) = record.events
    assert (event.type, event.basis, event.at) == ("attention_spike", "star_growth", MEASURED)
    assert [(a.metric, a.value) for a in record.attention] == [
        ("stars_total", 1100),
        ("star_delta_24h", 100),
    ]
    assert record.github.star_delta_24h == 100
    assert record.links == ("https://github.com/a/up",)  # 候補リンクとしてリポジトリの URL
    assert record.meta_terms == ("a/up",)
    assert (record.published_at, record.published_precision) == (OLD, "datetime")


def test_top_n_by_increase_largest_first():
    today = [snap("a/x", 1150), snap("b/y", 1300), snap("c/z", 1200)]
    before = [snap(s.repo, 1000) for s in today]
    result = judge(today, before)
    assert sorted(by_repo(result)) == ["b/y", "c/z"]  # 増分 300・200。150 は上位2件の外


def test_featured_repo_is_kept_but_does_not_use_a_top_n_slot():
    today = [snap("a/x", 1150), snap("b/y", 1300), snap("c/z", 1200)]
    before = [snap(s.repo, 1000) for s in today]
    result = judge(today, before, featured={"https://github.com/b/y"})
    # b/y は観測として残る（除外は hold_reason で行う）。上位2件は a/x と c/z
    assert sorted(by_repo(result)) == ["a/x", "b/y", "c/z"]


def test_new_repo_is_a_separate_event_and_needs_no_previous_record():
    result = judge([snap("n/new", 250, NEW)], [snap("a/other", 10)])
    (record,) = result.records
    (event,) = record.events
    assert (event.type, event.basis) == ("new_repo", None)
    assert record.github.star_delta_24h is None
    assert [a.metric for a in record.attention] == ["stars_total"]


def test_new_repo_is_not_also_a_spike():
    result = judge([snap("n/new", 900, NEW)], [snap("n/new", 100, NEW)])
    (record,) = result.records
    assert [e.type for e in record.events] == ["new_repo"]
    assert record.github.star_delta_24h == 800


def test_new_repo_conditions():
    assert is_new_repo(SETTINGS, TARGET, snap("n/a", 200, datetime(2026, 10, 9, 21, tzinfo=UTC)))
    assert not is_new_repo(SETTINGS, TARGET, snap("n/a", 199, NEW))  # 累計が C 未満
    assert not is_new_repo(
        SETTINGS, TARGET, snap("n/a", 500, datetime(2026, 10, 9, 20, tzinfo=UTC))
    )
    assert not is_new_repo(SETTINGS, TARGET, snap("n/a", 500, datetime(2026, 10, 25, tzinfo=UTC)))


def test_repo_without_a_previous_record_is_not_judged_but_others_are():
    result = judge([snap("a/first", 5000), snap("a/up", 1200)], [snap("a/up", 1000)])
    assert list(by_repo(result)) == ["a/up"]  # 初めて見る a/first は記録だけ
    assert result.star_unjudged is False


def test_no_previous_record_at_all_is_reported_as_unjudged():
    result = judge([snap("a/x", 5000), snap("n/new", 300, NEW)], [])
    assert result.star_unjudged is True
    assert list(by_repo(result)) == ["n/new"]  # 前日比のいらない新規は判定する


def test_mentioned_by_is_passed_through():
    today = [snap("n/new", 300, NEW)]
    result = judge(today, [], mentioned={"n/new": ("https://news.example.com/x",)})
    assert result.records[0].github.mentioned_by == ("https://news.example.com/x",)


def test_weekly_is_not_judged():
    result = judge([snap("n/new", 300, NEW)], [], target=weekly_target(date(2026, 10, 26), 7))
    assert result.records == () and result.star_unjudged is False


def test_decreasing_stars_are_not_spikes():
    result = judge([snap("a/x", 500)], [snap("a/x", 1000)])
    assert result.records == ()


def test_new_repos_are_limited_to_top_n_by_total_stars():
    today = [snap("n/a", 300, NEW), snap("n/b", 900, NEW), snap("n/c", 500, NEW)]
    result = judge(today)
    assert sorted(by_repo(result)) == ["n/b", "n/c"]  # 累計の多い上位2件


def test_featured_new_repo_does_not_use_a_top_n_slot():
    today = [snap("n/a", 300, NEW), snap("n/b", 900, NEW), snap("n/c", 500, NEW)]
    result = judge(today, featured={"https://github.com/n/b"})
    # n/b は観測として残り（hold で除外）、上位2件は n/c と n/a
    assert sorted(by_repo(result)) == ["n/a", "n/b", "n/c"]
