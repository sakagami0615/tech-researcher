"""agent コンテナの agent-run（docker/agent-run.sh）。

イメージの中のスキルの置き場所（/app/.claude/skills）を一時フォルダに置き換えた写しを、
偽の claude を PATH の先頭に置いて直接動かす。invoke.py がその終了コードを missing_skill に
分けることは tests/unit/agent/test_invoke.py で確かめる。
"""

import os
import subprocess
from pathlib import Path

import pytest

from tech_researcher.agent import invoke

SCRIPT = Path(__file__).parents[2] / "docker" / "agent-run.sh"
IMAGE_SKILLS = "/app/.claude/skills"


@pytest.fixture
def run(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.count(IMAGE_SKILLS) == 1
    script = tmp_path / "agent-run"
    script.write_text(text.replace(IMAGE_SKILLS, str(skills)), encoding="utf-8")
    script.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "claude-args"
    claude = bin_dir / "claude"
    claude.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{record}"\necho result\n')
    claude.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

    def _run(*args: str) -> tuple[subprocess.CompletedProcess, list[str] | None]:
        done = subprocess.run([str(script), *args], env=env, capture_output=True, text=True)
        called = record.read_text().splitlines() if record.exists() else None
        return done, called

    _run.skills = skills
    return _run


def test_missing_skill_stops_before_claude(run):
    done, called = run("tr-daily-evaluate", "-p", "/tr-daily-evaluate")
    assert done.returncode == invoke.MISSING_SKILL_EXIT == 78
    assert called is None  # claude を起動しない
    assert done.stdout == ""
    assert "イメージにない" in done.stderr


def test_existing_skill_runs_claude_with_remaining_args(run):
    skill = run.skills / "tr-daily-evaluate"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: tr-daily-evaluate\n---\n")
    done, called = run("tr-daily-evaluate", "-p", "/tr-daily-evaluate", "--tools", "")
    assert done.returncode == 0
    assert done.stdout == "result\n"
    assert called == ["-p", "/tr-daily-evaluate", "--tools", ""]  # スキル名は渡さない


@pytest.mark.parametrize("name", ["", "../etc", "Tr-Daily", "a/b", "x y"])
def test_bad_skill_name_is_rejected(run, name):
    done, called = run(name, "-p", "/x")
    assert done.returncode == 64
    assert called is None
