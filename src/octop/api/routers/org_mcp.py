"""Tenant-face MCP catalog (2026-10-01 distribution design).

The tenant admin's ONE MCP surface, per the user decision flow:

    平台管理员建服务+发布 → 租户管理员接入连接器、配服务级/工具级角色 →
    建专家发模板 → 普通用户装模板即用

Endpoint group ``/api/org/{org_id}/mcp`` (mounted with the other tenant
routers). Configuration lives HERE — the resource-acl ``mcp-services`` family
is retired (one resource, one config point); platform-shared ``'*'`` rows are
listed read-only, own-tenant rows are writable.

Standalone module per user convention: registration is one line in app.py;
deleting the module and that line restores pre-feature behaviour verbatim.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from octop.api.deps import current_user, get_server
from octop.api.routers.org_admin import _validate_tenant_keys
from octop.api.routers.org_members import _require_org_admin
from octop.mcp_catalog.publisher import effective_tool_roles, validate_tool_roles_subset
from octop.mcp_catalog.repo import CatalogRepo
from octop.mcp_catalog.router import _connector_service, _schedule_connector_reload
from octop.mcp_catalog.tenant_share import (
    attach_service_to_org,
    catalog_service_attachments,
    detach_service_from_org,
    sync_service_attachments,
)

router = APIRouter()


def _repo(server: Any) -> CatalogRepo:
    return CatalogRepo(server.services.db)


def _load_service_for_org(server: Any, org_id: str, service_id: str) -> Any:
    """Service visible to this tenant (own rows + platform-shared), else 404."""
    svc = _repo(server).get_service(service_id)
    if svc is None or (svc.org_id != org_id and svc.org_id != "*"):
        raise HTTPException(status_code=404, detail=f"service {service_id!r} not found")
    return svc


def _load_own_service(server: Any, org_id: str, service_id: str) -> Any:
    """Service ATTRIBUTED to this tenant; shared rows are platform-managed."""
    svc = _load_service_for_org(server, org_id, service_id)
    if svc.org_id != org_id:
        raise HTTPException(
            status_code=403,
            detail="platform-shared MCP services are configured by the platform administrator",
        )
    return svc


def _service_payload(
    repo: CatalogRepo, attached_repo: Any, svc: Any, org_id: str
) -> dict[str, Any]:
    tools = repo.list_tools(svc.service_id)
    return {
        "service_id": svc.service_id,
        "display_name": svc.display_name,
        "description": svc.description,
        "status": svc.status,
        "org_id": svc.org_id,
        "allowed_roles": list(svc.allowed_roles or []),
        "writable": svc.org_id == org_id,
        "attached": catalog_service_attachments(attached_repo, svc.service_id, org_id),
        "tools": [
            {
                "tool_name": t.tool_name,
                "description": t.description,
                "enabled": t.enabled,
                "allowed_roles": list(t.allowed_roles or []),
                # what the hub will actually honor (subset rule applied)
                "effective_roles": effective_tool_roles(t.allowed_roles, svc.allowed_roles),
                "inherited": not t.allowed_roles,
            }
            for t in tools
        ],
    }


@router.get("/{org_id}/mcp/services", summary="List MCP services visible to this tenant")
async def list_tenant_mcp_services(
    org_id: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> list[dict[str, Any]]:
    _require_org_admin(server, user, org_id)
    repo = _repo(server)
    attached_repo = _connector_service(server).catalog_attachment_repo()
    with server.services.db.connect() as conn:
        rows = conn.execute(
            "SELECT service_id FROM mcp_services WHERE org_id = ? OR org_id = '*' "
            "ORDER BY display_name",
            (org_id,),
        ).fetchall()
    return [
        _service_payload(repo, attached_repo, repo.get_service(str(r["service_id"])), org_id)
        for r in rows
    ]


class RolesBody(BaseModel):
    allowed_roles: list[str] = Field(default_factory=list)


def _push_acl_if_published(server: Any, repo: CatalogRepo, svc: Any, actor: str) -> None:
    if svc.status != "published":
        return
    from octop.mcp_catalog.router import _global_acl, _publish_lock, _publisher

    try:
        with _publish_lock(repo) as fence:
            _publisher().push_acl(_global_acl(repo), fence=fence)
    except Exception as e:  # noqa: BLE001 — surfaced, not swallowed
        repo.log_publish(
            svc.service_id, "acl-repush", "error", {"error": str(e)}, actor=actor
        )
        raise HTTPException(
            status_code=502,
            detail="roles updated in the catalog but the hub re-distribution failed; retry or run reconcile",
        ) from e
    repo.log_publish(
        svc.service_id, "acl-repush", "ok", {"written": True}, actor=actor
    )


@router.put(
    "/{org_id}/mcp/services/{service_id}/allowed-roles",
    summary="Set which packages can use this tenant's service",
)
async def set_tenant_mcp_service_roles(
    org_id: str,
    service_id: str,
    body: RolesBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    _require_org_admin(server, user, org_id)
    repo = _repo(server)
    _load_own_service(server, org_id, service_id)
    roles = _validate_tenant_keys(server, org_id, body.allowed_roles)
    repo.update_service(service_id, {"allowed_roles": roles})
    svc = repo.get_service(service_id)
    # Subset rule: narrowing the service default pulls explicit tool configs
    # back into the subset, then re-distributes the hub ACL (published only).
    from octop.mcp_catalog.publisher import prune_tools_to_service_roles

    prune_tools_to_service_roles(repo, service_id, roles)
    _push_acl_if_published(server, repo, svc, actor=user.username)
    try:
        conn_svc = _connector_service(server)
        sync_service_attachments(conn_svc, conn_svc.catalog_attachment_repo(), svc)
    except Exception:  # noqa: BLE001 — catalog write already landed
        import logging

        logging.getLogger(__name__).exception(
            "attachment sync failed for service %s", service_id
        )
    server.services.audit_repo.write(
        actor=user.username,
        action="resource.acl.set",
        target=f"mcp-services/{service_id}",
        payload=f"org={org_id} roles={','.join(roles) or '-'}",
        org_id=org_id,
    )
    svc = repo.get_service(service_id)
    return {"service_id": service_id, "allowed_roles": list(svc.allowed_roles or [])}  # type: ignore[union-attr]


@router.put(
    "/{org_id}/mcp/services/{service_id}/tools/{tool_name}/allowed-roles",
    summary="Narrow one tool to a subset of the service's packages",
)
async def set_tenant_mcp_tool_roles(
    org_id: str,
    service_id: str,
    tool_name: str,
    body: RolesBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    _require_org_admin(server, user, org_id)
    repo = _repo(server)
    svc = _load_own_service(server, org_id, service_id)
    tool = repo.get_tool(service_id, tool_name)
    if tool is None:
        raise HTTPException(status_code=404, detail=f"tool {tool_name!r} not found")
    roles = _validate_tenant_keys(server, org_id, body.allowed_roles)
    # Subset rule (2026-10-01): a tool may only narrow the service default.
    try:
        validate_tool_roles_subset(svc.allowed_roles, roles)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    repo.upsert_tool(
        {
            "service_id": service_id,
            "tool_name": tool_name,
            "description": tool.description,
            "http_method": tool.http_method,
            "http_path": tool.http_path,
            "parameters": tool.parameters,
            "request_body": tool.request_body,
            "response_body": tool.response_body,
            "allowed_roles": roles,
            "enabled": tool.enabled,
            "source": tool.source,
        }
    )
    _push_acl_if_published(server, repo, svc, actor=user.username)
    server.services.audit_repo.write(
        actor=user.username,
        action="resource.acl.set",
        target=f"mcp-services/{service_id}/tools/{tool_name}",
        payload=f"org={org_id} roles={','.join(roles) or '-'}",
        org_id=org_id,
    )
    return {"service_id": service_id, "tool_name": tool_name, "allowed_roles": roles}


@router.post(
    "/{org_id}/mcp/services/{service_id}/attach",
    summary="Attach a published service as this tenant's shared connector",
)
async def attach_tenant_mcp_service(
    org_id: str,
    service_id: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    _require_org_admin(server, user, org_id)
    svc = _load_service_for_org(server, org_id, service_id)
    if svc.status != "published":
        raise HTTPException(
            status_code=409, detail="attach a service after it is published"
        )
    conn_svc = _connector_service(server)
    base = conn_svc.catalog_setting("mcp_catalog_higress_base")
    if not base:
        raise HTTPException(
            status_code=400,
            detail="Higress base URL not configured — the platform administrator sets it first",
        )
    result = attach_service_to_org(
        conn_svc,
        conn_svc.catalog_attachment_repo(),
        svc,
        org_id=org_id,
        owner_user_id=user.id,
        higress_base=base,
    )
    server.services.audit_repo.write(
        actor=user.username,
        action="mcp.connector.attach",
        target=service_id,
        payload=f"org={org_id} action={result['action']}",
        org_id=org_id,
    )
    _schedule_connector_reload(server, user.id)
    return {
        "service_id": service_id,
        "org_id": org_id,
        "action": result["action"],
        "instance_id": result["instance_id"],
        "allowed_roles": list(svc.allowed_roles or []),
    }


@router.delete(
    "/{org_id}/mcp/services/{service_id}/attach",
    summary="Detach this tenant's connector for the service",
)
async def detach_tenant_mcp_service(
    org_id: str,
    service_id: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    _require_org_admin(server, user, org_id)
    _load_service_for_org(server, org_id, service_id)
    removed = detach_service_from_org(
        _connector_service(server).catalog_attachment_repo(), service_id, org_id
    )
    if not removed:
        raise HTTPException(status_code=404, detail="service is not attached")
    server.services.audit_repo.write(
        actor=user.username,
        action="mcp.connector.detach",
        target=service_id,
        payload=f"org={org_id}",
        org_id=org_id,
    )
    _schedule_connector_reload(server, user.id)
    return {"service_id": service_id, "org_id": org_id, "detached": True}
