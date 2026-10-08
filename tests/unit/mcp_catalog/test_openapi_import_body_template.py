"""OpenAPI import request-body template generation (nested-object fix).

Regression context (2026-10-06 submit/applicantInfo incident): the importer
used to render every required body property as a quoted template slot
(``"x":"{{.args.x}}"``), which made the mcp-server plugin stringify nested
object arguments into JSON-text strings; upstream strict Jackson
deserialization then failed with ``MismatchedInputException``. Object/array
properties must render as unquoted JSON slots, and optional object/array
properties must not be silently dropped from the body.
"""

from __future__ import annotations

import pytest

from octop.mcp_catalog.openapi_import import _build_body_template, import_openapi


def _doc(*, schema_props: dict, required: list[str] | None = None) -> dict:
    return {
        "openapi": "3.0.1",
        "paths": {
            "/things": {
                "post": {
                    "operationId": "createThing",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": schema_props,
                                    **({"required": required} if required else {}),
                                }
                            }
                        }
                    },
                }
            }
        },
    }


def _body(template: dict | None) -> str | None:
    return None if template is None else template["request_body"]


class TestRequiredNestedObjectUnquoted:
    def test_required_object_renders_unquoted_slot(self) -> None:
        report = import_openapi(
            _doc(
                schema_props={
                    "applicantInfo": {"type": "object"},
                    "remark": {"type": "string"},
                },
                required=["applicantInfo", "remark"],
            )
        )
        body = _body(report["tools"][0])
        assert body is not None
        assert '"applicantInfo":{{.args.applicantInfo}}' in body
        # string slot stays quoted
        assert '"remark":"{{.args.remark}}"' in body

    def test_required_array_renders_unquoted_slot(self) -> None:
        report = import_openapi(
            _doc(
                schema_props={"guarantorInfos": {"type": "array", "items": {"type": "object"}}},
                required=["guarantorInfos"],
            )
        )
        body = _body(report["tools"][0])
        assert '"guarantorInfos":{{.args.guarantorInfos}}' in body


class TestOptionalNestedObjectNotDropped:
    def test_optional_object_gets_conditional_unquoted_pair(self) -> None:
        report = import_openapi(
            _doc(
                schema_props={
                    "vehicleInfo": {"type": "object"},
                    "note": {"type": "string"},
                },
                required=["note"],
            )
        )
        body = _body(report["tools"][0])
        assert body is not None
        # previously optional non-scalar properties were silently dropped
        assert '{{if .args.vehicleInfo}},"vehicleInfo":{{.args.vehicleInfo}}{{end}}' in body

    def test_optional_and_required_mix(self) -> None:
        report = import_openapi(
            _doc(
                schema_props={
                    "applicantInfo": {"type": "object"},
                    "spouseInfo": {"type": "object"},
                    "authNo": {"type": "string"},
                },
                required=["applicantInfo"],
            )
        )
        body = _body(report["tools"][0])
        assert body is not None
        assert body.startswith('{ "applicantInfo":{{.args.applicantInfo}}')
        assert '{{if .args.spouseInfo}},"spouseInfo":{{.args.spouseInfo}}{{end}}' in body
        assert '{{if .args.authNo}},"authNo":"{{.args.authNo}}"{{end}}' in body
        assert body.endswith("}")


class TestNoRequiredFields:
    def test_optional_only_still_valid(self) -> None:
        report = import_openapi(
            _doc(
                schema_props={
                    "a": {"type": "string"},
                    "b": {"type": "object"},
                }
            )
        )
        body = _body(report["tools"][0])
        assert body is not None
        assert body.startswith("{ ") and body.endswith(" }")
        assert '{{if .args.a}}"a":"{{.args.a}}"{{end}}' in body
        # second optional guards its comma on any earlier optional being present
        assert '{{if .args.b}}{{if or .args.a}},{{end}}"b":{{.args.b}}{{end}}' in body


class TestBuildBodyTemplateDirect:
    def test_required_pairs_joined_without_json_quoting(self) -> None:
        template = _build_body_template(
            [
                ("applicantInfo", '"applicantInfo":{{.args.applicantInfo}}'),
                ("remark", '"remark":"{{.args.remark}}"'),
            ],
            [],
        )
        assert template == '{ "applicantInfo":{{.args.applicantInfo}},"remark":"{{.args.remark}}"}'


class TestArgsTypesCarriedThrough:
    def test_arg_declares_real_type(self) -> None:
        report = import_openapi(
            _doc(
                schema_props={
                    "applicantInfo": {"type": "object"},
                    "count": {"type": "integer"},
                },
                required=["applicantInfo", "count"],
            )
        )
        types = {a["name"]: a["type"] for a in report["tools"][0]["parameters"]}
        assert types["applicantInfo"] == "object"
        assert types["count"] == "integer"


class TestSpringdocDtoDoc:
    """End-to-end: a springdoc-style DTO body with $ref'd nested DTOs."""

    def test_nested_ref_dto_gets_unquoted_slot(self) -> None:
        doc = {
            "openapi": "3.0.1",
            "components": {
                "schemas": {
                    "PreApprovalSubmitReq": {
                        "type": "object",
                        "properties": {
                            "applicationType": {"type": "string"},
                            "applicantInfo": {"$ref": "#/components/schemas/ApplicantInfoVO"},
                        },
                        "required": ["applicantInfo"],
                    },
                    "ApplicantInfoVO": {
                        "type": "object",
                        "properties": {"applicantName": {"type": "string"}},
                    },
                }
            },
            "paths": {
                "/mcp-api/loan/pre-approval/submit": {
                    "post": {
                        "operationId": "submitPreApproval",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/PreApprovalSubmitReq"}
                                }
                            }
                        },
                    }
                }
            },
        }
        report = import_openapi(doc)
        tool = report["tools"][0]
        assert tool["tool_name"] == "submit_pre_approval"
        body = tool["request_body"]
        assert body is not None
        assert '"applicantInfo":{{.args.applicantInfo}}' in body
        # unresolved-ref fallback would have produced a quoted slot — guard it
        assert '"applicantInfo":"' not in body


class TestResidualGuard:
    def test_no_quoted_object_slots_anywhere(self) -> None:
        """Any tool whose arg type is object/array must never have a quoted slot."""
        report = import_openapi(
            _doc(
                schema_props={
                    "obj": {"type": "object"},
                    "arr": {"type": "array", "items": {"type": "string"}},
                    "s": {"type": "string"},
                    "n": {"type": "integer"},
                },
                required=["obj", "arr", "s", "n"],
            )
        )
        body = _body(report["tools"][0])
        assert body is not None
        assert '"obj":"' not in body
        assert '"arr":"' not in body
        assert '"s":"' in body
        # required non-string scalars also render unquoted (JSON-typed slot)
        assert '"n":{{.args.n}}' in body
