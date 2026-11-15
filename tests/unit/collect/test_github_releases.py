import base64
import json
import logging
import time
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tech_researcher.collect.aggregate import aggregate
from tech_researcher.collect.github_api import GitHubClient
from tech_researcher.collect.github_candidates import mentioned_repos
from tech_researcher.collect.github_releases import (
    SOURCE_ID,
    Readme,
    collect_releases,
    fetch_readmes,
    markdown_links,
    with_readme,
)
from tech_researcher.collect.http import FetchError, Response
from tech_researcher.collect.period_filter import in_period
from tech_researcher.collect.records import Event, FailedPart, Record
from tech_researcher.config import GitHubReleases
from tech_researcher.db.items import ItemRow, upsert_items
from tech_researcher.db.runs import start_run
from tech_researcher.db.schema import open_db
from tech_researcher.periods import daily_target

FIXTURE = (Path(__file__).parent / "fixtures" / "github_releases.json").read_bytes()
RELEASES = GitHubReleases(True, "GitHub Releases", ("example/tool",))
TARGET = daily_target(date(2026, 11, 3), 1)  # 期間は 11/2 0:00〜11/3 0:00（JST）
TOKEN = "ghp_" + "x" * 36  # 形だけの偽のトークン
BODY_MARK = "リリース本文の目印-a41c"
README_MARK = "README の本文の目印-77d2"


class FakeFetch:
    """URL ごとの応答（bytes か例外）を返す http.fetch の偽物。受けた要求を残す。"""

    def __init__(self, responses, attempts=1):
        self.responses = responses
        self.attempts = attempts
        self.calls = []

    def __call__(self, url, limits, *, headers, sleep, follow_redirects):
        self.calls.append((url, limits.http_retry_waits_s, dict(headers), follow_redirects))
        body = self.responses[url]
        if isinstance(body, Exception):
            raise body
        # 本物の fetch と同じく、試行は渡された再試行の回数＋1を超えない
        return Response(url, 200, body, min(self.attempts, len(limits.http_retry_waits_s) + 1))

    def urls(self):
        return [url for url, *_ in self.calls]


def client(limits, fetch, token=TOKEN):
    return GitHubClient(token, limits, fetch_url=fetch, sleep=lambda s: None)


def releases_url(repo, page=1):
    return f"https://api.github.com/repos/{repo}/releases?per_page=30&page={page}"


def readme_url(repo):
    return f"https://api.github.com/repos/{repo}/readme"


def readme_body(text):
    encoded = base64.encodebytes(text.encode()).decode()  # GitHub と同じく改行を含む
    return json.dumps({"encoding": "base64", "content": encoded}).encode()


def collect(limits, fetch, releases=RELEASES):
    return collect_releases(releases, TARGET.period, client(limits, fetch))


def test_releases_become_records(limits):
    fetch = FakeFetch({releases_url("example/tool"): FIXTURE})
    result = collect(limits, fetch)
    assert (result.source_id, result.error, result.attempts) == (SOURCE_ID, None, 1)
    assert result.failed_parts == ()
    url, _, headers, follow = fetch.calls[0]
    assert url == releases_url("example/tool")
    assert headers["Authorization"] == f"Bearer {TOKEN}" and follow is False
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


def test_blank_body_is_no_primary_text(limits):
    release = {
        "html_url": "https://github.com/a/b/releases/tag/v1",
        "tag_name": "v1",
        "published_at": "2026-11-02T00:00:00Z",
        "body": " \r\n",
    }
    fetch = FakeFetch({releases_url("example/tool"): json.dumps([release]).encode()})
    (record,) = collect(limits, fetch).records
    assert (record.primary_text, record.primary_text_origin) == (None, None)


def release_json(**extra):
    return {
        "html_url": "https://github.com/Example/Tool/releases/tag/v1",
        "tag_name": "v1",
        "published_at": "2026-11-02T00:00:00Z",
        **extra,
    }


@pytest.mark.parametrize("name", [None, "", "  "])
def test_blank_name_falls_back_to_tag(limits, name):
    fetch = FakeFetch(
        {releases_url("example/tool"): json.dumps([release_json(name=name)]).encode()}
    )
    (record,) = collect(limits, fetch).records
    assert record.title == "v1"
    # 設定の名前は小文字なので、meta_terms には本文に出る元の表記を残す
    assert record.meta_terms == ("Example/Tool", "v1")


