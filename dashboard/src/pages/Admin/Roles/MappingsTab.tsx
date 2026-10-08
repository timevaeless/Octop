import {
  Alert,
  Button,
  Form,
  Input,
  Modal,
  Select,
  Table,
  Tag,
  Tooltip,
} from "antd";
import {
  Check,
  History,
  Link2,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Sparkles,
  Trash2,
} from "lucide-react";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { orgAdminApi, type TenantRole } from "../../../api/modules/orgAdmin";
import {
  ANY_ORG,
  rolesApi,
  type OrgInfo,
  type RoleMapping,
  type RolePackage,
} from "../../../api/modules/roles";
import { message } from "../../../utils/antdMessage";
import { apiErrorMessage } from "../../../utils/apiError";
import { showConfirmModal } from "../../../utils/confirmModal";
import styles from "./Roles.module.less";
import {
  RoleTagList,
  ListSkeleton,
  LoadError,
  TableEmpty,
  isPlatformSystemPackage,
  roleOptionLabel,
  useAsyncData,
  useOrgRoleRefs,
  useRoleLabels,
  useTableArea,
  useTablePagination,
} from "./shared";

/** Mirrors the backend's role-code pattern for auto-provisioned codes. */
const ROLE_CODE_PATTERN = /^[A-Za-z0-9_.-]+$/;
const ROLE_CODE_MAX = 64;

interface MappingFormValues {
  org_id: string;
  job_code: string;
  role_keys: string[];
}

/**
 * One role code and every ability package it grants.
 *
 * The API stores one row per (scope, role code, package) triple, but an admin
 * thinks in *role codes*: "what does Finance Manager get?". Grouping the
 * triples here means the page can edit a role code as a unit, and a rename is
 * a single atomic write rather than N deletes plus N inserts.
 *
 * ``origin`` summarises the provenance of the group's rows: the single origin
 * when they all agree, ``'mixed'`` when they do not. ``'manual'`` is the normal
 * case and renders no badge; anything else means part of this mapping was never
 * reviewed by a human and deserves a visible mark.
 */
interface RoleMappingGroup {
  key: string;
  org_id: string;
  job_code: string;
  role_keys: string[];
  origin: "manual" | "jit" | "legacy" | "mixed";
  /** Tenants whose first login auto-created part of this group. */
  source_orgs: string[];
}

function groupMappings(mappings: RoleMapping[]): RoleMappingGroup[] {
  const byCode = new Map<string, RoleMappingGroup>();
  const origins = new Map<string, Set<string>>();
  for (const mapping of mappings) {
    const key = `${mapping.org_id}|${mapping.job_code}`;
    let group = byCode.get(key);
    if (!group) {
      group = {
        key,
        org_id: mapping.org_id,
        job_code: mapping.job_code,
        role_keys: [],
        origin: "manual",
        source_orgs: [],
      };
      byCode.set(key, group);
      origins.set(key, new Set());
    }
    if (!group.role_keys.includes(mapping.role_key)) {
      group.role_keys.push(mapping.role_key);
    }
    // Anything unrecognised (an older server, a hand-written row) is treated as
    // reviewed rather than raising a badge nobody can act on.
    const rowOrigin =
      mapping.origin === "jit"
        ? "jit"
        : mapping.origin === "legacy"
        ? "legacy"
        : "manual";
    origins.get(key)?.add(rowOrigin);
    if (rowOrigin === "jit") {
      // `source_org` is provenance, not scope: the row is written against `'*'`
      // and applies everywhere, but it remembers which tenant's login created
      // it so an admin is not looking at an unattributed global default.
      if (
        mapping.source_org &&
        !group.source_orgs.includes(mapping.source_org)
      ) {
        group.source_orgs.push(mapping.source_org);
      }
    }
  }
  for (const group of byCode.values()) {
    group.role_keys.sort();
    const kinds = origins.get(group.key);
    group.origin =
      kinds && kinds.size === 1
        ? ([...kinds][0] as RoleMappingGroup["origin"])
        : "mixed";
  }
  return [...byCode.values()];
}

