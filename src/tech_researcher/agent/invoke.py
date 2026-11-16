"""② ④ の AI の呼び出しの境界（structure.md 2.2・3.3・8.3）。AI の差し替えはここで行う。

agent コンテナの Claude Code を `docker compose run --rm -T agent claude -p …` で呼ぶ（起動は統括
スクリプトの Compose が行う）。入力は標準入力の JSON、出力は標準出力の結果の JSON で、そこから
構造化出力（structured_output）を取り出す。

- 道具（`--tools ""`）と MCP（`--disallowedTools "mcp__*"`・`--strict-mcp-config`）をなくす
  （F4・F9）
- 構造化出力（`--output-format json --json-schema`）で受け取り、セッションを保存しない
  （`--no-session-persistence`）（F6・F9）
- `--bare` は付けない（サブスクリプションのトークンを読まなくなる。F1）。`--model` も付けず、
  既定のモデルに任せる。使われたモデルの名前は結果の modelUsage から取り出す
- 認証情報（agent.env）は compose が agent にだけ渡す。ここでは扱わない。書き込み先・自動更新・
  通信の設定はイメージ（docker/agent.Dockerfile）と compose.yaml が持つ

テストでは runner（Compose）を偽の実装に差し替えるか、call_agent ごと差し替える（製品には偽の AI の
モードを作らない）。再試行（structure.md 6.3）と、認証の失敗・利用上限の分類は後の Issue で足す。
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Protocol

AGENT_SERVICE = "agent"
DAILY_PROMPT = "/tr-daily-evaluate"  # 内側のスキル（agent/skills/。structure.md 3.5）

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
class AgentResult:
    """呼び出しの結果。output は構造化出力（形は ③ が D6(a) で検査する）。"""

    output: object
    models: tuple[str, ...] = ()  # 使われたモデルの名前（runs の model。data-model.md）
    num_turns: int | None = None


class AgentError(Exception):
    """AI の呼び出しの失敗。category は要約とログのエラーの分類（コードが決めた値だけ）。

    - timeout：段階の上限の時間を超えた（コンテナは止めて消す）
    - agent_failed：終了コードが0でない、is_error、または構造化出力がない（F6・F8）
    - bad_output：標準出力に結果の JSON がない
    - bad_schema：スキーマのファイルを読めない、または JSON でない

    detail はログにだけ残す情報（stderr と、その後ろに Claude Code の分類）。AI の出力の文は
    入れない。
    """

    def __init__(self, category: str, detail: str = "", *, remove_failed: bool = False):
        super().__init__(category)
        self.category = category
        self.detail = detail
        self.remove_failed = remove_failed


def claude_command(prompt: str, schema: str, max_turns: int | None) -> list[str]:
    """agent コンテナで動かす claude の引数（structure.md 3.3）。"""
    command = [
        "claude",
        "-p",
        prompt,  # 先頭の /スキル名 で、Skill の道具なしにスキルが展開される（F5・F15）
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

    ファイルがなければ NotImplementedError（スキーマを作る Issue の前。統括スクリプトは ② を
    not_implemented にする）。読めない・JSON でなければ AgentError(bad_schema)。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise NotImplementedError("出力のスキーマがまだない") from None
    except OSError, UnicodeDecodeError:  # 権限・フォルダ・UTF-8 でないなど
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
    prompt: str,
    schema: Path | Traversable,
    max_turns: int | None,
) -> AgentResult:
    """入力の JSON（標準入力に渡すバイト列）を AI に渡し、構造化出力を返す。

    timeout_s は段階の上限（limits の analyze。D-21）。失敗は AgentError。docker を起動できなければ
    OSError。返す構造化出力の形は確かめない（③ が D6(a) で検査する）。
    """
    command = claude_command(prompt, load_schema(schema), max_turns)
    result = runner.run(AGENT_SERVICE, command, timeout_s, stdin=body)
    if result.returncode is None:
        raise AgentError("timeout", result.stderr, remove_failed=result.remove_failed)
    try:
        return parse_result(result.returncode, result.stdout)
    except AgentError as e:
        # ログでは末尾を残して切るので、Claude Code の分類を stderr の後ろに置く
        detail = f"{result.stderr}\n{e.detail}" if result.stderr else e.detail
        raise AgentError(e.category, detail, remove_failed=result.remove_failed) from None


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
    ok = returncode == 0 and data.get("is_error") is False and has_output
    if not ok:
        fields = [
            f"exit={returncode}",
            f"is_error={_code(data.get('is_error'))}",
            f"subtype={_code(data.get('subtype'))}",
            f"terminal_reason={_code(data.get('terminal_reason'))}",
            f"structured_output={str(has_output).lower()}",
        ]
        raise AgentError("agent_failed", " ".join(fields))
    turns = data.get("num_turns")
    return AgentResult(
        output=data["structured_output"],
        models=_models(data.get("modelUsage")),
        num_turns=turns if isinstance(turns, int) and not isinstance(turns, bool) else None,
    )


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


def _models(usage: object) -> tuple[str, ...]:
    if not isinstance(usage, dict):
        return ()
    return tuple(sorted(name for name in usage if isinstance(name, str) and _MODEL.fullmatch(name)))
