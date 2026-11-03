"""GitHub のリポジトリの候補集め（D-13 の判定の流れの 1、data-model.md 2.6・3.4）。

Star 数を測る候補を、次の3つの経路から集めて1つにまとめる。

- search（新規）：Search API で、作成日が対象日の D_search 日前以降、Star が C_search 以上の
  リポジトリを、設定のトピックごとに Star 数の順で取る
- mention（言及）：その回に集めたほかの情報源のレコードの URL・指している先・候補リンクから
  `github.com/{owner}/{repo}` を抜き出す。レコードを受け取るだけで、ほかの収集処理には依存しない
- watch（監視リスト）：直近 WATCH_DAYS 日の間に初めて Star 数を測ったリポジトリ
  （star_snapshots）と、設定の watch_repos

Search の失敗では止めず、取れた分と、ほかの経路の候補を返す（error に最初の失敗を残す）。
GitHub Trending のページや外部サービスは使わない（D-13）。
"""

import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urlsplit

from tech_researcher.collect import github_api
from tech_researcher.collect.aggregate import record_key
from tech_researcher.collect.github_api import GitHubClient, GitHubError
from tech_researcher.collect.records import Record
from tech_researcher.collect.urls import normalize_url
from tech_researcher.config import GITHUB_REPO_NAME, GitHubRepos
from tech_researcher.db.star_snapshots import recent_repos

log = logging.getLogger(__name__)

# 監視リストに入れる、過去に候補になったリポジトリの期間（日）。D-13 の例の値
WATCH_DAYS = 30
ROUTES = ("search", "mention", "watch")
PER_PAGE = 100  # Search API の1ページの上限
MAX_SEARCH_RESULTS = 1000  # Search API が1つの検索で返す上限
# github.com の1つめのパスがリポジトリの持ち主でないもの（GitHub の機能のページ）。
# 漏れたものは候補に入っても、Star 数を測るときにリポジトリが見つからないだけになる
NON_OWNER_PATHS = frozenset(
    {
        "about",
        "account",
        "advisories",
        "apps",
        "blog",
        "codespaces",
        "collections",
        "contact",
        "copilot",
        "customer-stories",
        "dashboard",
        "enterprise",
        "events",
        "explore",
        "features",
        "github-copilot",
        "home",
        "issues",
        "join",
        "login",
        "logout",
        "marketplace",
        "new",
        "notifications",
        "organizations",
        "partners",
        "orgs",
        "pricing",
        "pulls",
        "readme",
        "resources",
        "search",
        "security",
        "settings",
        "signup",
        "site",
        "solutions",
        "sponsors",
        "stars",
        "team",
        "topics",
        "trending",
        "users",
    }
)


@dataclass(frozen=True)
class Candidate:
    """Star 数を測る候補。repo は `owner/repo`（小文字）。

    routes は見つけた経路（ROUTES の順）。PoC で経路ごとの効果を数えるのに使う（D-13）。
    mentioned_by は言及元の項目の URL（mention の経路のときだけ）。
    """

    repo: str
    routes: tuple[str, ...]
    mentioned_by: tuple[str, ...] = ()

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repo}"


@dataclass(frozen=True)
class CandidateResult:
    """候補集めの結果。error・error_category は Search の失敗（ほかの経路の候補は残る）。

    search_attempts は Search の要求の回数（再試行を含む）。
    """

    candidates: tuple[Candidate, ...]
    search_attempts: int
    error: str | None = None
    error_category: str | None = None


def collect_candidates(
    settings: GitHubRepos,
    day: date,
    records: Iterable[Record],
    client: GitHubClient | None,
    conn: sqlite3.Connection | None,
) -> CandidateResult:
    """3つの経路から候補を集め、リポジトリの名前の順で返す。

    day は対象日（日次の対象日 T）。records はその回にほかの情報源から集めたレコード。
    client が None か認証なしなら Search はせず、その経路を失敗とする（D-05 は収集用トークンで
    取る前提で、認証なしの上限は1分10回と小さいため）。conn が None なら DB の監視リストは使わない。
    """
    if not settings.enabled:
        return CandidateResult((), 0)
    searched, attempts, error = _search(settings, day, client)
    mentions = mentioned_repos(records)
    watched = list(settings.watch_repos)
    if conn is not None:
        watched += recent_repos(conn, day, WATCH_DAYS)
    found: dict[str, set[str]] = {}  # repo → 経路
    for route, repos in (("search", searched), ("mention", mentions), ("watch", watched)):
        for repo in repos:
            found.setdefault(repo, set()).add(route)
    candidates = tuple(
        Candidate(repo, tuple(r for r in ROUTES if r in found[repo]), mentions.get(repo, ()))
        for repo in sorted(found)
    )
    if error is None:
        return CandidateResult(candidates, attempts)
    return CandidateResult(candidates, attempts, str(error), error.category)


