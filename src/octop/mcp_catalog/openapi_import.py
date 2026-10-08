"""OpenAPI 3 → MCP tool definitions.

Reads a service's OpenAPI document (Spring Boot's ``/v3/api-docs`` is the
canonical source) and produces one tool definition per operation. Conversion
rules are deliberately conservative:

* only ``GET``/``POST``/``PUT``/``DELETE`` operations are imported (the
  mcp-server plugin requestTemplate passes the method through to the
  upstream verbatim — PUT/DELETE verified live against the echo backend
  2026-09-29; other verbs still need review);
* operation names prefer ``operationId`` (camelCase → snake_case) and fall
  back to ``method_pathslug``;
* path/query parameters become plugin ``args`` entries;
* path parameters are rewritten into the plugin's Go-template syntax —
  ``/banks/{id}`` becomes ``/banks/{{.id}}`` so the plugin substitutes the
  argument value instead of requesting the literal URL (review 2.5);
* a JSON request body becomes a Go-template body referencing each property
  via ``{{.args.prop}}`` (the mcp-server plugin exposes tool arguments under
  the ``.args`` data path — verified live 2026-09-25; bare ``{{.prop}}``
  renders to nothing),
* ``$ref`` request-body schemas are resolved against ``#/components/schemas``
  before property extraction (springdoc emits ``{"$ref": …}`` for every DTO
  body — the 2026-10-01 CMC regression: unref'd bodies produced no body
  template at all, the LLM called with ``{}`` and upstream answered 400);
* imported tools default to ``allowed_roles: []`` (visible to nobody) — the
  catalog is fail-closed until an admin assigns roles;
* nothing is silently dropped: every non-convertible or duplicate operation
  lands in the report's ``skipped`` list with a reason (review 2.4);
* KNOWN LIMIT (round-5 §4): ``{{if .x}}`` is falsy for 0 and ``""`` — a
  caller explicitly sending zero / empty-string for an OPTIONAL body field
  silently omits it (Go template truthiness; the plugin does not expose
  ``hasKey``). Documented, not fixed.

Pure functions over ``dict`` — no I/O, fully unit-testable.
"""

from __future__ import annotations

import re
from typing import Any

_CONVERTIBLE_METHODS = ("get", "post", "put", "delete")

# scalar schema types renderable into the conditional body template
_SCALAR_TYPES = ("string", "integer", "number", "boolean")

_OP_ID_NON_WORD = re.compile(r"[^A-Za-z0-9]+")

# {name} in an OpenAPI path → {{.name}} Go-template placeholder
_PATH_PARAM_RE = re.compile(r"\{([A-Za-z0-9_]+)\}")

_REF_PREFIX = "#/components/schemas/"

# cycle/depth guard for recursive $ref resolution
_MAX_DEREF_DEPTH = 12


def _component_schemas(doc: dict[str, Any]) -> dict[str, Any]:
    components = doc.get("components")
    if not isinstance(components, dict):
        return {}
    schemas = components.get("schemas")
    return schemas if isinstance(schemas, dict) else {}


def _ref_name(ref: Any) -> str | None:
    if isinstance(ref, str) and ref.startswith(_REF_PREFIX):
        return ref[len(_REF_PREFIX):] or None
    return None


def _deref(schema: Any, schemas: dict[str, Any], depth: int = 0) -> Any:
    """Resolve ``#/components/schemas/...`` pointers recursively.

    springdoc emits ``{"$ref": …}`` for every DTO — bodies, nested properties
    and arrays alike — and the mcp-server plugin template engine only sees
    concrete properties, so refs must be inlined before property extraction.
    Handles nested ``properties``/``items`` and shallow-merges ``allOf``
    members (springdoc inheritance). Unresolvable refs are returned untouched;
    the ``$ref`` → ``"string"`` type fallback still applies downstream.
    """
    if depth > _MAX_DEREF_DEPTH or not isinstance(schema, dict):
        return schema
    name = _ref_name(schema.get("$ref"))
    if name is not None:
        target = schemas.get(name)
        if not isinstance(target, dict):
            return schema
        return _deref(target, schemas, depth + 1)
    out = dict(schema)
    props = out.get("properties")
    if isinstance(props, dict):
        out["properties"] = {
            k: _deref(v, schemas, depth + 1) for k, v in props.items()
        }
    items = out.get("items")
    if isinstance(items, dict):
        out["items"] = _deref(items, schemas, depth + 1)
    members = out.get("allOf")
    if isinstance(members, list):
        merged: dict[str, Any] = {}
        required: list[str] = []
        for member in members:
            m = _deref(member, schemas, depth + 1)
            if not isinstance(m, dict):
                continue
            member_props = m.get("properties")
            if isinstance(member_props, dict):
                for k, v in member_props.items():
                    merged.setdefault(k, v)
            required.extend(r for r in m.get("required") or [] if r not in required)
        if merged:
            out = {**{k: v for k, v in out.items() if k != "allOf"}, "properties": merged}
            if required:
                out["required"] = required
    # oneOf/anyOf (review 2026-10-02 P1-4): springdoc emits these for union
    # DTOs (interface multi-implementations, Response<T> wrappers) and an
    # unhandled union degrades exactly like the unhandled $ref did — no body
    # template, LLM calls upstream with empty args. Surface the UNION of the
    # member properties so the LLM gets every possible argument; ``required``
    # is deliberately NOT merged (anyOf members are alternatives — none of
    # them is individually mandatory).
    for combo_key in ("oneOf", "anyOf"):
        members = out.get(combo_key)
        if not isinstance(members, list):
            continue
        combo: dict[str, Any] = {}
        for member in members:
            m = _deref(member, schemas, depth + 1)
            member_props = m.get("properties") if isinstance(m, dict) else None
            if isinstance(member_props, dict):
                for k, v in member_props.items():
                    combo.setdefault(k, v)
        if combo:
            out = {
                **{k: v for k, v in out.items() if k != combo_key},
                "properties": combo,
            }
    return out


