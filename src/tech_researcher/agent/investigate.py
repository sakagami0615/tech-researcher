"""④ 失敗時の調査の入力と出力の検査（data-model.md 4.4、report-templates.md 3.2・3.5、D-07）。

- 入力：失敗の定型の項目（コードが作った値）と、ログ・検証結果・履歴の抜粋を1行1要素にして行 ID
  （L001, L002, …）を付けたもの。行の文は、コードが作った分類・数・名前だけから組み立てる。
  ログは issue_fields の項目だけを使い、detail・exception（外部由来の文や、除外した項目の本文・
  タイトルを含みうる）は入れない。検証結果は合否・分類・件数と、形の正しい項目 ID だけにする。
  どの行も秘匿情報の検出器でマスクしてから入れる（US-E6-06）
- 出力：スキーマ（D6(a)）・D2・D3・確度・空の欄・文字数を検査し、違反したら AI を利用できない
  ときの定型（3.5）に落とす。根拠の行は AI が選んだ ID から、コードが元の行を取り出す（存在しない
  ID は捨てる。10行×3ブロックまで）。AI の出力の全文と根拠の抜粋は秘匿情報の検査にもかけ、
  見つかれば AI の文も抜粋も載せない 3.5 の定型にする（③ と同じく、捨てる文の中も見る）
- ② がイメージにスキルがない・docker を起動できないで失敗した実行エラーでは、AI を利用できない
  ものとして ④ を呼ばない（D-04）
- ④ は再試行しない（structure.md 6.3）。失敗したら定型にする

統括スクリプトの失敗の分岐へのつなぎ込みと、investigation.json・runs の investigation_json への
保存は failures.py で行う。
"""

import json
import re
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

from tech_researcher.agent import invoke
from tech_researcher.db.runs import RunHistory
from tech_researcher.logging import issue_fields
from tech_researcher.periods import JST, Target
from tech_researcher.render.failure import (
    STAGES,
    Countermeasure,
    Investigation,
    investigation_violation,
    select_evidence,
)
from tech_researcher.validate.schema import INVESTIGATE_OUTPUT_SCHEMA, check_schema
from tech_researcher.validate.secret_scan import SecretScanner

INVESTIGATE_SCHEMA = resources.files("tech_researcher") / "schemas" / INVESTIGATE_OUTPUT_SCHEMA

INVESTIGATION = "investigation.json"  # ④ の出力（検査を通ったもの）。実行のフォルダに置く
KINDS = ("run_error", "rejected")  # 実行エラー・不合格
CONFIDENCE_LABELS = {"high": "高", "medium": "中", "low": "低"}  # 表記はコードが変える（4.4）
HISTORY_DAYS = 7  # 履歴の行にする直近の日数（data-model.md 4.4 の例）
# ② の失敗のうち、④ も同じく動かせないと分かっている分類。AI 自体を利用できない失敗として ④ を
# 飛ばす（D-04）。イメージにスキルがない（missing_skill）、docker を起動できない（launch_failed）。
# ほかの ② の失敗（agent_failed・bad_output・timeout、claude を呼ばずに止めた input_too_large など）
# では ④ を呼び、④ が失敗したら定型にする（調査結果が載る場面を優先する。失敗の日はまれ）。
# 利用上限・認証の失敗の分類を足したら、その分類もここに入れる
AGENT_UNUSABLE = frozenset({"missing_skill", "launch_failed"})

# 検証結果の行に入れてよい項目 ID の形（i{主キー}・ドライランの仮の d{番号}。2.6）
_ITEM_ID = re.compile(r"[id][0-9]{1,18}")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Failure:
    """④ に渡す失敗の定型の項目（report-templates.md 3.3・3.4 の概要）。値はすべてコードが作る。

    error_category は Issue に書くエラーの概要（コードが分類した短い文）、error_code は
    統括スクリプトの失敗の分類（AI を利用できるかの判定にだけ使い、入力には入れない）。
    rejected_checks は不合格となった検証項目（不合格のときだけ）。
    """

    kind: str
    target: Target
    failed_stage: str | None = None
    error_category: str | None = None
    error_code: str | None = None
    retry_count: int | None = None
    rejected_checks: tuple[str, ...] | None = None

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"知らない失敗の種類 {self.kind!r}")
        if (self.kind == "run_error") != (self.failed_stage is not None):
            raise ValueError("failed_stage は実行エラーのときだけ必要")
        if self.failed_stage is not None and self.failed_stage not in STAGES:
            raise ValueError(f"処理段階にない {self.failed_stage!r}")
        if (self.kind == "rejected") != (self.rejected_checks is not None):
            raise ValueError("rejected_checks は不合格のときだけ必要")
        if self.kind == "run_error" and not self.error_category:
            raise ValueError("実行エラーにはエラーの概要が要る")


