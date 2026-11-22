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
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from tech_researcher.config import Limits

# 取得元を示す独自の User-Agent（D-11）
USER_AGENT = "tech-researcher/0.1 (+https://github.com/sakagami0615/tech-researcher)"
# 応答の本文の上限（暫定）。大きすぎる応答や、少しずつ送り続ける応答で止まらないようにする。
# 足りるかは運用で確かめて決める
MAX_BODY_BYTES = 20_000_000
# 誤りの応答の本文を読む上限。理由の判定（GitHub の二次レート制限など）に使う先頭だけ読む
ERROR_BODY_BYTES = 4096
# 時間の上限があるとき、本文を読む塊の大きさ（塊の間で上限を確かめる）
CHUNK_BYTES = 64 * 1024


# 取得の失敗の理由の分類（error_category・source_fetches。classify が付ける）
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
INVALID_RESPONSE = "invalid_response"  # 応答が読めない・契約に合わない
EXIT_CODE = "exit_code"  # 子プロセス（last30days-skill）の終了コードの異常
GRAPHQL = "graphql_error"  # GraphQL の応答の errors（問い合わせ全体の誤り）


class FetchError(Exception):
    """再試行を使い切ったか、再試行しない失敗。attempts は初回を含めた要求の回数。

    headers・body は HTTP の誤りの応答のヘッダ（キーは小文字）と本文の先頭（レート制限の
    判定などに使う）。
    本文は再試行しない誤りのときだけ持つ。それ以外は空。
    category は、応答の大きさの超過・gzip を展開できないなど、応答が読めない失敗の分類
    （invalid_response）。
    """

    def __init__(
        self,
        message: str,
        attempts: int,
        status: int | None = None,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
        category: str | None = None,
    ):
        super().__init__(message)
        self.category = category  # 状態コードから決められない失敗の分類（classify が先に返す）
        self.attempts = attempts
        self.status = status
        self.headers = headers or {}
        self.body = body


@dataclass(frozen=True)
class Response:
    url: str  # リダイレクトの後の URL
    status: int
    body: bytes
    attempts: int
    headers: Mapping[str, str] = field(default_factory=dict)  # キーは小文字


class _OutOfTime(Exception):
    """時間の上限を過ぎた（本文の読み込み・リダイレクトの途中）。fetch が timeout にする。"""


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
    follow_redirects: bool = True,
    data: bytes | None = None,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    min_wait: float = 0.0,
) -> Response:
    """GET して本文を返す。待ちは設定の http_retry_waits_s で、Retry-After があれば従う。

    deadline は時間の上限（clock の値。structure.md 6.4）。1回の要求の接続・応答の上限時間を
    残りの時間まで縮め、残りに収まらない再試行（待ちを含む。min_wait は呼ぶ側が待つ最短の秒数）
    は始めずに、そのときの失敗を FetchError にする。過ぎていれば要求を出さずに timeout にする。
    本文は 64KB ずつ読み、塊の間と、リダイレクトをたどるたびに上限を確かめる（少しずつ送り続ける
    応答や、転送の連続でも上限を超えない）。

    data を渡すと、それを本文にして POST する（GitHub の GraphQL の問い合わせ用）。POST でも
    同じく再試行するので、読み取りだけの要求（何度送っても結果が変わらないもの）にだけ使う
    （Projects への登録のように、繰り返しても結果が変わらない書き込みも含む）。

    headers は足す要求ヘッダ（認証など）。リダイレクトの先には送らない（別のホストへ
    認証情報を渡さないため）。follow_redirects が False なら、リダイレクトをたどらずに
    3xx の FetchError にする（Location は headers に残る。要求の回数を呼ぶ側で数えるため）。
    """
    if data is not None and follow_redirects:
        # urllib は POST の 301〜303 を本文のない GET に変えてたどるため、組み合わせない
        raise ValueError("data を送るときは follow_redirects=False にする")
    default_open = open_url is None
    if default_open and deadline is None:
        opener = _opener(limits, follow_redirects)

        def open_url(req):
            return opener.open(req, timeout=limits.http_connect_timeout_s)

    request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    for name, value in (headers or {}).items():
        request.add_unredirected_header(name, value)
    waits = limits.http_retry_waits_s
    for attempt in range(1, len(waits) + 2):
        if deadline is not None:
            left = deadline - clock()
            if left <= 0:
                raise FetchError("時間の上限に達した", attempt - 1, category=TIMEOUT)
            if default_open:  # 1回の上限時間を残りの時間まで縮める
                opener = _opener(_shrunk(limits, left), follow_redirects, deadline, clock)
                connect = min(limits.http_connect_timeout_s, left)

                def open_url(req, opener=opener, connect=connect):
                    return opener.open(req, timeout=connect)

        try:
            return _get(open_url, request, attempt, deadline, clock)
        except _OutOfTime as e:
            raise FetchError("時間の上限に達した", attempt, category=TIMEOUT) from e
        except _Retryable as e:
            if attempt > len(waits):
                raise FetchError(f"再試行を使い切った：{e}", attempt, e.status, e.headers) from e
            wait = waits[attempt - 1]
            if e.retry_after is not None:
                wait = min(e.retry_after, limits.retry_after_max_s)
            if deadline is not None and clock() + max(wait, min_wait) >= deadline:
                # 待った後に要求する時間が残らない。再試行せずに、この失敗で終える
                raise FetchError(
                    f"時間の上限のため再試行しない：{e}", attempt, e.status, e.headers
                ) from e
            sleep(wait)
    raise AssertionError("ここには来ない")


