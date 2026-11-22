"""Issue の Projects への登録（⑤。D-05・D-20・D-21、structure.md 6.3・6.5、R 4.4）。

- 認証：Projects 用トークン（classic。`project` スコープだけ）を worker の環境変数
  `GITHUB_PROJECTS_TOKEN` から読む。Projects の指定は、同じ worker.env の `GITHUB_PROJECTS_OWNER`
  （ユーザー名）と `GITHUB_PROJECTS_NUMBER`（Projects の番号）。トークンと指定は組で読み、
  1つでも欠ければ設定のエラーにする。値はログ・例外のメッセージに出さない
- 方式：REST の `POST /users/{owner}/projectsV2/{number}/items` に、Issue の owner・repo・number を
  渡して追加する（D-05 の文書どおり。実機の確認は #7 で行う）。Issue の本文・状態・ラベル・
  コメントや、Projects のステータスなどの項目には触れない（US-E4-04）。追加の要求だけを出す
- 再試行：http.fetch に任せる（タイムアウト・通信の失敗・429・5xx。待ちは 5 秒・20 秒）。同じ
  Issue を追加し直しても Projects の項目は増えない前提（#7 で確かめる）なので、要求をそのまま
  繰り返してよい。回数は Issue の作成と共有する WriteBudget の残りに収める
- リダイレクトはたどらない（認証のヘッダを別の先に送らない）
"""

import json
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from urllib.parse import quote

from tech_researcher.collect.github_api import API_ROOT, API_VERSION, TOKEN_FORMAT, load_json
from tech_researcher.collect.http import (
    BUDGET,
    INVALID_RESPONSE,
    REDIRECT,
    FetchError,
    Response,
    classify,
    fetch,
    time_limit,
)
from tech_researcher.config import GITHUB_REPO_NAME, Limits
from tech_researcher.publish.github_issues import IssueRef
from tech_researcher.publish.github_write import WriteBudget

TOKEN_ENV = "GITHUB_PROJECTS_TOKEN"
OWNER_ENV = "GITHUB_PROJECTS_OWNER"
NUMBER_ENV = "GITHUB_PROJECTS_NUMBER"
CONFIG = "config"  # 設定の誤り（分類の語）
_OWNER = re.compile(r"[A-Za-z0-9](?:-?[A-Za-z0-9]){0,38}")  # GitHub のユーザー名
_NUMBER = re.compile(r"[0-9]{1,9}")
_REDIRECTS = frozenset({301, 302, 303, 307, 308})


class ProjectsError(Exception):
    """Projects に登録できなかった（記録して次の実行に回す）。category は http.py の分類の語
    （budget・invalid_response・config を含む）。メッセージにはトークンを入れない。

    status が 404・410 のときは、Issue が見つからないのか、Projects・権限の問題なのかを
    応答からは区別していない（#7 で確かめる）。
    """

    def __init__(self, message: str, category: str, status: int | None = None):
        super().__init__(message)
        self.category = category
        self.status = status


@dataclass(frozen=True)
class ProjectsTarget:
    """登録先の Projects（個人のもの）。"""

    owner: str
    number: int


@dataclass(frozen=True)
class ProjectsConfig:
    token: str
    target: ProjectsTarget

    def __repr__(self) -> str:  # トークンを repr に出さない
        return f"ProjectsConfig(target={self.target!r})"


def projects_config(environ: Mapping[str, str] = os.environ) -> ProjectsConfig:
    """worker.env から Projects のトークンと指定を組で読む。欠けていれば ProjectsError。"""
    names = (TOKEN_ENV, OWNER_ENV, NUMBER_ENV)
    values = {name: (environ.get(name) or "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ProjectsError(f"{'・'.join(missing)} が設定されていない", CONFIG)
    if not TOKEN_FORMAT.fullmatch(values[TOKEN_ENV]):
        raise ProjectsError(f"{TOKEN_ENV} に使えない文字が含まれる", CONFIG)
    if not _OWNER.fullmatch(values[OWNER_ENV]):
        raise ProjectsError(f"{OWNER_ENV} がユーザー名の形でない", CONFIG)
    if not _NUMBER.fullmatch(values[NUMBER_ENV]) or int(values[NUMBER_ENV]) <= 0:
        raise ProjectsError(f"{NUMBER_ENV} が正の整数でない", CONFIG)
    return ProjectsConfig(
        values[TOKEN_ENV], ProjectsTarget(values[OWNER_ENV], int(values[NUMBER_ENV]))
    )


class ProjectsRegistrar:
    """Projects 用トークンで、Issue を Projects に追加する。fetch_url・clock は差し替え可。"""

    def __init__(
        self,
        config: ProjectsConfig,
        limits: Limits,
        budget: WriteBudget,
        *,
        fetch_url: Callable[..., Response] = fetch,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "Authorization": f"Bearer {config.token}",
            "Content-Type": "application/json",
        }
        self._url = (
            f"{API_ROOT}/users/{quote(config.target.owner, safe='')}"
            f"/projectsV2/{config.target.number}/items"
        )
        self._limits = limits
        self._budget = budget
        self._fetch = fetch_url
        self._clock = clock
        # 要求を出してよい時間の上限（clock の値。⑤ の段階の上限。6.4）。None なら制限なし
        self.deadline: float | None = None

    def __repr__(self) -> str:  # トークンを repr に出さない
        return "ProjectsRegistrar()"

    def add_issue(self, ref: IssueRef) -> None:
        """Issue を Projects に追加する。要求は再試行を含めて budget の残りまで。"""
        if not GITHUB_REPO_NAME.fullmatch(ref.repo) or ref.number <= 0:
            raise ValueError("Issue の指定が使えない")
        if self._budget.left <= 0:
            raise ProjectsError("この実行の書き込みの上限に達した", BUDGET)
        owner, _, repo = ref.repo.partition("/")
        # 再試行は、書き込みの残りの回数に収まる分だけ（初回の分を引く）
        waits = self._limits.http_retry_waits_s[: self._budget.left - 1]
        data = json.dumps(
            {"type": "Issue", "owner": owner, "repo": repo, "number": ref.number}
        ).encode()
        try:
            response = self._fetch(
                self._url,
                replace(self._limits, http_retry_waits_s=waits),
                headers=self._headers,
                follow_redirects=False,
                data=data,
                **time_limit(self.deadline, self._clock),
            )
        except FetchError as e:
            self._budget.used += e.attempts
            category = REDIRECT if e.status in _REDIRECTS else classify(e)
            raise ProjectsError(f"{category}（HTTP {e.status}）", category, e.status) from e
        self._budget.used += response.attempts
        _check(response)


def _check(response: Response) -> None:
    """応答が追加の成功（200・201 で項目の id がある）かを確かめる。"""
    try:
        data = load_json(response.body)
    except (ValueError, RecursionError) as e:
        raise ProjectsError("応答が JSON でない", INVALID_RESPONSE, response.status) from e
    if (
        response.status not in (200, 201)
        or not isinstance(data, dict)
        or type(data.get("id")) is not int
    ):
        raise ProjectsError("応答の形が違う", INVALID_RESPONSE, response.status)
