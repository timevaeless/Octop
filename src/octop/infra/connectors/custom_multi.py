"""Multi-instance custom-MCP connectors (T4 pluggable module).

One connector = one ``connectors`` row with ``kind='custom-mcp'`` holding its
own encrypted ``servers`` document. A user may own MANY rows; the mount
namespace (server names) stays USER-level because own servers mount under
their bare name (:meth:`ConnectorService.list_active_mcp_server_names`), so
every write here enforces cross-document server-name uniqueness — harness
keys therefore stay stable no matter how the connectors are organised.

Contract shared with the single-document era:

* reads are merged views keyed by server name (``merged_custom_servers``) —
  every legacy call site keeps working unchanged;
* writes always target exactly ONE document (the one owning the server, or
  the addressed ``instance_id``) — a patch or OAuth refresh on connector A
  can never rewrite connector B;
* document ``display_name`` is the user-chosen connector name (the fixed
  「自定义 MCP」 label is gone); resource-ACL rows map 1:1 to connectors.

Deleting this module together with the hooks in ``service.py``,
``org_admin.py``, ``roles.py`` and ``routers/connectors.py`` restores the
single-document behaviour verbatim.
"""

from __future__ import annotations

import logging
from typing import Any

from octop.infra.connectors.custom_mcp import (
    CUSTOM_MCP_KIND,
    _DISPLAY_NAME_MAX,
    expand_custom_instances,
    extract_servers,
    is_custom_mcp_kind,
    merge_preserved_oauth,
    parse_synthetic_instance_id,
    redact_servers_for_api,
    server_display_name,
    validate_servers_map,
    wrap_servers,
)
from octop.infra.connectors.service import ConnectorNameTakenError
from octop.infra.db.repos.connectors import ConnectorRow
from octop.infra.utils.ulid import new_ulid

__all__ = [
    "apply_server_oauth",
    "connector_server_labels",
    "connector_view",
    "create_connector",
    "delete_connector",
    "doc_servers",
    "ensure_fresh",
    "expand_own",
    "list_connector_rows",
    "list_connectors_for_api",
    "locate_connector_for_server",
    "merged_custom_servers",
    "note_oauth_required",
    "patch_server",
    "rename_connector",
    "resolve_acl_parent",
    "save_connector_servers",
    "svc_for",
]


def svc_for(server: Any) -> Any:
    """ConnectorService over this server's repos (needs secrets for decrypt)."""
    from octop.infra.connectors.service import ConnectorService

    services = server.services
    return ConnectorService(
        repo=services.connector_repo,
        secret_repo=services.secret_repo,
        settings_repo=services.settings_repo,
        config=services.config,
    )


# ---------------------------------------------------------------- read side


def list_connector_rows(svc: Any, user_id: int) -> list[ConnectorRow]:
    """All of the user's custom-MCP connector rows, oldest first."""
    return svc._repo.list_by_user_kind(user_id, CUSTOM_MCP_KIND)


def doc_servers(svc: Any, row: ConnectorRow) -> dict[str, Any]:
    """The decrypted servers map of ONE document (empty when unusable)."""
    if row is None or not row.has_credentials:
        return {}
    return extract_servers(svc.decrypt(row.instance_id))


def merged_custom_servers(svc: Any, user_id: int) -> dict[str, Any]:
    """Merged read across ALL of the user's documents, keyed by server name.

    Stable order (created_at, instance_id) makes the merge deterministic;
    cross-document name collisions are prevented at every write path, so a
    collision here can only come from out-of-band edits — last doc wins.
    """
    out: dict[str, Any] = {}
    for row in list_connector_rows(svc, user_id):
        for name, spec in doc_servers(svc, row).items():
            out[str(name)] = spec
    return out


def locate_connector_for_server(
    svc: Any, user_id: int, server_name: str
) -> ConnectorRow | None:
    """The one document owning *server_name* — the single addressing helper.

    Every name-addressed mutation (patch / OAuth / refresh / note) MUST go
    through this so the read-modify-write pair can never straddle documents.
    """
    for row in list_connector_rows(svc, user_id):
        if server_name in doc_servers(svc, row):
            return row
    return None


def expand_own(svc: Any, user_id: int) -> list[dict[str, Any]]:
    """Expanded list-API dicts for the user's own connectors (list view)."""
    out: list[dict[str, Any]] = []
    for row in list_connector_rows(svc, user_id):
        if not row.has_credentials:
            continue
        out.extend(expand_custom_instances(parent=row, servers=doc_servers(svc, row)))
    return out


