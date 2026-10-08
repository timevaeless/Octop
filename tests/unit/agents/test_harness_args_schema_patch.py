"""Tests for the octop-side patch of octop-harness ``mcp_args_model``.

Upstream maps JSON Schema ``object``/``array`` to ``str`` (only
integer/number/boolean handled), so the LLM-facing tool schema advertised
nested DTOs as strings and models serialised JSON into a string (2026-10-08
LAAC ``submit`` incident). The patch maps object→dict, array→list and keeps
nested subschemas visible via ``json_schema_extra``.
"""

from __future__ import annotations

from typing import Any

import pytest

from octop.infra.agents.harness_args_schema_patch import (
    apply_harness_args_schema_patch,
)


@pytest.fixture(name="patched")
def _patched() -> None:
    apply_harness_args_schema_patch()


def _build(schema: dict[str, Any]) -> Any:
    from octop_harness.mcp import mcp_args_model

    return mcp_args_model("submit", schema)


def test_patch_is_idempotent() -> None:
    import octop_harness.mcp as harness_mcp

    already = getattr(harness_mcp, "_octop_args_schema_patched", False)
    applied = apply_harness_args_schema_patch()
    assert applied is not already  # flips exactly once
    assert apply_harness_args_schema_patch() is False  # second call is a no-op
    assert getattr(harness_mcp, "_octop_args_schema_patched", False) is True


def test_object_arg_maps_to_dict_with_nested_properties(patched: None) -> None:
    schema = {
        "type": "object",
        "properties": {
            "applicantInfo": {
                "type": "object",
                "description": "申请人信息",
                "properties": {
                    "name": {"type": "string"},
                    "idNumber": {"type": "string"},
                },
            },
        },
        "required": ["applicantInfo"],
    }
    model = _build(schema)
    model_schema = model.model_json_schema()
    prop = model_schema["properties"]["applicantInfo"]
    assert prop["type"] == "object"
    # Nested properties must survive into the LLM-facing schema.
    assert set(prop["properties"]) == {"name", "idNumber"}


def test_array_arg_maps_to_list_with_items(patched: None) -> None:
    schema = {
        "type": "object",
        "properties": {
            "guarantorInfos": {
                "type": "array",
                "items": {"type": "object", "properties": {"name": {"type": "string"}}},
            },
        },
    }
    model = _build(schema)
    prop = model.model_json_schema()["properties"]["guarantorInfos"]
    assert prop["type"] == "array"
    assert prop["items"]["type"] == "object"


def test_scalars_keep_upstream_mapping(patched: None) -> None:
    schema = {
        "type": "object",
        "properties": {
            "count": {"type": "integer"},
            "amount": {"type": "number"},
            "flag": {"type": "boolean"},
            "note": {"type": "string"},
        },
        "required": ["count", "amount", "flag", "note"],
    }
    model = _build(schema)
    hints = model.model_fields
    assert hints["count"].annotation is int
    assert hints["amount"].annotation is float
    assert hints["flag"].annotation is bool
    assert hints["note"].annotation is str


def test_optional_and_description_preserved(patched: None) -> None:
    schema = {
        "type": "object",
        "properties": {
            "vehicleInfo": {"type": "object", "description": "车辆信息"},
        },
    }
    model = _build(schema)
    field = model.model_fields["vehicleInfo"]
    assert field.description == "车辆信息"
    assert field.default is None  # optional (not in `required`)


def test_object_arg_validates_dict_and_rejects_string(patched: None) -> None:
    schema = {
        "type": "object",
        "properties": {"applicantInfo": {"type": "object"}},
        "required": ["applicantInfo"],
    }
    model = _build(schema)
    # A real object validates — this is what fixed behaviour looks like.
    assert model(applicantInfo={"name": "Lim"}) is not None
    # The old failure mode (JSON stringified into a string) now fails fast
    # instead of silently reaching the backend.
    with pytest.raises(Exception):
        model(applicantInfo='{"name": "Lim"}')


def test_renamed_field_keeps_alias_roundtrip(patched: None) -> None:
    schema = {
        "type": "object",
        "properties": {"1st-choice": {"type": "object"}},
        "required": ["1st-choice"],
    }
    model = _build(schema)
    field_name = next(iter(model.model_fields))
    assert field_name != "1st-choice"  # sanitised python identifier
    dumped = model(**{"1st-choice": {"a": 1}}).model_dump(by_alias=True)
    assert dumped == {"1st-choice": {"a": 1}}
