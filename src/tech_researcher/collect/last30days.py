"""last30days-skill のアダプタ（D-01・D-02・D-16・D-21、structure.md 5.3・6.3、data-model.md 2.8）。

HN・GitHub Search を last30days-skill の CLI で集め、agent JSON（schema_version 1.4）を
収集レコードにする。第三者コードは import せず、子プロセスとして呼ぶ（D-02）。

- 計画（--plan）・情報源（--search）・Web 検索の backend（none）を常に明示する
- 子プロセスには許可した環境変数だけを渡す。推論プロバイダのキーや gh のログイン情報は渡さない。
  HOME は実行ごとの空の一時ディレクトリにし、gh の設定や library.db に届かないようにする
- 比較形式（{"comparison": true}）と、版や必須構造の違う出力は拒否し、再試行しない
- 終了コード 3（一部の情報源の失敗）は、状態が ok・no-results の情報源の結果だけを使う
- 1回の上限時間と再試行の回数は設定の limits（300 秒・1回）。終了コード 2 は再試行しない
- 計画（last30days.subqueries）で使わない情報源と、収集用トークンがないときの GitHub Search は、
  CLI に渡さずにその情報源の失敗とする（D-01 は収集用トークンを渡す前提）

agent JSON から収集レコードへの対応：

| agent JSON | 収集レコード |
|---|---|
| `results[].source`（hackernews・github） | `source_id`（hackernews・github_search） |
| `results[].url` | `url`（HN は記事の URL。HN の item ページは agent JSON にない） |
| `results[].title` | `title` |
| `results[].summary`（タイトルと同じなら捨てる） | `excerpt` |
| `results[].published_at`（日付） | `published_at`（UTC 0:00、精度 date）・出来事 published |
| `results[].engagement` の数値 | `attention`（metric は `hn_points`・`github_reactions` など） |
| GitHub の URL の `owner/repo` | `meta_terms` |
"""

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from pathlib import Path

from tech_researcher.collect.records import Attention, Event, Record, SourceResult
from tech_researcher.config import Config, Limits, Subquery
from tech_researcher.periods import Period, last30days_window

log = logging.getLogger(__name__)

SCHEMA_VERSION = "1.4"
SCRIPT = "skills/last30days/scripts/last30days.py"
# トピックは固定する。「A vs B」の形だと比較モードに入るため、比較対象を含めない（D-01）
TOPIC = "technology news"
# 計画の固定の値（D-01。値は固定の文字列でよい）
PLAN_HEADER = {"intent": "breaking_news", "freshness_mode": "strict_recent", "cluster_mode": "none"}
# 設定の情報源 ID → last30days-skill の情報源名
SOURCE_NAMES = {"hackernews": "hackernews", "github_search": "github"}
# 注目度の metric の接頭辞（data-model.md 2.4）
METRIC_PREFIXES = {"hackernews": "hn", "github": "github"}
OK_STATES = frozenset({"ok", "no-results"})
EXIT_PARTIAL = 3  # LAST30DAYS_STRICT_EXIT=1 で、一部の情報源が失敗した
EXIT_USAGE = 2  # 引数・設定の誤り。再試行しても直らない
STDERR_TAIL_CHARS = 2000  # 失敗の原因の分類に使う stderr の末尾（D-01）

_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_GITHUB_REPO = re.compile(r"https://github\.com/([^/?#]+/[^/?#]+)")


class ContractError(ValueError):
    """agent JSON が契約（schema_version 1.4 の必須構造）に合わない。"""


class _CliError(Exception):
    def __init__(self, message: str, attempts: int):
        super().__init__(message)
        self.attempts = attempts


Runner = Callable[..., subprocess.CompletedProcess]


