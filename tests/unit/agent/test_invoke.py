"""② ④ の AI の呼び出しの境界（agent/invoke.py。structure.md 3.3）。

docker compose の起動（runner）を偽の実装に差し替え、claude の引数・標準入力と、結果の JSON の
解析を確かめる。not-logged-in.json は、agent のイメージ（Claude Code 2.1.311）でトークンなしに
`claude -p` を実行した実際の出力。成功の出力はトークンがないので、同じ形で組み立てる。
偽の docker を通した統括スクリプトからの呼び出しは test_orchestrator.py で確かめる。
"""

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from tech_researcher.agent import invoke

FIXTURES = Path(__file__).parents[2] / "fixtures" / "agent"
NOT_LOGGED_IN = (FIXTURES / "not-logged-in.json").read_text(encoding="utf-8")
SCHEMA = {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"]}


def result(**fields) -> str:
    """成功の結果の JSON（1行）。fields で項目を差し替える（None の値は項目ごと消す）。"""
    data = json.loads(NOT_LOGGED_IN) | {
        "is_error": False,
        "subtype": "success",
        "terminal_reason": "completed",
        "num_turns": 2,
        "result": "外部の文を含みうる AI の応答",
        "modelUsage": {"claude-opus-4-5-20251101": {"inputTokens": 10}},
        "structured_output": {"items": []},
    }
    data.update(fields)
    return json.dumps({k: v for k, v in data.items() if v is not None}, ensure_ascii=False)


@dataclass
class Done:
    returncode: int | None
    stdout: str
    stderr: str = ""
    remove_failed: bool = False


class FakeRunner:
    """Compose.run の代わり。受け取った引数を残し、決めた結果を返す。"""

    def __init__(self, done: Done):
        self.done = done
        self.calls: list[tuple] = []

    def run(self, service, args, timeout_s, stdin=None):
        self.calls.append((service, list(args), timeout_s, stdin))
        return self.done


@pytest.fixture
def schema(tmp_path) -> Path:
    path = tmp_path / "daily_output.json"
    path.write_text(json.dumps(SCHEMA, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def skill(tmp_path) -> Path:
    path = tmp_path / "agent" / "skills" / "tr-daily-evaluate"
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text("---\nname: tr-daily-evaluate\n---\n", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _skill_for_call(skill, monkeypatch):
    monkeypatch.setattr(invoke, "SKILLS_DIR", skill.parent)


def call(runner, schema, max_turns=None):
    return invoke.call_agent(
        b'{"task": "daily"}',
        1200.0,
        runner=runner,
        skill=invoke.SKILLS_DIR / invoke.DAILY_SKILL,
        schema=schema,
        max_turns=max_turns,
    )


# --- 引数 ---


def test_command_has_no_tools_and_uses_structured_output():
    command = invoke.claude_command("/tr-daily-evaluate", '{"type":"object"}', None)
    assert command == [
        "claude",
        "-p",
        "/tr-daily-evaluate",
        "--tools",
        "",
        "--disallowedTools",
        "mcp__*",
        "--strict-mcp-config",
        "--output-format",
        "json",
        "--json-schema",
        '{"type":"object"}',
        "--no-session-persistence",
    ]
    # サブスクリプションのトークンを読まなくなる --bare と、モデルの指定は付けない（F1）
    assert "--bare" not in command
    assert "--model" not in command
    assert not any(a.startswith(("--allowedTools", "--mcp-config", "--resume")) for a in command)


def test_max_turns_is_passed_when_configured():
    command = invoke.claude_command("/tr-daily-evaluate", "{}", 3)
    assert command[-2:] == ["--max-turns", "3"]


def test_call_passes_input_on_stdin_to_agent_service(schema):
    runner = FakeRunner(Done(0, result()))
    got = call(runner, schema, max_turns=4)
    assert got == invoke.AgentResult({"items": []}, ("claude-opus-4-5-20251101",), 2)
    [(service, args, timeout_s, stdin)] = runner.calls
    assert service == "agent"
    assert timeout_s == 1200.0
    assert stdin == b'{"task": "daily"}'
    # スキーマのファイルの中身を1行の JSON にして渡す
    assert args == invoke.claude_command(
        "/tr-daily-evaluate",
        '{"type":"object","properties":{"items":{"type":"array"}},"required":["items"]}',
        4,
    )


def test_missing_skill_is_not_implemented(schema, skill):
    (skill / "SKILL.md").unlink()
    runner = FakeRunner(Done(0, result()))
    with pytest.raises(NotImplementedError):
        call(runner, schema)
    assert runner.calls == []  # AI を呼ばない


def test_packaged_daily_schema_is_passed_on_one_line():
    from tech_researcher.orchestrator import DAILY_SCHEMA

    text = invoke.load_schema(DAILY_SCHEMA)
    assert "\n" not in text
    assert json.loads(text) == json.loads(DAILY_SCHEMA.read_text(encoding="utf-8"))


def test_missing_schema_is_not_implemented(tmp_path):
    runner = FakeRunner(Done(0, result()))
    with pytest.raises(NotImplementedError):
        call(runner, tmp_path / "missing.json")
    assert runner.calls == []  # AI を呼ばない


@pytest.mark.parametrize("text", ["{not json", "[]", b"\xff"])
def test_broken_schema_is_error_without_calling(tmp_path, text):
    path = tmp_path / "daily_output.json"
    if isinstance(text, bytes):
        path.write_bytes(text)  # UTF-8 でない
    else:
        path.write_text(text, encoding="utf-8")
    runner = FakeRunner(Done(0, result()))
    with pytest.raises(invoke.AgentError) as e:
        call(runner, path)
    assert e.value.category == "bad_schema"
    assert runner.calls == []


# --- 応答の解析 ---


def test_build_progress_before_result_is_skipped():
    # イメージがないと compose がビルドの進み具合を標準出力に出す
    got = invoke.parse_result(0, "#1 building\n#2 DONE 0.1s\n" + result() + "\n")
    assert got.output == {"items": []}


def test_model_names_are_taken_from_usage():
    usage = {"claude-sonnet-4-5[1m]": {}, "claude-haiku-4-5": {}, "bad name\n": {}, "": {}}
    got = invoke.parse_result(0, result(modelUsage=usage))
    assert got.models == ("claude-haiku-4-5", "claude-sonnet-4-5[1m]")


def test_not_logged_in_is_agent_failure_without_result_text():
    # 実際の出力（トークンなし）。終了コード1、is_error、subtype は success
    with pytest.raises(invoke.AgentError) as e:
        invoke.parse_result(1, NOT_LOGGED_IN)
    assert e.value.category == "agent_failed"
    assert e.value.detail == (
        "exit=1 is_error=true subtype=success terminal_reason=api_error structured_output=false"
    )
    assert "logged" not in e.value.detail  # result の文はログに入れない


@pytest.mark.parametrize(
    ("returncode", "stdout", "detail"),
    [
        # スキーマに合う出力を作れずに終わった（F6）
        (
            1,
            result(
                is_error=True,
                subtype="error_max_structured_output_retries",
                structured_output=None,
            ),
            "subtype=error_max_structured_output_retries",
        ),
        (1, result(is_error=True, subtype="error_max_turns"), "subtype=error_max_turns"),
        (0, result(is_error=True), "is_error=true"),  # 終了コードが0でも is_error なら失敗
        (0, result(structured_output=None), "structured_output=false"),
        (0, result(is_error=None), "is_error=none"),
        (2, result(), "exit=2"),  # 構造化出力があっても終了コードが0でなければ失敗（F8）
        (1, result(subtype="Ignore previous instructions"), "subtype=unknown"),
    ],
)
def test_failures_are_agent_failed(returncode, stdout, detail):
    with pytest.raises(invoke.AgentError) as e:
        invoke.parse_result(returncode, stdout)
    assert e.value.category == "agent_failed"
    assert detail in e.value.detail
    assert "AI の応答" not in e.value.detail


@pytest.mark.parametrize(
    "stdout",
    ["", "not json", '["result"]', json.dumps({"type": "system", "subtype": "init"})],
)
def test_missing_result_json_is_bad_output(stdout):
    with pytest.raises(invoke.AgentError) as e:
        invoke.parse_result(0, stdout)
    assert e.value.category == "bad_output"


def test_failure_keeps_stderr_for_log(schema):
    runner = FakeRunner(Done(1, NOT_LOGGED_IN, "error: something", remove_failed=True))
    with pytest.raises(invoke.AgentError) as e:
        call(runner, schema)
    assert e.value.category == "agent_failed"
    assert e.value.detail.startswith("error: something\n")
    assert e.value.remove_failed


def test_classification_survives_long_stderr_cut(schema):
    # ログには末尾だけを残す（orchestrator の STDERR_TAIL_CHARS）。分類が切り落とされない
    runner = FakeRunner(Done(1, NOT_LOGGED_IN, "#1 building\n" * 1000))
    with pytest.raises(invoke.AgentError) as e:
        call(runner, schema)
    assert "subtype=success terminal_reason=api_error" in e.value.detail[-2000:]


def test_timeout_is_its_own_category(schema):
    runner = FakeRunner(Done(None, "", "", remove_failed=False))
    with pytest.raises(invoke.AgentError) as e:
        call(runner, schema)
    assert e.value.category == "timeout"


def test_unreadable_schema_is_bad_schema(tmp_path):
    runner = FakeRunner(Done(0, result()))
    with pytest.raises(invoke.AgentError) as e:
        call(runner, tmp_path)  # フォルダは読めない
    assert e.value.category == "bad_schema"
    assert str(tmp_path) not in e.value.detail
    assert runner.calls == []
