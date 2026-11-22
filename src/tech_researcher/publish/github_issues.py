"""レポート Issue の GitHub からの読み取り（重複判定用。report-templates.md 4.3、D-05・D-21）。

- トークン：Issue 用トークン（fine-grained。Issues の読み書き）を worker の環境変数
  `GITHUB_ISSUE_TOKEN` から読む。収集用の `GITHUB_TOKEN` とは別に扱う。投稿先のリポジトリは
  `GITHUB_ISSUE_REPO`（`owner/repo`。D-20）。値はログ・例外のメッセージに出さない
- GET のリダイレクトは自動でたどらない。301（転送された Issue）を見分けるため、Location を
  自分で検めて、API の Issue の形のときだけ数回たどる
- 識別子は本文の1行目だけを読む。タイトル・検索 API は使わない
- 再試行は http.fetch に任せる（タイムアウト・接続の失敗・429・5xx）
"""

import os
import re
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlencode, urljoin

from tech_researcher.collect.github_api import (
    API_ROOT,
    API_VERSION,
    TOKEN_FORMAT,
    classify,
    load_json,
)
from tech_researcher.collect.http import FetchError, Response, fetch, time_limit
from tech_researcher.config import GITHUB_REPO_NAME, Limits

TOKEN_ENV = "GITHUB_ISSUE_TOKEN"
REPO_ENV = "GITHUB_ISSUE_REPO"
PER_PAGE = 100
MAX_PAGES = 100  # 一覧の上限（1万件）。超えたら、見落として重複させないよう失敗にする
MAX_TRANSFERS = 3  # 転送をたどる回数の上限
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_NAME = r"[A-Za-z0-9_.\-]+"
# 転送先の Location に使える形（API の Issue の URL だけ）
_ISSUE_PATH = re.compile(rf"/(?:repositories/[0-9]+|repos/{_NAME}/{_NAME})/issues/[0-9]+")
_HTML_URL = re.compile(rf"https://github\.com/({_NAME}/{_NAME})/issues/([0-9]+)")
# 本文の1行目の識別子（4.1）。値は英数字と - だけ。ほかの形は識別子として扱わない
_MARKER = re.compile(
    r"<!-- tech-researcher kind=report cadence=(daily|weekly) target=(\d{4}-\d{2}-\d{2})"
    r" run=([A-Za-z0-9-]+) -->"
)


class IssueReadError(Exception):
    """Issue を読めなかった（実行エラーにする）。category は github_api の分類の語など。

    メッセージにはトークンを入れない。
    """

    def __init__(self, message: str, category: str, status: int | None = None):
        super().__init__(message)
        self.category = category
        self.status = status


@dataclass(frozen=True)
class IssueRef:
    repo: str
    number: int


@dataclass(frozen=True)
class IssueLookup:
    """番号の Issue の取得結果。ref は見つかった Issue、なければ None（404・410）。

    転送されていれば（301）、ref は転送先で transferred が True。
    """

    ref: IssueRef | None
    transferred: bool = False
    status: int | None = None  # ref がないときの 404・410


@dataclass(frozen=True)
class Marker:
    """レポート Issue の1行目の識別子。"""

    cadence: str
    target: date
    run_id: str


def _is_number(value) -> bool:
    """Issue の番号。bool（True）や 0・負の数を通さない。"""
    return type(value) is int and value > 0


def parse_marker(body: str | None) -> Marker | None:
    """本文の1行目だけを読む。識別子でなければ None（2行目以降は見ない）。"""
    if not body:
        return None
    first = body.split("\n", 1)[0].rstrip("\r")
    match = _MARKER.fullmatch(first)
    if match is None:
        return None
    try:
        return Marker(match[1], date.fromisoformat(match[2]), match[3])
    except ValueError:  # 2026-13-45 のような日付
        return None


@dataclass(frozen=True)
class ListedIssue:
    number: int
    marker: Marker | None


def issue_token(environ: Mapping[str, str] = os.environ) -> str:
    """Issue 用トークン。ないか使えない文字を含めば IssueReadError（値は出さない）。"""
    value = (environ.get(TOKEN_ENV) or "").strip()
    if not value:
        raise IssueReadError(f"{TOKEN_ENV} が設定されていない", "auth")
    if not TOKEN_FORMAT.fullmatch(value):
        raise IssueReadError(f"{TOKEN_ENV} に使えない文字が含まれる", "auth")
    return value


def issue_repo(environ: Mapping[str, str] = os.environ) -> str:
    """投稿先のリポジトリ（`owner/repo`、小文字にそろえる）。"""
    value = (environ.get(REPO_ENV) or "").strip().lower()
    if not GITHUB_REPO_NAME.fullmatch(value):
        raise IssueReadError(f"{REPO_ENV} が owner/repo の形でない", "config")
    return value


