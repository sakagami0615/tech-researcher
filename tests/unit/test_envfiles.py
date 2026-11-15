"""worker.env の確認とマスク用の秘匿値（envfiles.py。D-05・structure.md 3.7）。

置き場所と権限の確認は、統括スクリプトの起動の検査（test_orchestrator.py）で確かめる。
"""

import pytest

from tech_researcher.envfiles import remove_snapshot, secret_values, snapshot


def test_secret_values_follow_name_rule(tmp_path):
    path = tmp_path / "worker.env"
    path.write_text(
        "# コメント\n"
        "GITHUB_TOKEN=fake-collect\n"
        "export TR_ISSUE_TOKEN = 'fake-issue'\n"
        'TR_PROJECTS_PAT="fake-pat" # コメント\n'
        "TR_YAML_TOKEN: 'fake-yaml'\n"  # compose は `名前: 値` も受け付ける
        "TR_ISSUE_REPO=example/repo\n"  # 投稿先は秘匿値ではない（D-20）
        "TR_EMPTY_TOKEN=\n"
        "no equals sign\n"
        # compose が値ごとエラー文に出す行。引用符が閉じないので、続く行も伏せる側に倒す
        'TR_BROKEN_KEY="fake-unterminated\n'
        "fake-after-broken\n",
        encoding="utf-8",
    )
    values = set(secret_values(path))
    expected = {"fake-collect", "fake-issue", "fake-pat", "fake-yaml"}
    assert expected | {"fake-unterminated", "fake-after-broken"} <= values
    assert "example/repo" not in values
    assert not any("example" in v for v in values)


def test_secret_values_handle_crlf_and_bad_bytes(tmp_path):
    path = tmp_path / "worker.env"
    path.write_bytes(b"GITHUB_TOKEN=fake-crlf\r\nX=\xff\r\n")
    assert "fake-crlf" in secret_values(path)


def test_secret_values_error_has_no_path(tmp_path):
    with pytest.raises(ValueError) as e:
        secret_values(tmp_path)  # フォルダは読めない
    assert str(tmp_path) not in str(e.value)


def _secrets(tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    (secrets / "worker.env").write_text("GITHUB_TOKEN=fake-collect\n")
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


def test_secret_values_include_multiline_quoted_value(tmp_path):
    path = tmp_path / "worker.env"
    path.write_text(
        "TR_MULTI_KEY='fake-first\n  fake-second\nfake-third'\nTR_ISSUE_REPO=example/repo\n",
        encoding="utf-8",
    )
    values = set(secret_values(path))
    assert {"fake-first", "fake-second", "fake-third"} <= values
    assert "example/repo" not in values  # 引用符が閉じた後の行は名前の規則に戻る


def test_escaped_double_quote_does_not_close_value(tmp_path):
    path = tmp_path / "worker.env"
    path.write_text(
        'TR_MULTI_KEY="fake-first\\"\nfake-second \\" x\nfake-third"\nTR_ISSUE_REPO=example/repo\n',
        encoding="utf-8",
    )
    values = set(secret_values(path))
    assert {'fake-second \\" x', "fake-third"} <= values
    assert "example/repo" not in values


def test_short_or_symbol_only_lines_are_not_secret_values(tmp_path):
    # 二重引用符の JSON。`\"` で閉じたとみなさず、最後の行まで続きとして読む
    path = tmp_path / "worker.env"
    path.write_text(
        'TR_SA_KEY="{\n  \\"type\\": \\"fake-service\\",\n  \\"x\\": 1\n}"\n'
        "TR_ISSUE_REPO=example/repo\n",
        encoding="utf-8",
    )
    values = set(secret_values(path))
    assert any("fake-service" in v for v in values)
    assert any('\\"x\\": 1' in v for v in values)
    assert not {"{", "}", '"{', '}"', "1"} & values
    assert "example/repo" not in values


def test_escaped_single_quote_does_not_close_value(tmp_path):
    path = tmp_path / "worker.env"
    path.write_text("TR_KEY='fake-abc\\'\nfake-second'\nTR_ISSUE_REPO=example/repo\n")
    values = set(secret_values(path))
    assert "fake-second" in values
    assert "example/repo" not in values
