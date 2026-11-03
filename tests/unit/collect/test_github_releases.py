import json
import logging
import threading
import urllib.error
from dataclasses import replace
from datetime import UTC, date, datetime
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tech_researcher.collect.aggregate import aggregate
from tech_researcher.collect.github_releases import (
    JSON,
    RAW,
    SOURCE_ID,
    Readme,
    api_get,
    collect_releases,
    fetch_readmes,
    markdown_links,
    with_readme,
)
from tech_researcher.collect.http import FetchError, Response
from tech_researcher.collect.period_filter import in_period
from tech_researcher.collect.records import Event, Record
from tech_researcher.config import GitHubReleases
from tech_researcher.db.items import ItemRow, upsert_items
from tech_researcher.db.runs import start_run
from tech_researcher.db.schema import open_db
from tech_researcher.periods import daily_target

FIXTURE = (Path(__file__).parent / "fixtures" / "github_releases.json").read_bytes()
RELEASES = GitHubReleases(True, "GitHub Releases", ("example/tool",))
TARGET = daily_target(date(2026, 11, 3), 1)  # 期間は 11/2 0:00〜11/3 0:00（JST）
TOKEN = "dummy-collect-token-9f3e"
BODY_MARK = "リリース本文の目印-a41c"
README_MARK = "README の本文の目印-77d2"


class FakeApi:
    """URL ごとの応答（bytes か例外）を返す。受けた要求と、渡された再試行の待ちを残す。"""

    def __init__(self, responses, attempts=1):
        self.responses = responses
        self.attempts = attempts
        self.calls = []

    def __call__(self, url, accept, limits):
        self.calls.append((url, accept, limits.http_retry_waits_s))
        body = self.responses[url]
        if isinstance(body, Exception):
            raise body
        # 本物の fetch と同じく、試行は渡された再試行の回数＋1を超えない
        return Response(url, 200, body, min(self.attempts, len(limits.http_retry_waits_s) + 1))


def releases_url(repo, page=1):
    return f"https://api.github.com/repos/{repo}/releases?per_page=30&page={page}"


def readme_url(repo):
    return f"https://api.github.com/repos/{repo}/readme"


def test_releases_become_records(limits):
    api = FakeApi({releases_url("example/tool"): FIXTURE})
    result = collect_releases(RELEASES, TARGET.period, limits, get=api)
    assert (result.source_id, result.error, result.attempts) == (SOURCE_ID, None, 1)
    assert api.calls == [(releases_url("example/tool"), JSON, (5, 20))]
    # 下書きとプレリリースは除く
    assert [r.url for r in result.records] == [
        "https://github.com/example/tool/releases/tag/v1.2.0",
        "https://github.com/example/tool/releases/tag/v1.1.0",
    ]
    record = result.records[0]
    published = datetime(2026, 11, 2, 3, 5, tzinfo=UTC)
    assert record.title == "Tool 1.2.0"
    assert record.events == (Event("released", published),)
    assert (record.published_at, record.published_precision) == (published, "datetime")
    assert record.primary_text_origin == "github_release" and BODY_MARK in record.primary_text
    assert record.excerpt is None  # 抜粋は DB に保存されるので、本文から作らない（D-18）
    assert record.meta_terms == ("example/tool", "v1.2.0")
    assert record.links == (
        "https://ci.example.com/tool",
        "https://docs.example.com/tool/guide",
        "https://example.com/blog/1-2-0",
        "https://github.com/example/tool/pull/42",
        "https://example.com/html-link",
        "https://example.com/install.sh",
    )
    # 名前が空なら tag を題にする。本文がなければ本文なし
    empty = result.records[1]
    assert empty.title == "v1.1.0"
    assert (empty.primary_text, empty.primary_text_origin, empty.links) == (None, None, ())


def test_release_in_period_is_kept_and_old_one_is_dropped(limits):
    api = FakeApi({releases_url("example/tool"): FIXTURE})
    records = collect_releases(RELEASES, TARGET.period, limits, get=api).records
    items = [i for i in aggregate(records) if in_period(i, TARGET)]
    assert [i.url for i in items] == ["https://github.com/example/tool/releases/tag/v1.2.0"]
    # 候補リンクは正規化して持つ（D-06）
    assert "https://github.com/example/tool/pull/42" in items[0].links


