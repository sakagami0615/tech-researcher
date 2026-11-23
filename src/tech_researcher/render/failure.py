"""不合格 Issue・実行エラー Issue の本文（report-templates.md 3章、4.2）。

次の3つを区別する。
- AI の調査あり（3.3・3.4）：④ の調査結果と、コードが取り出したログの抜粋を載せる
- AI を利用できないときの定型（3.5）：定型の項目と、調査結果を掲載できなかった旨だけ
- 秘匿情報を見つけたときの公開停止（3.6）：固定文・対象期間・処理段階だけ。
  実行ID・日時・検出の内容は出さない

不合格の本文には、AI の出力の全文も、除外した項目の中身も出さない（件数だけを理由別に出す）。
④ の文章は信頼できない入力として扱う。D2・D3・確度・発生した事象の文字数・空の欄に違反するときは、
3.5 の定型にする（D-07）。本文には sanitize_text を通して入れる。識別子は付けない（3.2）。
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from tech_researcher.periods import JST, Target
from tech_researcher.render.common import (
    Report,
    day_label,
    period_text,
    plain_text,
    run_id_code,
)
from tech_researcher.render.sanitize import BIDI, sanitize_text
from tech_researcher.validate.text import LIMIT_FACTOR, text_violation

STAGES = {
    "collect": "① 収集",
    "analyze": "② 評価・要約",
    "verify": "③ 検証・検査",
    "investigate": "④ 失敗時の調査",
    "post": "⑤ 投稿",
}
SECRET_STAGES = ("verify", "post")  # 秘匿情報を検出しうる段階（3.6）
CONFIDENCES = ("高", "中", "低")
EVENT_LIMIT = (
    200  # 発生した事象の文字数の目安（3.3）。除外するのは LIMIT_FACTOR 倍を超えたとき（0.3）
)
TARGETS = {  # 対策案の修正対象（data-model.md 4.4 の enum）
    "prompt": "プロンプト",
    "collect_config": "収集設定",
    "code": "コード",
    "environment": "環境",
}
MAX_EVIDENCE_LINES = 10  # 1ブロックの行数の上限（3.2）
MAX_EVIDENCE_BLOCKS = 3
_BACKTICKS = re.compile(r"`+")

FOOTER_REJECTED = (
    "この Issue は自動生成です。コード・設定の自動修正やレポートの再生成は行いません。"
    "次の定時の実行から再開します。この期間のレポートは作り直しません。"
)
FOOTER_ERROR = (
    "この Issue は自動生成です。コード・設定の自動修正は行いません。"
    "次の定時の実行から再開します。この期間のレポートは作り直しません。"
)
NO_INVESTIGATION = "AI による調査結果を掲載できませんでした。"
SECRET_NOTICE = "秘匿情報の可能性がある文字列を検出したため、Issue の公開を停止しました。"


@dataclass(frozen=True)
class Countermeasure:
    """対策案。target は TARGETS のキー（修正対象）。"""

    target: str
    text: str


@dataclass(frozen=True)
class Investigation:
    """④ の調査結果（D2・D3・D6(a) の検査を通ったもの）。evidence は select_evidence の結果。"""

    event: str
    cause: str
    confidence: str  # CONFIDENCES のどれか
    contents: Sequence[str]
    measures: Sequence[Countermeasure]
    evidence: Sequence[Sequence[str]] = ()


@dataclass(frozen=True)
class Counts:
    """不合格の件数（3.3）。excluded は除外の理由（コードの分類）から件数への対応。

    excluded_rows は週次の比較の行の除外（理由 → 件数）。項目（領域）と単位が違うので、excluded と
    別に数えて別に出す（AI の出力 − 除外 = 掲載の計算に入れない）。
    """

    collected: int
    ai_output: int
    excluded: Mapping[str, int]
    published: int
    excluded_rows: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceResult:
    """情報源ごとの取得結果（3.4）。reason は失敗の理由の分類。"""

    name: str
    ok: bool
    reason: str | None
    retries: int


def select_evidence(log: Mapping[str, str], line_ids: Sequence[str]) -> tuple[tuple[str, ...], ...]:
    """AI が選んだ行の ID から、根拠の抜粋を取り出す（3.2）。

    log は ID から行への対応（ログの順）。存在しない ID は捨てる。選ばれた行をログの順に並べ、
    連続する行を1ブロックにする。1ブロックは10行以内、全体で3ブロックまで。
    """
    chosen = set(line_ids)
    blocks: list[list[str]] = []
    previous_picked = False
    for line_id, line in log.items():
        if line_id not in chosen:
            previous_picked = False
            continue
        for part in _split(line):  # 行の中の改行も行として数える
            if not previous_picked or len(blocks[-1]) >= MAX_EVIDENCE_LINES:
                blocks.append([])
            blocks[-1].append(part)
            previous_picked = True
    return tuple(tuple(b) for b in blocks[:MAX_EVIDENCE_BLOCKS])


def _split(line: str) -> list[str]:
    return line.splitlines() or [""]


def code_fence(lines: Sequence[str]) -> str:
    """中身でフェンスを閉じられないコードブロック。フェンスは中身の最長のバッククォートより長くする。"""
    text = BIDI.sub("", "\n".join(part for line in lines for part in _split(line)))
    longest = max((len(m.group()) for m in _BACKTICKS.finditer(text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def rejected_report(
    target: Target,
    run_id: str,
    run_at: datetime,
    failed: Sequence[str],
    counts: Counts,
    checks: Sequence[tuple[str, bool]],
    investigation: Investigation | None,
) -> Report:
    """不合格 Issue（3.3）。investigation が None なら、AI を利用できないときの定型（3.5）。

    failed は不合格となった検証項目（名前と ID。例：`D6(a) 出力の形式が不正`）、
    checks は検証項目ごとの（名前、合否）。
    """
    title = (
        f"[不合格]{_cadence(target)} {_target_label(target)}のレポート（{_run_text(run_at)} 実行）"
    )
    labels = ("type:rejected", f"cadence:{target.cadence}")
    _check_aware(run_at)
    investigation = _usable(investigation)
    failed_text = "、".join(map(plain_text, failed))
    if investigation is None:
        rows = [("対象期間", period_text(target)), ("不合格となった検証項目", failed_text)]
        return Report(title, _unavailable(rows), labels)
    rows = [
        ("対象期間", period_text(target)),
        ("実行ID", run_id_code(run_id)),
        ("実行日時", f"{run_at.astimezone(JST):%Y-%m-%d %H:%M} JST"),
        ("不合格となった検証項目", failed_text),
        ("件数", _counts(counts)),
    ]
    marks = [(name, "合格" if ok else "不合格") for name, ok in checks]
    details = _details("検証結果の詳細", _table(("検証項目", "結果"), marks, plain_text))
    body = [_overview(rows), _investigation(investigation), details, _footer(FOOTER_REJECTED)]
    return Report(title, "\n\n".join(body) + "\n", labels)


def run_error_report(
    target: Target,
    run_id: str,
    run_at: datetime,
    stage: str,
    summary: str,
    retries: tuple[int, int],
    sources: Sequence[SourceResult],
    investigation: Investigation | None,
    omitted: int = 0,
) -> Report:
    """実行エラー Issue（3.4）。investigation が None なら、AI を利用できないときの定型（3.5）。

    stage は STAGES のキー、summary はコードが分類した短い文（例外のメッセージは入れない）、
    retries は（再試行回数、上限）。omitted は本文の長さの上限に収めるため表から省いた情報源の行の数
    （表の最後に「ほか N 件」と出す）。
    """
    stage_text = _stage(stage)
    title = (
        f"[実行エラー]{_cadence(target)} {_target_label(target)}"
        f"（{stage_text}・{_run_text(run_at)} 実行）"
    )
    labels = ("type:run-error", f"cadence:{target.cadence}")
    investigation = _usable(investigation)
    retry_text = f"{retries[0]} 回（上限 {retries[1]} 回）"
    if investigation is None:
        rows = [
            ("対象期間", period_text(target)),
            ("失敗した処理段階", stage_text),
            ("エラーの概要", plain_text(summary)),
            ("再試行回数", retry_text),
        ]
        return Report(title, _unavailable(rows), labels)
    rows = [
        ("対象期間", period_text(target)),
        ("実行ID", run_id_code(run_id)),
        ("実行日時", f"{run_at.astimezone(JST):%Y-%m-%d %H:%M} JST"),
        ("失敗した処理段階", stage_text),
        ("エラーの概要", plain_text(summary)),
        ("再試行回数", retry_text),
    ]
    results = [
        (s.name, "成功" if s.ok else "失敗", s.reason or "—", f"{s.retries} 回") for s in sources
    ]
    if omitted:
        results.append((f"ほか {omitted} 件", "—", "—", "—"))
    table = (
        _table(("情報源", "結果", "理由", "再試行"), results, plain_text)
        if results
        else "取得結果を読めなかった。"
    )
    details = _details("情報源ごとの取得結果", table)
    body = [_overview(rows), _investigation(investigation), details, _footer(FOOTER_ERROR)]
    return Report(title, "\n\n".join(body) + "\n", labels)


def secret_stop_report(target: Target, stage: str) -> Report:
    """秘匿情報を検出したときの公開停止の定型（3.6）。

    タイトルにも本文にも、実行ID・実行日時・検出の種類・位置・文字列を入れない。
    """
    if stage not in SECRET_STAGES:
        raise ValueError(f"秘匿情報の検出の段階でない {stage!r}")
    title = f"[実行エラー]{_cadence(target)} {_target_label(target)}（公開停止）"
    rows = [("対象期間", period_text(target)), ("処理段階", STAGES[stage])]
    labels = ("type:run-error", f"cadence:{target.cadence}")
    return Report(title, f"{SECRET_NOTICE}\n\n{_table(('項目', '値'), rows, str)}\n", labels)


def _cadence(target: Target) -> str:
    return {"daily": "[日次]", "weekly": "[週次]"}[target.cadence]


def _target_label(target: Target) -> str:
    """日次は対象日、週次は対象週の月曜に「号」を付ける（3.1）。"""
    label = day_label(target.key)
    return label + "号" if target.cadence == "weekly" else label


def _run_text(run_at: datetime) -> str:
    _check_aware(run_at)
    return f"{run_at.astimezone(JST):%Y-%m-%d %H:%M}"


def _check_aware(run_at: datetime) -> None:
    if run_at.utcoffset() is None:
        raise ValueError("run_at にタイムゾーンがない")


def _stage(stage: str) -> str:
    try:
        return STAGES[stage]
    except KeyError:
        raise ValueError(f"処理段階にない {stage!r}") from None


def _counts(counts: Counts) -> str:
    rows = sum(counts.excluded_rows.values())
    row_text = f"／比較の行の除外 {rows} 件{_reasons(counts.excluded_rows)}" if rows else ""
    return (
        f"収集 {counts.collected} 件／AI の出力 {counts.ai_output} 件／"
        f"除外 {sum(counts.excluded.values())} 件{_reasons(counts.excluded)}{row_text}／"
        f"掲載 {counts.published} 件"
    )


def _reasons(by_reason: Mapping[str, int]) -> str:
    reasons = "、".join(f"{plain_text(r)} {n} 件" for r, n in by_reason.items() if n)
    return f"（{reasons}）" if reasons else ""


def _table(header: Sequence[str], rows: Sequence[Sequence[str]], cell: Callable[[str], str]) -> str:
    """表。セルは cell（plain_text など）を通す。"""
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(cell(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _overview(rows: Sequence[tuple[str, str]]) -> str:
    return "## 概要\n\n" + _table(("項目", "値"), rows, str)


def _unavailable(rows: Sequence[tuple[str, str]]) -> str:
    return f"{_table(('項目', '値'), rows, str)}\n\n{NO_INVESTIGATION}\n"


def _details(summary: str, table: str) -> str:
    # </summary> の後と </details> の前に空行が要る（3.2）
    return f"<details>\n<summary>{summary}</summary>\n\n{table}\n\n</details>"


def _footer(text: str) -> str:
    return f"---\n{text}"


def _usable(inv: Investigation | None) -> Investigation | None:
    """D2・D3・確度の形に違反する ④ の出力は、AI を利用できないときの定型にする（D-07、3.5）。"""
    return None if inv is None or investigation_violation(inv) else inv


def investigation_violation(inv: Investigation) -> str | None:
    """④ の調査結果が載せられない理由（分類）。載せられるなら None。

    分類は too_long（発生した事象の文字数）・bad_confidence・empty（空の欄）・bad_target・
    forbidden_text（D2・D3）。理由に AI の文は入れない。
    """
    if inv.confidence not in CONFIDENCES:
        return "bad_confidence"
    if len(inv.event) > EVENT_LIMIT * LIMIT_FACTOR:
        return "too_long"
    texts = (inv.event, inv.cause, *inv.contents, *(m.text for m in inv.measures))
    # 調査内容・対策案がない、または空・空白だけ・無害化の後に空になる文がある
    if not (inv.contents and inv.measures) or not all(sanitize_text(t) for t in texts):
        return "empty"
    if any(m.target not in TARGETS for m in inv.measures):
        return "bad_target"
    return "forbidden_text" if any(text_violation(t) for t in texts) else None


def _investigation(inv: Investigation) -> str:
    if len(inv.evidence) > MAX_EVIDENCE_BLOCKS or any(
        sum(len(_split(line)) for line in b) > MAX_EVIDENCE_LINES for b in inv.evidence
    ):
        raise ValueError("根拠の抜粋が上限を超えている")
    cause = [f"確度：{inv.confidence}。{sanitize_text(inv.cause)}"]
    cause += [code_fence(block) for block in inv.evidence]
    parts = [
        "## 調査結果",
        "### 発生した事象\n\n" + sanitize_text(inv.event),
        "### 推定される原因と根拠\n\n" + "\n\n".join(cause),
        "### 調査内容\n\n" + _bullets(inv.contents),
        "### 対策案\n\n" + _measures(inv.measures),
    ]
    return "\n\n".join(parts)


def _measures(measures: Sequence[Countermeasure]) -> str:
    return "\n".join(f"- **{TARGETS[m.target]}**：{sanitize_text(m.text)}" for m in measures)


def _bullets(texts: Sequence[str]) -> str:
    return "\n".join(f"- {sanitize_text(t)}" for t in texts)