def collect_last30days(
    config: Config,
    period: Period,
    root: Path,
    *,
    github_token: str | None = None,
    run: Runner = subprocess.run,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[SourceResult]:
    """有効な情報源（hackernews・github_search）を1回の実行で集め、情報源ごとの結果を返す。

    root は last30days-skill を置いたディレクトリ（third_party/last30days）。
    github_token は収集用トークンで、子プロセスに GITHUB_TOKEN として渡す（D-05）。
    """
    enabled = [
        sid
        for sid, source in (
            ("hackernews", config.hackernews),
            ("github_search", config.github_search),
        )
        if source is not None and source.enabled
    ]
    results: dict[str, SourceResult] = {}
    for sid in enabled:
        if not any(SOURCE_NAMES[sid] in s.sources for s in config.subqueries):
            error = f"last30days.subqueries に {SOURCE_NAMES[sid]} を使う計画がない"
            results[sid] = SourceResult(sid, (), 0, error)
        elif sid == "github_search" and not github_token:
            results[sid] = SourceResult(sid, (), 0, "収集用トークンがない")
    source_ids = [sid for sid in enabled if sid not in results]
    if source_ids:
        results.update(_collect(config, period, root, source_ids, github_token, run, now))
    return [results[sid] for sid in enabled]


def _collect(
    config: Config,
    period: Period,
    root: Path,
    source_ids: list[str],
    github_token: str | None,
    run: Runner,
    now: Callable[[], datetime],
) -> dict[str, SourceResult]:
    """source_ids を1回の CLI の実行で集める。"""
    names = [SOURCE_NAMES[sid] for sid in source_ids]
    args = build_args(root, build_plan(config.subqueries, names), names, period)
    with tempfile.TemporaryDirectory(prefix="last30days-") as home:
        try:
            stdout, stderr, attempts = _run(
                args, child_env(home, github_token), home, config.limits, run
            )
            data = parse_agent_json(stdout)
        except _CliError as e:
            return {sid: SourceResult(sid, (), e.attempts, str(e)) for sid in source_ids}
        except ContractError as e:
            error = f"契約に合わない出力：{e}"
            return {sid: SourceResult(sid, (), attempts, error) for sid in source_ids}
    fetched_at = now()
    results = {}
    for sid, name in zip(source_ids, names, strict=True):
        state = data["source_status"].get(name)
        if state not in OK_STATES:
            reason = f"状態が {state}" if state is not None else "状態が出力にない"
            results[sid] = SourceResult(sid, (), attempts, f"{name} の{reason}：{_tail(stderr)}")
            continue
        records = tuple(_record(sid, r, fetched_at) for r in data["results"] if r["source"] == name)
        if not records:
            log.info("%s：0件", sid)
        results[sid] = SourceResult(sid, records, attempts)
    return results


def build_plan(subqueries: tuple[Subquery, ...], names: list[str]) -> dict:
    """設定から --plan の JSON を決定的に作る。names にない情報源は計画から除く。"""
    entries = []
    for s in subqueries:
        sources = [n for n in s.sources if n in names]
        if sources:
            entries.append(
                {
                    "search_query": s.search_query,
                    "ranking_query": s.ranking_query,
                    "sources": sources,
                    "weight": s.weight,
                }
            )
    return {**PLAN_HEADER, "subqueries": entries}


def build_args(root: Path, plan: dict, names: list[str], period: Period) -> list[str]:
    """CLI の引数。期間は対象期間を覆う UTC の日付で渡す（D-16）。"""
    days, as_of = last30days_window(period)
    return [
        sys.executable,
        str(root.resolve() / SCRIPT),  # 子プロセスは一時ディレクトリで動くので絶対パスにする
        TOPIC,
        "--emit=json",
        "--search",
        ",".join(names),
        "--web-backend",
        "none",
        "--plan",
        json.dumps(plan, ensure_ascii=False),
        "--days",
        str(days),
        "--as-of",
        as_of.isoformat(),
    ]


def child_env(home: str, github_token: str | None) -> dict[str, str]:
    """子プロセスに渡す環境変数（許可リスト。D-01・D-05）。"""
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": home,
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",  # third_party の中に __pycache__ を作らない
        "LAST30DAYS_CONFIG_DIR": "",  # 設定ファイル（.env）を読ませない
        "LAST30DAYS_LIBRARY_CONTEXT": "off",  # 過去の実行の内容を混ぜない（冪等性）
        "LAST30DAYS_STRICT_EXIT": "1",  # 情報源の失敗を終了コード 3 にする
    }
    if github_token:
        env["GITHUB_TOKEN"] = github_token
    return env


