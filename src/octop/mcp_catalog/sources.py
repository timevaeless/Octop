"""Live-read Higress service sources — v2 design (mcp-catalog-env-registry
design §〇-2): the "服务来源" dropdown reads ``mcpbridges.default``
``spec.registries[]`` in real time through the nacos config channel.

Read-ONLY by design: Octop never writes this dataId (the Higress console owns
it), stores no snapshot, and degrades to an explicit error — never fake data —
when nacos is unreachable or the bridge is malformed.
"""

from __future__ import annotations

from typing import Any

import yaml

from octop.mcp_catalog.nacos_client import NacosConfigClient

_BRIDGE_DATA_ID = "mcpbridges.default"


def parse_bridge_registries(raw: str) -> list[dict[str, Any]]:
    """Extract the dropdown projection from a mcpbridge YAML document.

    Pure function (unit-testable). Fail-closed: anything that does not look
    like a mcpbridge raises ValueError instead of yielding an empty list —
    an empty dropdown would look like "no sources exist" and mislead the
    admin, while the truth is "the gateway config is unreadable".

    Entries missing ``name``/``type`` are skipped (a registry entry the
    controller itself could not use), everything else is passed through
    verbatim — unknown registry types (zk/consul/...) are surfaced, not
    filtered: the UI marks them, the publisher gates them.
    """
    doc = yaml.safe_load(raw)
    if not isinstance(doc, dict):
        raise ValueError("content is not a YAML object")
    spec = doc.get("spec")
    registries = spec.get("registries") if isinstance(spec, dict) else None
    if not isinstance(registries, list):
        raise ValueError("spec.registries is missing or not a list")
    sources: list[dict[str, Any]] = []
    for entry in registries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        rtype = entry.get("type")
        if not name or not rtype:
            continue
        port = entry.get("port")
        source: dict[str, Any] = {
            "name": str(name),
            "type": str(rtype),
            "domain": str(entry.get("domain") or ""),
            "port": int(port) if port is not None else None,
        }
        # nacos registries carry the default namespace/group the form can prefill
        nacos_ns = entry.get("nacosNamespaceId")
        if nacos_ns:
            source["nacos_namespace_id"] = str(nacos_ns)
        groups = entry.get("nacosGroups")
        if isinstance(groups, list) and groups:
            source["nacos_group"] = str(groups[0])
        sources.append(source)
    return sources


def list_bridge_sources(server_addr: str, username: str, password: str) -> list[dict[str, Any]]:
    """Fetch ``mcpbridges.default`` through the nacos config channel.

    Raises ``NacosConfigError`` (nacos unreachable / auth failure) and
    ``ValueError`` (content missing or malformed) — both surface as 502 in
    the router; the caller must never see a silently empty list.
    """
    client = NacosConfigClient(server_addr, username, password)
    raw = client.get_config(_BRIDGE_DATA_ID)
    if raw is None:
        raise ValueError(
            f"{_BRIDGE_DATA_ID} not found in nacos — is the Higress gateway connected?"
        )
    return parse_bridge_registries(raw)
