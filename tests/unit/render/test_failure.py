from datetime import UTC, date, datetime

import cmarkgfm
import pytest

from tech_researcher.periods import daily_target, weekly_target
from tech_researcher.render.failure import (
    FOOTER_ERROR,
    FOOTER_REJECTED,
    NO_INVESTIGATION,
    Counts,
    Investigation,
    SourceResult,
    code_fence,
    rejected_report,
    run_error_report,
    secret_stop_report,
    select_evidence,
)

DAILY = daily_target(date(2026, 10, 24), 1)
WEEKLY = weekly_target(date(2026, 10, 26), 7)
RUN_AT = datetime(2026, 10, 23, 21, 0, tzinfo=UTC)  # JST 2026-10-24 06:00
RUN = "20261024-060000-a1b2"
PERIOD = "2026-10-23（金）00:00 〜 2026-10-24（土）00:00（JST）"
COUNTS = Counts(20, 10, {"URL 不正": 2, "期間外": 3, "なし": 0}, 5)
INV = Investigation(
    "06:00 に接続が失敗した。",
    "DNS の不具合の可能性が高い。",
    "高",
    ("収集段階のログ", "直近7日の履歴"),
    ("DNS 設定を確認する",),
    (("06:00:12 collect error=dns",),),
)


def rejected(inv=INV):
    return rejected_report(
        DAILY,
        RUN,
        RUN_AT,
        ["D6(a) 出力の形式が不正"],
        COUNTS,
        [("D6(a)", False), ("D2", True)],
        inv,
    )


def run_error(inv=INV, stage="collect"):
    sources = [SourceResult("Hacker News", False, "名前解決の失敗", 3)]
    return run_error_report(DAILY, RUN, RUN_AT, stage, "名前解決に失敗した", (3, 3), sources, inv)


def test_rejected_title_and_labels():
    report = rejected()
    assert report.title == "[不合格][日次] 2026-10-23（金）のレポート（2026-10-24 06:00 実行）"
    assert report.labels == ("type:rejected", "cadence:daily")
    weekly = rejected_report(WEEKLY, RUN, RUN_AT, ["x"], COUNTS, [], None)
    assert weekly.title == "[不合格][週次] 2026-10-26（月）号のレポート（2026-10-24 06:00 実行）"
    assert weekly.labels == ("type:rejected", "cadence:weekly")


def test_rejected_body_has_template_items():
    body = rejected().body
    assert f"| 対象期間 | {PERIOD} |" in body
    assert f"| 実行ID | `{RUN}` |" in body
    assert "| 実行日時 | 2026-10-24 06:00 JST |" in body
    assert "| 不合格となった検証項目 | D6\\(a\\) 出力の形式が不正 |" in body
    assert (
        "| 件数 | 収集 20 件／AI の出力 10 件／除外 5 件（URL 不正 2 件、期間外 3 件）／掲載 5 件 |"
        in body
    )
    for heading in ("### 発生した事象", "### 推定される原因と根拠", "### 調査内容", "### 対策案"):
        assert heading in body
    assert "確度：高。" in body
    assert "<summary>検証結果の詳細</summary>\n\n| 検証項目 | 結果 |" in body
    assert "| D6\\(a\\) | 不合格 |" in body
    assert "\n\n</details>" in body
    assert body.endswith(f"---\n{FOOTER_REJECTED}\n")
    assert "<!--" not in body  # 識別子は付けない


def test_run_error_title_labels_body():
    report = run_error()
    assert report.title == "[実行エラー][日次] 2026-10-23（金）（① 収集・2026-10-24 06:00 実行）"
    assert report.labels == ("type:run-error", "cadence:daily")
    body = report.body
    assert "| 失敗した処理段階 | ① 収集 |" in body
    assert "| エラーの概要 | 名前解決に失敗した |" in body
    assert "| 再試行回数 | 3 回（上限 3 回） |" in body
    assert "<summary>情報源ごとの取得結果</summary>" in body
    assert "| Hacker News | 失敗 | 名前解決の失敗 | 3 回 |" in body
    assert body.endswith(f"---\n{FOOTER_ERROR}\n")
    weekly = run_error_report(WEEKLY, RUN, RUN_AT, "post", "x", (0, 2), [], None)
    assert weekly.title == "[実行エラー][週次] 2026-10-26（月）号（⑤ 投稿・2026-10-24 06:00 実行）"