def test_published_at_without_timezone_fails_the_repo(limits):
    body = json.dumps([release_json(published_at="2026-11-02T00:00:00")]).encode()
    result = collect(limits, FakeFetch({releases_url("example/tool"): body}))
    assert result.failed_parts == (FailedPart("example/tool", "invalid_response"),)
    assert result.error is not None  # 追跡先がこれだけなので、情報源は失敗


@pytest.mark.parametrize("tag", ["v" * 51, "あ" * 10, "v1 2"])
def test_unusual_tag_is_not_a_meta_term(limits, tag):
    """長い・版の表記らしくないタグで D4 の一致の判定を外せないよう、meta_terms に入れない。"""
    body = json.dumps([release_json(tag_name=tag)]).encode()
    (record,) = collect(limits, FakeFetch({releases_url("example/tool"): body})).records
    assert record.meta_terms == ("Example/Tool",)


def test_out_of_range_date_fails_only_that_repo(limits):
    repos = ("example/tool", "a/one")
    bad = json.dumps([release_json(published_at="0001-01-01T00:00:00+14:00")]).encode()
    fetch = FakeFetch({releases_url("example/tool"): bad, releases_url("a/one"): FIXTURE})
    result = collect(limits, fetch, replace(RELEASES, repos=repos))
    assert result.failed_parts == (FailedPart("example/tool", "invalid_response"),)
    assert result.records


@pytest.mark.parametrize(
    "error, category",
    [
        (FetchError("HTTP 401", 1, 401), "auth"),
        (FetchError("HTTP 429", 1, 429), "rate_limited"),
        (FetchError("HTTP 403", 1, 403, {"x-ratelimit-remaining": "0"}), "rate_limited"),
    ],
)
def test_requests_stop_after_auth_or_rate_limit(limits, error, category):
    """トークンが無効・レート制限なら、残りのリポジトリには要求を出さない。"""
    repos = ("a/one", "b/two", "c/three")
    status = error.status
    fetch = FakeFetch({releases_url("a/one"): error})
    result = collect(limits, fetch, replace(RELEASES, repos=repos))
    assert fetch.urls() == [releases_url("a/one")]
    assert result.failed_parts == tuple(FailedPart(r, category) for r in repos)
    assert result.error is not None
    readme_fetch = FakeFetch(
        {readme_url("a/one"): FetchError(f"HTTP {status}", 1, status, error.headers)}
    )
    readmes = fetch_readmes(["a/one", "b/two"], client(limits, readme_fetch))
    assert readme_fetch.urls() == [readme_url("a/one")]
    assert readmes.errors == {"a/one": category, "b/two": category}


def test_name_of_wrong_type_fails_the_repo(limits):
    body = json.dumps([release_json(name={"x": 1})]).encode()
    result = collect(limits, FakeFetch({releases_url("example/tool"): body}))
    assert result.failed_parts == (FailedPart("example/tool", "invalid_response"),)


def test_release_in_period_is_kept_and_old_one_is_dropped(limits):
    records = collect(limits, FakeFetch({releases_url("example/tool"): FIXTURE})).records
    items = [i for i in aggregate(records) if in_period(i, TARGET)]
    assert [i.url for i in items] == ["https://github.com/example/tool/releases/tag/v1.2.0"]
    # 候補リンクは正規化して持つ（D-06）
    assert "https://github.com/example/tool/pull/42" in items[0].links


def test_release_is_not_a_mention_of_its_own_repo(limits):
    """リリースのレコードは、そのリポジトリの言及にしない（github_candidates の活動のページ）。"""
    records = collect(limits, FakeFetch({releases_url("example/tool"): FIXTURE})).records
    assert "example/tool" not in mentioned_repos(records)


def test_each_repo_is_requested_and_attempts_add_up(limits):
    repos = ("a/one", "b/two")
    fetch = FakeFetch({releases_url(r): b"[]" for r in repos}, attempts=2)
    result = collect(limits, fetch, replace(RELEASES, repos=repos))
    assert fetch.urls() == [releases_url(r) for r in repos]
    assert (result.records, result.error, result.attempts) == ((), None, 4)


