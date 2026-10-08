import { request } from "../request";

/**
 * One SSO-provisioned user still waiting for a tenant (T5-4).
 *
 * Derived server-side from SSO users whose ``default_org_id`` is NULL —
 * there is no dedicated table, so the list is always live.
 */
export interface PendingUser {
  id: number;
  username: string;
  display_name: string;
  email: string | null;
  /** IdP alias for display (falls back to the provider id). */
  idp_alias: string;
  created_at: number;
}

export interface AssignResult {
  user_id: number;
  username: string;
  org_id: string;
  role_keys: string[];
  /** True when this call claimed the user's still-NULL ``default_org_id``. */
  default_org_claimed: boolean;
}

export const pendingUsersApi = {
  list(): Promise<PendingUser[]> {
    return request<PendingUser[]>("/admin/pending-users", { method: "GET" });
  },
  /**
   * Drop a pending user into a tenant: active manual membership +
   * ``default_org_id`` claim + epoch bump, so their next login lands via
   * resolution level ③ (existing membership).
   */
  assign(userId: number, body: { org_id: string; role_keys?: string[] }): Promise<AssignResult> {
    return request<AssignResult>(`/admin/pending-users/${userId}/assign`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  },
};
