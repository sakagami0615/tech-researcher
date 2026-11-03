import gzip
import http.client
import io
import socket
import ssl
import urllib.error
from dataclasses import replace
from email.message import Message

import pytest

from tech_researcher.collect import http as tr_http
from tech_researcher.collect.http import USER_AGENT, FetchError, fetch

URL = "https://feed.example/rss"


class FakeResponse(io.BytesIO):
    status = 200

    def __init__(self, body, content_encoding=None):
        super().__init__(body)
        self.headers = Message()
        if content_encoding is not None:
            self.headers["Content-Encoding"] = content_encoding

    def geturl(self):
        return URL


def http_error(code, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(URL, code, "err", headers, None)


def scripted(*outcomes):
    """順に例外を投げるか本文（bytes・FakeResponse）を返す open_url。要求を記録する。"""
    requests = []

    def open_url(request):
        requests.append(request)
        outcome = outcomes[len(requests) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, FakeResponse):
            return outcome
        return FakeResponse(outcome)

    return open_url, requests


def run(limits, *outcomes):
    open_url, requests = scripted(*outcomes)
    waits = []
    try:
        return fetch(URL, limits, open_url=open_url, sleep=waits.append), waits, requests
    except FetchError as e:
        return e, waits, requests


def test_success_sends_user_agent(limits):
    response, waits, requests = run(limits, b"body")
    assert (response.body, response.attempts, waits) == (b"body", 1, [])
    assert requests[0].get_header("User-agent") == USER_AGENT


@pytest.mark.parametrize(
    "error",
    [
        http_error(500),
        http_error(503),
        http_error(429),
        TimeoutError("timed out"),
        urllib.error.URLError(socket.gaierror("name")),
        ConnectionResetError(),
        http.client.RemoteDisconnected(),
        ssl.SSLEOFError(),  # 読んでいる途中で TLS が切れた
    ],
)
def test_transient_failures_are_retried_with_waits(limits, error):
    response, waits, _ = run(limits, error, error, b"ok")
    assert response.body == b"ok" and response.attempts == 3
    assert waits == [5, 20]


def test_gives_up_after_two_retries(limits):
    error, waits, requests = run(limits, http_error(502), http_error(502), http_error(502))
    assert isinstance(error, FetchError)
    assert (error.attempts, error.status, len(requests), waits) == (3, 502, 3, [5, 20])


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError(ssl.SSLCertVerificationError("bad cert")),
        urllib.error.URLError("unknown url type: ftp"),
        PermissionError(),
    ],
)
def test_permanent_failures_are_not_retried(limits, error):
    result, waits, requests = run(limits, error, b"never")
    assert isinstance(result, FetchError) and (len(requests), waits) == (1, [])


def test_too_large_body_is_refused(limits, monkeypatch):
    monkeypatch.setattr(tr_http, "MAX_BODY_BYTES", 3)
    result, _, _ = run(limits, b"1234")
    assert isinstance(result, FetchError) and "超えた" in str(result)


@pytest.mark.parametrize("encoding", ["gzip", "x-gzip", " GZIP "])
def test_gzip_body_is_decompressed(limits, encoding):
    response, _, _ = run(limits, FakeResponse(gzip.compress(b"<rss/>"), encoding))
    assert response.body == b"<rss/>"


def test_concatenated_gzip_members_are_all_decompressed(limits):
    body = gzip.compress(b"<rss>") + gzip.compress(b"</rss>")
    response, _, _ = run(limits, FakeResponse(body, "gzip"))
    assert response.body == b"<rss></rss>"


def test_trailing_zero_padding_is_ignored(limits):
    response, _, _ = run(limits, FakeResponse(gzip.compress(b"<rss/>") + b"\0\0\0", "gzip"))
    assert response.body == b"<rss/>"


def test_empty_gzip_body_is_empty(limits):
    response, _, _ = run(limits, FakeResponse(b"", "gzip"))
    assert response.body == b""


def test_uncompressed_body_is_kept_even_if_it_looks_like_gzip(limits):
    # ヘッダがなければ、gzip の先頭バイトで始まっていても展開しない
    data = gzip.compress(b"<rss/>")
    response, _, _ = run(limits, FakeResponse(data))
    assert response.body == data


def test_gzip_body_at_limit_is_accepted(limits, monkeypatch):
    monkeypatch.setattr(tr_http, "MAX_BODY_BYTES", 100)
    response, _, _ = run(limits, FakeResponse(gzip.compress(b"a" * 100), "gzip"))
    assert response.body == b"a" * 100


