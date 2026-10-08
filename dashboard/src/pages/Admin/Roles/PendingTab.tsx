import { Button, Form, Modal, Select, Table, Tag } from "antd";
import { RefreshCw, UserPlus } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { pendingUsersApi, type PendingUser } from "../../../api/modules/pendingUsers";
import { message } from "../../../utils/antdMessage";
import { apiErrorMessage } from "../../../utils/apiError";
import styles from "./Roles.module.less";
import {
  ListSkeleton,
  LoadError,
  TableEmpty,
  roleOptionLabel,
  useAsyncData,
  useOrgRoleRefs,
  useTableArea,
  useTablePagination,
} from "./shared";

interface AssignFormValues {
  org_id: string;
  role_keys?: string[];
}

function formatTime(value: number): string {
  const d = new Date(value * 1000);
  return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString();
}

/**
 * T5-4: the "pending users" backlog — SSO logins the five-level resolution
 * chain could not place (fail-closed), now discoverable and assignable.
 *
 * The list is derived server-side (SSO users whose ``default_org_id`` is
 * still NULL), so a successful assign makes the row disappear on refresh.
 */
export default function PendingTab() {
  const { t } = useTranslation();
  const { areaRef, scrollY } = useTableArea();
  const pagination = useTablePagination(10);
  const [assignOpen, setAssignOpen] = useState(false);
  const [target, setTarget] = useState<PendingUser | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [form] = Form.useForm<AssignFormValues>();

  const backlog = useAsyncData<PendingUser[]>(() => pendingUsersApi.list(), {
    fallbackError: t("adminRoles.pending.loadFailed"),
    t,
  });
  const refs = useOrgRoleRefs();

  const refresh = useCallback(() => backlog.reload(), [backlog]);

  const openAssign = useCallback(
    (user: PendingUser) => {
      setTarget(user);
      form.resetFields();
      setAssignOpen(true);
    },
    [form],
  );

  const activeOrgs = useMemo(
    () => (refs.data?.orgs ?? []).filter((o) => o.status === "active"),
    [refs.data],
  );

  const submitAssign = useCallback(
    async (values: AssignFormValues) => {
      if (!target) return;
      setSubmitting(true);
      try {
        await pendingUsersApi.assign(target.id, {
          org_id: values.org_id,
          role_keys: values.role_keys ?? [],
        });
        message.success(t("adminRoles.pending.assignSuccess"));
        setAssignOpen(false);
        refresh();
      } catch (err) {
        message.error(apiErrorMessage(err, t("adminRoles.pending.assignFailed"), t));
      } finally {
        setSubmitting(false);
      }
    },
    [target, refresh, t],
  );

  if (backlog.loading) return <ListSkeleton />;
  if (backlog.error) return <LoadError message={backlog.error} onRetry={refresh} />;

  const rows = backlog.data ?? [];

  return (
    <div className={styles.tableArea} ref={areaRef}>
      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 8 }}>
        <Button icon={<RefreshCw size={14} />} onClick={refresh}>
          {t("adminRoles.pending.refresh")}
        </Button>
      </div>
      <Table<PendingUser>
        rowKey="id"
        size="middle"
        dataSource={rows}
        scroll={scrollY === undefined ? { x: 720 } : { x: 720, y: scrollY }}
        pagination={pagination}
        locale={{
          emptyText: (
            <TableEmpty
              title={t("adminRoles.pending.emptyTitle")}
              description={t("adminRoles.pending.emptyHint")}
            />
          ),
        }}
        columns={[
          {
            title: t("adminRoles.pending.columnUser"),
            key: "user",
            width: 220,
            render: (_, u) => (
              <div>
                <div>{u.display_name || u.username}</div>
                <div style={{ opacity: 0.6, fontSize: 12 }}>@{u.username}</div>
              </div>
            ),
          },
          {
            title: t("adminRoles.pending.columnEmail"),
            key: "email",
            width: 200,
            render: (_, u) => u.email || "—",
          },
          {
            title: t("adminRoles.pending.columnIdp"),
            key: "idp",
            width: 120,
            render: (_, u) => <Tag>{u.idp_alias}</Tag>,
          },
          {
            title: t("adminRoles.pending.columnCreated"),
            key: "created",
            width: 180,
            render: (_, u) => formatTime(u.created_at),
          },
          {
            title: t("adminRoles.pending.columnActions"),
            key: "actions",
            width: 120,
            fixed: "right",
            render: (_, u) => (
              <Button
                size="small"
                type="primary"
                icon={<UserPlus size={14} />}
                onClick={() => openAssign(u)}
              >
                {t("adminRoles.pending.assign")}
              </Button>
            ),
          },
        ]}
      />

      <Modal
        open={assignOpen}
        title={t("adminRoles.pending.assignTitle", { user: target?.username ?? "" })}
        okText={t("adminRoles.pending.assign")}
        confirmLoading={submitting}
        onCancel={() => setAssignOpen(false)}
        onOk={() => form.submit()}
        destroyOnHidden
      >
        <Form form={form} layout="vertical" onFinish={submitAssign}>
          <Form.Item
            name="org_id"
            label={t("adminRoles.pending.orgLabel")}
            rules={[{ required: true, message: t("adminRoles.pending.orgRequired") }]}
          >
            <Select
              showSearch
              optionFilterProp="value"
              options={activeOrgs.map((o) => ({
                value: o.org_id,
                label: `${o.name} (${o.org_id})`,
              }))}
              placeholder={t("adminRoles.pending.orgPlaceholder")}
            />
          </Form.Item>
          <Form.Item
            name="role_keys"
            label={t("adminRoles.pending.rolesLabel")}
            extra={t("adminRoles.pending.rolesExtra")}
          >
            <Select
              mode="multiple"
              allowClear
              options={(refs.data?.roles ?? []).map((r) => ({
                value: r.role_key,
                label: roleOptionLabel(r),
              }))}
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
