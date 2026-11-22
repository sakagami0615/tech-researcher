"""② ④ の AI の呼び出しの境界（structure.md 2.2・3.3・8.3）。AI の差し替えはここで行う。

agent コンテナの Claude Code を `docker compose run --rm -T agent agent-run <スキル名> -p …` で呼ぶ
（起動は統括スクリプトの Compose が行う。agent-run はスキルの SKILL.md があることを確かめてから
claude を動かす）。入力は標準入力の JSON、出力は標準出力の結果の JSON で、そこから
構造化出力（structured_output）を取り出す。

- 道具（`--tools ""`）と MCP（`--disallowedTools "mcp__*"`・`--strict-mcp-config`）をなくす
  （F4・F9）
- 構造化出力（`--output-format json --json-schema`）で受け取り、セッションを保存しない
  （`--no-session-persistence`）（F6・F9）
- `--bare` は付けない（サブスクリプションのトークンを読まなくなる。F1）。`--model` も付けず、
  既定のモデルに任せる。使われたモデルの名前は結果の modelUsage から取り出す
- 認証情報（agent.env）は compose が agent にだけ渡す。ここでは扱わない。書き込み先・自動更新・
  通信の設定はイメージ（docker/agent.Dockerfile）と compose.yaml が持つ

- 入力が上限（日次 2MB・週次 8MB。D-21）を超えたら、claude を呼ばずに AgentError(input_too_large)
- 1回の呼び出しの使用量・所要時間・ターン数を結果の JSON から取り出す（AgentUsage。
  structure.md 3.8 の7・8 の記録に使う）。失敗のときも、結果の JSON があれば取り出す

- 出力がなく終わった失敗は、Claude Code の結果の JSON から分類する（classify_failure）。
  一時的な失敗（overloaded・server_error・network）は ② が合計の上限の中で1回だけ再試行する
  （structure.md 6.3。再試行は統括スクリプトの `_analyze`）。利用上限（usage_limit）・認証（auth）の
  失敗は再試行せず、④ も動かせないので ④ を飛ばす

テストでは runner（Compose）を偽の実装に差し替えるか、call_agent ごと差し替える（製品には偽の AI の
モードを作らない）。
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Protocol

AGENT_SERVICE = "agent"
AGENT_RUN = "agent-run"  # イメージの中のラッパー（docker/agent-run.sh）
MISSING_SKILL_EXIT = 78  # agent-run がスキルを見つけられなかったときの終了コード
# claude を動かさずに終わった失敗の分類（上限の超過、スキーマの誤り、イメージにスキルがない）。
# 呼び出し側は使用量（usage）の行を残さない
NOT_CALLED = frozenset({"input_too_large", "bad_schema", "missing_skill"})
# 出力がなく終わった失敗の分類（classify_failure）。TRANSIENT は ② が再試行する一時的な失敗
OVERLOADED = "overloaded"
SERVER_ERROR = "server_error"
NETWORK = "network"
USAGE_LIMIT = "usage_limit"
AUTH = "auth"
TRANSIENT = frozenset({OVERLOADED, SERVER_ERROR, NETWORK})
# 失敗の文（Claude Code が結果の JSON の result に書く。出力がなく is_error のときだけ読む）の
# 手がかり。分類の語を決めるためだけに使い、文はログにも残さない。実際の文は PoC（structure.md
# 3.8 の8）で確かめて足す。順に見て、最初に合った分類にする（利用上限を先に見る）
_FAILURE_TEXT = (
    (
        USAGE_LIMIT,
        re.compile(r"usage limit|limit reached|rate limit|quota|too many requests", re.I),
    ),
    (AUTH, re.compile(r"invalid api key|/login|not logged in|authentication|unauthorized", re.I)),
    (OVERLOADED, re.compile(r"overloaded", re.I)),
    (SERVER_ERROR, re.compile(r"internal server error|bad gateway|service unavailable", re.I)),
    (
        NETWORK,
        re.compile(
            r"unable to connect|connection (error|refused|reset)|econn(reset|refused)|"
            r"etimedout|enotfound|network error|fetch failed",
            re.I,
        ),
    ),
)
# 内側のスキル（structure.md 3.5）。リポジトリの agent/skills/<名前>/SKILL.md を、agent のイメージの
# 作業ディレクトリの .claude/skills/ にコピーする（docker/agent.Dockerfile）
DAILY_SKILL = "tr-daily-evaluate"
WEEKLY_SKILL = "tr-weekly-analyze"  # ② 週次の分析（スキルの本体は週次分析スキルの Issue で作る）
INVESTIGATE_SKILL = "tr-investigate"  # ④ 失敗時の調査（日次・週次で共通）

# Claude Code が結果の JSON に書く分類（subtype・terminal_reason）。ログに残す前に形を確かめる
_CODE = re.compile(r"[a-z0-9_]{1,64}")
# モデルの名前（例：claude-opus-4-1-20250805、claude-sonnet-4-5[1m]）
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/\[\]-]{0,127}")


class ProcessResult(Protocol):
    returncode: int | None  # None は上限の時間で止めた
    stdout: str
    stderr: str
    remove_failed: bool


class Runner(Protocol):
    def run(
        self, service: str, args: Sequence[str], timeout_s: float, stdin: bytes | None = None
    ) -> ProcessResult: ...


@dataclass(frozen=True)
class AgentUsage:
    """1回の呼び出しの使用量と所要時間（Claude Code の結果の JSON の値。ないものは None）。

    total_cost_usd はクライアント側の見積もりで、サブスクリプションの利用量そのものではない
    （structure.md 6.5）。比べる目安としてだけ残す。
    """

    duration_ms: int | None = None
    duration_api_ms: int | None = None
    num_turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    total_cost_usd: float | None = None

    def fields(self) -> str:
        """ログに残す `名前=値` の並び（値は数だけ。ないものは unknown）。"""
        return " ".join(
            f"{name}={'unknown' if value is None else value}" for name, value in vars(self).items()
        )


@dataclass(frozen=True)
class AgentResult:
    """呼び出しの結果。output は構造化出力（形は ③ が D6(a) で検査する）。"""

    output: object
    models: tuple[str, ...] = ()  # 使われたモデルの名前（runs の model。data-model.md）
    usage: AgentUsage = AgentUsage()


class AgentError(Exception):
    """AI の呼び出しの失敗。category は要約とログのエラーの分類（コードが決めた値だけ）。

    - timeout：段階の上限の時間を超えた（コンテナは止めて消す）
    - agent_failed：終了コードが0でない、is_error、または構造化出力がない（F6・F8）
    - overloaded・server_error・network：出力がなく、Claude Code が過負荷・サーバーエラー・通信の
      失敗を返した（一時的。② が1回だけ再試行する）
    - usage_limit・auth：出力がなく、利用上限・認証の失敗を返した（再試行しない。④ も飛ばす）
    - bad_output：標準出力に結果の JSON がない
    - bad_schema：スキーマのファイルがない・読めない、または JSON でない
    - missing_skill：イメージに渡すスキルがない（イメージを作り直していない。claude を動かさない）
    - input_too_large：入力が上限を超えた（claude を呼ばない。D-21）

    detail はログにだけ残す情報（stderr と、その後ろに Claude Code の分類）。AI の出力の文は
    入れない。models・usage は、結果の JSON があったときだけその値（ないときは空）。
    """

    def __init__(
        self,
        category: str,
        detail: str = "",
        *,
        remove_failed: bool = False,
        models: tuple[str, ...] = (),
        usage: AgentUsage | None = None,
    ):
        super().__init__(category)
        self.category = category
        self.detail = detail
        self.remove_failed = remove_failed
        self.models = models
        self.usage = usage


def claude_command(skill: str, schema: str, max_turns: int | None) -> list[str]:
    """agent コンテナで動かすコマンド（structure.md 3.3）。

    agent-run（docker/agent-run.sh）がスキルがイメージにあることを確かめてから、残りの引数で
    claude を動かす。
    """
    command = [
        AGENT_RUN,
        skill,
        "-p",
        f"/{skill}",  # 先頭の /スキル名 で、Skill の道具なしにスキルが展開される（F5・F15）
        "--tools",
        "",
        "--disallowedTools",
        "mcp__*",
        "--strict-mcp-config",
        "--output-format",
        "json",
        "--json-schema",
        schema,
        "--no-session-persistence",
    ]
    if max_turns is not None:  # 値は PoC で決める（3.8 の7）。決めるまでは段階の上限の時間で止める
        command += ["--max-turns", str(max_turns)]
    return command


def load_schema(path: Path | Traversable) -> str:
    """`--json-schema` に渡すスキーマ（1行の JSON）。

    ない・読めない・JSON でなければ AgentError(bad_schema)（パッケージの作り方の誤りなど）。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:  # ない・権限・フォルダ・UTF-8 でないなど
        raise AgentError("bad_schema", "スキーマのファイルを読めない") from None
    try:
        schema = json.loads(text)
    except json.JSONDecodeError:
        raise AgentError("bad_schema", "スキーマのファイルが JSON でない") from None
    if not isinstance(schema, dict):
        raise AgentError("bad_schema", "スキーマがオブジェクトでない")
    return json.dumps(schema, ensure_ascii=False, separators=(",", ":"))