@pytest.mark.parametrize(
    "response, category",
    [
        (FetchError("HTTP 404", 1, 404), "not_found"),
        (FetchError("HTTP 401", 1, 401), "auth"),
        (FetchError("再試行を使い切った：HTTP 504", 3, 504), "http_5xx"),
        (b"{}", "invalid_response"),
        (b"not json", "invalid_response"),
        (  # tag_name がない
            json.dumps(
                [{"html_url": "https://github.com/b/two/x", "published_at": "2026-11-02T00:00:00Z"}]
            ).encode(),
            "invalid_response",
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
            "invalid_response",
        ),
    ],
)
def test_failed_repo_is_kept_as_failed_part(limits, response, category):
    """1件が取れなくても、取れたリポジトリの分は使い、取れなかったものを残す（US-E6-03）。"""
    repos = ("a/one", "b/two")
    fetch = FakeFetch({releases_url("a/one"): FIXTURE, releases_url("b/two"): response})
    result = collect(limits, fetch, replace(RELEASES, repos=repos))
    assert result.error is None
    assert len(result.records) == 2
    assert result.failed_parts == (FailedPart("b/two", category),)


def test_all_repos_failing_fails_the_source(limits):
    repos = ("a/one", "b/two")
    fetch = FakeFetch(
        {
            releases_url("a/one"): FetchError("HTTP 404", 1, 404),
            releases_url("b/two"): FetchError("HTTP 401", 1, 401),
        }
    )
    result = collect(limits, fetch, replace(RELEASES, repos=repos))
    assert result.records == ()
    assert result.error == "すべての追跡先が取れない（auth・not_found）"
    assert result.failed_parts == (FailedPart("a/one", "not_found"), FailedPart("b/two", "auth"))
    assert result.attempts == 2


def test_request_budget_is_shared_through_client(limits):
    repos = ("a/one", "b/two", "c/three")
    fetch = FakeFetch({releases_url(r): b"[]" for r in repos})
    result = collect(replace(limits, github_rest_calls=2), fetch, replace(RELEASES, repos=repos))
    assert result.error is None and len(fetch.calls) == 2
    assert result.failed_parts == (FailedPart("c/three", "budget"),)
    # 残りが1回なら、再試行の待ちを渡さない（上限を超えて再試行しない）
    assert [waits for _, waits, _, _ in fetch.calls] == [(5,), ()]


def test_token_does_not_appear_in_results_or_logs(limits, caplog):
    fetch = FakeFetch(
        {
            releases_url("example/tool"): FetchError("HTTP 401", 1, 401),
            readme_url("example/tool"): FetchError("HTTP 401", 1, 401),
        }
    )
    github = client(limits, fetch)
    with caplog.at_level(logging.DEBUG):
        result = collect_releases(RELEASES, TARGET.period, github)
        readmes = fetch_readmes(["example/tool"], github)
    assert result.failed_parts == (FailedPart("example/tool", "auth"),)
    assert readmes.errors == {"example/tool": "auth"}
    assert TOKEN not in repr(result) + repr(readmes) + caplog.text


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
    # 1ページ目はすべて期間内。2ページ目がすべて期間の始まりより前なら、そこで止める
    fetch = FakeFetch(
        {
            releases_url("example/tool", 1): page_of(0, "2026-11-02T12:00:00Z"),
            releases_url("example/tool", 2): page_of(30, "2026-10-01T00:00:00Z"),
        }
    )
    with caplog.at_level(logging.WARNING):
        result = collect(limits, fetch)
    assert fetch.urls() == [releases_url("example/tool", 1), releases_url("example/tool", 2)]
    assert len(result.records) == 60 and result.attempts == 2
    assert "取りこぼしたおそれ" not in caplog.text


