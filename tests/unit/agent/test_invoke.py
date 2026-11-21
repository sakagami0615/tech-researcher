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
from tech_researcher.agent.daily import CATEGORIES
from tech_researcher.validate import text as text_checks

FIXTURES = Path(__file__).parents[2] / "fixtures" / "agent"
NOT_LOGGED_IN = (FIXTURES / "not-logged-in.json").read_text(encoding="utf-8")
# 同じ形で、分類の手がかりのない失敗の文（agent_failed になる）
UNCLASSIFIED = json.dumps(
    {**json.loads(NOT_LOGGED_IN), "result": "something went wrong"}, ensure_ascii=False
)
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


def call(runner, schema, max_turns=None, max_input_bytes=2_000_000):
    return invoke.call_agent(
        b'{"task": "daily"}',
        1200.0,
        runner=runner,
        skill=invoke.DAILY_SKILL,
        schema=schema,
        max_turns=max_turns,
        max_input_bytes=max_input_bytes,
    )


# --- 引数 ---


def test_command_has_no_tools_and_uses_structured_output():
    command = invoke.claude_command("tr-daily-evaluate", '{"type":"object"}', None)
    assert command == [
        "agent-run",  # スキルがイメージにあることを確かめてから claude を動かす
        "tr-daily-evaluate",
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
    command = invoke.claude_command("tr-daily-evaluate", "{}", 3)
    assert command[-2:] == ["--max-turns", "3"]


def test_call_passes_input_on_stdin_to_agent_service(schema):
    runner = FakeRunner(Done(0, result()))
    got = call(runner, schema, max_turns=4)
    assert got.output == {"items": []}
    assert got.models == ("claude-opus-4-5-20251101",)
    assert got.usage.num_turns == 2
    [(service, args, timeout_s, stdin)] = runner.calls
    assert service == "agent"
    assert timeout_s == 1200.0
    assert stdin == b'{"task": "daily"}'
    # スキーマのファイルの中身を1行の JSON にして渡す
    assert args == invoke.claude_command(
        "tr-daily-evaluate",
        '{"type":"object","properties":{"items":{"type":"array"}},"required":["items"]}',
        4,
    )


def test_repository_has_the_daily_skill():
    # イメージに入れる内側のスキル（docker/agent.Dockerfile が agent/skills/ をコピーする）
    skill = Path(__file__).parents[3] / "agent" / "skills" / invoke.DAILY_SKILL / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    assert text.startswith(f"---\nname: {invoke.DAILY_SKILL}\n")
    # スキルに書いた目安が、③ の検査の値（コードの固定値。その2倍で除外）とカテゴリにそろっている
    for limit in (
        text_checks.HEADLINE_LIMIT,
        text_checks.POINT_LIMIT,
        *text_checks.IMPORTANCE_LIMIT.values(),
        text_checks.PERIOD_EVENT_LIMIT,
    ):
        assert f"{limit}字以内" in text
    assert f"1〜{text_checks.POINTS_LIMIT}つ" in text
    assert all(f"| `{c}` |" in text for c in CATEGORIES)


@pytest.mark.parametrize(("size", "called"), [(17, True), (16, False)])
def test_input_over_limit_stops_before_calling(schema, size, called):
    runner = FakeRunner(Done(0, result()))
    if called:
        call(runner, schema, max_input_bytes=size)  # ちょうど上限は通す
    else:
        with pytest.raises(invoke.AgentError) as e:
            call(runner, schema, max_input_bytes=size)
        assert e.value.category == "input_too_large"
    assert bool(runner.calls) == called  # 超えたら AI を呼ばない


def test_packaged_daily_schema_is_passed_on_one_line():
    from tech_researcher.orchestrator import DAILY_SCHEMA

    text = invoke.load_schema(DAILY_SCHEMA)
    assert "\n" not in text
    assert json.loads(text) == json.loads(DAILY_SCHEMA.read_text(encoding="utf-8"))


def test_missing_schema_is_bad_schema(tmp_path):
    runner = FakeRunner(Done(0, result()))
    with pytest.raises(invoke.AgentError) as e:
        call(runner, tmp_path / "missing.json")
    assert e.value.category == "bad_schema"
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


def test_usage_and_duration_are_taken_from_result():
    got = invoke.parse_result(
        0,
        result(
            duration_ms=81234,
            duration_api_ms=79000,
            num_turns=2,
            total_cost_usd=0.42,
            usage={
                "input_tokens": 12,
                "output_tokens": 3456,
                "cache_creation_input_tokens": 50000,
                "cache_read_input_tokens": 7000,
            },
        ),
    )
    assert got.usage == invoke.AgentUsage(81234, 79000, 2, 12, 3456, 50000, 7000, 0.42)
    assert got.usage.fields() == (
        "duration_ms=81234 duration_api_ms=79000 num_turns=2 input_tokens=12 output_tokens=3456"
        " cache_creation_input_tokens=50000 cache_read_input_tokens=7000 total_cost_usd=0.42"
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"duration_ms": "1 ms", "num_turns": True, "usage": "x", "total_cost_usd": float("nan")},
        {"duration_ms": -1, "usage": {"input_tokens": 1.5}, "total_cost_usd": "0.1"},
    ],
)
def test_usage_keeps_only_numbers(fields):
    data = json.loads(result()) | fields
    got = invoke.parse_result(0, json.dumps(data))
    assert got.usage.duration_ms is None
    assert got.usage.input_tokens is None
    assert got.usage.total_cost_usd is None
    assert "unknown" in got.usage.fields()


