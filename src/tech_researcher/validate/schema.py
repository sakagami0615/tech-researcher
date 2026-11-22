"""D6(a) 出力全体の形の検査（D-07、data-model.md 4.1）。

スキーマは `schemas/` のファイル1つを、統括スクリプトが `--json-schema` に渡し、ここでも
同じものを検査に使う。スキーマに書くのは型・必須・enum・null の可否・追加のプロパティの禁止だけ。
文字数・要素数・ID の存在は、スキーマに入れずコードで確かめる（入れると全体が不合格になり、
仕様より厳しくなる）。

スキーマには `$schema` を書かない。Claude Code（2.1.311）の `--json-schema` が draft 2020-12 の
`$schema` を受け付けず、起動の時点で止まるため。② の側（Claude Code）がどの規格でスキーマを
解釈するかは明示されていないので、② と ③ で意味が同じと確かめたキーワード（型・必須・enum・
anyOf・追加のプロパティの禁止など）だけを使う。③ はここで draft 2020-12 の検査器を名指しで使う。
使えるキーワードは tests/unit/validate/test_schema.py の ALLOWED_KEYWORDS で守る（data-model.md
4.5）。
"""

import json
from functools import cache
from importlib import resources

from jsonschema import Draft202012Validator

DAILY_OUTPUT_SCHEMA = "daily_output.json"
WEEKLY_OUTPUT_SCHEMA = "weekly_output.json"  # 週次の ② の出力（data-model.md 4.3）
INVESTIGATE_OUTPUT_SCHEMA = "investigate_output.json"  # ④ の出力（data-model.md 4.4）


@cache
def schema_text(name: str) -> str:
    """スキーマのファイルの中身（③ の検査用）。

    `--json-schema` には agent/invoke.py の load_schema が同じファイルを1行にして渡す。
    """
    return (resources.files("tech_researcher") / "schemas" / name).read_text(encoding="utf-8")


@cache
def _validator(name: str) -> Draft202012Validator:
    schema = json.loads(schema_text(name))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def check_schema(output: object, name: str = DAILY_OUTPUT_SCHEMA) -> None:
    """出力がスキーマに合わなければ ValueError。出力がない（None）場合も違反。"""
    if output is None:
        raise ValueError("出力がない")
    errors = sorted(_validator(name).iter_errors(output), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        where = "/".join(str(p) for p in first.absolute_path) or "(全体)"
        # AI の出力の値を含みうる first.message は使わず、場所と違反の種類だけを残す
        raise ValueError(f"スキーマに合わない（{where}：{first.validator}）")
