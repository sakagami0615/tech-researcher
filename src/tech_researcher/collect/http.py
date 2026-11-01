"""外部への HTTP 要求（structure.md 6.3、D-21）。標準ライブラリだけで、タイムアウトと再試行を包む。

再試行するのは、タイムアウト・名前解決や接続の失敗・HTTP 429・5xx。ほかの 4xx は再試行しない。
"""

import http.client
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from tech_researcher.config import Limits

# 取得元を示す独自の User-Agent（D-11）
USER_AGENT = "tech-researcher/0.1 (+https://github.com/sakagami0615/tech-researcher)"
# 応答の本文の上限（暫定）。大きすぎる応答や、少しずつ送り続ける応答で止まらないようにする。
# 足りるかは運用で確かめて決める
MAX_BODY_BYTES = 20_000_000


class FetchError(Exception):
    """再試行を使い切ったか、再試行しない失敗。attempts は初回を含めた要求の回数。"""

    def __init__(self, message: str, attempts: int, status: int | None = None):
        super().__init__(message)
        self.attempts = attempts
        self.status = status


@dataclass(frozen=True)
class Response:
    url: str  # リダイレクトの後の URL
    status: int
    body: bytes
    attempts: int


class _Retryable(Exception):
    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def fetch(
    url: str,
    limits: Limits,
    *,
    open_url: Callable[[urllib.request.Request], http.client.HTTPResponse] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Response:
    """GET して本文を返す。待ちは設定の http_retry_waits_s で、Retry-After があれば従う。"""
    if open_url is None:
        opener = _opener(limits)

        def open_url(req):
            return opener.open(req, timeout=limits.http_connect_timeout_s)

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    waits = limits.http_retry_waits_s
    for attempt in range(1, len(waits) + 2):
        try:
            return _get(open_url, request, attempt)
        except _Retryable as e:
            if attempt > len(waits):
                raise FetchError(f"再試行を使い切った：{e}", attempt, e.status) from e
            wait = waits[attempt - 1]
            if e.retry_after is not None:
                wait = min(e.retry_after, limits.retry_after_max_s)
            sleep(wait)
    raise AssertionError("ここには来ない")


def _get(open_url, request, attempt: int) -> Response:
    try:
        with open_url(request) as res:
            body = res.read(MAX_BODY_BYTES + 1)
            if len(body) > MAX_BODY_BYTES:
                raise FetchError(f"応答が {MAX_BODY_BYTES} バイトを超えた", attempt)
            return Response(res.geturl(), res.status, body, attempt)
    except urllib.error.HTTPError as e:
        e.close()
        if e.code == 429 or e.code >= 500:
            raise _Retryable(f"HTTP {e.code}", e.code, _retry_after(e.headers)) from e
        raise FetchError(f"HTTP {e.code}", attempt, e.code) from e
    except urllib.error.URLError as e:
        if _transient(e.reason):
            raise _Retryable(f"{type(e.reason).__name__}: {e.reason}") from e
        raise FetchError(f"URLError: {e.reason}", attempt) from e
    except (OSError, http.client.HTTPException) as e:
        if _transient(e) or isinstance(e, http.client.HTTPException):
            raise _Retryable(f"{type(e).__name__}: {e}") from e
        raise FetchError(f"{type(e).__name__}: {e}", attempt) from e


def _transient(reason) -> bool:
    """タイムアウト・名前解決・接続の失敗か。証明書の誤りなどは再試行しても直らない。"""
    if isinstance(reason, ssl.SSLCertVerificationError):
        return False
    return isinstance(reason, (TimeoutError, socket.gaierror, ConnectionError, ssl.SSLError))


def _retry_after(headers) -> float | None:
    """Retry-After の秒数（整数か HTTP の日付）。読めなければ None。"""
    value = (headers or {}).get("Retry-After")
    if value is None:
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9]+", value):
        return float(value)
    try:
        moment = parsedate_to_datetime(value)
    except TypeError, ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return max(0.0, (moment - datetime.now(UTC)).total_seconds())


def _opener(limits: Limits) -> urllib.request.OpenerDirector:
    """http・https だけを扱う opener。

    接続のタイムアウトは open に渡し、応答のタイムアウトは接続の後にソケットへ設定する。
    """
    read = limits.http_read_timeout_s
    opener = urllib.request.OpenerDirector()
    for handler in (
        _HTTPHandler(read),
        _HTTPSHandler(read),
        urllib.request.HTTPRedirectHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
    ):
        opener.add_handler(handler)
    return opener


def _with_read_timeout(base: type[http.client.HTTPConnection], read: float):
    class Connection(base):
        def connect(self):
            super().connect()  # self.timeout（接続のタイムアウト）で接続する
            self.sock.settimeout(read)

    return Connection


class _HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, read: float):
        super().__init__()
        self._conn = _with_read_timeout(http.client.HTTPConnection, read)

    def http_open(self, req):
        return self.do_open(self._conn, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, read: float):
        super().__init__()
        self._conn = _with_read_timeout(http.client.HTTPSConnection, read)

    def https_open(self, req):
        return self.do_open(self._conn, req, context=self._context)