def call_agent(
    body: bytes,
    timeout_s: float,
    *,
    runner: Runner,
    skill: str,
    schema: Path | Traversable,
    max_turns: int | None,
    max_input_bytes: int,
) -> AgentResult:
    """入力の JSON（標準入力に渡すバイト列）を AI に渡し、構造化出力を返す。

    skill は内側のスキルの名前（agent/skills/<名前>）。プロンプトは `/<名前>`。スキルがイメージに
    なければ agent-run が claude を動かさずに止める（missing_skill）。
    timeout_s は段階の上限（limits の analyze。D-21）。max_input_bytes は入力の上限（limits の
    agent_input_bytes。D-21）で、超えたら claude を呼ばない。失敗は AgentError。docker を起動
    できなければ OSError。返す構造化出力の形は確かめない（③ が D6(a) で検査する）。
    """
    if len(body) > max_input_bytes:
        raise AgentError(
            "input_too_large", f"入力が {len(body)} バイトで、上限 {max_input_bytes} を超えた"
        )
    command = claude_command(skill, load_schema(schema), max_turns)
    result = runner.run(AGENT_SERVICE, command, timeout_s, stdin=body)
    if result.returncode is None:
        # 止める前に結果の JSON が出ていれば、使った量とモデルは残す
        data = _last_json(result.stdout)
        done = data is not None and data.get("type") == "result"
        raise AgentError(
            "timeout",
            result.stderr,
            remove_failed=result.remove_failed,
            models=_models(data.get("modelUsage")) if done else (),
            usage=_usage(data) if done else None,
        )
    if result.returncode == MISSING_SKILL_EXIT and _last_json(result.stdout) is None:
        raise AgentError("missing_skill", result.stderr, remove_failed=result.remove_failed)
    try:
        return parse_result(result.returncode, result.stdout)
    except AgentError as e:
        # ログでは末尾を残して切るので、Claude Code の分類を stderr の後ろに置く
        detail = f"{result.stderr}\n{e.detail}" if result.stderr else e.detail
        raise AgentError(
            e.category,
            detail,
            remove_failed=result.remove_failed,
            models=e.models,
            usage=e.usage,
        ) from None


