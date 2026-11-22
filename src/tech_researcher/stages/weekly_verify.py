"""週次の ③ 検証・検査と、結果の保存（D-04・D-07・D-18、data-model.md 3.4・3.6・3.7。worker）。

日次の run_verify（stages/daily.py）と同じ流れを、週次の4つのセクション（テーマ・重要な技術的変化・
比較・今後注目すべきテーマ）に行う。検査と本文の作成は verify_weekly（stages/weekly.py）で、ここは
その結果を実行のフォルダ（validation.json・secret-scan.json・issue.md）と DB に残す。

- ② の出力の原文（無害化の前）に秘匿情報があれば、AI の出力も本文も項目も保存しない（US-E7-05）
- 出力全体がスキーマに合わなければ不合格（invalid_output。D6(a)）。項目の検査で1件も掲載できなければ
  不合格（all_excluded）。AI が0件を選んだ週は、「該当なし」のレポートにする
- 本番は、結果（合格・不合格・秘匿情報の検出）を reports・report_entries に保存する。除外した項目も
  理由つきで残し、掲載したテーマには、新しいテーマも含めてコードが確かめた theme_id を付ける
  （採番は verify_weekly）。同じ実行の ③ のやり直しは置き換える（テーマの番号も同じになる）。
  ドライランは DB に書かない（D-22。比較対象を引くための読み取り専用の接続だけを渡す）
- 比較は領域ごとに1件。掲載した領域の payload の rows は本文の行と同じ（除外した行は含まない）。
  除外した行は、領域とは別の項目（section は comparison）に、領域名と行を payload に持たせて残す
- 週次の失敗の分岐（④・不合格 Issue・実行エラー Issue）は、統括スクリプトの側で行う
"""

import json
import sqlite3
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

from tech_researcher.agent.weekly_output import (
    ComparisonEntry,
    ComparisonRow,
    TechChangeEntry,
    ThemeEntry,
    WatchEntry,
    WeeklyOutput,
    parse_weekly_output,
)
from tech_researcher.config import Config
from tech_researcher.db.reports import EntryRow, ReportRow, save_report
from tech_researcher.logging import RunLog
from tech_researcher.periods import Target
from tech_researcher.render.common import body_too_long
from tech_researcher.stages.collect import load_item_ids
from tech_researcher.stages.daily import (
    AGENT_OUTPUT,
    ISSUE,
    REJECTED,
    REPORT,
    SECRET_DETECTED,
    SECRET_SCAN,
    finish_verify,
    issue_text,
    json_strings,
    mask_json,
    write_atomic,
)
from tech_researcher.stages.weekly import WeeklyVerified, verify_weekly
from tech_researcher.validate.secret_scan import SecretScanner
from tech_researcher.validate.weekly import Entry, WeeklyExcluded


