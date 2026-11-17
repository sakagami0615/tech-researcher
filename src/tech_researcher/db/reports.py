"""生成したレポート案と検証結果の保存：reports・report_entries（data-model.md 3.4・3.7、D-18）。

③ の結果を実行ごとに1件残す。除外した項目も、理由つきで残す（US-E7-01）。同じ実行の中の
③ の再試行は、同じ実行のレポートと項目をすべて置き換える（別の実行は別の行）。
ドライランの実行 ID は受け付けない（D-22）。AI の出力・本文は、呼ぶ側がマスクしてから渡す。
"""

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from tech_researcher.db.runs import CADENCES, check_run_target, check_writable_run_id
from tech_researcher.db.schema import transaction

REPORT_STATUSES = frozenset({"passed", "rejected"})
DAILY_SECTIONS = frozenset({"daily_item"})
WEEKLY_SECTIONS = frozenset({"theme", "tech_change", "comparison", "watch"})
ENTRY_STATUSES = frozenset({"published", "excluded"})

# 除外の理由・不合格の検査の名前は、③ のログの error_category と同じ分類名（invalid_id・
# invalid_source・missing_period_event・duplicate_id・invalid_output・all_excluded など）。
# 検査が増えるたびにここを直さなくてよいよう、名前の形だけを確かめる
_NAME = re.compile(r"[a-z][a-z0-9_]*")
_ITEM_ID = re.compile(r"i([1-9]\d*)")
_THEME_ID = re.compile(r"T\d{4,}")


@dataclass(frozen=True)
class EntryRow:
    """レポート案の1項目。除外した項目は status を excluded にして、理由を持たせる。

    item_ids・past_ids は AI が選んだ ID。掲載した項目（published）は items にある ID に限る。
    除外した項目は、存在しない ID を返したことが理由でありうるので、そのまま残す。
    theme_id は掲載したテーマの、コードが確かめた番号だけ。除外したテーマで AI が返した
    値は形が不正でありうるので、payload（例：ai_theme_id）に入れる。
    """

    section: str
    status: str
    item_ids: tuple[str, ...] = ()
    past_ids: tuple[str, ...] = ()
    payload: Mapping = field(default_factory=dict)
    exclusion_reasons: tuple[str, ...] = ()
    display_no: int | None = None
    theme_id: str | None = None

    def __post_init__(self):
        if self.section not in DAILY_SECTIONS | WEEKLY_SECTIONS:
            raise ValueError(f"知らない section {self.section}")
        if self.status not in ENTRY_STATUSES:
            raise ValueError(f"知らない項目の状態 {self.status}")
        published = self.status == "published"
        if published == bool(self.exclusion_reasons):
            raise ValueError("除外の理由は、除外した項目（excluded）にだけ、1つ以上付ける")
        for reason in self.exclusion_reasons:
            _check_name(reason)
        if (self.display_no is not None) != (published and self.section == "daily_item"):
            raise ValueError("display_no は、掲載した日次の項目にだけ付ける")
        if self.display_no is not None and self.display_no < 1:
            raise ValueError("display_no は 1 以上にする")
        if self.theme_id is not None and (
            not published or self.section != "theme" or not _THEME_ID.fullmatch(self.theme_id)
        ):
            raise ValueError(
                "theme_id は、掲載したテーマに T0007 の形で付ける"
                f"（除外した項目の AI の値は payload に入れる）: {self.theme_id}"
            )
        if published and self.section == "theme" and self.theme_id is None:
            raise ValueError("掲載したテーマには theme_id が要る")
        if published and not (self.item_ids or self.past_ids):
            raise ValueError("掲載した項目には item_ids か past_ids が要る")