def _search(
    settings: GitHubRepos, day: date, client: GitHubClient | None
) -> tuple[list[str], int, GitHubError | None]:
    """Search の経路。（見つけたリポジトリ、要求の回数、最初の失敗）を返す。"""
    if not settings.search_topics:
        return [], 0, None
    if client is None or not client.authenticated:
        return [], 0, GitHubError("収集用トークンがない", github_api.AUTH, 0)
    before = client.search_calls
    repos, error = search_repos(client, settings, day)
    return repos, client.search_calls - before, error


# これらの失敗では、次のトピックも同じく失敗するので Search をやめる
_STOP_CATEGORIES = frozenset({github_api.AUTH, github_api.RATE_LIMITED, github_api.BUDGET})


def search_repos(
    client: GitHubClient, settings: GitHubRepos, day: date
) -> tuple[list[str], GitHubError | None]:
    """設定のトピックごとに Search API を呼び、（見つかったリポジトリ、最初の失敗）を返す。

    Star 数の順にページを送り、件数が尽きるか、Search の上限（1,000件）まで取る。
    あるトピックで失敗したら、そのトピックをやめて次へ進む。認証・レート制限・この実行の
    上限の失敗では、残りのトピックも呼ばずにやめる。重なったリポジトリは除く。
    """
    since = day - timedelta(days=settings.search_days)
    repos: dict[str, None] = {}
    first_error: GitHubError | None = None
    for topic in settings.search_topics:
        try:
            repos.update(dict.fromkeys(_search_topic(client, topic, since, settings)))
        except GitHubError as e:
            first_error = first_error or e
            if e.category in _STOP_CATEGORIES:
                break
    return list(repos), first_error


def _search_topic(
    client: GitHubClient, topic: str, since: date, settings: GitHubRepos
) -> list[str]:
    query = search_query(topic, since, settings.search_min_stars)
    params = {"q": query, "sort": "stars", "order": "desc", "per_page": PER_PAGE}
    names: list[str] = []
    for page in range(1, MAX_SEARCH_RESULTS // PER_PAGE + 1):
        data = client.search("repositories", {**params, "page": page})
        page_names = parse_search(data)
        names += page_names
        if data.get("incomplete_results"):
            log.warning("GitHub Search：%s の結果が途中で切られた（page %d）", topic, page)
        total = min(data["total_count"], MAX_SEARCH_RESULTS)
        if len(page_names) < PER_PAGE or page * PER_PAGE >= total:
            break
    return names


def search_query(topic: str, since: date, min_stars: int) -> str:
    """トピック・作成日の下限・Star の下限の検索条件（GitHub の検索の構文）。"""
    return f"topic:{topic} created:>={since.isoformat()} stars:>={min_stars}"


def parse_search(data: object) -> list[str]:
    """Search の応答から `owner/repo`（小文字）を取り出す。形が違えば GitHubError。"""
    try:
        if not isinstance(data, dict) or not isinstance(data["total_count"], int):
            raise TypeError("total_count がない")
        names = [item["full_name"] for item in data["items"]]
        if not all(isinstance(n, str) and GITHUB_REPO_NAME.fullmatch(n.lower()) for n in names):
            raise ValueError("full_name が owner/repo の形でない")
    except (KeyError, TypeError, ValueError) as e:
        raise GitHubError(
            f"Search の応答として読めない：{type(e).__name__}", github_api.INVALID_RESPONSE, 0
        ) from e
    return [n.lower() for n in names]


def mentioned_repos(records: Iterable[Record]) -> dict[str, tuple[str, ...]]:
    """レコードの URL・指している先・候補リンクに現れたリポジトリを、言及元とともに返す。

    言及元は、そのレコードがまとまる収集項目の URL（指している先の正規化した URL）。
    それがリポジトリ自身の URL（投稿がリポジトリへ直接リンクした場合）なら、レコード自身の
    URL（はてなのエントリーのページなど）にし、それも同じなら言及元に入れない（言及の経路には入る）。
    返す順はリポジトリを最初に見た順で、言及元も見た順に重なりを除く。
    """
    found: dict[str, dict[str, None]] = {}
    for record in records:
        key = record_key(record)
        if key is None:
            continue
        for url in (record.url, record.link_url, *record.links):
            repo = repo_from_url(url or "")
            if repo is None:
                continue
            sources = found.setdefault(repo, {})
            own = f"https://github.com/{repo}"
            source = key if key != own else normalize_url(record.url)
            if source is not None and source != own:
                sources[source] = None
    return {repo: tuple(sources) for repo, sources in found.items()}


def repo_from_url(url: str) -> str | None:
    """`https://github.com/{owner}/{repo}` で始まる URL の `owner/repo`（小文字）。ほかは None。"""
    normalized = normalize_url(url)
    if normalized is None:
        return None
    parts = urlsplit(normalized)
    if parts.netloc != "github.com":
        return None
    segments = parts.path.split("/")[1:3]
    if len(segments) < 2 or segments[0] in NON_OWNER_PATHS:
        return None
    repo = "/".join(segments)
    return repo if GITHUB_REPO_NAME.fullmatch(repo) else None
