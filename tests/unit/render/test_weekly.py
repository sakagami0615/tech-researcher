"""週次レポートの本文と観測数（report-templates.md 2章）。"""

from datetime import UTC, date, datetime

import pytest

from tech_researcher.agent.weekly_output import (
    ComparisonEntry,
    ComparisonRow,
    TechChangeEntry,
    ThemeEntry,
    WatchEntry,
    WeeklyOutput,
)
from tech_researcher.collect.aggregate import aggregate
from tech_researcher.collect.records import Event, GitHubRepo, Record
from tech_researcher.config import QuoteLimits
from tech_researcher.db.weekly import PastObservation
from tech_researcher.periods import weekly_target
from tech_researcher.render.weekly import (
    FOOTER,
    PastWindow,
    count_observations,
    render_weekly,
    theme_display_key,
)
from tech_researcher.validate.weekly import check_weekly

TARGET = weekly_target(date(2026, 10, 26), 7)  # 10-19 0:00〜10-26 0:00（JST）
AT = datetime(2026, 10, 21, 3, 0, tzinfo=UTC)
NAMES = {"rss:blog": "公式ブログ", "github_repos": "GitHub", "rss:old": "旧ブログ"}
RUN = "20261026-070000-c3d4"


def record(n, source_id="rss:blog", url=None, github=None):
    return Record(
        source_id=source_id,
        url=url or f"https://example.com/{n}",
        title="外部のタイトル",
        published_precision="datetime",
        fetched_at=AT,
        published_at=AT,
        events=(Event("published", AT),),
        github=github,
    )


REPO = GitHubRepo("example/tiny-agent", AT, 500)
ITEMS = {f"i{n}": item for n, item in enumerate(aggregate(record(n) for n in range(1, 9)), 1)}
ITEMS["i9"] = aggregate([record(9, "github_repos", "https://github.com/example/tiny-agent", REPO)])[
    0
]


def past(n, week, url=None):
    return PastObservation(
        f"i{n}", week, "rss:old", "article", "t", url or f"https://old.example.com/{n}"
    )


WEEKS = [date(2026, 10, 19), date(2026, 10, 12), date(2026, 10, 5), date(2026, 9, 28)]
PAST = [
    past(90, date(2026, 10, 19)),
    past(91, date(2026, 10, 19)),
    past(92, date(2026, 10, 12)),
    past(93, date(2026, 10, 5)),
    past(94, date(2026, 9, 28)),
    past(95, date(2026, 9, 28)),
]
WINDOW = PastWindow(WEEKS, 4, PAST)


def theme(name="テーマ", status="rising", item_ids=("i1",), past_ids=(), theme_id="T0001", **kw):
    fields = {"change": "変化", "comparison": "比較", **kw}
    return ThemeEntry(
        theme_id, name, status, item_ids=tuple(item_ids), past_ids=tuple(past_ids), **fields
    )


def render(themes=(), changes=(), comparisons=(), watch=(), window=WINDOW, target=TARGET, **kw):
    output = WeeklyOutput(tuple(themes), tuple(changes), tuple(comparisons), tuple(watch))
    return render_weekly(target, RUN, output, ITEMS, NAMES, window, kw.pop("observed", 8), **kw)


def section(body, heading):
    """見出し（## ...）から次の ## までの本文。"""
    chunks = body.split("\n---\n")[0].split("\n## ")
    return next(c for c in chunks if c.startswith(heading) or c.startswith(f"## {heading}"))


def test_title_labels_and_header():
    report = render([theme()], observed=1684)
    assert report.title == "[週次] 2026-10-26（月）号 技術トレンド（10/19〜10/25）"
    assert report.labels == ("type:report", "cadence:weekly")
    lines = report.body.splitlines()
    assert lines[0] == (
        f"<!-- tech-researcher kind=report cadence=weekly target=2026-10-26 run={RUN} -->"
    )
    assert lines[1:4] == [
        "> **調査期間**：2026-10-19（月）〜 2026-10-25（日）（JST）",
        "> **比較に使った過去の観測**：2026-09-21 〜 2026-10-18（4 週分）",
        "> **今週の観測**：1,684 件",
    ]
    assert report.body.endswith("\n")


