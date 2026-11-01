"""URL の正規化（data-model.md 2.8）。同じ先を指す URL の表記ゆれを1つにそろえる。

URL は外部の誰でも書けるので、ブラウザと解釈が分かれうるもの（\\・ユーザー情報・制御文字）や、
ホスト名に使えない文字を含むものは捨てる。パスとクエリの記号は % で符号化する。
Markdown の記号（( ) [ ] など）は、レポートに書くときにエスケープする。
"""

import re
from urllib.parse import quote, unquote_plus, urlsplit, urlunsplit

# 追跡用で、指している先を変えないクエリ。utm_ で始まるものも外す
_TRACKING_PARAMS = frozenset({"fbclid", "gclid"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_HOST = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)*")
_UNSAFE = re.compile(r"[\x00-\x20\x7f\\]")
# RFC 3986 でパスに書ける記号と、符号化済みを表す %
_PATH_SAFE = "/%:@!$&'()*+,;=-._~"


def normalize_url(url: str) -> str | None:
    """正規化した URL を返す。http・https 以外と、安全に扱えない URL は None。

    - http は https にそろえ、ホストは小文字・ASCII（IDNA）にし、末尾の . と既定のポートを外す
    - フラグメントと追跡用のクエリ（utm_*・fbclid・gclid）を外す。他のクエリはそのまま残す
    - パスの末尾の / を外す
    - github.com の owner・repo を小文字にし、末尾の .git を外す（GitHub は大小文字を区別しない）
    """
    url = url.strip()
    if _UNSAFE.search(url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or "@" in parts.netloc:
        return None
    host = _ascii_host(parts.hostname or "")
    if host is None:
        return None
    path = quote(parts.path, safe=_PATH_SAFE).rstrip("/")
    if host in ("github.com", "www.github.com"):
        host, path = "github.com", _github_repo_path(path)
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        host = f"{host}:{port}"
    return urlunsplit(("https", host, path, _strip_tracking(parts.query), ""))


def _ascii_host(host: str) -> str | None:
    try:
        host = host.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    return host if _HOST.fullmatch(host) else None


def _strip_tracking(query: str) -> str:
    pairs = [p for p in query.split("&") if p and not _tracking(p.split("=", 1)[0])]
    return quote("&".join(pairs), safe=_PATH_SAFE + "?")


def _tracking(key: str) -> bool:
    key = unquote_plus(key).lower()
    return key.startswith("utm_") or key in _TRACKING_PARAMS


def _github_repo_path(path: str) -> str:
    segments = path.split("/")  # 先頭は空文字
    if len(segments) == 3:
        segments[2] = segments[2].removesuffix(".git")
    return "/".join(s.lower() if i in (1, 2) else s for i, s in enumerate(segments))
