import { Button, Dropdown, Input, Modal, Tag, Tooltip } from "antd";
import type { MenuProps } from "antd";
import {
  CloudDownload,
  CloudUpload,
  Copy,
  FileClock,
  FilterX,
  MoreHorizontal,
  Pencil,
  Plug,
  Plus,
  Search,
  Settings2,
  Trash2,
  Wrench,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type {
  McpCatalogService,
  McpPublishDiff,
  McpPublishResult,
  McpServiceStatus,
} from "../../../api/modules/mcpCatalog";
import { mcpCatalogApi } from "../../../api/modules/mcpCatalog";
import type { RolePackage } from "../../../api/modules/roles";
import { rolesApi } from "../../../api/modules/roles";
import { EmptyState } from "../../../components/EmptyState";
import { ResizableTable } from "../../../components/ResizableTable";
import PageShell from "../../../layouts/PageShell";
import { message } from "../../../utils/antdMessage";
import { apiErrorMessage } from "../../../utils/apiError";
import { showConfirmModal } from "../../../utils/confirmModal";
import { copyText } from "../../../utils/copyText";
import { LoadError, useRoleLabels } from "../Roles/shared";
import styles from "./McpCatalog.module.less";
import PublishDiffModal from "./PublishDiffModal";
import PublishLogModal from "./PublishLogModal";
import PublishResultModal from "./PublishResultModal";
import ServiceFormDrawer from "./ServiceFormDrawer";
import ToolsDrawer from "./ToolsDrawer";

type StatusFilter = "all" | McpServiceStatus;

const STATUS_TONE: Record<McpServiceStatus, string> = {
  draft: "toneDraft",
  published: "tonePublished",
  disabled: "toneDisabled",
};

const PAGE_SIZE_OPTIONS = ["10", "20", "50"];

export default function AdminMcpCatalogPage() {
  const { t } = useTranslation();
  const [services, setServices] = useState<McpCatalogService[]>([]);
  const [loading, setLoading] = useState(false);
  // 与 Roles 各 tab 一致：列表加载失败走 LoadError 重试面板，
  // 不能让 services 保持 [] 后把错误伪装成「暂无服务」空态。
  const [loadError, setLoadError] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);

  const [formOpen, setFormOpen] = useState(false);
  const [editingService, setEditingService] =
    useState<McpCatalogService | null>(null);
  const [toolsService, setToolsService] = useState<McpCatalogService | null>(
    null,
  );
  const [logService, setLogService] = useState<McpCatalogService | null>(null);
  const [publishResult, setPublishResult] = useState<McpPublishResult | null>(
    null,
  );
  // P4.3: 发布前先拉 tool 级 diff，弹窗确认——对冲无版本树的治理缺口
  const [pendingPublish, setPendingPublish] = useState<{
    service: McpCatalogService;
    diff: McpPublishDiff;
  } | null>(null);
  const [publishing, setPublishing] = useState(false);

  // v2 追加：一键接入连接器 + Higress 入口地址设置（部署拓扑相关）
  const [higressBase, setHigressBase] = useState("");
  const [baseDraft, setBaseDraft] = useState("");
  const [baseSaving, setBaseSaving] = useState(false);
  const [baseModalOpen, setBaseModalOpen] = useState(false);
  const [attachingId, setAttachingId] = useState<string | null>(null);
  // 默认角色列要显示中文名而不是 role_key（GET /admin/roles，本页 admin-only）
  const [rolePkgs, setRolePkgs] = useState<RolePackage[]>([]);

  useEffect(() => {
    rolesApi
      .list()
      .then(setRolePkgs)
      .catch(() => {
        /* 非阻断：拿不到角色表时列回退显示 role_key */
      });
  }, []);

  const roleLabels = useRoleLabels(rolePkgs);

  useEffect(() => {
    mcpCatalogApi
      .getConnectorSettings()
      .then((s) => setHigressBase(s.higress_base ?? ""))
      .catch(() => {
        /* 非阻断：未读到地址时 attach 会给出明确提示 */
      });
  }, []);

  const saveBase = async () => {
    setBaseSaving(true);
    try {
      const r = await mcpCatalogApi.putConnectorSettings(baseDraft.trim());
      setHigressBase(r.higress_base);
      setBaseModalOpen(false);
      message.success(t("mcpCatalog.connector.baseSaved"));
    } catch (err) {
      message.error(apiErrorMessage(err, t("mcpCatalog.loadFailed"), t));
    } finally {
      setBaseSaving(false);
    }
  };

  const doAttach = async (service: McpCatalogService) => {
    setAttachingId(service.service_id);
    try {
      const r = await mcpCatalogApi.attachConnector(service.service_id);
      message.success(
        t(
          r.action === "created"
            ? "mcpCatalog.connector.attached"
            : "mcpCatalog.connector.updated",
          { name: r.server_name },
        ),
      );
    } catch (err) {
      message.error(apiErrorMessage(err, t("mcpCatalog.loadFailed"), t));
    } finally {
      setAttachingId(null);
    }
  };

  /** 接入会在用户的连接器列表里落一条记录 —— 先确认，避免行菜单误触。 */
  const attachConnector = (service: McpCatalogService) => {
    if (!higressBase) {
      setBaseDraft("");
      setBaseModalOpen(true);
      message.warning(t("mcpCatalog.connector.baseRequired"));
      return;
    }
    showConfirmModal({
      title: t("mcpCatalog.connector.attachConfirmTitle"),
      content: t("mcpCatalog.connector.attachConfirm", {
        name: service.display_name || service.service_id,
      }),
      onOk: () => doAttach(service),
    });
  };

  const reload = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const data = await mcpCatalogApi.listServices();
      setServices(data.services ?? []);
    } catch (err) {
      setLoadError(apiErrorMessage(err, t("mcpCatalog.loadFailed"), t));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const openCreate = () => {
    setEditingService(null);
    setFormOpen(true);
  };

  const openEdit = (service: McpCatalogService) => {
    setEditingService(service);
    setFormOpen(true);
  };

  const doDelete = async (service: McpCatalogService) => {
    try {
      await mcpCatalogApi.deleteService(service.service_id);
      message.success(t("mcpCatalog.deleteOk"));
      void reload();
    } catch (err) {
      message.error(apiErrorMessage(err, t("mcpCatalog.loadFailed"), t));
    }
  };

  const publish = (service: McpCatalogService) => {
    // P4.3: 先取 diff（失败则不打断发布，退化为普通确认）
    mcpCatalogApi
      .publishDiff(service.service_id)
      .then((diff) => setPendingPublish({ service, diff }))
      .catch((err) => {
        message.error(apiErrorMessage(err, t("mcpCatalog.loadFailed"), t));
      });
  };

  const confirmPublish = async () => {
    if (!pendingPublish) return;
    setPublishing(true);
    try {
      const result = await mcpCatalogApi.publish(
        pendingPublish.service.service_id,
        false,
      );
      message.success(t("mcpCatalog.publishOk"));
      setPublishResult(result);
      setPendingPublish(null);
      void reload();
    } catch (err) {
      message.error(apiErrorMessage(err, t("mcpCatalog.loadFailed"), t));
    } finally {
      setPublishing(false);
    }
  };

  const unpublish = (service: McpCatalogService) => {
    showConfirmModal({
      title: t("mcpCatalog.unpublish"),
      content: t("mcpCatalog.unpublishConfirm"),
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await mcpCatalogApi.unpublish(service.service_id, false);
          message.success(t("mcpCatalog.unpublishOk"));
          void reload();
        } catch (err) {
          message.error(apiErrorMessage(err, t("mcpCatalog.loadFailed"), t));
        }
      },
    });
  };

  const confirmDelete = (service: McpCatalogService) => {
    showConfirmModal({
      title: t("mcpCatalog.deleteServiceTitle"),
      content: t("mcpCatalog.deleteConfirm", { id: service.service_id }),
      okButtonProps: { danger: true },
      onOk: () => doDelete(service),
    });
  };

  const statusLabel = (status: McpServiceStatus) =>
    status === "published"
      ? t("mcpCatalog.statusPublished")
      : status === "disabled"
      ? t("mcpCatalog.statusDisabled")
      : t("mcpCatalog.statusDraft");

  const copy = useCallback(
    async (value: string) => {
      const ok = await copyText(value);
      if (ok) message.success(t("common.copied"));
      else message.error(t("common.copyFailed"));
    },
    [t],
  );

  const stats = useMemo(() => {
    const byStatus: Record<McpServiceStatus, number> = {
      draft: 0,
      published: 0,
      disabled: 0,
    };
    let tools = 0;
    for (const s of services) {
      byStatus[s.status] = (byStatus[s.status] ?? 0) + 1;
      tools += s.tool_count ?? 0;
    }
    return { total: services.length, ...byStatus, tools };
  }, [services]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return services.filter((s) => {
      if (statusFilter !== "all" && s.status !== statusFilter) return false;
      if (!q) return true;
      return [
        s.service_id,
        s.display_name,
        s.description ?? "",
        s.nacos_service_name,
        s.nacos_namespace,
        s.nacos_group,
        s.mcp_path ?? "",
        s.registry_name ?? "",
        (s.allowed_roles ?? []).join(" "),
      ].some((v) => v.toLowerCase().includes(q));
    });
  }, [services, query, statusFilter]);

  useEffect(() => {
    setPage(1);
  }, [query, statusFilter]);

  const statCards: { key: StatusFilter; label: string; value: number }[] = [
    { key: "all", label: t("mcpCatalog.statAll"), value: stats.total },
    {
      key: "published",
      label: t("mcpCatalog.statusPublished"),
      value: stats.published,
    },
    { key: "draft", label: t("mcpCatalog.statusDraft"), value: stats.draft },
    {
      key: "disabled",
      label: t("mcpCatalog.statusDisabled"),
      value: stats.disabled,
    },
  ];

  const rowMenu = (row: McpCatalogService): MenuProps["items"] => [
    { key: "tools", icon: <Wrench size={14} />, label: t("mcpCatalog.tools") },
    ...(row.status === "published"
      ? [
          {
            key: "attach",
            icon: <Plug size={14} />,
            label: t("mcpCatalog.connector.attach"),
          },
        ]
      : []),
    {
      key: "log",
      icon: <FileClock size={14} />,
      label: t("mcpCatalog.publishLog"),
    },
    { key: "edit", icon: <Pencil size={14} />, label: t("common.edit") },
    { type: "divider" as const },
    {
      key: "delete",
      icon: <Trash2 size={14} />,
      label: t("common.delete"),
      danger: true,
    },
  ];

  const onRowMenu = (key: string, row: McpCatalogService) => {
    if (key === "tools") setToolsService(row);
    else if (key === "attach") attachConnector(row);
    else if (key === "log") setLogService(row);
    else if (key === "edit") openEdit(row);
    else if (key === "delete") confirmDelete(row);
  };

  const filterActive = query.trim().length > 0 || statusFilter !== "all";
  const clearFilters = () => {
    setQuery("");
    setStatusFilter("all");
  };

  return (
    <PageShell.FillTabs
      title={t("pageShell.mcpCatalog.title")}
      subtitle={t("pageShell.mcpCatalog.subtitle")}
    >
      <div className={styles.page}>
        <div className={styles.statsRow}>
          {statCards.map((card) => (
            <button
              key={card.key}
              type="button"
              className={`${styles.statCard} ${
                statusFilter === card.key ? styles.statCardActive : ""
              }`}
              onClick={() => setStatusFilter(card.key)}
              aria-pressed={statusFilter === card.key}
            >
              <span className={styles.statValue}>{card.value}</span>
              <span className={styles.statLabel}>{card.label}</span>
            </button>
          ))}
          <div className={`${styles.statCard} ${styles.statCardStatic}`}>
            <span className={styles.statValue}>{stats.tools}</span>
            <span className={styles.statLabel}>
              {t("mcpCatalog.statTools")}
            </span>
          </div>
        </div>

        <div className={styles.listToolbar}>
          <Input
            className={styles.search}
            allowClear
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            prefix={<Search size={14} />}
            placeholder={t("mcpCatalog.searchPlaceholder")}
          />
          <div className={styles.toolbarRight}>
            <Button
              className={styles.higressBtn}
              icon={<Settings2 size={14} />}
              onClick={() => {
                setBaseDraft(higressBase);
                setBaseModalOpen(true);
              }}
            >
              <span className={styles.higressLabel}>
                {t("mcpCatalog.connector.baseButton")}
              </span>
              <span
                className={
                  higressBase ? styles.higressValue : styles.higressUnset
                }
              >
                {higressBase || t("mcpCatalog.connector.baseUnset")}
              </span>
            </Button>
            <Button
              type="primary"
              icon={<Plus size={14} />}
              onClick={openCreate}
            >
              {t("mcpCatalog.newService")}
            </Button>
          </div>
        </div>

        {loadError ? (
          <div className={styles.emptyWrap}>
            <LoadError message={loadError} onRetry={reload} />
          </div>
        ) : !loading && services.length === 0 ? (
          <div className={styles.emptyWrap}>
            <EmptyState
              variant="mascot"
              title={t("mcpCatalog.emptyTitle")}
              description={t("mcpCatalog.emptyDescription")}
              actionLabel={t("mcpCatalog.newService")}
              onAction={openCreate}
            />
          </div>
        ) : (
          <div className={styles.tableWrap}>
            <ResizableTable
              // v2: 列宽默认值改过（操作列收紧、角色列显示名称）——
              // 换 key 作废旧 localStorage 里用户拖过的旧宽度，否则新默认值不生效
              storageKey="admin-mcp-catalog-v2"
              rowKey="service_id"
              size="middle"
              loading={loading}
              dataSource={filtered}
              scroll={{ x: 1242 }}
              locale={{
                emptyText: (
                  <div className={styles.filterEmpty}>
                    <FilterX size={28} strokeWidth={1.3} />
                    <div className={styles.filterEmptyTitle}>
                      {t("mcpCatalog.filterEmptyTitle")}
                    </div>
                    <div className={styles.filterEmptyHint}>
                      {t("mcpCatalog.filterEmptyHint")}
                    </div>
                    <Button size="small" onClick={clearFilters}>
                      {t("mcpCatalog.clearFilters")}
                    </Button>
                  </div>
                ),
              }}
              pagination={{
                current: page,
                pageSize,
                pageSizeOptions: PAGE_SIZE_OPTIONS,
                showSizeChanger: true,
                size: "small",
                showTotal: (total) =>
                  t("mcpCatalog.filterTotal", { count: total }),
                // pageSize 受控就必须自己接住它，否则选择器会「弹回」10 条
                onChange: (nextPage, nextSize) => {
                  if (nextSize !== pageSize) {
                    setPageSize(nextSize);
                    setPage(1);
                    return;
                  }
                  setPage(nextPage);
                },
              }}
              columns={[
                {
                  title: t("mcpCatalog.colService"),
                  key: "service",
                  width: 240,
                  fixed: "left",
                  render: (_: unknown, row: McpCatalogService) => (
                    <div className={styles.serviceCell}>
                      <span className={styles.serviceName}>
                        {row.display_name || row.service_id}
                      </span>
                      {/* Same string twice reads as noise — drop the duplicate. */}
                      {row.display_name !== row.service_id ? (
                        <code className={styles.serviceId}>
                          {row.service_id}
                        </code>
                      ) : null}
                    </div>
                  ),
                },
                {
                  title: t("mcpCatalog.colStatus"),
                  dataIndex: "status",
                  width: 108,
                  render: (v: McpServiceStatus) => (
                    <span
                      className={`${styles.statusPill} ${
                        styles[STATUS_TONE[v]]
                      }`}
                    >
                      <span className={styles.statusDot} />
                      {statusLabel(v)}
                    </span>
                  ),
                },
                {
                  title: t("mcpCatalog.colAttribution"),
                  dataIndex: "org_id",
                  width: 120,
                  render: (v: string | null | undefined) =>
                    v === "*" ? (
                      <Tag color="blue">{t("mcpCatalog.orgShared")}</Tag>
                    ) : v ? (
                      <Tag>{v}</Tag>
                    ) : (
                      <Tooltip title={t("mcpCatalog.orgPrivateHint")}>
                        <span className={styles.muted}>
                          {t("mcpCatalog.orgPrivate")}
                        </span>
                      </Tooltip>
                    ),
                },
                {
                  title: t("mcpCatalog.colTools"),
                  key: "tools",
                  width: 100,
                  render: (_: unknown, row: McpCatalogService) => (
                    <Tooltip title={t("mcpCatalog.toolsHint")}>
                      <button
                        type="button"
                        className={styles.toolBadge}
                        onClick={() => setToolsService(row)}
                      >
                        <Wrench size={12} />
                        <span className={styles.toolBadgeNum}>
                          {row.tool_count ?? "—"}
                        </span>
                      </button>
                    </Tooltip>
                  ),
                },
                {
                  title: t("mcpCatalog.colRegistryTarget"),
                  key: "target",
                  width: 250,
                  render: (_: unknown, row: McpCatalogService) => (
                    <Tooltip
                      title={`${row.nacos_namespace}/${row.nacos_group}/${row.nacos_service_name}:${row.backend_port}`}
                    >
                      <div className={styles.targetCell}>
                        <span className={styles.targetMain}>
                          {row.nacos_service_name}
                          <span className={styles.targetPort}>
                            :{row.backend_port}
                          </span>
                        </span>
                        <span className={styles.targetScope}>
                          {row.nacos_namespace}/{row.nacos_group}
                        </span>
                      </div>
                    </Tooltip>
                  ),
                },
                {
                  title: t("mcpCatalog.colMcpPath"),
                  dataIndex: "mcp_path",
                  width: 250,
                  render: (v: string) =>
                    v ? (
                      <div className={styles.pathCell}>
                        <Tooltip title={v}>
                          <code className={styles.pathText}>{v}</code>
                        </Tooltip>
                        <Tooltip title={t("mcpCatalog.copyPath")}>
                          <button
                            type="button"
                            className={styles.copyBtn}
                            onClick={() => void copy(v)}
                          >
                            <Copy size={12} />
                          </button>
                        </Tooltip>
                      </div>
                    ) : (
                      <span className={styles.muted}>—</span>
                    ),
                },
                {
                  title: t("mcpCatalog.colDefaultRoles"),
                  dataIndex: "allowed_roles",
                  width: 150,
                  render: (roles: string[] | undefined) =>
                    (roles ?? []).length ? (
                      <div className={styles.roleChips}>
                        {(roles ?? []).map((role) => (
                          <Tooltip key={role} title={role}>
                            <span className={styles.roleChip}>
                              {roleLabels.get(role) ?? role}
                            </span>
                          </Tooltip>
                        ))}
                      </div>
                    ) : (
                      <Tooltip title={t("mcpCatalog.defaultRolesInherit")}>
                        <span className={styles.muted}>—</span>
                      </Tooltip>
                    ),
                },
                {
                  title: t("mcpCatalog.colActions"),
                  key: "actions",
                  width: 140,
                  // 内容用 flex-end 贴右，表头也跟着右对齐，否则列里会空出一大块
                  align: "right",
                  fixed: "right",
                  render: (_: unknown, row: McpCatalogService) => (
                    <div className={styles.actions}>
                      {row.status === "published" ? (
                        <Button
                          size="small"
                          icon={<CloudDownload size={13} />}
                          onClick={() => unpublish(row)}
                        >
                          {t("mcpCatalog.unpublish")}
                        </Button>
                      ) : (
                        <Button
                          size="small"
                          type="primary"
                          ghost
                          icon={<CloudUpload size={13} />}
                          onClick={() => publish(row)}
                        >
                          {t("mcpCatalog.publish")}
                        </Button>
                      )}
                      <Dropdown
                        trigger={["click"]}
                        placement="bottomRight"
                        menu={{
                          items: rowMenu(row),
                          onClick: ({ key }) => onRowMenu(key, row),
                        }}
                      >
                        <Button
                          size="small"
                          icon={<MoreHorizontal size={14} />}
                          aria-label={t("common.more")}
                          loading={attachingId === row.service_id}
                        />
                      </Dropdown>
                    </div>
                  ),
                },
              ]}
            />
            {filterActive && filtered.length > 0 ? (
              <div className={styles.filterFoot}>
                <Tag className={styles.filterTag}>
                  {t("mcpCatalog.filterTotal", { count: filtered.length })}
                </Tag>
                <Button type="link" size="small" onClick={clearFilters}>
                  {t("mcpCatalog.clearFilters")}
                </Button>
              </div>
            ) : null}
          </div>
        )}
      </div>

      <ServiceFormDrawer
        open={formOpen}
        service={editingService}
        onClose={() => setFormOpen(false)}
        onSaved={() => void reload()}
      />
      <ToolsDrawer
        open={toolsService !== null}
        service={toolsService}
        onClose={() => setToolsService(null)}
        onPublish={publish}
        onToolsChanged={() => void reload()}
      />
      <PublishLogModal
        open={logService !== null}
        service={logService}
        onClose={() => setLogService(null)}
      />

      {/* P4.3: 发布确认 —— tool 级变更清单，确认前零写入 */}
      <PublishDiffModal
        open={pendingPublish !== null}
        serviceId={pendingPublish?.service.service_id ?? null}
        diff={pendingPublish?.diff ?? null}
        confirmLoading={publishing}
        onCancel={() => setPendingPublish(null)}
        onConfirm={() => void confirmPublish()}
      />

      <PublishResultModal
        open={publishResult !== null}
        result={publishResult}
        onClose={() => setPublishResult(null)}
      />

      {/* v2 追加：Higress 入口地址（一键接入连接器的 URL 前缀） */}
      <Modal
        open={baseModalOpen}
        title={t("mcpCatalog.connector.baseModalTitle")}
        onCancel={() => setBaseModalOpen(false)}
        onOk={() => void saveBase()}
        okText={t("common.save")}
        confirmLoading={baseSaving}
        width={520}
        destroyOnClose
      >
        <div style={{ marginBottom: 8 }}>
          {t("mcpCatalog.connector.baseHelp")}
        </div>
        <Input
          value={baseDraft}
          onChange={(e) => setBaseDraft(e.target.value)}
          placeholder="http://172.17.0.1"
          maxLength={256}
        />
      </Modal>
    </PageShell.FillTabs>
  );
}
