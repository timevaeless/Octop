import {
  Alert,
  Button,
  Input,
  Segmented,
  Select,
  Switch,
  Table,
  Tag,
  Tooltip,
} from "antd";
import { RefreshCw, Save, Search, Undo2 } from "lucide-react";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { orgAdminApi } from "../../../api/modules/orgAdmin";
import {
  rolesApi,
  type ResourceAclRow,
  type ResourceKind,
  type RolePackage,
} from "../../../api/modules/roles";
import { message } from "../../../utils/antdMessage";
import { apiErrorMessage } from "../../../utils/apiError";
import styles from "./Roles.module.less";
import {
  ListSkeleton,
  LoadError,
  RoleTagList,
  TableEmpty,
  isPlatformSystemPackage,
  roleOptionLabel,
  useAsyncData,
  useOrgRoleRefs,
  useRoleLabels,
  useTableArea,
  useTablePagination,
} from "./shared";

/** Latest rows plus the resource family they belong to (guards stale renders). */
interface AclSnapshot {
  kind: ResourceKind;
  rows: ResourceAclRow[];
  aclExposed: boolean;
  orgExposed: boolean;
}

const RESOURCE_KINDS: ResourceKind[] = [
  "experts",
  "connectors",
  "agents",
  "knowledge-bases",
];

/** Order-insensitive comparison of two role-key lists. */
function sameSelection(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  const left = [...a].sort();
  const right = [...b].sort();
  return left.every((key, index) => key === right[index]);
}

/**
 * 2026-09-27 three-state attribution (editable in this tab): ``null`` =
 * unattributed (admin-only draft), ``"*"`` = explicit platform share,
 * otherwise a tenant id. The Select cannot hold ``null`` as a value, so the
 * unassigned option carries this sentinel and is mapped back on save.
 */
const UNASSIGNED = "__unattributed__";

type OrgDraft = string | null;

/**
 * @param tenantOrgId when set, the tab renders the **tenant face**: only rows
 *   attributed to this tenant are listed (server-side filter), the attribution
 *   editor disappears (attribution is platform-only), and saves go through
 *   ``/org/{org}/resource-acl`` which accepts ``allowed_roles`` alone.
 * @param reloadKey bumped by the packages tab after a write — refetches the
 *   role pool so a just-created package appears in the pickers immediately.
 */
