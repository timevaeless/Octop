"""Catalog-service → tenant-shared connector attachments (2026-10-01 design).

User decision (MCP distribution simplification, "one resource one config
point"): a published catalog service becomes usable through a CONNECTOR —
experts can only mount connectors — but the manual attach/share/configure
chore is collapsed into ONE tenant-admin action:

    接入连接器 (attach)  →  one tenant-scoped shared connector per service,
    whose ``allowed_roles`` are OWNED by the catalog service and kept in sync
    (one-way: catalog → connector). The connector itself is locked in the UI
    ("managed by the MCP catalog").

Shape: one connector parent row per (service, tenant), ``kind =
custom-mcp:{service_id}`` — the per-service kind isolates role sync (a
tenant attaching two services with different roles must not have one parent's
roles gate the other) and keeps these rows out of the manual custom-MCP
loops (those query ``kind = 'custom-mcp'`` exactly). Viewer-side mounting
goes through :func:`catalog_shared_harness_configs`, hooked at the end of
``ConnectorService.custom_harness_configs`` — the single choke point both
agent-build and chat-time tool loading go through. Mount-name consistency
(the key becomes the LLM tool-name prefix) is enforced at two more hook
tails: :func:`refine_catalog_mount_names` (``list_active_mcp_server_names``)
and :func:`append_catalog_assignable_items`
(``list_assignable_instances_for_api``).

Standalone module per user convention (extract, don't weave): upstream files
carry one-to-three-line lazy hooks; deleting this module and the hooks
restores the pre-feature behaviour verbatim.
"""

from __future__ import annotations

import logging
from typing import Any

from octop.infra.connectors.custom_mcp import (
    enabled_harness_configs,
    extract_servers,
    server_enabled,
    shared_mcp_server_name,
    validate_servers_map,
    wrap_servers,
)
from octop.infra.users.tenancy import shared_row_visible
from octop.infra.utils.ulid import new_ulid

logger = logging.getLogger(__name__)

__all__ = [
    "CATALOG_KIND_PREFIX",
    "catalog_kind",
    "catalog_parent_row",
    "catalog_parent_rows",
    "catalog_service_attachments",
    "catalog_shared_harness_configs",
    "attach_service_to_org",
    "detach_service_from_org",
    "sync_service_attachments",
    "delete_service_attachments",
    "refine_catalog_mount_names",
    "append_catalog_assignable_items",
]

#: ``custom-mcp:{service_id}`` — prefix-matched so the per-service parents
#: never collide with the manual single ``custom-mcp`` doc, and so the
#: resource-ACL connectors family can exclude them with one LIKE clause.
CATALOG_KIND_PREFIX = "custom-mcp:"


def catalog_kind(service_id: str) -> str:
    return f"{CATALOG_KIND_PREFIX}{service_id}"


def catalog_service_id(kind: str) -> str | None:
    if kind.startswith(CATALOG_KIND_PREFIX):
        return kind[len(CATALOG_KIND_PREFIX):] or None
    return None


def catalog_parent_rows(conn_repo: Any, service_id: str) -> list[Any]:
    return conn_repo.list_by_kind(catalog_kind(service_id))


def catalog_parent_row(conn_repo: Any, service_id: str, org_id: str) -> Any | None:
    """The one attachment row for (service, tenant) — attach is idempotent."""
    for row in catalog_parent_rows(conn_repo, service_id):
        if row.org_id == org_id:
            return row
    return None


def _server_name(service_id: str) -> str:
    return f"mcp-{service_id}"


def _spec_for(svc: Any, higress_base: str) -> dict[str, Any]:
    """The harness spec for one attached service (same shape as the platform
    admin's self-test attach, plus ``shared`` for tenant distribution)."""
    return {
        "transport": "streamable_http",
        "url": f"{higress_base}{svc.resolved_mcp_path}",
        "enabled": True,
        # identity passthrough (T3): the token is minted per VIEWER at
        # harness build, so every member rides their own rls — hub filters
        # tools per the catalog ACL projection.
        "delegate_identity": True,
        "display_name": (svc.display_name or svc.service_id).strip()[:64],
        "shared": True,
    }


