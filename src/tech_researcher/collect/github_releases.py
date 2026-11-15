"""GitHub Releases と README の一次情報の取得（D-11・D-14、data-model.md 2.3・2.6〜2.8）。

Releases は REST の `GET /repos/{owner}/{repo}/releases` で取る（releases.atom は robots.txt で
禁止されているので使わない。D-11）。README は `GET /repos/{owner}/{repo}/readme` を、
渡されたリポジトリ（急上昇・新規の検出が選んだもの）について取る。
本文（リリース本文・README）は primary_text にだけ入れ、抜粋にはしない。抜粋は DB に保存される
ため（D-18）。候補リンクは本文の Markdown から集める。

要求は github_api.GitHubClient で行う（トークン・リダイレクト・利用量の上限・エラーの分類）。
"""

import base64
import binascii
import html
import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from tech_researcher.collect import github_api
from tech_researcher.collect.github_api import GitHubClient, GitHubError
from tech_researcher.collect.records import Event, FailedPart, Record, SourceResult
from tech_researcher.collect.urls import normalize_url
from tech_researcher.config import GITHUB_REPO_NAME, GitHubReleases
from tech_researcher.periods import Period

log = logging.getLogger(__name__)

SOURCE_ID = "github_releases"
# 1ページの件数。100 件では本文の大きいリポジトリ（openai/codex）で HTTP 504 が続いた
# （2026-11-04）ので、既定の 30 件にしてページを送る
PER_PAGE = 30
MAX_PAGES = 5  # 1リポジトリあたりのページの上限。設定の誤りなどで要求を増やしすぎないように

# 応答の形が違うときの例外。GitHubError の INVALID_RESPONSE にまとめる（応答を読む所だけを囲む）
# OverflowError は範囲外の日時（0001-01-01T00:00+14:00 など）を UTC にしたとき
_SHAPE_ERRORS = (KeyError, TypeError, ValueError, RecursionError, OverflowError)
# meta_terms（D-07 の D4 で一致の判定から外す語句）に入れるタグ名の形。版の表記らしい ASCII で
# 50 字までに限る。長い語句や日本語のタグで、長い引用の検査を外せないようにする
_META_TAG = re.compile(r"[A-Za-z0-9._+\-/@]{1,50}")
# ページの上限に達し、期間内のリリースを取りこぼしたおそれがあるリポジトリの理由の分類
TRUNCATED = "truncated"
# この分類の失敗の後は、残りのリポジトリに要求を出さず、同じ分類で取れなかったものにする
# （トークンが無効・レート制限・利用量の上限は、続けても取れず、待ちと回数を使うだけのため。
# github_candidates の候補集めと同じ）
_STOP_CATEGORIES = frozenset({github_api.AUTH, github_api.RATE_LIMITED, github_api.BUDGET})


