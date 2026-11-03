"""外部への HTTP 要求（structure.md 6.3、D-21）。標準ライブラリだけで、タイムアウトと再試行を包む。

再試行するのは、タイムアウト・名前解決や接続の失敗・HTTP 429・5xx。ほかの 4xx は再試行しない。

Accept-Encoding は送らない（圧縮を求めない）。それでも Content-Encoding: gzip で返すサーバーが
あるため（Google DeepMind の CDN）、gzip・x-gzip の本文は展開する。判定はヘッダだけで行い、
本文の先頭バイトは見ない（観察した gzip の応答は必ずヘッダを付けていた。.gz のファイルそのものを
取得したときに、勝手に展開しないようにする）。
"""

import http.client
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from tech_researcher.config import Limits

# 取得元を示す独自の User-Agent（D-11）
USER_AGENT = "tech-researcher/0.1 (+https://github.com/sakagami0615/tech-researcher)"
# 応答の本文の上限（暫定）。大きすぎる応答や、少しずつ送り続ける応答で止まらないようにする。
# 足りるかは運用で確かめて決める
MAX_BODY_BYTES = 20_000_000


class FetchError(Exception):
    """再試行を使い切ったか、再試行しない失敗。attempts は初回を含めた要求の回数。

    headers は HTTP の誤りの応答のヘッダ（レート制限の判定などに使う）。それ以外は空。
    """

    def __init__(
        self,
        message: str,
        attempts: int,
        status: int | None = None,
        headers: Mapping[str, str] | None = None,
    ):
        super().__init__(message)
        self.attempts = attempts
        self.status = status
        self.headers = headers or {}


@dataclass(frozen=True)
class Response:
    url: str  # リダイレクトの後の URL
    status: int
    body: bytes
    attempts: int
    headers: Mapping[str, str] = field(default_factory=dict)


class _Retryable(Exception):
    def __init__(
        self,
        message: str,
        status: int | None = None,
        retry_after: float | None = None,
        headers: Mapping[str, str] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.headers = headers


def fetch(
    url: str,
    limits: Limits,
    *,
    open_url: Callable[[urllib.request.Request], http.client.HTTPResponse] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """GET して本文を返す。待ちは設定の http_retry_waits_s で、Retry-After があれば従う。

    headers は足す要求ヘッダ（認証など）。リダイレクトの先には送らない（別のホストへ
    認証情報を渡さないため）。
    """
    if open_url is None:
        opener = _opener(limits)

        def open_url(req):
            return opener.open(req, timeout=limits.http_connect_timeout_s)

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for name, value in (headers or {}).items():
        request.add_unredirected_header(name, value)
    waits = limits.http_retry_waits_s
    for attempt in range(1, len(waits) + 2):
        try:
            return _get(open_url, request, attempt)
        except _Retryable as e:
            if attempt > len(waits):
                raise FetchError(f"再試行を使い切った：{e}", attempt, e.status, e.headers) from e
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
            encoding = (res.headers.get("Content-Encoding") or "").strip().lower()
            if encoding in ("gzip", "x-gzip"):
                body = _gunzip(body, attempt)
            return Response(res.geturl(), res.status, body, attempt, dict(res.headers.items()))
    except urllib.error.HTTPError as e:
        e.close()
        headers = dict((e.headers or {}).items())
        if e.code == 429 or e.code >= 500:
            raise _Retryable(f"HTTP {e.code}", e.code, _retry_after(e.headers), headers) from e
        raise FetchError(f"HTTP {e.code}", attempt, e.code, headers) from e
    except urllib.error.URLError as e:
        if _transient(e.reason):
            raise _Retryable(f"{type(e.reason).__name__}: {e.reason}") from e
        raise FetchError(f"URLError: {e.reason}", attempt) from e
    except (OSError, http.client.HTTPException) as e:
        if _transient(e) or isinstance(e, http.client.HTTPException):
            raise _Retryable(f"{type(e).__name__}: {e}") from e
        raise FetchError(f"{type(e).__name__}: {e}", attempt) from e


def _gunzip(data: bytes, attempt: int) -> bytes:
    """gzip を展開する。展開後も MAX_BODY_BYTES を超えた時点で止める（圧縮爆弾対策）。

    壊れた gzip はサーバーの返す中身の誤りで、再試行しても直らないため再試行しない。
    """
    out = bytearray()
    while data:  # 複数のメンバーを連結した gzip もある
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            # max_length は 1 以上（0 だと上限なし）。上限を 1 バイト超えるまでで止める
            out += d.decompress(data, MAX_BODY_BYTES + 1 - len(out))
        except zlib.error as e:
            raise FetchError(f"gzip を展開できない：{e}", attempt) from e
        if len(out) > MAX_BODY_BYTES:
            raise FetchError(f"展開した応答が {MAX_BODY_BYTES} バイトを超えた", attempt)
        if not d.eof:
            raise FetchError("gzip を展開できない：途中で終わっている", attempt)
        data = d.unused_data.lstrip(b"\0")  # 末尾のゼロ埋めは無視する（gzip モジュールと同じ）
    return bytes(out)


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
