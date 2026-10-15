"""Live-read Higress service sources via the Console API.

v3 (2026-10-15): the "服务来源" dropdown reads the gateway's live service
sources through the **Console channel**（``/v1/service-sources``，即人在
页面上看到的那份数据）——存储无关。Read-ONLY by design: Octop never writes
service sources (the operator manages them in the Console), stores no
snapshot, and degrades to an explicit error — never fake data — when the
Console is unreachable or the payload is malformed.
"""

from __future__ import annotations

from typing import Any


def parse_service_sources(items: Any) -> list[dict[str, Any]]:
    """Extract the dropdown projection from Console service-source entries.

    Pure function (unit-testable). Fail-closed: a non-list payload raises
    ValueError instead of yielding an empty list — an empty dropdown would
    look like "no sources exist" and mislead the admin, while the truth is
    "the gateway config is unreadable".

    Entries missing ``name``/``type`` are skipped (a source the controller
    itself could not use); everything else is projected — unknown registry
    types (zk/consul/...) are surfaced, not filtered: the UI marks them, the
    publisher gates them.

    Console nacos2 shape (verified live 2026-10-15): namespace/group live in
    ``properties.nacosNamespaceId`` / ``properties.nacosGroups``.
    """
    if not isinstance(items, list):
        raise ValueError("service sources payload is not a list")
    sources: list[dict[str, Any]] = []
    for entry in items:
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
        props = entry.get("properties") or {}
        if isinstance(props, dict):
            nacos_ns = props.get("nacosNamespaceId")
            if nacos_ns:
                source["nacos_namespace_id"] = str(nacos_ns)
            groups = props.get("nacosGroups")
            if isinstance(groups, list) and groups:
                source["nacos_group"] = str(groups[0])
        sources.append(source)
    return sources


def list_service_sources(client: Any) -> list[dict[str, Any]]:
    """Fetch service sources through the Console channel.

    ``client`` is a gateway client exposing ``list_service_sources()``.
    Raises ``GatewayClientError`` (Console unreachable / auth failure /
    rejection) and ``ValueError`` (malformed payload) — both surface as 502
    in the router; the caller must never see a silently empty list.
    """
    return parse_service_sources(client.list_service_sources())
