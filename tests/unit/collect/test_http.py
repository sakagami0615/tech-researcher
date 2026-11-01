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

    def geturl(self):
        return URL


def http_error(code, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(URL, code, "err", headers, None)


def scripted(*outcomes):
    """順に例外を投げるか本文を返す open_url。受け取った要求を記録する。"""
    requests = []

    def open_url(request):
        requests.append(request)
        outcome = outcomes[len(requests) - 1]
        if isinstance(outcome, Exception):
            raise outcome
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
