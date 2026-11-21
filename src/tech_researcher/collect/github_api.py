"""GitHub の REST API・GraphQL API の共通の取得処理（D-05・D-21、structure.md 6.3・6.5）。

GitHub の情報源（リポジトリの候補集め、Star 数の測定、Releases・README など）から共通に使う。

- トークン：収集用トークンを worker の環境変数 `GITHUB_TOKEN` から読み（D-05）、
  `Authorization: Bearer` で付ける。値はログ・例外のメッセージに出さない。要求の先は
  `https://api.github.com` に固定し、リダイレクトの先には送らない（http.fetch の headers）
- 利用量：1回の実行の中で、REST は limits.github_rest_calls 回、Search は
  limits.github_search_calls 回まで（再試行も数える）。Search は1分 limits.github_search_per_minute
  回の間隔をあけて出す。GraphQL は limits.github_graphql_calls 回まで。上限に達したら要求を
  出さずに失敗にする（structure.md 6.5）
- 再試行：http.fetch に任せる（タイムアウト・接続の失敗・429・5xx。ほかの 4xx はしない）
- エラーの分類：source_fetches の error_category（data-model.md 3.4）に使う短い語にする

1回の実行で1つの GitHubClient を作り、GitHub の情報源のあいだで使い回す（利用量を合算するため）。
"""

import json
import os
import re
import socket
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from typing import Any
from urllib.parse import urlencode, urljoin

from tech_researcher.collect.http import FetchError, Response, fetch
from tech_researcher.config import Limits

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
GRAPHQL_PATH = "/graphql"
TOKEN_ENV = "GITHUB_TOKEN"  # 収集用トークン（D-05）
# たどるリダイレクトの状態コードと回数の上限
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 3
# トークンに使える形。ヘッダに入れたときに要求を壊す文字（改行など）を止める。
# 不正な値を http.client に渡すと、例外のメッセージに値がそのまま入るため、先に確かめる
TOKEN_FORMAT = re.compile(r"[A-Za-z0-9_.\-]+")
# 応答の JSON の入れ子の深さの上限。一番外の配列・オブジェクトを 1 と数え、
# 値（文字列や数）は数えない。# GitHub の正しい応答はずっと浅い。
# json の RecursionError に頼ると、Python の版によっては深い入れ子も読めてしまうため、自分で数える
MAX_JSON_DEPTH = 64
_END = object()
# API のパス。`/` で区切った空でない部分だけにし、ホストを差し替えられないようにする
# （`.`・`..` の部分は別に止める）
_PATH = re.compile(r"(?:/[A-Za-z0-9_.\-]+)+")
# リダイレクトの先のクエリに使えない文字（印字できる ASCII 以外と #）
_UNSAFE_QUERY = re.compile(r"[^\x21-\x7e]|#")

# error_category に使う語
AUTH = "auth"  # 401。トークンがない・無効
RATE_LIMITED = "rate_limited"  # 429、または 403 でレート制限の印がある
FORBIDDEN = "forbidden"  # レート制限以外の 403
NOT_FOUND = "not_found"
HTTP_4XX = "http_4xx"
HTTP_5XX = "http_5xx"
TIMEOUT = "timeout"
DNS = "dns"
NETWORK = "network"
BUDGET = "budget"  # この実行の利用量の上限に達した
REDIRECT = "redirect"  # API の外へのリダイレクト、またはリダイレクトが多すぎる
INVALID_RESPONSE = "invalid_response"
GRAPHQL = "graphql_error"  # GraphQL の応答の errors（問い合わせ全体の誤り）


class GitHubError(Exception):
    """GitHub の API の失敗。category は上の分類、attempts は初回を含めた要求の回数。

    メッセージにはトークンを入れない（状態コードと分類、パスだけ）。
    """

    def __init__(self, message: str, category: str, attempts: int, status: int | None = None):
        super().__init__(message)
        self.category = category
        self.attempts = attempts
        self.status = status


def collect_token(environ: Mapping[str, str] = os.environ) -> str | None:
    """収集用トークン。ないか空なら None（認証なしで使う側が扱いを決める）。

    env_file の改行コード（CRLF）で付く前後の空白は除く。使えない文字を含めば GitHubError
    （値はメッセージに入れない）。
    """
    value = (environ.get(TOKEN_ENV) or "").strip()
    if not value:
        return None
    if not TOKEN_FORMAT.fullmatch(value):
        raise GitHubError(f"{TOKEN_ENV} に使えない文字が含まれる", AUTH, 0)
    return value


