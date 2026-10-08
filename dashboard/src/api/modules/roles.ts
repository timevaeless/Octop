import { request } from "../request";

/**
 * Ability package (capability bundle) row from the ``roles`` table.
 *
 * ``org_id === null`` marks a platform package (seeded or admin-created); a
 * tenant id marks a tenant-owned package whose storage key carries the
 * reserved ``t_{org}_`` prefix. ``is_system`` rows (``org_admin``/``member``)
 * are platform property — no tenant-side edit, delete, or grant.
 */
export interface RolePackage {
  role_key: string;
  name_zh: string;
  org_id: string | null;
  is_default: boolean;
  is_system?: boolean;
  description: string;
}

export interface OrgInfo {
  org_id: string;
  name: string;
  kind: "internal" | "partner" | string;
  status: "active" | "disabled" | string;
  /**
   * Tenant-scoped row counts.
   *
   * ``binding_count === 0`` is the state that matters: the five-level
   * resolution chain needs a key to match, so a tenant with no binding can
   * never receive a user no matter how many exist.
   */
  member_count?: number;
  binding_count?: number;
}

/** One upstream value → tenant resolution key (``idp_org_bindings`` row). */
export interface OrgBinding {
  idp_alias: string;
  match_type: "claim" | "static" | "domain" | string;
  /**
   * The value matched against the upstream claim. Empty for ``static``, where
   * the *absence* of a value is what means "every user of this IdP".
   */
  match_value: string;
  org_id: string;
  requires_approval: boolean;
  note: string;
}

export interface OrgMembership {
  org_id: string;
  role_keys: string[];
  status: "active" | "pending" | "suspended" | string;
  source: "jit" | "manual" | "invite" | string;
}

/**
 * Row from ``GET /users`` — only the fields the access page needs.
 *
 * The endpoint returns a much wider user record; keeping the narrow shape here
 * documents exactly what this page depends on.
 */
export interface AdminUserRow {
  id: number;
  username: string;
  role: "admin" | "user" | string;
  display_name: string | null;
  email: string | null;
  disabled: boolean;
}

/**
 * Sentinel ``org_id`` meaning "applies to every tenant".
 *
 * A row with this scope is **added to** tenant-specific rows, never replaced by
 * them (the server unions `org_id IN ('*', orgId)`), so it is a platform-wide
 * grant. Single definition on purpose: a second literal would let the two
 * copies drift, and the whole point of this constant is the semantics it
 * encodes.
 */
export const ANY_ORG = "*";

/**
 * Who created a mapping row: an admin (or the seed file), or JIT provisioning.
 *
 * The distinction matters because a `'jit'` row was never reviewed by a human
 * — it exists only so a first-time user is not left with zero permissions.
 */
/**
 * Where a mapping row came from.
 *
 * - ``manual`` — an admin (or the seed file) wrote it.
 * - ``jit``    — a first login auto-created it; ``source_org`` says whose.
 * - ``legacy`` — the row predates provenance tracking, so it is unattributable.
 *   Kept separate instead of folding into ``manual`` on purpose: those rows
 *   include every auto-provisioned one written before the column existed.
 */
export type RoleMappingOrigin = "manual" | "jit" | "legacy";

export interface RoleMapping {
  org_id: string;
  job_code: string;
  role_key: string;
  origin?: RoleMappingOrigin | string;
  /**
   * Provenance only — **not** scope.
   *
   * A JIT row is written against `org_id === "*"`, so it applies to every
   * tenant; `source_org` names the tenant whose first login *created* it. Do
   * not read it as "this row belongs to that tenant".
   */
  source_org?: string | null;
  created_at?: number;
}

/** Response of ``PUT /admin/role-mappings`` — a role code and its packages. */
export interface RoleMappingSet {
  org_id: string;
  job_code: string;
  role_keys: string[];
}

export interface MembershipUpdateResult {
  username: string;
  org_id: string | null;
  role_keys: string[];
}

