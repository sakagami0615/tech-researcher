"""worker.env の確認とマスク用の秘匿値（envfiles.py。D-05・structure.md 3.7）。

置き場所と権限の確認は、統括スクリプトの起動の検査（test_orchestrator.py）で確かめる。
"""

import os

import pytest

from tech_researcher.envfiles import remove_snapshot, secret_values, snapshot


@pytest.fixture(autouse=True)
def private_umask():
    """worker.env は 600 でないと読まないので、テストで作るファイルを持ち主だけの権限にする。"""
    previous = os.umask(0o077)
    yield
    os.umask(previous)


def test_secret_values_follow_name_rule(tmp_path):
    path = tmp_path / "worker.env"
    path.write_text(
        "# コメント\n"
        "  # 字下げしたコメント\n"
        "\t# タブで字下げしたコメント\n"
        " \t \n"
        "\n"
        "GITHUB_TOKEN=fake-collect\r\n"  # CRLF も読む
        "TR_PROJECTS_PAT=fake-pat#not-comment\n"
        "TR_ISSUE_REPO=example/repo\n"  # 投稿先は秘匿値ではない（D-20）
        "TR_EMPTY_TOKEN=\n",
        encoding="utf-8",
    )
    assert secret_values(path) == ["fake-collect", "fake-pat#not-comment", ""]


@pytest.mark.parametrize(
    ("line", "number"),
    [
        (
            'TR_ISSUE_TOKEN="fake-quoted"',
            2,
        ),  # 引用符（閉じていないと compose が値ごとエラー文に出す）
        ("TR_ISSUE_TOKEN='fake-quoted'", 2),
        ("TR_ISSUE_TOKEN=${MISSING:?fake-in-message}", 2),  # compose が展開し、エラー文に出す
        ("TR_ISSUE_TOKEN=fake\\escaped", 2),
        ("TR_ISSUE_TOKEN=fake with space", 2),
        ("TR_ISSUE_TOKEN = fake-spaced", 2),
        ("export TR_ISSUE_TOKEN=fake-exported", 2),
        ("TR_ISSUE_TOKEN: fake-yaml", 2),
        ("fake-pasted/without+name=", 2),  # 名前のない行（compose は行ごとエラー文に出す）
        ("\x1c# fake-control-indent", 2),  # compose は \x1c を字下げとみなさない
    ],
)
def test_rejects_lines_compose_would_misread(tmp_path, line, number):
    path = tmp_path / "worker.env"
    path.write_text(f"GITHUB_TOKEN=fake-collect\n{line}\n", encoding="utf-8")
    with pytest.raises(ValueError) as e:
        secret_values(path)
    message = str(e.value)
    assert f"{number} 行目" in message
    assert "fake" not in message and str(tmp_path) not in message


def test_secret_values_error_has_no_path(tmp_path):
    with pytest.raises(ValueError) as e:
        secret_values(tmp_path)  # フォルダは読めない
    assert str(tmp_path) not in str(e.value)


def test_rejects_non_utf8(tmp_path):
    path = tmp_path / "worker.env"
    path.write_bytes(b"GITHUB_TOKEN=\xff\n")
    with pytest.raises(ValueError):
        secret_values(path)


def _secrets(tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    (secrets / "worker.env").write_text("GITHUB_TOKEN=fake-collect\n")
    (secrets / "worker.env").chmod(0o600)
    return secrets


def test_snapshot_is_private_copy(tmp_path):
    secrets = _secrets(tmp_path)
    copy = snapshot(secrets, "run1")
    assert copy == secrets / ".run-run1"
    assert copy.stat().st_mode & 0o777 == 0o700
    assert (copy / "worker.env").stat().st_mode & 0o777 == 0o600
    assert (copy / "worker.env").read_text() == "GITHUB_TOKEN=fake-collect\n"
    remove_snapshot(copy)
    assert [p.name for p in secrets.iterdir()] == ["worker.env"]


def test_snapshot_keeps_existing_folder_of_same_name(tmp_path):
    # 同じ名前のフォルダはほかの実行の写しかもしれないので、作れなくても消さない
    secrets = _secrets(tmp_path)
    other = snapshot(secrets, "run1")
    with pytest.raises(ValueError) as e:
        snapshot(secrets, "run1")
    assert str(secrets) not in str(e.value)
    assert (other / "worker.env").exists()


def test_snapshot_failure_leaves_nothing(tmp_path):
    secrets = _secrets(tmp_path)
    (secrets / "worker.env").unlink()
    (secrets / "worker.env").symlink_to(tmp_path / "elsewhere")  # リンクはたどらない
    with pytest.raises(ValueError) as e:
        snapshot(secrets, "run1")
    assert str(secrets) not in str(e.value)
    assert sorted(p.name for p in secrets.iterdir()) == ["worker.env"]


def test_remove_snapshot_only_removes_snapshot_folders(tmp_path):
    secrets = _secrets(tmp_path)
    remove_snapshot(secrets)
    assert (secrets / "worker.env").exists()


def test_snapshot_rejects_fifo_without_blocking(tmp_path):
    # 確かめた後に FIFO に差し替わっても、待たずに止める
    secrets = _secrets(tmp_path)
    (secrets / "worker.env").unlink()
    os.mkfifo(secrets / "worker.env", 0o600)
    with pytest.raises(ValueError):
        snapshot(secrets, "run1")
    assert sorted(p.name for p in secrets.iterdir()) == ["worker.env"]


def test_snapshot_rejects_loose_permissions_found_after_check(tmp_path):
    secrets = _secrets(tmp_path)
    (secrets / "worker.env").chmod(0o644)
    with pytest.raises(ValueError, match="いまは 644"):  # 理由をそのまま伝える
        snapshot(secrets, "run1")
    assert sorted(p.name for p in secrets.iterdir()) == ["worker.env"]


def test_rejects_bom_with_clear_message(tmp_path):
    path = tmp_path / "worker.env"
    path.write_text("\ufeffGITHUB_TOKEN=fake-collect\n", encoding="utf-8")
    with pytest.raises(ValueError) as e:
        secret_values(path)
    assert "BOM" in str(e.value)