def _templated_path(path: str) -> str:
    """Rewrite ``{param}`` placeholders into the plugin's ``{{.param}}`` syntax."""
    return _PATH_PARAM_RE.sub(r"{{.\1}}", path)


def _snake_case(operation_id: str) -> str:
    # camelCase → snake_case first (listBanks → list_banks), then split on
    # any remaining non-word separators (get_agent-detail → get_agent_detail).
    camel = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", operation_id.strip())
    parts = _OP_ID_NON_WORD.split(camel)
    name = "_".join(p for p in parts if p).lower()
    return re.sub(r"_+", "_", name).strip("_")


def _path_slug(path: str) -> str:
    return (
        path.strip("/")
        .replace("{", "")
        .replace("}", "")
        .replace("/", "_")
        .replace("-", "_")
        .lower()
    )


def _schema_type(schema: dict[str, Any]) -> str:
    if not isinstance(schema, dict):
        return "string"
    if "$ref" in schema:
        # Resolve-free: keep the ref tail as a descriptive type. Fully resolved
        # documents (springdoc inline) are the expected input.
        return "string"
    return str(schema.get("type", "string"))


def _build_body_template(
    required_specs: list[tuple[str, str]], optional_specs: list[tuple[str, str]]
) -> str:
    """Assemble the Go-template body for a JSON request body.

    ``required_specs`` are ``(name, rendered ``"name":<value>`` pair)`` for
    required properties (rendered unconditionally); ``optional_specs`` are the
    same for optional properties, rendered only when the caller supplies the
    argument. The pair value is quoted (``"name":"{{.args.name}}"``) for
    string slots and unquoted (``"name":{{.args.name}}``) for JSON-typed slots
    (numbers, booleans, objects, arrays) — quoting a nested-object slot would
    make the plugin stringify the object (2026-10-06 submit/applicantInfo bug).

    Review round-4 N5: each optional tail carries its leading comma INSIDE
    the ``{{if}}`` — correct after a required field, but with ZERO required
    fields the first supplied optional produced ``{,"x":…}`` (invalid JSON).
    In the no-required case the first optional renders without a comma and
    every later one guards its comma on ANY earlier optional being present
    (plain text/template ``or`` — no variable assignment, which the plugin's
    template feature set has not verified).
    """
    if required_specs:
        template = "{ " + ",".join(pair for _, pair in required_specs)
        for name, pair in optional_specs:
            template += "{{if .args." + name + "}}," + pair + "{{end}}"
        return template + "}"
    parts: list[str] = []
    for idx, (name, pair) in enumerate(optional_specs):
        if idx == 0:
            parts.append("{{if .args." + name + "}}" + pair + "{{end}}")
        else:
            earlier = " ".join(".args." + p for p, _ in optional_specs[:idx])
            parts.append(
                "{{if .args." + name + "}}{{if or " + earlier + "}},{{end}}" + pair + "{{end}}"
            )
    # Review round-7 §2: "{" immediately followed by "{{if" yields "{{{if" —
    # real Go text/template CANNOT parse that (its lexer errors with
    # "unrecognized character in action: U+007B" once inside the action), and
    # neither could the round-6 validator. A space after the opening brace
    # makes the leading "{" literal text; rendering stays valid JSON.
    return "{ " + "".join(parts) + " }"


