"""GitHub Releases と README の一次情報の取得（D-11・D-14、data-model.md 2.3・2.6・2.7）。

Releases は REST の `GET /repos/{owner}/{repo}/releases` で取る（releases.atom は robots.txt で
禁止されているので使わない。D-11）。README は `GET /repos/{owner}/{repo}/readme` の生の本文を、
渡されたリポジトリ（急上昇・新規の検出が選んだもの）について取る。
本文（リリース本文・README）は primary_text にだけ入れ、抜粋にはしない。抜粋は DB に保存される
ため（D-18）。候補リンクは本文の Markdown から集める。

GitHub REST の要求は api_get の1か所で行う。共通の取得処理（collect/github_api.py）ができたら、
そちらに寄せる。収集用トークン（D-05）は Authorization にだけ載せ、リダイレクト先には送らない。
"""

import html
import json
import logging
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from tech_researcher.collect.http import FetchError, Response, _opener, fetch
from tech_researcher.collect.records import Event, Record, SourceResult
from tech_researcher.config import REPO_NAME, GitHubReleases, Limits
from tech_researcher.periods import Period

log = logging.getLogger(__name__)

SOURCE_ID = "github_releases"
API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
JSON = "application/vnd.github+json"
RAW = "application/vnd.github.raw+json"  # README の生の本文
# 1ページの件数。100 件では本文の大きいリポジトリ（openai/codex）で HTTP 504 が続いた
# （2026-11-04）ので、既定の 30 件にしてページを送る
PER_PAGE = 30
MAX_PAGES = 5  # 1リポジトリあたりのページの上限。設定の誤りなどで要求を増やしすぎないように


def api_get(
    url: str,
    accept: str,
    limits: Limits,
    token: str | None,
    *,
    open_url: Callable[[urllib.request.Request], Any] | None = None,
    fetch_url=fetch,
) -> Response:
    """GitHub REST の GET。再試行・タイムアウトは http.fetch に任せる。

    リダイレクトには従わず、HTTP 301 などの失敗にする。名前を変えたリポジトリへの 301 で、
    要求の回数に数えない要求を出したり、トークンなしで要求し直したりしないため。
    名前を変えたリポジトリは、設定を新しい名前に直す。
    トークンは念のため add_unredirected_header で載せる（リダイレクト先へ写さない置き場所）。
    トークンの値は例外・戻り値に入れない。ヘッダに使えない文字（末尾の改行など）を含むと、
    標準ライブラリが値を含む例外を出すので、要求の前に決まった文の失敗にする。
    """
    if token is not None:
        token = token.strip()  # CRLF の env_file 対策
        if token and not _TOKEN_CHARS.fullmatch(token):
            raise FetchError("収集用トークンにヘッダに使えない文字がある", 0)
    if open_url is None:
        opener = _opener(limits)
        opener.add_handler(_NoRedirect())

        def open_url(req):
            return opener.open(req, timeout=limits.http_connect_timeout_s)

    def open_with_headers(req: urllib.request.Request):
        req.add_header("Accept", accept)
        req.add_header("X-GitHub-Api-Version", API_VERSION)
        if token:
            req.add_unredirected_header("Authorization", f"Bearer {token}")
        return open_url(req)

    return fetch_url(url, limits, open_url=open_with_headers)


_TOKEN_CHARS = re.compile(r"[\x21-\x7e]+")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """リダイレクトに従わない（既定の HTTPRedirectHandler より先に呼ばれる）。"""

    handler_order = 400

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


Get = Callable[[str, str, Limits], Response]


class _Budget:
    """1回の呼び出しの要求の回数（再試行を含む）を limits.github_rest_calls までにする。

    残りの回数を超えないよう、1回の要求の再試行を残りに合わせて減らす（qiita と同じ）。
    """

    def __init__(self, limits: Limits, get: Get):
        self.limits = limits
        self.get = get
        self.attempts = 0

    def __call__(self, url: str, accept: str) -> Response:
        remaining = self.limits.github_rest_calls - self.attempts
        if remaining < 1:
            raise FetchError(f"要求の上限 {self.limits.github_rest_calls} 回に達した", 0)
        waits = self.limits.http_retry_waits_s[: remaining - 1]
        try:
            response = self.get(url, accept, replace(self.limits, http_retry_waits_s=waits))
        except FetchError as e:
            self.attempts += e.attempts
            raise
        self.attempts += response.attempts
        return response


