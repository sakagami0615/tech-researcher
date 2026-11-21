"""日次の ② の入力の用意と、③ 検証・検査（D-04・D-07。worker で動く）。

- ① の後に、collected.json から ② の入力（agent-input.json）を作る。統括スクリプトがそれを
  agent に標準入力で渡し、構造化出力を agent-output.json に保存する
- ③ は、agent に渡したコピーではなく、worker の collected.json から同じ入力を作り直して照合する
  （D-04）。いまは D6(a) のスキーマ、D6(b) の ID、D1、期間内の出来事の欠落、D2・D3、
  文字数と要点数、D4 を検査する
- 秘匿情報を検出したら Issue の本文を残さない（US-E7-05）。⑤ の直前の検査は ⑤ の Issue で足す
- 不合格・実行エラー・公開停止の本文は、統括スクリプト（failures.py）が render/failure.py で作り、
  issue.md に書く（④ の調査結果を含めるため）。③ は不合格のとき issue.md・secret-scan.json を
  書かず、件数・検証項目ごとの合否・不合格となった検査の名前を validation.json に残す

- 項目の ID は ① が item-ids.json に書く（本番は items の `i{主キー}`、ドライランは DB にある項目の
  ID か仮の ID。data-model.md 2.6）。② の入力と ③ の照合は同じ ID を使う
- 取得できなかった情報源は、① が書く unavailable.json から本文の末尾に入れる。急上昇を判定しなかった
  情報源は、unjudged.json から本文のヘッダに入れる（report-templates.md 1.4・1.6）
- 本番は、結果（合格・不合格・秘匿情報の検出）を reports・report_entries に保存する（除外した
  項目も、理由つきで残す。D-18、data-model.md 3.4）。同じ実行の ③ のやり直しは置き換える。
  ドライランは DB に書かない（D-22）
"""

import json
import sqlite3
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