def _describe_operation(
    method: str, path: str, op: dict[str, Any], schemas: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Build one tool field set, or ``None`` when the op is not convertible."""
    if method not in _CONVERTIBLE_METHODS:
        return None
    schemas = schemas or {}

    raw_id = op.get("operationId") or f"{method}_{_path_slug(path)}"
    tool_name = _snake_case(raw_id)
    if not tool_name:
        return None

    description = op.get("summary") or op.get("description") or f"{method.upper()} {path}"

    args: list[dict[str, Any]] = []
    for param in op.get("parameters") or []:
        if not isinstance(param, dict) or param.get("in") not in ("path", "query"):
            continue
        schema = _deref(param.get("schema") or {}, schemas)
        args.append(
            {
                "name": str(param.get("name", "")),
                "description": str(param.get("description") or param.get("name", "")),
                "location": param.get("in"),
                "required": bool(param.get("required", False)),
                "type": _schema_type(schema),
            }
        )

    request_body: str | None = None
    body = op.get("requestBody")
    if isinstance(body, dict) and method in ("post", "put"):
        content = (body.get("content") or {}).get("application/json") or {}
        schema = _deref(content.get("schema") or {}, schemas)
        props = schema.get("properties")
        if isinstance(schema, dict) and isinstance(props, dict) and props:
            required = set(schema.get("required") or [])
            # (name, rendered ``"name":<value>`` pair) per property
            required_specs: list[tuple[str, str]] = []
            optional_specs: list[tuple[str, str]] = []

            def _pair(name: str, prop_type: str) -> str:
                """Render one ``"name":<slot>`` body pair for *prop_type*.

                Only string slots carry quotes: quoting an object/array slot
                would make the plugin stringify the argument into a
                JSON-text string and upstream strict deserialization fails
                (2026-10-06 submit ``applicantInfo`` incident).
                """
                if prop_type == "string":
                    return '"' + name + '":"{{.args.' + name + '}}"'
                return '"' + name + '":{{.args.' + name + "}}"

            for prop_name in props:
                prop_type = _schema_type(props[prop_name] or {})
                args.append(
                    {
                        "name": prop_name,
                        "description": str(
                            (props[prop_name] or {}).get("description") or prop_name
                        ),
                        "location": "body",
                        "required": prop_name in required,
                        "type": prop_type,
                    }
                )
                if prop_name in required:
                    required_specs.append((prop_name, _pair(prop_name, prop_type)))
                elif prop_type in _SCALAR_TYPES:
                    # review 2.5 (2026-09-25): optional scalars previously got a
                    # arg declaration but never reached the body — the plugin
                    # silently dropped them. Emit a Go-template conditional so
                    # the field is rendered only when the caller provides it.
                    optional_specs.append((prop_name, _pair(prop_name, prop_type)))
                else:
                    # Optional object/array properties were previously dropped
                    # from the body entirely — the caller could pass the arg
                    # but it never reached upstream. Render them conditionally,
                    # unquoted (same JSON-slot rule as required ones).
                    optional_specs.append((prop_name, _pair(prop_name, prop_type)))
            if required_specs or optional_specs:
                request_body = _build_body_template(required_specs, optional_specs)
        elif not schema:
            # Explicit empty body (e.g. the rpc endpoints) — keep a literal {}.
            request_body = "{}"

    return {
        "tool_name": tool_name,
        "description": description,
        "http_method": method.upper(),
        # rewrite {param} → {{.param}} so the plugin substitutes argument
        # values; args already carry location="path" (review 2.5)
        "http_path": _templated_path(path),
        "parameters": args,
        "request_body": request_body,
    }


def import_openapi(doc: dict[str, Any]) -> dict[str, Any]:
    """Convert an OpenAPI document into a tool-import report.

    Returns ``{"tools": [...], "skipped": [{"path", "method", "reason"}, ...]}``
    where ``tools`` entries are ready for :meth:`CatalogRepo.upsert_tool`
    (minus ``service_id`` / ``allowed_roles`` / ``enabled``). Nothing is
    silently dropped (review 2.4). Raises ``ValueError`` on documents that
    are not OpenAPI 3 JSON.
    """
    if not isinstance(doc, dict) or not isinstance(doc.get("paths"), dict):
        raise ValueError("not an OpenAPI document (missing 'paths')")

    schemas = _component_schemas(doc)
    tools: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path, path_item in doc["paths"].items():
        if not isinstance(path_item, dict):
            continue
        for method, op in path_item.items():
            m = method.lower()
            if m not in _CONVERTIBLE_METHODS:
                if isinstance(op, dict):
                    skipped.append(
                        {"path": path, "method": m.upper(), "reason": "unsupported_verb"}
                    )
                continue
            if not isinstance(op, dict):
                continue
            converted = _describe_operation(m, path, op, schemas)
            if converted is None or not converted["tool_name"]:
                skipped.append({"path": path, "method": m.upper(), "reason": "not_convertible"})
                continue
            if converted["tool_name"] in seen:
                skipped.append(
                    {
                        "path": path,
                        "method": m.upper(),
                        "reason": f"duplicate_tool_name:{converted['tool_name']}",
                    }
                )
                continue
            seen.add(converted["tool_name"])
            tools.append(converted)
    return {"tools": tools, "skipped": skipped}