def _run(
    args: list[str], env: dict[str, str], cwd: str, limits: Limits, run: Runner
) -> tuple[str, str, int]:
    """CLI を呼ぶ。終了コード 0・3 なら stdout・stderr・試行回数を返す。"""
    attempts = limits.last30days_retries + 1
    for attempt in range(1, attempts + 1):
        try:
            done = run(
                args,
                env=env,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=limits.last30days_timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # 外側から止めると stdout は空になるので、部分結果は使えない（D-01）
            error = f"{limits.last30days_timeout_s} 秒の上限を超えた"
        else:
            if done.returncode in (0, EXIT_PARTIAL):
                return done.stdout, done.stderr, attempt
            error = f"終了コード {done.returncode}：{_tail(done.stderr)}"
            if done.returncode == EXIT_USAGE:
                raise _CliError(error, attempt)
        if attempt < attempts:
            log.warning("last30days-skill を再試行する（%d 回目の失敗）：%s", attempt, error)
    raise _CliError(error, attempts)


def parse_agent_json(stdout: str) -> dict:
    """agent JSON を読み、必須構造を確かめる。合わなければ ContractError。"""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ContractError(f"JSON として読めない：{e}") from e
    if not isinstance(data, dict):
        raise ContractError("最上位がオブジェクトでない")
    if data.get("comparison"):
        raise ContractError("比較形式の出力")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ContractError(f"schema_version が {data.get('schema_version')!r}")
    status = data.get("source_status")
    if not isinstance(status, dict) or not all(isinstance(v, str) for v in status.values()):
        raise ContractError("source_status が情報源名と状態の対応でない")
    results = data.get("results")
    if not isinstance(results, list):
        raise ContractError("results が配列でない")
    for i, r in enumerate(results):
        _check_result(i, r)
    return data


def _check_result(i: int, r) -> None:
    if not isinstance(r, dict):
        raise ContractError(f"results[{i}] がオブジェクトでない")
    for name in ("source", "url"):
        if not isinstance(r.get(name), str) or not r[name]:
            raise ContractError(f"results[{i}].{name} が空でない文字列でない")
    if not isinstance(r.get("title"), str):  # 空のタイトルは last30days-skill が出しうる
        raise ContractError(f"results[{i}].title が文字列でない")
    published = r.get("published_at")
    if published is not None and not _is_date(published):
        raise ContractError(f"results[{i}].published_at が UTC の日付でない（{published!r}）")
    if not isinstance(r.get("summary", ""), str):
        raise ContractError(f"results[{i}].summary が文字列でない")
    if not isinstance(r.get("engagement", {}), dict):
        raise ContractError(f"results[{i}].engagement がオブジェクトでない")


def _record(source_id: str, r: dict, fetched_at: datetime) -> Record:
    published_at = None
    if "published_at" in r:
        published_at = datetime.combine(date.fromisoformat(r["published_at"]), time(0), UTC)
    summary = r.get("summary") or None
    repo = _GITHUB_REPO.match(r["url"]) if r["source"] == "github" else None
    return Record(
        source_id=source_id,
        url=r["url"],
        title=r["title"],
        published_precision="date" if published_at else "unknown",
        fetched_at=fetched_at,
        events=(Event("published", published_at),) if published_at else (),
        excerpt=summary if summary != r["title"] else None,
        published_at=published_at,
        attention=tuple(
            Attention(f"{METRIC_PREFIXES[r['source']]}_{key}", value, fetched_at)
            for key, value in r.get("engagement", {}).items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ),
        meta_terms=(repo.group(1),) if repo else (),
    )


def _is_date(value) -> bool:
    if not (isinstance(value, str) and _DATE.fullmatch(value)):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _tail(stderr: str | None) -> str:
    return (stderr or "").strip()[-STDERR_TAIL_CHARS:]
