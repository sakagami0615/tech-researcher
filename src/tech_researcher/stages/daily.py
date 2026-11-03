"""日次の ② の入力の用意と、③ 検証・検査（D-04・D-07。worker で動く）。

- ① の後に、collected.json から ② の入力（agent-input.json）を作る。統括スクリプトがそれを
  agent に標準入力で渡し、構造化出力を agent-output.json に保存する
- ③ は、agent に渡したコピーではなく、worker の collected.json から同じ入力を作り直して照合する
  （D-04）。いまは D6(a) の最小の形と D6(b) の ID だけを検査する。正式なスキーマ・D1・期間内の
  出来事の欠落は #19、D2・D3・文字数は #20、D4 は #21 で足す
- 秘匿情報を検出したら Issue の本文を残さない（US-E7-05）。⑤ の直前の検査は ⑤ の Issue で足す

項目の ID は、DB への保存（items）を実装するまで、本番も仮の ID（d1, d2, …）にする。
取得できなかった情報源と、急上昇を判定しなかった情報源の本文への記載は、③ の保存と全情報源の
接続（#40）または部分失敗の分岐（#41）で足す（① の要約にはすでにある）。
"""

import json
from collections.abc import Mapping
from pathlib import Path

from tech_researcher.agent.daily import InputItem, build_daily_input, parse_daily_output
from tech_researcher.collect.records import Item
from tech_researcher.collect.sources import source_names
from tech_researcher.config import Config
from tech_researcher.db.readonly import tentative_ids
from tech_researcher.logging import RunLog
from tech_researcher.periods import Target
from tech_researcher.render.common import Report
from tech_researcher.render.daily import render_daily
from tech_researcher.stages.collect import COLLECTED, load_collected
from tech_researcher.validate.ids import check_daily_ids
from tech_researcher.validate.secret_scan import SecretScanner

AGENT_INPUT = "agent-input.json"
AGENT_OUTPUT = "agent-output.json"
VALIDATION = "validation.json"
SECRET_SCAN = "secret-scan.json"
ISSUE = "issue.md"

# ③ の結果。統括スクリプトは report のときだけ ⑤ に進む
REPORT = "report"
REJECTED = "rejected"
SECRET_DETECTED = "secret_detected"
RESULTS = frozenset({REPORT, REJECTED, SECRET_DETECTED})


def write_daily_input(config: Config, target: Target, run_dir: Path) -> None:
    """run_dir/collected.json から、run_dir/agent-input.json を書く。"""
    data = _input(config, target, _numbered(run_dir))
    _write(run_dir / AGENT_INPUT, json.dumps(data, ensure_ascii=False))


def run_verify(
    config: Config,
    target: Target,
    run_id: str,
    run_dir: Path,
    log: RunLog,
    scanner: SecretScanner,
) -> dict:
    """agent-output.json を検査し、通った項目から本文を作る。結果と件数を返す。

    validation.json（合否と除外の理由）と secret-scan.json（検出の有無と種類）を書く。
    合格して秘匿情報もなければ、作るはずの Issue を issue.md に書く。
    """
    log.write("verify", "start")
    items = _numbered(run_dir)
    data = _input(config, target, items)
    output = json.loads((run_dir / AGENT_OUTPUT).read_text(encoding="utf-8"))
    try:
        entries = parse_daily_output(output)
    except ValueError as e:
        # D6(a)：出力全体が不合格（US-E7-02）。不合格の後に再生成はしない
        log.write("verify", "failure", error_category="invalid_output", detail=str(e))
        return _finish(run_dir, log, REJECTED, "invalid_output", entries=0, excluded=[])
    kept, excluded = check_daily_ids(entries, data)
    for x in excluded:
        log.write("verify", "excluded", error_category="invalid_id", detail=x.reason)
    # ID と理由には AI が返した文字列が入るので、マスクしてから残す（ログは RunLog がマスクする）
    reasons = [
        {"item_ids": [scanner.mask(i) for i in x.entry.item_ids], "reason": scanner.mask(x.reason)}
        for x in excluded
    ]
    if entries and not kept:  # 除外で0件（US-E7-02）。AI が0件を選んだときはレポートにする
        log.write("verify", "failure", error_category="all_excluded")
        return _finish(run_dir, log, REJECTED, "all_excluded", entries=0, excluded=reasons)
    report = render_daily(target, run_id, kept, items, source_names(config), collected=len(items))
    kinds = sorted({f.kind for f in scanner.find(_issue_text(report))})
    _write(run_dir / SECRET_SCAN, json.dumps({"detected": bool(kinds), "kinds": kinds}))
    if kinds:
        log.write("verify", "secret_detected", error_category=",".join(kinds))
        return _finish(run_dir, log, SECRET_DETECTED, None, entries=len(kept), excluded=reasons)
    _write(run_dir / ISSUE, _issue_text(report))
    return _finish(run_dir, log, REPORT, None, entries=len(kept), excluded=reasons)


def _numbered(run_dir: Path) -> dict[str, Item]:
    """collected.json の項目に、並びの順で ID を付ける（② の入力と ③ で同じ ID になる）。"""
    # tentative_ids は終わりのない並びなので、項目の数で止める
    return dict(zip(tentative_ids(), load_collected(run_dir / COLLECTED), strict=False))


def _input(config: Config, target: Target, items: Mapping[str, Item]) -> dict:
    inputs = [InputItem(item_id, item) for item_id, item in items.items()]
    return build_daily_input(target, inputs, source_names(config))


def _finish(
    run_dir: Path,
    log: RunLog,
    result: str,
    reason: str | None,
    *,
    entries: int,
    excluded: list[dict],
) -> dict:
    validation = {"result": result, "reason": reason, "entries": entries, "excluded": excluded}
    _write(run_dir / VALIDATION, json.dumps(validation, ensure_ascii=False))
    log.write("verify", "end", count=entries)
    return {"result": result, "entries": entries, "excluded": len(excluded)}


def _issue_text(report: Report) -> str:
    """作るはずの Issue（タイトル・ラベル・本文）。秘匿情報の検査もこの全体にかける。"""
    return f"{report.title}\n{', '.join(report.labels)}\n\n---\n\n{report.body}"


def _write(path: Path, text: str) -> None:
    """途中で止まっても壊れたファイルを残さないよう、一時ファイルに書いてから置き換える。"""
    partial = path.with_name(f"{path.name}.partial")
    partial.write_text(text, encoding="utf-8")
    partial.replace(path)
