import {
  Alert,
  Button,
  Form,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
} from "antd";
import { Pencil, Save, Trash2, Undo2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  rolesApi,
  type AdminUserRow,
  type OrgInfo,
  type OrgMembership,
  type RolePackage,
} from "../../../api/modules/roles";
import { message } from "../../../utils/antdMessage";
import { apiErrorMessage } from "../../../utils/apiError";
import { showConfirmModal } from "../../../utils/confirmModal";
import styles from "./Roles.module.less";
import {
  ListSkeleton,
  LoadError,
  RoleTagList,
  TableEmpty,
  roleOptionLabel,
  useAsyncData,
} from "./shared";

interface GrantFormValues {
  org_id: string;
  role_keys: string[];
  status: string;
}

const STATUS_KEYS: Record<string, string> = {
  active: "adminRoles.membership.statusActive",
  pending: "adminRoles.membership.statusPending",
  suspended: "adminRoles.membership.statusSuspended",
};

const STATUS_COLORS: Record<string, string> = {
  active: "green",
  pending: "orange",
  suspended: "red",
};

const SOURCE_KEYS: Record<string, string> = {
  jit: "adminRoles.membership.sourceJit",
  manual: "adminRoles.membership.sourceManual",
  invite: "adminRoles.membership.sourceInvite",
};

/** Feedback for the most recent successful write. */
interface SavedInfo {
  org: string;
  count: number;
}

interface UserGrantModalProps {
  /** Account being edited; ``null`` keeps the modal closed. */
  user: AdminUserRow | null;
  orgs: OrgInfo[];
  roles: RolePackage[];
  roleLabels: Map<string, string>;
  onClose: () => void;
  /** Called after every successful write, so the list can refresh its summary. */
  onSaved: (username: string) => void;
}

function displayName(user: AdminUserRow): string {
  return user.display_name || user.username;
}

/**
 * Per-user authorization editor.
 *
 * Lists the user's current memberships (one row per tenant) and offers a small
 * form to grant or edit them. A user may hold several ability packages per
 * tenant; the package picker is therefore multi-select.
 *
 * Only the existing admin endpoints are used: a write is
 * ``PUT /admin/users/{username}/membership``, which upserts one membership.
 */