/** Resource families that carry an ``allowed_roles`` ACL. 044: the
 * ``mcp-services`` family retired — tenant MCP roles live in the tenant MCP
 * catalog page. */
export type ResourceKind =
  | "experts"
  | "connectors"
  | "agents"
  | "knowledge-bases";

/** Normalized ACL row — one shape for all four resource families. */
export interface ResourceAclRow {
  id: string;
  name: string;
  org_id: string | null;
  allowed_roles: string[];
  /**
   * Share flag of the underlying resource. ``undefined`` = the family has no
   * share concept (experts: publishing IS sharing). While ``false`` the ACL is
   * dormant — only the owner sees the resource regardless of ``allowed_roles``.
   */
  shared?: boolean;
}

/**
 * One resource family's ACL listing.
 *
 * ``aclExposed`` / ``orgExposed`` record whether the listing endpoint actually
 * serialized those fields. Some families (e.g. ``/agents``) omit them, so an
 * empty ``allowed_roles`` there means "not reported", NOT "unrestricted" —
 * the UI must not present the two as equivalent.
 */
export interface ResourceAclSnapshot {
  rows: ResourceAclRow[];
  aclExposed: boolean;
  orgExposed: boolean;
}

/** True when every row carries ``key`` (an empty list exposes nothing to lie about). */
function exposes<T extends object>(rows: T[], key: string): boolean {
  return rows.length === 0 || rows.every((row) => key in row);
}

/* Wire shapes of the four listing endpoints (field names differ per family). */
interface RawPublishedExpert {
  id: string;
  name: string;
  org_id?: string | null;
  allowed_roles?: string[] | null;
}

interface RawConnectorInstance {
  instance_id: string;
  display_name: string;
  org_id?: string | null;
  allowed_roles?: string[] | null;
  shared?: boolean;
}

interface RawAgent {
  agent_id: string;
  name: string;
  org_id?: string | null;
  allowed_roles?: string[] | null;
  is_shared?: boolean;
}

interface RawKnowledgeBase {
  knowledge_base_id: string;
  name: string;
  org_id?: string | null;
  allowed_roles?: string[] | null;
  shared?: boolean;
}

