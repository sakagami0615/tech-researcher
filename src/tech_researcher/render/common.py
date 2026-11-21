"""日次・週次で共通の本文の部品（report-templates.md 0.3・0.4・4.1）。

出典はコードが ID から URL を引いて書く（D-06）。リンク文字列は設定の情報源名とドメインだけにし、
外部の記事のタイトルは使わない（0.3）。
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date
from urllib.parse import urlsplit

from tech_researcher.agent.daily import display_name
from tech_researcher.collect.aggregate import candidate_links
from tech_researcher.collect.records import Item, Record
from tech_researcher.collect.urls import normalize_url
from tech_researcher.periods import JST, Target
from tech_researcher.render.sanitize import BIDI

_WEEKDAYS = "月火水木金土日"
_ID_VALUE = re.compile(r"[A-Za-z0-9]+(-[A-Za-z0-9]+)*")  # HTML コメントの中に -- を作らない（4.1）
# GitHub の owner は英数字と -、repo は英数字と . _ -（0.3）
_REPO = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")
# リンク先の中で Markdown のリンクを閉じうる記号（正規化した URL に残るのは ( ) だけ）と、&。
# GFM はリンク先の実体参照（&num; など）を復号するので、& を &amp; にして行き先を変えさせない
_LINK_DEST = str.maketrans({"(": "%28", ")": "%29", "&": "&amp;"})
_SPACES = re.compile(r"\s+")
_MARKDOWN = re.compile(r"([\\`*_{}\[\]()<>#+\-.!|~&:])")
SEPARATOR = " ／ "


# 取得の失敗の理由の分類（source_fetches の error_category）の日本語の表記。一覧にない分類
# （例外の型の名前など）は、名前を本文に出さず「取得に失敗」にする
FETCH_REASONS = {
    "timeout": "タイムアウト",
    "auth": "認証エラー",
    "rate_limited": "レート制限",
    "forbidden": "アクセス拒否（HTTP 403）",
    "not_found": "見つからない（HTTP 404）",
    "http_4xx": "HTTP 4xx",
    "http_5xx": "HTTP 5xx",
    "dns": "名前解決の失敗",
    "network": "接続の失敗",
    "budget": "利用量の上限",
    "redirect": "リダイレクトの異常",
    "invalid_response": "応答の形の異常",
    "graphql_error": "GraphQL のエラー",
    "exit_code": "実行の失敗（終了コード）",
    "truncated": "取りこぼしたおそれ",
}
FETCH_FAILED_TEXT = "取得に失敗"


def fetch_reason(category: str | None) -> str:
    """取得の失敗の理由の分類を、本文に書く日本語にする。"""
    return FETCH_REASONS.get(category or "", FETCH_FAILED_TEXT)


@dataclass(frozen=True)
class Report:
    """投稿する Issue の中身。labels は D-08（4.2）。"""

    title: str
    body: str
    labels: tuple[str, ...]


BODY_LIMIT = 60_000  # GitHub の Issue 本文の上限（report-templates.md 0.3）


def body_too_long(body: str) -> bool:
    """本文が上限を超えているか（超えたら投稿せず、不合格などの定型にする）。"""
    return len(body) > BODY_LIMIT


def day_label(day: date) -> str:
    """`YYYY-MM-DD（曜）`（0.4）。"""
    return f"{day.isoformat()}（{_WEEKDAYS[day.weekday()]}）"


def period_text(target: Target) -> str:
    """`YYYY-MM-DD（曜）HH:MM 〜 YYYY-MM-DD（曜）HH:MM（JST）`（0.4）。"""
    start, end = (t.astimezone(JST) for t in (target.period.start, target.period.end))
    return f"{day_label(start.date())}{start:%H:%M} 〜 {day_label(end.date())}{end:%H:%M}（JST）"


def run_id_code(run_id: str) -> str:
    """実行ID を表のセルに入れる形（インラインコード）にする。英数字と - だけを許す。"""
    if not _ID_VALUE.fullmatch(run_id):
        raise ValueError(f"実行ID に使えない値 {run_id!r}")
    return f"`{run_id}`"


def identifier(cadence: str, target: date, run_id: str) -> str:
    """本文の1行目に置く重複防止用の識別子（4.1）。"""
    for value in (cadence, run_id):
        if not _ID_VALUE.fullmatch(value):
            raise ValueError(f"識別子に使えない値 {value!r}")
    return (
        f"<!-- tech-researcher kind=report cadence={cadence} target={target.isoformat()} "
        f"run={run_id} -->"
    )


def plain_text(text: str) -> str:
    """コードが本文に差し込む名前（情報源の表示名など）を、ただの文字列として表示させる。

    設定由来の値だが、多重防御として Markdown・HTML の記号をエスケープし、改行は空白に畳む。
    @ と # は全角にする。GitHub はメンション（@名前）と参照（#数字）を描画後の文字列から探すので、
    バックスラッシュでエスケープしても @・# として残り、通知・リンクが起きるため。
    """
    text = _SPACES.sub(" ", BIDI.sub("", text)).strip().replace("@", "＠").replace("#", "＃")
    return _MARKDOWN.sub(r"\\\1", text)


def repo_code(full_name: str) -> str:
    """`owner/repo` の形だけを許し、インラインコードにする（0.3）。"""
    if not _REPO.fullmatch(full_name) or full_name.split("/")[1] in (".", ".."):
        raise ValueError(f"owner/repo の形でない {full_name!r}")
    return f"`{full_name}`"


@dataclass(frozen=True)
class _Source:
    url: str
    name: str | None  # 情報源名。候補リンクなど、情報源のレコードがない URL は None
    date: str | None


def source_line(
    item_ids: Sequence[str],
    items: Mapping[str, Item],
    names: Mapping[str, str],
    primary_item_id: str | None = None,
    primary_link_id: str | None = None,
) -> str:
    """出典の並び（1.3）。一次情報を先頭に印を付けて置き、残りは item_ids の順に並べる。

    - 一次情報は primary_link_id（候補リンク）を、なければ primary_item_id の項目の URL を使う
    - その URL の収集レコードがあれば、そのレコードを先頭に移す。なければドメインだけで書く
    - names は source_id から表示名への対応。同じ URL の出典は1つにする
    """
    sources: dict[str, _Source] = {}
    for item_id in item_ids:
        item = items[item_id]
        own = [s for s in (_from_record(r, names) for r in item.sources) if s is not None]
        if item.url not in {s.url for s in own}:
            # 集約型（HN など）だけが指す記事は、記事そのものの URL をレコードの前に置く
            own.insert(0, _Source(item.url, None, None))
        for source in own:
            sources.setdefault(source.url, source)
    primary = _primary_url(items, primary_item_id, primary_link_id)
    marked = []
    if primary is not None:
        first = sources.pop(primary, None) or _Source(primary, None, None)
        marked = [f"一次情報 {_format(first)}"]
    parts = marked + [_format(s) for s in sources.values()]
    if not parts:
        raise ValueError(f"{list(item_ids)} の出典がない")
    return SEPARATOR.join(parts)


def _primary_url(items: Mapping[str, Item], item_id: str | None, link_id: str | None) -> str | None:
    # 両方あれば候補リンクを優先する（data-model.md 4.2 の初期案）。D6(b) に合わせて他項目の
    # 候補リンクも引ける。自項目の出典に限ることは D1（validate/sources.py）で検査する
    if link_id is not None:
        links = {
            link.link_id: link.url for i, it in items.items() for link in candidate_links(it, i)
        }
        return links[link_id]
    return None if item_id is None else items[item_id].url


def _from_record(record: Record, names: Mapping[str, str]) -> _Source | None:
    """URL を正規化できないレコードは、安全にリンクにできないので出典に書かない。"""
    url = normalize_url(record.url)
    if url is None:
        return None
    return _Source(url, display_name(names, record), _date(record))


def _date(record: Record) -> str | None:
    """公開日、なければ投稿日（0.4）。日付だけの公開は UTC の日付（時刻がないため）。"""
    if record.published_at is not None:
        zone = UTC if record.published_precision == "date" else JST
        return f"公開 {record.published_at.astimezone(zone).date().isoformat()}"
    if record.posted_at is not None:
        return f"投稿 {record.posted_at.astimezone(JST).date().isoformat()}"
    return None


def _format(source: _Source) -> str:
    domain = urlsplit(source.url).hostname
    text = domain if source.name is None else f"{plain_text(source.name)}（{domain}）"
    link = f"[{text}]({source.url.translate(_LINK_DEST)})"
    return link if source.date is None else f"{link} {source.date}"