export default function UserGrantModal({
  user,
  orgs,
  roles,
  roleLabels,
  onClose,
  onSaved,
}: UserGrantModalProps) {
  const { t } = useTranslation();
  const [form] = Form.useForm<GrantFormValues>();
  const [editingOrg, setEditingOrg] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedInfo, setSavedInfo] = useState<SavedInfo | null>(null);

  const username = user?.username;

  const memberships = useAsyncData<OrgMembership[]>(
    async () => (username ? rolesApi.listMemberships(username) : []),
    {
      fallbackError: t("adminRoles.membership.loadFailed"),
      t,
      reloadKey: username,
    },
  );

  const rows = useMemo(() => memberships.data ?? [], [memberships.data]);

  /**
   * Rows worth showing: a membership that grants no ability package is not a
   * grant. The write endpoint is an upsert (there is no DELETE), so clearing a
   * row leaves an empty membership behind on the server — listing it would look
   * like a deletion that silently failed.
   */
  const grants = useMemo(
    () => rows.filter((row) => row.role_keys.length > 0),
    [rows],
  );
  const clearedCount = rows.length - grants.length;

  const orgOptions = useMemo(
    () =>
      orgs.map((org) => ({
        value: org.org_id,
        label: `${org.name} (${org.org_id})`,
      })),
    [orgs],
  );

  const roleOptions = useMemo(
    () =>
      roles.map((role) => ({
        value: role.role_key,
        label: roleOptionLabel(role),
      })),
    [roles],
  );

  const statusOptions = useMemo(
    () => [
      { value: "active", label: t("adminRoles.membership.statusActive") },
      { value: "pending", label: t("adminRoles.membership.statusPending") },
      { value: "suspended", label: t("adminRoles.membership.statusSuspended") },
    ],
    [t],
  );

  const orgLabel = useCallback(
    (orgId: string) => orgs.find((org) => org.org_id === orgId)?.name ?? orgId,
    [orgs],
  );

  const resetForm = useCallback(() => {
    setEditingOrg(null);
    form.resetFields();
  }, [form]);

  /**
   * Fresh user ⇒ drop the stale "just saved" banner.
   *
   * The form itself is remounted through its ``key`` (see below), so it starts
   * from ``initialValues`` without us poking an unmounted form instance.
   */
  useEffect(() => {
    setEditingOrg(null);
    setSavedInfo(null);
  }, [username]);

  const editRow = useCallback(
    (row: OrgMembership) => {
      setEditingOrg(row.org_id);
      setSavedInfo(null);
      form.setFieldsValue({
        org_id: row.org_id,
        role_keys: [...row.role_keys],
        status: row.status,
      });
    },
    [form],
  );

  const submit = useCallback(async () => {
    if (!username) return;
    let values: GrantFormValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    setSaving(true);
    try {
      await rolesApi.setMembership(username, {
        org_id: values.org_id,
        role_keys: values.role_keys ?? [],
        status: values.status ?? "active",
      });
      // Count what was submitted, not what the response echoes: the endpoint
      // answers with the user's *refreshed session context*
      // (``refresh_tenant_context``), which is whichever tenant they are active
      // in — often a different one. Pairing that count with the submitted org
      // produced banners like 「已保存：合作商 002 · 3 个角色」 after granting
      // a single role.
      const count = (values.role_keys ?? []).length;
      setSavedInfo({ org: orgLabel(values.org_id), count });
      message.success(t("adminRoles.membership.savedToast"));
      resetForm();
      memberships.reload();
      onSaved(username);
    } catch (err) {
      message.error(apiErrorMessage(err, t("adminRoles.saveFailed"), t));
    } finally {
      setSaving(false);
    }
  }, [form, memberships, onSaved, orgLabel, resetForm, t, username]);

  const confirmRemoveAll = useCallback(
    (row: OrgMembership) => {
      if (!username) return;
      const org = orgLabel(row.org_id);
      // A ``jit`` row is re-expanded from the job → package mapping on the next
      // login, so clearing it here only lasts until then. Say so instead of
      // letting the user believe the packages are gone for good.
      showConfirmModal({
        title: t("adminRoles.membership.removeAllTitle", { org }),
        content: (
          <div>
            <div>
              {t("adminRoles.membership.removeAllBody", {
                name: username,
                org,
              })}
            </div>
            {row.source === "jit" && (
              <div style={{ marginTop: 8 }}>
                {t("adminRoles.membership.removeAllJit")}
              </div>
            )}
          </div>
        ),
        okText: t("adminRoles.membership.removeAll"),
        okType: "danger",
        cancelText: t("adminRoles.cancel"),
        onOk: async () => {
          try {
            await rolesApi.setMembership(username, {
              org_id: row.org_id,
              role_keys: [],
              status: row.status,
            });
            message.success(
              t("adminRoles.membership.removeAllSuccess", { org }),
            );
            if (editingOrg === row.org_id) resetForm();
            memberships.reload();
            onSaved(username);
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
    [editingOrg, memberships, onSaved, orgLabel, resetForm, t, username],
  );

  const firstLoad = memberships.loading && memberships.data === null;

  return (
    <Modal
      open={user !== null}
      title={
        user
          ? t("adminRoles.membership.grantTitle", { name: displayName(user) })
          : ""
      }
      width={760}
      footer={null}
      maskClosable={!saving}
      onCancel={onClose}
    >
      {user && (
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <span className={styles.cellSecondary}>
            {t("adminRoles.membership.grantAccount", {
              username: user.username,
            })}
          </span>

          {memberships.error ? (
            <LoadError
              message={memberships.error}
              onRetry={memberships.reload}
            />
          ) : firstLoad ? (
            <ListSkeleton rows={3} />
          ) : (
            <>
              <div>
                <div className={styles.modalSection}>
                  <span className={styles.modalSectionTitle}>
                    {t("adminRoles.membership.grantCurrent")}
                  </span>
                  <span className={styles.cellSecondary}>
                    {t("adminRoles.membership.grantSummary", {
                      count: grants.length,
                    })}
                  </span>
                </div>

                {savedInfo && (
                  <Alert
                    closable
                    showIcon
                    type="success"
                    style={{ marginBottom: 8 }}
                    onClose={() => setSavedInfo(null)}
                    message={t("adminRoles.membership.savedInline", {
                      org: savedInfo.org,
                      count: savedInfo.count,
                    })}
                  />
                )}

                <Table<OrgMembership>
                  className={styles.modalTable}
                  rowKey="org_id"
                  size="small"
                  loading={memberships.loading}
                  dataSource={grants}
                  pagination={false}
                  scroll={{ y: 220 }}
                  locale={{
                    emptyText: (
                      <TableEmpty
                        title={t("adminRoles.membership.empty")}
                        description={t("adminRoles.membership.emptyHint")}
                      />
                    ),
                  }}
                  columns={[
                    {
                      title: t("adminRoles.membership.org"),
                      dataIndex: "org_id",
                      width: 200,
                      render: (value: string) => (
                        <div className={styles.cellStack}>
                          <span className={styles.cellPrimary}>
                            {orgLabel(value)}
                          </span>
                          <span className={styles.mono}>{value}</span>
                        </div>
                      ),
                    },
                    {
                      title: t("adminRoles.membership.roleKeys"),
                      dataIndex: "role_keys",
                      render: (keys: string[]) => (
                        <RoleTagList
                          roleKeys={keys}
                          labels={roleLabels}
                          emptyText={t("adminRoles.membership.noRoles")}
                        />
                      ),
                    },
                    {
                      title: t("adminRoles.membership.status"),
                      dataIndex: "status",
                      width: 96,
                      render: (status: string) => (
                        <Tag color={STATUS_COLORS[status] ?? "default"}>
                          {STATUS_KEYS[status]
                            ? t(STATUS_KEYS[status])
                            : status}
                        </Tag>
                      ),
                    },
                    {
                      title: t("adminRoles.membership.source"),
                      dataIndex: "source",
                      width: 96,
                      render: (source: string) =>
                        SOURCE_KEYS[source] ? t(SOURCE_KEYS[source]) : source,
                    },
                    {
                      title: t("adminRoles.actions"),
                      key: "actions",
                      width: 168,
                      align: "right",
                      render: (_, record) => (
                        <Space size={0}>
                          <Tooltip title={t("adminRoles.membership.editRow")}>
                            <Button
                              size="small"
                              type="link"
                              aria-label={t("adminRoles.membership.editRow")}
                              icon={<Pencil size={13} />}
                              onClick={() => editRow(record)}
                            />
                          </Tooltip>
                          <Tooltip title={t("adminRoles.membership.removeAll")}>
                            <Button
                              size="small"
                              type="link"
                              danger
                              aria-label={t("adminRoles.membership.removeAll")}
                              icon={<Trash2 size={13} />}
                              onClick={() => confirmRemoveAll(record)}
                            />
                          </Tooltip>
                        </Space>
                      ),
                    },
                  ]}
                />

                {clearedCount > 0 && (
                  // Explain the rows the table is not showing, so a cleared
                  // grant does not look like data going missing.
                  <div className={styles.modalHint} style={{ marginTop: 8 }}>
                    {t("adminRoles.membership.clearedNote", {
                      count: clearedCount,
                    })}
                  </div>
                )}
              </div>

              <div className={styles.modalDivider} />

              <div>
                <div className={styles.modalSection}>
                  <span className={styles.modalSectionTitle}>
                    {editingOrg
                      ? t("adminRoles.membership.editSection", {
                          org: orgLabel(editingOrg),
                        })
                      : t("adminRoles.membership.addSection")}
                  </span>
                  {editingOrg && (
                    <Button
                      size="small"
                      type="link"
                      icon={<Undo2 size={13} />}
                      onClick={resetForm}
                    >
                      {t("adminRoles.membership.resetForm")}
                    </Button>
                  )}
                </div>

                {/* Remount per user so the fields always start clean. */}
                <Form
                  key={username ?? "none"}
                  form={form}
                  layout="vertical"
                  requiredMark
                  initialValues={{ role_keys: [], status: "active" }}
                >
                  <Form.Item
                    name="org_id"
                    label={t("adminRoles.membership.pickOrg")}
                    rules={[
                      {
                        required: true,
                        message: t("adminRoles.membership.pickOrgRequired"),
                      },
                    ]}
                  >
                    <Select
                      showSearch
                      optionFilterProp="label"
                      disabled={editingOrg !== null}
                      placeholder={t("adminRoles.membership.pickOrg")}
                      options={orgOptions}
                    />
                  </Form.Item>
                  <Form.Item
                    name="role_keys"
                    label={t("adminRoles.membership.pickRoles")}
                    extra={t("adminRoles.membership.rolesHint")}
                  >
                    <Select
                      mode="multiple"
                      allowClear
                      showSearch
                      optionFilterProp="label"
                      maxTagCount="responsive"
                      placeholder={t("adminRoles.membership.pickRoles")}
                      options={roleOptions}
                    />
                  </Form.Item>
                  <Form.Item
                    name="status"
                    label={t("adminRoles.membership.status")}
                  >
                    <Select options={statusOptions} />
                  </Form.Item>
                  <div className={styles.modalFormActions}>
                    <Button onClick={resetForm}>{t("common.reset")}</Button>
                    <Button
                      type="primary"
                      icon={<Save size={14} />}
                      loading={saving}
                      onClick={() => void submit()}
                    >
                      {t("adminRoles.membership.save")}
                    </Button>
                  </div>
                </Form>
              </div>
            </>
          )}
        </Space>
      )}
    </Modal>
  );
}
