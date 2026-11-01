"""設定ファイル（TOML）の読み込みと検証（D-20、structure.md 4章）。

置くのは期間・情報源・急上昇のしきい値・last30days-skill の計画・上限値だけ。
知らない項目や型の誤りはエラーにする。
投稿先・Projects の指定・認証情報は worker.env に置くので、ここでは受け付けない。
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


# --plan に書ける情報源名。設定の sources.hackernews は "hackernews"、
# sources.github_search は "github" にあたる
LAST30DAYS_PLAN_SOURCES = frozenset({"hackernews", "github"})


@dataclass(frozen=True)
class Last30DaysSource:
    """last30days-skill で集める情報源（hackernews・github_search）。"""

    enabled: bool
    display_name: str


@dataclass(frozen=True)
class Subquery:
    """last30days-skill の --plan の1件（D-01）。sources は last30days-skill の情報源名。"""

    search_query: str
    ranking_query: str
    sources: tuple[str, ...]
    weight: float


@dataclass(frozen=True)
class GitHubRepos:
    """GitHub の新着・急上昇（D-13）。

    D-13 の記号との対応：threshold_increase＝A、report_top_n＝N_report、new_repo_days＝D、
    new_repo_stars＝C、search_days＝D_search、search_min_stars＝C_search。
    search_days・search_min_stars は観測（PoC）で決める。
    """

    enabled: bool
    display_name: str
    search_topics: tuple[str, ...]
    watch_repos: tuple[str, ...]
    threshold_increase: int
    report_top_n: int
    new_repo_days: int
    new_repo_stars: int
    search_days: int
    search_min_stars: int


@dataclass(frozen=True)
class GitHubReleases:
    enabled: bool
    display_name: str
    repos: tuple[str, ...]


@dataclass(frozen=True)
class Qiita:
    enabled: bool
    display_name: str
    tags: tuple[str, ...]


# フィードのどの欄を一次情報の本文・抜粋にするか（D-14）。none は本文なし
FEED_TEXT_FIELDS = ("content", "summary")


@dataclass(frozen=True)
class Feed:
    """RSS・Atom のフィード。kind は new（新着）か popular（人気型。D-13 の初登場の判定の対象）。

    full_text：全文が入る欄（content・summary・none）。excerpt：抜粋にする欄（content・summary）。
    entry_date：エントリーの日時を公開日時（published）とするか、投稿日時（posted）とするか。
    pages：WordPress のページ送り（?paged=N）で取るページ数。
    """

    id: str
    display_name: str
    url: str
    kind: str
    enabled: bool
    full_text: str = "content"
    excerpt: str = "summary"
    entry_date: str = "published"
    pages: int = 1


@dataclass(frozen=True)
class StageLimits:
    """日次・週次それぞれの上限。

    段階ごとの実行時間（分）と ② の入力（バイト）。structure.md 6.4・6.5。
    """

    collect: int
    collect_per_source: int
    analyze: int
    verify: int
    investigate: int
    post: int
    job: int
    agent_input_bytes: int


@dataclass(frozen=True)
class Limits:
    """再試行・実行時間・API 利用量の上限（D-21、structure.md 6章）。"""

    http_connect_timeout_s: int
    http_read_timeout_s: int
    http_retry_waits_s: tuple[int, ...]
    retry_after_max_s: int
    last30days_timeout_s: int
    last30days_retries: int
    github_rest_calls: int
    github_search_calls: int
    github_search_per_minute: int
    github_graphql_calls: int
    github_graphql_batch: int
    github_write_calls: int
    qiita_calls: int
    agent_max_turns: int | None
    daily: StageLimits
    weekly: StageLimits


@dataclass(frozen=True)
class Config:
    daily_period_days: int
    weekly_period_days: int
    hackernews: Last30DaysSource | None
    github_search: Last30DaysSource | None
    github_repos: GitHubRepos | None
    github_releases: GitHubReleases | None
    qiita: Qiita | None
    feeds: tuple[Feed, ...]
    subqueries: tuple[Subquery, ...]
    limits: Limits


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: TOML として読めない: {e}") from e
    return parse_config(data)


def parse_config(data: dict[str, Any]) -> Config:
    top = _Table(data, "")
    daily = top.table("daily")
    weekly = top.table("weekly")
    sources = top.table("sources", required=False)
    last30days = top.table("last30days", required=False)
    limits = top.table("limits")
    feeds = top.array_of_tables("feeds")
    top.done()

    config = Config(
        daily_period_days=_period_days(daily),
        weekly_period_days=_period_days(weekly),
        hackernews=_last30days_source(sources, "hackernews"),
        github_search=_last30days_source(sources, "github_search"),
        github_repos=_github_repos(sources),
        github_releases=_github_releases(sources),
        qiita=_qiita(sources),
        feeds=tuple(_feed(f) for f in feeds),
        subqueries=_subqueries(last30days),
        limits=_limits(limits),
    )
    if sources is not None:
        sources.done()
    _check_unique_feed_ids(config.feeds)
    return config


class _Table:
    """TOML の表を1つずつ読み、読まなかった項目を「知らない項目」としてエラーにする。"""

    def __init__(self, data: Any, path: str):
        if not isinstance(data, dict):
            raise ConfigError(f"{path or '設定'}: 表（テーブル）にする")
        self._data = data
        self._path = path
        self._seen: set[str] = set()

    def path_of(self, name: str) -> str:
        return f"{self._path}.{name}" if self._path else name

    def _get(self, name: str, required: bool) -> Any:
        self._seen.add(name)
        if name not in self._data:
            if required:
                raise ConfigError(f"{self.path_of(name)}: 必須の項目がない")
            return None
        return self._data[name]

    def table(self, name: str, required: bool = True) -> _Table | None:
        value = self._get(name, required)
        return None if value is None else _Table(value, self.path_of(name))

    def array_of_tables(self, name: str) -> list[_Table]:
        value = self._get(name, required=False)
        if value is None:
            return []
        if not isinstance(value, list):
            raise ConfigError(f"{self.path_of(name)}: 表の配列（[[{name}]]）にする")
        return [_Table(v, f"{self.path_of(name)}[{i}]") for i, v in enumerate(value)]

    def value(self, name: str, kind: type, required: bool = True) -> Any:
        value = self._get(name, required)
        if value is None:
            return None
        # bool は int の一種なので、int を求めるときは bool を拒否する
        if not isinstance(value, kind) or (kind in (int, float) and isinstance(value, bool)):
            raise ConfigError(f"{self.path_of(name)}: {kind.__name__} にする（{value!r}）")
        return value

    def text(self, name: str) -> str:
        value = self.value(name, str)
        if not value.strip():
            raise ConfigError(f"{self.path_of(name)}: 空にしない")
        return value

    def int_at_least(self, name: str, minimum: int = 1) -> int:
        value = self.value(name, int)
        if value < minimum:
            raise ConfigError(f"{self.path_of(name)}: {minimum} 以上にする（{value}）")
        return value

    def strings(self, name: str) -> tuple[str, ...]:
        value = self.value(name, list)
        if not all(isinstance(v, str) and v for v in value):
            raise ConfigError(f"{self.path_of(name)}: 空でない文字列の配列にする")
        return tuple(value)

    def done(self) -> None:
        unknown = sorted(set(self._data) - self._seen)
        if unknown:
            raise ConfigError(f"{self._path or '設定'}: 知らない項目 {', '.join(unknown)}")


def _period_days(t: _Table) -> int:
    value = t.int_at_least("period_days")
    t.done()
    return value


def _source(sources: _Table | None, name: str) -> _Table | None:
    return None if sources is None else sources.table(name, required=False)


def _last30days_source(sources: _Table | None, name: str) -> Last30DaysSource | None:
    t = _source(sources, name)
    if t is None:
        return None
    result = Last30DaysSource(t.value("enabled", bool), t.text("display_name"))
    t.done()
    return result


def _github_repos(sources: _Table | None) -> GitHubRepos | None:
    t = _source(sources, "github_repos")
    if t is None:
        return None
    result = GitHubRepos(
        enabled=t.value("enabled", bool),
        display_name=t.text("display_name"),
        search_topics=t.strings("search_topics"),
        watch_repos=t.strings("watch_repos"),
        threshold_increase=t.int_at_least("threshold_increase"),
        report_top_n=t.int_at_least("report_top_n"),
        new_repo_days=t.int_at_least("new_repo_days"),
        new_repo_stars=t.int_at_least("new_repo_stars"),
        search_days=t.int_at_least("search_days"),
        search_min_stars=t.int_at_least("search_min_stars", minimum=0),
    )
    t.done()
    return result


def _github_releases(sources: _Table | None) -> GitHubReleases | None:
    t = _source(sources, "github_releases")
    if t is None:
        return None
    result = GitHubReleases(t.value("enabled", bool), t.text("display_name"), t.strings("repos"))
    t.done()
    return result


def _qiita(sources: _Table | None) -> Qiita | None:
    t = _source(sources, "qiita")
    if t is None:
        return None
    result = Qiita(t.value("enabled", bool), t.text("display_name"), t.strings("tags"))
    t.done()
    return result


def _feed(t: _Table) -> Feed:
    kind = t.value("kind", str)
    if kind not in ("new", "popular"):
        raise ConfigError(f"{t.path_of('kind')}: new か popular にする（{kind!r}）")
    url = t.value("url", str)
    if not url.startswith("https://"):
        raise ConfigError(f"{t.path_of('url')}: https:// で始める（{url!r}）")
    full_text = _choice(t, "full_text", (*FEED_TEXT_FIELDS, "none"), "content")
    excerpt = _choice(t, "excerpt", FEED_TEXT_FIELDS, "summary")
    entry_date = _choice(t, "entry_date", ("published", "posted"), "published")
    pages = t.int_at_least("pages") if "pages" in t._data else 1
    result = Feed(
        t.text("id"),
        t.text("display_name"),
        url,
        kind,
        t.value("enabled", bool),
        full_text,
        excerpt,
        entry_date,
        pages,
    )
    t.done()
    return result


def _choice(t: _Table, name: str, choices: tuple[str, ...], default: str) -> str:
    value = t.value(name, str, required=False)
    if value is None:
        return default
    if value not in choices:
        raise ConfigError(f"{t.path_of(name)}: {'・'.join(choices)} のどれかにする（{value!r}）")
    return value


def _check_unique_feed_ids(feeds: tuple[Feed, ...]) -> None:
    ids = [f.id for f in feeds]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    if duplicated:
        raise ConfigError(f"feeds: id が重複している {', '.join(duplicated)}")


def _subqueries(t: _Table | None) -> tuple[Subquery, ...]:
    if t is None:
        return ()
    items = t.array_of_tables("subqueries")
    t.done()
    result = []
    for s in items:
        weight = s.value("weight", float)
        names = s.strings("sources")
        unknown = sorted(set(names) - LAST30DAYS_PLAN_SOURCES)
        if unknown:
            # last30days-skill は使えない名前を黙って除くので、誤字をここで止める（D-01・D-20）
            raise ConfigError(f"{s.path_of('sources')}: 使えない情報源名 {', '.join(unknown)}")
        if weight <= 0:
            raise ConfigError(f"{s.path_of('weight')}: 0 より大きくする（{weight}）")
        result.append(
            Subquery(
                s.text("search_query"),
                s.text("ranking_query"),
                names,
                weight,
            )
        )
        s.done()
    return tuple(result)


def _stage_limits(t: _Table) -> StageLimits:
    result = StageLimits(
        collect=t.int_at_least("collect_minutes"),
        collect_per_source=t.int_at_least("collect_per_source_minutes"),
        analyze=t.int_at_least("analyze_minutes"),
        verify=t.int_at_least("verify_minutes"),
        investigate=t.int_at_least("investigate_minutes"),
        post=t.int_at_least("post_minutes"),
        job=t.int_at_least("job_minutes"),
        agent_input_bytes=t.int_at_least("agent_input_bytes"),
    )
    t.done()
    # 段階ごとの上限の合計がジョブ全体に収まること（structure.md 6.4）
    total = result.collect + result.analyze + result.verify + result.investigate + result.post
    if total > result.job:
        raise ConfigError(
            f"{t._path}: 段階の上限の合計 {total} 分がジョブ全体 {result.job} 分を超える"
        )
    if result.collect_per_source > result.collect:
        raise ConfigError(f"{t._path}: collect_per_source_minutes が collect_minutes を超える")
    return result


def _limits(t: _Table) -> Limits:
    waits = t.value("http_retry_waits_s", list)
    if not all(isinstance(w, int) and not isinstance(w, bool) and w >= 0 for w in waits):
        raise ConfigError("limits.http_retry_waits_s: 0 以上の整数の配列にする")
    turns = t.value("agent_max_turns", int, required=False)
    if turns is not None and turns < 1:
        raise ConfigError(f"limits.agent_max_turns: 1 以上にする（{turns}）")
    result = Limits(
        http_connect_timeout_s=t.int_at_least("http_connect_timeout_s"),
        http_read_timeout_s=t.int_at_least("http_read_timeout_s"),
        http_retry_waits_s=tuple(waits),
        retry_after_max_s=t.int_at_least("retry_after_max_s"),
        last30days_timeout_s=t.int_at_least("last30days_timeout_s"),
        last30days_retries=t.int_at_least("last30days_retries", minimum=0),
        github_rest_calls=t.int_at_least("github_rest_calls"),
        github_search_calls=t.int_at_least("github_search_calls"),
        github_search_per_minute=t.int_at_least("github_search_per_minute"),
        github_graphql_calls=t.int_at_least("github_graphql_calls"),
        github_graphql_batch=t.int_at_least("github_graphql_batch"),
        github_write_calls=t.int_at_least("github_write_calls"),
        qiita_calls=t.int_at_least("qiita_calls"),
        agent_max_turns=turns,
        daily=_stage_limits(t.table("daily")),
        weekly=_stage_limits(t.table("weekly")),
    )
    t.done()
    return result