def time_limit(deadline: float | None, clock: Callable[[], float]) -> dict:
    """fetch に渡す時間の上限の引数（上限がなければ空）。"""
    return {} if deadline is None else {"deadline": deadline, "clock": clock}


def _shrunk(limits: Limits, left: float) -> Limits:
    """接続・応答の上限時間を、残りの時間（秒）に収める（小数の秒にする）。"""
    return replace(
        limits,
        http_connect_timeout_s=min(limits.http_connect_timeout_s, left),
        http_read_timeout_s=min(limits.http_read_timeout_s, left),
    )


def _read_body(res, attempt: int, deadline: float | None, clock: Callable[[], float]) -> bytes:
    """本文を読む。上限（MAX_BODY_BYTES）を超えたら INVALID_RESPONSE。deadline があれば、
    64KB ずつ読んで、塊の間で上限を確かめる（過ぎていれば _OutOfTime）。"""
    if deadline is None:
        body = res.read(MAX_BODY_BYTES + 1)
    else:
        chunks = []
        size = 0
        while True:
            # read1 は、そのとき届いている分だけを返す（read は指定の量がそろうまで戻らず、少しずつ
            # 送り続ける応答では上限を確かめる所に来ない）
            chunk = res.read1(min(CHUNK_BYTES, MAX_BODY_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                break
            if getattr(res, "isclosed", lambda: False)():
                break  # 全部受け取った。上限を過ぎていても捨てない
            if clock() >= deadline:  # 次の塊を読む前に確かめる
                raise _OutOfTime
        body = b"".join(chunks)
    if len(body) > MAX_BODY_BYTES:
        raise FetchError(
            f"応答が {MAX_BODY_BYTES} バイトを超えた", attempt, category=INVALID_RESPONSE
        )
    return body


def _get(
    open_url,
    request,
    attempt: int,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Response:
    try:
        with open_url(request) as res:
            body = _read_body(res, attempt, deadline, clock)
            encoding = (res.headers.get("Content-Encoding") or "").strip().lower()
            if encoding in ("gzip", "x-gzip"):
                body = _gunzip(body, attempt)
            return Response(res.geturl(), res.status, body, attempt, _lower_keys(res.headers))
    except urllib.error.HTTPError as e:
        headers = _lower_keys(e.headers)
        if e.code == 429 or e.code >= 500:
            e.close()
            raise _Retryable(f"HTTP {e.code}", e.code, _retry_after(e.headers), headers) from e
        body = _error_body(e)
        raise FetchError(f"HTTP {e.code}", attempt, e.code, headers, body) from e
    except urllib.error.URLError as e:
        if _transient(e.reason):
            raise _Retryable(f"{type(e.reason).__name__}: {e.reason}") from e
        raise FetchError(f"URLError: {e.reason}", attempt) from e
    except (OSError, http.client.HTTPException) as e:
        if _transient(e) or isinstance(e, http.client.HTTPException):
            raise _Retryable(f"{type(e).__name__}: {e}") from e
        raise FetchError(f"{type(e).__name__}: {e}", attempt) from e


def _lower_keys(headers) -> dict[str, str]:
    return {k.lower(): v for k, v in (headers or {}).items()}


def _error_body(error: urllib.error.HTTPError) -> bytes:
    """誤りの応答の本文の先頭。読めなければ空。"""
    try:
        return error.read(ERROR_BODY_BYTES) if error.fp is not None else b""
    except OSError, http.client.HTTPException:
        return b""
    finally:
        error.close()


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
            raise FetchError(f"gzip を展開できない：{e}", attempt, category=INVALID_RESPONSE) from e
        if len(out) > MAX_BODY_BYTES:
            raise FetchError(
                f"展開した応答が {MAX_BODY_BYTES} バイトを超えた",
                attempt,
                category=INVALID_RESPONSE,
            )
        if not d.eof:
            raise FetchError(
                "gzip を展開できない：途中で終わっている", attempt, category=INVALID_RESPONSE
            )
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


def _opener(
    limits: Limits,
    follow_redirects: bool = True,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> urllib.request.OpenerDirector:
    """http・https だけを扱う opener。follow_redirects が False ならリダイレクトをたどらない。

    接続のタイムアウトは open に渡し、応答のタイムアウトは接続の後にソケットへ設定する。
    """
    read = limits.http_read_timeout_s
    opener = urllib.request.OpenerDirector()
    handlers = [
        _HTTPHandler(read),
        _HTTPSHandler(read),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
    ]
    if follow_redirects:
        handlers.append(_RedirectHandler(deadline, clock))
    for handler in handlers:
        opener.add_handler(handler)
    return opener


class _RedirectHandler(urllib.request.HTTPRedirectHandler):
    """リダイレクトをたどる前に、時間の上限を確かめる（転送のたびに上限時間がかかり直さない）。"""

    def __init__(self, deadline: float | None, clock: Callable[[], float]):
        self._deadline = deadline
        self._clock = clock

    def redirect_request(self, *args, **kwargs):
        if self._deadline is not None and self._clock() >= self._deadline:
            if args[1] is not None:
                args[1].close()  # 前の応答（fp）を閉じてから止める
            raise _OutOfTime
        return super().redirect_request(*args, **kwargs)


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


def classify(error: FetchError) -> str:
    """http.fetch の失敗を error_category の語にする。"""
    if error.category is not None:
        return error.category
    status = error.status
    if status is None:
        return _network_category(error)
    if 300 <= status < 400:  # たどらないリダイレクト
        return REDIRECT
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