class GitHubClient:
    """GitHub の REST API の GET と GraphQL API の POST。1回の実行の中で使った回数を数える。

    token が None なら認証なしで要求する（GitHub の上限は小さくなる。structure.md 6.2）。
    fetch_url・clock・sleep はテストで差し替える。
    """

    def __init__(
        self,
        token: str | None,
        limits: Limits,
        *,
        fetch_url: Callable[..., Response] = fetch,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if token is not None and not TOKEN_FORMAT.fullmatch(token):
            raise GitHubError("トークンに使えない文字が含まれる", AUTH, 0)
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }
        if token is not None:
            self._headers["Authorization"] = f"Bearer {token}"
        self._limits = limits
        self._fetch = fetch_url
        self._clock = clock
        self._sleep = sleep
        self.rest_calls = 0  # Search 以外の REST の要求の回数（再試行を含む）
        self.search_calls = 0  # Search の要求の回数（再試行を含む）
        self.graphql_calls = 0  # GraphQL の要求の回数（再試行を含む）
        self._last_search: float | None = None

    @property
    def authenticated(self) -> bool:
        return "Authorization" in self._headers

    def __repr__(self) -> str:  # トークンを repr に出さない
        return f"GitHubClient(authenticated={self.authenticated})"

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """REST の GET（Search 以外）。応答の JSON を返す。"""
        if _is_search(path):
            raise ValueError("Search は search() で呼ぶ")
        remaining = self._limits.github_rest_calls - self.rest_calls
        try:
            data, used = self._request(path, params, remaining)
        except GitHubError as e:
            self.rest_calls += e.attempts
            raise
        self.rest_calls += used
        return data

    def search(self, kind: str, params: Mapping[str, Any]) -> Any:
        """Search API（`/search/{kind}`）。前の Search から間隔をあけて出す（再試行も）。

        間隔は設定の1分あたりの回数から決める。認証なしの GitHub の上限（1分10回）は
        考えないので、認証なしで続けて呼ぶ側は回数に気をつける。
        """
        interval = 60 / self._limits.github_search_per_minute
        remaining = self._limits.github_search_calls - self.search_calls
        if remaining >= 1 and self._last_search is not None:
            wait = self._last_search + interval - self._clock()
            if wait > 0:
                self._sleep(wait)
        try:
            data, used = self._request(f"/search/{kind}", params, remaining, interval)
        except GitHubError as e:
            self.search_calls += e.attempts
            if e.attempts:
                self._last_search = self._clock()
            raise
        self.search_calls += used
        self._last_search = self._clock()
        return data

    def graphql(self, query: str, variables: Mapping[str, Any]) -> Any:
        """GraphQL API に POST で問い合わせ、応答の JSON を返す（errors の扱いは呼ぶ側）。

        GraphQL は認証なしでは使えないので、トークンがなければ要求を出さずに AUTH の失敗にする。
        POST だが、読み取りの問い合わせだけを送るので、http.fetch の再試行に任せてよい。
        リダイレクトはたどらない（GraphQL の入口は1つで、転送される理由がないため）。
        """
        if not self.authenticated:
            raise GitHubError(f"{GRAPHQL_PATH}：収集用トークンがない", AUTH, 0)
        left = self._limits.github_graphql_calls - self.graphql_calls
        if left < 1:
            raise GitHubError(f"{GRAPHQL_PATH}：この実行の要求の上限に達した", BUDGET, 0)
        body = json.dumps({"query": query, "variables": dict(variables)}).encode()
        try:
            response = self._fetch(
                API_ROOT + GRAPHQL_PATH,
                replace(
                    self._limits, http_retry_waits_s=self._limits.http_retry_waits_s[: left - 1]
                ),
                headers={**self._headers, "Content-Type": "application/json"},
                sleep=self._sleep,
                follow_redirects=False,
                data=body,
            )
        except FetchError as e:
            self.graphql_calls += e.attempts
            category = REDIRECT if e.status in _REDIRECTS else classify(e)
            message = f"{GRAPHQL_PATH}：{category}（{e}）"
            raise GitHubError(message, category, e.attempts, e.status) from e
        self.graphql_calls += response.attempts
        try:
            return load_json(response.body)
        except (ValueError, RecursionError) as e:  # 入れ子が深すぎる JSON も読めない応答とする
            message = f"{GRAPHQL_PATH}：応答が JSON でない"
            raise GitHubError(message, INVALID_RESPONSE, response.attempts) from e

    def _request(
        self,
        path: str,
        params: Mapping[str, Any] | None,
        remaining: int,
        min_wait: float = 0.0,
    ) -> tuple[Any, int]:
        """要求して（JSON, 要求の回数）を返す。再試行は残りの回数に収まる分だけにする。

        min_wait は再試行の前に待つ最短の秒数（Search の間隔を再試行にも守らせる）。
        リダイレクト（名前を変えたリポジトリなど）は http.fetch にたどらせず、ここで1回ずつ
        要求として数えてたどる。先は API_ROOT の中だけにする（認証のヘッダを付けて送るため）。
        """
        if not _PATH.fullmatch(path) or {".", ".."} & set(path.split("/")):
            raise ValueError(f"API のパスにできない：{path!r}")
        url = API_ROOT + path + (f"?{urlencode(params)}" if params else "")
        used = 0
        for _ in range(MAX_REDIRECTS + 1):
            left = remaining - used
            if left < 1:
                raise GitHubError(f"{path}：この実行の要求の上限に達した", BUDGET, used)
            try:
                response = self._fetch(
                    url,
                    replace(
                        self._limits, http_retry_waits_s=self._limits.http_retry_waits_s[: left - 1]
                    ),
                    headers=self._headers,
                    sleep=lambda seconds: self._sleep(max(seconds, min_wait)),
                    follow_redirects=False,
                )
            except FetchError as e:
                used += e.attempts
                if e.status in _REDIRECTS:
                    url = _redirect_target(url, e.headers, _is_search(path))
                    if url is not None:
                        self._sleep(min_wait)  # Search の間隔はリダイレクトの先にも守る
                        continue
                    raise GitHubError(f"{path}：たどれないリダイレクト", REDIRECT, used) from e
                category = classify(e)
                raise GitHubError(f"{path}：{category}（{e}）", category, used, e.status) from e
            used += response.attempts
            break
        else:
            raise GitHubError(f"{path}：リダイレクトが多すぎる", REDIRECT, used)
        try:
            return load_json(response.body), used
        except (ValueError, RecursionError) as e:  # 深すぎる入れ子も読めない応答とする
            raise GitHubError(f"{path}：応答が JSON でない", INVALID_RESPONSE, used) from e