def run_verify_weekly(
    config: Config,
    target: Target,
    run_id: str,
    run_dir: Path,
    log: RunLog,
    scanner: SecretScanner,
    db: sqlite3.Connection | None = None,
    *,
    write: bool = False,
) -> dict:
    """agent-output.json を検査し、通った項目から本文を作る。結果と件数を返す。

    validation.json（合否と除外の理由）と secret-scan.json を書き、合格して秘匿情報もなければ、
    作るはずの Issue を issue.md に書く。db は比較対象を引く接続（ドライランは読み取り専用）で、
    write が True（本番）のときだけ、結果を reports・report_entries に保存する。保存はどの分岐でも
    結果のファイルを書く前に行う。保存できなければ例外にし、ファイルは書かない。
    """
    log.write("verify", "start")
    store = _Store(db if write else None, run_id, target, scanner, len(load_item_ids(run_dir)))
    output = json.loads((run_dir / AGENT_OUTPUT).read_text(encoding="utf-8"))
    # 無害化で値の途中が割れると本文の検査では見えないので、AI の出力の全文と、キー・文字列の値を
    # 1つずつも検査する（日次と同じ）
    texts = [json.dumps(output, ensure_ascii=False), *json_strings(output)]
    raw_kinds = sorted({f.kind for text in texts for f in scanner.find(text)})
    if raw_kinds:
        # 原文に秘匿情報があるので、AI の出力も本文も保存しない
        facts = store.save(
            _output_count(output), (), {"secret_detected": "failed"}, ["secret_detected"]
        )
        write_atomic(run_dir / SECRET_SCAN, json.dumps({"detected": True, "kinds": raw_kinds}))
        log.write(
            "verify", "secret_detected", error_category=SECRET_DETECTED, detail=",".join(raw_kinds)
        )
        return finish_verify(
            run_dir, log, SECRET_DETECTED, None, entries=0, excluded=[], facts=facts
        )
    try:
        parse_weekly_output(output)
    except ValueError as e:
        # D6(a)：スキーマに合わない出力全体が不合格（US-E7-02）。不合格の後に再生成はしない
        log.write("verify", "failure", error_category="invalid_output", detail=str(e))
        facts = store.save(
            _output_count(output),
            (),
            {"secret_detected": "passed", "invalid_output": "failed"},
            ["invalid_output"],
            output=output,
        )
        return finish_verify(
            run_dir, log, REJECTED, "invalid_output", entries=0, excluded=[], facts=facts
        )
    verified = verify_weekly(config, target, run_id, run_dir, output, db)
    checked = verified.checked
    for x in checked.excluded:
        log.write("verify", "excluded", error_category=x.category, detail=x.reason)
    # ID と理由には AI が返した文字列が入るので、マスクしてから残す
    reasons = [
        {"item_ids": [scanner.mask(i) for i in _ids(x.entry)], "reason": scanner.mask(x.reason)}
        for x in checked.excluded
    ]
    verification = dict(checked.verification)
    ai_count = _entry_count(verified.parsed)
    if (
        ai_count and not checked.published
    ):  # 除外で0件（US-E7-02）。AI が0件を選んだ週はレポートにする
        log.write("verify", "failure", error_category="all_excluded")
        verification["secret_detected"] = "passed"  # 原文の検査は通っている
        facts = store.save(
            ai_count,
            checked.excluded,
            verification,
            ["all_excluded"],
            output=output,
            verified=verified,
        )
        return finish_verify(
            run_dir, log, REJECTED, "all_excluded", entries=0, excluded=reasons, facts=facts
        )
    report = verified.report
    kinds = sorted({f.kind for f in scanner.find(issue_text(report))})
    verification["secret_detected"] = "failed" if kinds else "passed"
    if kinds:
        log.write(
            "verify", "secret_detected", error_category=SECRET_DETECTED, detail=",".join(kinds)
        )
        # 公開しないので、項目と本文は残さない（件数には AI の出力と除外を数える）
        facts = store.save(ai_count, checked.excluded, verification, ["secret_detected"])
        write_atomic(run_dir / SECRET_SCAN, json.dumps({"detected": True, "kinds": kinds}))
        return finish_verify(
            run_dir,
            log,
            SECRET_DETECTED,
            None,
            entries=checked.published,
            excluded=reasons,
            facts=facts,
        )
    if body_too_long(report.body):  # 投稿できない長さ。掲載を絞って作り直さない（再生成しない）
        log.write("verify", "failure", error_category="body_too_long")
        verification["body_too_long"] = "failed"
        facts = store.save(
            ai_count,
            checked.excluded,
            verification,
            ["body_too_long"],
            output=output,
            verified=verified,
        )
        return finish_verify(
            run_dir, log, REJECTED, "body_too_long", entries=0, excluded=reasons, facts=facts
        )
    facts = store.save(
        ai_count,
        checked.excluded,
        verification,
        [],
        output=output,
        verified=verified,
        body=report.body,
    )
    write_atomic(run_dir / SECRET_SCAN, json.dumps({"detected": False, "kinds": []}))
    write_atomic(run_dir / ISSUE, issue_text(report))
    return finish_verify(
        run_dir, log, REPORT, None, entries=checked.published, excluded=reasons, facts=facts
    )


