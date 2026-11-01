import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tech_researcher.config import load_config

CONFIG_PATH = Path(__file__).parents[3] / "config" / "tech-researcher.toml"


@pytest.fixture
def limits():
    # テストで使う値は明示する。設定ファイルの値を運用で変えても、テストが壊れないように
    return replace(
        load_config(CONFIG_PATH).limits,
        http_connect_timeout_s=10,
        http_read_timeout_s=30,
        http_retry_waits_s=(5, 20),
        retry_after_max_s=60,
    )


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.paths.append(self.path)
        if self.path == "/slow":
            self.server.release.wait(5)  # 応答を返さない
            return
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "file:///etc/passwd")
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(self.headers["User-Agent"].encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    """手元の HTTP サーバー。受けたパスは server.paths に残る。"""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.release = threading.Event()
    srv.paths = []
    srv.base = f"http://127.0.0.1:{srv.server_address[1]}"
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.release.set()
    srv.shutdown()
    srv.server_close()
    thread.join()
