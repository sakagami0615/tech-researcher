"""worker.env の確認とマスク用の秘匿値（envfiles.py。D-05・structure.md 3.7）。

置き場所と権限の確認は、統括スクリプトの起動の検査（test_orchestrator.py）で確かめる。
"""

from tech_researcher.envfiles import secret_values


def test_secret_values_follow_name_rule(tmp_path):
    path = tmp_path / "worker.env"
    path.write_text(
        "# コメント\n"
        "GITHUB_TOKEN=fake-collect\n"
        "export TR_ISSUE_TOKEN = 'fake-issue'\n"
        'TR_PROJECTS_PAT="fake-pat" # コメント\n'
        'TR_BROKEN_KEY="fake-unterminated\n'  # compose が値ごとエラー文に出す行
        "TR_ISSUE_REPO=example/repo\n"  # 投稿先は秘匿値ではない（D-20）
        "TR_EMPTY_TOKEN=\n"
        "no equals sign\n",
        encoding="utf-8",
    )
    values = set(secret_values(path))
    assert {"fake-collect", "fake-issue", "fake-pat", "fake-unterminated"} <= values
    assert "example/repo" not in values
    assert not any("example" in v for v in values)


def test_secret_values_handle_crlf_and_bad_bytes(tmp_path):
    path = tmp_path / "worker.env"
    path.write_bytes(b"GITHUB_TOKEN=fake-crlf\r\nX=\xff\r\n")
    assert "fake-crlf" in secret_values(path)
