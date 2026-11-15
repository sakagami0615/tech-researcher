"""GitHub のリポジトリの Star 数の測定（D-13 の判定の流れの 2、data-model.md 3.4・3.7）。

候補（github_candidates）すべての累計の Star 数を GraphQL（`stargazerCount`）でまとめて取り、
日次の測定として star_snapshots に残す。

- まとめ方：1回の問い合わせで limits.github_graphql_batch 件（最大100）のリポジトリを別名
  （`r0`・`r1`…）で引く。リポジトリの名前は変数で渡し、問い合わせの文に埋め込まない
- 利用量：問い合わせは limits.github_graphql_calls 回まで（GitHubClient が数える）。上限に
  達したら、残りの候補は測らない（structure.md 6.5）
- 名前：GitHub が返す `nameWithOwner`（小文字）で残す。名前を変えたリポジトリは新しい名前に
  なり、古い名前と新しい名前の両方が候補なら1つにまとめる（3.5。名前の変更をまたいで同じもの
  とはみなさない）
- 見つからないリポジトリ（消えた・非公開になった・リポジトリでない URL）は missing、取れなかった
  リポジトリは unmeasured に分け、記録しない。どちらも前日比を判定できないだけになる
- 部分失敗：ある問い合わせが失敗しても、ほかの問い合わせの分は残す（error に最初の失敗）。
  認証・レート制限・この実行の上限の失敗では、残りの問い合わせを出さない
- 日次だけで測る。週次は日次の増分を合計するので測らない（3.4）。ドライランは書かない
- Star の履歴を取る API（Stargazers の一覧）は使わない（D-13）
"""

import logging
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from tech_researcher.collect import github_api
from tech_researcher.collect.github_api import GitHubClient, GitHubError
from tech_researcher.collect.github_candidates import Candidate
from tech_researcher.config import GITHUB_REPO_NAME
from tech_researcher.db.star_snapshots import (
    ROUTES,
    StarSnapshot,
    load_star_snapshot,
    record_star_snapshots,
)
from tech_researcher.periods import Target

log = logging.getLogger(__name__)

# 1件ごとに取る欄。Star の数・作成日時（新規の判定用）・名前の変更を知るための名前
_FIELDS = "nameWithOwner stargazerCount createdAt"
# GraphQL の誤りの種類（type）として、メッセージに入れてよい形
_ERROR_TYPE = re.compile(r"[A-Z_]{1,40}")
# Star 数の上限。GitHub の GraphQL の Int は32ビットなので、それを超える値は応答の崩れとみなす
_MAX_STARS = 2**31 - 1
# これらの失敗では、次の問い合わせも同じく失敗するのでやめる
_STOP_CATEGORIES = frozenset({github_api.AUTH, github_api.RATE_LIMITED, github_api.BUDGET})


@dataclass(frozen=True)
class StarResult:
    """測定の結果。snapshots はリポジトリの名前の順。

    同じ対象日の記録がすでにあれば（同じ対象の再実行・ドライラン）、その最初の測定を返す。
    mentioned_by は測ったリポジトリ → 言及元の項目の URL（候補の mentioned_by をまとめたもの）。
    missing は GitHub に見つからなかった候補、unmeasured は失敗で測れなかった候補（候補の名前）。
    calls は GraphQL の要求の回数（再試行を含む）。error・error_category は最初の失敗。
    """

    snapshots: tuple[StarSnapshot, ...]
    mentioned_by: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    missing: tuple[str, ...] = ()
    unmeasured: tuple[str, ...] = ()
    calls: int = 0
    error: str | None = None
    error_category: str | None = None


