import { Button, Form, Input, Modal, Select, Table, Tag, Tooltip } from "antd";
import {
  Building2,
  KeyRound,
  Pencil,
  Plus,
  Power,
  PowerOff,
  RefreshCw,
  Search,
  ShieldAlert,
  Trash2,
} from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  ANY_ORG,
  rolesApi,
  type OrgBinding,
  type OrgInfo,
  type RoleMapping,
  type RolePackage,
} from "../../../api/modules/roles";
import { message } from "../../../utils/antdMessage";
import { apiErrorMessage } from "../../../utils/apiError";
import { showConfirmModal } from "../../../utils/confirmModal";
import styles from "./Roles.module.less";
import {
  ListSkeleton,
  LoadError,
  TableEmpty,
  useAsyncData,
  useRoleLabels,
  useTableArea,
  useTablePagination,
} from "./shared";

/** Mirrors the backend's ``_ORG_ID_RE`` (``org_id`` is an opaque slug). */
const ORG_ID_PATTERN = /^[A-Za-z0-9_.-]+$/;
const ORG_ID_MAX = 64;

const KINDS = ["internal", "partner"] as const;
const MATCH_TYPES = ["claim", "static", "domain"] as const;

const KIND_TAG_COLOR: Record<string, string> = {
  internal: "blue",
  partner: "gold",
};

/**
 * ``match_type`` → the i18n key suffix for its label, option hint and the
 * "which value do I type here" hint. Keeping the mapping in one place stops
 * the three lookups from drifting apart.
 */
const MATCH_TYPE_SUFFIX: Record<string, string> = {
  claim: "Claim",
  static: "Static",
  domain: "Domain",
};

function matchSuffix(matchType: string | undefined): string {
  return MATCH_TYPE_SUFFIX[matchType ?? "claim"] ?? "Claim";
}

interface OrgFormValues {
  org_id: string;
  name: string;
  kind: string;
  status: string;
}

interface BindingFormValues {
  idp_alias: string;
  match_type: string;
  match_value: string;
  note: string;
}

/**
 * Tenants — the prerequisite half of the authorization model.
 *
 * Each tenant owns two tables: the tenant row itself (``orgs``) and the
 * *resolution keys* (``idp_org_bindings``) that map an upstream value onto it.
 * Both are needed before anyone can be routed here: the five-level resolution
 * chain fails closed on an org that does not exist, and needs a key to match
 * in the first place — so "created the tenant but no key" is an invisible
 * tenant, which is why the listing flags a zero key count loudly.
 *
 * ``org_id`` is immutable after creation. It is the primary key that every
 * binding and membership references ``ON DELETE CASCADE``, so renaming it
 * would delete-then-insert and silently wipe both.
 */
/**
 * @param onDataChanged called after every successful create/update/delete so
 *   the sibling tabs (members, mappings, ACL) can refetch their tenant
 *   pickers — antd keeps their panes mounted, so a just-created tenant would
 *   otherwise be missing from their org dropdowns until a page reload.
 */