from tech_researcher.agent.daily import (
    DailyEntry,
    InputItem,
    build_daily_input,
    parse_daily_output,
)
from tech_researcher.collect.records import Item
from tech_researcher.collect.sources import source_names
from tech_researcher.config import Config, QuoteLimits
from tech_researcher.db.reports import EntryRow, ReportRow, save_report
from tech_researcher.logging import RunLog
from tech_researcher.periods import Target
from tech_researcher.render.common import Report, body_too_long
from tech_researcher.render.daily import display_key, render_daily
from tech_researcher.stages.collect import (
    COLLECTED,
    load_collected,
    load_item_ids,
    unavailable_notes,
    unjudged_notes,
)
from tech_researcher.validate.ids import Excluded, check_daily_duplicates, check_daily_ids
from tech_researcher.validate.period_event import check_period_events
from tech_researcher.validate.quote import check_daily_quotes, meta_terms_by_id
from tech_researcher.validate.secret_scan import SecretScanner
from tech_researcher.validate.sources import check_daily_sources
from tech_researcher.validate.text import check_daily_html, check_daily_lengths, check_daily_links

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
    db: sqlite3.Connection | None = None,
) -> dict:
    """agent-output.json を検査し、通った項目から本文を作る。結果と件数を返す。

    validation.json（合否と除外の理由）と secret-scan.json（検出の有無と種類）を書く。
    合格して秘匿情報もなければ、作るはずの Issue を issue.md に書く。
    db は本番の接続で、渡されたら結果を reports・report_entries に保存する（ドライランは渡さない）。
    保存はどの分岐でも結果のファイルを書く前に行う。保存できなければ例外にし、ファイルは書かない。
    """
    log.write("verify", "start")
    items = _numbered(run_dir)
    data = _input(config, target, items)
    output = json.loads((run_dir / AGENT_OUTPUT).read_text(encoding="utf-8"))
    # 無害化でエスケープや囲みが入ると値の途中が割れ、本文の検査では見えなくなる。GitHub は描画で
    # エスケープを消すので、無害化の前の AI の出力の全文も検査する。除外した項目の中でも全体を止める
    # 全文は json.dumps で1段エスケープされるので、すでにエスケープ済みの値は照合の段数を超える。
    # キーと文字列の値を1つずつも検査する
    texts = [json.dumps(output, ensure_ascii=False), *_strings(output)]
    raw_kinds = sorted({f.kind for text in texts for f in scanner.find(text)})
    if raw_kinds:
        # 原文に秘匿情報があるので、AI の出力も本文も保存しない
        facts = _save(
            db,
            run_id,
            target,
            len(items),
            (),
            (),
            {"secret_detected": "failed"},
            ["secret_detected"],
            ai_count=_output_count(output),
        )
        _write(run_dir / SECRET_SCAN, json.dumps({"detected": True, "kinds": raw_kinds}))
        log.write(
            "verify", "secret_detected", error_category=SECRET_DETECTED, detail=",".join(raw_kinds)
        )
        return _finish(run_dir, log, SECRET_DETECTED, None, entries=0, excluded=[], facts=facts)
    try:
        entries = parse_daily_output(output)
    except ValueError as e:
        # D6(a)：スキーマに合わない出力全体が不合格（US-E7-02）。不合格の後に再生成はしない
        log.write("verify", "failure", error_category="invalid_output", detail=str(e))
        facts = _save(
            db,
            run_id,
            target,
            len(items),
            (),
            (),
            {"secret_detected": "passed", "invalid_output": "failed"},
            ["invalid_output"],
            output=output,
            scanner=scanner,
            ai_count=_output_count(output),
        )
        return _finish(
            run_dir, log, REJECTED, "invalid_output", entries=0, excluded=[], facts=facts
        )
    kept, excluded, verification, outcomes = _check_entries(entries, data, items, config.quote)
    for category, x in excluded:
        log.write("verify", "excluded", error_category=category, detail=x.reason)
    # ID と理由には AI が返した文字列が入るので、マスクしてから残す（ログは RunLog がマスクする）
    reasons = [
        {"item_ids": [scanner.mask(i) for i in x.entry.item_ids], "reason": scanner.mask(x.reason)}
        for _, x in excluded
    ]
    if entries and not kept:  # 除外で0件（US-E7-02）。AI が0件を選んだときはレポートにする
        log.write("verify", "failure", error_category="all_excluded")
        verification["secret_detected"] = "passed"  # 原文の検査は通っている
        facts = _save(
            db,
            run_id,
            target,
            len(items),
            entries,
            excluded,
            verification,
            ["all_excluded"],
            outcomes=outcomes,
            output=output,
            scanner=scanner,
        )
        return _finish(
            run_dir, log, REJECTED, "all_excluded", entries=0, excluded=reasons, facts=facts
        )
    unjudged = unjudged_notes(run_dir, config)
    report = render_daily(
        target,
        run_id,
        kept,
        items,
        source_names(config),
        collected=len(items),
        unavailable=unavailable_notes(run_dir, config),
        unjudged=unjudged,
    )
    kinds = sorted({f.kind for f in scanner.find(issue_text(report))})
    verification["secret_detected"] = "failed" if kinds else "passed"
    if kinds:
        log.write(
            "verify", "secret_detected", error_category=SECRET_DETECTED, detail=",".join(kinds)
        )
        # 公開しないので、項目と本文は残さない（件数には AI の出力と除外を数える）
        facts = _save(
            db,
            run_id,
            target,
            len(items),
            entries,
            excluded,
            verification,
            ["secret_detected"],
            keep_entries=False,
        )
        _write(run_dir / SECRET_SCAN, json.dumps({"detected": True, "kinds": kinds}))
        return _finish(
            run_dir, log, SECRET_DETECTED, None, entries=len(kept), excluded=reasons, facts=facts
        )
    if body_too_long(report.body):  # 投稿できない長さ。掲載を絞って作り直さない（再生成しない）
        log.write("verify", "failure", error_category="body_too_long")
        verification.update({"secret_detected": "passed", "body_too_long": "failed"})
        facts = _save(
            db,
            run_id,
            target,
            len(items),
            entries,
            excluded,
            verification,
            ["body_too_long"],
            outcomes=outcomes,
            output=output,
            scanner=scanner,
        )
        return _finish(
            run_dir, log, REJECTED, "body_too_long", entries=0, excluded=reasons, facts=facts
        )
    facts = _save(
        db,
        run_id,
        target,
        len(items),
        entries,
        excluded,
        verification,
        [],
        outcomes=outcomes,
        output=output,
        scanner=scanner,
        body=report.body,
        kept=kept,
    )
    _write(run_dir / SECRET_SCAN, json.dumps({"detected": False, "kinds": []}))
    _write(run_dir / ISSUE, issue_text(report))
    return _finish(run_dir, log, REPORT, None, entries=len(kept), excluded=reasons, facts=facts)