def test_title_adds_years_when_the_period_crosses_a_year():
    target = weekly_target(date(2027, 1, 4), 7)  # 2026-12-28〜2027-01-03
    report = render([theme()], target=target)
    assert report.title == "[週次] 2027-01-04（月）号 技術トレンド（2026/12/28〜2027/01/03）"
    assert "2026-12-28（月）〜 2027-01-03（日）（JST）" in report.body


def test_observation_counts_from_ids_and_missing_weeks():
    t = theme(item_ids=("i1", "i2", "i3"), past_ids=("i90", "i91", "i92", "i93", "i94", "i95"))
    counts = count_observations(t, TARGET, WINDOW)
    assert (counts.this_week, counts.last_week, counts.average) == (3, 2, 1.5)
    # 前週が「データなし」：前週は None（—）、平均は残りの週で割る
    window = PastWindow(WEEKS[1:], 4, PAST)
    counts = count_observations(t, TARGET, window)
    assert (counts.last_week, counts.average) == (None, 4 / 3)
    # 窓がない：前週・平均とも None
    counts = count_observations(t, TARGET, PastWindow([], 4, []))
    assert (counts.last_week, counts.average) == (None, None)
    # 窓の外の週の観測は数えない
    outside = PastWindow(WEEKS[:1], 4, PAST)
    assert count_observations(t, TARGET, outside).average == 2.0


def test_theme_table_and_theme_section_show_counts():
    t = theme(
        "小型モデル", item_ids=("i1", "i2"), past_ids=("i90", "i92", "i93"), comparison="増えた"
    )
    body = render([t]).body
    assert "| テーマ | 状態 | 今週 | 前週 | 過去4週平均 |" in body
    assert "| 小型モデル | 🔺台頭 | 2 | 1 | 0.8 |" in body
    rising = section(body, "台頭したテーマ")
    assert "### 小型モデル" in rising
    assert "- **過去の観測との比較**：増えた（今週 2 件／前週 1 件／過去4週平均 0.8 件）" in rising
    assert "- **主な出典**：[公式ブログ（example.com）](https://example.com/1)" in rising


def test_missing_previous_week_is_a_dash_and_the_window_label_follows_the_setting():
    window = PastWindow(WEEKS[1:], 3, PAST)
    body = render([theme(past_ids=("i92",))], window=window).body
    assert "| テーマ | 状態 | 今週 | 前週 | 過去3週平均 |" in body
    assert "| テーマ | 🔺台頭 | 1 | — | 0.3 |" in body
    assert "（今週 1 件／前週 —／過去3週平均 0.3 件）" in body
    assert "（3 週分）" in body


def test_no_past_observations_replaces_the_comparison():
    body = render([theme(comparison="AI の比較")], window=PastWindow([], 4, [])).body
    assert "> **比較に使った過去の観測**：なし" in body
    assert "- **過去の観測との比較**：比較できる過去の観測なし" in body
    assert "AI の比較" not in body
    assert "| テーマ | 🔺台頭 | 1 | — | — |" in body


def test_statuses_are_grouped_in_a_fixed_order_with_labels_and_empty_sections():
    themes = [
        theme("沈静", "fading", ("i3",), ("i90",), "T0003"),
        theme("台頭", "rising", ("i1",), (), "T0001"),
        theme("継続1", "steady", ("i2",), (), "T0002"),
        theme("継続2", "steady", ("i4",), (), "T0004"),
    ]
    assert [t.name for t in sorted(themes, key=theme_display_key)] == [
        "台頭",
        "継続1",
        "継続2",
        "沈静",
    ]
    body = render(themes).body
    table = section(body, "テーマの動き").splitlines()
    assert [row.split(" | ")[:2] for row in table[4:]] == [
        ["| 台頭", "🔺台頭"],
        ["| 継続1", "➡️継続"],
        ["| 継続2", "➡️継続"],
        ["| 沈静", "🔻沈静"],
    ]
    assert body.index("### 台頭") < body.index("### 継続1") < body.index("### 継続2")
    assert body.index("### 継続2") < body.index("### 沈静")
    # テーマがない状態の節は見出しを残して「該当なし」
    only = render([theme()]).body
    assert section(only, "継続して注目されているテーマ").strip().endswith("今週は該当なし")
    assert section(only, "注目が落ち着いたテーマ").strip().endswith("今週は該当なし")