def test_failure_keeps_usage_and_models():
    # 上限のターン数で止まったときなども、使った量は残す（structure.md 3.8 の7・8）
    with pytest.raises(invoke.AgentError) as e:
        invoke.parse_result(1, result(is_error=True, subtype="error_max_turns", num_turns=5))
    assert e.value.usage.num_turns == 5
    assert e.value.models == ("claude-opus-4-5-20251101",)


def test_not_logged_in_usage_is_zero():
    with pytest.raises(invoke.AgentError) as e:
        invoke.parse_result(1, NOT_LOGGED_IN)
    assert e.value.usage == invoke.AgentUsage(43, 0, 1, 0, 0, 0, 0, 0.0)
    assert e.value.models == ()


def test_split_models():
    assert invoke.split_models("claude-a,claude-b[1m]") == ("claude-a", "claude-b[1m]")
    for bad in ("", "a,,b", "a b", "a\nb"):
        with pytest.raises(ValueError):
            invoke.split_models(bad)


def test_model_names_are_taken_from_usage():
    usage = {"claude-sonnet-4-5[1m]": {}, "claude-haiku-4-5": {}, "bad name\n": {}, "": {}}
    got = invoke.parse_result(0, result(modelUsage=usage))
    assert got.models == ("claude-haiku-4-5", "claude-sonnet-4-5[1m]")


def test_not_logged_in_is_auth_failure_without_result_text():
    # 実際の出力（トークンなし）。終了コード1、is_error、subtype は success。認証の失敗にする
    with pytest.raises(invoke.AgentError) as e:
        invoke.parse_result(1, NOT_LOGGED_IN)
    assert e.value.category == "auth"
    assert e.value.detail == (
        "exit=1 is_error=true subtype=success terminal_reason=api_error structured_output=false"
        " classified=auth"
    )
    assert "logged" not in e.value.detail  # result の文はログに入れない


def test_unclassified_failure_stays_agent_failed():
    with pytest.raises(invoke.AgentError) as e:
        invoke.parse_result(1, UNCLASSIFIED)
    assert e.value.category == "agent_failed"
    assert "classified" not in e.value.detail


