"""Issue の作成（GitHub への書き込み。⑤。structure.md 6.3・6.5、D-05・D-08・D-21）。

- トークン：Issue 用トークン（`GITHUB_ISSUE_TOKEN`）。値はログ・例外のメッセージに出さない
- 1回の呼び出しで要求は1回だけ出す。再試行は呼び出し側（publish/post.py）が行う。POST は、
  応答が届かなくても Issue が作られていることがあり、同じ要求を黙って繰り返すと重複するため、
  再試行の前に重複判定をやり直す必要がある（report-templates.md 4.3）
- ラベルは作成の要求そのものに含める。作った後に付けると、その間に落ちたときラベルで探せず重複する
- 利用量：1回の実行の書き込みの要求を limits.github_write_calls 回までにする（Issue 用・Projects
  用を合わせた上限。この実行の中で作った書き込みの回数を数える）。上限に達したら要求を出さずに
  失敗にする。1回の上限時間は HTTP の応答の上限（30 秒）
- リダイレクトはたどらない（認証のヘッダを別の先に送らない。転送されたリポジトリへの書き込みは
  失敗にする）
"""

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from urllib.parse import quote

from tech_researcher.collect.github_api import API_ROOT, API_VERSION, TOKEN_FORMAT, load_json
from tech_researcher.collect.http import (
    BUDGET,
    DNS,
    HTTP_5XX,
    INVALID_RESPONSE,
    NETWORK,
    RATE_LIMITED,
    REDIRECT,
    TIMEOUT,
    FetchError,
    Response,
    classify,
    fetch,
    time_limit,
)
from tech_researcher.config import GITHUB_REPO_NAME, Limits
from tech_researcher.publish.github_issues import IssueRef

TITLE_MAX = 256  # GitHub の Issue のタイトルの上限
_HTML_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)/issues/([0-9]+)")
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
# 再試行してよい失敗（structure.md 6.3）：タイムアウト・通信の失敗・5xx。レート制限は、待つ時間
# （Retry-After）が示されたときだけ（二次レート制限）
_TRANSIENT = frozenset({TIMEOUT, DNS, NETWORK, HTTP_5XX})


class IssueWriteError(Exception):
    """Issue を作れなかった。category は http.py の分類の語（budget・invalid_response を含む）。

    メッセージにはトークンを入れない。retry_after は応答の Retry-After の秒数（なければ None）。
    """

    def __init__(
        self,
        message: str,
        category: str,
        status: int | None = None,
        retry_after: float | None = None,
        unsent: bool = False,
    ):
        super().__init__(message)
        self.category = category
        self.status = status
        self.retry_after = retry_after
        # Issue が作られていないと分かる失敗（送る前に失敗した・GitHub が作らずに断った）。
        # 応答が返らない失敗は、Issue が作られていることがあるので False
        self.unsent = unsent

    @property
    def retryable(self) -> bool:
        if self.category in _TRANSIENT:
            return True
        return self.category == RATE_LIMITED and self.retry_after is not None


@dataclass(frozen=True)
class CreatedIssue:
    """作った Issue。missing_labels は、要求したのに応答のラベルにないもの（権限が足りないと、
    GitHub はラベルを黙って外す。ラベルがないと重複判定のラベル探索で見つからない）。"""

    ref: IssueRef
    missing_labels: tuple[str, ...] = ()


class IssueWriter:
    """Issue 用トークンで、Issue を作る。fetch_url・clock はテストで差し替える。"""

    def __init__(
        self,
        token: str,
        limits: Limits,
        *,
        fetch_url: Callable[..., Response] = fetch,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not TOKEN_FORMAT.fullmatch(token):
            raise IssueWriteError("トークンに使えない文字が含まれる", "auth")
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._limits = limits
        self._fetch = fetch_url
        self._clock = clock
        self.calls = 0  # この実行の書き込みの要求の回数
        # 要求を出してよい時間の上限（clock の値。⑤ の段階の上限。6.4）。None なら制限なし
        self.deadline: float | None = None

    def __repr__(self) -> str:  # トークンを repr に出さない
        return "IssueWriter()"

    def create_issue(
        self, repo: str, title: str, body: str, labels: tuple[str, ...]
    ) -> CreatedIssue:
        """要求を1回出して、作った Issue を返す。ラベルは要求に含める。"""
        if not GITHUB_REPO_NAME.fullmatch(repo):
            raise ValueError("投稿先が owner/repo の形でない")
        if not title or len(title) > TITLE_MAX:
            raise ValueError("タイトルの長さが使えない")
        if self.calls >= self._limits.github_write_calls:
            raise IssueWriteError("この実行の書き込みの上限に達した", BUDGET)
        path = "/".join(quote(part, safe="") for part in repo.split("/"))
        url = f"{API_ROOT}/repos/{path}/issues"
        data = json.dumps({"title": title, "body": body, "labels": list(labels)}).encode()
        try:
            response = self._fetch(
                url,
                replace(self._limits, http_retry_waits_s=()),  # 再試行は呼び出し側が行う
                headers=self._headers,
                follow_redirects=False,
                data=data,
                **time_limit(self.deadline, self._clock),
            )
        except FetchError as e:
            self.calls += e.attempts
            category = REDIRECT if e.status in _REDIRECTS else classify(e)
            raise IssueWriteError(
                f"{category}（HTTP {e.status}）",
                category,
                e.status,
                _retry_after(e.headers),
                unsent=_unsent(e, category),
            ) from e
        self.calls += response.attempts
        return _created(repo, labels, response)


def _unsent(error: FetchError, category: str) -> bool:
    """Issue が作られていないと言える失敗か。時間の上限のため送らなかった（attempts が 0）、名前解決
    の失敗、接続の拒否、レート制限の拒否（429・403。GitHub は作らずに断る）。応答の読み取りの
    タイムアウト・送った後の通信の失敗・5xx は、作られていることがあるので含めない。"""
    if error.attempts == 0 or category in (DNS, RATE_LIMITED):
        return True
    cause: BaseException | None = error
    for _ in range(8):  # 原因の鎖（FetchError → 再試行の印 → URLError → reason）をたどる
        if cause is None:
            break
        if isinstance(cause, ConnectionRefusedError) or isinstance(
            getattr(cause, "reason", None), ConnectionRefusedError
        ):
            return True
        cause = cause.__cause__
    return False


def _retry_after(headers: Mapping[str, str]) -> float | None:
    value = (headers.get("retry-after") or "").strip()
    return float(value) if re.fullmatch(r"[0-9]{1,6}", value) else None


def _created(repo: str, labels: tuple[str, ...], response: Response) -> CreatedIssue:
    """応答を確かめる。Issue は作られているので、読めなくても再試行しない（invalid_response）。"""
    try:
        data = load_json(response.body)
    except (ValueError, RecursionError) as e:
        raise IssueWriteError("応答が JSON でない", INVALID_RESPONSE, response.status) from e
    number = data.get("number") if isinstance(data, dict) else None
    found = _HTML_URL.fullmatch(str(data.get("html_url"))) if isinstance(data, dict) else None
    if (
        response.status != 201
        or type(number) is not int
        or number <= 0
        or found is None
        or int(found[2]) != number
        or found[1].lower() != repo.lower()
        or "pull_request" in data
    ):
        raise IssueWriteError("応答の形が違う", INVALID_RESPONSE, response.status)
    names = {
        label["name"].casefold()
        for label in data.get("labels") or []
        if isinstance(label, dict) and isinstance(label.get("name"), str)
    }
    missing = tuple(label for label in labels if label.casefold() not in names)
    return CreatedIssue(IssueRef(repo.lower(), number), missing)
