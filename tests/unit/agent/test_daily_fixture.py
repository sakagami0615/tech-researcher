"""確認用のデータ（tests/fixtures/agent/daily-*.json）が、製品の形と検査に合っていることを確かめる。

- daily-input.json：② の入力の例（架空の内容）。下の収集レコードから build_daily_input で作ったもの
  と同じであることを確かめる（入力の形が変わったら、このファイルを直接実行して作り直す）。
  トークンが届いた後、agent コンテナに標準入力で渡して、スキル・スキーマ・ターン数を確かめるのに
  使う（README の「トークンを使う確認」）
- daily-success.json：その入力に対する Claude Code の成功の結果の JSON の例。形は
  not-logged-in.json（実際の出力）に合わせて組み立てた。structured_output はスキルの決まりどおりに
  書いたもので、スキーマと ③ の項目の検査（D1 を除く）をすべて通る
"""

import json
from datetime import UTC, date, datetime
from pathlib import Path

from tech_researcher.agent import invoke
from tech_researcher.agent.daily import InputItem, build_daily_input, parse_daily_output
from tech_researcher.collect.aggregate import aggregate
from tech_researcher.collect.records import Attention, Event, GitHubRepo, Record
from tech_researcher.config import QuoteLimits
from tech_researcher.periods import daily_target
from tech_researcher.stages.daily import _check_entries
from tech_researcher.validate import text
from tech_researcher.validate.schema import check_schema

FIXTURES = Path(__file__).parents[2] / "fixtures" / "agent"
INPUT = FIXTURES / "daily-input.json"
SUCCESS = FIXTURES / "daily-success.json"

TARGET = daily_target(date(2026, 10, 24), 1)  # 対象日 2026-10-23（金）
FETCHED = datetime(2026, 10, 23, 20, 50, tzinfo=UTC)  # JST 10/24 5:50（急上昇の窓の中）
CREATED = datetime(2026, 8, 2, 3, 0, tzinfo=UTC)
NAMES = {
    "rss:example-cloud": "Example Cloud ブログ",
    "zenn:trend": "Zenn",
    "github_releases": "GitHub Releases",
    "github_repos": "GitHub",
    "hackernews": "Hacker News",
    "qiita": "Qiita",
}


def _at(hour: int, day: int = 23) -> datetime:
    return datetime(2026, 10, day, hour, 0, tzinfo=UTC)