def attach_service_to_org(
    conn_svc: Any,
    conn_repo: Any,
    svc: Any,
    *,
    org_id: str | None,
    owner_user_id: int,
    higress_base: str,
) -> dict[str, Any]:
    """Create or refresh the tenant-shared connector for one service.

    Idempotent: re-attach (or a second tenant admin clicking) updates the
    existing (service, tenant) row in place — payload, display name and the
    synced ``allowed_roles`` — instead of creating a duplicate mount.

    ``org_id=None`` (2026-10-05) serves the platform admin's one-click
    「接入连接器」: the attachment row inherits the SERVICE's own attribution,
    which for a platform-owned service is NULL (= platform-private, admin's
    own use) — tenants keep attaching explicitly via their own catalog page.
    """
    kind = catalog_kind(svc.service_id)
    servers = validate_servers_map(
        {_server_name(svc.service_id): _spec_for(svc, higress_base)}
    )
    payload = wrap_servers(servers)
    row = catalog_parent_row(conn_repo, svc.service_id, org_id)
    display_name = f"目录接入 · {svc.display_name or svc.service_id}"
    if row is not None:
        conn_svc.encrypt_and_store(instance_id=row.instance_id, payload=payload)
        conn_repo.update_metadata(
            row.instance_id,
            display_name=display_name,
            allowed_roles=list(svc.allowed_roles or []),
        )
        return {"action": "updated", "instance_id": row.instance_id}
    instance_id = new_ulid()
    conn_repo.create(
        instance_id=instance_id,
        user_id=owner_user_id,
        kind=kind,
        display_name=display_name,
        mcp_server_name=f"{kind}__{instance_id}",
        shared=True,
        org_id=org_id,
    )
    # Stamp the catalog-owned roles immediately — the viewer-mount gate reads
    # them, so a fresh attachment without them would mount role-unrestricted.
    conn_repo.update_metadata(
        instance_id, allowed_roles=list(svc.allowed_roles or [])
    )
    conn_svc.encrypt_and_store(instance_id=instance_id, payload=payload)
    return {"action": "created", "instance_id": instance_id}


def detach_service_from_org(conn_repo: Any, service_id: str, org_id: str) -> bool:
    row = catalog_parent_row(conn_repo, service_id, org_id)
    if row is None:
        return False
    conn_repo.delete(row.instance_id)
    return True


def sync_service_attachments(conn_svc: Any, conn_repo: Any, svc: Any) -> int:
    """One-way sync: catalog service → every tenant attachment of it.

    Re-encrypts the payload (URL/display_name may have changed with a
    re-publish) and refreshes ``allowed_roles``. Returns the number of
    attachments refreshed.
    """
    rows = catalog_parent_rows(conn_repo, svc.service_id)
    if not rows:
        return 0
    higress_base = _higress_base(conn_svc)
    # Roles always sync (they live in plain metadata); the payload re-encrypt
    # needs the gateway base — without it (platform never configured) skip the
    # re-encryption instead of raising, the rows keep their previous payload.
    payload: dict[str, Any] | None = None
    if higress_base:
        servers = validate_servers_map(
            {_server_name(svc.service_id): _spec_for(svc, higress_base)}
        )
        payload = wrap_servers(servers)
    for row in rows:
        if payload is not None:
            conn_svc.encrypt_and_store(instance_id=row.instance_id, payload=payload)
        conn_repo.update_metadata(
            row.instance_id, allowed_roles=list(svc.allowed_roles or [])
        )
    return len(rows)


def delete_service_attachments(conn_repo: Any, service_id: str) -> int:
    """Remove every tenant attachment (service deleted or unpublished)."""
    removed = 0
    for row in catalog_parent_rows(conn_repo, service_id):
        conn_repo.delete(row.instance_id)
        removed += 1
    return removed


def catalog_service_attachments(conn_repo: Any, service_id: str, org_id: str) -> bool:
    """Whether the tenant has attached this service (UI toggle state)."""
    return catalog_parent_row(conn_repo, service_id, org_id) is not None


def _higress_base(conn_svc: Any) -> str:
    return conn_svc.catalog_setting("mcp_catalog_higress_base")


def catalog_shared_harness_configs(
    conn_svc: Any, user_id: int, authz: Any = None
) -> dict[str, Any]:
    """Harness MCP configs from catalog attachments visible to ``user_id``.

    Hooked at the end of ``ConnectorService.custom_harness_configs`` (the one
    choke point agent-build and chat-time loading share). Own attachments
    always mount; someone else's attachment passes the shared-row gate
    (tenant boundary + synced ``allowed_roles``). ``authz=None`` (legacy
    internal callers) is fail-closed: only the caller's OWN attachments
    mount, never another tenant's.
    """
    repo = conn_svc.catalog_attachment_repo()
    configs: dict[str, Any] = {}
    try:
        parents = repo.list_by_kind_prefix(CATALOG_KIND_PREFIX)
    except Exception:  # pragma: no cover - partial test doubles
        return configs
    for parent in parents:
        own = parent.user_id == user_id
        if not own:
            if authz is None or not parent.has_credentials:
                continue
            if not shared_row_visible(parent, authz):
                continue
        elif not parent.has_credentials:
            continue
        try:
            servers = extract_servers(conn_svc.decrypt(parent.instance_id))
        except Exception:  # noqa: BLE001 — one bad blob must not kill the build
            logger.exception("catalog attachment %s undecryptable", parent.instance_id)
            continue
        for name, spec in servers.items():
            if not isinstance(spec, dict) or not server_enabled(spec):
                continue
            if not own and spec.get("shared") is not True:
                continue
            built = enabled_harness_configs(
                {name: spec},
                delegate_token=conn_svc.delegated_identity_token(user_id, authz),
            ).get(name)
            if built is not None:
                # Keyed by ``shared_mcp_server_name`` (the safe ``custom__…``
                # form, no colons) — this key becomes the LLM tool-name prefix,
                # so the raw row ``mcp_server_name`` (``custom-mcp:{sid}__…``,
                # colon-bearing, >64 chars with a tool suffix) must never leak
                # into it. Mount-side names must come from
                # :func:`harness_key_for_row` instead (2026-10-01 fix).
                configs[shared_mcp_server_name(parent.instance_id, name)] = built
    return configs


