"""掲載の履歴を引く問い合わせ（data-model.md 3.5・3.6、D-13・D-16・D-18）。

「載せた」は専用の表に持たず、作成済みの日次レポート Issue（issues の kind = report、
state = created）と、その実行で掲載した項目（report_entries の published）の組み合わせから導く。
不合格・失敗・止めた実行、除外した項目、ドライランは、この条件で外れる。読むだけで、書かない。
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

# 作成済みの日次レポート Issue に掲載した項目（issues i、report_entries e、items it）。
# items は主キーで引く（'i' || item_id と比べると全件を走査し、履歴が増えると遅くなる）。
# 掲載した項目の ID は `i[1-9]\d*` の形に限るので、先頭の i を除いた数が主キーになる。
# 必ず issues（作成済みの Issue）を起点にする。report_entries の status だけで数えない
# （本文が長すぎて投稿しない日の項目は published のまま残るため）
_PUBLISHED_DAILY = (
    "FROM issues i"
    " JOIN report_entries e ON e.run_id = i.run_id AND e.section = 'daily_item'"
    " AND e.status = 'published'"
    " JOIN json_each(e.item_ids_json) j"
    " JOIN items it ON it.item_id = CAST(substr(j.value, 2) AS INTEGER)"
    " WHERE i.kind = 'report' AND i.state = 'created' AND i.cadence = 'daily'"
)
SPIKE_EVENTS = ("attention_spike", "new_repo")


def featured_urls(conn: sqlite3.Connection, before: date | None = None) -> set[str]:
    """急上昇・新規として載せた項目の canonical_url（D-13 の「載せた」）。

    掲載の根拠の順位によらず、events に attention_spike・new_repo を持つ項目を数える。
    今日の項目の canonical_url がこの集合にあれば、急上昇・新規として再び載せない。
    before を渡すと、対象日がそれより前の日次レポートだけを数える（同じ対象の再実行・ドライランで、
    その日に載せた項目で自分を除外しないため）。
    """
    marks = ", ".join("?" * len(SPIKE_EVENTS))
    limit = "" if before is None else " AND i.target < ?"
    args = (*(() if before is None else (before.isoformat(),)), *SPIKE_EVENTS)
    rows = conn.execute(
        f"SELECT DISTINCT it.canonical_url {_PUBLISHED_DAILY}{limit}"
        " AND EXISTS (SELECT 1 FROM json_each(it.events_json) ev"
        f" WHERE json_extract(ev.value, '$.type') IN ({marks}))",
        args,
    )
    return {r[0] for r in rows}


def shown_urls(conn: sqlite3.Connection, day: date) -> set[str]:
    """対象日 day の日次レポート Issue に載せた項目の canonical_url（D-16。T−1 を渡す）。"""
    rows = conn.execute(
        f"SELECT DISTINCT it.canonical_url {_PUBLISHED_DAILY} AND i.target = ?",
        (day.isoformat(),),
    )
    return {r[0] for r in rows}


@dataclass(frozen=True)
class PastTheme:
    """週次の過去のテーマ。name は最新の週のもの、history は（週、状態、件数）を古い順に並べる。"""

    theme_id: str
    name: str
    history: tuple[tuple[date, str | None, int], ...]


def past_themes(conn: sqlite3.Connection, target: date, weeks: int) -> list[PastTheme]:
    """比較の窓（target の前の weeks 週）で、公開した週次レポートに載せたテーマ（3.6）。

    不合格・失敗の週と、ドライランのテーマは入らない。name・status は payload の同名の値。
    """
    windows = [(target - timedelta(weeks=n)).isoformat() for n in range(1, weeks + 1)]
    if not windows:
        return []
    marks = ", ".join("?" * len(windows))
    rows = conn.execute(
        "SELECT e.theme_id, i.target, e.payload_json, e.item_ids_json FROM issues i"
        " JOIN report_entries e ON e.run_id = i.run_id AND e.section = 'theme'"
        " AND e.status = 'published'"
        " WHERE i.kind = 'report' AND i.state = 'created' AND i.cadence = 'weekly'"
        f" AND i.target IN ({marks}) ORDER BY i.target",
        windows,
    ).fetchall()
    themes: dict[str, PastTheme] = {}
    for theme_id, week, payload_json, item_ids_json in rows:
        payload = json.loads(payload_json)
        old = themes.get(theme_id)
        week_entry = (
            date.fromisoformat(week),
            payload.get("status"),
            len(json.loads(item_ids_json)),
        )
        themes[theme_id] = PastTheme(
            theme_id,
            payload.get("name") or (old.name if old else ""),
            (*(old.history if old else ()), week_entry),
        )
    return sorted(themes.values(), key=lambda t: t.theme_id)


def next_theme_id(conn: sqlite3.Connection, run_id: str | None = None) -> str:
    """新しいテーマの番号（T0001 から。3.6）。掲載したテーマの最大の番号の次。

    不合格の週の番号も数える。除外した項目で AI が返した値は数えない。run_id を渡すと、その実行が
    すでに保存した行は数えない（同じ実行の ③ のやり直しが、同じ番号を振り直せるように。3.7）。
    """
    (last,) = conn.execute(
        "SELECT max(CAST(substr(theme_id, 2) AS INTEGER)) FROM report_entries"
        " WHERE theme_id IS NOT NULL AND status = 'published' AND run_id IS NOT ?",
        (run_id,),
    ).fetchone()
    return f"T{(last or 0) + 1:04d}"
