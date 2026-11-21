import gzip
import http.client
import io
import socket
import ssl
import urllib.error
import urllib.request
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


def test_redirect_is_not_followed_when_asked(limits, server):
    with pytest.raises(FetchError) as e:
        fetch(server.base + "/to-ok", limits, follow_redirects=False)
    assert e.value.status == 302 and e.value.headers["location"] == "/ok"
    assert server.paths == ["/to-ok"]


def test_response_and_error_headers_are_kept(limits):
    ok = FakeResponse(b"{}")
    ok.headers["X-RateLimit-Remaining"] = "42"
    response, _, _ = run(limits, ok)
    assert response.headers["x-ratelimit-remaining"] == "42"  # キーは小文字にそろえる
    error = http_error(403)
    error.headers["X-RateLimit-Remaining"] = "0"
    result, _, _ = run(limits, error)
    assert result.headers["x-ratelimit-remaining"] == "0"
    result, _, _ = run(limits, http_error(429, "1"), http_error(429, "1"), http_error(429, "1"))
    assert result.status == 429 and result.headers["retry-after"] == "1"


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


def test_data_is_posted_and_kept_on_retry(limits):
    open_url, requests = scripted(http_error(502), b"ok")
    response = fetch(
        URL,
        limits,
        open_url=open_url,
        sleep=lambda _: None,
        headers={"Content-Type": "application/json"},
        data=b'{"query":"{}"}',
        follow_redirects=False,
    )
    assert response.body == b"ok"
    assert [r.get_method() for r in requests] == ["POST", "POST"]
    assert all(r.data == b'{"query":"{}"}' for r in requests)
    assert requests[0].get_header("Content-type") == "application/json"


def test_without_data_the_request_is_get(limits):
    _, _, requests = run(limits, b"ok")
    assert requests[0].get_method() == "GET"
    assert requests[0].data is None


def test_data_with_redirects_followed_is_refused(limits):
    open_url, requests = scripted(b"ok")
    with pytest.raises(ValueError):
        fetch(URL, limits, open_url=open_url, data=b"{}")
    assert requests == []


# ① の時間の上限（deadline。structure.md 6.4）
def test_expired_deadline_sends_no_request(limits):
    open_url, requests = scripted(b"x")
    with pytest.raises(FetchError) as e:
        fetch(URL, limits, open_url=open_url, deadline=10.0, clock=lambda: 10.0)
    assert requests == [] and e.value.attempts == 0 and e.value.category == "timeout"


def test_retry_that_does_not_fit_in_the_remaining_time_is_not_started(limits):
    now = [0.0]
    open_url, requests = scripted(http_error(503), b"ok")
    waits = []
    with pytest.raises(FetchError) as e:
        # 待ち（5 秒）の後に要求する時間が残らない（残り 4 秒）
        fetch(
            URL,
            limits,
            open_url=open_url,
            sleep=waits.append,
            deadline=4.0,
            clock=lambda: now[0],
        )
    assert waits == [] and len(requests) == 1
    assert e.value.attempts == 1 and e.value.status == 503


def test_retry_after_and_min_wait_count_toward_the_deadline(limits):
    open_url, requests = scripted(http_error(429, "50"), b"ok")
    with pytest.raises(FetchError):
        fetch(
            URL, limits, open_url=open_url, sleep=lambda s: None, deadline=40.0, clock=lambda: 0.0
        )
    assert len(requests) == 1  # Retry-After の 50 秒が残り 40 秒に収まらない
    open_url, requests = scripted(http_error(503), b"ok")
    with pytest.raises(FetchError):
        fetch(
            URL,
            limits,
            open_url=open_url,
            sleep=lambda s: None,
            deadline=30.0,
            clock=lambda: 0.0,
            min_wait=40.0,  # 呼ぶ側が待つ最短の秒数（Search の間隔）
        )
    assert len(requests) == 1


def test_retry_that_fits_is_made(limits):
    open_url, requests = scripted(http_error(503), b"ok")
    waits = []
    response = fetch(
        URL, limits, open_url=open_url, sleep=waits.append, deadline=100.0, clock=lambda: 0.0
    )
    assert response.body == b"ok" and waits == [5] and len(requests) == 2


def test_timeouts_of_one_request_are_cut_to_the_remaining_time(limits):
    shrunk = tr_http._shrunk(limits, 2.5)
    assert (shrunk.http_connect_timeout_s, shrunk.http_read_timeout_s) == (2.5, 2.5)
    same = tr_http._shrunk(limits, 100.0)
    assert (same.http_connect_timeout_s, same.http_read_timeout_s) == (10, 30)


def test_real_request_to_a_silent_server_is_cut_at_the_deadline(limits):
    """応答しない相手に対し、応答の上限（30 秒）ではなく残りの時間で打ち切る。"""
    import time

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        started = time.monotonic()
        with pytest.raises(FetchError):
            fetch(
                f"http://127.0.0.1:{server.getsockname()[1]}/",
                limits,
                deadline=started + 0.3,
            )
        assert time.monotonic() - started < 5
    finally:
        server.close()