def test_gzip_bomb_is_refused_without_full_decompression(limits, monkeypatch):
    monkeypatch.setattr(tr_http, "MAX_BODY_BYTES", 2000)
    bomb = gzip.compress(b"\0" * 1_000_000)  # 約 1KB が 1MB に広がる
    assert len(bomb) < 2000
    calls = []
    real = tr_http.zlib.decompressobj

    class Spy:
        def __init__(self, *args):
            self._d = real(*args)

        def decompress(self, data, max_length):
            out = self._d.decompress(data, max_length)
            calls.append(len(out))
            return out

    monkeypatch.setattr(tr_http.zlib, "decompressobj", Spy)
    result, waits, requests = run(limits, FakeResponse(bomb, "gzip"), b"never")
    assert isinstance(result, FetchError) and "展開した応答" in str(result)
    assert (len(requests), waits) == (1, [])  # 再試行しない
    assert calls == [2001]  # 上限を 1 バイト超えたところで展開を止めた


def test_gzip_members_over_limit_in_total_are_refused(limits, monkeypatch):
    monkeypatch.setattr(tr_http, "MAX_BODY_BYTES", 100)
    body = gzip.compress(b"a" * 60) + gzip.compress(b"b" * 60)
    result, _, _ = run(limits, FakeResponse(body, "gzip"))
    assert isinstance(result, FetchError) and "展開した応答" in str(result)


@pytest.mark.parametrize(
    "body",
    [
        b"not gzip at all",
        gzip.compress(b"<rss/>" * 100)[:-20],  # 途中で切れている
        b"\x1f\x8b\x08\x00broken",
    ],
)
def test_broken_gzip_is_an_error_without_retry(limits, body):
    result, waits, requests = run(limits, FakeResponse(body, "gzip"), b"never")
    assert isinstance(result, FetchError) and "gzip" in str(result)
    assert (len(requests), waits) == (1, [])


@pytest.mark.parametrize("code", [400, 401, 403, 404, 410])
def test_client_errors_are_not_retried(limits, code):
    error, waits, requests = run(limits, http_error(code), b"never")
    assert isinstance(error, FetchError) and error.status == code
    assert (error.attempts, len(requests), waits) == (1, 1, [])


@pytest.mark.parametrize(
    "header, wait",
    [
        ("7", 7),
        ("600", 60),  # 60 秒まで
        ("Thu, 01 Jan 1970 00:00:00 GMT", 0),  # 過去の日付
        ("soon", 5),  # 読めなければ既定の待ち
        ("²", 5),
    ],
)
def test_retry_after_is_followed_up_to_limit(limits, header, wait):
    _, waits, _ = run(limits, http_error(429, header), b"ok")
    assert waits == [wait]


def test_real_opener_against_local_server(limits, server):
    response = fetch(server.base + "/ok", limits)
    assert response.body == USER_AGENT.encode()


def test_extra_headers_are_sent_but_not_to_redirect_target(limits, server):
    response = fetch(server.base + "/to-ok", limits, headers={"Authorization": "Bearer secret"})
    assert server.paths == ["/to-ok", "/ok"]
    first, second = server.headers
    assert first["Authorization"] == "Bearer secret"
    assert "Authorization" not in second
    assert second["User-Agent"] == USER_AGENT
    assert response.status == 200


def test_error_body_head_is_kept_for_client_errors(limits):
    body = b"x" * (tr_http.ERROR_BODY_BYTES + 10)
    error = urllib.error.HTTPError(URL, 403, "err", Message(), io.BytesIO(body))
    result, _, _ = run(limits, error)
    assert result.body == body[: tr_http.ERROR_BODY_BYTES]
    result, _, _ = run(limits, http_error(404))  # 本文のない誤り
    assert result.body == b""


def test_response_and_error_headers_are_kept(limits):
    ok = FakeResponse(b"{}")
    ok.headers["X-RateLimit-Remaining"] = "42"
    response, _, _ = run(limits, ok)
    assert response.headers["X-RateLimit-Remaining"] == "42"
    error = http_error(403)
    error.headers["X-RateLimit-Remaining"] = "0"
    result, _, _ = run(limits, error)
    assert result.headers["X-RateLimit-Remaining"] == "0"
    result, _, _ = run(limits, http_error(429, "1"), http_error(429, "1"), http_error(429, "1"))
    assert result.status == 429 and result.headers["Retry-After"] == "1"


def test_read_timeout_is_applied(limits, server):
    fast = replace(limits, http_read_timeout_s=0.2, http_retry_waits_s=())
    with pytest.raises(FetchError, match="timed out"):
        fetch(server.base + "/slow", fast, sleep=lambda s: None)


def test_redirect_to_file_scheme_is_refused(limits, server):
    with pytest.raises(FetchError):
        fetch(server.base + "/redirect", limits, sleep=lambda s: None)


def test_connect_and_read_timeouts_are_separate(monkeypatch):
    seen = {}

    class FakeSocket:
        def setsockopt(self, *args):
            pass

        def settimeout(self, value):
            seen["read"] = value

    def create_connection(address, timeout, source_address=None, **kw):
        seen["connect"] = timeout
        return FakeSocket()

    monkeypatch.setattr(http.client.socket, "create_connection", create_connection)
    conn_class = tr_http._with_read_timeout(http.client.HTTPConnection, 30)
    conn_class("feed.example", timeout=10).connect()
    assert seen == {"connect": 10, "read": 30}
