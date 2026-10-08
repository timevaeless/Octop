import {
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
} from "antd";
import { Pencil, Plus, RefreshCw, Search, Trash2 } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { orgAdminApi } from "../../../api/modules/orgAdmin";
import { rolesApi, type RolePackage } from "../../../api/modules/roles";
import { message } from "../../../utils/antdMessage";
import { apiErrorMessage } from "../../../utils/apiError";
import styles from "./Roles.module.less";
import {
  ListSkeleton,
  LoadError,
  TableEmpty,
  isPlatformSystemPackage,
  useAsyncData,
  useTableArea,
  useTablePagination,
} from "./shared";

/** Backend contract: lowercase letters, digits and underscores only. */
const ROLE_KEY_PATTERN = /^[a-z0-9_]+$/;

/** One row of the packages table — the tenant face adds the display key. */
type PackageRow = RolePackage & { bare_key?: string };

interface PackageFormValues {
  role_key: string;
  name_zh: string;
  description?: string;
  /** Marks the package as the default minimum for an unseen role code. */
  is_default?: boolean;
}

/**
 * @param onDataChanged called after every successful create/update/delete so
 *   the sibling tabs (mappings, members, ACL) can refetch their role pools —
 *   antd keeps their panes mounted, so they would otherwise serve stale
 *   dropdown options until a page reload.
 */
