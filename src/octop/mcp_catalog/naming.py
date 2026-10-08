"""Naming queries against registry backends (v2 P3.1).

The service form's AutoComplete data source: list service names registered
in one mcpbridge registry entry, and resolve one service's instance (ip/port)
for auto-fill. nacos walks the v3 admin ns API (shapes live-verified against
nacos3 2026-09-29):

* service list: ``GET /nacos/v3/admin/ns/service/list`` → ``data.pageItems[].name``;
* instance list: ``GET /nacos/v3/admin/ns/instance/list`` → ``data[].ip/port/healthy``.

eureka walks ``/eureka/apps`` (Accept: application/json), best-effort without
credentials — the bridge's authSecret is for the gateway, not for us.

Failures raise ``NamingError`` → the endpoint 502s and the frontend falls
back to manual input. NEVER return fabricated options.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

_TIMEOUT_SECONDS = 10


class NamingError(RuntimeError):
    pass


# ---- nacos2/nacos3 ----


def list_nacos_services(
    client: Any, *, namespace: str, group: str, page_size: int = 200
) -> list[str]:
    """Registered service names under one nacos namespace/group."""
    payload = client.admin_get(
        "/nacos/v3/admin/ns/service/list",
        {"namespaceId": namespace, "groupName": group, "pageNo": 1, "pageSize": page_size},
    )
    data = payload.get("data")
    items = data.get("pageItems") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise NamingError(f"nacos service list returned unexpected shape: {payload!r:.200}")
    names: set[str] = set()
    for it in items:
        if isinstance(it, dict) and it.get("name"):
            names.add(str(it["name"]))
        elif isinstance(it, str) and it:
            names.add(it)  # tolerate a bare-name list shape
    return sorted(names)


def nacos_instance(
    client: Any, *, namespace: str, group: str, service_name: str
) -> dict[str, Any]:
    """First healthy instance (else first instance) of one nacos service."""
    payload = client.admin_get(
        "/nacos/v3/admin/ns/instance/list",
        {"namespaceId": namespace, "groupName": group, "serviceName": service_name},
    )
    data = payload.get("data")
    hosts = data if isinstance(data, list) else (data or {}).get("pageItems")
    if not isinstance(hosts, list) or not hosts:
        raise NamingError(f"no instance registered for '{service_name}' in {namespace}/{group}")
    hosts = [h for h in hosts if isinstance(h, dict)]
    healthy = [h for h in hosts if h.get("healthy")]
    pick = (healthy or hosts)[0]
    return {
        "ip": str(pick.get("ip") or ""),
        "port": int(pick["port"]) if pick.get("port") is not None else None,
        "namespace": namespace,
        "group": group,
    }


# ---- eureka ----


def _eureka_get(domain: str, path: str) -> dict[str, Any]:
    url = f"http://{domain.rstrip('/')}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            return json.load(resp)  # type: ignore[no-any-return]
    except (OSError, ValueError) as e:
        raise NamingError(f"eureka query failed: {e}") from e


def list_eureka_services(domain: str) -> list[str]:
    """Application names from ``/eureka/apps`` (eureka stores them UPPERCASED)."""
    doc = _eureka_get(domain, "/eureka/apps")
    apps = (doc.get("applications") or {}).get("application")
    if apps is None:
        apps = (doc.get("applications") or {}).get("application")  # keep shape tolerance
    if isinstance(apps, dict):  # single-app registry quirk
        apps = [apps]
    if not isinstance(apps, list):
        raise NamingError(f"eureka /apps returned unexpected shape")
    names = {str(a["name"]) for a in apps if isinstance(a, dict) and a.get("name")}
    return sorted(names)


def eureka_instance(domain: str, service_name: str) -> dict[str, Any]:
    """First UP instance of one eureka application.

    Eureka matches paths case-sensitively on the uppercased name; try the
    name as given first, then uppercased (the form may carry either).
    """
    doc: dict[str, Any] | None = None
    last_error: Exception | None = None
    for candidate in dict.fromkeys([service_name, service_name.upper()]):
        try:
            doc = _eureka_get(domain, f"/eureka/apps/{candidate}")
            break
        except NamingError as e:
            last_error = e
    app = ((doc or {}).get("application") or {})
    instances = app.get("instance")
    if isinstance(instances, dict):
        instances = [instances]
    if not isinstance(instances, list) or not instances:
        raise NamingError(
            f"no instance registered for '{service_name}' in eureka"
        ) from last_error
    up = [i for i in instances if isinstance(i, dict) and str(i.get("status")) == "UP"]
    pick = (up or instances)[0]
    port = pick.get("port")
    port_value = port.get("$") if isinstance(port, dict) else port
    return {
        "ip": str(pick.get("hostName") or pick.get("ipAddr") or ""),
        "port": int(port_value) if port_value is not None else None,
        "namespace": None,
        "group": None,
    }