def test_pages_are_not_cut_by_one_old_release(limits):
    """一覧が公開日時の順に並ぶ保証はないので、古いものが混ざっても次のページを取る。"""
    mixed = json.loads(page_of(0, "2026-11-02T12:00:00Z"))
    mixed[5]["published_at"] = "2026-09-01T00:00:00Z"
    fetch = FakeFetch(
        {
            releases_url("example/tool", 1): json.dumps(mixed).encode(),
            releases_url("example/tool", 2): page_of(30, "2026-11-02T13:00:00Z", count=3),
        }
    )
    result = collect(limits, fetch)
    assert len(fetch.calls) == 2 and len(result.records) == 33


def test_page_of_drafts_only_goes_to_next_page(limits):
    drafts = [release_json(draft=True, published_at=None) for _ in range(30)]
    fetch = FakeFetch(
        {
            releases_url("example/tool", 1): json.dumps(drafts).encode(),
            releases_url("example/tool", 2): page_of(0, "2026-11-02T12:00:00Z", count=2),
        }
    )
    result = collect(limits, fetch)
    assert len(fetch.calls) == 2 and len(result.records) == 2


def test_failure_on_later_page_fails_the_repo(limits):
    """途中のページで失敗したら、取れたページの分を含めてそのリポジトリを取れなかったものにする。"""
    repos = ("example/tool", "a/one")
    fetch = FakeFetch(
        {
            releases_url("example/tool", 1): page_of(0, "2026-11-02T12:00:00Z"),
            releases_url("example/tool", 2): FetchError("再試行を使い切った：HTTP 504", 3, 504),
            releases_url("a/one"): FIXTURE,
        }
    )
    result = collect(limits, fetch, replace(RELEASES, repos=repos))
    assert result.failed_parts == (FailedPart("example/tool", "http_5xx"),)
    assert {r.meta_terms[0] for r in result.records} == {"a/one"}


def test_possible_truncation_is_kept_as_failed_part(limits, caplog):
    """ページの上限に達したら、取れた分を使い、取りこぼしたおそれを読者に伝えられる形で残す。"""
    recent = "2026-11-02T12:00:00Z"
    fetch = FakeFetch(
        {releases_url("example/tool", p): page_of(p * 30, recent) for p in range(1, 7)}
    )
    with caplog.at_level(logging.WARNING):
        result = collect(limits, fetch)
    assert len(fetch.calls) == 5  # ページの上限で止める
    assert result.error is None and len(result.records) == 150
    assert result.failed_parts == (FailedPart("example/tool", "truncated"),)
    assert "取りこぼしたおそれ" in caplog.text


def test_readmes_are_fetched_for_given_repos(limits):
    text = f"# Tool\n\n{README_MARK}\n\n[docs](https://docs.example.com/tool)\n"
    fetch = FakeFetch(
        {
            readme_url("example/tool"): readme_body(text),
            readme_url("example/none"): FetchError("HTTP 404", 1, 404),
            readme_url("example/broken"): FetchError("再試行を使い切った：HTTP 500", 3, 500),
            readme_url("example/empty"): readme_body("  \n"),
            readme_url("example/large"): json.dumps({"encoding": "none", "content": ""}).encode(),
            readme_url("example/bad64"): json.dumps(
                {"encoding": "base64", "content": "abc"}
            ).encode(),
            readme_url("example/latin1"): json.dumps(
                {
                    "encoding": "base64",
                    "content": base64.b64encode(b"\xef\xbb\xbfcaf\xe9 ok").decode(),
                }
            ).encode(),
            readme_url("example/list"): b"[]",
            readme_url("example/junk64"): json.dumps(
                {"encoding": "base64", "content": "!!!!eA==!!!"}
            ).encode(),
            readme_url("example/utf16"): json.dumps(
                {
                    "encoding": "base64",
                    "content": base64.b64encode("\ufeffhi".encode("utf-16")).decode(),
                }
            ).encode(),
            readme_url("example/deep"): b"[" * 100_000,
            readme_url("example/nul"): readme_body("h\x00i"),
            readme_url("example/ref"): readme_body("&#" + "9" * 5000 + "; [d](https://d.example/)"),
        }
    )
    names = [
        "example/tool",
        "example/none",
        "example/broken",
        "example/empty",
        "example/large",
        "example/bad64",
        "example/latin1",
        "example/list",
        "example/junk64",
        "example/utf16",
        "example/deep",
        "example/nul",
        "example/ref",
        "Example/Tool",
    ]
    result = fetch_readmes(names, client(limits, fetch))
    assert fetch.urls() == [readme_url(n) for n in names[:13]]  # 大文字・小文字の違いは1回だけ
    assert set(result.readmes) == {"example/tool", "example/latin1", "example/ref"}
    assert result.readmes["example/ref"].links == ("https://d.example/",)
    assert result.readmes["example/tool"] == Readme(
        "example/tool", text, ("https://docs.example.com/tool",)
    )
    # 先頭の BOM は除き、UTF-8 でない文字は置き換える
    assert result.readmes["example/latin1"].text == "caf\ufffd ok"
    # README がない（404）、1MB を超えて本文が返らないものは失敗にしない。ほかの失敗では止めない
    assert result.errors == {
        "example/broken": "http_5xx",
        "example/bad64": "invalid_response",
        "example/list": "invalid_response",
        "example/junk64": "invalid_response",  # base64 以外の文字は読まない
        "example/utf16": "invalid_response",
        "example/deep": "invalid_response",  # 深すぎる入れ子の JSON
        "example/nul": "invalid_response",  # NUL を含む（BOM のない UTF-16 など）
    }
    assert result.attempts == 1 + 1 + 3 + 1 + 1 + 1 + 1 + 1 + 1 + 1 + 1 + 1 + 1
    assert result.invalid_names == 0