def parse_result(returncode: int, stdout: str) -> AgentResult:
    """claude の終了コードと標準出力（`--output-format json`）から結果を取り出す。

    標準出力の最後の行を結果の JSON として読む（イメージがないと compose がビルドの進み具合を
    標準出力に出すため）。終了コードが0で、is_error でなく、structured_output があるときだけ成功
    （structure.md 3.3、F6・F8）。失敗の detail には Claude Code の分類だけを入れ、result の文
    （AI の出力や、入力の外部由来の文を含みうる）は入れない。
    """
    data = _last_json(stdout)
    if data is None or data.get("type") != "result":
        raise AgentError("bad_output", f"exit={returncode} 結果の JSON がない")
    has_output = data.get("structured_output") is not None
    models, usage = _models(data.get("modelUsage")), _usage(data)
    ok = returncode == 0 and data.get("is_error") is False and has_output
    if not ok:
        fields = [
            f"exit={returncode}",
            f"is_error={_code(data.get('is_error'))}",
            f"subtype={_code(data.get('subtype'))}",
            f"terminal_reason={_code(data.get('terminal_reason'))}",
            f"structured_output={str(has_output).lower()}",
        ]
        category = classify_failure(data) if not has_output else "agent_failed"
        if category != "agent_failed":
            fields.append(f"classified={category}")
        raise AgentError(category, " ".join(fields), models=models, usage=usage)
    return AgentResult(output=data["structured_output"], models=models, usage=usage)