@dataclass(frozen=True)
class Line:
    """④ に渡す1行。kind は log・verification・history。"""

    line_id: str
    kind: str
    text: str


@dataclass(frozen=True)
class InvestigateResult:
    """④ の結果。investigation が None なら 3.5 の定型にし、reason にその分類を入れる。

    reason：agent_unusable（④ を呼ばなかった）、AgentError の分類・launch_failed・
    agent_failed（想定外の例外も含む。④ の失敗）、invalid_output（D6(a)）、
    investigation_violation の分類（D2・D3 など）、secret_detected（AI の出力か根拠の抜粋に
    秘匿情報があった。AI の文と抜粋を載せない 3.5 の定型にし、secret_kinds をログに残す）。
    output は検査を通った出力（investigation.json・runs の investigation_json に残すもの）。

    ログ（structure.md 3.3 の usage の行と failure の行）に残す値も持つ：called は AI を
    呼んだか、input_bytes は標準入力のバイト数、wall_ms はコンテナの起動を含めた所要時間、
    models・usage は結果の JSON の値、detail は失敗の詳細（stderr と Claude Code の分類。
    呼び出し側がマスクしてから残す）、remove_failed は残ったコンテナを消せなかったか。
    """

    investigation: Investigation | None
    reason: str | None = None
    output: dict | None = None
    secret_kinds: tuple[str, ...] = ()
    called: bool = False
    input_bytes: int | None = None
    wall_ms: int | None = None
    models: tuple[str, ...] = ()
    usage: invoke.AgentUsage | None = None
    detail: str = ""
    remove_failed: bool = False


def agent_unusable(failure: Failure) -> bool:
    """② が AGENT_UNUSABLE の分類で失敗した実行エラーか（AI を使えないので ④ を飛ばす。D-04）。"""
    return failure.failed_stage == "analyze" and failure.error_code in AGENT_UNUSABLE


def log_lines(path: Path) -> list[str]:
    """1回の実行のログ（JSON Lines）から、許可した項目（issue_fields）だけで作った行。

    detail・exception は使わない（外部由来の文や、除外した項目の本文・タイトルを含みうる）。
    読めない行（JSON でない、UTF-8 として読めない。最後の行が書き込みの途中で切れたときなど）は
    飛ばす。時刻は JST の時:分:秒にする。
    """
    lines = []
    for raw in path.read_bytes().splitlines():
        try:
            record = json.loads(raw.decode("utf-8"))
        except ValueError:  # UnicodeDecodeError・JSONDecodeError
            continue
        if not isinstance(record, dict) or "stage" not in record or "event" not in record:
            continue
        fields = issue_fields(record)
        head = [_time(fields.get("time")), _value(fields["stage"]), _value(fields["event"])]
        tail = [
            f"{name}={_value(fields[name])}"
            for name in ("source", "error_category", "count", "attempt")
            if name in fields
        ]
        lines.append(" ".join(p for p in head + tail if p))
    return lines


def verification_lines(validation: Mapping) -> list[str]:
    """③ の結果（validation.json）から、合否・分類・件数と、除外した項目の ID だけの行。

    除外の理由の文（AI が返した ID の文字列を含みうる）は入れない。除外の分類はログの行
    （excluded の error_category）にある。項目 ID は形の正しいものだけにし、ほかは数だけにする。
    """
    excluded = validation.get("excluded")
    excluded = excluded if isinstance(excluded, list) else []
    lines = [
        f"verify result={_value(validation.get('result'))} "
        f"reason={_value(validation.get('reason'))} "
        f"entries={_value(validation.get('entries'))} excluded={len(excluded)}"
    ]
    for entry in excluded:
        ids = entry.get("item_ids") if isinstance(entry, dict) else None
        ids = ids if isinstance(ids, list) else []
        good = [i for i in ids if isinstance(i, str) and _ITEM_ID.fullmatch(i)]
        text = f"verify excluded item_ids={','.join(good) or 'none'}"
        if len(good) != len(ids):
            text += f" malformed_ids={len(ids) - len(good)}"
        lines.append(text)
    return lines


