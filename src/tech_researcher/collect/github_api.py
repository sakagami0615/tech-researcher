"""GitHub の REST API の共通の取得処理（D-05・D-21、structure.md 6.3・6.5）。

GitHub の情報源（リポジトリの候補集め、Releases・README など）から共通に使う。

- トークン：収集用トークンを worker の環境変数 `GITHUB_TOKEN` から読み（D-05）、
  `Authorization: Bearer` で付ける。値はログ・例外のメッセージに出さない。要求の先は
  `https://api.github.com` に固定し、リダイレクトの先には送らない（http.fetch の headers）
- 利用量：1回の実行の中で、REST は limits.github_rest_calls 回、Search は
  limits.github_search_calls 回まで（再試行も数える）。Search は1分 limits.github_search_per_minute
  回の間隔をあけて出す。上限に達したら要求を出さずに失敗にする（structure.md 6.5）
- 再試行：http.fetch に任せる（タイムアウト・接続の失敗・429・5xx。ほかの 4xx はしない）
- エラーの分類：source_fetches の error_category（data-model.md 3.4）に使う短い語にする

1回の実行で1つの GitHubClient を作り、GitHub の情報源のあいだで使い回す（利用量を合算するため）。
"""

import json
import os
import re
import socket
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any
from urllib.parse import urlencode

from tech_researcher.collect.http import FetchError, Response, fetch
from tech_researcher.config import Limits

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
TOKEN_ENV = "GITHUB_TOKEN"  # 収集用トークン（D-05）
# トークンに使える形。ヘッダに入れたときに要求を壊す文字（改行など）を止める。
# 不正な値を http.client に渡すと、例外のメッセージに値がそのまま入るため、先に確かめる
_TOKEN = re.compile(r"[A-Za-z0-9_.\-]+")
# API のパス。`/` で区切った空でない部分だけにし、ホストを差し替えられないようにする
# （`.`・`..` の部分は別に止める）
_PATH = re.compile(r"(?:/[A-Za-z0-9_.\-]+)+")
# 認証なしの Search の上限（1分の回数。structure.md 6.2）
UNAUTHENTICATED_SEARCH_PER_MINUTE = 10

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
INVALID_RESPONSE = "invalid_response"


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
    if not _TOKEN.fullmatch(value):
        raise GitHubError(f"{TOKEN_ENV} に使えない文字が含まれる", AUTH, 0)
    return value


class GitHubClient:
    """GitHub の REST API の GET。1回の実行の中で使った回数を数える。

    token が None なら認証なしで要求する（Search は1分10回までなど、上限が小さくなる）。
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
        if token is not None and not _TOKEN.fullmatch(token):
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
        self._last_search: float | None = None

    @property
    def authenticated(self) -> bool:
        return "Authorization" in self._headers

    def __repr__(self) -> str:  # トークンを repr に出さない
        return f"GitHubClient(authenticated={self.authenticated})"

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """REST の GET（Search 以外）。応答の JSON を返す。"""
        if path.startswith("/search/"):
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
        """Search API（`/search/{kind}`）。前の Search から間隔をあけて出す。

        認証なしでは、GitHub の上限（1分10回）に合わせて間隔を広げる。
        """
        per_minute = self._limits.github_search_per_minute
        if not self.authenticated:
            per_minute = min(per_minute, UNAUTHENTICATED_SEARCH_PER_MINUTE)
        if self._last_search is not None:
            wait = self._last_search + 60 / per_minute - self._clock()
            if wait > 0:
                self._sleep(wait)
        remaining = self._limits.github_search_calls - self.search_calls
        try:
            data, used = self._request(f"/search/{kind}", params, remaining)
        except GitHubError as e:
            self.search_calls += e.attempts
            if e.attempts:
                self._last_search = self._clock()
            raise
        self.search_calls += used
        self._last_search = self._clock()
        return data

    def _request(
        self, path: str, params: Mapping[str, Any] | None, remaining: int
    ) -> tuple[Any, int]:
        """要求して（JSON, 要求の回数）を返す。再試行は残りの回数に収まる分だけにする。"""
        if not _PATH.fullmatch(path) or {".", ".."} & set(path.split("/")):
            raise ValueError(f"API のパスにできない：{path!r}")
        if remaining < 1:
            raise GitHubError(f"{path}：この実行の要求の上限に達した", BUDGET, 0)
        waits = self._limits.http_retry_waits_s[: remaining - 1]
        url = API_ROOT + path + (f"?{urlencode(params)}" if params else "")
        try:
            response = self._fetch(
                url, replace(self._limits, http_retry_waits_s=waits), headers=self._headers
            )
        except FetchError as e:
            category = classify(e)
            raise GitHubError(f"{path}：{category}（{e}）", category, e.attempts, e.status) from e
        try:
            return json.loads(response.body), response.attempts
        except ValueError as e:
            raise GitHubError(
                f"{path}：応答が JSON でない", INVALID_RESPONSE, response.attempts
            ) from e


def classify(error: FetchError) -> str:
    """http.fetch の失敗を error_category の語にする。"""
    status = error.status
    if status is None:
        return _network_category(error)
    headers = {k.lower(): v for k, v in error.headers.items()}
    if status == 401:
        return AUTH
    if status == 429 or (
        status == 403 and (headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers)
    ):
        # 一次のレート制限は残り0、二次のレート制限は Retry-After で示される
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