def load_json(body: bytes | str) -> Any:
    """JSON を読み、入れ子が MAX_JSON_DEPTH より深ければ ValueError にする。

    深さは再帰を使わずに数える。
    """
    data = json.loads(body)
    # 子を1つずつ取り出すイテレータを積むので、追加のメモリは深さに比例する（幅によらない）
    stack: list[Iterator[Any]] = [iter((data,))]
    while stack:
        value = next(stack[-1], _END)
        if value is _END:
            stack.pop()
            continue
        if isinstance(value, dict | list):
            if len(stack) > MAX_JSON_DEPTH:
                raise ValueError("JSON の入れ子が深すぎる")
            stack.append(iter(value.values() if isinstance(value, dict) else value))
    return data


def _is_search(path: str) -> bool:
    return path.lower().startswith("/search/")


def _redirect_target(url: str, headers: Mapping[str, str], search: bool) -> str | None:
    """リダイレクトの先。たどれなければ None。

    たどるのは、API_ROOT の中で、パスが API のパスの形のもの（空白などを含まない）だけ。
    Search とそれ以外をまたぐ先もたどらない（利用量と間隔を数え分けているため）。
    """
    location = {k.lower(): v for k, v in headers.items()}.get("location")
    if not location:
        return None
    target = urljoin(url, location.strip())
    if not target.startswith(API_ROOT + "/"):
        return None
    path, _, query = target.removeprefix(API_ROOT).partition("?")
    if not _PATH.fullmatch(path) or {".", ".."} & set(path.split("/")):
        return None
    if _UNSAFE_QUERY.search(query) or _is_search(path) != search:
        return None
    return target


def classify(error: FetchError) -> str:
    """http.fetch の失敗を error_category の語にする。"""
    status = error.status
    if status is None:
        return _network_category(error)
    headers = {k.lower(): v for k, v in error.headers.items()}
    if status == 401:
        return AUTH
    if status == 429 or (
        status == 403
        and (
            headers.get("x-ratelimit-remaining") == "0"
            or "retry-after" in headers
            or b"rate limit" in error.body.lower()
        )
    ):
        # 一次のレート制限は残り0、二次のレート制限は Retry-After か本文の文言で示される
        return RATE_LIMITED
    if status == 403:
        return FORBIDDEN
    if status == 404:
        return NOT_FOUND
    if status >= 500:
        return HTTP_5XX
    return HTTP_4XX


def _network_category(error: BaseException) -> str:
    """原因の例外をたどり、タイムアウト・名前解決・それ以外の通信の失敗に分ける。"""
    seen: BaseException | None = error
    while seen is not None:
        reason = getattr(seen, "reason", None)
        for cause in (seen, reason):
            if isinstance(cause, TimeoutError):
                return TIMEOUT
            if isinstance(cause, socket.gaierror):
                return DNS
        seen = seen.__cause__
    return NETWORK