class IssueReader:
    """Issue 用トークンで、Issue の GET だけを行う。fetch_url はテストで差し替える。"""

    def __init__(
        self,
        token: str,
        limits: Limits,
        *,
        fetch_url: Callable[..., Response] = fetch,
        deadline: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not TOKEN_FORMAT.fullmatch(token):
            raise IssueReadError("トークンに使えない文字が含まれる", "auth")
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "Authorization": f"Bearer {token}",
        }
        self._limits = limits
        self._fetch = fetch_url
        # 要求を出してよい時間の上限（clock の値。⑤ の段階の上限。structure.md 6.4）
        self._deadline = deadline
        self._clock = clock

    def __repr__(self) -> str:  # トークンを repr に出さない
        return "IssueReader()"

    def get_issue(self, repo: str, number: int) -> IssueLookup:
        """番号の Issue を取る。301 は転送先をたどり、404・410 は ref なしで返す。ほかは失敗。"""
        url = f"{API_ROOT}/repos/{repo}/issues/{number}"
        transfers = 0
        while True:
            try:
                response = self._get(url)
            except FetchError as e:
                if e.status in _REDIRECTS:
                    if e.status != 301 or transfers >= MAX_TRANSFERS:
                        raise _error(url, "redirect", e.status) from e
                    url = _transfer_target(url, e.headers)
                    transfers += 1
                    continue
                if e.status in (404, 410):
                    return IssueLookup(None, status=e.status)
                raise _error(url, classify(e), e.status) from e
            break
        data = _json(url, response)
        if (
            not isinstance(data, dict)
            or not _is_number(data.get("number"))
            or "pull_request" in data
            or (not transfers and data["number"] != number)
        ):
            raise _error(url, "invalid_response", None)
        if not transfers:
            return IssueLookup(IssueRef(repo, number))
        match = _HTML_URL.fullmatch(str(data.get("html_url")))
        new_repo = match[1].lower() if match else ""
        # Location の末尾・応答の number・html_url の番号が一致し、repo が投稿先として使える形のとき
        if (
            match is None
            or not GITHUB_REPO_NAME.fullmatch(new_repo)
            or not (int(match[2]) == data["number"] == int(url.rsplit("/", 1)[1]))
            or not _same_repo(url, new_repo, data.get("repository_url"))
        ):
            raise _error(url, "invalid_response", None)
        return IssueLookup(IssueRef(new_repo, data["number"]), transferred=True)

    def list_report_issues(self, repo: str, cadence: str) -> Iterator[ListedIssue]:
        """`type:report` と `cadence:{cadence}` のラベルの Issue を、Open・Closed とも全ページ読む。

        番号の古い順。プルリクエストは除く。本文の1行目だけを識別子として読む。
        """
        for page in range(1, MAX_PAGES + 1):
            params = {
                "labels": f"type:report,cadence:{cadence}",
                "state": "all",
                "sort": "created",
                "direction": "asc",
                "per_page": PER_PAGE,
                "page": page,
            }
            url = f"{API_ROOT}/repos/{repo}/issues?{urlencode(params)}"
            try:
                response = self._get(url)
            except FetchError as e:
                category = "redirect" if e.status in _REDIRECTS else classify(e)
                raise _error(url, category, e.status) from e
            items = _json(url, response)
            if not isinstance(items, list):
                raise _error(url, "invalid_response", None)
            for item in items:
                if not isinstance(item, dict) or not _is_number(item.get("number")):
                    raise _error(url, "invalid_response", None)
                if "pull_request" in item:
                    continue
                names = _label_names(item.get("labels"))
                if names is None:
                    raise _error(url, "invalid_response", None)
                if not {"type:report", f"cadence:{cadence}".casefold()} <= names:
                    continue  # ラベルが付いていない（外された）ものは候補にしない
                body = item.get("body")
                marker = parse_marker(body if isinstance(body, str) else None)
                yield ListedIssue(item["number"], marker)
            if len(items) < PER_PAGE:
                return
        raise IssueReadError(f"ラベル付きの Issue が {MAX_PAGES} ページを超えた", "too_many")

    def _get(self, url: str) -> Response:
        return self._fetch(
            url,
            self._limits,
            headers=self._headers,
            follow_redirects=False,
            **time_limit(self._deadline, self._clock),
        )


def _same_repo(url: str, repo: str, repository_url) -> bool:
    """html_url の repo が、Location（`/repos/{repo}/issues/N` の形のとき）と応答の repository_url
    と一致するか。大文字・小文字は区別しない。Location が `/repositories/{id}/...` の形なら
    repository_url は必須。"""
    path = url.removeprefix(API_ROOT)
    if path.startswith("/repos/") and "/".join(path.split("/")[2:4]).casefold() != repo.casefold():
        return False
    if repository_url is None and path.startswith("/repositories/"):
        return False  # Location に repo 名がないので、応答の repository_url で確かめる
    if repository_url is not None and (
        not isinstance(repository_url, str)
        or repository_url.casefold() != f"{API_ROOT}/repos/{repo}".casefold()
    ):
        return False
    return True


def _label_names(labels) -> set[str] | None:
    """応答の labels のラベル名。形が壊れていれば None。"""
    if not isinstance(labels, list):
        return None
    names = set()
    for label in labels:
        if not isinstance(label, dict) or not isinstance(label.get("name"), str):
            return None
        names.add(label["name"].casefold())
    return names


def _transfer_target(url: str, headers: Mapping[str, str]) -> str:
    """301 の Location。API の Issue の URL でなければ失敗（認証のヘッダを別の先に送らない）。"""
    location = {k.lower(): v for k, v in headers.items()}.get("location", "").strip()
    target = urljoin(url, location) if location else ""
    path = target[len(API_ROOT) :]
    if (
        not target.startswith(API_ROOT + "/")
        or not _ISSUE_PATH.fullmatch(path)
        or {".", ".."} & set(path.split("/"))
    ):
        raise _error(url, "redirect", 301)
    return target


def _json(url: str, response: Response):
    try:
        return load_json(response.body)
    except (ValueError, RecursionError) as e:
        raise _error(url, "invalid_response", None) from e


def _error(url: str, category: str, status: int | None) -> IssueReadError:
    path = url.removeprefix(API_ROOT).split("?", 1)[0]
    return IssueReadError(f"{path}：{category}（HTTP {status}）", category, status)