def _getter(token: str | None) -> Get:
    return lambda url, accept, limits: api_get(url, accept, limits, token)


def collect_releases(
    releases: GitHubReleases,
    period: Period,
    limits: Limits,
    *,
    token: str | None = None,
    get: Get | None = None,
) -> SourceResult:
    """追跡するリポジトリのリリースを、期間の始まりより前のものが出るまでページを送って取る。

    期間の絞り込みは、ほかの情報源とまとめた後に period_filter で行う。
    下書き（draft）とプレリリースは除く。プレリリースは本文がほとんどない版が多いため
    （openai/codex の alpha など。2026-11-04 に確認）。
    どれかのリポジトリが失敗したら、情報源全体を失敗にする（rss・qiita と同じ）。
    MAX_PAGES まで送っても期間の始まりに届かなければ、取りこぼしのおそれをログに残す。
    """
    budget = _Budget(limits, get or _getter(token))
    fetched_at = datetime.now(UTC)
    records: dict[str, Record] = {}
    for full_name in releases.repos:
        try:
            for record in _repo_releases(full_name, period, budget, fetched_at):
                records.setdefault(record.url, record)
        except FetchError as e:
            return SourceResult(SOURCE_ID, (), budget.attempts, f"{full_name}: {e}")
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            error = f"{full_name}: Releases API の応答として読めない：{type(e).__name__}: {e}"
            return SourceResult(SOURCE_ID, (), budget.attempts, error)
    return SourceResult(SOURCE_ID, tuple(records.values()), budget.attempts)


def _repo_releases(
    full_name: str, period: Period, budget: _Budget, fetched_at: datetime
) -> list[Record]:
    """1つのリポジトリのリリースを、新しい順のページで期間の始まりまで取る。

    一覧が公開日時の順に並ぶ保証はない（公式の文書に書かれていない）ので、ページの中の
    すべてが期間の始まりより前になったときだけ止める。日次でもふつうは2ページを取る。
    下書き・プレリリースも件数に数わるので、プレリリースの多いリポジトリはページが増える。
    """
    records = []
    for page in range(1, MAX_PAGES + 1):
        url = f"{API_ROOT}/repos/{full_name}/releases?per_page={PER_PAGE}&page={page}"
        data = _json_list(budget(url, JSON).body)
        records += [r for r in (_release(full_name, x, fetched_at) for x in data) if r]
        dates = [_utc(x["published_at"]) for x in data if x.get("published_at")]
        if len(data) < PER_PAGE or (dates and max(dates) < period.start):
            return records
    log.warning("%s：%s の期間内のリリースを取りこぼしたおそれ", SOURCE_ID, full_name)
    return records


def _json_list(body: bytes) -> list[dict]:
    data = json.loads(body)
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        raise TypeError("オブジェクトの配列でない")
    return data


def _release(full_name: str, release: dict, fetched_at: datetime) -> Record | None:
    """リリース1件を収集レコードにする。下書き・プレリリースは None。"""
    if release.get("draft") or release.get("prerelease") or not release.get("published_at"):
        return None
    url = release["html_url"]
    if not isinstance(url, str) or not url.startswith("https://github.com/"):
        raise ValueError("html_url が github.com の URL でない")
    tag = release["tag_name"]
    if not isinstance(tag, str) or not tag:
        raise ValueError("tag_name がない")
    published_at = _utc(release["published_at"])
    body = release.get("body")
    if body is not None and not isinstance(body, str):
        raise TypeError("body が文字列でない")
    if body is not None and not body.strip():
        body = None  # 空白だけの本文は本文なし（README と同じ）
    return Record(
        source_id=SOURCE_ID,
        url=url,
        title=release.get("name") or tag,
        published_precision="datetime",
        fetched_at=fetched_at,
        events=(Event("released", published_at),),
        primary_text=body,
        primary_text_origin="github_release" if body else None,
        published_at=published_at,
        links=markdown_links(body),
        meta_terms=(full_name, tag),
    )