def connector_view(
    svc: Any, row: ConnectorRow | None, *, redact: bool = True
) -> dict[str, Any]:
    """API projection of one connector: metadata + its own servers only."""
    if row is None:
        return {}
    servers = doc_servers(svc, row)
    return {
        "instance_id": row.instance_id,
        "name": row.display_name,
        "servers": redact_servers_for_api(servers) if redact else servers,
        "org_id": row.org_id,
        "allowed_roles": list(row.allowed_roles or []),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def list_connectors_for_api(svc: Any, user_id: int) -> list[dict[str, Any]]:
    return [connector_view(svc, row) for row in list_connector_rows(svc, user_id)]


def connector_server_labels(svc: Any, instance_id: str) -> list[str]:
    """Server display labels of ONE document — for the resource-ACL row."""
    row = svc._repo.get(instance_id)
    if row is None or not is_custom_mcp_kind(row.kind):
        return []
    labels = {
        server_display_name(str(name), spec)
        for name, spec in doc_servers(svc, row).items()
        if isinstance(spec, dict)
    }
    return sorted(labels, key=str.casefold)


# --------------------------------------------------------------- write side


def _other_doc_server_names(
    svc: Any, user_id: int, exclude_instance_id: str | None
) -> set[str]:
    """Server names living in the user's OTHER documents — mount collisions."""
    names: set[str] = set()
    for row in list_connector_rows(svc, user_id):
        if exclude_instance_id is not None and row.instance_id == exclude_instance_id:
            continue
        names.update(str(name) for name in doc_servers(svc, row))
    return names


def _reserved_for(
    svc: Any, user_id: int, exclude_instance_id: str | None = None
) -> set[str]:
    """Names a document's servers must not take: built-ins + other docs."""
    return set(svc.reserved_builtin_mcp_names(user_id)) | _other_doc_server_names(
        svc, user_id, exclude_instance_id
    )


def _check_doc_name_free(
    svc: Any, user_id: int, name: str, exclude_instance_id: str | None = None
) -> None:
    """Connector names are user-unique: vs other custom docs AND other kinds."""
    for row in list_connector_rows(svc, user_id):
        if exclude_instance_id is not None and row.instance_id == exclude_instance_id:
            continue
        if row.display_name == name:
            raise ConnectorNameTakenError(name)
    if svc._repo.name_exists(user_id, name):
        raise ConnectorNameTakenError(name)


def _write_doc(svc: Any, row: ConnectorRow, servers: Any) -> dict[str, Any]:
    """Validate + encrypt ONE document. Empty map deletes the connector."""
    normalized = validate_servers_map(
        servers, reserved_names=_reserved_for(svc, row.user_id, row.instance_id)
    )
    display_names: set[str] = set()
    for name, spec in normalized.items():
        display_name = (
            str(spec.get("display_name") or "").strip() if isinstance(spec, dict) else ""
        ) or name
        if display_name in display_names:
            raise ConnectorNameTakenError(display_name)
        if svc._repo.name_exists(row.user_id, display_name):
            raise ConnectorNameTakenError(display_name)
        display_names.add(display_name)
    if not normalized:
        svc._repo.delete(row.instance_id)
        return {}
    svc.encrypt_and_store(instance_id=row.instance_id, payload=wrap_servers(normalized))
    return normalized


def create_connector(
    svc: Any,
    user_id: int,
    name: str,
    servers: Any,
    org_id: str | None = None,
) -> dict[str, Any]:
    """Mint a new connector: fresh ULID row + its own encrypted document."""
    clean = str(name or "").strip()
    if not clean:
        raise ValueError("connector name must not be empty")
    if len(clean) > _DISPLAY_NAME_MAX:
        raise ValueError(f"connector name must be at most {_DISPLAY_NAME_MAX} characters")
    _check_doc_name_free(svc, user_id, clean)
    if not isinstance(servers, dict) or not servers:
        # An empty create would be deleted by _write_doc immediately —
        # reject it instead of returning a view for a vanished row.
        raise ValueError("connector must contain at least one server")
    # Validate against existing names BEFORE the row exists so a rejected
    # create leaves no stub behind.
    validate_servers_map(servers, reserved_names=_reserved_for(svc, user_id))
    instance_id = new_ulid()
    from octop.infra.connectors.builder import mcp_server_name

    svc._repo.create(
        instance_id=instance_id,
        user_id=user_id,
        kind=CUSTOM_MCP_KIND,
        display_name=clean,
        mcp_server_name=mcp_server_name(CUSTOM_MCP_KIND, instance_id),
        org_id=org_id,
    )
    row = svc._repo.get(instance_id)
    _write_doc(svc, row, servers)
    return connector_view(svc, svc._repo.get(instance_id))


def save_connector_servers(
    svc: Any, user_id: int, instance_id: str, servers: Any, org_id: str | None = None
) -> dict[str, Any]:
    """Replace ONE connector's servers — never touches its siblings.

    ``org_id`` mirrors the single-document adoption semantics: an orgless
    (platform) row adopts the tenant the first time its owner saves.
    """
    row = svc._repo.get(instance_id)
    if (
        row is None
        or row.user_id != user_id
        or not is_custom_mcp_kind(row.kind)
    ):
        raise KeyError(instance_id)
    if row.org_id is None and org_id is not None:
        svc._repo.update_metadata(instance_id, org_id=org_id)
        row = svc._repo.get(instance_id)
    # API save path: the dashboard works on redacted specs, so preserved
    # stored OAuth tokens are merged back per server name — identical to the
    # single-document ``put_custom_servers`` semantics (internal callers such
    # as OAuth apply/refresh go through update_server_spec, never here, so
    # freshly minted grants are never stripped).
    merged = merge_preserved_oauth(servers, doc_servers(svc, row))
    _write_doc(svc, row, merged)
    final = svc._repo.get(instance_id)
    if final is None:
        return {}
    return connector_view(svc, final)


def rename_connector(
    svc: Any, user_id: int, instance_id: str, name: str
) -> dict[str, Any]:
    clean = str(name or "").strip()
    if not clean:
        raise ValueError("connector name must not be empty")
    if len(clean) > _DISPLAY_NAME_MAX:
        raise ValueError(f"connector name must be at most {_DISPLAY_NAME_MAX} characters")
    _check_doc_name_free(svc, user_id, clean, exclude_instance_id=instance_id)
    row = svc._repo.get(instance_id)
    if row is None or row.user_id != user_id or not is_custom_mcp_kind(row.kind):
        raise KeyError(instance_id)
    svc._repo.update_metadata(instance_id, display_name=clean)
    return connector_view(svc, svc._repo.get(instance_id))


def delete_connector(svc: Any, user_id: int, instance_id: str) -> dict[str, Any]:
    """Drop the connector row + its encrypted document. Returns its servers."""
    row = svc._repo.get(instance_id)
    if row is None or row.user_id != user_id or not is_custom_mcp_kind(row.kind):
        raise KeyError(instance_id)
    servers = doc_servers(svc, row)
    svc._repo.delete(instance_id)
    return servers


def remove_server(svc: Any, user_id: int, server_name: str) -> None:
    """Drop one server from whichever document owns it (only that doc)."""
    row = locate_connector_for_server(svc, user_id, server_name)
    if row is None:
        raise KeyError(server_name)
    servers = dict(doc_servers(svc, row))
    servers.pop(server_name, None)
    _write_doc(svc, row, servers)


def _merged_or_raise(svc: Any, user_id: int) -> dict[str, Any]:
    return merged_custom_servers(svc, user_id)


def update_server_spec(
    svc: Any,
    user_id: int,
    server_name: str,
    transform: Any,
) -> dict[str, Any]:
    """Locate the owning document, transform one spec, rewrite THAT doc only.

    ``transform(spec)`` returns the new spec dict, or ``None`` to signal
    "no change" (the document is left untouched). Returns the merged view.
    """
    row = locate_connector_for_server(svc, user_id, server_name)
    if row is None:
        raise KeyError(server_name)
    servers = dict(doc_servers(svc, row))
    new_spec = transform(dict(servers.get(server_name) or {}))
    if new_spec is None:
        return _merged_or_raise(svc, user_id)
    servers[server_name] = new_spec
    _write_doc(svc, row, servers)
    return _merged_or_raise(svc, user_id)


# ------------------------------------------------- name-addressed mutations


def patch_server(
    svc: Any,
    user_id: int,
    server_name: str,
    *,
    enabled: bool | None = None,
    default_open: bool | None = None,
    shared: bool | None = None,
) -> dict[str, Any]:
    """One server's flags, scoped to its owning document."""

    def transform(spec: dict[str, Any]) -> dict[str, Any]:
        if enabled is not None:
            spec["enabled"] = enabled
            if not enabled:
                spec.pop("default_open", None)
        if default_open is not None:
            if default_open:
                spec["default_open"] = True
            else:
                spec.pop("default_open", None)
        if shared is not None:
            if shared:
                spec["shared"] = True
            else:
                spec.pop("shared", None)
        return spec

    return update_server_spec(svc, user_id, server_name, transform)


def apply_server_oauth(
    svc: Any,
    user_id: int,
    server_name: str,
    tokens: dict[str, Any],
    *,
    issuer: str,
    resource: str | None,
) -> dict[str, Any]:
    from octop.infra.connectors.custom_mcp import (
        build_oauth_storage,
        set_oauth_required_in_spec,
    )

    def transform(spec: dict[str, Any]) -> dict[str, Any]:
        access = str(tokens.get("access_token") or "").strip()
        if not access:
            raise ValueError("missing access_token")
        spec["oauth"] = build_oauth_storage(tokens, issuer=issuer, resource=resource)
        return set_oauth_required_in_spec(spec, required=False)

    return update_server_spec(svc, user_id, server_name, transform)


def note_oauth_required(
    svc: Any, user_id: int, server_name: str, *, required: bool
) -> dict[str, Any]:
    from octop.infra.connectors.custom_mcp import set_oauth_required_in_spec

    def transform(spec: dict[str, Any]) -> dict[str, Any]:
        return set_oauth_required_in_spec(spec, required=required)

    return update_server_spec(svc, user_id, server_name, transform)


async def ensure_fresh(svc: Any, user_id: int) -> dict[str, Any]:
    """OAuth refresh pass — per-document writes, siblings never rewritten."""
    import time

    from octop.infra.connectors.custom_mcp import (
        mark_oauth_reauth_required,
        oauth_configured,
        oauth_tokens_from_spec,
    )
    from octop.infra.connectors.oauth.registry import refresh_custom_mcp_oauth
    from octop.infra.connectors.service import _OAUTH_REFRESH_SKEW_SEC

    now = int(time.time())
    for row in list_connector_rows(svc, user_id):
        if not row.has_credentials:
            continue
        servers = dict(doc_servers(svc, row))
        changed = False
        for name, raw in list(servers.items()):
            if not isinstance(raw, dict):
                continue
            oauth = oauth_tokens_from_spec(raw)
            if not oauth_configured(raw):
                continue
            expires_at_raw = oauth.get("expires_at")
            expires_at = int(expires_at_raw) if expires_at_raw is not None else None
            refresh = str(oauth.get("refresh_token") or "").strip()
            if expires_at is not None and expires_at <= now and not refresh:
                servers[name] = mark_oauth_reauth_required(dict(raw))
                changed = True
                continue
            if not refresh:
                continue
            if expires_at is not None and expires_at > now + _OAUTH_REFRESH_SKEW_SEC:
                continue
            try:
                refreshed = await refresh_custom_mcp_oauth(oauth)
            except Exception:
                logging.getLogger(__name__).warning(
                    "custom MCP oauth refresh failed for %r (user_id=%s)",
                    name,
                    user_id,
                    exc_info=True,
                )
                if expires_at is not None and expires_at <= now:
                    servers[name] = mark_oauth_reauth_required(dict(raw))
                    changed = True
                continue
            spec = dict(raw)
            spec["oauth"] = refreshed
            servers[name] = spec
            changed = True
        if changed:
            _write_doc(svc, row, servers)
    return merged_custom_servers(svc, user_id)


# -------------------------------------------------------------- ACL plumbing


def resolve_acl_parent(svc: Any, instance_id: str, user_id: int) -> ConnectorRow | None:
    """Map a synthetic custom-MCP child id to its owning connector row.

    ``custom:PARENT:NAME`` points at a shared parent directly; plain
    ``custom:NAME`` is one of the caller's own servers and now resolves by
    scanning the caller's documents for the one containing that server —
    the single-document ``get_by_user_kind`` guess is gone.
    """
    raw = parse_synthetic_instance_id(instance_id)
    if raw is None:
        return None
    parent_id, separator, _ = raw.partition(":")
    if separator:
        parent = svc._repo.get(parent_id)
        if parent is not None and is_custom_mcp_kind(parent.kind):
            return parent
        return None
    return locate_connector_for_server(svc, user_id, parent_id)