def test_fading_theme_without_this_week_uses_the_last_observed_past_articles():
    t = theme("沈静", "fading", item_ids=(), past_ids=("i92", "i90", "i94"))
    fading = section(render([t]).body, "注目が落ち着いたテーマ")
    assert "- **主な出典（最後に観測した記事）**：" in fading
    line = next(ln for ln in fading.splitlines() if "最後に観測" in ln)
    # 新しい週から（10-19 → 10-12 → 9-28）。過去の観測は日付を持たず、情報源名とドメインで書く
    assert line.endswith(
        "[旧ブログ（old.example.com）](https://old.example.com/90) ／ "
        "[旧ブログ（old.example.com）](https://old.example.com/92) ／ "
        "[旧ブログ（old.example.com）](https://old.example.com/94)"
    )
    assert "今週 0 件／前週 1 件" in fading


def test_theme_sources_are_limited_to_five_in_the_ai_order():
    t = theme(item_ids=("i3", "i1", "i2", "i4", "i5", "i6", "i7"))
    line = next(ln for ln in render([t]).body.splitlines() if ln.startswith("- **主な出典"))
    assert [n for n in range(1, 8) if f"example.com/{n})" in line] == [1, 2, 3, 4, 5]
    assert line.index("example.com/3)") < line.index("example.com/1)")  # AI の順


def test_tech_changes_and_watch_have_sources_per_entry():
    body = render(
        changes=[TechChangeEntry("見出し", "説明", ("i1", "i2"))],
        watch=[WatchEntry("注目", "理由", ("i3",))],
    ).body
    assert (
        "- **見出し**：説明\n"
        "  出典：[公式ブログ（example.com）](https://example.com/1) 公開 2026-10-21 ／ "
        "[公式ブログ（example.com）](https://example.com/2) 公開 2026-10-21"
    ) in body
    assert "- **注目**：理由\n  出典：[公式ブログ（example.com）](https://example.com/3)" in body


def test_empty_sections_say_nothing_to_report():
    body = render().body
    for heading in (
        "テーマの動き",
        "重要な技術的変化",
        "OSS・ツールの比較",
        "今後注目すべきテーマ",
    ):
        assert section(body, heading).strip().endswith("今週は該当なし")
    assert f"---\n{FOOTER}" in body


def test_comparison_table_names_github_by_code_and_others_by_ai_text():
    rows = (
        ComparisonRow("i9", "無視される製品名", "軽い", "試行"),
        ComparisonRow("i1", "Example|Tool", "多機能", "運用"),
    )
    body = render(comparisons=[ComparisonEntry("軽量な環境", rows, "違いのまとめ")]).body
    table = section(body, "OSS・ツールの比較")
    assert "### 軽量な環境" in table
    assert "| 名前 | 特徴 | 向いている用途 | 出典 |" in table
    assert "| `example/tiny-agent` | 軽い | 試行 | [GitHub（github.com）]" in table
    assert "| Example\\|Tool | 多機能 | 運用 | [公式ブログ（example.com）]" in table
    assert "無視される製品名" not in body
    assert table.rstrip().endswith("違いのまとめ")