def collect_releases(
    releases: GitHubReleases,
    period: Period,
    client: GitHubClient,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> SourceResult:
    """追跡するリポジトリのリリースを、期間の始まりより前のものだけになるまでページを送って取る。

    期間の絞り込みは、ほかの情報源とまとめた後に period_filter で行う。
    下書き（draft）とプレリリースは除く。プレリリースは本文がほとんどない版が多いため
    （openai/codex の alpha など。2026-11-04 に確認）。
    取れなかったリポジトリは failed_parts に残し、取れた分は使う。すべてが取れなかったときだけ
    情報源を失敗にする（US-E6-03）。ページの上限に達したリポジトリは、取れた分を使ったうえで
    TRUNCATED として failed_parts に残す（取りこぼしたおそれを読者に伝えるため）。
    deadline は情報源ごとの時間の上限（clock の値。structure.md 6.4）。リポジトリの間とページの間で確かめ、
    過ぎた後のリポジトリには要求を出さず、timeout として取れなかったものにする。
    attempts はこの情報源で使った要求の回数（再試行を含む）。
    """
    before = client.rest_calls
    fetched_at = datetime.now(UTC)
    records: dict[str, Record] = {}
    failed: list[FailedPart] = []
    fetched = 0  # 取れたリポジトリの数（上限に達したものを含む）
    stopped: str | None = None  # 残りの要求をやめた分類
    for full_name in releases.repos:
        if stopped is None and deadline is not None and clock() >= deadline:
            log.warning("%s：情報源ごとの時間の上限に達した", SOURCE_ID)
            stopped = github_api.TIMEOUT
        if stopped is not None:
            failed.append(FailedPart(full_name, stopped))
            continue
        try:
            repo_records, complete = _repo_releases(
                full_name, period, client, fetched_at, deadline, clock
            )
        except GitHubError as e:
            log.warning("%s：%s が取れない（%s）", SOURCE_ID, full_name, e.category)
            failed.append(FailedPart(full_name, e.category))
            if e.category in _STOP_CATEGORIES:
                stopped = e.category
            continue
        fetched += 1
        if not complete:
            log.warning("%s：%s の期間内のリリースを取りこぼしたおそれ", SOURCE_ID, full_name)
            failed.append(FailedPart(full_name, TRUNCATED))
        for record in repo_records:
            records.setdefault(record.url, record)
    attempts = client.rest_calls - before
    if failed and not fetched:
        categories = sorted({f.error_category for f in failed})
        error = f"すべての追跡先が取れない（{'・'.join(categories)}）"
        return SourceResult(SOURCE_ID, (), attempts, error, tuple(failed))
    return SourceResult(SOURCE_ID, tuple(records.values()), attempts, None, tuple(failed))


def _repo_releases(
    full_name: str,
    period: Period,
    client: GitHubClient,
    fetched_at: datetime,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[Record], bool]:
    """1つのリポジトリのリリースを、新しい順のページで期間の始まりまで取る。

    （レコード、期間の始まりまで届いたか）を返す。MAX_PAGES で届かなければ False。

    一覧が公開日時の順に並ぶ保証はない（公式の文書に書かれていない）ので、ページの中の
    すべてが期間の始まりより前になったときだけ止める（期間内のリリースがあれば2ページを取る）。
    ページをまたいだ並びの乱れ（前に作った下書きを後で公開したものが後ろのページにある、など）は
    追わない。まれで、追うにはすべてのページを取る必要があるため。
    下書き・プレリリースも件数に加わるので、プレリリースの多いリポジトリはページが増える。
    応答の形が違えば GitHubError（INVALID_RESPONSE）。途中のページで失敗したときや、1件の
    リリースの形が違うときも、取れたページの分を含めてそのリポジトリを取れなかったものにする
    （応答の異常を見逃さないため。取りこぼしは「取得できなかった情報源」に出る）。
    """
    records = []
    for page in range(1, MAX_PAGES + 1):
        if deadline is not None and clock() >= deadline:  # ページの間でも時間の上限を確かめる
            raise GitHubError(f"{full_name}：時間の上限に達した", github_api.TIMEOUT, 0)
        data = client.get(f"/repos/{full_name}/releases", {"per_page": PER_PAGE, "page": page})
        try:
            if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
                raise TypeError("オブジェクトの配列でない")
            records += [r for r in (_release(full_name, x, fetched_at) for x in data) if r]
            dates = [_utc(x["published_at"]) for x in data if x.get("published_at")]
        except _SHAPE_ERRORS as e:
            message = f"{full_name}：Releases API の応答として読めない：{type(e).__name__}"
            raise GitHubError(message, github_api.INVALID_RESPONSE, 0) from e
        if len(data) < PER_PAGE or (dates and max(dates) < period.start):
            return records, True
    return records, False


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
    name = release.get("name")
    if name is not None and not isinstance(name, str):
        raise TypeError("name が文字列でない")
    body = release.get("body")
    if body is not None and not isinstance(body, str):
        raise TypeError("body が文字列でない")
    if body is not None and not body.strip():
        body = None  # 空白だけの本文は本文なし（README と同じ）
    return Record(
        source_id=SOURCE_ID,
        url=url,
        title=name if name and name.strip() else tag,
        published_precision="datetime",
        fetched_at=fetched_at,
        events=(Event("released", published_at),),
        primary_text=body,
        primary_text_origin="github_release" if body else None,
        published_at=published_at,
        links=markdown_links(body),
        meta_terms=(_display_repo(full_name, url), *([tag] if _META_TAG.fullmatch(tag) else [])),
    )


def _display_repo(full_name: str, html_url: str) -> str:
    """meta_terms に入れるリポジトリ名。html_url の名前が同じ（大文字・小文字だけの違い）なら使う。

    設定の名前は小文字にそろえてあるので、本文に出る元の表記（microsoft/TypeScript など）を残す。
    """
    shown = "/".join(html_url.removeprefix("https://github.com/").split("/")[:2])
    # ASCII に限る（ケルビン記号の K なども lower() で k になるため）
    return shown if shown.isascii() and shown.lower() == full_name else full_name


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

    キーは `owner/repo`（小文字。形を確かめた名前だけ）。errors は失敗の理由の分類
    （github_api の語）。attempts は使った要求の回数（再試行を含む）。
    invalid_names は `owner/repo` の形でなかった名前の数。名前は外部の URL から抜き出したもので
    ありうるので、外部の文をコードの値に混ぜないよう、名前そのものは残さない（US-E7-06）。
    """

    readmes: dict[str, Readme]
    errors: dict[str, str]
    attempts: int
    invalid_names: int = 0


def fetch_readmes(
    full_names: Iterable[str],
    client: GitHubClient,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ReadmeResult:
    """渡されたリポジトリ（`owner/repo`）の README を取る。

    1件の失敗では止めず、続きを取る（README は項目の補足で、なくても項目は載せられるため）。
    README がない（HTTP 404）ものは失敗にしない。名前は外部の URL から抜き出したものでありうる
    ので、`owner/repo` の形（ASCII）でなければ要求せず、数だけを数える。GitHub の名前は
    大文字・小文字を区別しないので、小文字にそろえて重なりを除く。
    1MB を超える README は API が本文を返さない（encoding が none）ので、README がないものと
    同じく失敗にしない。
    deadline は時間の上限（clock の値）。過ぎた後のリポジトリには要求を出さず、timeout にする。
    """
    before = client.rest_calls
    readmes: dict[str, Readme] = {}
    errors: dict[str, str] = {}
    seen: set[str] = set()
    invalid = 0
    stopped: str | None = None  # 残りの要求をやめた分類（collect_releases と同じ）
    for name in full_names:
        # 小文字にする前に ASCII に限る（ケルビン記号 K が k になるなど、別の名前に化けるため）
        if not isinstance(name, str) or not name.isascii():
            invalid += 1
            continue
        full_name = name.lower()
        if not GITHUB_REPO_NAME.fullmatch(full_name):
            invalid += 1
            continue
        if full_name in seen:
            continue
        seen.add(full_name)
        if stopped is None and deadline is not None and clock() >= deadline:
            stopped = github_api.TIMEOUT
        if stopped is not None:
            errors[full_name] = stopped
            continue
        try:
            data = client.get(f"/repos/{full_name}/readme")
        except GitHubError as e:
            if e.category != github_api.NOT_FOUND:
                log.warning("%s：%s の README が取れない（%s）", SOURCE_ID, full_name, e.category)
                errors[full_name] = e.category
            if e.category in _STOP_CATEGORIES:
                stopped = e.category
            continue
        try:
            text = _readme_text(full_name, data)
            links = markdown_links(text) if text is not None else ()
        except _SHAPE_ERRORS:
            log.warning("%s：%s の README を読めない", SOURCE_ID, full_name)
            errors[full_name] = github_api.INVALID_RESPONSE
            continue
        if text is not None and text.strip():
            readmes[full_name] = Readme(full_name, text, links)
    return ReadmeResult(readmes, errors, client.rest_calls - before, invalid)


def _readme_text(full_name: str, data) -> str | None:
    """README の API の応答（base64 の content）を文字列にする。形が違えば ValueError など。

    1MB を超えて本文が返らないもの（encoding が none）は None。
    """
    if data["encoding"] == "none":
        log.info("%s：%s の README は 1MB を超え、本文が返らない", SOURCE_ID, full_name)
        return None
    if data["encoding"] != "base64" or not isinstance(data["content"], str):
        raise ValueError("encoding が base64 でない")
    try:
        # GitHub は 60 字ごとに改行を入れる。改行を除いたうえで、base64 以外の文字を拒む
        raw = base64.b64decode(re.sub(r"[\r\n]", "", data["content"]), validate=True)
    except binascii.Error as e:
        raise ValueError("content を base64 として読めない") from e
    # ASCII（改行など）を含む UTF-16・UTF-32 とバイナリは NUL を含む。BOM で始まるものも止める
    if b"\x00" in raw or raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValueError("UTF-16・UTF-32・バイナリの README は読まない")
    return raw.decode("utf-8-sig", errors="replace")  # 先頭の BOM は除く


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


# Markdown の画像（![alt](url "title")）と <img>・<source>（<picture> の中）のタグ。
# バッジなどの画像は候補リンクにしない。外部の本文に使うので、文字の種類で探索を次の括弧・改行・
# < で止め、時間を本文の長さに比例させる（[^\]]* だと、閉じない ![ の繰り返しで二次的に遅くなる）
_IMAGES = re.compile(r"!\[[^\[\]\n]*\]\([^()\n]*\)|<(?:img|source)\b[^<>]*>", re.IGNORECASE)
# 参照形式の画像（![alt][label]・![alt][]・![label]）。その定義の行（[label]: url）を除く
_REF_IMAGE = re.compile(r"!\[([^\[\]\n]+)\](?:\[([^\[\]\n]*)\])?(?!\()")
_REF_DEF = re.compile(r"^ {0,3}\[([^\[\]\n]+)\]:[^\n]*", re.MULTILINE)
# 絶対 URL。Markdown のリンク・自動リンク・裸の URL・HTML の href をまとめて拾う。
# URL は ASCII の印字できる文字だけとし、< > ( ) [ ] { } " ' ` で止める。日本語の文の句読点・
# 全角の括弧（「https://…。次に」など）を含めると、IDNA が「。」を「.」として扱い、別のホストに
# なりうるため。ホスト名を IDN のまま書いたものは切れるが、候補リンクとしては受け入れる
# IGNORECASE は Unicode の対応でケルビン記号なども [a-z] に一致させるので、スキームにだけ付ける
_URL = re.compile(r"(?i:https?)://[!#-&*-;=?-Z\\^_a-z|~]+")
_TRAILING = ".,;:!?*_~"
# 1つの本文から集める候補リンクの数の上限（正規化した先で重なりを除いた先頭から）と、1つの
# URL の長さの上限（超えるものは切り詰めずに候補から外す。切ると別の先を指しうるため）。
# 外部の本文1つで、DB の links_json や ② の入力を大きくできないようにする（data-model.md 2.8）
MAX_LINKS = 100
MAX_URL_LENGTH = 2048
# 10 進の数値文字参照で、有効な符号位置（7桁まで）より長いもの。html.unescape は桁数の上限なく
# int() にかけ、4300 桁を超えると ValueError になるので、先に置き換える
_LONG_DECIMAL_REF = re.compile(r"&#0*[0-9]{8,};?")


def markdown_links(text: str | None) -> tuple[str, ...]:
    """Markdown の本文の絶対 URL を順に、正規化した先の重なりを除いて先頭の MAX_LINKS 件まで
    集める。相対リンクと、正規化できない URL・MAX_URL_LENGTH より長い URL は集めない。

    本文に混ざる HTML の文字参照（&amp; など）は先に戻す。括弧を含む URL は括弧の前で切れる
    （Wikipedia の一部の記事など。候補リンクとしては受け入れる）。
    正規化と http(s) の絞り込みはまとめるときに行う（aggregate）。
    """
    if not text:
        return ()
    text = html.unescape(_LONG_DECIMAL_REF.sub("\ufffd", _drop_comments(text)))
    images = {(label or alt).casefold() for alt, label in _REF_IMAGE.findall(text)}
    text = _REF_DEF.sub(lambda m: " " if m.group(1).casefold() in images else m.group(), text)
    text = _IMAGES.sub(" ", text)
    links: dict[str, str] = {}  # 正規化した URL → 本文の URL
    for m in _URL.finditer(text):
        url = m.group().rstrip(_TRAILING)
        key = normalize_url(url) if len(url) <= MAX_URL_LENGTH else None
        if key is not None and key not in links:
            links[key] = url
            if len(links) >= MAX_LINKS:
                break
    return tuple(links.values())


def _drop_comments(text: str) -> str:
    """HTML のコメント（<!-- … -->。読者に見えない）を除く。閉じないコメントは末尾まで除く。

    正規表現の .*? は閉じない <!-- の繰り返しで二次的に遅くなるので、find で1回ずつ進める。
    """
    parts = []
    position = 0
    while (start := text.find("<!--", position)) != -1:
        parts.append(text[position:start])
        # <!--> と <!---> は空のコメントとして閉じる（HTML の仕様と同じ）
        for empty in ("<!-->", "<!--->"):
            if text.startswith(empty, start):
                position = start + len(empty)
                break
        else:
            end = text.find("-->", start + 4)
            if end == -1:
                return "".join(parts)
            position = end + 3
    parts.append(text[position:])
    return "".join(parts)