def _utc(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError("日時にタイムゾーンがない")
    return moment.astimezone(UTC)


@dataclass(frozen=True)
class Readme:
    """リポジトリの README の本文（Markdown など、外部由来で信頼しない）と、その中のリンク。"""

    full_name: str
    text: str
    links: tuple[str, ...]


@dataclass(frozen=True)
class ReadmeResult:
    """README の取得結果。README のないリポジトリは readmes にも errors にも入らない。

    errors はリポジトリ名から失敗の理由への対応。attempts は初回を含めた要求の回数。
    """

    readmes: dict[str, Readme]
    errors: dict[str, str]
    attempts: int


def fetch_readmes(
    full_names: Iterable[str],
    limits: Limits,
    *,
    token: str | None = None,
    get: Get | None = None,
) -> ReadmeResult:
    """渡されたリポジトリ（`owner/repo`）の README を取る。

    1件の失敗では止めず、続きを取る（README は項目の補足で、なくても項目は載せられるため）。
    要求の上限に達したら、残りを失敗にする。README がない（HTTP 404）ものは失敗にしない。
    名前は外部の URL から抜き出したものでありうるので、`owner/repo` の形でなければ要求しない。
    GitHub の名前は大文字・小文字を区別しないので、違いがそれだけの名前は最初のものだけを取る。
    """
    budget = _Budget(limits, get or _getter(token))
    readmes: dict[str, Readme] = {}
    errors: dict[str, str] = {}
    seen: set[str] = set()
    for full_name in full_names:
        if not isinstance(full_name, str) or not REPO_NAME.fullmatch(full_name):
            errors[str(full_name)] = "owner/repo の形でない"
            continue
        if full_name.casefold() in seen:
            continue
        seen.add(full_name.casefold())
        try:
            response = budget(f"{API_ROOT}/repos/{full_name}/readme", RAW)
        except FetchError as e:
            if e.status != 404:
                errors[full_name] = str(e)
            continue
        text = response.body.decode("utf-8", errors="replace")
        if text.strip():
            readmes[full_name] = Readme(full_name, text, markdown_links(text))
    return ReadmeResult(readmes, errors, budget.attempts)


def with_readme(record: Record, readme: Readme) -> Record:
    """リポジトリの項目のレコードに README を一次情報の本文として加える。

    本文をすでに持つレコードには加えない（どちらの本文かが分からなくなるため）。
    """
    if record.primary_text is not None:
        raise ValueError("本文をすでに持つレコードには README を加えない")
    return replace(
        record,
        primary_text=readme.text,
        primary_text_origin="github_readme",
        links=record.links + readme.links,
    )


# Markdown の画像（![alt](url)）と <img> タグ。バッジなどの画像は候補リンクにしない。
# 外部の本文に使うので、文字の種類で探索を次の括弧・改行・< で止め、時間を本文の長さに比例させる
# （[^\]]* だと、閉じない ![ の繰り返しで二次的に遅くなる）
_IMAGES = re.compile(r"!\[[^\[\]\n]*\]\([^()\s]*\)|<img\b[^<>]*>", re.IGNORECASE)
# 参照形式の画像（![alt][label]）と、その定義の行（[label]: url）
_REF_IMAGE = re.compile(r"!\[[^\[\]\n]*\]\[([^\[\]\n]+)\]")
_REF_DEF = re.compile(r"^ {0,3}\[([^\[\]\n]+)\]:[^\n]*", re.MULTILINE)
# 絶対 URL。Markdown のリンク・自動リンク・裸の URL・HTML の href をまとめて拾う
_URL = re.compile(r"https?://[^\s<>()\[\]{}\"'`]+", re.IGNORECASE)
_TRAILING = ".,;:!?*_~"


def markdown_links(text: str | None) -> tuple[str, ...]:
    """Markdown の本文の絶対 URL を順に集める。相対リンクは集めない。

    本文に混ざる HTML の文字参照（&amp; など）は先に戻す。括弧を含む URL は括弧の前で切れる
    （Wikipedia の一部の記事など。候補リンクとしては受け入れる）。
    正規化と http(s) の絞り込みはまとめるときに行う（aggregate）。
    """
    if not text:
        return ()
    text = html.unescape(text)
    images = {label.casefold() for label in _REF_IMAGE.findall(text)}
    text = _REF_DEF.sub(lambda m: " " if m.group(1).casefold() in images else m.group(), text)
    text = _IMAGES.sub(" ", text)
    return tuple(m.group().rstrip(_TRAILING) for m in _URL.finditer(text))