def test_rows_dropped_by_the_check_are_the_rows_missing_from_the_body():
    """名前を決められない行は検査で除外し、本文は残りを書く"""
    bad = aggregate(
        [
            record(
                10,
                "github_repos",
                "https://github.com/bad/repo",
                GitHubRepo("bad repo/x", AT, 1),
            )
        ]
    )[0]
    items = {**ITEMS, "i10": bad}
    data = {
        "items": [{"id": i, "untrusted": {"title": "t", "excerpt": None}} for i in items],
        "past_observations": [],
        "past_themes": [],
    }
    rows = (
        ComparisonRow("i10", None, "特徴", "用途"),
        ComparisonRow("i1", None, "特徴", "用途"),
        ComparisonRow("i9", None, "軽い", "試行"),
    )
    output = WeeklyOutput((), (), (ComparisonEntry("領域", rows, "まとめ"),), ())
    checked = check_weekly(output, data, items, {}, QuoteLimits())
    assert [(x.section, x.category) for x in checked.excluded] == [
        ("comparison_row", "invalid_id"),
        ("comparison_row", "unknown_product"),
    ]
    report = render_weekly(TARGET, RUN, checked.output, items, NAMES, WINDOW, 1)
    table = section(report.body, "OSS・ツールの比較")
    assert [ln for ln in table.splitlines() if ln.startswith("| `")] == [
        ln for ln in table.splitlines() if "tiny-agent" in ln
    ]
    assert "bad/repo" not in table and table.count("\n| ") == 2  # 見出し行と1行（区切り行は |-）
    # 行がすべて除外された領域は、検査で領域ごと除外され、本文は「該当なし」
    none = check_weekly(
        WeeklyOutput((), (), (ComparisonEntry("領域", rows[:2], "まとめ"),), ()),
        data,
        items,
        {},
        QuoteLimits(),
    )
    assert ("comparison", "empty_comparison") in [(x.section, x.category) for x in none.excluded]
    body = render_weekly(TARGET, RUN, none.output, items, NAMES, WINDOW, 1).body
    assert section(body, "OSS・ツールの比較").strip().endswith("今週は該当なし")


def test_unavailable_sources_come_last_and_only_when_present():
    assert "取得できなかった情報源" not in render([theme()]).body
    body = render([theme()], unavailable=[("Qiita", "タイムアウト")]).body
    assert body.rstrip().endswith("## 取得できなかった情報源\n\n- Qiita：タイムアウト")
    assert body.index(FOOTER) < body.index("取得できなかった情報源")


@pytest.mark.parametrize("text", ["# 見出し", "@user と #12", "<b>x</b>"])
def test_ai_text_is_sanitized(text):
    body = render([theme(name=text, change=text, comparison=text)]).body
    assert "\n# 見出し" not in body
    assert "<b>" not in body.replace("\\<b>", "")  # HTML は \< でエスケープされる
    assert "@user" not in body.replace("`@user`", "")


def test_theme_sources_count_every_source_of_an_item_toward_the_limit():
    from dataclasses import replace

    many = aggregate(
        replace(record(n), link_url="https://example.com/multi") for n in range(11, 15)
    )[0]
    assert len(many.sources) == 4
    items = {**ITEMS, "i20": many}
    output = WeeklyOutput((theme(item_ids=("i20", "i1", "i2", "i3")),), (), (), ())
    body = render_weekly(TARGET, RUN, output, items, NAMES, WINDOW, 4).body
    line = next(ln for ln in body.splitlines() if ln.startswith("- **主な出典"))
    assert line.count("](") == 5  # 1項目の4出典 + 次の項目の1出典


def test_fading_theme_with_this_week_items_uses_the_plain_source_label():
    t = theme("沈静", "fading", item_ids=("i3",), past_ids=("i90",))
    fading = section(render([t]).body, "注目が落ち着いたテーマ")
    assert "- **主な出典**：[公式ブログ（example.com）](https://example.com/3)" in fading
    assert "最後に観測" not in fading


def test_past_sources_drop_the_same_url_seen_in_several_weeks():
    same = [past(96, date(2026, 10, 12), "https://old.example.com/90")]
    window = PastWindow(WEEKS, 4, [*PAST, *same])
    t = theme("沈静", "fading", item_ids=(), past_ids=("i90", "i96", "i92"))
    line = next(ln for ln in render([t], window=window).body.splitlines() if "最後に観測" in ln)
    assert line.count("old.example.com/90)") == 1 and line.count("](") == 2