/**
 * @param tenantOrgId when set, the tab renders the **tenant face**: only this
 *   tenant's ``(org, code)`` rows (never the ``'*'`` platform defaults), the
 *   org picker disappears, and writes go to ``/org/{org}/role-mappings``.
 * @param reloadKey bumped by the packages tab after a write — refetches the
 *   role pools so a just-created package appears in the pickers without a
 *   page reload (antd keeps this pane mounted).
 */
export default function MappingsTab({
  tenantOrgId,
  reloadKey = 0,
}: {
  tenantOrgId?: string;
  reloadKey?: number;
}) {
  const { t } = useTranslation();
  const [form] = Form.useForm<MappingFormValues>();
  const [editing, setEditing] = useState<RoleMappingGroup | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [keyword, setKeyword] = useState("");
  const [orgFilter, setOrgFilter] = useState<string | undefined>();
  const [codeFilter, setCodeFilter] = useState<string | undefined>();
  const [onlyDefault, setOnlyDefault] = useState(false);
  const pagination = useTablePagination(10);
  const { areaRef, scrollY } = useTableArea();

  const refs = useOrgRoleRefs(!tenantOrgId, reloadKey);
  const dataKey = `${tenantOrgId ?? ""}:${reloadKey}`;
  const tenantRolesRef = useAsyncData<TenantRole[]>(
    () =>
      tenantOrgId
        ? orgAdminApi.listRoles(tenantOrgId)
        : Promise.resolve([]),
    { fallbackError: t("adminRoles.loadFailed"), t, reloadKey: dataKey },
  );
  const orgs = useMemo<OrgInfo[]>(
    () => (tenantOrgId ? [] : (refs.data?.orgs ?? [])),
    [refs.data, tenantOrgId],
  );
  const roles = useMemo<RolePackage[]>(
    () =>
      tenantOrgId
        ? (tenantRolesRef.data ?? []).map((r) => ({
            role_key: r.role_key,
            name_zh: r.name_zh,
            org_id: r.org_id,
            is_default: r.is_default,
            is_system: r.is_system,
            description: r.description,
          }))
        : (refs.data?.roles ?? []),
    [refs.data, tenantOrgId, tenantRolesRef.data],
  );
  const roleLabels = useRoleLabels(roles);

  const { data, loading, error, reload } = useAsyncData<RoleMapping[]>(
    () =>
      tenantOrgId
        ? orgAdminApi.listMappings(tenantOrgId)
        : rolesApi.listMappings(),
    {
      fallbackError: t("adminRoles.mappings.loadFailed"),
      t,
      reloadKey: dataKey,
    },
  );

  const groups = useMemo(() => groupMappings(data ?? []), [data]);

  /** Ability packages flagged as the default minimum for a brand-new role code. */
  const defaultKeys = useMemo(
    () =>
      new Set(
        roles.filter((role) => role.is_default).map((role) => role.role_key),
      ),
    [roles],
  );

  /** Display name of a real tenant (never the `'*'` sentinel). */
  const tenantName = useCallback(
    (orgId: string) => orgs.find((org) => org.org_id === orgId)?.name ?? orgId,
    [orgs],
  );

  const orgName = useCallback(
    (orgId: string) =>
      orgId === ANY_ORG ? t("adminRoles.mappings.anyOrg") : tenantName(orgId),
    [t, tenantName],
  );

  /**
   * Badge for a mapping no human has reviewed.
   *
   * ``origin='manual'`` (the normal case) renders nothing on purpose — a badge
   * on every row would just be noise. ``'jit'`` means the whole group was
   * auto-created by a first login; the scope column already names the tenant
   * it applies to (auto-provisioning is tenant-scoped), so the tooltip only
   * has to say "not reviewed yet". ``'legacy'`` is the honest "we cannot say"
   * for rows written before provenance existed, and its tooltip points at the
   * one way to settle it.
   */
  const originBadge = useCallback(
    (group: RoleMappingGroup) => {
      if (group.origin === "manual") return null;
      if (group.origin === "legacy") {
        return (
          <Tooltip title={t("adminRoles.mappings.originLegacyTip")}>
            <Tag style={{ marginInlineEnd: 0 }} icon={<History size={11} />}>
              {t("adminRoles.mappings.originLegacy")}
            </Tag>
          </Tooltip>
        );
      }
      const title =
        group.origin === "jit"
          ? t("adminRoles.mappings.originJitTip")
          : t("adminRoles.mappings.originMixedTip");
      return (
        <Tooltip title={title}>
          <Tag
            color="orange"
            style={{ marginInlineEnd: 0 }}
            icon={<Sparkles size={11} />}
          >
            {group.origin === "jit"
              ? t("adminRoles.mappings.originJit")
              : t("adminRoles.mappings.originMixed")}
          </Tag>
        </Tooltip>
      );
    },
    [t],
  );

  /**
   * The scope filter doubles as a "view as tenant" switch.
   *
   * Picking a tenant must show what that tenant **effectively** gets, which is
   * its own rows *plus* the platform-wide `'*'` ones — the resolver unions them
   * (`org_id IN ('*', ?)` server-side, ``expand_role_keys`` client-side of the
   * API). Filtering to `group.org_id === tenant` alone would render an empty
   * table for a tenant whose access comes entirely from the global rows, which
   * is exactly the wrong conclusion to draw. Picking 「平台通配」 shows the
   * global rows alone.
   */
  const matchesOrgView = useCallback(
    (groupId: string) => {
      if (orgFilter === undefined) return true;
      if (orgFilter === ANY_ORG) return groupId === ANY_ORG;
      return groupId === orgFilter || groupId === ANY_ORG;
    },
    [orgFilter],
  );

  const filtered = useMemo(() => {
    const needle = keyword.trim().toLowerCase();
    return groups.filter((group) => {
      if (!matchesOrgView(group.org_id)) return false;
      if (codeFilter !== undefined && group.job_code !== codeFilter)
        return false;
      if (onlyDefault) {
        const hasDefault = group.role_keys.some((key) => defaultKeys.has(key));
        if (!hasDefault) return false;
      }
      if (!needle) return true;
      return [
        group.org_id,
        group.job_code,
        orgName(group.org_id),
        ...group.role_keys,
        ...group.role_keys.map((key) => roleLabels.get(key) ?? key),
      ]
        .join(" ")
        .toLowerCase()
        .includes(needle);
    });
  }, [
    codeFilter,
    defaultKeys,
    groups,
    keyword,
    matchesOrgView,
    onlyDefault,
    orgName,
    roleLabels,
  ]);

  /** True when this row only shows up because the admin filtered by a tenant. */
  const isInherited = useCallback(
    (groupId: string) =>
      orgFilter !== undefined && orgFilter !== ANY_ORG && groupId === ANY_ORG,
    [orgFilter],
  );

  const orgOptions = useMemo(
    () => [
      { value: ANY_ORG, label: t("adminRoles.mappings.anyOrg") },
      ...orgs.map((org) => ({
        value: org.org_id,
        label: `${org.name} (${org.org_id})`,
      })),
    ],
    [orgs, t],
  );

  // Picker options: the tenant face hides platform-owned system packages
  // (member/org_admin — platform designation + JIT machinery). The full
  // ``roles`` list stays in the label pool so already-mapped rows render
  // Chinese names instead of raw keys.
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

  const codeFilterOptions = useMemo(
    () =>
      [...new Set(groups.map((group) => group.job_code))]
        .sort()
        .map((code) => ({ value: code, label: code })),
    [groups],
  );

  const openCreate = useCallback(() => {
    setEditing(null);
    form.resetFields();
    form.setFieldsValue({ org_id: ANY_ORG, job_code: "", role_keys: [] });
    setModalOpen(true);
  }, [form]);

  const openEdit = useCallback(
    (group: RoleMappingGroup) => {
      setEditing(group);
      form.resetFields();
      form.setFieldsValue({
        org_id: group.org_id,
        job_code: group.job_code,
        role_keys: [...group.role_keys],
      });
      setModalOpen(true);
    },
    [form],
  );

  const submit = useCallback(async () => {
    let values: MappingFormValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    const jobCode = values.job_code.trim();
    setSubmitting(true);
    try {
      if (tenantOrgId) {
        // Tenant face: the scope IS the path org — no org_id in the payload.
        await orgAdminApi.setMapping(tenantOrgId, {
          job_code: jobCode,
          role_keys: values.role_keys,
          ...(editing ? { previous_job_code: editing.job_code } : {}),
        });
      } else {
        const orgId = values.org_id || ANY_ORG;
        await rolesApi.setMapping({
          org_id: orgId,
          job_code: jobCode,
          role_keys: values.role_keys,
          ...(editing
            ? {
                previous_org_id: editing.org_id,
                previous_job_code: editing.job_code,
              }
            : {}),
        });
      }
      message.success(
        editing
          ? t("adminRoles.mappings.updateSuccess", { job: jobCode })
          : t("adminRoles.mappings.addSuccess", { job: jobCode }),
      );
      setModalOpen(false);
      setEditing(null);
      reload();
    } catch (err) {
      message.error(apiErrorMessage(err, t("adminRoles.saveFailed"), t));
    } finally {
      setSubmitting(false);
    }
  }, [editing, form, reload, t, tenantOrgId]);

  const confirmRemove = useCallback(
    (group: RoleMappingGroup) => {
      const autoRebuilt = group.role_keys.some((key) => defaultKeys.has(key));
      showConfirmModal({
        title: t("adminRoles.mappings.removeTitle", { job: group.job_code }),
        content: (
          <div>
            <div>
              {t("adminRoles.mappings.removeBody", {
                org: orgName(group.org_id),
                job: group.job_code,
                count: group.role_keys.length,
              })}
            </div>
            {autoRebuilt && (
              <div style={{ marginTop: 8 }}>
                {t("adminRoles.mappings.removeRebuiltHint")}
              </div>
            )}
          </div>
        ),
        okText: t("adminRoles.delete"),
        okType: "danger",
        cancelText: t("adminRoles.cancel"),
        onOk: async () => {
          try {
            // An empty package set is the delete: one atomic call instead of
            // one DELETE per triple.
            if (tenantOrgId) {
              await orgAdminApi.setMapping(tenantOrgId, {
                job_code: group.job_code,
                role_keys: [],
                previous_job_code: group.job_code,
              });
            } else {
              await rolesApi.setMapping({
                org_id: group.org_id,
                job_code: group.job_code,
                role_keys: [],
                previous_org_id: group.org_id,
                previous_job_code: group.job_code,
              });
            }
            message.success(
              t("adminRoles.mappings.removeSuccess", { job: group.job_code }),
            );
            reload();
          } catch (err) {
            message.error(
              apiErrorMessage(err, t("adminRoles.deleteFailed"), t),
            );
            // Rethrow so the confirm dialog stays open on failure.
            throw err;
          }
        },
      });
    },
    [defaultKeys, orgName, reload, t, tenantOrgId],
  );

  const firstLoad = loading && data === null;

  return (
    <div className={styles.panel}>
      {error ? (
        <LoadError message={error} onRetry={reload} />
      ) : firstLoad ? (
        <ListSkeleton rows={6} />
      ) : (
        <>
          <div className={styles.toolbar}>
            <Input
              allowClear
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
              placeholder={t("adminRoles.mappings.searchPlaceholder")}
              prefix={<Search size={14} />}
              style={{ maxWidth: 260 }}
            />
            {!tenantOrgId && (
              <Select
                allowClear
                showSearch
                optionFilterProp="label"
                style={{ minWidth: 180 }}
                placeholder={t("adminRoles.mappings.filterOrg")}
                value={orgFilter}
                onChange={setOrgFilter}
                options={orgOptions}
              />
            )}
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              style={{ minWidth: 180 }}
              placeholder={t("adminRoles.mappings.filterCode")}
              value={codeFilter}
              onChange={setCodeFilter}
              options={codeFilterOptions}
            />
            <Button
              size="small"
              type={onlyDefault ? "primary" : "default"}
              onClick={() => setOnlyDefault((previous) => !previous)}
            >
              {t("adminRoles.mappings.filterDefault")}
            </Button>
            <div className={styles.toolbarEnd}>
              <Button
                icon={<RefreshCw size={14} />}
                onClick={reload}
                loading={loading && !firstLoad}
              >
                {t("adminRoles.refresh")}
              </Button>
              {/*
                Wrapped in a span because a disabled button swallows the hover
                too, and "greyed out for no stated reason" is a dead end: the
                only thing that can turn this off is having no role to grant.
              */}
              <Tooltip
                title={
                  roles.length === 0
                    ? t("adminRoles.mappings.rolesEmpty")
                    : undefined
                }
              >
                <span>
                  <Button
                    type="primary"
                    icon={<Plus size={14} />}
                    disabled={roles.length === 0}
                    onClick={openCreate}
                  >
                    {t("adminRoles.mappings.add")}
                  </Button>
                </span>
              </Tooltip>
            </div>
          </div>

          <div className={styles.toolbar}>
            <span className={styles.cellSecondary}>
              {t("adminRoles.mappings.summary", {
                codes: filtered.length,
                rows: filtered.reduce(
                  (total, group) => total + group.role_keys.length,
                  0,
                ),
              })}
            </span>
          </div>

          <div className={styles.tableArea} ref={areaRef}>
            <Table<RoleMappingGroup>
              rowKey="key"
              size="small"
              loading={loading}
              dataSource={filtered}
              pagination={pagination}
              /* 四列都给显式宽度（见下方各列 width）——antd 在 table-layout:fixed 下
                 把容器多出来的宽度**按比例分摊**给有宽度的列；只要留一列不给宽度，
                 那一列就会独吞全部余量（改前「角色」列因此拿到 660px）。
                 896 = 320 + 180 + 240 + 156，窄屏才出横向滚动。 */
              scroll={
                scrollY === undefined ? { x: 896 } : { x: 896, y: scrollY }
              }
              locale={{
                emptyText: (
                  <TableEmpty
                    filtered={
                      keyword.trim().length > 0 ||
                      orgFilter !== undefined ||
                      codeFilter !== undefined ||
                      onlyDefault
                    }
                    onClear={() => {
                      setKeyword("");
                      setOrgFilter(undefined);
                      setCodeFilter(undefined);
                      setOnlyDefault(false);
                    }}
                  />
                ),
              }}
              columns={[
                {
                  title: t("adminRoles.mappings.jobCode"),
                  dataIndex: "job_code",
                  /* 240 太窄：最长的 IdP 角色串（如 channel_management_officer）
                     实测 188px，加上左右内边距几乎贴边 */
                  width: 320,
                  render: (value: string, record: RoleMappingGroup) => {
                    const isDefault = record.role_keys.some((key) =>
                      defaultKeys.has(key),
                    );
                    const origin = originBadge(record);
                    return (
                      <div className={styles.cellStack}>
                        <span className={styles.mono}>{value}</span>
                        {/* 两个标记并排一行：列宽够放下（实测 441px），
                            竖排会白白多占一行行高 */}
                        {(isDefault || origin) && (
                          <div className={styles.cellTags}>
                            {isDefault && (
                              <Tooltip
                                title={t("adminRoles.packages.isDefaultHint")}
                              >
                                <Tag
                                  color="green"
                                  style={{ marginInlineEnd: 0 }}
                                  icon={<Check size={11} />}
                                >
                                  {t("adminRoles.mappings.hasDefaultPackage")}
                                </Tag>
                              </Tooltip>
                            )}
                            {origin}
                          </div>
                        )}
                      </div>
                    );
                  },
                },
                ...(tenantOrgId
                  ? []
                  : [
                      {
                        title: t("adminRoles.mappings.orgColumn"),
                        dataIndex: "org_id",
                        width: 180,
                        render: (value: string) => (
                          <div className={styles.cellStack}>
                            <span>{orgName(value)}</span>
                            <span className={styles.mono}>
                              {value !== ANY_ORG ? value : ANY_ORG}
                            </span>
                            {isInherited(value) && (
                              <Tooltip
                                title={t("adminRoles.mappings.inheritedTip", {
                                  org: orgName(ANY_ORG),
                                })}
                              >
                                <Tag
                                  style={{ marginInlineEnd: 0 }}
                                  icon={<Link2 size={11} />}
                                >
                                  {t("adminRoles.mappings.inheritedTag")}
                                </Tag>
                              </Tooltip>
                            )}
                          </div>
                        ),
                      } satisfies ColumnsType<RoleMappingGroup>[number],
                    ]),
                {
                  title: t("adminRoles.mappings.roleKey"),
                  dataIndex: "role_keys",
                  /* 必须给宽度：不给的话它就是「没有宽度的那一列」，
                     会独吞容器余量（改前实测 660px，只放一个标签） */
                  width: 240,
                  render: (keys: string[]) => (
                    <RoleTagList roleKeys={keys} labels={roleLabels} />
                  ),
                },
                {
                  title: t("adminRoles.actions"),
                  key: "actions",
                  width: 156,
                  align: "right",
                  render: (_, record) => (
                    <div className={styles.rowActions}>
                      <Button
                        size="small"
                        type="link"
                        icon={<Pencil size={13} />}
                        aria-label={t("adminRoles.mappings.edit")}
                        onClick={() => openEdit(record)}
                      >
                        {t("adminRoles.edit")}
                      </Button>
                      <Button
                        size="small"
                        danger
                        type="link"
                        icon={<Trash2 size={13} />}
                        aria-label={t("adminRoles.delete")}
                        onClick={() => confirmRemove(record)}
                      >
                        {t("adminRoles.delete")}
                      </Button>
                    </div>
                  ),
                },
              ]}
            />
          </div>
        </>
      )}

      <Modal
        open={modalOpen}
        title={
          editing
            ? t("adminRoles.mappings.editTitle", { job: editing.job_code })
            : t("adminRoles.mappings.add")
        }
        okText={t("adminRoles.save")}
        cancelText={t("adminRoles.cancel")}
        confirmLoading={submitting}
        onOk={() => void submit()}
        onCancel={() => {
          setModalOpen(false);
          setEditing(null);
        }}
      >
        <Form form={form} layout="vertical" requiredMark>
          {/*
            Warning lives inside a `shouldUpdate` render prop rather than
            `Form.useWatch`: the Modal's children mount lazily, so a watcher
            registered before the first open can miss the initial
            `setFieldsValue` — and this is exactly the message that must not go
            missing before a platform-wide save.
          */}
          {!tenantOrgId && (
            <Form.Item
              noStyle
              shouldUpdate={(previous, next) => previous.org_id !== next.org_id}
            >
              {({ getFieldValue }) =>
                getFieldValue("org_id") === ANY_ORG ? (
                  <Alert
                    type="warning"
                    showIcon
                    style={{ marginBottom: 16 }}
                    message={t("adminRoles.mappings.globalEditWarning")}
                  />
                ) : null
              }
            </Form.Item>
          )}
          <Form.Item
            name="job_code"
            label={t("adminRoles.mappings.jobCode")}
            extra={t("adminRoles.mappings.jobCodeHint")}
            rules={[
              {
                required: true,
                message: t("adminRoles.mappings.jobCodeRequired"),
              },
              {
                pattern: ROLE_CODE_PATTERN,
                message: t("adminRoles.mappings.jobCodePattern"),
              },
            ]}
          >
            <Input placeholder="finance_manager" maxLength={ROLE_CODE_MAX} />
          </Form.Item>
          {!tenantOrgId && (
            <Form.Item
              name="org_id"
              label={t("adminRoles.mappings.orgColumn")}
              extra={t("adminRoles.mappings.anyOrgHint")}
              rules={[
                {
                  required: true,
                  message: t("adminRoles.mappings.orgRequired"),
                },
              ]}
            >
              <Select showSearch optionFilterProp="label" options={orgOptions} />
            </Form.Item>
          )}
          <Form.Item
            name="role_keys"
            label={t("adminRoles.mappings.roleKey")}
            extra={t("adminRoles.mappings.rolesHint")}
            rules={[
              {
                required: true,
                // Required on purpose: an edit that empties the set would
                // silently become a delete, which the JIT resolver then
                // re-creates on the next login. Deleting is its own action.
                message: t("adminRoles.mappings.roleRequired"),
              },
            ]}
          >
            <Select
              mode="multiple"
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder={t("adminRoles.mappings.pickRole")}
              options={roleOptions}
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
