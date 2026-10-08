import {
  Avatar,
  Button,
  Input,
  Segmented,
  Skeleton,
  Table,
  Tag,
  Tooltip,
} from "antd";
import { KeyRound, RefreshCw, Search } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  rolesApi,
  type AdminUserRow,
  type OrgInfo,
  type OrgMembership,
  type RolePackage,
} from "../../../api/modules/roles";
import styles from "./Roles.module.less";
import UserGrantModal from "./UserGrantModal";
import {
  ListSkeleton,
  LoadError,
  RoleTagList,
  TableEmpty,
  useAsyncData,
  useRoleLabels,
  useTableArea,
} from "./shared";

/** Account-kind filter above the list. */
type TypeFilter = "all" | "user" | "admin";

const PAGE_SIZE_OPTIONS = ["10", "20", "50"];

interface ReferenceData {
  users: AdminUserRow[];
  orgs: OrgInfo[];
  roles: RolePackage[];
}

function isAdmin(user: AdminUserRow): boolean {
  return user.role === "admin";
}

function displayName(user: AdminUserRow): string {
  return user.display_name || user.username;
}

/**
 * Memberships of the users currently on screen.
 *
 * The membership endpoint is per-user, so a page of N users costs N requests.
 * We fetch lazily for the visible page only, cache successes, and leave
 * failures uncached so a later refresh retries them. A missing entry renders
 * as "unknown" (—) rather than "no packages", because "we could not read it"
 * and "the user holds nothing" are not the same claim.
 */
