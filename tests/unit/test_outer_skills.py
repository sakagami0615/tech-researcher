"""外側のスキル（/tr-daily・/tr-weekly。structure.md 3.4・3.5）の検査。

文書のコマンドが実行できる形であることを、偽の bin/tr-run で確かめる
（引数の渡り方と要約の受け取り）。
スキルの実際の振る舞い（引数を断る・報告の形）は本物の Claude Code でしか確かめられない。
本物の Claude Code・Docker・トークンは使わない。
"""

import dataclasses
import json
import re
import subprocess
from pathlib import Path

import pytest

from tech_researcher.orchestrator import Summary

REPO = Path(__file__).resolve().parents[2]
SKILLS = REPO / ".claude" / "skills"
PLACEHOLDER = "${CLAUDE_PROJECT_DIR}"
COMMAND = re.compile(r"`(\$\{CLAUDE_PROJECT_DIR\}/bin/tr-run [^`]+)`")

# 偽の bin/tr-run：引数を記録し、統括スクリプトと同じ形の要約を1行で出す
FAKE_TR_RUN = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$(dirname "$0")/../calls.txt"
cat "$(dirname "$0")/../summary.json"
"""


def _skill(kind: str) -> tuple[dict[str, str], str]:
    text = (SKILLS / f"tr-{kind}" / "SKILL.md").read_text(encoding="utf-8")
    _, front, body = text.split("---\n", 2)
    meta = dict(line.split(": ", 1) for line in front.strip().splitlines())
    return meta, body


def _commands(kind: str) -> list[str]:
    """本文に書かれた実行コマンド（${CLAUDE_PROJECT_DIR} から始まるもの）。"""
    return COMMAND.findall(_skill(kind)[1])


@pytest.mark.parametrize("kind", ["daily", "weekly"])
def test_frontmatter(kind):
    meta, _ = _skill(kind)
    assert meta["name"] == f"tr-{kind}"
    assert meta["disable-model-invocation"] == "true"
    assert meta["argument-hint"] == '"[--dry-run]"'
    base = f"Bash({PLACEHOLDER}/bin/tr-run {kind}"
    # 許可するのは2つのコマンドだけ
    assert meta["allowed-tools"] == f"{base}) {base} --dry-run)"


@pytest.mark.parametrize("kind", ["daily", "weekly"])
def test_body_commands_match_allowed_tools(kind):
    meta, body = _skill(kind)
    commands = _commands(kind)
    assert commands == [
        f"{PLACEHOLDER}/bin/tr-run {kind}",
        f"{PLACEHOLDER}/bin/tr-run {kind} --dry-run",
    ]
    for command in commands:
        assert f"Bash({command})" in meta["allowed-tools"]
    # 内側のスキル・共通の起動スキルを呼ばせない
    for inner in ("tr-daily-evaluate", "tr-weekly-analyze", "tr-investigate"):
        assert inner not in body
    assert "Skill" not in meta["allowed-tools"]


@pytest.mark.parametrize("kind", ["daily", "weekly"])
def test_body_has_no_injection_or_shell_metacharacters(kind):
    _, body = _skill(kind)
    assert body.count("$ARGUMENTS") == 1
    assert "!`" not in body
    assert "${" not in body.replace(PLACEHOLDER, "")
    for command in _commands(kind):
        for meta in (";", "&&", "|", "$(", "`", ">", "<", "\n"):
            assert meta not in command


def test_only_two_outer_skills():
    # 共通の起動スキルは作らない。内側のスキルは .claude/skills/ の外（D-19）
    assert sorted(p.name for p in SKILLS.iterdir()) == ["tr-daily", "tr-weekly"]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "bin").mkdir()
    fake = tmp_path / "bin" / "tr-run"
    fake.write_text(FAKE_TR_RUN, encoding="utf-8")
    fake.chmod(0o755)
    return tmp_path


def _summary(kind: str, **fields) -> Summary:
    base = {"run_id": "r1", "cadence": kind, "target": "2026-11-22", "result": "report"}
    return Summary(**{**base, **fields})


@pytest.mark.parametrize("kind", ["daily", "weekly"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_documented_command_calls_entrypoint_once_and_returns_summary(project, kind, dry_run):
    summary = _summary(
        kind,
        result="dry_run" if dry_run else "report",
        output_dir="/data/dry-run/x" if dry_run else None,
    )
    (project / "summary.json").write_text(
        json.dumps(dataclasses.asdict(summary), ensure_ascii=False), encoding="utf-8"
    )
    command = _commands(kind)[1 if dry_run else 0].replace(PLACEHOLDER, str(project))
    done = subprocess.run(command.split(), capture_output=True, text=True, check=True)

    calls = (project / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert calls == [f"{kind} --dry-run" if dry_run else kind]
    received = json.loads(done.stdout)
    assert received == dataclasses.asdict(summary)
