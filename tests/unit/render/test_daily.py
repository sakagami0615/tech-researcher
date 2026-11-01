from datetime import UTC, date, datetime

import pytest

from tech_researcher.agent.daily import DailyEntry
from tech_researcher.collect.aggregate import aggregate
from tech_researcher.collect.records import Event, GitHubRepo, Record
from tech_researcher.periods import daily_target
from tech_researcher.render.daily import FOOTER, render_daily

TARGET = daily_target(date(2026, 10, 24), 1)  # 対象日 10/23（金）
IN = datetime(2026, 10, 23, 0, 0, tzinfo=UTC)  # JST 9:00
OLD = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
NAMES = {"rss:blog": "公式ブログ", "github_repos": "GitHub"}
RUN = "20261024-060000-a1b2"


def record(n, published_at=IN, source_id="rss:blog", **kw):
    return Record(
        source_id=source_id,
        url=f"https://example.com/{n}",
        title="外部のタイトル",
        published_precision="datetime",
        fetched_at=IN,
        published_at=published_at,
        events=(Event("published", published_at),),
        **kw,
    )


ITEMS = {f"i{n}": item for n, item in enumerate(aggregate(record(n) for n in range(1, 21)), 1)}


def entry(n, category="ai", rank=1, depth="full", points=("要点",), period_event=None):
    return DailyEntry(
        (f"i{n}",),
        f"i{n}",
        None,
        category,
        rank,
        depth,
        f"見出し{n}",
        points,
        f"理由{n}",
        period_event,
    )


def render(entries, items=ITEMS, collected=20, names=NAMES, **kw):
    return render_daily(TARGET, RUN, entries, items, names, collected, **kw)


def headings(body):
    return [line for line in body.splitlines() if line.startswith(("## ", "#### "))]


def test_title_and_labels():
    report = render([entry(1)])
    assert report.title == "[日次] 2026-10-23（金）の技術動向"
    assert report.labels == ("type:report", "cadence:daily")


def test_header_has_identifier_period_and_count():
    lines = render([entry(1)]).body.splitlines()
    assert lines[0] == (
        f"<!-- tech-researcher kind=report cadence=daily target=2026-10-23 run={RUN} -->"
    )
    assert lines[1] == "> **対象期間**：2026-10-23（金）00:00 〜 2026-10-24（土）00:00（JST）"
    assert lines[2] == "> **掲載**：1 件"


def test_categories_in_fixed_order_ranks_and_serial_numbers():
    entries = [
        entry(1, "other"),
        entry(2, "ai", rank=2),
        entry(3, "oss"),
        entry(4, "ai", rank=1),
        entry(5, "devtools"),
    ]
    body = render(entries).body
    assert headings(body) == [
        "## 目次",
        "## 生成AI・LLM・AIエージェント（2 件）",
        "#### 1. 見出し4",
        "#### 2. 見出し2",
        "## 開発者ツール（1 件）",
        "#### 3. 見出し5",
        "## OSS（1 件）",
        "#### 4. 見出し3",
        "## その他の新テーマ（1 件）",
        "#### 5. 見出し1",
    ]
    # 0件のカテゴリは見出しを省き、目次には 0 件と出す
    assert "- クラウド・インフラ（0 件）" in body
    assert "## クラウド・インフラ" not in body


def test_standard_form():
    body = render([entry(1, points=("要点1", "要点2"))]).body
    assert (
        "#### 1. 見出し1\n\n"
        "- **要点**\n  - 要点1\n  - 要点2\n"
        "- **重要性**：理由1\n"
        "- **出典**：一次情報 [公式ブログ（example.com）](https://example.com/1) 公開 2026-10-23\n"
    ) in body
    assert "期間内の出来事" not in body


def test_brief_form():
    body = render([entry(1, depth="brief")]).body
    assert (
        "#### 1. 見出し1\n\n"
        "要点 **重要性**：理由1\n"
        "出典：一次情報 [公式ブログ（example.com）](https://example.com/1) 公開 2026-10-23\n"
    ) in body