export default function OrgsTab({ onDataChanged }: { onDataChanged?: () => void }) {
  const { t } = useTranslation();
  const [form] = Form.useForm<OrgFormValues>();
  const [bindingForm] = Form.useForm<BindingFormValues>();
  const [editing, setEditing] = useState<OrgInfo | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  /** Tenant whose resolution keys are being edited; ``null`` closes the modal. */
  const [bindingOrg, setBindingOrg] = useState<OrgInfo | null>(null);
  const [bindingSubmitting, setBindingSubmitting] = useState(false);
  const [keyword, setKeyword] = useState("");
  const [kindFilter, setKindFilter] = useState<string | undefined>();
  const [statusFilter, setStatusFilter] = useState<string | undefined>();
  const [onlyNoBinding, setOnlyNoBinding] = useState(false);
  const pagination = useTablePagination(10);
  const { areaRef, scrollY } = useTableArea();

  const orgsState = useAsyncData<OrgInfo[]>(() => rolesApi.listOrgs(), {
    fallbackError: t("adminRoles.orgs.loadFailed"),
    t,
  });
  const bindingsState = useAsyncData<OrgBinding[]>(
    () => rolesApi.listOrgBindings(),
    { fallbackError: t("adminRoles.orgs.bindingsLoadFailed"), t },
  );
  // The mapping table is small, so one unfiltered fetch covers every tenant;
  // asking per tenant would be N requests for the same rows.
  const mappingsState = useAsyncData<RoleMapping[]>(
    () => rolesApi.listMappings(),
    { fallbackError: t("adminRoles.mappings.loadFailed"), t },
  );
  const rolesState = useAsyncData<RolePackage[]>(() => rolesApi.list(), {
    fallbackError: t("adminRoles.orgs.loadFailed"),
    t,
  });

  const orgs = useMemo<OrgInfo[]>(() => orgsState.data ?? [], [orgsState.data]);
  const bindings = useMemo<OrgBinding[]>(
    () => bindingsState.data ?? [],
    [bindingsState.data],
  );
  const mappings = useMemo<RoleMapping[]>(
    () => mappingsState.data ?? [],
    [mappingsState.data],
  );
  const roleLabels = useRoleLabels(
    useMemo<RolePackage[]>(() => rolesState.data ?? [], [rolesState.data]),
  );

  /**
   * What each tenant **effectively** grants.
   *
   * = its own rows **plus** the platform-wide `'*'` rows. The two are added
   * together (`org_id IN ('*', ?)` on the server, unioned by
   * ``expand_role_keys``), so a tenant row never replaces the global one. This
   * is what makes the column worth showing: a tenant whose access comes
   * entirely from auto-provisioned global rows looks empty in the 角色映射 tab
   * unless you filter by it, and looks like nothing applies here at all.
   */
  const effectiveByOrg = useMemo(() => {
    const globalRows = mappings.filter((row) => row.org_id === ANY_ORG);
    const byOrg = new Map<
      string,
      { job_code: string; role_key: string; inherited: boolean }[]
    >();
    for (const org of orgs) {
      const rows = [
        ...mappings
          .filter((row) => row.org_id === org.org_id)
          .map((row) => ({
            job_code: row.job_code,
            role_key: row.role_key,
            inherited: false,
          })),
        ...globalRows.map((row) => ({
          job_code: row.job_code,
          role_key: row.role_key,
          inherited: true,
        })),
      ].sort(
        (a, b) =>
          a.job_code.localeCompare(b.job_code) ||
          a.role_key.localeCompare(b.role_key),
      );
      byOrg.set(org.org_id, rows);
    }
    return byOrg;
  }, [mappings, orgs]);

  const bindingsByOrg = useMemo(() => {
    const byOrg = new Map<string, OrgBinding[]>();
    for (const binding of bindings) {
      const list = byOrg.get(binding.org_id);
      if (list) list.push(binding);
      else byOrg.set(binding.org_id, [binding]);
    }
    return byOrg;
  }, [bindings]);

  /** The lists share the tenant counts, so a write must refresh them together. */
  const reloadAll = useCallback(() => {
    orgsState.reload();
    bindingsState.reload();
    mappingsState.reload();
  }, [orgsState, bindingsState, mappingsState]);

  const kindLabel = useCallback(
    (kind: string) =>
      kind === "partner"
        ? t("adminRoles.orgs.kindPartner")
        : t("adminRoles.orgs.kindInternal"),
    [t],
  );

  const filtered = useMemo(() => {
    const needle = keyword.trim().toLowerCase();
    return orgs.filter((org) => {
      if (kindFilter !== undefined && org.kind !== kindFilter) return false;
      if (statusFilter !== undefined && org.status !== statusFilter)
        return false;
      if (onlyNoBinding && (org.binding_count ?? 0) > 0) return false;
      if (!needle) return true;
      const keys = bindingsByOrg.get(org.org_id) ?? [];
      const effective = effectiveByOrg.get(org.org_id) ?? [];
      return [
        org.org_id,
        org.name,
        kindLabel(org.kind),
        ...keys.flatMap((key) => [
          key.idp_alias,
          key.match_type,
          key.match_value,
        ]),
        // Matching on effective role codes makes "which tenant gets
        // super_admin?" answerable from the tenant list.
        ...effective.flatMap((row) => [
          row.job_code,
          row.role_key,
          roleLabels.get(row.role_key) ?? row.role_key,
        ]),
      ]
        .join(" ")
        .toLowerCase()
        .includes(needle);
    });
  }, [
    bindingsByOrg,
    effectiveByOrg,
    kindFilter,
    keyword,
    kindLabel,
    onlyNoBinding,
    orgs,
    roleLabels,
    statusFilter,
  ]);

  const activeCount = useMemo(
    () => orgs.filter((org) => org.status === "active").length,
    [orgs],
  );
  const unboundCount = useMemo(
    () => orgs.filter((org) => (org.binding_count ?? 0) === 0).length,
    [orgs],
  );

  const kindOptions = useMemo(
    () => KINDS.map((kind) => ({ value: kind, label: kindLabel(kind) })),
    [kindLabel],
  );
  const statusOptions = useMemo(
    () => [
      { value: "active", label: t("adminRoles.orgs.statusActive") },
      { value: "disabled", label: t("adminRoles.orgs.statusDisabled") },
    ],
    [t],
  );

  /* ----- tenant create / edit ----- */

  const openCreate = useCallback(() => {
    setEditing(null);
    form.resetFields();
    form.setFieldsValue({
      org_id: "",
      name: "",
      kind: "internal",
      status: "active",
    });
    setModalOpen(true);
  }, [form]);

  const openEdit = useCallback(
    (org: OrgInfo) => {
      setEditing(org);
      form.resetFields();
      form.setFieldsValue({
        org_id: org.org_id,
        name: org.name,
        kind: org.kind,
        status: org.status,
      });
      setModalOpen(true);
    },
    [form],
  );

  const submitOrg = useCallback(async () => {
    let values: OrgFormValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    setSubmitting(true);
    try {
      if (editing) {
        // ``org_id`` is not sent: it is the primary key the bindings and
        // memberships cascade from, so the server keeps it fixed.
        const row = await rolesApi.updateOrg(editing.org_id, {
          name: values.name.trim(),
          kind: values.kind,
          status: values.status,
        });
        message.success(t("adminRoles.orgs.updateSuccess", { name: row.name }));
      } else {
        const row = await rolesApi.createOrg({
          org_id: values.org_id.trim(),
          name: values.name.trim(),
          kind: values.kind,
          status: values.status,
        });
        message.success(t("adminRoles.orgs.createSuccess", { name: row.name }));
      }
      setModalOpen(false);
      setEditing(null);
      reloadAll();
      onDataChanged?.();
    } catch (err) {
      message.error(
        apiErrorMessage(
          err,
          editing
            ? t("adminRoles.saveFailed")
            : t("adminRoles.orgs.createFailed"),
          t,
        ),
      );
    } finally {
      setSubmitting(false);
    }
  }, [editing, form, onDataChanged, reloadAll, t]);

  /* ----- enable / disable / delete ----- */

  const toggleStatus = useCallback(
    async (org: OrgInfo) => {
      const next = org.status === "active" ? "disabled" : "active";
      try {
        await rolesApi.updateOrg(org.org_id, { status: next });
        message.success(
          t(
            next === "active"
              ? "adminRoles.orgs.enableSuccess"
              : "adminRoles.orgs.disableSuccess",
            { name: org.name },
          ),
        );
        reloadAll();
        onDataChanged?.();
      } catch (err) {
        message.error(apiErrorMessage(err, t("adminRoles.saveFailed"), t));
      }
    },
    [onDataChanged, reloadAll, t],
  );

  const confirmRemove = useCallback(
    (org: OrgInfo) => {
      const members = org.member_count ?? 0;
      const keyCount = org.binding_count ?? 0;
      // Both child tables cascade, so a tenant with either one is a delete
      // that revokes access. The counts come from the listing, so the dialog
      // can say exactly what is at stake *before* the call rather than
      // reporting it afterwards in a 409.
      const cascades = members > 0 || keyCount > 0;
      showConfirmModal({
        title: t("adminRoles.orgs.removeTitle", { name: org.name }),
        content: (
          <div>
            {cascades
              ? t("adminRoles.orgs.removeCascadeBody", {
                  name: org.name,
                  org: org.org_id,
                  members,
                  bindings: keyCount,
                })
              : t("adminRoles.orgs.removeSafeBody", {
                  name: org.name,
                  org: org.org_id,
                })}
          </div>
        ),
        okText: cascades
          ? t("adminRoles.orgs.removeConfirmForce")
          : t("adminRoles.delete"),
        okType: "danger",
        cancelText: t("adminRoles.cancel"),
        onOk: async () => {
          try {
            await rolesApi.deleteOrg(org.org_id, cascades);
            message.success(
              t("adminRoles.orgs.removeSuccess", { name: org.name }),
            );
            reloadAll();
            onDataChanged?.();
          } catch (err) {
            message.error(
              apiErrorMessage(err, t("adminRoles.orgs.deleteFailed"), t),
            );
            // Rethrow so the confirm dialog stays open on failure.
            throw err;
          }
        },
      });
    },
    [onDataChanged, reloadAll, t],
  );

  /* ----- resolution keys ----- */

  const openBindings = useCallback(
    (org: OrgInfo) => {
      setBindingOrg(org);
      bindingForm.resetFields();
      bindingForm.setFieldsValue({
        idp_alias: "",
        match_type: "claim",
        match_value: "",
        note: "",
      });
    },
    [bindingForm],
  );

  /** Live ``match_type`` so the value field can follow it. */
  const matchType =
    (Form.useWatch("match_type", bindingForm) as string | undefined) ?? "claim";

  const addBinding = useCallback(async () => {
    if (!bindingOrg) return;
    let values: BindingFormValues;
    try {
      values = await bindingForm.validateFields();
    } catch {
      return;
    }
    setBindingSubmitting(true);
    try {
      await rolesApi.createOrgBinding({
        idp_alias: values.idp_alias.trim(),
        match_type: values.match_type,
        // ``static`` means "every user of this IdP" and must carry no value —
        // the server rejects a non-empty one outright.
        match_value:
          values.match_type === "static" ? "" : values.match_value.trim(),
        org_id: bindingOrg.org_id,
        note: values.note ?? "",
      });
      message.success(t("adminRoles.orgs.bindingCreateSuccess"));
      bindingForm.resetFields();
      bindingForm.setFieldsValue({
        idp_alias: values.idp_alias.trim(),
        match_type: values.match_type,
        match_value: "",
        note: "",
      });
      reloadAll();
    } catch (err) {
      message.error(
        apiErrorMessage(err, t("adminRoles.orgs.bindingCreateFailed"), t),
      );
    } finally {
      setBindingSubmitting(false);
    }
  }, [bindingForm, bindingOrg, reloadAll, t]);

  const confirmRemoveBinding = useCallback(
    (binding: OrgBinding) => {
      const shown = binding.match_value
        ? `${binding.idp_alias} / ${binding.match_type} / ${binding.match_value}`
        : `${binding.idp_alias} / ${binding.match_type}`;
      showConfirmModal({
        title: t("adminRoles.orgs.bindingRemoveTitle"),
        content: (
          <div>{t("adminRoles.orgs.bindingRemoveBody", { key: shown })}</div>
        ),
        okText: t("adminRoles.delete"),
        okType: "danger",
        cancelText: t("adminRoles.cancel"),
        onOk: async () => {
          try {
            await rolesApi.removeOrgBinding(binding);
            message.success(t("adminRoles.orgs.bindingRemoveSuccess"));
            reloadAll();
          } catch (err) {
            message.error(
              apiErrorMessage(err, t("adminRoles.orgs.bindingRemoveFailed"), t),
            );
            throw err;
          }
        },
      });
    },
    [reloadAll, t],
  );

  const currentBindings = bindingOrg
    ? bindingsByOrg.get(bindingOrg.org_id) ?? []
    : [];

  const firstLoad = orgsState.loading && orgsState.data === null;

  return (
    <div className={styles.panel}>
      {orgsState.error ? (
        <LoadError message={orgsState.error} onRetry={reloadAll} />
      ) : firstLoad ? (
        <ListSkeleton rows={6} />
      ) : (
        <>
          <div className={styles.toolbar}>
            <Input
              allowClear
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
              placeholder={t("adminRoles.orgs.searchPlaceholder")}
              prefix={<Search size={14} />}
              style={{ maxWidth: 260 }}
            />
            <Select
              allowClear
              style={{ minWidth: 140 }}
              placeholder={t("adminRoles.orgs.filterKind")}
              value={kindFilter}
              onChange={setKindFilter}
              options={kindOptions}
            />
            <Select
              allowClear
              style={{ minWidth: 140 }}
              placeholder={t("adminRoles.orgs.filterStatus")}
              value={statusFilter}
              onChange={setStatusFilter}
              options={statusOptions}
            />
            <Button
              size="small"
              type={onlyNoBinding ? "primary" : "default"}
              danger={onlyNoBinding}
              icon={<ShieldAlert size={13} />}
              onClick={() => setOnlyNoBinding((previous) => !previous)}
            >
              {t("adminRoles.orgs.filterNoBinding")}
            </Button>
            <div className={styles.toolbarEnd}>
              <Button
                icon={<RefreshCw size={14} />}
                onClick={reloadAll}
                loading={orgsState.loading && !firstLoad}
              >
                {t("adminRoles.refresh")}
              </Button>
              <Button
                type="primary"
                icon={<Plus size={14} />}
                onClick={openCreate}
              >
                {t("adminRoles.orgs.add")}
              </Button>
            </div>
          </div>

          <div className={styles.toolbar}>
            <span className={styles.cellSecondary}>
              {t("adminRoles.orgs.summary", {
                total: filtered.length,
                active: activeCount,
                unbound: unboundCount,
              })}
            </span>
          </div>

          <div className={styles.tableArea} ref={areaRef}>
            <Table<OrgInfo>
              rowKey="org_id"
              size="small"
              loading={orgsState.loading}
              dataSource={filtered}
              pagination={pagination}
              scroll={
                scrollY === undefined ? { x: 1140 } : { x: 1140, y: scrollY }
              }
              locale={{
                emptyText: (
                  <TableEmpty
                    filtered={
                      keyword.trim().length > 0 ||
                      kindFilter !== undefined ||
                      statusFilter !== undefined ||
                      onlyNoBinding
                    }
                    onClear={() => {
                      setKeyword("");
                      setKindFilter(undefined);
                      setStatusFilter(undefined);
                      setOnlyNoBinding(false);
                    }}
                  />
                ),
              }}
              columns={[
                {
                  title: t("adminRoles.orgs.name"),
                  dataIndex: "name",
                  width: 220,
                  render: (value: string, record: OrgInfo) => (
                    <div className={styles.cellStack}>
                      <span className={styles.cellPrimary}>{value}</span>
                      <span className={styles.mono}>{record.org_id}</span>
                    </div>
                  ),
                },
                {
                  title: t("adminRoles.orgs.kind"),
                  dataIndex: "kind",
                  width: 100,
                  render: (value: string) => (
                    <Tag color={KIND_TAG_COLOR[value] ?? "default"}>
                      {kindLabel(value)}
                    </Tag>
                  ),
                },
                {
                  title: t("adminRoles.orgs.status"),
                  dataIndex: "status",
                  width: 120,
                  render: (value: string) =>
                    value === "active" ? (
                      <Tag color="green">
                        {t("adminRoles.orgs.statusActive")}
                      </Tag>
                    ) : (
                      <Tooltip title={t("adminRoles.orgs.disabledHint")}>
                        <Tag style={{ marginInlineEnd: 0 }}>
                          {t("adminRoles.orgs.statusDisabled")}
                        </Tag>
                      </Tooltip>
                    ),
                },
                {
                  title: t("adminRoles.orgs.memberCount"),
                  dataIndex: "member_count",
                  width: 96,
                  align: "right",
                  render: (value: number | undefined) => value ?? 0,
                },
                {
                  title: t("adminRoles.orgs.bindingCount"),
                  dataIndex: "binding_count",
                  width: 160,
                  render: (value: number | undefined, record: OrgInfo) =>
                    (value ?? 0) > 0 ? (
                      <Button
                        type="link"
                        size="small"
                        style={{ paddingInline: 0 }}
                        onClick={() => openBindings(record)}
                      >
                        {value}
                      </Button>
                    ) : (
                      <Tooltip title={t("adminRoles.orgs.noBindingHint")}>
                        <Tag color="red" style={{ marginInlineEnd: 0 }}>
                          {t("adminRoles.orgs.noBinding")}
                        </Tag>
                      </Tooltip>
                    ),
                },
                {
                  title: t("adminRoles.orgs.effectiveTitle"),
                  key: "effective_mappings",
                  width: 140,
                  render: (_, record: OrgInfo) => {
                    const rows = effectiveByOrg.get(record.org_id) ?? [];
                    if (rows.length === 0) {
                      return (
                        <Tooltip title={t("adminRoles.orgs.effectiveEmptyTip")}>
                          <span className={styles.cellSecondary}>
                            {t("adminRoles.orgs.effectiveEmpty")}
                          </span>
                        </Tooltip>
                      );
                    }
                    return (
                      <Tooltip
                        title={
                          <div>
                            <div style={{ marginBottom: 4 }}>
                              {t("adminRoles.orgs.effectiveHint")}
                            </div>
                            {rows.map((row, index) => (
                              <div
                                key={`${row.job_code}|${row.role_key}|${index}`}
                              >
                                {row.job_code} →{" "}
                                {roleLabels.get(row.role_key) ?? row.role_key}
                                {row.inherited
                                  ? ` · ${t(
                                      "adminRoles.orgs.effectiveInherited",
                                    )}`
                                  : ""}
                              </div>
                            ))}
                          </div>
                        }
                      >
                        <span>{rows.length}</span>
                      </Tooltip>
                    );
                  },
                },
                {
                  title: t("adminRoles.actions"),
                  key: "actions",
                  width: 300,
                  align: "right",
                  render: (_, record: OrgInfo) => (
                    <div className={styles.rowActions}>
                      <Button
                        size="small"
                        type="link"
                        icon={<KeyRound size={13} />}
                        aria-label={t("adminRoles.orgs.bindingsAction")}
                        onClick={() => openBindings(record)}
                      >
                        {t("adminRoles.orgs.bindingsAction")}
                      </Button>
                      <Button
                        size="small"
                        type="link"
                        icon={<Pencil size={13} />}
                        aria-label={t("adminRoles.edit")}
                        onClick={() => openEdit(record)}
                      >
                        {t("adminRoles.edit")}
                      </Button>
                      <Button
                        size="small"
                        type="link"
                        icon={
                          record.status === "active" ? (
                            <PowerOff size={13} />
                          ) : (
                            <Power size={13} />
                          )
                        }
                        aria-label={
                          record.status === "active"
                            ? t("adminRoles.orgs.disable")
                            : t("adminRoles.orgs.enable")
                        }
                        onClick={() => void toggleStatus(record)}
                      >
                        {record.status === "active"
                          ? t("adminRoles.orgs.disable")
                          : t("adminRoles.orgs.enable")}
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
            ? t("adminRoles.orgs.editTitle", { name: editing.name })
            : t("adminRoles.orgs.addTitle")
        }
        okText={t("adminRoles.save")}
        cancelText={t("adminRoles.cancel")}
        confirmLoading={submitting}
        onOk={() => void submitOrg()}
        onCancel={() => {
          setModalOpen(false);
          setEditing(null);
        }}
      >
        <Form form={form} layout="vertical" requiredMark>
          <Form.Item
            name="org_id"
            label={t("adminRoles.orgs.orgId")}
            extra={editing ? t("adminRoles.orgs.orgIdHint") : undefined}
            rules={[
              { required: true, message: t("adminRoles.orgs.orgIdRequired") },
              {
                pattern: ORG_ID_PATTERN,
                message: t("adminRoles.orgs.orgIdPattern"),
              },
            ]}
          >
            {/* Immutable once created: every binding and membership cascades
                from this key, so the server has no rename and the field is
                locked instead of silently ignored. */}
            <Input
              placeholder="chery-dealer-001"
              maxLength={ORG_ID_MAX}
              disabled={editing !== null}
            />
          </Form.Item>
          <Form.Item
            name="name"
            label={t("adminRoles.orgs.name")}
            rules={[
              { required: true, message: t("adminRoles.orgs.nameRequired") },
            ]}
          >
            <Input placeholder="合作商 001" maxLength={128} />
          </Form.Item>
          <Form.Item
            name="kind"
            label={t("adminRoles.orgs.kind")}
            extra={t("adminRoles.orgs.kindHint")}
          >
            <Select options={kindOptions} />
          </Form.Item>
          <Form.Item
            name="status"
            label={t("adminRoles.orgs.status")}
            extra={t("adminRoles.orgs.statusHint")}
          >
            <Select options={statusOptions} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        open={bindingOrg !== null}
        title={t("adminRoles.orgs.bindingsTitle", {
          name: bindingOrg?.name ?? "",
        })}
        footer={null}
        onCancel={() => setBindingOrg(null)}
      >
        <div className={styles.modalHint} style={{ marginBottom: 12 }}>
          {t("adminRoles.orgs.bindingsHint")}
        </div>

        {bindingsState.error ? (
          <LoadError message={bindingsState.error} onRetry={reloadAll} />
        ) : currentBindings.length === 0 ? (
          <div className={styles.modalHint}>
            {t("adminRoles.orgs.bindingsEmpty")}
          </div>
        ) : (
          <Table<OrgBinding>
            rowKey={(record) =>
              `${record.idp_alias}|${record.match_type}|${record.match_value}`
            }
            size="small"
            pagination={false}
            dataSource={currentBindings}
            scroll={{ x: 460 }}
            columns={[
              {
                title: t("adminRoles.orgs.idpAlias"),
                dataIndex: "idp_alias",
                width: 130,
                render: (value: string) => (
                  <span className={styles.mono}>{value}</span>
                ),
              },
              {
                title: t("adminRoles.orgs.matchType"),
                dataIndex: "match_type",
                width: 110,
              },
              {
                title: t("adminRoles.orgs.matchValue"),
                dataIndex: "match_value",
                render: (value: string) =>
                  value ? (
                    <span className={styles.mono}>{value}</span>
                  ) : (
                    // An empty value is the *point* of a static binding, not
                    // missing data — show it as such.
                    <span className={styles.cellSecondary}>
                      {t("adminRoles.orgs.matchTypeStaticHint")}
                    </span>
                  ),
              },
              {
                title: t("adminRoles.actions"),
                key: "actions",
                width: 90,
                align: "right",
                render: (_, record: OrgBinding) => (
                  <div className={styles.rowActions}>
                    <Button
                      size="small"
                      danger
                      type="link"
                      icon={<Trash2 size={13} />}
                      aria-label={t("adminRoles.delete")}
                      onClick={() => confirmRemoveBinding(record)}
                    >
                      {t("adminRoles.delete")}
                    </Button>
                  </div>
                ),
              },
            ]}
          />
        )}

        <div className={styles.modalDivider} />

        <div className={styles.modalSection}>
          <span className={styles.modalSectionTitle}>
            <Building2 size={13} style={{ verticalAlign: -2 }} />{" "}
            {t("adminRoles.orgs.addBinding")}
          </span>
        </div>
        <Form form={bindingForm} layout="vertical" requiredMark>
          <Form.Item
            name="idp_alias"
            label={t("adminRoles.orgs.idpAlias")}
            extra={t("adminRoles.orgs.idpAliasHint")}
            rules={[
              {
                required: true,
                message: t("adminRoles.orgs.idpAliasRequired"),
              },
            ]}
          >
            <Input placeholder="summer-cloud" maxLength={64} />
          </Form.Item>
          <Form.Item
            name="match_type"
            label={t("adminRoles.orgs.matchType")}
            extra={t(`adminRoles.orgs.matchType${matchSuffix(matchType)}Hint`)}
          >
            <Select
              options={MATCH_TYPES.map((value) => ({
                value,
                label: t(`adminRoles.orgs.matchType${matchSuffix(value)}`),
              }))}
            />
          </Form.Item>
          <Form.Item
            name="match_value"
            label={
              matchType === "domain"
                ? t("adminRoles.orgs.matchValueDomain")
                : t("adminRoles.orgs.matchValueClaim")
            }
            extra={
              matchType === "static"
                ? t("adminRoles.orgs.matchValueEmptyForStatic")
                : undefined
            }
            rules={
              matchType === "static"
                ? []
                : [
                    {
                      required: true,
                      message: t("adminRoles.orgs.matchValueRequired"),
                    },
                  ]
            }
          >
            <Input
              placeholder={matchType === "domain" ? "qq.com" : "1"}
              maxLength={128}
              disabled={matchType === "static"}
            />
          </Form.Item>
          <Form.Item name="note" label={t("adminRoles.orgs.note")}>
            <Input maxLength={256} />
          </Form.Item>
          <div className={styles.modalFormActions}>
            <Button
              type="primary"
              icon={<Plus size={14} />}
              loading={bindingSubmitting}
              onClick={() => void addBinding()}
            >
              {t("adminRoles.orgs.addBinding")}
            </Button>
          </div>
        </Form>
      </Modal>
    </div>
  );
}