def test_each_repo_is_requested_and_attempts_add_up(limits):
    repos = ("a/one", "b/two")
    api = FakeApi({releases_url(r): b"[]" for r in repos}, attempts=2)
    result = collect_releases(replace(RELEASES, repos=repos), TARGET.period, limits, get=api)
    assert [url for url, _, _ in api.calls] == [releases_url(r) for r in repos]
    assert (result.records, result.error, result.attempts) == ((), None, 4)


@pytest.mark.parametrize(
    "response, message",
    [
        (FetchError("HTTP 404", 1, 404), "b/two: HTTP 404"),
        (b"{}", "b/two: Releases API の応答として読めない"),
        (b"not json", "読めない"),
        (  # tag_name がない
            json.dumps(
                [{"html_url": "https://github.com/b/two/x", "published_at": "2026-11-02T00:00:00Z"}]
            ).encode(),
            "読めない：KeyError",
        ),
        (
            json.dumps(
                [
                    {
                        "html_url": "https://evil.example/x",
                        "tag_name": "v1",
                        "published_at": "2026-11-02T00:00:00Z",
                    }
                ]
            ).encode(),
            "github.com の URL でない",
        ),
    ],
)
def test_one_failed_repo_fails_the_source(limits, response, message):
    repos = ("a/one", "b/two")
    api = FakeApi({releases_url("a/one"): FIXTURE, releases_url("b/two"): response})
    result = collect_releases(replace(RELEASES, repos=repos), TARGET.period, limits, get=api)
    assert result.records == ()
    assert message in result.error
    assert result.attempts == 2


def test_request_budget_is_shared_by_repos(limits):
    repos = ("a/one", "b/two", "c/three")
    api = FakeApi({releases_url(r): b"[]" for r in repos})
    small = replace(limits, github_rest_calls=2)
    result = collect_releases(replace(RELEASES, repos=repos), TARGET.period, small, get=api)
    assert "要求の上限 2 回に達した" in result.error
    # 残りが1回なら、再試行の待ちを渡さない（上限を超えて再試行しない）
    assert [waits for _, _, waits in api.calls] == [(5,), ()]


def page_of(start, published_at, count=30):
    return json.dumps(
        [
            {
                "html_url": f"https://github.com/example/tool/releases/tag/v{n}",
                "tag_name": f"v{n}",
                "published_at": published_at,
            }
            for n in range(start, start + count)
        ]
    ).encode()


def test_pages_are_followed_until_period_start(limits, caplog):
    # 1ページ目はすべて期間内。2ページ目に期間の始まりより前のものがあれば、そこで止める
    api = FakeApi(
        {
            releases_url("example/tool", 1): page_of(0, "2026-11-02T12:00:00Z"),
            releases_url("example/tool", 2): page_of(30, "2026-10-01T00:00:00Z"),
        }
    )
    with caplog.at_level(logging.WARNING):
        result = collect_releases(RELEASES, TARGET.period, limits, get=api)
    assert [url for url, _, _ in api.calls] == [
        releases_url("example/tool", 1),
        releases_url("example/tool", 2),
    ]
    assert len(result.records) == 60 and result.attempts == 2
    assert "取りこぼしたおそれ" not in caplog.text


def test_pages_are_not_cut_by_one_old_release(limits):
    """一覧が公開日時の順に並ぶ保証はないので、古いものが混ざっても次のページを取る。"""
    mixed = json.loads(page_of(0, "2026-11-02T12:00:00Z"))
    mixed[5]["published_at"] = "2026-09-01T00:00:00Z"
    api = FakeApi(
        {
            releases_url("example/tool", 1): json.dumps(mixed).encode(),
            releases_url("example/tool", 2): page_of(30, "2026-11-02T13:00:00Z", count=3),
        }
    )
    result = collect_releases(RELEASES, TARGET.period, limits, get=api)
    assert len(api.calls) == 2 and len(result.records) == 33


def test_possible_truncation_is_logged(limits, caplog):
    recent = "2026-11-02T12:00:00Z"
    api = FakeApi({releases_url("example/tool", p): page_of(p * 30, recent) for p in range(1, 7)})
    with caplog.at_level(logging.WARNING):
        result = collect_releases(RELEASES, TARGET.period, limits, get=api)
    assert len(api.calls) == 5  # ページの上限で止める
    assert result.error is None and len(result.records) == 150
    assert "取りこぼしたおそれ" in caplog.text


