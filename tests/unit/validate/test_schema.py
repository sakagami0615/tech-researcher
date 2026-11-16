import json

import pytest

from tech_researcher.agent.daily import CATEGORIES, DEPTHS
from tech_researcher.validate.schema import DAILY_OUTPUT_SCHEMA, check_schema, schema_text

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
    banned = {"minLength", "maxLength", "minItems", "maxItems", "pattern", "format", "minimum"}
    assert not banned & _keys(json.loads(schema_text(DAILY_OUTPUT_SCHEMA)))


def test_schema_enums_match_code_constants():
    item = json.loads(schema_text(DAILY_OUTPUT_SCHEMA))["properties"]["items"]["items"]
    assert tuple(item["properties"]["category"]["enum"]) == CATEGORIES
    assert tuple(item["properties"]["depth"]["enum"]) == DEPTHS


def test_schema_has_no_url_property():
    """出典は ID だけで、URL を書かせない（D-06）。"""
    item = json.loads(schema_text(DAILY_OUTPUT_SCHEMA))["properties"]["items"]["items"]
    assert not [k for k in item["properties"] if "url" in k]


def _keys(node):
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in _keys(v)}
    return set()