def spiking_items(delta=1850):
    repo = GitHubRepo("example/tiny-agent", OLD, 5000, star_delta_24h=delta)
    old = record("tiny-agent", published_at=OLD, source_id="github_repos", github=repo)
    return {"i1": aggregate([old])[0]}


@pytest.mark.parametrize(
    "depth, line",
    [
        ("full", "- **期間内の出来事**：紹介されて急増（Star +1,850／24時間）\n"),
        ("brief", "\n期間内の出来事：紹介されて急増（Star +1,850／24時間）\n"),
    ],
)
def test_period_event_for_item_published_before_period(depth, line):
    e = entry(1, "oss", depth=depth, period_event="紹介されて急増")
    body = render([e], items=spiking_items()).body
    assert line in body
    assert "公開 2026-08-01" in body


def test_period_event_is_hidden_for_item_in_period():
    body = render([entry(1, period_event="余計な文")]).body
    assert "余計な文" not in body


def test_missing_period_event_is_an_error():
    with pytest.raises(ValueError):
        render([entry(1, "oss")], items=spiking_items())


def test_few_items_notice_below_threshold():
    body = render([entry(n) for n in range(1, 5)]).body
    assert "> ℹ️ 重要な情報が少ない日でした（掲載 4 件）。" in body
    assert "少ない日" not in render([entry(n) for n in range(1, 6)]).body


def test_zero_items_is_a_normal_report():
    body = render([], collected=37).body
    assert "> **掲載**：0 件" in body
    assert (
        "**該当なし**：対象期間に、掲載基準を満たす重要な技術情報はありませんでした"
        "（収集 37 件）。ジョブは正常に完了しています。"
    ) in body
    assert "## 目次" not in body and "####" not in body
    assert "少ない日" not in body
    assert FOOTER in body


@pytest.mark.parametrize("delta", [None, 0, -3])
def test_period_event_without_positive_star_delta(delta):
    e = entry(1, "oss", period_event="紹介された")
    body = render([e], items=spiking_items(delta)).body
    assert "- **期間内の出来事**：紹介された\n" in body
    assert "Star" not in body


def test_unjudged_spikes_in_header_after_few_items_notice():
    unjudged = (
        ("GitHub の Star の急増", ()),
        ("人気型フィードへの初登場", ("Hacker News", "Lobsters")),
    )
    header = render([entry(1)], unjudged=unjudged).body.split("\n\n")[0]
    assert header.splitlines()[3:] == [
        "> ℹ️ 重要な情報が少ない日でした（掲載 1 件）。",
        "> ℹ️ GitHub の Star の急増は、前日の記録がないため判定していません。",
        "> ℹ️ 人気型フィードへの初登場（Hacker News、Lobsters）は、前日の記録がないため"
        "判定していません。",
    ]


def test_zero_items_with_unjudged_and_unavailable():
    body = render(
        [],
        collected=0,
        unjudged=(("GitHub の Star の急増", ()),),
        unavailable=(("Qiita", "認証エラー"),),
    ).body
    assert "> ℹ️ GitHub の Star の急増は" in body and "少ない日" not in body
    assert "**該当なし**" in body and "####" not in body
    assert body.endswith("## 取得できなかった情報源\n\n- Qiita：認証エラー\n")


EVIL = "Feed](https://evil.example) @victim <!--"


def test_malicious_names_are_plain_text_everywhere():
    body = render(
        [entry(1)],
        names={"rss:blog": EVIL},
        unjudged=((EVIL, (EVIL,)),),
        unavailable=((EVIL, EVIL),),
    ).body
    assert "@" not in body and "](https://evil" not in body
    assert body.count("<!--") == 1  # 1行目の識別子だけ


