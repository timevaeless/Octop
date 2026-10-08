import { request } from "../request";

/**
 * One member of a tenant (T4-2) — the joined read behind
 * ``GET /api/org/{org_id}/members``: the membership row stays the authority
 * for roles/status/source, ``users`` contributes display fields and the
 * account-level ``disabled`` flag (account suspension lives there, since the
 * JIT re-expansion would resurrect a merely-suspended membership on the next
 * login).
 */
export interface OrgMember {
  user_id: number;
  username: string;
  display_name: string | null;
  email: string | null;
  /** "user" | "admin" — platform admins are outside tenant purview. */
  user_role: string;
  /** Account-level flag; suspend/reactivate flip this, not the membership. */
  disabled: boolean;
  /** True when this org is the member's ``default_org_id``. */
  is_default_org: boolean;
  role_keys: string[];
  status: "active" | "pending" | "suspended" | string;
  source: "jit" | "manual" | "invite" | string;
  created_at: number;
  updated_at: number;
}

export interface SetRolesResult {
  user_id: number;
  org_id: string;
  role_keys: string[];
}

export interface SuspendResult {
  user_id: number;
  username: string;
  disabled: boolean;
}

/** Reference data for the roster faces (028): this org + grantable packages. */
export interface OrgRefs {
  org: { org_id: string; name: string; status: string };
  /** Grantable = platform non-system rows plus the tenant's own. */
  roles: {
    role_key: string;
    name_zh: string;
    org_id: string | null;
    is_default: boolean;
    is_system?: boolean;
    description: string;
  }[];
}

export const orgMembersApi = {
  list(orgId: string): Promise<OrgMember[]> {
    return request<OrgMember[]>(`/org/${encodeURIComponent(orgId)}/members`, {
      method: "GET",
    });
  },
  /**
   * Per-tenant reference data — the org_admin-accessible counterpart of the
   * platform-admin-only ``GET /api/roles`` + ``GET /api/orgs`` pair.
   */
  refs(orgId: string): Promise<OrgRefs> {
    return request<OrgRefs>(`/org/${encodeURIComponent(orgId)}/refs`, {
      method: "GET",
    });
  },
  /** Grant/revoke ability packages wholesale (tenant-scope keys only). */
  setRoles(orgId: string, userId: number, roleKeys: string[]): Promise<SetRolesResult> {
    return request<SetRolesResult>(`/org/${encodeURIComponent(orgId)}/members/${userId}/roles`, {
      method: "PUT",
      body: JSON.stringify({ role_keys: roleKeys }),
    });
  },
  suspend(orgId: string, userId: number): Promise<SuspendResult> {
    return request<SuspendResult>(`/org/${encodeURIComponent(orgId)}/members/${userId}/suspend`, {
      method: "POST",
    });
  },
  reactivate(orgId: string, userId: number): Promise<SuspendResult> {
    return request<SuspendResult>(
      `/org/${encodeURIComponent(orgId)}/members/${userId}/reactivate`,
      { method: "POST" },
    );
  },
};