@pytest.mark.parametrize(
    ("fields", "category"),
    [
        ({"api_error_status": 529}, "overloaded"),
        ({"api_error_status": 500}, "server_error"),
        ({"api_error_status": 503}, "server_error"),
        ({"api_error_status": 429}, "usage_limit"),
        ({"api_error_status": 401}, "auth"),
        ({"api_error_status": 403}, "auth"),
        ({"api_error_status": 400, "result": "bad request"}, "agent_failed"),
        ({"api_error_status": True, "result": "bad request"}, "agent_failed"),
        ({"result": "API Error: Overloaded"}, "overloaded"),
        ({"result": "Internal server error"}, "server_error"),
        ({"result": "Unable to connect to API (ECONNRESET)"}, "network"),
        ({"result": "Claude AI usage limit reached"}, "usage_limit"),
        ({"result": "Invalid API key · Please run /login"}, "auth"),
        # 状態が先。文に別の手がかりがあっても、状態で決める
        ({"api_error_status": 529, "result": "Not logged in"}, "overloaded"),
        # is_error が真でない文は読まない
        ({"is_error": False, "result": "overloaded"}, "agent_failed"),
    ],
)
def test_failure_classification(fields, category):
    data = {**json.loads(NOT_LOGGED_IN), "result": "x", **fields}
    assert invoke.classify_failure(data) == category
    if category in ("overloaded", "server_error", "network"):
        assert category in invoke.TRANSIENT
    else:
        assert category not in invoke.TRANSIENT


def test_output_present_is_never_reclassified():
    # 出力があるのに失敗（is_error など）は agent_failed のまま（文を読まない）
    with pytest.raises(invoke.AgentError) as e:
        invoke.parse_result(1, result(is_error=True, result="Overloaded"))
    assert e.value.category == "agent_failed"


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
    runner = FakeRunner(Done(1, UNCLASSIFIED, "error: something", remove_failed=True))
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
    assert (e.value.models, e.value.usage) == ((), None)


def test_timeout_keeps_usage_when_result_was_printed(schema):
    runner = FakeRunner(Done(None, result(num_turns=7)))
    with pytest.raises(invoke.AgentError) as e:
        call(runner, schema)
    assert e.value.category == "timeout"
    assert e.value.usage.num_turns == 7
    assert e.value.models == ("claude-opus-4-5-20251101",)


def test_unreadable_schema_is_bad_schema(tmp_path):
    runner = FakeRunner(Done(0, result()))
    with pytest.raises(invoke.AgentError) as e:
        call(runner, tmp_path)  # フォルダは読めない
    assert e.value.category == "bad_schema"
    assert str(tmp_path) not in e.value.detail
    assert runner.calls == []


def test_missing_skill_in_image_is_its_own_category(schema):
    # agent-run がイメージにスキルを見つけられなかった（イメージを作り直していない）
    runner = FakeRunner(Done(78, "", "agent-run: スキル tr-daily-evaluate がイメージにない"))
    with pytest.raises(invoke.AgentError) as e:
        call(runner, schema)
    assert e.value.category == "missing_skill"
    assert "イメージにない" in e.value.detail


def test_claude_exit_78_with_result_is_parsed_normally(schema):
    # claude の結果の JSON があれば、終了コードが 78 でも agent-run の失敗とはみなさない
    runner = FakeRunner(Done(78, UNCLASSIFIED))
    with pytest.raises(invoke.AgentError) as e:
        call(runner, schema)
    assert e.value.category == "agent_failed"


def test_status_is_not_read_unless_is_error():
    data = {**json.loads(NOT_LOGGED_IN), "is_error": False, "api_error_status": 529}
    assert invoke.classify_failure(data) == "agent_failed"


@pytest.mark.parametrize("reason", ["max_turns", "completed", None])
def test_result_text_is_read_only_for_api_errors(reason):
    # スキーマに合わない出力・ターン数の超過などの文は、手がかりに合っても読まない
    data = {**json.loads(NOT_LOGGED_IN), "terminal_reason": reason, "result": "limit reached"}
    assert invoke.classify_failure(data) == "agent_failed"