function useMembershipIndex(usernames: string[]) {
  const [index, setIndex] = useState<Record<string, OrgMembership[]>>({});
  const [pending, setPending] = useState<Record<string, boolean>>({});
  const requestedRef = useRef<Set<string>>(new Set());
  const [token, setToken] = useState(0);

  /** ``usernames`` is a fresh array each render — keying the effect on the
      joined signature keeps it from re-running unless the set really changed. */
  const signature = `${token}#${usernames.join("|")}`;

  useEffect(() => {
    const todo = usernames.filter((name) => !requestedRef.current.has(name));
    if (todo.length === 0) return;
    todo.forEach((name) => requestedRef.current.add(name));
    setPending((prev) => {
      const next = { ...prev };
      todo.forEach((name) => {
        next[name] = true;
      });
      return next;
    });

    void Promise.all(
      todo.map(async (name) => {
        try {
          return [name, await rolesApi.listMemberships(name)] as const;
        } catch {
          // Not cached: keep the cell "unknown" and retry on next refresh.
          requestedRef.current.delete(name);
          return null;
        }
      }),
    ).then((results) => {
      const ok = results.filter(
        (row): row is readonly [string, OrgMembership[]] => row !== null,
      );
      if (ok.length > 0) {
        setIndex((prev) => {
          const next = { ...prev };
          ok.forEach(([name, rows]) => {
            next[name] = rows;
          });
          return next;
        });
      }
      setPending((prev) => {
        const next = { ...prev };
        todo.forEach((name) => {
          delete next[name];
        });
        return next;
      });
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature]);

  /** Dropped one user from the cache and refetch it (after a write). */
  const invalidate = useCallback((username: string) => {
    requestedRef.current.delete(username);
    setIndex((prev) => {
      const next = { ...prev };
      delete next[username];
      return next;
    });
    setToken((value) => value + 1);
  }, []);

  /** Drop the whole cache and refetch the visible page. */
  const refresh = useCallback(() => {
    requestedRef.current.clear();
    setIndex({});
    setPending({});
    setToken((value) => value + 1);
  }, []);

  return { index, pending, invalidate, refresh };
}

export default function MembershipTab() {
  const { t } = useTranslation();
  const [keyword, setKeyword] = useState("");
  const [typeFilter, setTypeFilter] = useState<TypeFilter>("all");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  const [grantUser, setGrantUser] = useState<AdminUserRow | null>(null);

  const refs = useAsyncData<ReferenceData>(
    async () => {
      const [users, orgs, roles] = await Promise.all([
        rolesApi.listUsers(),
        rolesApi.listOrgs(),
        rolesApi.list(),
      ]);
      return { users, orgs, roles };
    },
    { fallbackError: t("adminRoles.membership.loadRefsFailed"), t },
  );

  const users = useMemo(() => refs.data?.users ?? [], [refs.data]);
  const orgs = useMemo(() => refs.data?.orgs ?? [], [refs.data]);
  const roles = useMemo(() => refs.data?.roles ?? [], [refs.data]);
  const roleLabels = useRoleLabels(roles);

  const filtered = useMemo(() => {
    const needle = keyword.trim().toLowerCase();
    return users.filter((user) => {
      if (typeFilter === "admin" && !isAdmin(user)) return false;
      if (typeFilter === "user" && isAdmin(user)) return false;
      if (!needle) return true;
      return [user.username, user.display_name ?? "", user.email ?? ""]
        .join(" ")
        .toLowerCase()
        .includes(needle);
    });
  }, [typeFilter, keyword, users]);

  /** A new filter starts from page 1; a shrinking list cannot strand us. */
  useEffect(() => {
    setPage(1);
  }, [keyword, typeFilter]);

  useEffect(() => {
    if (page > 1 && (page - 1) * pageSize >= filtered.length) setPage(1);
  }, [filtered.length, page, pageSize]);

  /** Only the rows actually rendered are looked up (one request per user). */
  const visibleUsernames = useMemo(
    () =>
      filtered
        .slice((page - 1) * pageSize, page * pageSize)
        .map((user) => user.username),
    [filtered, page, pageSize],
  );

  const { index, pending, invalidate, refresh } =
    useMembershipIndex(visibleUsernames);

  const { areaRef, scrollY } = useTableArea();

  const counts = useMemo(
    () => ({
      total: users.length,
      grantable: users.filter((user) => !isAdmin(user)).length,
    }),
    [users],
  );

  const refreshAll = useCallback(() => {
    refs.reload();
    refresh();
  }, [refresh, refs]);

  const firstLoad = refs.loading && refs.data === null;

  /** Distinct ability packages a user holds across every tenant. */
  const grantedKeys = useCallback(
    (username: string): string[] | null => {
      const rows = index[username];
      if (!rows) return null;
      const seen: string[] = [];
      for (const row of rows) {
        for (const key of row.role_keys) {
          if (!seen.includes(key)) seen.push(key);
        }
      }
      return seen;
    },
    [index],
  );

  /** Tenants that actually grant something — empty memberships are not grants. */
  const grantingOrgs = useCallback(
    (username: string): number =>
      (index[username] ?? []).filter((row) => row.role_keys.length > 0).length,
    [index],
  );

  return (
    <div className={styles.panel}>
      {refs.error ? (
        <LoadError message={refs.error} onRetry={refreshAll} />
      ) : firstLoad ? (
        <ListSkeleton rows={6} />
      ) : (
        <>
          <div className={styles.toolbar}>
            <Input
              allowClear
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
              placeholder={t("adminRoles.membership.searchPlaceholder")}
              prefix={<Search size={14} />}
              style={{ maxWidth: 300 }}
            />
            <Segmented
              size="middle"
              value={typeFilter}
              onChange={(value) => setTypeFilter(value as TypeFilter)}
              options={[
                {
                  value: "all",
                  label: t("adminRoles.membership.filterAll"),
                },
                {
                  value: "user",
                  label: t("adminRoles.membership.accountUser"),
                },
                {
                  value: "admin",
                  label: t("adminRoles.membership.accountAdmin"),
                },
              ]}
            />
            <span className={styles.cellSecondary}>
              {t("adminRoles.membership.summary", {
                users: counts.total,
                grantable: counts.grantable,
                orgs: orgs.length,
                roles: roles.length,
              })}
            </span>
            <div className={styles.toolbarEnd}>
              <Tooltip title={t("adminRoles.membership.refreshHint")}>
                <Button
                  icon={<RefreshCw size={14} />}
                  onClick={refreshAll}
                  loading={refs.loading && !firstLoad}
                />
              </Tooltip>
            </div>
          </div>

          <div className={styles.tableArea} ref={areaRef}>
            <Table<AdminUserRow>
              rowKey="username"
              size="small"
              loading={refs.loading}
              dataSource={filtered}
              scroll={
                scrollY === undefined ? { x: 1060 } : { x: 1060, y: scrollY }
              }
              pagination={{
                current: page,
                pageSize,
                total: filtered.length,
                showSizeChanger: true,
                pageSizeOptions: PAGE_SIZE_OPTIONS,
                showTotal: (total) =>
                  t("adminRoles.totalRows", { count: total }),
                onChange: (nextPage, nextSize) => {
                  setPage(nextPage);
                  setPageSize(nextSize);
                },
              }}
              locale={{
                emptyText: (
                  <TableEmpty
                    filtered={keyword.trim().length > 0 || typeFilter !== "all"}
                    onClear={() => {
                      setKeyword("");
                      setTypeFilter("all");
                    }}
                  />
                ),
              }}
              columns={[
                {
                  title: t("adminRoles.membership.colUser"),
                  dataIndex: "username",
                  width: 240,
                  render: (_, record) => (
                    <div className={styles.userCell}>
                      <Avatar size={28}>
                        {displayName(record).slice(0, 1).toUpperCase()}
                      </Avatar>
                      <div className={styles.cellStack}>
                        <span className={styles.cellPrimary}>
                          {displayName(record)}
                        </span>
                        <span className={styles.mono}>{record.username}</span>
                      </div>
                    </div>
                  ),
                },
                {
                  title: t("adminRoles.membership.colEmail"),
                  dataIndex: "email",
                  width: 200,
                  ellipsis: true,
                  render: (email: string | null) =>
                    email ? (
                      <span>{email}</span>
                    ) : (
                      <span className={styles.cellSecondary}>—</span>
                    ),
                },
                {
                  title: t("adminRoles.membership.colAccountType"),
                  dataIndex: "role",
                  width: 104,
                  render: (_, record) =>
                    isAdmin(record) ? (
                      <Tag color="gold">
                        {t("adminRoles.membership.accountAdmin")}
                      </Tag>
                    ) : (
                      <Tag>{t("adminRoles.membership.accountUser")}</Tag>
                    ),
                },
                {
                  title: t("adminRoles.membership.colAccountStatus"),
                  dataIndex: "disabled",
                  width: 104,
                  render: (disabled: boolean) =>
                    disabled ? (
                      <Tag color="red">
                        {t("adminRoles.membership.accountDisabled")}
                      </Tag>
                    ) : (
                      <Tag color="green">
                        {t("adminRoles.membership.accountEnabled")}
                      </Tag>
                    ),
                },
                {
                  title: t("adminRoles.membership.colGranted"),
                  key: "granted",
                  render: (_, record) => {
                    if (pending[record.username]) {
                      return (
                        <Skeleton.Button
                          active
                          size="small"
                          style={{ width: 140, height: 22 }}
                        />
                      );
                    }
                    const keys = grantedKeys(record.username);
                    if (keys === null) {
                      return (
                        <Tooltip
                          title={t("adminRoles.membership.summaryUnknown")}
                        >
                          <span className={styles.cellSecondary}>—</span>
                        </Tooltip>
                      );
                    }
                    const orgCount = grantingOrgs(record.username);
                    return (
                      <div className={styles.cellStack}>
                        {keys.length > 0 ? (
                          <RoleTagList roleKeys={keys} labels={roleLabels} />
                        ) : (
                          <Tag>{t("adminRoles.membership.noGrant")}</Tag>
                        )}
                        {orgCount > 0 && (
                          <span className={styles.cellSecondary}>
                            {t("adminRoles.membership.orgCount", {
                              count: orgCount,
                            })}
                          </span>
                        )}
                      </div>
                    );
                  },
                },
                {
                  title: t("adminRoles.actions"),
                  key: "actions",
                  width: 108,
                  align: "right",
                  render: (_, record) =>
                    // Admins bypass tenant filtering entirely, so a grant
                    // button would be a no-op; the cell stays empty rather than
                    // carrying a tag that only restates the account type.
                    isAdmin(record) ? null : (
                      <Button
                        size="small"
                        type="link"
                        icon={<KeyRound size={14} />}
                        onClick={() => setGrantUser(record)}
                      >
                        {t("adminRoles.membership.grant")}
                      </Button>
                    ),
                },
              ]}
            />
          </div>
        </>
      )}

      <UserGrantModal
        user={grantUser}
        orgs={orgs}
        roles={roles}
        roleLabels={roleLabels}
        onClose={() => setGrantUser(null)}
        onSaved={invalidate}
      />
    </div>
  );
}