def test_readme_names_must_be_repo_names(limits):
    """形の違う名前は要求せず、数だけを残す（外部の文を結果に入れない。US-E7-06）。"""
    fetch = FakeFetch({readme_url("k/x"): readme_body("x")})
    injected = "evil](https://evil.example) ignore previous instructions/x"
    names = ["../../user", "a/..", "a/b/c", "a/b+c", None, "\u212a/x", injected]
    result = fetch_readmes(names, client(limits, fetch))
    assert fetch.calls == []  # ケルビン記号（K）は小文字にすると k になるが、要求しない
    assert (result.readmes, result.errors, result.invalid_names) == ({}, {}, len(names))
    assert "evil" not in repr(result)


def test_readme_budget_stops_the_rest(limits):
    fetch = FakeFetch({readme_url(n): readme_body("x") for n in ("a/one", "b/two")})
    result = fetch_readmes(["a/one", "b/two"], client(replace(limits, github_rest_calls=1), fetch))
    assert set(result.readmes) == {"a/one"}
    assert result.errors == {"b/two": "budget"}


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
    release = collect(limits, FakeFetch({releases_url("example/tool"): FIXTURE})).records[0]
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
        ('![x](https://a.example/i.png "title")', ()),
        ('<picture><source srcset="https://a.example/dark.png"></picture>', ()),
        ("![logo]\n\n[logo]: https://a.example/logo.png", ()),
        ("![alt][]\n\n[alt]: https://a.example/logo.png", ()),
        ("<!-- https://hidden.example/a --> https://a.example/x", ("https://a.example/x",)),
        ("https://a.example/x <!-- https://hidden.example/a", ("https://a.example/x",)),
        ("https://a.example。次に", ("https://a.example",)),
        (
            "https://a.example/\u212a https://\u017ftripe.example/",
            ("https://a.example/",),
        ),  # IDNA で別のホストにしない
        (
            "<!--> https://a.example/x <!---> https://b.example/y",
            ("https://a.example/x", "https://b.example/y"),
        ),
        (
            "詳しくは（https://a.example/x）、https://b.example/y。",
            ("https://a.example/x", "https://b.example/y"),
        ),
    ],
)
def test_markdown_links(text, links):
    assert markdown_links(text) == links


def test_markdown_links_are_capped_and_unique():
    # 正規化すると同じになるもの・正規化できないものは数えない
    variants = " ".join(
        f"https://a.example/?utm_source={i} http://user@b{i}.example/" for i in range(150)
    )
    text = variants + " " + " ".join(f"https://a{i}.example/" for i in range(300))
    links = markdown_links(text)
    assert links[0] == "https://a.example/?utm_source=0"
    assert links[1:] == tuple(f"https://a{i}.example/" for i in range(99))