def test_readmes_are_fetched_for_given_repos(limits):
    text = f"# Tool\n\n{README_MARK}\n\n[docs](https://docs.example.com/tool)\n"
    api = FakeApi(
        {
            readme_url("example/tool"): text.encode(),
            readme_url("example/none"): FetchError("HTTP 404", 1, 404),
            readme_url("example/broken"): FetchError("HTTP 500", 3, 500),
            readme_url("example/empty"): b"  \n",
        }
    )
    names = ["example/tool", "example/none", "example/broken", "example/empty", "Example/Tool"]
    result = fetch_readmes(names, limits, get=api)
    assert [(url, accept) for url, accept, _ in api.calls] == [
        (readme_url(n), RAW) for n in names[:4]
    ]  # 同じ名前は1回だけ
    assert result.readmes == {
        "example/tool": Readme("example/tool", text, ("https://docs.example.com/tool",))
    }
    # README がない（404）のは失敗にしない。ほかの失敗では止めずに続ける
    assert result.errors == {"example/broken": "HTTP 500"}
    assert result.attempts == 1 + 1 + 3 + 1


def test_readme_names_must_be_repo_names(limits):
    api = FakeApi({})
    result = fetch_readmes(["../../user", "a/..", "a/b/c", None], limits, get=api)
    assert api.calls == []
    assert set(result.errors) == {"../../user", "a/..", "a/b/c", "None"}


def test_readme_budget_stops_the_rest(limits):
    api = FakeApi({readme_url(n): b"x" for n in ("a/one", "b/two")})
    result = fetch_readmes(["a/one", "b/two"], replace(limits, github_rest_calls=1), get=api)
    assert set(result.readmes) == {"a/one"}
    assert "要求の上限 1 回に達した" in result.errors["b/two"]


def repo_record(**extra):
    return Record(
        source_id="github_repos",
        url="https://github.com/example/tool",
        title="example/tool",
        published_precision="unknown",
        fetched_at=datetime(2026, 11, 2, 21, 0, tzinfo=UTC),
        events=(Event("new_repo", datetime(2026, 11, 2, 21, 0, tzinfo=UTC)),),
        links=("https://github.com/example/tool",),
        **extra,
    )


def test_readme_is_attached_as_primary_text():
    readme = Readme("example/tool", README_MARK, ("https://docs.example.com/tool",))
    record = with_readme(repo_record(), readme)
    assert (record.primary_text, record.primary_text_origin) == (README_MARK, "github_readme")
    assert record.links == ("https://github.com/example/tool", "https://docs.example.com/tool")
    with pytest.raises(ValueError, match="本文をすでに持つ"):
        with_readme(record, readme)


def test_bodies_are_not_stored_in_db(tmp_path, limits):
    """リリース本文・README は DB に残らず、ハッシュと文字数だけが残る（D-14・D-18）。"""
    api = FakeApi({releases_url("example/tool"): FIXTURE})
    release = collect_releases(RELEASES, TARGET.period, limits, get=api).records[0]
    repo = with_readme(repo_record(), Readme("example/tool", README_MARK, ()))
    db_path = tmp_path / "tech-researcher.sqlite3"
    conn = open_db(db_path)
    run_id = "20261103-060000-abcd"
    start_run(conn, run_id, TARGET, datetime(2026, 11, 2, 21, 0, tzinfo=UTC))
    rows = [
        ItemRow(
            cadence="daily",
            target=TARGET.key,
            item=item,
            kind=kind,
            title=item.sources[0].title,
            published_precision=item.sources[0].published_precision,
            inclusion=item.events[0],
            excerpt=item.sources[0].excerpt,
            published_at=item.sources[0].published_at,
            primary_text=item.sources[0].primary_text,
        )
        for item, kind in zip(aggregate([release, repo]), ("release", "repo"), strict=True)
    ]
    upsert_items(conn, run_id, rows)
    lengths = conn.execute("SELECT primary_text_len FROM items ORDER BY item_id").fetchall()
    assert [n for (n,) in lengths] == [len(release.primary_text), len(README_MARK)]
    conn.close()  # 最後の接続を閉じると WAL が DB に書き戻される
    data = b"".join(p.read_bytes() for p in tmp_path.iterdir())
    assert BODY_MARK.encode() not in data
    assert README_MARK.encode() not in data
    assert b"Tool 1.2.0" in data  # 確かめ方が正しいことの対照（タイトルは保存する）