def _check_entries(
    entries: list[DailyEntry],
    data: dict,
    items: Mapping[str, Item],
    quote: QuoteLimits,
) -> tuple[
    list[DailyEntry], list[tuple[str, Excluded]], dict[str, str], dict[int, tuple[str, str]]
]:
    """項目単位の検査を順に行う。違反した項目は除外し、検査の分類と一緒に返す。

    3つ目は検査ごとの合否（passed・failed。reports の verification）、4つ目は除外した項目の
    （AI の出力の順の番号 → 検査の分類、理由）。

    ID の存在（D6(b)）を先に確かめる。後ろの検査は ID から値を引くので、存在する ID だけを渡す。
    ID の重複は最後に判定する。
    """
    excluded: list[tuple[str, Excluded]] = []
    pending = list(enumerate(entries))  # （AI の出力の順の番号、項目）。通った項目だけ残る
    outcomes: dict[int, tuple[str, str]] = {}
    verification = {"invalid_output": "passed"}  # スキーマ（D6(a)）は、ここに来る前に通っている
    for category, check in (
        ("invalid_id", lambda e: check_daily_ids(e, data)),
        ("invalid_source", lambda e: check_daily_sources(e, items)),
        ("missing_period_event", lambda e: check_period_events(e, data)),
        ("forbidden_link", check_daily_links),  # D2
        ("forbidden_html", check_daily_html),  # D3
        ("too_long", check_daily_lengths),
        ("long_quote", lambda e: check_daily_quotes(e, data, meta_terms_by_id(items), quote)),
        # 重複は最後に、ほかの検査をすべて通った項目の中で判定する（後の検査もこの前に足す）
        ("duplicate_id", check_daily_duplicates),
    ):
        entries, rejected = check(entries)
        excluded += [(category, x) for x in rejected]
        verification[category] = "failed" if rejected else "passed"
        # 検査は通った項目を元の順に、同じ項目として返す。順に突き合わせて番号を引き継ぐ
        rest = iter(rejected)
        survivors = []
        for number, entry in pending:
            if len(survivors) < len(entries) and entry is entries[len(survivors)]:
                survivors.append((number, entry))
            else:
                outcomes[number] = (category, next(rest).reason)
        pending = survivors
    return entries, excluded, verification, outcomes


def _output_count(output: object) -> int:
    """AI の出力の項目の数。形が崩れていて数えられなければ 0。"""
    items = output.get("items") if isinstance(output, dict) else None
    return len(items) if isinstance(items, list) else 0


def _strings(value: object) -> Iterator[str]:
    """JSON の値の、すべてのキーと文字列の値。"""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _numbered(run_dir: Path) -> dict[str, Item]:
    """collected.json の項目に、① が書いた ID（item-ids.json）を付ける（② の入力と ③ で同じ）。"""
    items = load_collected(run_dir / COLLECTED)
    ids = load_item_ids(run_dir)
    if len(ids) != len(items):
        raise ValueError(f"項目の数 {len(items)} と ID の数 {len(ids)} が違う")
    return dict(zip(ids, items, strict=True))


def _input(config: Config, target: Target, items: Mapping[str, Item]) -> dict:
    inputs = [InputItem(item_id, item) for item_id, item in items.items()]
    return build_daily_input(target, inputs, source_names(config))


