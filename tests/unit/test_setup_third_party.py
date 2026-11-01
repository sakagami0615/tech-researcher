"""bin/setup-third-party.sh の検査（D-01）。ローカルの偽の last30days-skill を使う。"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "bin" / "setup-third-party.sh"
SUBMODULE = "third_party/last30days"

# 偽の last30days-skill の中身。実物と同じく、置かないファイル（指示ファイルなど）を含める
UPSTREAM_FILES = {
    "LICENSE": "MIT",
    "README.md": "readme",
    "CLAUDE.md": "instructions",
    "AGENTS.md": "instructions",
    ".github/copilot-instructions.md": "instructions",
    "skills/last30days/SKILL.md": "instructions",
    "skills/last30days/scripts/last30days.py": "print('ok')",
    "skills/last30days/scripts/lib/util.py": "",
}


@pytest.fixture
def git_env(tmp_path: Path) -> dict[str, str]:
    """利用者の Git の設定に左右されない環境。ローカルの submodule を許可する。"""
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "protocol.file.allow",
        "GIT_CONFIG_VALUE_0": "always",
    }


def git(env: dict[str, str], cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def make_parent_clone(
    tmp_path: Path, env: dict[str, str], files: dict[str, str], *clone_args: str
) -> Path:
    """偽の last30days-skill を submodule に持つ親リポジトリを作り、clone する。"""
    upstream = tmp_path / "upstream"
    for rel, text in files.items():
        (upstream / rel).parent.mkdir(parents=True, exist_ok=True)
        (upstream / rel).write_text(text)
    git(env, upstream, "init", "-q")
    git(env, upstream, "add", ".")
    git(env, upstream, "commit", "-q", "-m", "upstream")

    parent = tmp_path / "parent"
    (parent / "bin").mkdir(parents=True)
    shutil.copy2(SCRIPT, parent / "bin" / SCRIPT.name)
    git(env, parent, "init", "-q")
    git(env, parent, "submodule", "add", "-q", str(upstream), SUBMODULE)
    git(env, parent, "add", ".")
    git(env, parent, "commit", "-q", "-m", "parent")

    clone = tmp_path / "clone"
    git(env, tmp_path, "clone", "-q", *clone_args, str(parent), str(clone))
    return clone


def run_setup(clone: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # スクリプトは自分の場所からリポジトリのルートを求めるので、ルートの外から起動する
    script = clone / "bin" / SCRIPT.name
    return subprocess.run(
        [str(script)], cwd=clone.parent, env=env, capture_output=True, text=True, encoding="utf-8"
    )


def worktree_files(clone: Path) -> set[str]:
    root = clone / SUBMODULE
    return {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if not p.is_dir() and ".git" not in p.relative_to(root).parts
    }


EXPECTED_FILES = {
    "LICENSE",
    "skills/last30days/scripts/last30days.py",
    "skills/last30days/scripts/lib/util.py",
}


def test_places_only_scripts_and_license(tmp_path: Path, git_env: dict[str, str]) -> None:
    clone = make_parent_clone(tmp_path, git_env, UPSTREAM_FILES)

    result = run_setup(clone, git_env)

    assert result.returncode == 0, result.stderr
    assert worktree_files(clone) == EXPECTED_FILES


def test_is_idempotent(tmp_path: Path, git_env: dict[str, str]) -> None:
    clone = make_parent_clone(tmp_path, git_env, UPSTREAM_FILES)

    assert run_setup(clone, git_env).returncode == 0
    result = run_setup(clone, git_env)

    assert result.returncode == 0, result.stderr
    assert worktree_files(clone) == EXPECTED_FILES


def test_recovers_after_recursive_clone(tmp_path: Path, git_env: dict[str, str]) -> None:
    # 再帰 clone では全ファイルが置かれるが、スクリプトを流せば scripts と LICENSE だけになる
    clone = make_parent_clone(tmp_path, git_env, UPSTREAM_FILES, "--recurse-submodules")
    assert "CLAUDE.md" in worktree_files(clone)

    result = run_setup(clone, git_env)

    assert result.returncode == 0, result.stderr
    assert worktree_files(clone) == EXPECTED_FILES


def test_keeps_new_sha_after_update_procedure(tmp_path: Path, git_env: dict[str, str]) -> None:
    # README の SHA を変える手順：fetch・checkout・git add の後に流すと、新しい SHA のまま残る
    clone = make_parent_clone(tmp_path, git_env, UPSTREAM_FILES)
    assert run_setup(clone, git_env).returncode == 0
    upstream = tmp_path / "upstream"
    (upstream / "skills/last30days/scripts/new.py").write_text("")
    git(git_env, upstream, "add", ".")
    git(git_env, upstream, "commit", "-q", "-m", "new")
    new_sha = git(git_env, upstream, "rev-parse", "HEAD")
    sub = clone / SUBMODULE
    git(git_env, sub, "fetch", "-q")
    git(git_env, sub, "checkout", "-q", new_sha)
    git(git_env, clone, "add", SUBMODULE)

    result = run_setup(clone, git_env)

    assert result.returncode == 0, result.stderr
    assert git(git_env, sub, "rev-parse", "HEAD") == new_sha
    assert worktree_files(clone) == EXPECTED_FILES | {"skills/last30days/scripts/new.py"}


@pytest.mark.parametrize(
    "instruction_file",
    [
        "skills/last30days/scripts/AGENTS.md",
        "skills/last30days/scripts/lib/claude.md",
        "skills/last30days/scripts/.cursorrules",
        "skills/last30days/scripts/.claude/settings.json",
    ],
)
def test_fails_when_instruction_file_remains(
    tmp_path: Path, git_env: dict[str, str], instruction_file: str
) -> None:
    clone = make_parent_clone(tmp_path, git_env, {**UPSTREAM_FILES, instruction_file: "x"})

    result = run_setup(clone, git_env)

    assert result.returncode != 0
    assert "指示ファイルが残っています" in result.stderr
    assert instruction_file in result.stderr


def test_fails_on_file_outside_kept_paths(tmp_path: Path, git_env: dict[str, str]) -> None:
    clone = make_parent_clone(tmp_path, git_env, UPSTREAM_FILES)
    assert run_setup(clone, git_env).returncode == 0
    (clone / SUBMODULE / "stray.py").write_text("")

    result = run_setup(clone, git_env)

    assert result.returncode != 0
    assert "対象外のファイルがあります" in result.stderr


def test_fails_when_collector_is_missing(tmp_path: Path, git_env: dict[str, str]) -> None:
    files = {k: v for k, v in UPSTREAM_FILES.items() if not k.endswith("last30days.py")}
    clone = make_parent_clone(tmp_path, git_env, files)

    result = run_setup(clone, git_env)

    assert result.returncode != 0
    assert "必要なファイルがありません" in result.stderr