class _Opened:
    """open_url の偽の応答（http.fetch が読む分だけ）。"""

    status = 200
    headers = Message()

    def __init__(self, url):
        self.url = url

    def read(self, _size):
        return b"[]"

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_api_get_sends_headers_and_keeps_token_off_redirects(limits):
    seen = []

    def open_url(req):
        seen.append(req)
        return _Opened(req.full_url)

    api_get(releases_url("a/b"), JSON, limits, TOKEN, open_url=open_url)
    req = seen[0]
    assert req.get_header("Accept") == JSON
    assert req.get_header("X-github-api-version") == "2022-11-28"
    assert req.get_header("User-agent").startswith("tech-researcher/")
    # リダイレクトで写されない置き場所にだけ載せる
    assert req.unredirected_hdrs["Authorization"] == f"Bearer {TOKEN}"
    assert "Authorization" not in req.headers

    seen.clear()
    api_get(releases_url("a/b"), JSON, limits, None, open_url=open_url)
    assert not seen[0].has_header("Authorization")


def test_token_does_not_appear_in_errors(limits):
    def open_url(req):
        raise urllib.error.HTTPError(req.full_url, 401, "Bad credentials", Message(), None)

    def get(url, accept, limits):
        return api_get(url, accept, limits, TOKEN, open_url=open_url)

    result = collect_releases(RELEASES, TARGET.period, limits, get=get)
    assert result.error == "example/tool: HTTP 401"
    readmes = fetch_readmes(["example/tool"], limits, get=get)
    assert TOKEN not in repr(readmes) and TOKEN not in repr(result)


class _RedirectHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.seen.append((self.path, self.headers.get("Authorization")))
        if self.path == "/from":
            self.send_response(301)
            self.send_header("Location", "/to")
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"[]")

    def log_message(self, *args):
        pass


def test_redirect_is_not_followed(limits):
    """名前を変えたリポジトリの 301 には従わない。数えない要求も、トークンなしの要求も出さない。"""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
    srv.seen = []
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/from"
        with pytest.raises(FetchError) as caught:
            api_get(url, JSON, limits, TOKEN)
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join()
    assert (caught.value.status, caught.value.attempts) == (301, 1)
    assert srv.seen == [("/from", f"Bearer {TOKEN}")]


def test_token_with_invalid_characters_is_rejected_without_its_value(limits):
    seen = []

    def open_url(req):
        seen.append(req)
        return _Opened(req.full_url)

    with pytest.raises(FetchError) as caught:
        api_get(releases_url("a/b"), JSON, limits, "abc\ndef-secret", open_url=open_url)
    assert "secret" not in str(caught.value) and seen == []
    # 末尾の改行（CRLF の env_file）は落として使う
    api_get(releases_url("a/b"), JSON, limits, f"{TOKEN}\r\n", open_url=open_url)
    assert seen[0].unredirected_hdrs["Authorization"] == f"Bearer {TOKEN}"
    result = collect_releases(
        RELEASES,
        TARGET.period,
        limits,
        get=lambda url, accept, lim: api_get(url, accept, lim, "x\ny-secret", open_url=open_url),
    )
    assert "secret" not in result.error


@pytest.mark.parametrize(
    "text, links",
    [
        (None, ()),
        ("リンクなし", ()),
        (
            "[a](https://a.example/x) and [b](http://b.example/y)",
            ("https://a.example/x", "http://b.example/y"),
        ),
        ("See https://a.example/x.", ("https://a.example/x",)),
        ("(https://a.example/x)", ("https://a.example/x",)),
        ("[ref]: https://a.example/ref", ("https://a.example/ref",)),
        ("![img](https://a.example/i.png)", ()),
        ('<img alt="x" src="https://a.example/i.png"/>', ()),
        ("[![b](https://a.example/badge.svg)](https://a.example/ci)", ("https://a.example/ci",)),
        ("[local](docs/a.md) [abs](/x)", ()),
        ("javascript:alert(1) ftp://a.example/x", ()),
        ('<a href="https://a.example/x?b=1&amp;c=2">x</a>', ("https://a.example/x?b=1&c=2",)),
        (
            "[![b][badge]][ci]\n\n[badge]: https://a.example/b.svg\n[ci]: https://a.example/ci",
            ("https://a.example/ci",),
        ),
        ("&lt;img src=&quot;https://a.example/i.png&quot;&gt;", ()),
    ],
)
def test_markdown_links(text, links):
    assert markdown_links(text) == links