class Trickle(FakeResponse):
    """1回の読み込みごとに時計を進める応答（少しずつ送り続ける相手）。"""

    def __init__(self, body, clock, step):
        super().__init__(body)
        self.clock, self.step, self.reads = clock, step, 0

    def read1(self, size=-1):
        self.reads += 1
        self.clock[0] += self.step
        return super().read1(size)


def test_body_read_in_chunks_stops_at_the_deadline(limits):
    now = [0.0]
    body = Trickle(b"x" * (3 * tr_http.CHUNK_BYTES), now, step=10.0)
    open_url, _ = scripted(body)
    with pytest.raises(FetchError) as e:
        fetch(URL, limits, open_url=open_url, deadline=15.0, clock=lambda: now[0])
    assert e.value.category == "timeout"
    assert body.reads == 2  # 2塊目の後（20 秒）で上限（15 秒）を過ぎた。3塊目は読まない


def test_chunked_read_returns_the_whole_body_within_the_deadline(limits):
    now = [0.0]
    data = b"y" * (2 * tr_http.CHUNK_BYTES + 5)
    open_url, _ = scripted(Trickle(data, now, step=1.0))
    response = fetch(URL, limits, open_url=open_url, deadline=100.0, clock=lambda: now[0])
    assert response.body == data


def test_body_size_limit_still_applies_with_a_deadline(limits, monkeypatch):
    monkeypatch.setattr(tr_http, "MAX_BODY_BYTES", 100)
    open_url, _ = scripted(b"z" * 101)
    with pytest.raises(FetchError) as e:
        fetch(URL, limits, open_url=open_url, deadline=100.0, clock=lambda: 0.0)
    assert e.value.category == "invalid_response"


def test_redirect_is_not_followed_after_the_deadline():
    now = [0.0]
    handler = tr_http._RedirectHandler(10.0, lambda: now[0])
    request = urllib.request.Request(URL)
    headers = Message()
    headers["Location"] = "https://feed.example/next"
    followed = handler.redirect_request(
        request, None, 301, "moved", headers, "https://feed.example/next"
    )
    assert followed is not None  # 上限の内ならたどる
    now[0] = 10.0
    with pytest.raises(tr_http._OutOfTime):
        handler.redirect_request(request, None, 301, "moved", headers, "https://feed.example/next")


class Closing:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_redirect_stopped_by_the_deadline_closes_the_previous_response():
    handler = tr_http._RedirectHandler(5.0, lambda: 9.0)
    fp = Closing()
    with pytest.raises(tr_http._OutOfTime):
        handler.redirect_request(urllib.request.Request(URL), fp, 301, "m", Message(), URL)
    assert fp.closed


class Finished(FakeResponse):
    """全部を渡した後に isclosed() が真になる応答（HTTPResponse と同じ）。"""

    def isclosed(self):
        return self.tell() == len(self.getvalue())


def test_body_fully_received_is_kept_even_if_the_deadline_passed_then(limits):
    now = [0.0]

    class Late(Finished):
        def read1(self, size=-1):
            now[0] += 100.0  # 読み終わった時点で上限を過ぎる
            return super().read1(size)

    open_url, _ = scripted(Late(b"abc"))
    response = fetch(URL, limits, open_url=open_url, deadline=50.0, clock=lambda: now[0])
    assert response.body == b"abc"


def _trickle_server(chunked, count=100, pause=0.2):
    """1バイトずつ少しずつ送り続ける本物のサーバー（別スレッド）。（ソケット, URL）を返す。"""
    import threading
    import time

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def serve():
        try:
            conn, _ = server.accept()
            conn.recv(4096)
            head = "HTTP/1.1 200 OK\r\nConnection: close\r\n"
            head += "Transfer-Encoding: chunked\r\n" if chunked else f"Content-Length: {count}\r\n"
            conn.sendall((head + "\r\n").encode())
            for _ in range(count):
                conn.sendall(b"1\r\nx\r\n" if chunked else b"x")
                time.sleep(pause)
            conn.close()
        except OSError:
            pass  # 相手が先に切った

    threading.Thread(target=serve, daemon=True).start()
    return server, f"http://127.0.0.1:{server.getsockname()[1]}/"


@pytest.mark.parametrize("chunked", [False, True])
def test_real_trickling_response_is_cut_near_the_deadline(limits, chunked):
    """0.2 秒ごとに1バイトずつ送る応答を、残りの時間（1 秒）の付近で打ち切る。"""
    import time

    server, url = _trickle_server(chunked)
    try:
        started = time.monotonic()
        with pytest.raises(FetchError) as e:
            fetch(url, limits, deadline=started + 1.0)
        assert e.value.category == "timeout"
        assert time.monotonic() - started < 3  # 全部（20 秒）や応答の上限（30 秒）を待たない
    finally:
        server.close()
