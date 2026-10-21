"""Connector service — MCP config assembly and credential access."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any
from weakref import WeakKeyDictionary

from octop.config import OctopConfig
from octop.infra.connectors import qcc
from octop.infra.connectors.builder import build_http_mcp_spec, new_internal_token
from octop.infra.connectors.catalog import get_catalog_entry, uses_internal_http_mcp
from octop.infra.connectors.crypto import decrypt_credentials, encrypt_credentials
from octop.infra.connectors.custom_mcp import (
    CUSTOM_MCP_KIND,
    enabled_harness_configs,
    expand_custom_instances,
    extract_servers,
    is_custom_mcp_kind,
    redact_servers_for_api,
    server_enabled,
    shared_mcp_server_name,
)
from octop.infra.connectors.default_open import merge_mcp_servers_with_defaults, read_default_open
from octop.infra.connectors.gateway import agently_auth
from octop.infra.connectors.gateway.cli_dirs import resolve_cli_config_key
from octop.infra.connectors.gateway.feishu_user_auth import (
    complete_user_device_login,
    start_user_device_login,
)
from octop.infra.connectors.oauth import refresh_oauth_credentials
from octop.infra.connectors.oauth.registry import refresh_custom_mcp_oauth
from octop.infra.db.repos.connectors import ConnectorRepo, ConnectorRow
from octop.infra.db.repos.secrets import SecretRepo
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.paths import PathLayout
from octop.infra.utils.ulid import new_ulid

logger = logging.getLogger(__name__)

_OAUTH_REFRESH_SKEW_SEC = 120

# Shared only within one process and application repository.
_QCC_LOCKS: WeakKeyDictionary[ConnectorRepo, dict[str, asyncio.Lock]] = WeakKeyDictionary()
_AGENTLY_LOCKS: WeakKeyDictionary[ConnectorRepo, dict[str, asyncio.Lock]] = WeakKeyDictionary()


def _shared_custom_parent_visible(parent: ConnectorRow, viewer_id: int, authz: Any) -> bool:
    """T2 pluggable hook — implementation in octop.infra.users.tenancy.shared_row_visible.

    Tenancy + role gate for *another user's* shared custom-MCP parent row:
    without it the shared expansion loops would mount another tenant's
    business-system MCP specs into this tenant's runtime. Lazy import;
    deleting that function together with this delegation restores community
    behaviour verbatim.
    """
    from octop.infra.users.tenancy import shared_row_visible

    return shared_row_visible(parent, authz)


class ConnectorNameTakenError(ValueError):
    """Raised when a connector display name is already owned by the user."""


def list_user_connector_instances(
    repo: ConnectorRepo,
    user_id: int,
    *,
    active_only: bool = False,
    with_credentials: bool = False,
) -> list[ConnectorRow]:
    """List connector instances for *user_id* with optional filters."""
    rows = repo.list_by_user(user_id)
    if active_only:
        rows = [r for r in rows if r.status == "active"]
    if with_credentials:
        rows = [r for r in rows if r.has_credentials]
    return rows


class ConnectorService:
    def __init__(
        self,
        *,
        repo: ConnectorRepo,
        secret_repo: SecretRepo,
        settings_repo: Any,
        config: OctopConfig,
    ) -> None:
        self._repo = repo
        self._secret_repo = secret_repo
        self._settings_repo = settings_repo
        self._config = config

    def list_user_instances(
        self,
        user_id: int,
        *,
        active_only: bool = False,
        with_credentials: bool = False,
    ) -> list[ConnectorRow]:
        return list_user_connector_instances(
            self._repo,
            user_id,
            active_only=active_only,
            with_credentials=with_credentials,
        )

    def decrypt(self, instance_id: str) -> dict[str, Any]:
        row = self._repo.get(instance_id)
        if row is None or not row.credential_blob:
            return {}
        return decrypt_credentials(self._secret_repo, row.credential_blob)

    def encrypt_and_store(
        self,
        *,
        instance_id: str,
        payload: dict[str, Any],
    ) -> None:
        stored = dict(payload)
        row = self._repo.get(instance_id)
        if row is not None:
            entry = get_catalog_entry(row.kind)
            if entry is not None and uses_internal_http_mcp(entry):
                existing = (
                    decrypt_credentials(self._secret_repo, row.credential_blob)
                    if row.credential_blob
                    else {}
                )
                stored["internal_token"] = (
                    existing.get("internal_token")
                    or stored.get("internal_token")
                    or new_internal_token()
                )
            if row.kind == "agently-cli" and row.credential_blob:
                # Editing a connector must never switch to another instance's grant.
                existing = decrypt_credentials(self._secret_repo, row.credential_blob)
                stored["cli_config_key"] = resolve_cli_config_key(existing)
        stored["instance_id"] = instance_id
        expires_at = stored.get("expires_at")
        exp = int(expires_at) if expires_at is not None else None
        blob = encrypt_credentials(self._secret_repo, stored)
        self._repo.upsert_credentials(instance_id=instance_id, blob=blob, expires_at=exp)

    async def ensure_fresh_credentials(
        self,
        instance_id: str,
        kind: str,
    ) -> dict[str, Any]:
        if kind == "qcc":
            return await self._fresh_qcc(instance_id)
        creds = self.decrypt(instance_id)
        entry = get_catalog_entry(kind)
        if entry is None or entry.auth_kind != "oauth2":
            return creds
        expires_at = creds.get("expires_at")
        if expires_at and int(expires_at) > int(time.time()) + 120:
            return creds
        refresh = str(creds.get("refresh_token") or "")
        if not refresh:
            return creds
        try:
            refreshed = await refresh_oauth_credentials(
                kind=kind,
                creds=creds,
                settings_repo=self._settings_repo,
            )
        except Exception:
            return creds
        creds.update(refreshed)
        self.encrypt_and_store(instance_id=instance_id, payload=creds)
        return creds

    def _qcc_lock(self, instance_id: str) -> asyncio.Lock:
        locks = _QCC_LOCKS.setdefault(self._repo, {})
        return locks.setdefault(instance_id, asyncio.Lock())

    async def _fresh_qcc(
        self, instance_id: str, *, rejected_token: str | None = None
    ) -> dict[str, Any]:
        async with self._qcc_lock(instance_id):
            row = self._repo.get(instance_id)
            if row is None or row.kind != "qcc" or row.status != "active":
                return {}
            creds = self.decrypt(instance_id)
            if not qcc.is_oauth_grant(creds):
                return creds
            token = qcc.bearer_token(creds)
            # Another request may already have rotated the token rejected by a 401.
            force = rejected_token is not None and rejected_token == token
            expires = int(creds.get("expires_at") or 0)
            if not force and expires > int(time.time()) + _OAUTH_REFRESH_SKEW_SEC:
                return creds
            if not creds.get("refresh_token"):
                if force or (expires and expires <= int(time.time())):
                    raise ValueError("QCC authorization expired; please authorize again")
                return creds
            if not creds.get("oauth_client_id"):
                raise ValueError("QCC client registration missing; please authorize again")
            refreshed = await refresh_oauth_credentials(
                kind="qcc", creds=creds, settings_repo=self._settings_repo
            )
            creds.update(refreshed)
            self.encrypt_and_store(instance_id=instance_id, payload=creds)
            return creds

    async def _qcc_request(
        self, instance_id: str, resource: str, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        creds = await self._fresh_qcc(instance_id)
        token = qcc.bearer_token(creds)
        if not token:
            raise ValueError("QCC connector is disconnected")
        try:
            return await qcc.request_resource(resource, token, method, params)
        except Exception as exc:
            if not qcc.is_oauth_grant(creds) or not qcc.unauthorized(exc):
                raise
        creds = await self._fresh_qcc(instance_id, rejected_token=token)
        token = qcc.bearer_token(creds)
        if not token:
            raise ValueError("QCC connector is disconnected")
        return await qcc.request_resource(resource, token, method, params)

    async def handle_qcc_request(self, instance_id: str, body: dict[str, Any]) -> dict[str, Any]:
        from octop.infra.connectors.gateway.protocol import handle_mcp_request

        method = body.get("method")
        if method not in ("tools/list", "tools/call"):
            return handle_mcp_request(kind="qcc", creds={}, body=body)
        try:
            if method == "tools/list":
                listed = await asyncio.gather(
                    *[
                        self._qcc_request(instance_id, resource, "tools/list", {})
                        for resource in qcc.RESOURCES
                    ],
                    return_exceptions=True,
                )
                tools: list[dict[str, Any]] = []
                for resource, item in zip(qcc.RESOURCES, listed, strict=True):
                    if isinstance(item, BaseException):
                        continue
                    tools.extend(qcc.namespace_tools(resource, item))
                if not tools:
                    raise ValueError("QCC MCP request failed; check connection or authorize again")
                result: dict[str, Any] = {"tools": tools}
            else:
                params = dict(body.get("params") or {})
                resource, sep, name = str(params.get("name") or "").partition("__")
                if not sep or resource not in qcc.RESOURCES or not name:
                    raise ValueError("Unknown QCC tool")
                result = await self._qcc_request(
                    instance_id, resource, "tools/call", {**params, "name": name}
                )
            return {"jsonrpc": "2.0", "id": body.get("id"), "result": result}
        except Exception:
            # Upstream exceptions may include credential-bearing request details.
            return {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "error": {
                    "code": -32603,
                    "message": "QCC MCP request failed; check connection or authorize again",
                },
            }

    async def disconnect_qcc(self, instance_id: str) -> None:
        async with self._qcc_lock(instance_id):
            row = self._repo.get(instance_id)
            if row is None:
                return
            if row.kind != "qcc":
                raise ValueError("Not a QCC connector")
            try:
                creds = self.decrypt(instance_id)
                if qcc.is_oauth_grant(creds):
                    await qcc.revoke(creds)
            except Exception as exc:
                # Keep the encrypted grant so the user can retry remote revocation.
                raise ValueError("QCC revocation failed; retry disconnect") from exc
            self._repo.delete(instance_id)

    def reserved_builtin_mcp_names(self, user_id: int) -> set[str]:
        names: set[str] = set()
        for inst in self._repo.list_by_user(user_id):
            if is_custom_mcp_kind(inst.kind):
                continue
            names.add(inst.mcp_server_name)
        return names

    def get_custom_servers(self, user_id: int) -> dict[str, Any]:
        """Merged view of ALL the user's custom-MCP connectors (T4 multi-doc).

        Delegation hook: implementation in octop.infra.connectors.custom_multi
        (one connector = one document; reads merge by server name). Deleting
        that module and restoring the body below restores single-document
        behaviour verbatim.
        """
        from octop.infra.connectors.custom_multi import merged_custom_servers

        return merged_custom_servers(self, user_id)

    def get_custom_servers_for_api(self, user_id: int) -> dict[str, Any]:
        return redact_servers_for_api(self.get_custom_servers(user_id))

    # ---- multi-instance connector CRUD (T4: bodies in custom_multi) ----

    def list_custom_connectors(self, user_id: int) -> list[dict[str, Any]]:
        from octop.infra.connectors.custom_multi import list_connectors_for_api

        return list_connectors_for_api(self, user_id)

    def create_custom_connector(
        self, user_id: int, name: str, servers: Any, org_id: str | None = None
    ) -> dict[str, Any]:
        from octop.infra.connectors.custom_multi import create_connector

        return create_connector(self, user_id, name, servers, org_id=org_id)

    def save_custom_connector(
        self,
        user_id: int,
        instance_id: str,
        servers: Any,
        org_id: str | None = None,
    ) -> dict[str, Any]:
        from octop.infra.connectors.custom_multi import save_connector_servers

        return save_connector_servers(self, user_id, instance_id, servers, org_id=org_id)

    def rename_custom_connector(
        self, user_id: int, instance_id: str, name: str
    ) -> dict[str, Any]:
        from octop.infra.connectors.custom_multi import rename_connector

        return rename_connector(self, user_id, instance_id, name)

    def delete_custom_connector(self, user_id: int, instance_id: str) -> dict[str, Any]:
        from octop.infra.connectors.custom_multi import delete_connector

        return delete_connector(self, user_id, instance_id)

    def apply_custom_server_oauth(
        self,
        user_id: int,
        server_name: str,
        tokens: dict[str, Any],
        *,
        issuer: str,
        resource: str | None,
    ) -> dict[str, Any]:
        """OAuth grant persists to the server's OWN document (T4 multi-doc)."""
        from octop.infra.connectors.custom_multi import apply_server_oauth

        return apply_server_oauth(
            self, user_id, server_name, tokens, issuer=issuer, resource=resource
        )

    async def ensure_fresh_custom_servers(self, user_id: int) -> dict[str, Any]:
        """OAuth refresh pass — per-document writes (T4 multi-doc hook)."""
        from octop.infra.connectors.custom_multi import ensure_fresh

        return await ensure_fresh(self, user_id)

    def patch_custom_server_enabled(
        self,
        user_id: int,
        server_name: str,
        *,
        enabled: bool,
    ) -> dict[str, Any]:
        return self.patch_custom_server(user_id, server_name, enabled=enabled)

    def patch_custom_server(
        self,
        user_id: int,
        server_name: str,
        *,
        enabled: bool | None = None,
        default_open: bool | None = None,
        shared: bool | None = None,
    ) -> dict[str, Any]:
        """One server's flags, written to its OWN document (T4 multi-doc)."""
        from octop.infra.connectors.custom_multi import patch_server

        return patch_server(
            self,
            user_id,
            server_name,
            enabled=enabled,
            default_open=default_open,
            shared=shared,
        )

    def note_custom_server_oauth_required(
        self,
        user_id: int,
        server_name: str,
        *,
        required: bool,
    ) -> dict[str, Any]:
        from octop.infra.connectors.custom_multi import note_oauth_required

        return note_oauth_required(self, user_id, server_name, required=required)

    def patch_custom_server_default_open(
        self,
        user_id: int,
        server_name: str,
        *,
        default_open: bool,
    ) -> dict[str, Any]:
        return self.patch_custom_server(user_id, server_name, default_open=default_open)

    def list_instances_for_api(self, user_id: int, authz: Any = None) -> list[dict[str, Any]]:
        """Built-in rows + expanded custom servers (hide parent custom-mcp row).

        ``authz`` (an ``AuthzContext``) applies tenancy + role ACL to shared
        instances; ``None`` keeps the legacy unfiltered behavior.
        """
        out: list[dict[str, Any]] = []
        for inst in self._repo.list_visible(
            user_id,
            org_id=authz.org_id if authz is not None else None,
            role_keys=authz.role_filter if authz is not None else None,
        ):
            if is_custom_mcp_kind(inst.kind):
                continue
            config = ConnectorRepo.parse_config_json(inst)
            out.append(
                {
                    "instance_id": inst.instance_id,
                    "kind": inst.kind,
                    "display_name": inst.display_name,
                    "description": config.get("description"),
                    "status": inst.status,
                    "mcp_server_name": inst.mcp_server_name,
                    "has_credentials": inst.has_credentials,
                    "default_open": read_default_open(config),
                    "shared": inst.shared,
                    # Tenancy + resource ACL echo (016/017): the admin resource-ACL
                    # tab reads these off the listing; without them a stored ACL is
                    # indistinguishable from "no ACL" after a refresh.
                    "org_id": inst.org_id,
                    "allowed_roles": list(inst.allowed_roles or []),
                    "owner_user_id": inst.user_id,
                    "created_at": inst.created_at,
                    "updated_at": inst.updated_at,
                }
            )
        # Own custom connectors: one document per connector (T4 multi-doc).
        from octop.infra.connectors.custom_multi import expand_own

        out.extend(expand_own(self, user_id))
        for parent in self._repo.list_by_kind(CUSTOM_MCP_KIND):
            if parent.user_id == user_id or not parent.has_credentials:
                continue
            if not _shared_custom_parent_visible(parent, user_id, authz):
                continue
            out.extend(
                expand_custom_instances(
                    parent=parent,
                    servers=extract_servers(self.decrypt(parent.instance_id)),
                    shared_view=True,
                )
            )
        for item in out:
            item.setdefault("owner_user_id", user_id)
            item.setdefault("shared", False)
        return out

    def list_active_mcp_server_names(self, user_id: int, authz: Any = None) -> list[str]:
        names: list[str] = []
        for inst in self._repo.list_visible(
            user_id,
            org_id=authz.org_id if authz is not None else None,
            role_keys=authz.role_filter if authz is not None else None,
        ):
            if is_custom_mcp_kind(inst.kind):
                continue
            if inst.status != "active" or not inst.has_credentials:
                continue
            names.append(inst.mcp_server_name)
        for name, spec in self.get_custom_servers(user_id).items():
            if isinstance(spec, dict) and server_enabled(spec):
                names.append(name)
        for parent in self._repo.list_by_kind(CUSTOM_MCP_KIND):
            if parent.user_id == user_id or not parent.has_credentials:
                continue
            for name, spec in extract_servers(self.decrypt(parent.instance_id)).items():
                if isinstance(spec, dict) and spec.get("shared") is True and server_enabled(spec):
                    names.append(shared_mcp_server_name(parent.instance_id, name))
        # 可插拔 hook（mcp_catalog.tenant_share）：目录派生行以 harness 键挂载。
        from octop.mcp_catalog.tenant_share import refine_catalog_mount_names

        return refine_catalog_mount_names(self, user_id, authz, names)

    def list_assignable_instances_for_api(
        self, user_id: int, authz: Any = None
    ) -> list[dict[str, Any]]:
        """Instances *user_id* may attach to an agent — the save-time scope.

        Same rows as :meth:`list_instances_for_api` (so the picker keeps the
        display labels), narrowed to :meth:`list_active_mcp_server_names` — the
        exact name set :meth:`validate_mcp_servers_for_user` accepts. A picker
        built on the plain listing can therefore offer a server the save then
        rejects with ``CONNECTOR_NOT_BOUND``; this one cannot.
        """
        allowed = set(self.list_active_mcp_server_names(user_id, authz))
        items = [
            item
            for item in self.list_instances_for_api(user_id, authz)
            if item.get("mcp_server_name") in allowed
        ]
        # 可插拔 hook（mcp_catalog.tenant_share）：目录派生行按 harness 键补入。
        from octop.mcp_catalog.tenant_share import append_catalog_assignable_items

        return append_catalog_assignable_items(self, user_id, authz, allowed, items)

    def list_default_open_mcp_server_names(self, user_id: int) -> list[str]:
        """Active connectors marked default_open (dashboard + all IM channels)."""
        names: list[str] = []
        for inst in self._repo.list_by_user(user_id):
            if is_custom_mcp_kind(inst.kind):
                continue
            if inst.status != "active" or not inst.has_credentials:
                continue
            if read_default_open(ConnectorRepo.parse_config_json(inst)):
                names.append(inst.mcp_server_name)
        for name, spec in self.get_custom_servers(user_id).items():
            if not isinstance(spec, dict) or not server_enabled(spec):
                continue
            if spec.get("default_open") is True:
                names.append(name)
        return names

    def merge_turn_mcp_servers(
        self,
        user_id: int,
        explicit: list[str] | None,
        *,
        apply_defaults: bool | None = None,
        extra_defaults: list[str] | None = None,
    ) -> list[str] | None:
        """Resolve turn MCP servers vs the user's default_open set.

        Dashboard passes an explicit list (``apply_defaults=False``) so users can
        opt out for a turn. IM uses ``explicit is None`` + defaults. Cron follows
        defaults when the job has no picks; explicit Cron picks win as-is.
        ``extra_defaults`` (expert composer picks) are unioned with default_open
        when defaults apply, but only names still available to *user_id*.
        """
        defaults = list(self.list_default_open_mcp_server_names(user_id))
        if extra_defaults:
            allowed = set(self.list_active_mcp_server_names(user_id))
            for name in extra_defaults:
                text = str(name).strip()
                if text and text in allowed and text not in defaults:
                    defaults.append(text)
            # T4 hook (mount_health): expert picks dropped above are dead
            # keys — observe them here or they vanish before any failure
            # report can mention them.
            from octop.infra.agents.mount_health import note_dropped_turn_picks  # noqa: PLC0415

            note_dropped_turn_picks(user_id, extra_defaults, allowed)
        return merge_mcp_servers_with_defaults(
            explicit,
            defaults,
            apply_defaults=apply_defaults,
        )

    def validate_mcp_servers_for_user(
        self, user_id: int, names: list[str], authz: Any = None
    ) -> list[str]:
        allowed = set(self.list_active_mcp_server_names(user_id, authz))
        unknown = sorted(set(names) - allowed)
        if unknown:
            raise ValueError(f"mcp_servers not available for user: {unknown}")
        return list(names)

    def _delegated_identity_token_for(self, user_id: int, authz: Any) -> str | None:
        """Mint the delegated-identity JWT for harness-spec building.

        Identity = the user whose config namespace is being built (``user_id``
        + ``authz``). ``authz=None`` (internal/test callers) mints nothing —
        a hub-connected server then fails closed at the hub's 401 instead of
        silently calling with a forged/absent identity. Minting is a local
        HMAC (microseconds); the token only leaves the process when a spec
        actually opted in via ``delegate_identity: true``.
        """
        from octop.infra.connectors.delegation import mint_identity_token

        return mint_identity_token(self._secret_repo, user_id, authz)

    # ---- pluggable-module accessors (octop.mcp_catalog.tenant_share) ----
    # Review 2026-10-02 P1-5: the tenant-share module used to reach three
    # private members (``_repo``, ``_settings_repo``,
    # ``_delegated_identity_token_for``) from outside the class; an upstream
    # rename would have broken it SILENTLY — its broad ``except Exception``
    # guards turn an AttributeError into "no attachments / skip". These three
    # public accessors are the sanctioned surface: deleting them together
    # with the tenant_share hooks in this file restores upstream verbatim.

    def catalog_attachment_repo(self) -> Any:
        """The connector repo, for catalog attachment row queries."""
        return self._repo

    def catalog_setting(self, key: str) -> str:
        """One platform-level connector setting (``mcp_catalog_higress_base``)."""
        return self._settings_repo.get(key) or ""

    def delegated_identity_token(self, user_id: int, authz: Any = None) -> str | None:
        """Public wrapper: mint the viewer-scoped delegation JWT."""
        return self._delegated_identity_token_for(user_id, authz)

    def custom_harness_configs(self, user_id: int, authz: Any = None) -> dict[str, Any]:
        """Own custom MCP configs + shared ones the viewer is allowed to see.

        Shared parents from *other* users pass through
        :func:`_shared_custom_parent_visible` — a cross-tenant or
        role-restricted server must never reach the viewer's agent runtime,
        because its spec carries the owner's endpoint (and possibly header
        credentials) for a business system.
        """
        configs = enabled_harness_configs(
            self.get_custom_servers(user_id),
            delegate_token=self._delegated_identity_token_for(user_id, authz),
        )
        for parent in self._repo.list_by_kind(CUSTOM_MCP_KIND):
            if parent.user_id == user_id or not parent.has_credentials:
                continue
            if not _shared_custom_parent_visible(parent, user_id, authz):
                continue
            servers = extract_servers(self.decrypt(parent.instance_id))
            for name, spec in servers.items():
                if (
                    not isinstance(spec, dict)
                    or spec.get("shared") is not True
                    or not server_enabled(spec)
                ):
                    continue
                built = enabled_harness_configs(
                    {name: spec},
                    delegate_token=self._delegated_identity_token_for(user_id, authz),
                ).get(name)
                if built is not None:
                    configs[shared_mcp_server_name(parent.instance_id, name)] = built
        # Catalog attachments (2026-10-01): per-service parent rows carry the
        # tenant-shared MCP connectors minted from published catalog
        # services — expansion lives in the standalone module.
        from octop.mcp_catalog.tenant_share import catalog_shared_harness_configs

        configs.update(catalog_shared_harness_configs(self, user_id, authz=authz))
        return configs

    async def mcp_configs_for_user(self, user_id: int, authz: Any = None) -> dict[str, Any]:
        configs: dict[str, Any] = {}
        for inst in self._repo.list_visible(
            user_id,
            org_id=authz.org_id if authz is not None else None,
            role_keys=authz.role_filter if authz is not None else None,
        ):
            if inst.status != "active":
                continue
            if is_custom_mcp_kind(inst.kind):
                continue
            entry = get_catalog_entry(inst.kind)
            if entry is None:
                continue
            creds = await self.ensure_fresh_credentials(inst.instance_id, inst.kind)
            if not creds:
                continue
            configs[inst.mcp_server_name] = build_http_mcp_spec(
                entry=entry,
                instance_id=inst.instance_id,
                creds=creds,
                config=self._config,
            )
        await self.ensure_fresh_custom_servers(user_id)
        for parent in self._repo.list_by_kind(CUSTOM_MCP_KIND):
            if parent.user_id == user_id or not parent.has_credentials:
                continue
            if not _shared_custom_parent_visible(parent, user_id, authz):
                continue
            servers = extract_servers(self.decrypt(parent.instance_id))
            if any(
                isinstance(spec, dict) and spec.get("shared") is True for spec in servers.values()
            ):
                await self.ensure_fresh_custom_servers(parent.user_id)
        configs.update(self.custom_harness_configs(user_id))
        return configs

    def verify_internal_token(self, instance_id: str, token: str) -> dict[str, Any] | None:
        creds = self.decrypt(instance_id)
        expected = str(creds.get("internal_token") or "")
        if not expected or expected != token:
            return None
        return creds

    async def agently_auth_for_instance(
        self,
        instance_id: str,
        user_id: int,
        action: agently_auth.AuthAction,
        *,
        locale: str = "en",
    ) -> agently_auth.AuthResult:
        locks = _AGENTLY_LOCKS.setdefault(self._repo, {})
        async with locks.setdefault(instance_id, asyncio.Lock()):
            inst = self._repo.get(instance_id)
            if inst is None:
                raise OctopError.localized(ErrorCode.CONNECTOR_NOT_FOUND, locale)
            if inst.user_id != user_id:
                raise OctopError.localized(ErrorCode.FORBIDDEN, locale)
            if inst.kind not in ("agently-cli", "dingtalk-cli"):
                raise OctopError.localized(ErrorCode.CONNECTOR_KIND_UNSUPPORTED, locale)
            creds = self.decrypt(instance_id)
            creds.setdefault("instance_id", instance_id)
            result = await agently_auth.authorize(creds, action, kind=inst.kind, locale=locale)
            if action == "disconnect" and result["status"] == "idle":
                self._repo.delete(instance_id)
            return result

    # --- Feishu CLI device-code user auth (domain orchestration) ---

    @staticmethod
    def feishu_cli_config_dir(cli_config_key: str) -> Path:
        return PathLayout.from_env().ensure_connector_cli_instance_dir("feishu-cli", cli_config_key)

    async def start_feishu_user_auth(
        self,
        *,
        app_id: str,
        app_secret: str,
        cli_config_key: str | None = None,
        domains: list[str] | None = None,
    ) -> dict[str, Any]:
        """Begin OAuth device-code login (anonymous / pre-save credentials)."""
        app_id = str(app_id or "").strip()
        app_secret = str(app_secret or "").strip()
        if not app_id or not app_secret:
            raise ValueError("app_id and app_secret are required")
        cli_key = str(cli_config_key or "").strip() or new_ulid()
        config_dir = self.feishu_cli_config_dir(cli_key)
        started = await asyncio.to_thread(
            start_user_device_login,
            config_dir=config_dir,
            app_id=app_id,
            app_secret=app_secret,
            domains=domains,
        )
        return {**started, "cli_config_key": cli_key}

    async def complete_feishu_user_auth(
        self,
        *,
        app_id: str,
        app_secret: str,
        device_code: str,
        cli_config_key: str,
    ) -> dict[str, Any]:
        """Finish device-code login for pre-save credentials (no instance write)."""
        app_id = str(app_id or "").strip()
        app_secret = str(app_secret or "").strip()
        device_code = str(device_code or "").strip()
        cli_key = str(cli_config_key or "").strip()
        if not app_id or not app_secret or not device_code or not cli_key:
            raise ValueError("app_id, app_secret, device_code and cli_config_key are required")
        config_dir = self.feishu_cli_config_dir(cli_key)
        result = await asyncio.to_thread(
            complete_user_device_login,
            config_dir=config_dir,
            app_id=app_id,
            app_secret=app_secret,
            device_code=device_code,
        )
        return {**result, "cli_config_key": cli_key, "default_as": "user"}

    def _require_feishu_cli_instance(
        self, instance_id: str, user_id: int
    ) -> tuple[ConnectorRow, dict[str, Any]]:
        inst = self._repo.get(instance_id)
        if inst is None:
            raise OctopError(ErrorCode.CONNECTOR_NOT_FOUND, f"instance {instance_id!r} not found")
        if inst.user_id != user_id:
            raise OctopError(ErrorCode.FORBIDDEN, "not your connector instance")
        if inst.kind != "feishu-cli":
            raise OctopError(
                ErrorCode.CONNECTOR_KIND_UNSUPPORTED,
                "only feishu-cli supports user device login",
            )
        if not inst.has_credentials:
            raise OctopError(ErrorCode.CONNECTOR_INVALID_CREDENTIALS, "missing credentials")
        return inst, dict(self.decrypt(instance_id))

    async def start_feishu_user_auth_for_instance(
        self, instance_id: str, user_id: int
    ) -> dict[str, Any]:
        """Start device login using App Secret stored on the instance."""
        _inst, creds = self._require_feishu_cli_instance(instance_id, user_id)
        app_id = str(creds.get("app_id") or "").strip()
        app_secret = str(creds.get("app_secret") or "").strip()
        if not app_id or not app_secret:
            raise OctopError(
                ErrorCode.CONNECTOR_INVALID_CREDENTIALS,
                "stored Feishu app credentials incomplete",
                details={"reason": "stored Feishu app credentials incomplete"},
            )
        if not str(creds.get("cli_config_key") or "").strip():
            creds["cli_config_key"] = instance_id
        cli_key = resolve_cli_config_key(creds)
        config_dir = self.feishu_cli_config_dir(cli_key)
        started = await asyncio.to_thread(
            start_user_device_login,
            config_dir=config_dir,
            app_id=app_id,
            app_secret=app_secret,
            domains=None,
        )
        return {**started, "cli_config_key": cli_key}

    async def complete_feishu_user_auth_for_instance(
        self,
        instance_id: str,
        user_id: int,
        *,
        device_code: str,
        cli_config_key: str | None = None,
    ) -> dict[str, Any]:
        """Finish device login and persist ``default_as=user`` on the instance."""
        _inst, creds = self._require_feishu_cli_instance(instance_id, user_id)
        app_id = str(creds.get("app_id") or "").strip()
        app_secret = str(creds.get("app_secret") or "").strip()
        device_code = str(device_code or "").strip()
        if not str(creds.get("cli_config_key") or "").strip():
            creds["cli_config_key"] = instance_id
        override = str(cli_config_key or "").strip()
        if override:
            creds["cli_config_key"] = override
        cli_key = resolve_cli_config_key(creds)
        if not app_id or not app_secret or not device_code:
            raise OctopError(
                ErrorCode.CONNECTOR_INVALID_CREDENTIALS,
                "incomplete Feishu user auth payload",
                details={"reason": "incomplete Feishu user auth payload"},
            )
        config_dir = self.feishu_cli_config_dir(cli_key)
        result = await asyncio.to_thread(
            complete_user_device_login,
            config_dir=config_dir,
            app_id=app_id,
            app_secret=app_secret,
            device_code=device_code,
        )
        creds["default_as"] = "user"
        creds["cli_config_key"] = cli_key
        self.encrypt_and_store(instance_id=instance_id, payload=creds)
        return {**result, "cli_config_key": cli_key, "default_as": "user"}