def _save(
    db: sqlite3.Connection | None,
    run_id: str,
    target: Target,
    collected: int,
    entries: Sequence[DailyEntry],
    excluded: Sequence[tuple[str, Excluded]],
    verification: Mapping[str, str],
    rejected_checks: Sequence[str],
    *,
    output: object = None,
    scanner: SecretScanner | None = None,
    body: str | None = None,
    kept: Sequence[DailyEntry] = (),
    keep_entries: bool = True,
    outcomes: Mapping[int, tuple[str, str]] = {},
    ai_count: int | None = None,
) -> dict:
    """③ の結果を reports・report_entries に保存する。db がなければ（ドライラン）何もしない。

    保存するかによらず、件数・検証項目ごとの合否・不合格となった検査の名前を返す（validation.json に
    書き、統括スクリプトが不合格 Issue の本文に使う）。

    項目は AI の出力の順に残す。掲載した項目には、本文の通し番号（display_no。カテゴリの固定順、
    同じカテゴリでは rank 順。display_key）を付ける。AI が返した文は、マスクしてから保存する。
    outcomes は除外した項目の（AI の出力の順の番号 → 検査の分類、理由）。
    秘匿情報を検出した実行は、AI の出力も本文も保存しない（output・body を渡さない）。
    ai_count は AI の出力の項目の数（entries を渡さないとき、出力の項目の数を数えて渡す）。
    """
    by_reason = Counter(category for category, _ in excluded)
    counts = {
        "collected": collected,
        "ai_output": len(entries) if ai_count is None else ai_count,
        "excluded": len(excluded),
        "excluded_by_reason": dict(by_reason),
        "published": len(kept),  # 掲載した件数（公開しない不合格は kept を渡さず 0）
    }
    facts = {
        "counts": counts,
        "verification": dict(verification),
        "rejected_checks": list(rejected_checks),
    }
    if db is None:
        return facts
    published = [i for i in range(len(entries)) if i not in outcomes]
    ranked = sorted(published, key=lambda i: display_key(entries[i]))
    numbers = {i: n for n, i in enumerate(ranked, start=1)}
    rows = []
    for index, entry in enumerate(entries if keep_entries else ()):
        found = outcomes.get(index)
        rows.append(
            EntryRow(
                section="daily_item",
                status="published" if found is None else "excluded",
                item_ids=tuple(entry.item_ids),
                payload=_mask(_payload(entry, found), scanner),
                exclusion_reasons=() if found is None else (found[0],),
                display_no=numbers.get(index),
            )
        )
    ai_output = None if output is None else json.dumps(_mask(output, scanner), ensure_ascii=False)
    save_report(
        db,
        run_id,
        ReportRow(
            cadence=target.cadence,
            target=target.key,
            status="rejected" if rejected_checks else "passed",
            counts=counts,
            verification=dict(verification),
            entries=rows,
            rejected_checks=tuple(rejected_checks),
            ai_output=ai_output,
            body_markdown=body,
        ),
    )
    return facts


def _payload(entry: DailyEntry, excluded: tuple[str, str] | None) -> dict:
    """AI の文章の部品と選んだ値（除外した項目には、除外の詳細も）。"""
    payload = {
        "category": entry.category,
        "rank": entry.rank,
        "depth": entry.depth,
        "headline": entry.headline,
        "points": list(entry.points),
        "importance": entry.importance,
        "period_event": entry.period_event,
        "primary_item_id": entry.primary_item_id,
        "primary_link_id": entry.primary_link_id,
    }
    if excluded is not None:
        payload["exclusion_detail"] = excluded[1]
    return payload


def _mask(value, scanner: SecretScanner | None):
    """JSON の値の、すべての文字列（と辞書のキー）をマスクする。"""
    if scanner is None:
        return value
    if isinstance(value, str):
        return scanner.mask(value)
    if isinstance(value, dict):
        return {scanner.mask(str(k)): _mask(v, scanner) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v, scanner) for v in value]
    return value


def _finish(
    run_dir: Path,
    log: RunLog,
    result: str,
    reason: str | None,
    *,
    entries: int,
    excluded: list[dict],
    facts: Mapping,
) -> dict:
    validation = {
        "result": result,
        "reason": reason,
        "entries": entries,
        "excluded": excluded,
        **facts,
    }
    _write(run_dir / VALIDATION, json.dumps(validation, ensure_ascii=False))
    log.write("verify", "end", count=entries)
    return {"result": result, "entries": entries, "excluded": len(excluded)}


def issue_text(report: Report) -> str:
    """作るはずの Issue（タイトル・ラベル・本文）。秘匿情報の検査もこの全体にかける。"""
    return f"{report.title}\n{', '.join(report.labels)}\n\n---\n\n{report.body}"


def _write(path: Path, text: str) -> None:
    """途中で止まっても壊れたファイルを残さないよう、一時ファイルに書いてから置き換える。"""
    partial = path.with_name(f"{path.name}.partial")
    partial.write_text(text, encoding="utf-8")
    partial.replace(path)