class _Store:
    """③ の結果の件数・検証項目ごとの合否を作り、db があれば reports・report_entries に保存する。"""

    def __init__(
        self,
        db: sqlite3.Connection | None,
        run_id: str,
        target: Target,
        scanner: SecretScanner,
        collected: int,
    ):
        self.db = db
        self.run_id = run_id
        self.target = target
        self.scanner = scanner
        self.collected = collected

    def save(
        self,
        ai_count: int,
        excluded: Sequence[WeeklyExcluded],
        verification: Mapping[str, str],
        rejected_checks: Sequence[str],
        *,
        output: object = None,
        verified: WeeklyVerified | None = None,
        body: str | None = None,
    ) -> dict:
        """件数・合否・不合格の検査の名前を返す（validation.json に書く）。

        output を渡すと AI の出力（マスクして）を、verified を渡すと項目を保存する。掲載した件数は、
        本文を渡した（合格の）ときだけ数える。秘匿情報を検出した実行は、どちらも渡さない。
        """
        # 比較の行の除外は、項目（領域）の数と単位が違うので別に数える（AI の出力 − 除外 = 掲載）
        items = [x for x in excluded if x.section != "comparison_row"]
        counts = {
            "collected": self.collected,
            "ai_output": ai_count,
            "excluded": len(items),
            "excluded_by_reason": dict(Counter(x.category for x in items)),
            "excluded_rows": len(excluded) - len(items),
            "published": verified.checked.published if body is not None and verified else 0,
        }
        facts = {
            "counts": counts,
            "verification": dict(verification),
            "rejected_checks": list(rejected_checks),
        }
        if self.db is None:
            return facts
        ai_output = None if output is None else json.dumps(self._mask(output), ensure_ascii=False)
        save_report(
            self.db,
            self.run_id,
            ReportRow(
                cadence=self.target.cadence,
                target=self.target.key,
                status="rejected" if rejected_checks else "passed",
                counts=counts,
                verification=dict(verification),
                entries=self._entries(verified) if verified else [],
                rejected_checks=tuple(rejected_checks),
                ai_output=ai_output,
                body_markdown=body,
            ),
        )
        return facts

    def _mask(self, value):
        return mask_json(value, self.scanner)

    def _entries(self, verified: WeeklyVerified) -> list[EntryRow]:
        """AI の出力の順の項目。掲載した項目は検査を通った項目（新しいテーマは番号つき）、除外した
        項目は AI が返したままに、理由と詳細を付ける。"""
        bad = {id(x.entry): x for x in verified.checked.excluded}
        kept = verified.checked.output
        parsed = verified.parsed
        themes, changes = iter(kept.themes), iter(kept.tech_changes)
        comparisons, watch = iter(kept.comparisons), iter(kept.watch)
        rows: list[EntryRow] = []
        for t in parsed.themes:
            rows.append(self._theme(t, bad.get(id(t)), next(themes) if id(t) not in bad else None))
        for c in parsed.tech_changes:
            x = bad.get(id(c))
            payload = {"headline": c.headline, "description": c.description}
            rows.append(self._row("tech_change", c.item_ids, payload, x))
            if x is None:
                next(changes)
        for comparison in parsed.comparisons:
            x = bad.get(id(comparison))
            survivor = next(comparisons) if x is None else None
            rows.append(self._comparison(comparison, survivor, x))
            rows += self._excluded_rows(comparison, bad)
        for w in parsed.watch:
            x = bad.get(id(w))
            rows.append(self._row("watch", w.item_ids, {"name": w.name, "reason": w.reason}, x))
            if x is None:
                next(watch)
        # 反復子を使い切り、掲載＋除外（行を除く）が AI の出力の数と合うこと。ずれたら保存しない
        leftover = [next(it, None) for it in (themes, changes, comparisons, watch)]
        top = [r for r in rows if r.payload.get("row") is None]
        if any(x is not None for x in leftover) or len(top) != _entry_count(parsed):
            raise ValueError("掲載した項目と除外した項目が、AI の出力と対応しない")
        return rows

    def _theme(
        self, theme: ThemeEntry, x: WeeklyExcluded | None, kept: ThemeEntry | None
    ) -> EntryRow:
        payload = {
            "name": theme.name,
            "status": theme.status,
            "change": theme.change,
            "comparison": theme.comparison,
        }
        if x is not None:
            # 除外したテーマで AI が返した番号は、形が不正でありうるので列に入れない
            payload |= {"ai_theme_id": theme.theme_id, "exclusion_detail": x.reason}
        return EntryRow(
            section="theme",
            status="published" if x is None else "excluded",
            item_ids=theme.item_ids,
            past_ids=theme.past_ids,
            payload=self._mask(payload),
            exclusion_reasons=() if x is None else (x.category,),
            theme_id=kept.theme_id if kept is not None else None,
        )

    def _comparison(
        self, comparison: ComparisonEntry, kept: ComparisonEntry | None, x: WeeklyExcluded | None
    ) -> EntryRow:
        rows = comparison.rows if kept is None else kept.rows  # 掲載した領域は、本文に出した行だけ
        payload = {
            "domain": comparison.domain,
            "summary": comparison.summary,
            "rows": [_row_payload(r) for r in rows],
        }
        if x is not None:
            payload["exclusion_detail"] = x.reason
        return EntryRow(
            section="comparison",
            status="published" if x is None else "excluded",
            item_ids=tuple(r.item_id for r in rows),
            payload=self._mask(payload),
            exclusion_reasons=() if x is None else (x.category,),
        )

    def _excluded_rows(
        self, comparison: ComparisonEntry, bad: Mapping[int, WeeklyExcluded]
    ) -> Iterator[EntryRow]:
        """除外した行（領域が掲載されたかによらず）。領域の項目の後に、行ごとに1件。"""
        for r in comparison.rows:
            if (x := bad.get(id(r))) is not None:
                payload = {
                    "domain": comparison.domain,
                    "row": _row_payload(r),
                    "exclusion_detail": x.reason,
                }
                yield EntryRow(
                    section="comparison",
                    status="excluded",
                    item_ids=(r.item_id,),
                    payload=self._mask(payload),
                    exclusion_reasons=(x.category,),
                )

    def _row(
        self, section: str, item_ids: Sequence[str], payload: dict, x: WeeklyExcluded | None
    ) -> EntryRow:
        if x is not None:
            payload = {**payload, "exclusion_detail": x.reason}
        return EntryRow(
            section=section,
            status="published" if x is None else "excluded",
            item_ids=tuple(item_ids),
            payload=self._mask(payload),
            exclusion_reasons=() if x is None else (x.category,),
        )


def _row_payload(row: ComparisonRow) -> dict:
    return {
        "item_id": row.item_id,
        "product_name": row.product_name,
        "feature": row.feature,
        "use_case": row.use_case,
    }


def _ids(entry: Entry) -> tuple[str, ...]:
    """除外した項目の、AI が選んだ今週の観測の ID（比較は行の ID）。"""
    match entry:
        case ComparisonRow():
            return (entry.item_id,)
        case ComparisonEntry():
            return tuple(r.item_id for r in entry.rows)
        case ThemeEntry() | TechChangeEntry() | WatchEntry():
            return tuple(entry.item_ids)


def _entry_count(output: WeeklyOutput) -> int:
    """AI の出力の項目の数（比較は領域の数）。"""
    return (
        len(output.themes) + len(output.tech_changes) + len(output.comparisons) + len(output.watch)
    )


def _output_count(output: object) -> int:
    """AI の出力の項目の数。形が崩れていて数えられなければ 0。"""
    if not isinstance(output, dict):
        return 0
    return sum(
        len(v)
        for k in ("themes", "tech_changes", "comparisons", "watch")
        if isinstance(v := output.get(k), list)
    )