def records() -> list[Record]:
    """架空の収集レコード。同じ発表を扱う2件、リリース、急上昇のリポジトリと言及、価値の低い記事。"""
    return [
        Record(
            source_id="rss:example-cloud",
            url="https://blog.example.com/serverless-gpu",
            title="Example Cloud launches serverless GPU instances",
            excerpt="Run inference workloads without managing GPU servers.",
            primary_text=(
                "Today we are announcing serverless GPU instances. You can deploy a container "
                "image and Example Cloud starts GPU capacity on demand, scaling to zero when idle. "
                "Billing is per second. The preview is available in two regions, and general "
                "availability is planned for next year."
            ),
            primary_text_origin="rss_full",
            published_at=_at(0),
            published_precision="datetime",
            fetched_at=FETCHED,
            events=(Event("published", _at(0)),),
            links=("https://docs.example.com/gpu/serverless",),
        ),
        Record(
            source_id="zenn:trend",
            url="https://zenn.example.dev/articles/serverless-gpu-trial",
            title="Example Cloud のサーバーレス GPU を試してみた",
            excerpt="コンテナを置くだけで GPU 推論が動いた。コールドスタートは約20秒だった。",
            published_at=_at(6),
            published_precision="datetime",
            fetched_at=FETCHED,
            events=(Event("published", _at(6)),),
        ),
        Record(
            source_id="github_releases",
            url="https://github.com/example/pkgtool/releases/tag/v5.0.0",
            title="v5.0.0",
            primary_text=(
                "Breaking: the lockfile format changed to v3 and cannot be read by pkgtool 4.x. "
                "Run `pkgtool migrate` to convert existing lockfiles. Node 18 is no longer "
                "supported."
            ),
            primary_text_origin="github_release",
            published_at=_at(2),
            published_precision="datetime",
            fetched_at=FETCHED,
            events=(Event("released", _at(2)),),
            meta_terms=("example/pkgtool", "v5.0.0"),
        ),
        Record(
            source_id="github_repos",
            url="https://github.com/example/tiny-agent",
            title="example/tiny-agent",
            excerpt="A minimal runtime to run AI agents from a single config file.",
            primary_text=(
                "tiny-agent runs an agent loop defined in one YAML file. It has no runtime "
                "dependencies besides the Python standard library and supports tool calling "
                "through a small plugin interface."
            ),
            primary_text_origin="github_readme",
            fetched_at=FETCHED,
            # 公開は期間より前（作成日）で、期間内に Star が急増した（期間内の出来事が要る）
            published_at=CREATED,
            published_precision="datetime",
            events=(
                Event("published", CREATED),
                Event("attention_spike", FETCHED, basis="star_growth"),
            ),
            attention=(Attention("star_delta_24h", 1850, FETCHED),),
            github=GitHubRepo(
                full_name="example/tiny-agent",
                created_at=CREATED,
                stars_total=5400,
                star_delta_24h=1850,
                mentioned_by=("https://news.example.com/tiny-agent-review",),
            ),
            meta_terms=("example/tiny-agent",),
        ),
        Record(
            source_id="hackernews",
            url="https://news.example.com/tiny-agent-review",
            title="Show HN: tiny-agent, agents from one config file",
            published_at=datetime(2026, 10, 23, tzinfo=UTC),
            published_precision="date",
            fetched_at=FETCHED,
            events=(Event("published", datetime(2026, 10, 23, tzinfo=UTC)),),
            attention=(Attention("hn_points", 412, FETCHED), Attention("hn_comments", 96, FETCHED)),
        ),
        Record(
            source_id="qiita",
            url="https://qiita.example.com/items/0001",
            title="Python のリスト内包表記のおさらい",
            excerpt="基本的な書き方をまとめました。",
            published_at=_at(10),
            published_precision="datetime",
            fetched_at=FETCHED,
            events=(Event("published", _at(10)),),
            attention=(Attention("qiita_likes", 3, FETCHED),),
        ),
    ]


def build() -> dict:
    items = aggregate(records())
    inputs = [InputItem(f"d{n}", item) for n, item in enumerate(items, start=1)]
    return build_daily_input(TARGET, inputs, NAMES)


def test_input_fixture_is_what_the_code_builds():
    assert json.loads(INPUT.read_text(encoding="utf-8")) == build()


def test_input_fixture_covers_the_skill_branches():
    items = json.loads(INPUT.read_text(encoding="utf-8"))["items"]
    assert any(i["needs_period_event"] for i in items)  # 期間内の出来事が要る項目
    assert any(i["github"] for i in items)
    assert any(i["links"] for i in items)  # 一次情報の候補リンク
    assert {i["kind"] for i in items} >= {"article", "release", "repo"}


def test_success_fixture_parses_and_passes_checks():
    data = json.loads(INPUT.read_text(encoding="utf-8"))
    result = invoke.parse_result(0, SUCCESS.read_text(encoding="utf-8"))
    assert result.models == ("claude-opus-4-5-20251101",)
    assert result.usage.num_turns == 2
    check_schema(result.output)  # 製品のスキーマ（--json-schema に渡すもの）
    entries = parse_daily_output(result.output)
    items = {f"d{n}": item for n, item in enumerate(aggregate(records()), start=1)}
    kept, excluded = _check_entries(entries, data, items, QuoteLimits())
    assert excluded == []
    assert len(kept) == len(entries) == 3
    # スキルの決まりどおり、長さは目安（2倍ではなく目安そのもの）に収まり、一次情報は片方だけ
    for entry in entries:
        assert len(entry.headline) <= text.HEADLINE_LIMIT
        assert all(len(p) <= text.POINT_LIMIT for p in entry.points)
        assert len(entry.importance) <= text.IMPORTANCE_LIMIT[entry.depth]
        assert len(entry.period_event or "") <= text.PERIOD_EVENT_LIMIT
        assert len(entry.points) == 1 if entry.depth == "brief" else 1 <= len(entry.points) <= 3
        assert (entry.primary_item_id is None) or (entry.primary_link_id is None)


if __name__ == "__main__":  # 入力の形を変えたときに作り直す
    INPUT.write_text(json.dumps(build(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