def harness_key_for_row(row: Any) -> str:
    """The harness/mount name for one catalog attachment row.

    ``custom__{instance_id}__mcp-{service_id}`` — exactly the key
    :func:`catalog_shared_harness_configs` uses (the row's payload carries the
    single server ``mcp-{service_id}`` by construction). Mount pickers and
    ``validate_mcp_servers_for_user`` must issue THIS name; issuing the raw
    ``mcp_server_name`` produced mounts that validated but never loaded.
    """
    sid = catalog_service_id(row.kind) or ""
    return shared_mcp_server_name(row.instance_id, _server_name(sid))


def refine_catalog_mount_names(
    conn_svc: Any, user_id: int, authz: Any, names: list[str]
) -> list[str]:
    """Hook tail of ``ConnectorService.list_active_mcp_server_names``.

    Upstream's generic connector branch leaks each catalog attachment's raw
    ``mcp_server_name`` (``custom-mcp:{sid}__{instance}`` — colon-bearing,
    over-long with a tool suffix; the name becomes the LLM tool-name prefix,
    so that form can never load). Strip the leaked raw names and re-admit the
    attachments under their harness keys, gated exactly like the harness
    build via :func:`catalog_shared_harness_configs` (tenant boundary + synced
    roles; ``authz=None`` = own rows only). Deleting the hook in service.py
    restores upstream verbatim.
    """
    repo = conn_svc.catalog_attachment_repo()
    try:
        parents = repo.list_by_kind_prefix(CATALOG_KIND_PREFIX)
    except Exception:  # pragma: no cover - partial test doubles
        return sorted(names)
    raw = {p.mcp_server_name for p in parents}
    kept = [n for n in names if n not in raw]
    kept.extend(catalog_shared_harness_configs(conn_svc, user_id, authz=authz))
    return sorted(kept)


def append_catalog_assignable_items(
    conn_svc: Any,
    user_id: int,
    authz: Any,
    allowed: Any,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Hook tail of ``ConnectorService.list_assignable_instances_for_api``.

    The upstream filter keeps an item only when its raw ``mcp_server_name``
    is mountable, so catalog attachments — mountable under their harness key,
    never the raw name — always fall through. Re-admit every attachment whose
    harness key (:func:`harness_key_for_row`) is in ``allowed`` (the
    already-computed mountable set: membership there encodes tenancy + roles +
    credentials, no second permission check here), preserving the picker's
    display labels by re-reading :meth:`list_instances_for_api`. Deleting the
    hook in service.py restores upstream verbatim.
    """
    allowed_set = frozenset(allowed)
    listed = {item.get("instance_id") for item in items}
    out = list(items)
    repo = conn_svc.catalog_attachment_repo()
    try:
        parents = repo.list_by_kind_prefix(CATALOG_KIND_PREFIX)
    except Exception:  # pragma: no cover - partial test doubles
        return out
    todo = [
        p
        for p in parents
        if p.instance_id not in listed and harness_key_for_row(p) in allowed_set
    ]
    if not todo:
        return out
    try:
        api_items = {
            i.get("instance_id"): i
            for i in conn_svc.list_instances_for_api(user_id, authz)
        }
    except Exception:  # noqa: BLE001 — labels are cosmetic, never block the picker
        api_items = {}
    for parent in todo:
        base = dict(api_items.get(parent.instance_id) or {})
        base["instance_id"] = parent.instance_id
        base["kind"] = parent.kind
        base["display_name"] = base.get("display_name") or parent.display_name
        base["mcp_server_name"] = harness_key_for_row(parent)
        base["shared"] = True
        base.setdefault("status", "active")
        base.setdefault("has_credentials", parent.has_credentials)
        out.append(base)
    return out