def test_run_error_rejects_unknown_stage():
    with pytest.raises(ValueError):
        run_error(stage="nope")


def test_unavailable_template_has_only_fixed_items():
    report = run_error(None)
    assert report.title.endswith("（① 収集・2026-10-24 06:00 実行）")  # タイトルは日時を残す
    assert report.body == (
        "| 項目 | 値 |\n|---|---|\n"
        f"| 対象期間 | {PERIOD} |\n| 失敗した処理段階 | ① 収集 |\n"
        "| エラーの概要 | 名前解決に失敗した |\n| 再試行回数 | 3 回（上限 3 回） |\n\n"
        f"{NO_INVESTIGATION}\n"
    )
    body = rejected(None).body
    assert body == (
        "| 項目 | 値 |\n|---|---|\n"
        f"| 対象期間 | {PERIOD} |\n| 不合格となった検証項目 | D6\\(a\\) 出力の形式が不正 |\n\n"
        f"{NO_INVESTIGATION}\n"
    )
    for text in (run_error(None).body, body):
        assert RUN not in text and "実行日時" not in text and "<details>" not in text
        assert "---\n" not in text.split(NO_INVESTIGATION)[1]


def test_secret_stop_is_fixed_text_without_run_info():
    report = secret_stop_report(WEEKLY, "post")
    assert report.title == "[実行エラー][週次] 2026-10-26（月）号（公開停止）"
    assert report.labels == ("type:run-error", "cadence:weekly")
    assert report.body == (
        "秘匿情報の可能性がある文字列を検出したため、Issue の公開を停止しました。\n\n"
        "| 項目 | 値 |\n|---|---|\n"
        "| 対象期間 | 2026-10-19（月）00:00 〜 2026-10-26（月）00:00（JST） |\n"
        "| 処理段階 | ⑤ 投稿 |\n"
    )
    daily = secret_stop_report(DAILY, "validate")
    assert daily.title == "[実行エラー][日次] 2026-10-23（金）（公開停止）"
    assert "③ 検証・検査" in daily.body
    assert "06:00" not in daily.title and "06:00" not in daily.body and RUN not in daily.body


def test_secret_stop_rejects_other_stage():
    with pytest.raises(ValueError):
        secret_stop_report(DAILY, "collect")


def test_rejected_never_shows_excluded_contents_or_full_output():
    # 件数の理由には、コードの分類だけが入る（項目の見出しなどは渡す口がない）
    body = rejected().body
    assert "URL 不正 2 件" in body
    assert "headline" not in body


def test_investigation_text_is_sanitized():
    inv = Investigation(
        "@octocat に通知 #12",
        "# 見出し\n**偽**",
        "中",
        ("owner/repo#5",),
        ("- 項目",),
    )
    body = run_error(inv).body
    assert "`@octocat`" in body and "`#12`" in body and "`owner/repo#5`" in body
    assert "\\# 見出し" in body and "\\*\\*偽\\*\\*" in body
    assert "- \\- 項目" in body


@pytest.mark.parametrize(
    "text", ["https://example.com", "www.example.com", "[a](b)", "<img src=x>", "<!-- x -->"]
)
def test_investigation_with_link_or_html_falls_back_to_fixed_form(text):
    report = run_error(Investigation(text, "c", "高", (), ()))
    assert report.body == run_error(None).body
    assert rejected(Investigation("e", text, "高", (), ())).body == rejected(None).body


def test_investigation_confidence_and_evidence_limits():
    assert run_error(Investigation("e", "c", "不明", (), ())).body == run_error(None).body
    too_long = (tuple(f"l{i}" for i in range(11)),)
    with pytest.raises(ValueError):
        run_error(Investigation("e", "c", "高", (), (), too_long))
    with pytest.raises(ValueError):
        run_error(Investigation("e", "c", "高", (), (), (("a",),) * 4))