def history_lines(history: Iterable[RunHistory]) -> list[str]:
    """直近の実行の履歴（db/runs.py の recent_runs）を、1実行1行の文にする。"""
    lines = []
    for run in history:
        parts = [
            f"{run.started_at.astimezone(JST):%Y-%m-%d %H:%M}",
            run.cadence,
            f"target={run.target}",
            f"status={run.status}",
        ]
        for name, value in (
            ("failed_stage", run.failed_stage),
            ("error_category", run.error_category),
            ("retry_count", run.retry_count),
        ):
            if value is not None:
                parts.append(f"{name}={_value(value)}")
        if run.rejected_checks:
            parts.append(f"rejected_checks={','.join(map(_value, run.rejected_checks))}")
        if run.fetches:
            parts.append("sources " + " ".join(f"{s}={n}" for s, n in sorted(run.fetches.items())))
        if run.failed_sources:
            failed = ",".join(f"{s}:{c or 'unknown'}" for s, c in run.failed_sources)
            parts.append(f"failed_sources={_value(failed)}")
        if run.partial_sources:
            partial = ",".join(f"{s}:{n}" for s, n in run.partial_sources)
            parts.append(f"partial_sources={_value(partial)}")
        lines.append(" ".join(parts))
    return lines


def number_lines(
    scanner: SecretScanner,
    log: Sequence[str] = (),
    verification: Sequence[str] = (),
    history: Sequence[str] = (),
) -> list[Line]:
    """行に ID（L001, L002, …）を付ける。どの行もマスクしてから入れる（US-E6-06）。"""
    groups = (("log", log), ("verification", verification), ("history", history))
    texts = [(kind, text) for kind, group in groups for text in group]
    return [
        Line(f"L{n:03d}", kind, _value(scanner.mask(text)))
        for n, (kind, text) in enumerate(texts, 1)
    ]


def build_investigate_input(
    failure: Failure, lines: Sequence[Line], scanner: SecretScanner
) -> dict:
    """④ に渡す入力（data-model.md 4.4）。"""
    target = failure.target
    category = failure.error_category
    return {
        "task": "investigate",
        "failure": {
            "kind": failure.kind,
            "cadence": target.cadence,
            "target": target.key.isoformat(),
            "period": {
                "start": target.period.start.astimezone(JST).isoformat(),
                "end": target.period.end.astimezone(JST).isoformat(),
            },
            "failed_stage": failure.failed_stage,
            "error_category": None if category is None else scanner.mask(category),
            "retry_count": failure.retry_count,
            "rejected_checks": None
            if failure.rejected_checks is None
            else [scanner.mask(c) for c in failure.rejected_checks],
        },
        "lines": [{"id": line.line_id, "kind": line.kind, "text": line.text} for line in lines],
    }


def parse_investigation(output: object, lines: Sequence[Line]) -> Investigation:
    """構造化出力をスキーマで検査し（D6(a)。合わなければ ValueError）、調査結果にする。

    根拠は、選ばれた ID のうち入力にある行を、元の行から取り出す（存在しない ID は捨てる）。
    """
    check_schema(output, INVESTIGATE_OUTPUT_SCHEMA)
    texts = {line.line_id: line.text for line in lines}
    return Investigation(
        event=output["event"],
        cause=output["cause"],
        confidence=CONFIDENCE_LABELS[output["confidence"]],
        contents=tuple(output["investigation"]),
        measures=tuple(Countermeasure(m["target"], m["text"]) for m in output["countermeasures"]),
        evidence=select_evidence(texts, output["evidence_line_ids"]),
    )