def test_long_urls_and_numeric_references_are_handled():
    assert markdown_links("https://a.example/" + "a" * 3000 + " https://b.example/") == (
        "https://b.example/",
    )
    # 桁数の多い数値文字参照で例外にしない（html.unescape は 4300 桁を超えると ValueError）
    assert markdown_links("&#" + "0" * 5000 + "; https://a.example/x &#65;") == (
        "https://a.example/x",
    )


def test_deadline_stops_the_rest(limits):
    repos = ("a/one", "b/two", "c/three")
    fetch = FakeFetch({releases_url(r): b"[]" for r in repos})
    times = iter([0.0, 0.0, 5.0, 5.0, 11.0])  # リポジトリの前とページの前に確かめる
    result = collect_releases(
        replace(RELEASES, repos=repos),
        TARGET.period,
        client(limits, fetch),
        deadline=10.0,
        clock=lambda: next(times),
    )
    assert fetch.urls() == [releases_url("a/one"), releases_url("b/two")]
    assert result.error is None
    assert result.failed_parts == (FailedPart("c/three", "timeout"),)


def test_deadline_stops_between_pages(limits):
    fetch = FakeFetch({releases_url("a/one"): json.dumps([{"prerelease": True}] * 30).encode()})
    times = iter([0.0, 0.0, 11.0])  # 1ページ目の後、2ページ目の前で上限を過ぎる
    result = collect_releases(
        replace(RELEASES, repos=("a/one",)),
        TARGET.period,
        client(limits, fetch),
        deadline=10.0,
        clock=lambda: next(times),
    )
    assert len(fetch.calls) == 1
    assert result.error is not None and result.records == ()
    assert result.failed_parts == (FailedPart("a/one", "timeout"),)


def test_published_release_without_date_fails_the_repo(limits):
    item = {"html_url": "https://github.com/example/tool/releases/tag/v1", "tag_name": "v1"}
    fetch = FakeFetch({releases_url("example/tool"): json.dumps([item]).encode()})
    result = collect_releases(RELEASES, TARGET.period, client(limits, fetch))
    assert result.error is not None
    assert result.failed_parts == (FailedPart("example/tool", "invalid_response"),)


def test_non_boolean_draft_flag_fails_the_repo(limits):
    item = {"draft": "false", "prerelease": False}
    fetch = FakeFetch({releases_url("example/tool"): json.dumps([item]).encode()})
    result = collect_releases(RELEASES, TARGET.period, client(limits, fetch))
    assert result.failed_parts == (FailedPart("example/tool", "invalid_response"),)


def test_image_with_parentheses_in_url_is_not_a_link():
    text = "![図](https://img.example.com/a_(1).png) [docs](https://docs.example.com/x)"
    assert markdown_links(text) == ("https://docs.example.com/x",)


def test_query_with_legacy_entity_names_is_kept_whole():
    text = "https://example.com/s?q=a&region=us&param=1&notify=2 [x](https://ex.com/?a=1&amp;b=2)"
    assert markdown_links(text) == (
        "https://example.com/s?q=a&region=us&param=1&notify=2",
        "https://ex.com/?a=1&b=2",
    )


def test_readme_deadline_stops_the_rest(limits):
    fetch = FakeFetch(
        {readme_url("a/one"): readme_body("x"), readme_url("b/two"): readme_body("y")}
    )
    times = iter([0.0, 11.0])
    result = fetch_readmes(
        ["a/one", "b/two"], client(limits, fetch), deadline=10.0, clock=lambda: next(times)
    )
    assert fetch.urls() == [readme_url("a/one")]
    assert result.errors == {"b/two": "timeout"}


@pytest.mark.parametrize(
    "unit",
    [
        "![a",
        "![a](",
        "<img ",
        "<source ",
        "<!--",
        "http://",
        "[a]: ",
        "![a][",
        "&amp;",
        "![",
        "&#0",
    ],
)
def test_markdown_links_time_is_linear(unit):
    """外部の本文の最悪の形でも、時間が本文の長さに比例する（1MB ほどで1秒より十分短い）。"""
    text = unit * (1_000_000 // len(unit))
    started = time.perf_counter()
    markdown_links(text)
    assert time.perf_counter() - started < 1.0
