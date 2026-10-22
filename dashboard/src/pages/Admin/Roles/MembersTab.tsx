import { Button, Form, Modal, Select, Space, Table, Tag } from "antd";
import { RefreshCw, Settings2, UserX, UserCheck } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { MouseEvent as ReactMouseEvent, ReactNode } from "react";
import { useTranslation } from "react-i18next";
import {
  orgMembersApi,
  type OrgMember,
  type OrgRefs,
} from "../../../api/modules/orgMembers";
import { useCurrentUser } from "../../../hooks/useCurrentUser";
import { message } from "../../../utils/antdMessage";
import { apiErrorMessage } from "../../../utils/apiError";
import { showConfirmModal } from "../../../utils/confirmModal";
import styles from "./Roles.module.less";
import {
  ListSkeleton,
  LoadError,
  RoleTagList,
  TableEmpty,
  useAsyncData,
  useOrgRoleRefs,
  useRoleLabels,
  useTableArea,
  useTablePagination,
} from "./shared";

/**
 * T4-2: the tenant-member roster. Gate lives server-side (platform admin or
 * an active ``org_admin`` membership in the selected org); the tab merely
 * mirrors it by offering only active tenants. Suspension flips the account
 * flag (``users.disabled``), not the membership — the JIT re-expansion would
 * otherwise resurrect a suspended membership on the next login.
 *
 * 028: an org_admin caller is locked to ``/auth/me``'s ``org_id`` and feeds
 * its pickers from the per-tenant refs endpoint; a platform admin keeps the
 * tenant picker and the global refs pair.
 */
/**
 * @param reloadKey bumped by the packages tab after a write — refetches the
 *   refs so a just-created package appears in the role picker immediately.
 */