@dataclass(frozen=True)
class ReportRow:
    """1回の実行のレポート案と検証の結果。

    rejected_checks は不合格にした検査（invalid_output・all_excluded・secret_detected など）。
    counts は件数（収集・AI の出力・除外の理由別・掲載）、verification は検査ごとの合否。
    body_markdown は合格して公開できる本文（⑤ の再試行で同じ本文を使う）。秘匿情報を検出した
    本文は保存しない。
    """

    cadence: str
    target: date
    status: str
    counts: Mapping
    verification: Mapping
    entries: Sequence[EntryRow] = ()
    rejected_checks: tuple[str, ...] = ()
    ai_output: str | None = None
    body_markdown: str | None = None

    def __post_init__(self):
        if self.cadence not in CADENCES:
            raise ValueError(f"知らない cadence {self.cadence}")
        if self.status not in REPORT_STATUSES:
            raise ValueError(f"知らないレポートの状態 {self.status}")
        if (self.status == "rejected") != bool(self.rejected_checks):
            raise ValueError("rejected_checks は不合格（rejected）のときだけ付ける")
        for check in self.rejected_checks:
            _check_name(check)
        if (self.status == "passed") != bool(self.body_markdown):
            raise ValueError("本文（body_markdown）は合格のときだけ、必ず付ける")
        allowed = DAILY_SECTIONS if self.cadence == "daily" else WEEKLY_SECTIONS
        if any(e.section not in allowed for e in self.entries):
            raise ValueError(f"{self.cadence} に使えない section がある")


def save_report(conn: sqlite3.Connection, run_id: str, report: ReportRow) -> None:
    """レポートと項目を1つのトランザクションで書く。同じ実行の行があれば置き換える。

    項目の entry_no は entries の順（1から）。掲載した項目の ID が items になければ、何も書かず
    LookupError にする。中でトランザクションを始めるので、`transaction` の中からは呼べない。
    """
    check_writable_run_id(run_id)
    with transaction(conn):
        check_run_target(conn, run_id, report.cadence, report.target)
        _check_published_items(conn, report.entries)
        # 子から消す。reports は REPLACE にせず UPDATE にして、行を消し入れしない
        conn.execute("DELETE FROM report_entries WHERE run_id = ?", (run_id,))
        conn.execute(
            "INSERT INTO reports (run_id, cadence, target, status, rejected_checks, counts_json,"
            " verification_json, ai_output_json, body_markdown) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (run_id) DO UPDATE SET cadence = excluded.cadence,"
            " target = excluded.target, status = excluded.status,"
            " rejected_checks = excluded.rejected_checks, counts_json = excluded.counts_json,"
            " verification_json = excluded.verification_json,"
            " ai_output_json = excluded.ai_output_json, body_markdown = excluded.body_markdown",
            (
                run_id,
                report.cadence,
                report.target.isoformat(),
                report.status,
                _json(list(report.rejected_checks)),
                _json(report.counts),
                _json(report.verification),
                report.ai_output,
                report.body_markdown,
            ),
        )
        conn.executemany(
            "INSERT INTO report_entries (run_id, entry_no, section, status, exclusion_reasons,"
            " display_no, theme_id, item_ids_json, past_ids_json, payload_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    number,
                    e.section,
                    e.status,
                    _json(list(e.exclusion_reasons)),
                    e.display_no,
                    e.theme_id,
                    _json(list(e.item_ids)),
                    _json(list(e.past_ids)),
                    _json(e.payload),
                )
                for number, e in enumerate(report.entries, start=1)
            ],
        )


def _check_published_items(conn: sqlite3.Connection, entries: Sequence[EntryRow]) -> None:
    numbers = set()
    for e in entries:
        for ref in e.item_ids if e.status == "published" else ():
            match = _ITEM_ID.fullmatch(ref)
            if match is None:
                raise LookupError(f"掲載した項目の ID が items の形でない: {ref}")
            numbers.add(int(match[1]))
    if not numbers:
        return
    marks = ", ".join("?" * len(numbers))
    found = {
        r[0]
        for r in conn.execute(
            f"SELECT item_id FROM items WHERE item_id IN ({marks})", list(numbers)
        )
    }
    if missing := sorted(numbers - found):
        raise LookupError(f"掲載した項目の ID が items にない: {missing}")


def _check_name(name: str) -> None:
    if not _NAME.fullmatch(name):
        raise ValueError(f"理由・検査の名前は小文字の英数字と _ だけにする: {name!r}")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
