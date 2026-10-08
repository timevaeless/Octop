"""Runtime patch for octop-harness ``mcp.mcp_args_model`` type mapping.

Upstream (PyPI octop-harness 1.0.x) builds the Pydantic args model for every
MCP tool from the server's JSON Schema, but maps unknown JSON types to
``str``: only ``integer``/``number``/``boolean`` are handled, so
``{"type": "object"}`` and ``{"type": "array"}`` fields become ``str`` in the
LLM-facing tool schema. The model then serialises nested JSON into a string
(observed 2026-10-08: ``submit``'s ``applicantInfo`` object arrived at LAAC
as ``"{\\"name\\": …}"`` and Jackson rejected it — an OpenAPI import and a
catalog re-publish were both already correct, the coercion happened here).

This module is a pluggable octop-side fix: it replaces
``octop_harness.mcp.mcp_args_model`` with a version that maps ``object`` to
``dict`` and ``array`` to ``list`` and attaches the original JSON subschema
via ``json_schema_extra`` so nested ``properties``/``items`` still reach the
LLM. Everything else (required/optional handling, renamed-field aliases,
descriptions, the ``_McpArgsBase`` null-dropping base) replays upstream.
Delete the hook in ``AgentManager`` (and this module) once upstream maps
object/array correctly.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_PATCH_FLAG = "_octop_args_schema_patched"


def _patched_mcp_args_model(tool_name: str, input_schema: dict[str, Any]) -> type[Any]:
    """Upstream ``mcp_args_model`` with object/array mapped to real containers."""
    import re  # noqa: PLC0415

    from pydantic import Field, create_model  # noqa: PLC0415

    from octop_harness.mcp import (  # noqa: PLC0415
        _McpArgsBase,
        _pydantic_field_name,
    )

    props = input_schema.get("properties") or {}
    if not isinstance(props, dict):
        props = {}
    required = set(input_schema.get("required") or [])

    fields: dict[str, Any] = {}
    used_names: set[str] = set()
    for key, spec in props.items():
        spec_dict: dict[str, Any] = spec if isinstance(spec, dict) else {}
        json_type = spec_dict.get("type")
        if json_type == "integer":
            py_type: Any = int
        elif json_type == "number":
            py_type = float
        elif json_type == "boolean":
            py_type = bool
        elif json_type == "object":
            py_type = dict
        elif json_type == "array":
            py_type = list
        else:
            py_type = str
        desc = spec_dict.get("description")
        field_name = _pydantic_field_name(str(key), used_names)
        # Pass the original subschema through so nested object properties /
        # array items stay visible in the LLM-facing JSON schema. A callable
        # extra mutates the generated schema in place — pydantic's dict-merge
        # form lets its own ``items: {}`` override ours.
        extra: Any = None
        if json_type in ("object", "array"):
            extra_spec = {
                k: v for k, v in spec_dict.items() if k not in ("description", "title")
            }

            def _apply_extra(schema: dict[str, Any], _extra: dict[str, Any] = extra_spec,
                             _json_type: str = json_type) -> None:
                # Optional fields carry an ``anyOf`` wrapper at this stage (the
                # base class flattens it later, keeping only the non-null
                # variant) — mutate the variant that matches, not the wrapper.
                variants = schema.get("anyOf")
                if isinstance(variants, list):
                    for variant in variants:
                        if isinstance(variant, dict) and variant.get("type") == _json_type:
                            variant.update(_extra)
                    return
                schema.update(_extra)

            extra = _apply_extra
        if field_name != key:
            field = Field(
                description=desc,
                validation_alias=key,
                serialization_alias=key,
                json_schema_extra=extra,
            )
        else:
            field = Field(description=desc, json_schema_extra=extra)
        if key in required:
            fields[field_name] = (py_type, field)
        else:
            optional_field = (
                Field(
                    default=None,
                    description=desc,
                    validation_alias=key,
                    serialization_alias=key,
                    json_schema_extra=extra,
                )
                if field_name != key
                else Field(default=None, description=desc, json_schema_extra=extra)
            )
            fields[field_name] = (py_type | None, optional_field)

    model_name = re.sub(r"[^A-Za-z0-9_]", "_", tool_name)
    if not model_name or model_name[0].isdigit():
        model_name = f"Mcp_{model_name}"
    return create_model(model_name, __base__=_McpArgsBase, **fields)


def apply_harness_args_schema_patch() -> bool:
    """Idempotently patch ``octop_harness.mcp.mcp_args_model``.

    Returns ``True`` when the patch was applied now, ``False`` when it was
    already in place.
    """
    import octop_harness.mcp as harness_mcp  # noqa: PLC0415

    if getattr(harness_mcp, _PATCH_FLAG, False):
        return False

    setattr(harness_mcp, _PATCH_FLAG, True)
    harness_mcp.mcp_args_model = _patched_mcp_args_model
    logger.info(
        "patched octop_harness.mcp.mcp_args_model: object->dict, array->list "
        "(nested JSON schemas reach the LLM)"
    )
    return True


__all__ = ["apply_harness_args_schema_patch"]