def measure_stars(
    candidates: Iterable[Candidate],
    client: GitHubClient | None,
    target: Target,
    conn: sqlite3.Connection | None,
    *,
    write: bool,
    batch_size: int,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> StarResult:
    """候補の Star 数を測り、日次なら star_snapshots に書く。

    週次では何もしない。write が False（ドライラン）か conn が None なら書かない。
    conn があり、対象日の記録に同じ名前の行がある候補（同じ対象の再実行・ドライラン）は、
    問い合わせずにその値を返す（最初の測定を残す。3.7）。測った結果の名前が記録にあるとき
    （名前の変更）も、記録の値を返す。
    client が None か認証なしなら要求を出さず、測っていない候補を unmeasured にして AUTH の
    失敗とする。batch_size は limits.github_graphql_batch。now は測定時刻にする時計。
    """
    if target.cadence != "daily":
        return StarResult(())
    candidates = list(candidates)
    stored = load_star_snapshot(conn, target.key) if conn is not None and candidates else {}
    pending = [c for c in candidates if c.repo not in stored]
    if not pending:
        result = StarResult(())
    elif client is None:
        error = GitHubError("収集用トークンがない", github_api.AUTH, 0)
        result = StarResult((), unmeasured=tuple(c.repo for c in pending), **_error(error))
    else:
        result = fetch_stars(client, pending, batch_size=batch_size, now=now)
    if conn is not None and write and result.snapshots:
        written = record_star_snapshots(conn, target.key, result.snapshots)
        log.info("Star 数：%d 件を記録した（測定 %d 件）", written, len(result.snapshots))
    snapshots = {s.repo: stored.get(s.repo, s) for s in result.snapshots}
    mentioned = {r: dict.fromkeys(m) for r, m in result.mentioned_by.items()}
    for c in candidates:
        if c.repo in stored:
            snapshots[c.repo] = stored[c.repo]
            mentioned.setdefault(c.repo, {}).update(dict.fromkeys(c.mentioned_by))
    return StarResult(
        tuple(snapshots[r] for r in sorted(snapshots)),
        {r: tuple(mentioned[r]) for r in sorted(mentioned) if mentioned[r]},
        result.missing,
        result.unmeasured,
        result.calls,
        result.error,
        result.error_category,
    )


def fetch_stars(
    client: GitHubClient,
    candidates: Sequence[Candidate],
    *,
    batch_size: int,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> StarResult:
    """候補を batch_size 件ずつ GraphQL で問い合わせて測る（DB には触れない）。"""
    if batch_size < 1:
        raise ValueError(f"batch_size は1以上にする: {batch_size}")
    before = client.graphql_calls
    measured: dict[str, StarSnapshot] = {}
    mentioned: dict[str, dict[str, None]] = {}
    missing: list[str] = []
    unmeasured: list[str] = []
    first_error: GitHubError | None = None
    for start in range(0, len(candidates), batch_size):
        batch = candidates[start : start + batch_size]
        if first_error is not None and first_error.category in _STOP_CATEGORIES:
            unmeasured += [c.repo for c in batch]
            continue
        try:
            data = client.graphql(*batch_query([c.repo for c in batch]))
            found, lost, failed = parse_batch(data, len(batch))
        except GitHubError as e:
            log.warning("GitHub GraphQL：%d 件を測れなかった（%s）", len(batch), e.category)
            first_error = first_error or e
            unmeasured += [c.repo for c in batch]
            continue
        at = now()
        missing += [batch[i].repo for i in lost]
        unmeasured += [batch[i].repo for i in failed]
        for i, (repo, stars, created_at) in found.items():
            candidate = batch[i]
            if repo != candidate.repo:
                log.info("GitHub GraphQL：%s は %s になっていた", candidate.repo, repo)
            routes = set(candidate.routes)
            if repo in measured:  # 名前の変更で、別の候補と同じリポジトリになった
                routes.update(measured[repo].routes)
            measured[repo] = StarSnapshot(
                repo, stars, created_at, at, tuple(r for r in ROUTES if r in routes)
            )
            mentioned.setdefault(repo, {}).update(dict.fromkeys(candidate.mentioned_by))
    if failed_count := len(unmeasured):
        log.warning("GitHub GraphQL：%d 件の Star 数を取れなかった", failed_count)
    if missing:
        log.info("GitHub GraphQL：%d 件のリポジトリが見つからなかった", len(missing))
    return StarResult(
        tuple(measured[r] for r in sorted(measured)),
        {r: tuple(mentioned[r]) for r in sorted(measured) if mentioned[r]},
        tuple(missing),
        tuple(unmeasured),
        client.graphql_calls - before,
        **_error(first_error),
    )


def _error(error: GitHubError | None) -> dict[str, str | None]:
    if error is None:
        return {}
    return {"error": str(error), "error_category": error.category}


def batch_query(repos: Sequence[str]) -> tuple[str, dict[str, str]]:
    """リポジトリ（`owner/repo`）をまとめて引く問い合わせと変数。i 番目の別名は `r{i}`。"""
    params: list[str] = []
    selections: list[str] = []
    variables: dict[str, str] = {}
    for i, repo in enumerate(repos):
        if not GITHUB_REPO_NAME.fullmatch(repo):
            raise ValueError(f"owner/repo の形でない: {repo!r}")
        owner, name = repo.split("/")
        params.append(f"$o{i}: String!, $n{i}: String!")
        selections.append(f"r{i}: repository(owner: $o{i}, name: $n{i}) {{ {_FIELDS} }}")
        variables[f"o{i}"] = owner
        variables[f"n{i}"] = name
    query = f"query({', '.join(params)}) {{ {' '.join(selections)} }}"
    return query, variables


def parse_batch(
    data: object, size: int
) -> tuple[dict[int, tuple[str, int, datetime]], list[int], list[int]]:
    """問い合わせの応答を（番号 → (名前, Star 数, 作成日時)、見つからない番号、失敗の番号）にする。

    番号はまとめた候補の中の順（別名 `r{番号}`）。1件の誤り（errors の path がその別名）は、
    種類が NOT_FOUND なら見つからない、ほかは失敗とする。どの別名にも当たらない誤り、
    RATE_LIMITED の誤り（別名に当たっても）、応答の形の崩れは、問い合わせ全体の失敗
    （GitHubError）とする。
    """
    if not isinstance(data, dict):
        raise _invalid("応答がオブジェクトでない")
    errors = data.get("errors") or []
    if not isinstance(errors, list):
        raise _invalid("errors が配列でない")
    aliases = {f"r{i}": i for i in range(size)}
    error_types: dict[int, str] = {}
    for error in errors:
        kind = error.get("type") if isinstance(error, dict) else None
        kind = kind if isinstance(kind, str) and _ERROR_TYPE.fullmatch(kind) else "UNKNOWN"
        path = error.get("path") if isinstance(error, dict) else None
        if kind != "RATE_LIMITED" and isinstance(path, list) and path:
            alias = path[0]
            if isinstance(alias, str) and alias in aliases:
                error_types.setdefault(aliases[alias], kind)
                continue
        category = github_api.RATE_LIMITED if kind == "RATE_LIMITED" else github_api.GRAPHQL
        raise GitHubError(f"GraphQL の誤り：{kind}", category, 0)
    nodes = data.get("data")
    if not isinstance(nodes, dict):
        raise _invalid("data がない")
    found: dict[int, tuple[str, int, datetime]] = {}
    missing: list[int] = []
    failed: list[int] = []
    for alias, i in aliases.items():
        node = nodes.get(alias)
        if i in error_types:
            (missing if error_types[i] == "NOT_FOUND" else failed).append(i)
        elif node is None:
            # 値が null で誤りもないのは見つからないとき。別名そのものがないのは応答の崩れ
            (missing if alias in nodes else failed).append(i)
        elif (parsed := _parse_node(node)) is None:
            failed.append(i)
        else:
            found[i] = parsed
    return found, missing, failed


def _parse_node(node: object) -> tuple[str, int, datetime] | None:
    """1件の（名前（小文字）, Star 数, 作成日時）。形が違えば None。"""
    if not isinstance(node, dict):
        return None
    name, stars, created = (
        node.get("nameWithOwner"),
        node.get("stargazerCount"),
        node.get("createdAt"),
    )
    # ASCII だけを認める（Unicode の小文字化で別の ASCII の名前に化けないように）
    if not isinstance(name, str) or not name.isascii():
        return None
    if not GITHUB_REPO_NAME.fullmatch(name.lower()):
        return None
    if not isinstance(stars, int) or isinstance(stars, bool) or not 0 <= stars <= _MAX_STARS:
        return None
    if not isinstance(created, str):
        return None
    try:
        created_at = datetime.fromisoformat(created)
        if created_at.tzinfo is None:
            return None
        created_at = created_at.astimezone(UTC)  # 範囲の外の日時はここで OverflowError
    except ValueError, OverflowError:
        return None
    return name.lower(), stars, created_at


def _invalid(reason: str) -> GitHubError:
    return GitHubError(f"GraphQL の応答として読めない：{reason}", github_api.INVALID_RESPONSE, 0)
