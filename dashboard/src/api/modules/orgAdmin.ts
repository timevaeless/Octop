/**
 * Tenant-admin API (2026-09-30 split).
 *
 * The org_admin face of 「Roles & Access」: tenant-owned ability packages,
 * this tenant's job-code mappings, and ``allowed_roles`` on this tenant's
 * resources. Everything is scoped server-side to the path org — the client
 * never sends an ``org_id`` in a body, and the ``'*'`` platform rows are
 * invisible here by contract.
 */
import { request } from "../request";
import type { ResourceKind, RoleMapping } from "./roles";

/** A package as the tenant plane sees it. */
export interface TenantRole {
  /** Full storage key — tenant rows carry the ``t_{org}_`` prefix. */
  role_key: string;
  /** Display key: prefix stripped for tenant-owned rows. */
  bare_key: string;
  /** ``null`` = platform package. */
  org_id: string | null;
  name_zh: string;
  is_default: boolean;
  /** System rows are read-only and non-grantable from the tenant face. */
  is_system: boolean;
  description: string;
}

/** Normalized resource row from the tenant ACL listing. */
export interface TenantResourceAclRow {
  id: string;
  name: string;
  allowed_roles: string[];
  /** experts only: template connector refs (custom-mcp:{sid}__{instance}) —
   * advisory data for the tenant MCP catalog's role-mismatch hint. */
  default_mcp_servers?: string[];
}

const enc = encodeURIComponent;

export const orgAdminApi = {
  // ----- ability packages -----

  /** Platform packages (org_id IS NULL) plus the tenant's own rows. */
  listRoles(orgId: string): Promise<TenantRole[]> {
    return request<TenantRole[]>(`/org/${enc(orgId)}/roles`, { method: "GET" });
  },
  /** ``bare_key`` is the display key — the ``t_{org}_`` prefix is minted server-side. */
  createRole(
    orgId: string,
    body: { bare_key: string; name_zh: string; description?: string; is_default?: boolean },
  ): Promise<TenantRole> {
    return request<TenantRole>(`/org/${enc(orgId)}/roles`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  },
  updateRole(
    orgId: string,
    roleKey: string,
    body: { name_zh?: string; description?: string; is_default?: boolean },
  ): Promise<TenantRole> {
    return request<TenantRole>(`/org/${enc(orgId)}/roles/${enc(roleKey)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
  },
  deleteRole(orgId: string, roleKey: string): Promise<void> {
    return request<void>(`/org/${enc(orgId)}/roles/${enc(roleKey)}`, {
      method: "DELETE",
    });
  },

  // ----- job-code mappings (tenant-scoped; never the '*' rows) -----

  listMappings(orgId: string, jobCode?: string): Promise<RoleMapping[]> {
    const qs = jobCode ? `?job_code=${enc(jobCode)}` : "";
    return request<RoleMapping[]>(`/org/${enc(orgId)}/role-mappings${qs}`, {
      method: "GET",
    });
  },
  setMapping(
    orgId: string,
    body: { job_code: string; role_keys: string[]; previous_job_code?: string },
  ): Promise<RoleMapping> {
    return request<RoleMapping>(`/org/${enc(orgId)}/role-mappings`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
  },

  // ----- resource ACLs (allowed_roles only) -----

  async listResourceAcl(
    orgId: string,
    kind: ResourceKind,
  ): Promise<TenantResourceAclRow[]> {
    return request<TenantResourceAclRow[]>(
      `/org/${enc(orgId)}/resource-acl/${kind}`,
      { method: "GET" },
    );
  },
  setResourceAcl(
    orgId: string,
    kind: ResourceKind,
    id: string,
    allowedRoles: string[],
  ): Promise<{ kind: string; id: string; allowed_roles: string[] }> {
    return request(`/org/${enc(orgId)}/resource-acl/${kind}/${enc(id)}`, {
      method: "PUT",
      body: JSON.stringify({ allowed_roles: allowedRoles }),
    });
  },

  // ----- tenant MCP catalog (2026-10-01: the ONE MCP surface for the
  // tenant admin — attach published services as connectors and scope
  // service-/tool-level roles; tools may only narrow the service default) -----

  listMcpServices(orgId: string): Promise<TenantMcpService[]> {
    return request<TenantMcpService[]>(`/org/${enc(orgId)}/mcp/services`, {
      method: "GET",
    });
  },
  setMcpServiceRoles(
    orgId: string,
    serviceId: string,
    allowedRoles: string[],
  ): Promise<{ service_id: string; allowed_roles: string[] }> {
    return request(
      `/org/${enc(orgId)}/mcp/services/${enc(serviceId)}/allowed-roles`,
      { method: "PUT", body: JSON.stringify({ allowed_roles: allowedRoles }) },
    );
  },
  setMcpToolRoles(
    orgId: string,
    serviceId: string,
    toolName: string,
    allowedRoles: string[],
  ): Promise<{ service_id: string; tool_name: string; allowed_roles: string[] }> {
    return request(
      `/org/${enc(orgId)}/mcp/services/${enc(serviceId)}/tools/${enc(toolName)}/allowed-roles`,
      { method: "PUT", body: JSON.stringify({ allowed_roles: allowedRoles }) },
    );
  },
  attachMcpService(
    orgId: string,
    serviceId: string,
  ): Promise<{
    service_id: string;
    org_id: string;
    action: "created" | "updated";
    allowed_roles: string[];
  }> {
    return request(`/org/${enc(orgId)}/mcp/services/${enc(serviceId)}/attach`, {
      method: "POST",
    });
  },
  detachMcpService(
    orgId: string,
    serviceId: string,
  ): Promise<{ service_id: string; org_id: string; detached: boolean }> {
    return request(`/org/${enc(orgId)}/mcp/services/${enc(serviceId)}/attach`, {
      method: "DELETE",
    });
  },
};

/** One tool row of a tenant-visible MCP service. */
export interface TenantMcpTool {
  tool_name: string;
  description: string | null;
  enabled: boolean;
  /** Raw stored config — ``[]`` means "inherit the service default". */
  allowed_roles: string[];
  /** What the hub actually honors (subset rule applied). */
  effective_roles: string[];
  inherited: boolean;
}

/** A service as the tenant MCP catalog lists it. */
export interface TenantMcpService {
  service_id: string;
  display_name: string;
  description: string | null;
  status: "draft" | "published" | "disabled";
  /** ``"*"`` = platform-shared (read-only for the tenant), else own tenant. */
  org_id: string | null;
  allowed_roles: string[];
  /** Own-tenant rows are role-writable; platform-shared rows are not. */
  writable: boolean;
  /** Whether the tenant has attached this service as a shared connector. */
  attached: boolean;
  tools: TenantMcpTool[];
}