export const rolesApi = {
  list(): Promise<RolePackage[]> {
    return request<RolePackage[]>("/admin/roles", { method: "GET" });
  },
  /** Create a *platform* package (org_id is always NULL server-side). */
  create(body: {
    role_key: string;
    name_zh: string;
    description?: string;
    is_default?: boolean;
  }): Promise<RolePackage> {
    return request<RolePackage>("/admin/roles", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },
  /** Platform governance delete — works for tenant-owned rows too. */
  remove(roleKey: string): Promise<void> {
    return request<void>(`/admin/roles/${encodeURIComponent(roleKey)}`, {
      method: "DELETE",
    });
  },
  update(
    roleKey: string,
    body: { name_zh?: string; description?: string; is_default?: boolean },
  ): Promise<RolePackage> {
    return request<RolePackage>(`/admin/roles/${encodeURIComponent(roleKey)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
  },
  listOrgs(): Promise<OrgInfo[]> {
    return request<OrgInfo[]>("/admin/orgs", { method: "GET" });
  },
  /**
   * Create a tenant.
   *
   * Tenancy is Octop-owned — this plus {@link createOrgBinding} are the only
   * way a tenant becomes resolvable, since the chain fails closed on an org
   * that does not exist.
   */
  createOrg(body: {
    org_id: string;
    name: string;
    kind?: string;
    status?: string;
  }): Promise<OrgInfo> {
    return request<OrgInfo>("/admin/orgs", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },
  /** Patch a tenant. ``org_id`` is immutable — see {@link OrgsTab}. */
  updateOrg(
    orgId: string,
    body: { name?: string; kind?: string; status?: string },
  ): Promise<OrgInfo> {
    return request<OrgInfo>(`/admin/orgs/${encodeURIComponent(orgId)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
  },
  /**
   * Delete a tenant.
   *
   * ``force`` is required whenever the tenant still owns bindings or
   * memberships: both cascade, so the delete silently drops its resolution
   * keys and revokes every user's access inside it.
   */
  deleteOrg(orgId: string, force = false): Promise<void> {
    const qs = force ? "?force=true" : "";
    return request<void>(`/admin/orgs/${encodeURIComponent(orgId)}${qs}`, {
      method: "DELETE",
    });
  },
  /** Every resolution key, across all providers. */
  listOrgBindings(): Promise<OrgBinding[]> {
    return request<OrgBinding[]>("/admin/org-bindings", { method: "GET" });
  },
  createOrgBinding(body: {
    idp_alias: string;
    match_type: string;
    match_value?: string;
    org_id: string;
    requires_approval?: boolean;
    note?: string;
  }): Promise<OrgBinding> {
    return request<OrgBinding>("/admin/org-bindings", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },
  removeOrgBinding(binding: {
    idp_alias: string;
    match_type: string;
    /** Omitted for ``static``, whose value is the empty string. */
    match_value?: string;
  }): Promise<void> {
    const qs =
      `?idp_alias=${encodeURIComponent(binding.idp_alias)}` +
      `&match_type=${encodeURIComponent(binding.match_type)}` +
      `&match_value=${encodeURIComponent(binding.match_value ?? "")}`;
    return request<void>(`/admin/org-bindings${qs}`, { method: "DELETE" });
  },
  /** All local accounts (admins and disabled ones included). */
  listUsers(): Promise<AdminUserRow[]> {
    return request<AdminUserRow[]>("/users", { method: "GET" });
  },
  listMemberships(username: string): Promise<OrgMembership[]> {
    return request<OrgMembership[]>(
      `/admin/users/${encodeURIComponent(username)}/memberships`,
      { method: "GET" },
    );
  },
  setMembership(
    username: string,
    body: { org_id: string; role_keys: string[]; status?: string },
  ): Promise<MembershipUpdateResult> {
    return request<MembershipUpdateResult>(
      `/admin/users/${encodeURIComponent(username)}/membership`,
      { method: "PUT", body: JSON.stringify(body) },
    );
  },
  /**
   * List mapping rows.
   *
   * `orgId` means **effective for that tenant**, not "rows owned by it": the
   * server predicate is `org_id IN ('*', orgId)`, i.e. the tenant's own rows
   * plus the platform-wide `'*'` ones — the same two sets the resolver unions.
   * That is why the two are added together rather than the tenant row replacing
   * the global one. Omit it to get every row.
   */
  listMappings(orgId?: string): Promise<RoleMapping[]> {
    const qs = orgId ? `?org_id=${encodeURIComponent(orgId)}` : "";
    return request<RoleMapping[]>(`/admin/role-mappings${qs}`, {
      method: "GET",
    });
  },
  /**
   * Replace a role code's complete set of ability packages in one call.
   *
   * ``previous_*`` names the group being edited: sending it lets the server
   * drop the old role code / scope inside the same transaction, so renaming a
   * role code never leaves it momentarily granting nothing.
   */
  setMapping(body: {
    org_id: string;
    job_code: string;
    role_keys: string[];
    previous_org_id?: string;
    previous_job_code?: string;
  }): Promise<RoleMappingSet> {
    return request<RoleMappingSet>("/admin/role-mappings", {
      method: "PUT",
      body: JSON.stringify(body),
    });
  },
  addMapping(body: {
    org_id: string;
    job_code: string;
    role_key: string;
  }): Promise<RoleMapping> {
    return request<RoleMapping>("/admin/role-mappings", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },
  removeMapping(m: {
    org_id: string;
    job_code: string;
    role_key: string;
  }): Promise<void> {
    const qs = `?org_id=${encodeURIComponent(
      m.org_id,
    )}&job_code=${encodeURIComponent(m.job_code)}&role_key=${encodeURIComponent(
      m.role_key,
    )}`;
    return request<void>(`/admin/role-mappings${qs}`, { method: "DELETE" });
  },
  setResourceAcl(
    kind: ResourceKind,
    id: string,
    allowedRoles: string[],
    /**
     * 2026-09-27 attribution: when defined, the payload carries ``org_id``
     * and the server re-assigns the row — ``null`` = unattributed
     * (admin-only draft), ``"*"`` = explicit platform share, otherwise a
     * tenant id. ``undefined`` leaves the attribution untouched.
     */
    orgId?: string | null,
  ): Promise<{ allowed_roles: string[]; org_id?: string | null }> {
    const body: Record<string, unknown> = { allowed_roles: allowedRoles };
    if (orgId !== undefined) body.org_id = orgId;
    return request<{ allowed_roles: string[]; org_id?: string | null }>(
      `/admin/resource-acl/${kind}/${encodeURIComponent(id)}`,
      { method: "PUT", body: JSON.stringify(body) },
    );
  },
  /**
   * List resources of one family together with their ``allowed_roles`` ACL.
   *
   * Normalizes four different wire shapes into {@link ResourceAclRow} so the
   * ACL tab never has to branch on the resource type while rendering, and
   * reports which of the two ACL-ish fields the endpoint really returned.
   */
  async listResourceAcl(kind: ResourceKind): Promise<ResourceAclSnapshot> {
    if (kind === "experts") {
      const raw = await request<RawPublishedExpert[]>("/experts/published", {
        method: "GET",
      });
      return {
        rows: raw.map((row) => ({
          id: row.id,
          name: row.name,
          org_id: row.org_id ?? null,
          allowed_roles: row.allowed_roles ?? [],
        })),
        aclExposed: exposes(raw, "allowed_roles"),
        orgExposed: exposes(raw, "org_id"),
      };
    }
    if (kind === "connectors") {
      const raw = await request<RawConnectorInstance[]>(
        "/connector-instances",
        {
          method: "GET",
        },
      );
      return {
        rows: raw.map((row) => ({
          id: row.instance_id,
          name: row.display_name,
          org_id: row.org_id ?? null,
          allowed_roles: row.allowed_roles ?? [],
          shared: row.shared === true,
        })),
        aclExposed: exposes(raw, "allowed_roles"),
        orgExposed: exposes(raw, "org_id"),
      };
    }
    if (kind === "agents") {
      // scope=all: the ACL tab is an admin view — it must list every agent,
      // not just the caller's own plus shared ones (scope=mine default).
      const raw = await request<RawAgent[]>("/agents?scope=all", {
        method: "GET",
      });
      return {
        rows: raw.map((row) => ({
          id: row.agent_id,
          name: row.name,
          org_id: row.org_id ?? null,
          allowed_roles: row.allowed_roles ?? [],
          shared: row.is_shared === true,
        })),
        aclExposed: exposes(raw, "allowed_roles"),
        orgExposed: exposes(raw, "org_id"),
      };
    }
    const raw = await request<RawKnowledgeBase[]>("/knowledge-bases", {
      method: "GET",
    });
    return {
      rows: raw.map((row) => ({
        id: row.knowledge_base_id,
        name: row.name,
        org_id: row.org_id ?? null,
        allowed_roles: row.allowed_roles ?? [],
        shared: row.shared === true,
      })),
      aclExposed: exposes(raw, "allowed_roles"),
      orgExposed: exposes(raw, "org_id"),
    };
  },

  /**
   * Flip a resource's share flag from the ACL tab.
   *
   * Only families with an explicit share concept are accepted — experts have
   * none (publishing IS sharing), so the UI renders a dash there.
   */
  setShared(kind: ResourceKind, id: string, shared: boolean): Promise<unknown> {
    if (kind === "agents") {
      return request<unknown>(`/agents/${encodeURIComponent(id)}`, {
        method: "PATCH",
        body: JSON.stringify({ is_shared: shared }),
      });
    }
    if (kind === "connectors") {
      return request<unknown>(
        `/connector-instances/${encodeURIComponent(id)}`,
        { method: "PATCH", body: JSON.stringify({ shared }) },
      );
    }
    return request<unknown>(`/knowledge-bases/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ shared }),
    });
  },
};