export default function MembersTab({ reloadKey = 0 }: { reloadKey?: number }) {
  const { t } = useTranslation();
  const user = useCurrentUser();
  const isPlatformAdmin = user?.role === "admin";
  const { areaRef, scrollY } = useTableArea();
  const pagination = useTablePagination(10);
  const [orgId, setOrgId] = useState<string | null>(null);
  const [editing, setEditing] = useState<OrgMember | null>(null);
  // 028: an org_admin caller is locked to their own tenant — the global
  // ``/api/roles`` + ``/api/orgs`` pair is require_admin, so the pickers
  // feed from the per-tenant refs endpoint instead.
  const lockedOrgId = isPlatformAdmin ? null : user?.org_id ?? null;
  const refs = useOrgRoleRefs(isPlatformAdmin, reloadKey);
  const tenantRefs = useAsyncData<OrgRefs | null>(
    () =>
      lockedOrgId ? orgMembersApi.refs(lockedOrgId) : Promise.resolve(null),
    {
      fallbackError: t("adminRoles.loadFailed"),
      t,
      reloadKey: `${lockedOrgId}:${reloadKey}`,
    },
  );

  // Lock the non-admin caller onto their own tenant once /auth/me resolves.
  useEffect(() => {
    if (!isPlatformAdmin && lockedOrgId) setOrgId(lockedOrgId);
  }, [isPlatformAdmin, lockedOrgId]);

  const members = useAsyncData<OrgMember[]>(
    () => (orgId ? orgMembersApi.list(orgId) : Promise.resolve([])),
    {
      fallbackError: t("adminRoles.members.loadFailed"),
      t,
      reloadKey: `${orgId}:${reloadKey}`,
    },
  );

  const refresh = useCallback(() => {
    refs.reload();
    tenantRefs.reload();
    members.reload();
  }, [members, refs, tenantRefs]);

  const rolesPool = isPlatformAdmin ? refs.data?.roles : tenantRefs.data?.roles;
  // refs now ships the full visible pool (system rows flagged ``is_system``)
  // so ``labels`` can render existing memberships like member/org_admin with
  // their Chinese names. The grant picker itself still excludes them — they
  // are the platform's designation + JIT machinery, not tenant-grantable.
  const labels = useRoleLabels(rolesPool ?? []);
  const activeOrgs = useMemo(() => {
    if (isPlatformAdmin)
      return (refs.data?.orgs ?? []).filter((o) => o.status === "active");
    const own = tenantRefs.data?.org;
    return own && own.status === "active" ? [own] : [];
  }, [isPlatformAdmin, refs.data, tenantRefs.data]);

  const onSelectOrg = useCallback((next: string) => {
    setOrgId(next);
  }, []);

  const refsLoading = isPlatformAdmin ? refs.loading : tenantRefs.loading;
  const refsError = isPlatformAdmin ? refs.error : tenantRefs.error;
  const refsRetry = isPlatformAdmin ? refs.reload : tenantRefs.reload;

  const confirmSuspend = useCallback(
    (m: OrgMember) => {
      if (!orgId) return;
      showConfirmModal({
        title: t("adminRoles.members.suspendTitle", {
          user: m.display_name || m.username,
        }),
        content: t("adminRoles.members.suspendContent"),
        okText: t("adminRoles.members.suspend"),
        okButtonProps: { danger: true },
        onOk: async () => {
          try {
            await orgMembersApi.suspend(orgId, m.user_id);
            message.success(t("adminRoles.members.suspendSuccess"));
            members.reload();
          } catch (err) {
            message.error(
              apiErrorMessage(err, t("adminRoles.members.actionFailed"), t),
            );
          }
        },
      });
    },
    [members, orgId, t],
  );

  const confirmReactivate = useCallback(
    async (m: OrgMember) => {
      if (!orgId) return;
      try {
        await orgMembersApi.reactivate(orgId, m.user_id);
        message.success(t("adminRoles.members.reactivateSuccess"));
        members.reload();
      } catch (err) {
        message.error(
          apiErrorMessage(err, t("adminRoles.members.actionFailed"), t),
        );
      }
    },
    [members, orgId, t],
  );

  if (refsLoading && !orgId) return <ListSkeleton />;
  if (refsError) return <LoadError message={refsError} onRetry={refsRetry} />;

  const rows = members.data ?? [];

  /* Layout contract shared with the other tabs (OrgsTab/MembershipTab/…):
     the pane (.tabBody in index.tsx) is a fixed-height flex column; this
     .panel hands the leftover height to .tableArea, and useTableArea sizes
     the table body against it. The toolbar MUST stay outside .tableArea —
     TABLE_CHROME only budgets header + pagination, so a toolbar inside the
     measured area overflows it and overflow:hidden clips the pagination
     strip out of view ("the scrollbar is broken"). */
  return (
    <div className={styles.panel}>
      <Space style={{ width: "100%", justifyContent: "space-between" }}>
        {isPlatformAdmin ? (
          <Select
            showSearch
            optionFilterProp="value"
            style={{ minWidth: 280 }}
            value={orgId ?? undefined}
            onChange={onSelectOrg}
            options={activeOrgs.map((o) => ({
              value: o.org_id,
              label: `${o.name} (${o.org_id})`,
            }))}
            placeholder={t("adminRoles.members.orgPlaceholder")}
          />
        ) : activeOrgs[0] ? (
          <Select
            style={{ minWidth: 280 }}
            value={activeOrgs[0].org_id}
            disabled
            options={[
              {
                value: activeOrgs[0].org_id,
                label: `${activeOrgs[0].name} (${activeOrgs[0].org_id})`,
              },
            ]}
          />
        ) : null}
        <Button icon={<RefreshCw size={14} />} onClick={refresh}>
          {t("adminRoles.members.refresh")}
        </Button>
      </Space>

      {!orgId ? (
        <TableEmpty
          title={t("adminRoles.members.pickOrgTitle")}
          description={t("adminRoles.members.pickOrgHint")}
        />
      ) : members.loading ? (
        <ListSkeleton />
      ) : members.error ? (
        <LoadError message={members.error} onRetry={members.reload} />
      ) : (
        <div className={styles.tableArea} ref={areaRef}>
          <Table<OrgMember>
            rowKey="user_id"
            size="middle"
            dataSource={rows}
            scroll={scrollY === undefined ? { x: 960 } : { x: 960, y: scrollY }}
            pagination={pagination}
            locale={{
              emptyText: (
                <TableEmpty
                  title={t("adminRoles.members.emptyTitle")}
                  description={t("adminRoles.members.emptyHint")}
                />
              ),
            }}
            columns={[
              {
                title: t("adminRoles.members.columnUser"),
                key: "user",
                width: 200,
                render: (_, m) => (
                  <div>
                    <div>
                      {m.display_name || m.username}
                      {m.is_default_org ? (
                        <Tag style={{ marginLeft: 8 }}>
                          {t("adminRoles.members.defaultOrg")}
                        </Tag>
                      ) : null}
                    </div>
                    <div style={{ opacity: 0.6, fontSize: 12 }}>
                      @{m.username}
                    </div>
                  </div>
                ),
              },
              {
                title: t("adminRoles.members.columnStatus"),
                key: "status",
                width: 120,
                render: (_, m) =>
                  m.disabled ? (
                    <Tag color="red">
                      {t("adminRoles.members.statusDisabled")}
                    </Tag>
                  ) : m.status === "active" ? (
                    <Tag color="green">
                      {t("adminRoles.members.statusActive")}
                    </Tag>
                  ) : (
                    <Tag>{m.status}</Tag>
                  ),
              },
              {
                title: t("adminRoles.members.columnRoles"),
                key: "roles",
                width: 320,
                render: (_, m) => (
                  <RoleTagList
                    roleKeys={m.role_keys}
                    labels={labels}
                    emptyText={t("adminRoles.members.noRoles")}
                  />
                ),
              },
              {
                title: t("adminRoles.members.columnSource"),
                key: "source",
                width: 100,
                render: (_, m) => <Tag>{m.source}</Tag>,
              },
              {
                title: t("adminRoles.members.columnActions"),
                key: "actions",
                width: 200,
                fixed: "right",
                render: (_, m) =>
                  m.user_role === "admin" ? null : (
                    <Space size={4}>
                      <Button
                        size="small"
                        icon={<Settings2 size={14} />}
                        onClick={() => setEditing(m)}
                      >
                        {t("adminRoles.members.editRoles")}
                      </Button>
                      {m.disabled ? (
                        <Button
                          size="small"
                          icon={<UserCheck size={14} />}
                          onClick={() => void confirmReactivate(m)}
                        >
                          {t("adminRoles.members.reactivate")}
                        </Button>
                      ) : (
                        <Button
                          size="small"
                          danger
                          icon={<UserX size={14} />}
                          onClick={() => confirmSuspend(m)}
                        >
                          {t("adminRoles.members.suspend")}
                        </Button>
                      )}
                    </Space>
                  ),
              },
            ]}
          />
        </div>
      )}

      <EditRolesModal
        orgId={orgId}
        member={editing}
        allRoles={rolesPool ?? []}
        onClose={() => setEditing(null)}
        onSaved={() => {
          setEditing(null);
          members.reload();
        }}
      />
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * Edit-roles modal (grant/revoke packages, tenant scope only)
 * ------------------------------------------------------------------ */

function EditRolesModal({
  orgId,
  member,
  allRoles,
  onClose,
  onSaved,
}: {
  orgId: string | null;
  member: OrgMember | null;
  /** Full visible pool — system rows flagged ``is_system`` included. */
  allRoles: { role_key: string; name_zh: string; is_system?: boolean }[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm<{ role_keys?: string[] }>();

  // Picker options = the grantable pool plus — locked — the system packages
  // this member already holds (``member``/``org_admin``). Without the locked
  // rows the pre-filled Select would render them as bare-key tags, and the
  // server rejects granting or revoking them through this endpoint anyway
  // (they are the platform's designation + JIT machinery, not tenant-ruled).
  const options = useMemo(() => {
    const held = new Set(member?.role_keys ?? []);
    return allRoles
      .filter((r) => !r.is_system || held.has(r.role_key))
      .map((r) => ({
        value: r.role_key,
        label: `${r.name_zh} (${r.role_key})`,
        disabled: r.is_system === true,
      }));
  }, [allRoles, member]);

  // A locked system package must not be removable from the form value — the
  // server passes it through untouched, so a removed-then-saved tag would
  // silently reappear. Hide the close affordance on those tags.
  const tagRender = useCallback(
    (props: {
      label?: ReactNode;
      value?: string | number;
      closable?: boolean;
      onClose?: (e: ReactMouseEvent<HTMLElement>) => void;
    }) => {
      const locked = options.some((o) => o.value === props.value && o.disabled);
      return (
        <Tag
          closable={props.closable === true && !locked}
          onClose={props.onClose}
          style={{ marginInlineEnd: 4 }}
        >
          {props.label}
        </Tag>
      );
    },
    [options],
  );

  // Re-seed the picker whenever a (different) member opens the modal;
  // ``destroyOnHidden`` handles the unmount side.
  useEffect(() => {
    if (member) {
      form.setFieldsValue({ role_keys: member.role_keys });
    }
  }, [member, form]);

  const save = useCallback(
    async (values: { role_keys?: string[] }) => {
      if (!orgId || !member) return;
      // P1 guard: allowClear (or any other clearing path) can sweep the
      // locked system packages out of the form value in one click — re-union
      // them server-bound so the payload keeps matching the locked-tag
      // semantics whatever the user did in the picker.
      const locked = new Set(
        options.filter((o) => o.disabled).map((o) => String(o.value)),
      );
      const keys = Array.from(
        new Set([...(values.role_keys ?? []), ...locked]),
      );
      setSaving(true);
      try {
        await orgMembersApi.setRoles(orgId, member.user_id, keys);
        message.success(t("adminRoles.members.saveSuccess"));
        onSaved();
      } catch (err) {
        message.error(
          apiErrorMessage(err, t("adminRoles.members.saveFailed"), t),
        );
      } finally {
        setSaving(false);
      }
    },
    [member, onSaved, options, orgId, t],
  );

  return (
    <Modal
      open={member !== null}
      title={t("adminRoles.members.editTitle", {
        user: member?.display_name || member?.username || "",
      })}
      okText={t("adminRoles.members.save")}
      confirmLoading={saving}
      onCancel={onClose}
      onOk={() => form.submit()}
      destroyOnHidden
    >
      <Form form={form} layout="vertical" onFinish={save}>
        <Form.Item name="role_keys" label={t("adminRoles.members.rolesLabel")}>
          <Select
            mode="multiple"
            allowClear
            options={options}
            tagRender={tagRender}
          />
        </Form.Item>
        <div style={{ opacity: 0.65, fontSize: 12 }}>
          {t("adminRoles.members.editExtra")}
        </div>
      </Form>
    </Modal>
  );
}