def investigate(
    failure: Failure,
    lines: Sequence[Line],
    timeout_s: float,
    *,
    runner: invoke.Runner,
    scanner: SecretScanner,
    max_turns: int | None,
    max_input_bytes: int,
    schema: Path | Traversable = INVESTIGATE_SCHEMA,
) -> InvestigateResult:
    """④ を1回だけ呼び（再試行しない）、検査を通った調査結果を返す。

    timeout_s は ④ の上限（limits の investigate）、max_input_bytes は入力の上限。AI を利用
    できない失敗（agent_unusable）では呼ばない。
    """
    if agent_unusable(failure):
        return InvestigateResult(None, "agent_unusable")
    data = build_investigate_input(failure, lines, scanner)
    body = json.dumps(data, ensure_ascii=False).encode()
    sent = {"input_bytes": len(body)}
    started = time.monotonic()
    try:
        result = invoke.call_agent(
            body,
            timeout_s,
            runner=runner,
            skill=invoke.INVESTIGATE_SKILL,
            schema=schema,
            max_turns=max_turns,
            max_input_bytes=max_input_bytes,
        )
    except invoke.AgentError as e:
        return InvestigateResult(
            None,
            e.category,
            called=e.category not in invoke.NOT_CALLED,
            models=e.models,
            usage=e.usage,
            detail=e.detail,
            remove_failed=e.remove_failed,
            wall_ms=_elapsed(started),
            **sent,
        )
    except OSError as e:  # docker を起動できない
        return InvestigateResult(
            None, "launch_failed", detail=type(e).__name__, wall_ms=_elapsed(started), **sent
        )
    except Exception as e:
        # AI を呼んだ後の想定外の失敗。④ は失敗しても定型にする（structure.md 6.3）
        return InvestigateResult(
            None,
            "agent_failed",
            called=True,
            detail=type(e).__name__,
            wall_ms=_elapsed(started),
            **sent,
        )
    done = {
        "called": True,
        "models": result.models,
        "usage": result.usage,
        "wall_ms": _elapsed(started),
        **sent,
    }
    try:
        investigation, reason, kinds = _check_output(result.output, lines, scanner)
    except Exception:
        # 検査できない出力（深すぎる入れ子など）。載せずに定型にする（structure.md 6.3）
        return InvestigateResult(None, "invalid_output", **done)
    if investigation is None:
        return InvestigateResult(None, reason, secret_kinds=kinds, **done)
    return InvestigateResult(investigation, output=result.output, **done)


def _check_output(
    output: object, lines: Sequence[Line], scanner: SecretScanner
) -> tuple[Investigation | None, str | None, tuple[str, ...]]:
    """出力を検査し、（調査結果、載せない理由、秘匿情報の分類）を返す。秘匿情報を先に見る。"""
    # 1つずつの文字列と、無害化の前の全文を検査する（③ と同じ。stages/daily.py）。形の検査より
    # 先に行い、スキーマに合わない出力の中の秘匿値も見落とさない。文字列は再帰を使わずに集める
    # ので、深すぎる入れ子でも先に見られる。全文の JSON にできない出力は、形の検査で落ちる
    texts = list(_strings(output))
    try:
        texts.append(json.dumps(output, ensure_ascii=False))
    except RecursionError, TypeError, ValueError:
        pass
    if kinds := _secret_kinds(scanner, texts):
        return None, "secret_detected", kinds
    try:
        investigation = parse_investigation(output, lines)
    except ValueError:
        return None, "invalid_output", ()
    # 行はマスクしてから入れているので、通常は見つからない。抜粋も投稿前の検査の対象とする
    # 決まり（report-templates.md 3.2・US-E7-05）の保険として残す。分類をログに残せるよう、
    # 文章の違反より先に見る
    evidence = [line for block in investigation.evidence for line in block]
    if kinds := _secret_kinds(scanner, evidence):
        return None, "secret_detected", kinds
    if reason := investigation_violation(investigation):
        return None, reason, ()
    return investigation, None, ()


def _elapsed(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _secret_kinds(scanner: SecretScanner, texts: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({f.kind for text in texts for f in scanner.find(text)}))


def _strings(value: object) -> Iterator[str]:
    """JSON の値の、すべてのキーと文字列の値。入れ子が深くても止まらないよう、再帰を使わない。"""
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            yield node
        elif isinstance(node, dict):
            for key, item in node.items():
                yield str(key)
                stack.append(item)
        elif isinstance(node, list):
            stack.extend(node)


def _time(value: object) -> str:
    if not isinstance(value, str):
        return ""
    try:
        return f"{datetime.fromisoformat(value).astimezone(JST):%H:%M:%S}"
    except ValueError:
        return ""


def _value(value: object) -> str:
    """1行に収まる値（改行・連続する空白は1つの空白にする）。None は none。"""
    return "none" if value is None else _SPACE.sub(" ", str(value)).strip()