export default function PackagesTab({
  tenantOrgId,
  onDataChanged,
}: {
  tenantOrgId?: string;
  onDataChanged?: () => void;
}) {
  const { t } = useTranslation();
  const [form] = Form.useForm<PackageFormValues>();
  const [keyword, setKeyword] = useState("");
  const [mode, setMode] = useState<"create" | "edit" | null>(null);
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const pagination = useTablePagination(10);
  const { areaRef, scrollY } = useTableArea();

  const { data, loading, error, reload } = useAsyncData<PackageRow[]>(
    () =>
      tenantOrgId
        ? orgAdminApi.listRoles(tenantOrgId)
        : rolesApi.list(),
    {
      fallbackError: t("adminRoles.packages.loadFailed"),
      t,
      reloadKey: tenantOrgId ?? "",
    },
  );

  const roles = useMemo(() => data ?? [], [data]);

  // Tenant face: platform-owned system packages (member/org_admin) are the
  // platform's designation + JIT machinery — hidden from the tenant's table.
  // The raw list stays intact for the counts below only in the platform face.
  const visibleRoles = useMemo(
    () =>
      tenantOrgId
        ? roles.filter((role) => !isPlatformSystemPackage(role))
        : roles,
    [roles, tenantOrgId],
  );

  const filtered = useMemo(() => {
    const needle = keyword.trim().toLowerCase();
    if (!needle) return visibleRoles;
    return visibleRoles.filter((role) =>
      [role.role_key, role.bare_key ?? "", role.name_zh, role.description ?? ""]
        .join(" ")
        .toLowerCase()
        .includes(needle),
    );
  }, [visibleRoles, keyword]);

  const counts = useMemo(
    () => ({
      total: visibleRoles.length,
      platform: visibleRoles.filter((role) => role.org_id === null).length,
      tenant: visibleRoles.filter((role) => role.org_id !== null).length,
    }),
    [visibleRoles],
  );

  const openCreate = useCallback(() => {
    setEditingKey(null);
    form.resetFields();
    form.setFieldsValue({
      description: "",
      is_default: false,
    });
    setMode("create");
  }, [form]);

  const openEdit = useCallback(
    (role: PackageRow) => {
      setEditingKey(role.role_key);
      form.resetFields();
      form.setFieldsValue({
        role_key: role.bare_key ?? role.role_key,
        name_zh: role.name_zh,
        description: role.description ?? "",
        is_default: role.is_default,
      });
      setMode("edit");
    },
    [form],
  );

  const confirmRemove = useCallback(
    (role: PackageRow) => {
      const remove = tenantOrgId
        ? orgAdminApi.deleteRole(tenantOrgId, role.role_key)
        : rolesApi.remove(role.role_key);
      void remove
        .then(() => {
          message.success(t("adminRoles.packages.deleted", { name: role.name_zh }));
          reload();
          onDataChanged?.();
        })
        .catch((err: unknown) => {
          message.error(apiErrorMessage(err, t("adminRoles.deleteFailed"), t));
        });
    },
    [onDataChanged, reload, t, tenantOrgId],
  );

  const submit = useCallback(async () => {
    // Validation failures are surfaced inline by the form rules — no toast.
    let values: PackageFormValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    setSubmitting(true);
    try {
      if (mode === "edit" && editingKey) {
        if (tenantOrgId) {
          await orgAdminApi.updateRole(tenantOrgId, editingKey, {
            name_zh: values.name_zh,
            description: values.description ?? "",
            is_default: values.is_default ?? false,
          });
        } else {
          await rolesApi.update(editingKey, {
            name_zh: values.name_zh,
            description: values.description ?? "",
            is_default: values.is_default ?? false,
          });
        }
        message.success(
          t("adminRoles.packages.updated", { name: values.name_zh }),
        );
      } else if (tenantOrgId) {
        // The storage prefix (t_{org}_) is minted server-side from the path
        // org — the client only ever sends the display key.
        await orgAdminApi.createRole(tenantOrgId, {
          bare_key: values.role_key,
          name_zh: values.name_zh,
          description: values.description ?? "",
          is_default: values.is_default ?? false,
        });
        message.success(
          t("adminRoles.packages.created", { name: values.name_zh }),
        );
      } else {
        await rolesApi.create({
          role_key: values.role_key,
          name_zh: values.name_zh,
          description: values.description ?? "",
          is_default: values.is_default ?? false,
        });
        message.success(
          t("adminRoles.packages.created", { name: values.name_zh }),
        );
      }
      setMode(null);
      setEditingKey(null);
      reload();
      onDataChanged?.();
    } catch (err) {
      message.error(apiErrorMessage(err, t("adminRoles.saveFailed"), t));
    } finally {
      setSubmitting(false);
    }
  }, [editingKey, form, mode, onDataChanged, reload, t, tenantOrgId]);

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
              placeholder={t("adminRoles.packages.searchPlaceholder")}
              prefix={<Search size={14} />}
              style={{ maxWidth: 320 }}
            />
            <span className={styles.cellSecondary}>
              {t("adminRoles.packages.summary", {
                total: counts.total,
                platform: counts.platform,
                tenant: counts.tenant,
              })}
            </span>
            <div className={styles.toolbarEnd}>
              <Button
                icon={<RefreshCw size={14} />}
                onClick={reload}
                loading={loading && !firstLoad}
              >
                {t("adminRoles.refresh")}
              </Button>
              <Button
                type="primary"
                icon={<Plus size={14} />}
                onClick={openCreate}
              >
                {t("adminRoles.packages.create")}
              </Button>
            </div>
          </div>

          <div className={styles.tableArea} ref={areaRef}>
            <Table<PackageRow>
              rowKey="role_key"
              size="small"
              loading={loading}
              dataSource={filtered}
              pagination={pagination}
              scroll={
                scrollY === undefined ? { x: 980 } : { x: 980, y: scrollY }
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
                  // Name first: it is what an operator recognises at a glance;
                  // the machine-readable role_key follows it.
                  title: t("adminRoles.packages.name"),
                  dataIndex: "name_zh",
                  width: 200,
                  render: (name: string, record) => (
                    <Space size={6}>
                      <span className={styles.cellPrimary}>{name}</span>
                      {record.is_default && (
                        <Tag color="blue">
                          {t("adminRoles.packages.defaultTag")}
                        </Tag>
                      )}
                      {record.is_system && (
                        <Tag color="gold">
                          {t("adminRoles.packages.systemTag")}
                        </Tag>
                      )}
                    </Space>
                  ),
                },
                {
                  title: t("adminRoles.packages.key"),
                  dataIndex: "role_key",
                  width: 230,
                  render: (roleKey: string, record) =>
                    record.bare_key && record.bare_key !== roleKey ? (
                      <Tooltip title={roleKey}>
                        <span className={styles.mono}>{record.bare_key}</span>
                      </Tooltip>
                    ) : (
                      <span className={styles.mono}>{roleKey}</span>
                    ),
                },
                {
                  title: t("adminRoles.packages.scope"),
                  dataIndex: "org_id",
                  width: 110,
                  render: (orgId: string | null) =>
                    orgId === null ? (
                      <Tag color="purple">
                        {t("adminRoles.packages.scopePlatform")}
                      </Tag>
                    ) : (
                      <Tag color="blue">
                        {t("adminRoles.packages.scopeTenant")}
                      </Tag>
                    ),
                },
                {
                  title: t("adminRoles.packages.description"),
                  dataIndex: "description",
                  ellipsis: true,
                  render: (description: string) => description || "—",
                },
                {
                  title: t("adminRoles.actions"),
                  key: "actions",
                  width: 150,
                  align: "right",
                  render: (_, record) => {
                    // System rows (org_admin/member) are platform property:
                    // read-only for the tenant face, undeletable everywhere.
                    const editable = tenantOrgId
                      ? record.org_id !== null && !record.is_system
                      : true;
                    const deletable = !record.is_system;
                    return (
                      <div className={styles.rowActions}>
                        <Button
                          size="small"
                          type="link"
                          icon={<Pencil size={13} />}
                          disabled={!editable}
                          onClick={() => openEdit(record)}
                        >
                          {t("adminRoles.edit")}
                        </Button>
                        {deletable && (
                          <Popconfirm
                            title={t("adminRoles.packages.deleteTitle", {
                              name: record.name_zh,
                            })}
                            description={t("adminRoles.packages.deleteHint")}
                            okText={t("adminRoles.delete")}
                            okButtonProps={{ danger: true }}
                            cancelText={t("adminRoles.cancel")}
                            onConfirm={() => confirmRemove(record)}
                          >
                            <Button
                              size="small"
                              danger
                              type="link"
                              icon={<Trash2 size={13} />}
                            >
                              {t("adminRoles.delete")}
                            </Button>
                          </Popconfirm>
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

      <Modal
        open={mode !== null}
        title={
          mode === "edit"
            ? t("adminRoles.packages.editTitle")
            : t("adminRoles.packages.create")
        }
        okText={t("adminRoles.save")}
        cancelText={t("adminRoles.cancel")}
        confirmLoading={submitting}
        onOk={() => void submit()}
        onCancel={() => {
          setMode(null);
          setEditingKey(null);
        }}
      >
        <Form form={form} layout="vertical" requiredMark>
          <Form.Item
            name="role_key"
            label={t("adminRoles.packages.key")}
            extra={
              mode === "edit"
                ? t("adminRoles.packages.keyImmutable")
                : tenantOrgId
                ? t("adminRoles.packages.keyTenantHint")
                : t("adminRoles.packages.keyHint")
            }
            rules={[
              {
                required: true,
                message: t("adminRoles.packages.keyRequired"),
              },
              {
                pattern: ROLE_KEY_PATTERN,
                message: t("adminRoles.packages.keyPattern"),
              },
            ]}
          >
            <Input
              disabled={mode === "edit"}
              placeholder="expert_user"
              maxLength={64}
            />
          </Form.Item>
          <Form.Item
            name="name_zh"
            label={t("adminRoles.packages.name")}
            rules={[
              {
                required: true,
                message: t("adminRoles.packages.nameRequired"),
              },
            ]}
          >
            <Input
              placeholder={t("adminRoles.packages.namePlaceholder")}
              maxLength={64}
            />
          </Form.Item>
          <Form.Item
            name="description"
            label={t("adminRoles.packages.description")}
          >
            <Input.TextArea
              rows={2}
              maxLength={200}
              showCount
              placeholder={t("adminRoles.packages.descriptionPlaceholder")}
            />
          </Form.Item>
          <Form.Item
            name="is_default"
            label={t("adminRoles.packages.defaultTag")}
            valuePropName="checked"
            extra={t("adminRoles.packages.isDefaultHint")}
          >
            <Switch size="small" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