export default function ResourceAclTab({
  tenantOrgId,
  reloadKey = 0,
}: {
  tenantOrgId?: string;
  reloadKey?: number;
}) {
  const { t } = useTranslation();
  const [kind, setKind] = useState<ResourceKind>("experts");
  const [keyword, setKeyword] = useState("");
  const [drafts, setDrafts] = useState<Record<string, string[]>>({});
  const [orgDrafts, setOrgDrafts] = useState<Record<string, OrgDraft>>({});
  const [savingIds, setSavingIds] = useState<Record<string, boolean>>({});
  const [sharingIds, setSharingIds] = useState<Record<string, boolean>>({});
  /** Rows whose stored ACL we know — either reported by the API or just written. */
  const [knownIds, setKnownIds] = useState<Record<string, boolean>>({});
  const pagination = useTablePagination(10);
  const { areaRef, scrollY } = useTableArea();

  const refs = useOrgRoleRefs(!tenantOrgId, reloadKey);
  const rolesKey = `${tenantOrgId ?? ""}:${reloadKey}`;
  const tenantRolesRef = useAsyncData<RolePackage[]>(
    () =>
      tenantOrgId
        ? orgAdminApi.listRoles(tenantOrgId).then((rows) =>
            rows.map((r) => ({
              role_key: r.role_key,
              name_zh: r.name_zh,
              org_id: r.org_id,
              is_default: r.is_default,
              is_system: r.is_system,
              description: r.description,
            })),
          )
        : Promise.resolve([]),
    { fallbackError: t("adminRoles.loadFailed"), t, reloadKey: rolesKey },
  );
  const roles = useMemo(
    () =>
      tenantOrgId
        ? (tenantRolesRef.data ?? [])
        : (refs.data?.roles ?? []),
    [refs.data, tenantOrgId, tenantRolesRef.data],
  );
  const roleLabels = useRoleLabels(roles);
  const orgs = useMemo(() => refs.data?.orgs ?? [], [refs.data]);

  const { data, loading, error, reload, setData } = useAsyncData<AclSnapshot>(
    async () => {
      if (tenantOrgId) {
        const rows = await orgAdminApi.listResourceAcl(tenantOrgId, kind);
        return {
          kind,
          rows: rows.map((row) => ({
            id: row.id,
            name: row.name,
            org_id: tenantOrgId,
            allowed_roles: row.allowed_roles,
          })),
          aclExposed: true,
          orgExposed: true,
        };
      }
      const snapshot = await rolesApi.listResourceAcl(kind);
      return { kind, ...snapshot };
    },
    {
      fallbackError: t("adminRoles.acl.loadFailed"),
      t,
      reloadKey: `${tenantOrgId ?? ""}|${kind}|${reloadKey}`,
    },
  );

  /** Never render another family's rows while a switch is in flight. */
  const rows = data?.kind === kind ? data.rows : null;
  /** True when the listing endpoint honestly reports ``allowed_roles``. */
  const aclExposed = data?.kind === kind ? data.aclExposed : true;
  const orgExposed = data?.kind === kind ? data.orgExposed : true;

  /** Drop unsaved drafts when the resource family changes. */
  useEffect(() => {
    setDrafts({});
    setOrgDrafts({});
    setSavingIds({});
    setKnownIds({});
    setSharingIds({});
  }, [kind]);

  const kindLabel = useCallback(
    (value: ResourceKind) => t(`adminRoles.acl.${kindKey(value)}`),
    [t],
  );

  /** Attribution options: the two literals plus every known tenant. */
  const orgOptions = useMemo(
    () => [
      { value: UNASSIGNED, label: t("adminRoles.acl.unassigned") },
      { value: "*", label: t("adminRoles.acl.platformWide") },
      ...orgs.map((org) => ({
        value: org.org_id,
        label: `${org.name} (${org.org_id})`,
      })),
    ],
    [orgs, t],
  );

  /** Effective attribution value (draft wins over the stored row). */
  const orgDraftOf = useCallback(
    (row: ResourceAclRow): string =>
      orgDrafts[row.id] !== undefined
        ? (orgDrafts[row.id] ?? UNASSIGNED)
        : (row.org_id ?? UNASSIGNED),
    [orgDrafts],
  );

  const isOrgDirty = useCallback(
    (row: ResourceAclRow) => {
      const draft = orgDrafts[row.id];
      return draft !== undefined && (draft ?? null) !== (row.org_id ?? null);
    },
    [orgDrafts],
  );

  const setOrgDraft = useCallback((id: string, next: OrgDraft) => {
    setOrgDrafts((prev) => ({ ...prev, [id]: next }));
  }, []);

  const discardOrgDraft = useCallback((id: string) => {
    setOrgDrafts((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }, []);

  // Tenant face: platform-owned system packages (member/org_admin) are not
  // the tenant's to hand out — hidden from the picker. Labels keep the full
  // pool so stored rows render display names.
  const roleOptions = useMemo(
    () =>
      roles
        .filter((role) => !tenantOrgId || !isPlatformSystemPackage(role))
        .map((role) => ({
          value: role.role_key,
          label: roleOptionLabel(role),
        })),
    [roles, tenantOrgId],
  );

  const filtered = useMemo(() => {
    const list = rows ?? [];
    const needle = keyword.trim().toLowerCase();
    if (!needle) return list;
    return list.filter((row) =>
      [row.name, row.id, row.org_id ?? "", ...row.allowed_roles]
        .join(" ")
        .toLowerCase()
        .includes(needle),
    );
  }, [keyword, rows]);

  const stats = useMemo(() => {
    const list = rows ?? [];
    return {
      total: list.length,
      restricted: list.filter((row) => row.allowed_roles.length > 0).length,
    };
  }, [rows]);

  const draftOf = useCallback(
    (row: ResourceAclRow) => drafts[row.id] ?? row.allowed_roles,
    [drafts],
  );

  const isDirty = useCallback(
    (row: ResourceAclRow) => {
      const draft = drafts[row.id];
      return draft !== undefined && !sameSelection(draft, row.allowed_roles);
    },
    [drafts],
  );

  const setDraft = useCallback((id: string, next: string[]) => {
    setDrafts((prev) => ({ ...prev, [id]: next }));
  }, []);

  const discardDraft = useCallback((id: string) => {
    setDrafts((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }, []);

  const saveRow = useCallback(
    async (row: ResourceAclRow) => {
      const next = drafts[row.id] ?? row.allowed_roles;
      setSavingIds((prev) => ({ ...prev, [row.id]: true }));
      try {
        let stored = next;
        let storedOrg: string | null | undefined = row.org_id;
        if (tenantOrgId) {
          // Tenant face: allowed_roles only — attribution is not expressible.
          await orgAdminApi.setResourceAcl(tenantOrgId, kind, row.id, next);
        } else {
          // Attribution rides along only when the admin actually changed it —
          // ``undefined`` keeps the payload free of ``org_id`` (server no-op).
          const orgNext: string | null | undefined = isOrgDirty(row)
            ? (orgDrafts[row.id] ?? null)
            : undefined;
          const result = await rolesApi.setResourceAcl(
            kind,
            row.id,
            next,
            orgNext,
          );
          stored = result?.allowed_roles ?? next;
          storedOrg =
            orgNext !== undefined
              ? result?.org_id !== undefined
                ? result.org_id
                : orgNext
              : row.org_id;
        }
        setData((prev) =>
          prev
            ? {
                ...prev,
                rows: prev.rows.map((item) =>
                  item.id === row.id
                    ? { ...item, allowed_roles: stored, org_id: storedOrg }
                    : item,
                ),
              }
            : prev,
        );
        discardDraft(row.id);
        discardOrgDraft(row.id);
        setKnownIds((prev) => ({ ...prev, [row.id]: true }));
        message.success(t("adminRoles.acl.saveSuccess", { name: row.name }));
      } catch (err) {
        // Roll the editor back to the persisted value and say so explicitly.
        discardDraft(row.id);
        discardOrgDraft(row.id);
        message.error(apiErrorMessage(err, t("adminRoles.acl.saveFailed"), t));
        message.warning(t("adminRoles.acl.rolledBack"), 3);
      } finally {
        setSavingIds((prev) => {
          const nextState = { ...prev };
          delete nextState[row.id];
          return nextState;
        });
      }
    },
    [
      discardDraft,
      discardOrgDraft,
      drafts,
      isOrgDirty,
      kind,
      orgDrafts,
      setData,
      t,
      tenantOrgId,
    ],
  );

  /**
   * Flip the share flag inline. Optimistic: apply immediately, roll back on
   * failure. While ``shared`` is false the ACL is dormant — the owner is the
   * only viewer — so the toggle is the missing "make this ACL real" lever.
   */
  const toggleShare = useCallback(
    async (row: ResourceAclRow, next: boolean) => {
      const apply = (shared: boolean | undefined) =>
        setData((prev) =>
          prev
            ? {
                ...prev,
                rows: prev.rows.map((item) =>
                  item.id === row.id ? { ...item, shared } : item,
                ),
              }
            : prev,
        );
      setSharingIds((prev) => ({ ...prev, [row.id]: true }));
      apply(next);
      try {
        await rolesApi.setShared(kind, row.id, next);
        message.success(t("adminRoles.acl.shareUpdated", { name: row.name }));
      } catch (err) {
        apply(row.shared);
        message.error(
          apiErrorMessage(err, t("adminRoles.acl.shareUpdateFailed"), t),
        );
      } finally {
        setSharingIds((prev) => {
          const nextState = { ...prev };
          delete nextState[row.id];
          return nextState;
        });
      }
    },
    [kind, setData, t],
  );

  const firstLoad = rows === null;

  return (
    <div className={styles.panel}>
      {/* Resource family stays switchable even while a load is failing. */}
      <div className={styles.toolbar}>
        <Segmented
          value={kind}
          onChange={(value) => setKind(value as ResourceKind)}
          // 044: the mcp-services family retired — tenant MCP role config
          // lives in the tenant MCP catalog page for both faces.
          options={RESOURCE_KINDS.map(
            (item) => ({
              value: item,
              label: kindLabel(item),
            }),
          )}
        />
        <div className={styles.toolbarEnd}>
          <Button
            icon={<RefreshCw size={14} />}
            onClick={reload}
            loading={loading && !firstLoad}
          />
        </div>
      </div>

      {error ? (
        <LoadError message={error} onRetry={reload} />
      ) : firstLoad ? (
        <ListSkeleton rows={6} />
      ) : (
        <>
          <Alert
            type="info"
            showIcon
            message={t("adminRoles.acl.scopeHintTitle")}
            description={t("adminRoles.acl.scopeHint", {
              kind: kindLabel(kind),
            })}
          />

          {!aclExposed && (
            <Alert
              type="warning"
              showIcon
              message={t("adminRoles.acl.notReportedTitle")}
              description={t("adminRoles.acl.notReported", {
                kind: kindLabel(kind),
              })}
            />
          )}

          <div className={styles.toolbar}>
            <Input
              allowClear
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
              placeholder={t("adminRoles.acl.searchPlaceholder")}
              prefix={<Search size={14} />}
              style={{ maxWidth: 300 }}
            />
            <span className={styles.cellSecondary}>
              {t("adminRoles.acl.summary", {
                total: stats.total,
                // Unknowable when the endpoint does not report the ACL — an
                // explicit "—" beats a misleading 0.
                restricted: aclExposed ? stats.restricted : "—",
              })}
            </span>
          </div>

          <div className={styles.tableArea} ref={areaRef}>
            <Table<ResourceAclRow>
              rowKey="id"
              size="small"
              loading={loading}
              dataSource={filtered}
              pagination={pagination}
              scroll={
                scrollY === undefined ? { x: 1200 } : { x: 1200, y: scrollY }
              }
              locale={{
                emptyText: (
                  <TableEmpty
                    filtered={keyword.trim().length > 0}
                    onClear={() => setKeyword("")}
                  />
                ),
              }}
              columns={[
                {
                  title: t("adminRoles.acl.resource"),
                  dataIndex: "name",
                  render: (name: string, record: ResourceAclRow) => (
                    <div className={styles.cellStack}>
                      <span>{name}</span>
                      <Tooltip title={record.id}>
                        <span className={styles.mono}>{record.id}</span>
                      </Tooltip>
                    </div>
                  ),
                },
                ...(tenantOrgId
                  ? []
                  : [
                      {
                        title: t("adminRoles.acl.scopeColumn"),
                        dataIndex: "org_id",
                        width: 200,
                        render: (_: unknown, record: ResourceAclRow) => {
                          // 2026-09-27: the scope column IS the attribution
                          // editor — unattributed (admin draft) / platform
                          // share / tenant. Tenant face never sees it.
                          if (!orgExposed && !knownIds[record.id]) {
                            return (
                              <Tooltip title={t("adminRoles.acl.unknownHint")}>
                                <Tag>{t("adminRoles.acl.unknown")}</Tag>
                              </Tooltip>
                            );
                          }
                          return (
                            <Select
                              size="small"
                              showSearch
                              optionFilterProp="label"
                              style={{ width: "100%" }}
                              value={orgDraftOf(record)}
                              options={orgOptions}
                              onChange={(value: string) =>
                                setOrgDraft(
                                  record.id,
                                  value === UNASSIGNED ? null : value,
                                )
                              }
                            />
                          );
                        },
                      } satisfies ColumnsType<ResourceAclRow>[number],
                    ]),
                {
                  title: t("adminRoles.acl.sharedColumn"),
                  key: "shared",
                  width: 90,
                  render: (_, record) =>
                    // No share lever on the tenant face at all: the toggles
                    // behind it are platform-admin endpoints, and the tenant
                    // loader does not even carry the ``shared`` field —
                    // rendering a Switch here meant a guaranteed 403 toast.
                    tenantOrgId !== undefined ? (
                      <span className={styles.cellSecondary}>—</span>
                    ) : kind === "experts" ? (
                      <Tooltip title={t("adminRoles.acl.publishIsShared")}>
                        <span className={styles.cellSecondary}>—</span>
                      </Tooltip>
                    ) : (
                      <Tooltip
                        title={
                          record.shared
                            ? t("adminRoles.acl.sharedOnHint")
                            : t("adminRoles.acl.sharedOffHint")
                        }
                      >
                        <Switch
                          size="small"
                          checked={record.shared === true}
                          loading={sharingIds[record.id] === true}
                          onChange={(checked) =>
                            void toggleShare(record, checked)
                          }
                        />
                      </Tooltip>
                    ),
                },
                {
                  title: t("adminRoles.acl.allowedRoles"),
                  dataIndex: "allowed_roles",
                  width: 260,
                  render: (keys: string[], record) =>
                    aclExposed || knownIds[record.id] ? (
                      <RoleTagList roleKeys={keys} labels={roleLabels} />
                    ) : (
                      <Tooltip title={t("adminRoles.acl.unknownHint")}>
                        <Tag>{t("adminRoles.acl.unknown")}</Tag>
                      </Tooltip>
                    ),
                },
                {
                  title: t("adminRoles.acl.adjust"),
                  key: "adjust",
                  width: 420,
                  render: (_, record) => {
                    // Multi-select edits are staged locally instead of being PUT
                    // on every keystroke: one explicit request per intent, no
                    // debounce timer, and a trivial rollback path (drop draft).
                    // The attribution editor (scope column) shares the same
                    // staged-draft + explicit-save pattern, so its dirtiness
                    // participates in the save button below.
                    const dirty = isDirty(record) || isOrgDirty(record);
                    const known = aclExposed || knownIds[record.id] === true;
                    return (
                      <div className={styles.draftEditor}>
                        <Select
                          mode="multiple"
                          allowClear
                          showSearch
                          maxTagCount="responsive"
                          optionFilterProp="label"
                          style={{ minWidth: 220, flex: 1 }}
                          placeholder={
                            known
                              ? t("adminRoles.acl.unrestricted")
                              : t("adminRoles.acl.pickRoles")
                          }
                          value={draftOf(record)}
                          options={roleOptions}
                          onChange={(values: string[]) =>
                            setDraft(record.id, values)
                          }
                        />
                        {dirty && (
                          <>
                            <Tag color="orange">
                              {t("adminRoles.acl.unsaved")}
                            </Tag>
                            <Button
                              type="primary"
                              size="small"
                              icon={<Save size={13} />}
                              loading={savingIds[record.id] === true}
                              onClick={() => void saveRow(record)}
                            >
                              {t("adminRoles.save")}
                            </Button>
                            <Button
                              size="small"
                              icon={<Undo2 size={13} />}
                              onClick={() => {
                                discardDraft(record.id);
                                discardOrgDraft(record.id);
                              }}
                            >
                              {t("adminRoles.acl.discard")}
                            </Button>
                          </>
                        )}
                      </div>
                    );
                  },
                },
              ]}
            />
          </div>
        </>
      )}
    </div>
  );
}

/** ``knowledge-bases`` → ``knowledgeBases`` for the ``acl.*`` i18n keys. */
function kindKey(kind: ResourceKind): string {
  if (kind === "knowledge-bases") return "knowledgeBases";
  if (kind === "experts") return "experts";
  if (kind === "connectors") return "connectors";
  return "agents";
}