def test_period_longer_than_one_day_in_header():
    target = daily_target(date(2026, 10, 24), 3)
    report = render_daily(target, RUN, [entry(1)], ITEMS, NAMES, 20)
    assert report.title == "[日次] 2026-10-23（金）の技術動向"
    assert "> **対象期間**：2026-10-21（水）00:00 〜 2026-10-24（土）00:00（JST）" in report.body


def test_same_rank_keeps_ai_order():
    entries = [entry(3, rank=1), entry(1, rank=1), entry(2, rank=0)]
    assert headings(render(entries).body)[2:] == [
        "#### 1. 見出し2",
        "#### 2. 見出し3",
        "#### 3. 見出し1",
    ]


def test_brief_form_with_several_points_is_one_line():
    body = render([entry(1, depth="brief", points=("要点1", "要点2"))]).body
    assert "\n要点1 要点2 **重要性**：理由1\n" in body


def test_whole_body_like_template_example():
    names = {"rss:blog": "公式ブログ", "hackernews": "Hacker News", "github_repos": "GitHub"}
    posted = Record(
        source_id="hackernews",
        url="https://news.ycombinator.com/item?id=0",
        title="外部のタイトル",
        published_precision="unknown",
        fetched_at=IN,
        events=(Event("posted", IN),),
        link_url="https://example.com/blog/mini-2",
        posted_at=IN,
    )
    repo = GitHubRepo("example/tiny-agent", OLD, 5000, star_delta_24h=1850)
    records = [
        record("blog/mini-2"),
        posted,
        record("tiny-agent", published_at=OLD, source_id="github_repos", github=repo),
    ]
    items = {f"i{n}": item for n, item in enumerate(aggregate(records), 1)}
    entries = [
        DailyEntry(("i2",), None, None, "oss", 1, "full", "急増", ("軽量",), "試しやすい", "紹介"),
        DailyEntry(("i1",), "i1", None, "ai", 1, "brief", "公開", ("小型",), "手元で試せる", None),
    ]
    body = render_daily(TARGET, RUN, entries, items, names, 40, (("Qiita", "タイムアウト"),)).body
    assert (
        body
        == f"""<!-- tech-researcher kind=report cadence=daily target=2026-10-23 run={RUN} -->
> **対象期間**：2026-10-23（金）00:00 〜 2026-10-24（土）00:00（JST）
> **掲載**：2 件
> ℹ️ 重要な情報が少ない日でした（掲載 2 件）。

## 目次

- 生成AI・LLM・AIエージェント（1 件）
- 開発者ツール（0 件）
- クラウド・インフラ（0 件）
- OSS（1 件）
- その他の新テーマ（0 件）

## 生成AI・LLM・AIエージェント（1 件）

#### 1. 公開

小型 **重要性**：手元で試せる
出典：一次情報 [公式ブログ（example.com）](https://example.com/blog/mini-2) 公開 2026-10-23 ／ \
[Hacker News（news.ycombinator.com）](https://news.ycombinator.com/item?id=0) 投稿 2026-10-23

## OSS（1 件）

#### 2. 急増

- **要点**
  - 軽量
- **重要性**：試しやすい
- **期間内の出来事**：紹介（Star +1,850／24時間）
- **出典**：[GitHub（example.com）](https://example.com/tiny-agent) 公開 2026-08-01

---
{FOOTER}

## 取得できなかった情報源

- Qiita：タイムアウト
"""
    )


def test_footer_then_unavailable_sources_at_the_end():
    body = render([entry(1)], unavailable=(("Qiita", "タイムアウト（再試行 3 回）"),)).body
    assert body.endswith(
        f"---\n{FOOTER}\n\n## 取得できなかった情報源\n\n- Qiita：タイムアウト（再試行 3 回）\n"
    )
    assert "取得できなかった情報源" not in render([entry(1)]).body


def test_footer_invites_comments():
    assert "コメント" in FOOTER and "項目2 は誤検出" in FOOTER