def test_code_fence_longer_than_longest_backtick_run():
    assert code_fence(["plain"]) == "```text\nplain\n```"
    assert code_fence(["a ``` b"]).startswith("````text\n")
    block = code_fence(["x", "``````y", "``"])
    assert block.startswith("`" * 7 + "text\n") and block.endswith("\n" + "`" * 7)


def test_code_fence_splits_embedded_newlines():
    assert code_fence(["a\nb"]) == "```text\na\nb\n```"


def test_evidence_cannot_close_fence_when_rendered():
    inv = Investigation(
        "e", "c", "高", ("x",), ("y",), (("ok", "``` # 見出し", "<b>@octocat</b>"),)
    )
    html = cmarkgfm.github_flavored_markdown_to_html(run_error(inv).body)
    assert '<pre lang="text"><code>' in html
    assert "<h1>" not in html and "<b>" not in html
    assert "&lt;b&gt;@octocat&lt;/b&gt;" in html
    assert "```" in html.split('<pre lang="text">')[1].split("</pre>")[0]


def test_select_evidence_drops_unknown_orders_and_limits():
    log = {f"L{i:03d}": f"line {i}" for i in range(1, 40)}
    ids = ["L999", "L003", "L002", "L010", "L011"] + [f"L{i:03d}" for i in range(20, 35)]
    blocks = select_evidence(log, ids)
    assert blocks[0] == ("line 2", "line 3")
    assert blocks[1] == ("line 10", "line 11")
    assert len(blocks[2]) == 10  # 10行で打ち切る
    assert len(blocks) == 3  # 20〜29、30〜34 の4つ目以降は捨てる
    assert select_evidence(log, ["nope"]) == ()


def test_rendered_html_tables_and_details():
    unsafe = cmarkgfm.cmark.Options.CMARK_OPT_UNSAFE  # <details> を残して、GitHub の描画に近づける
    html = cmarkgfm.github_flavored_markdown_to_html(rejected().body, options=unsafe)
    assert html.count("<table>") == 2  # 概要と、<details> の中の表
    assert "<details>" in html and "<h2>概要</h2>" in html


def test_evidence_limit_counts_embedded_newlines():
    with pytest.raises(ValueError):
        run_error(Investigation("e", "c", "高", (), (), (("a\n" * 15,),)))
    blocks = select_evidence({"L1": "a\n" * 15}, ["L1"])
    assert [len(b) for b in blocks] == [10, 5]


def test_run_id_must_be_safe():
    with pytest.raises(ValueError):
        run_error_report(DAILY, "r|x", RUN_AT, "collect", "x", (0, 1), [], INV)


def test_config_derived_values_are_neutralized_in_tables():
    attack = "@octocat #12|\nGH-3 [x](y)"
    sources = [SourceResult(attack, False, attack, 1)]
    report = run_error_report(DAILY, RUN, RUN_AT, "collect", attack, (1, 1), sources, INV)
    counts = Counts(1, 1, {attack: 1}, 0)
    rej = rejected_report(DAILY, RUN, RUN_AT, [attack], counts, [(attack, False)], INV)
    for body in (report.body, rej.body):
        html = cmarkgfm.github_flavored_markdown_to_html(body)
        assert "@octocat" not in body.replace("＠octocat", "")
        assert "<a " not in html
    assert "| 項目 | 値 |" in report.body


def test_sanitized_reference_forms_in_investigation():
    inv = Investigation(
        "GH-34 と abcdef1 と owner/repo@abcdef1 と @org/team",
        "> 引用 | 表",
        "低",
        ("1. 番号",),
        ("x",),
    )
    body = run_error(inv).body
    for ref in ("`GH-34`", "`abcdef1`", "`owner/repo@abcdef1`", "`@org/team`"):
        assert ref in body
    assert "\\> 引用 \\| 表" in body and "1\\. 番号" in body