def classify_failure(data: dict) -> str:
    """出力がなく終わった結果の JSON の失敗の分類。分からなければ agent_failed（再試行しない）。

    HTTP の状態（api_error_status。あれば）を先に見る：401・403 は auth、429 は usage_limit、
    529 は overloaded、ほかの 5xx は server_error、知らない状態は agent_failed（状態があれば
    状態だけで決め、文で分類し直さない）。状態がなければ result の文の手がかりで決める
    （terminal_reason が api_error のときだけ）。is_error が真でなければ agent_failed。
    一時的と分かるものだけを TRANSIENT にし、迷うものは再試行しない。
    """
    if data.get("is_error") is not True:
        return "agent_failed"
    status = data.get("api_error_status")
    if isinstance(status, int) and not isinstance(status, bool):
        if status in (401, 403):
            return AUTH
        if status == 429:
            return USAGE_LIMIT
        if status == 529:
            return OVERLOADED
        if 500 <= status < 600:
            return SERVER_ERROR
        return "agent_failed"  # 知らない状態は、文で分類し直さない（再試行しない）
    text = data.get("result")
    # 文を読むのは API の失敗（terminal_reason が api_error）のときだけ。スキーマに合わない出力や
    # ターン数の超過で終わった結果の文は、AI や入力の外部の文を含みうるので読まない
    if data.get("terminal_reason") == "api_error" and isinstance(text, str):
        for category, pattern in _FAILURE_TEXT:
            if pattern.search(text):
                return category
    return "agent_failed"


def _last_json(stdout: str) -> dict | None:
    lines = stdout.strip().splitlines()
    try:
        data = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _code(value: object) -> str:
    """分類の値をログに残せる形にする（知らない形は unknown）。"""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, str) and _CODE.fullmatch(value):
        return value
    return "none" if value is None else "unknown"


def split_models(text: str) -> tuple[str, ...]:
    """`,` でつないだモデルの名前（runs の model の形）を分ける。形が違えば ValueError。"""
    names = tuple(text.split(","))
    if not all(_MODEL.fullmatch(n) for n in names):
        raise ValueError("モデルの名前の形が正しくない")
    return names


def _models(usage: object) -> tuple[str, ...]:
    if not isinstance(usage, dict):
        return ()
    return tuple(sorted(name for name in usage if isinstance(name, str) and _MODEL.fullmatch(name)))


def _usage(data: dict) -> AgentUsage:
    """結果の JSON の使用量と所要時間。数でない値は None にする（ログに外部の文を入れない）。"""
    tokens = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    cost = data.get("total_cost_usd")
    return AgentUsage(
        duration_ms=_count(data.get("duration_ms")),
        duration_api_ms=_count(data.get("duration_api_ms")),
        num_turns=_count(data.get("num_turns")),
        input_tokens=_count(tokens.get("input_tokens")),
        output_tokens=_count(tokens.get("output_tokens")),
        cache_creation_input_tokens=_count(tokens.get("cache_creation_input_tokens")),
        cache_read_input_tokens=_count(tokens.get("cache_read_input_tokens")),
        total_cost_usd=float(cost)
        if isinstance(cost, int | float) and not isinstance(cost, bool) and 0 <= cost < 1e9
        else None,
    )


def _count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None
