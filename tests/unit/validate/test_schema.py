import json

import pytest

from tech_researcher.agent.daily import CATEGORIES, DEPTHS
from tech_researcher.agent.investigate import CONFIDENCES
from tech_researcher.render.failure import TARGETS
from tech_researcher.validate.schema import (
    DAILY_OUTPUT_SCHEMA,
    INVESTIGATE_OUTPUT_SCHEMA,
    check_schema,
    schema_text,
)

ITEM = {
    "item_ids": ["i1"],
    "primary_item_id": "i1",
    "primary_link_id": None,
    "category": "cloud",
    "rank": 1,
    "depth": "full",
    "headline": "見出し",
    "points": ["要点"],
    "importance": "理由",
    "period_event": None,
}

# スキーマに使ってよいキーワード（型・必須・enum・null の可否・追加プロパティの禁止）。
# ほかのキーワード（minLength・pattern・format など）が入ったら失敗する。`$schema` も入れない
# （Claude Code 2.1.311 の --json-schema は draft 2020-12 の `$schema` を受け付けず、起動の時点で
# 止まる。トークンなしの実行で確認。検査の側は Draft202012Validator を名指しで使う）
ALLOWED_KEYWORDS = {
    "title",
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "anyOf",
}
PROPERTY_NAMES = set(ITEM) | {"items"}


def test_valid_output_passes():
    check_schema({"items": []})
    check_schema({"items": [ITEM, {**ITEM, "primary_item_id": None, "primary_link_id": "i1-l1"}]})


@pytest.mark.parametrize(
    "output",
    [
        None,  # 出力がない場合も違反
        [],
        {},
        {"items": None},
        {"items": [], "extra": 1},  # 追加のプロパティの禁止
        {"items": [{**ITEM, "url": "https://example.com"}]},
        {"items": [{k: v for k, v in ITEM.items() if k != "importance"}]},  # 必須
        {"items": [{k: v for k, v in ITEM.items() if k != "period_event"}]},  # null でも必須
        {"items": [{**ITEM, "category": "security"}]},  # enum
        {"items": [{**ITEM, "depth": "long"}]},
        {"items": [{**ITEM, "headline": None}]},  # null の可否
        {"items": [{**ITEM, "item_ids": None}]},
        {"items": [{**ITEM, "points": [None]}]},
        {"items": [{**ITEM, "primary_item_id": 3}]},
        {"items": [{**ITEM, "rank": "1"}]},
        {"items": [{**ITEM, "rank": True}]},
        {"items": [{**ITEM, "rank": 1.5}]},
    ],
)
def test_output_not_matching_schema_is_rejected(output):
    with pytest.raises(ValueError):
        check_schema(output)


def test_error_message_has_place_but_not_value():
    secret = "ghp-secret-value"
    with pytest.raises(ValueError) as e:
        check_schema({"items": [{**ITEM, "category": secret}]})
    assert "items/0/category" in str(e.value) and secret not in str(e.value)


def test_length_and_id_existence_are_not_in_the_schema():
    """文字数・要素数・ID の存在はスキーマに入れず、コードで確かめる（data-model.md 4.1）。"""
    check_schema(
        {
            "items": [
                {
                    **ITEM,
                    "item_ids": [],
                    "headline": "あ" * 10000,
                    "points": ["要点"] * 50,
                    "primary_item_id": "no-such-id",
                }
            ]
        }
    )
    used = _keys(json.loads(schema_text(DAILY_OUTPUT_SCHEMA)))
    assert used - ALLOWED_KEYWORDS - PROPERTY_NAMES == set()


def test_schema_enums_match_code_constants():
    item = json.loads(schema_text(DAILY_OUTPUT_SCHEMA))["properties"]["items"]["items"]
    assert tuple(item["properties"]["category"]["enum"]) == CATEGORIES
    assert tuple(item["properties"]["depth"]["enum"]) == DEPTHS


def test_schema_has_no_url_property():
    """出典は ID だけで、URL を書かせない（D-06）。"""
    item = json.loads(schema_text(DAILY_OUTPUT_SCHEMA))["properties"]["items"]["items"]
    assert not [k for k in item["properties"] if "url" in k]


# --- ④ 失敗時の調査（data-model.md 4.4） ---

INVESTIGATION = {
    "event": "事象",
    "cause": "原因",
    "confidence": "high",
    "investigation": ["確認したこと"],
    "countermeasures": [{"target": "code", "text": "対策"}],
    "evidence_line_ids": ["L001"],
}


def test_investigate_schema_uses_only_allowed_keywords():
    schema = json.loads(schema_text(INVESTIGATE_OUTPUT_SCHEMA))
    names = set(INVESTIGATION) | {"target", "text"}
    assert _keys(schema) - ALLOWED_KEYWORDS - names == set()
    assert not [k for k in schema["properties"] if "url" in k]
    # 文字数・要素数・ID の存在は入れない（空の配列・存在しない ID・長い文もスキーマは通す）
    check_schema(
        {**INVESTIGATION, "investigation": [], "evidence_line_ids": ["x"], "event": "あ" * 9999},
        INVESTIGATE_OUTPUT_SCHEMA,
    )


def test_investigate_schema_enums_match_code_constants():
    props = json.loads(schema_text(INVESTIGATE_OUTPUT_SCHEMA))["properties"]
    assert props["confidence"]["enum"] == list(CONFIDENCES)
    target = props["countermeasures"]["items"]["properties"]["target"]
    assert target["enum"] == list(TARGETS)


@pytest.mark.parametrize(
    "output",
    [
        None,
        {**INVESTIGATION, "extra": 1},
        {**INVESTIGATION, "confidence": "高"},
        {**INVESTIGATION, "cause": None},
        {**INVESTIGATION, "countermeasures": [{"target": "code"}]},
        {**INVESTIGATION, "countermeasures": [{"target": "code", "text": "x", "url": "y"}]},
        {k: v for k, v in INVESTIGATION.items() if k != "investigation"},
    ],
)
def test_investigation_not_matching_schema_is_rejected(output):
    with pytest.raises(ValueError):
        check_schema(output, INVESTIGATE_OUTPUT_SCHEMA)


def _keys(node):
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in _keys(v)}
    return set()
