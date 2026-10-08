/**
 * Tenant-face MCP catalog (2026-10-01 distribution design).
 *
 * The tenant admin's ONE MCP surface: platform-shared ('*') + own-tenant
 * services are listed; 接入连接器 mints the tenant-shared connector
 * (idempotent, roles synced from the catalog); service-/tool-level roles are
 * configured here — a tool may only ever NARROW the service default, so the
 * tool pickers only offer the service's own packages. Platform-shared rows
 * are read-only (configured by the platform administrator).
 *
 * Standalone module: one route + one nav entry; deleting them restores
 * pre-feature behaviour verbatim.
 */
import {
  Button,
  Modal,
  Select,
  Switch,
  Table,
  Tag,
  Tooltip,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  AlertTriangle,
  Plug,
  PlugZap,
  RefreshCw,
  Settings2,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  orgAdminApi,
  type TenantMcpService,
  type TenantMcpTool,
} from "../../../api/modules/orgAdmin";
import type { RolePackage } from "../../../api/modules/roles";
import { EmptyState } from "../../../components/EmptyState";
import PageShell from "../../../layouts/PageShell";
import { useCurrentUser } from "../../../hooks/useCurrentUser";
import { message } from "../../../utils/antdMessage";
import { apiErrorMessage } from "../../../utils/apiError";
import { LoadError, roleOptionLabel, useRoleLabels } from "../Roles/shared";
import catalogStyles from "./McpCatalog.module.less";
import rolesStyles from "../Roles/Roles.module.less";

/** tool_name → the draft role selection of that tool. */
type ToolDrafts = Record<string, string[]>;

/** One template whose visibility exceeds a referenced service's roles. */
interface TemplateMismatch {
  name: string;
  /** Role keys that can see the template but cannot use the service. */
  roles: string[];
  /** Template visible to the whole tenant (empty allowed_roles). */
  allVisible: boolean;
}

export default function TenantMcpCatalogPage() {
  const { t } = useTranslation();
  const user = useCurrentUser();
  const orgId = user?.org_id ?? "";

  const [services, setServices] = useState<TenantMcpService[]>([]);
  const [loading, setLoading] = useState(false);
  // 与 Roles 各 tab 一致：加载失败显示 LoadError 重试面板，而非「暂无服务」空态。
  const [loadError, setLoadError] = useState<string | null>(null);
  const [attachBusy, setAttachBusy] = useState<string | null>(null);
  const [configService, setConfigService] = useState<TenantMcpService | null>(
    null,
  );
  const [serviceDraft, setServiceDraft] = useState<string[]>([]);
  const [toolDrafts, setToolDrafts] = useState<ToolDrafts>({});
  const [savingService, setSavingService] = useState(false);
  const [savingTools, setSavingTools] = useState<Record<string, boolean>>({});
  const [rolePkgs, setRolePkgs] = useState<RolePackage[]>([]);
  /** experts ACL rows — connector refs power the role-mismatch advisory. */
  const [templateRows, setTemplateRows] = useState<
    { id: string; name: string; allowed_roles: string[]; default_mcp_servers?: string[] }[]
  >([]);

  const roleLabels = useRoleLabels(rolePkgs);

  const reload = useCallback(() => {
    if (!orgId) return;
    setLoading(true);
    setLoadError(null);
    Promise.all([
      orgAdminApi.listMcpServices(orgId),
      orgAdminApi.listRoles(orgId).then((rows) =>
        rows.map((r) => ({
          role_key: r.role_key,
          name_zh: r.name_zh,
          org_id: r.org_id,
          is_default: r.is_default,
          is_system: r.is_system,
          description: r.description,
        })),
      ),
      // Advisory only — a failure here must not break the page.
      orgAdminApi
        .listResourceAcl(orgId, "experts")
        .catch(() => []),
    ])
      .then(([svcRows, roleRows, expertRows]) => {
        setServices(svcRows);
        setRolePkgs(roleRows);
        setTemplateRows(expertRows);
      })
      .catch((err) =>
        setLoadError(apiErrorMessage(err, t("mcpCatalog.loadFailed"), t)),
      )
      .finally(() => setLoading(false));
  }, [orgId, t]);

  useEffect(() => {
    reload();
  }, [reload]);

  const statusTag = (status: TenantMcpService["status"]) =>
    status === "published" ? (
      <Tag color="green">{t("mcpCatalog.statusPublished")}</Tag>
    ) : status === "disabled" ? (
      <Tag color="red">{t("mcpCatalog.statusDisabled")}</Tag>
    ) : (
      <Tag color="gold">{t("mcpCatalog.statusDraft")}</Tag>
    );

  /** service_id → templates whose visible roles cannot use that service. */
  const mismatchesByService = useMemo(() => {
    const map = new Map<string, TemplateMismatch[]>();
    for (const row of templateRows) {
      for (const ref of row.default_mcp_servers ?? []) {
        // Ref shape: custom-mcp:{service_id}__{instance_id} (manual external
        // connectors carry no service suffix and are skipped).
        if (!ref.startsWith("custom-mcp:") || !ref.includes("__")) continue;
        const sid = ref.slice("custom-mcp:".length).split("__")[0];
        const svc = services.find((item) => item.service_id === sid);
        if (!svc) continue;
        const allVisible = row.allowed_roles.length === 0;
        const missing = row.allowed_roles.filter(
          (key) => !svc.allowed_roles.includes(key),
        );
        const roles = allVisible
          ? rolePkgs
              .map((r) => r.role_key)
              .filter((key) => !svc.allowed_roles.includes(key))
          : missing;
        if (roles.length === 0) continue;
        const list = map.get(sid) ?? [];
        list.push({ name: row.name, roles, allVisible });
        map.set(sid, list);
      }
    }
    return map;
  }, [templateRows, services, rolePkgs]);

  const formatRoles = useCallback(
    (keys: string[]) => keys.map((key) => roleLabels.get(key) ?? key).join("、"),
    [roleLabels],
  );

  const attach = useCallback(
    async (svc: TenantMcpService) => {
      setAttachBusy(svc.service_id);
      try {
        const result = await orgAdminApi.attachMcpService(
          orgId,
          svc.service_id,
        );
        message.success(
          t(
            result.action === "created"
              ? "tenantMcp.attachCreated"
              : "tenantMcp.attachUpdated",
            { name: svc.display_name || svc.service_id },
          ),
        );
        reload();
      } catch (err) {
        message.error(
          apiErrorMessage(err, t("tenantMcp.attachFailed"), t),
        );
      } finally {
        setAttachBusy(null);
      }
    },
    [orgId, reload, t],
  );

  const detach = useCallback(
    async (svc: TenantMcpService) => {
      setAttachBusy(svc.service_id);
      try {
        await orgAdminApi.detachMcpService(orgId, svc.service_id);
        message.success(t("tenantMcp.detached", { name: svc.display_name || svc.service_id }));
        reload();
      } catch (err) {
        message.error(apiErrorMessage(err, t("tenantMcp.detachFailed"), t));
      } finally {
        setAttachBusy(null);
      }
    },
    [orgId, reload, t],
  );

  const openConfig = useCallback((svc: TenantMcpService) => {
    setConfigService(svc);
    setServiceDraft([...svc.allowed_roles]);
    setToolDrafts(
      Object.fromEntries(
        svc.tools.map((tool) => [tool.tool_name, [...tool.allowed_roles]]),
      ),
    );
  }, []);

  const serviceDraftDirty = useMemo(
    () =>
      configService !== null &&
      !sameSelection(serviceDraft, configService.allowed_roles),
    [configService, serviceDraft],
  );

  const saveServiceRoles = useCallback(async () => {
    if (!configService) return;
    setSavingService(true);
    try {
      const result = await orgAdminApi.setMcpServiceRoles(
        orgId,
        configService.service_id,
        serviceDraft,
      );
      applyServiceRoles(setServices, configService.service_id, result.allowed_roles);
      message.success(t("tenantMcp.serviceRolesSaved"));
    } catch (err) {
      message.error(apiErrorMessage(err, t("tenantMcp.saveFailed"), t));
    } finally {
      setSavingService(false);
    }
  }, [configService, orgId, serviceDraft, t]);

  const saveToolRoles = useCallback(
    async (tool: TenantMcpTool) => {
      if (!configService) return;
      setSavingTools((prev) => ({ ...prev, [tool.tool_name]: true }));
      try {
        const roles = toolDrafts[tool.tool_name] ?? [];
        const result = await orgAdminApi.setMcpToolRoles(
          orgId,
          configService.service_id,
          tool.tool_name,
          roles,
        );
        setServices((prev) =>
          prev.map((svc) =>
            svc.service_id === configService.service_id
              ? {
                  ...svc,
                  tools: svc.tools.map((item) =>
                    item.tool_name === tool.tool_name
                      ? {
                          ...item,
                          allowed_roles: result.allowed_roles,
                          effective_roles: intersect(
                            result.allowed_roles,
                            svc.allowed_roles,
                          ),
                          inherited: result.allowed_roles.length === 0,
                        }
                      : item,
                  ),
                }
              : svc,
          ),
        );
        message.success(t("tenantMcp.toolRolesSaved"));
      } catch (err) {
        message.error(apiErrorMessage(err, t("tenantMcp.saveFailed"), t));
      } finally {
        setSavingTools((prev) => {
          const next = { ...prev };
          delete next[tool.tool_name];
          return next;
        });
      }
    },
    [configService, orgId, toolDrafts, t],
  );

  /** Options for a tool picker: the service default is the ceiling. */
  const toolRoleOptions = useMemo(
    () =>
      (configService?.allowed_roles ?? []).map((key) => ({
        value: key,
        label: roleLabels.get(key) ?? key,
      })),
    [configService, roleLabels],
  );

  const roleOptions = useMemo(
    () => rolePkgs.map((role) => ({ value: role.role_key, label: roleOptionLabel(role) })),
    [rolePkgs],
  );

  const toolColumns: ColumnsType<TenantMcpTool> = [
    {
      title: t("tenantMcp.toolName"),
      dataIndex: "tool_name",
      render: (name: string, tool) => (
        <div className={rolesStyles.cellStack}>
          <span>
            {name}
            {!tool.enabled && (
              <Tag color="red" style={{ marginLeft: 8 }}>
                {t("tenantMcp.toolDisabled")}
              </Tag>
            )}
          </span>
          {tool.description && (
            <span className={rolesStyles.cellSecondary}>{tool.description}</span>
          )}
        </div>
      ),
    },
    {
      title: t("tenantMcp.effectiveRoles"),
      key: "effective",
      width: 240,
      render: (_, tool) => (
        <span>
          {tool.effective_roles.length === 0 ? (
            <span className={rolesStyles.cellSecondary}>{t("tenantMcp.noRoles")}</span>
          ) : (
            tool.effective_roles.map((key) => (
              <Tag key={key}>{roleLabels.get(key) ?? key}</Tag>
            ))
          )}
        </span>
      ),
    },
    {
      title: t("tenantMcp.toolRoles"),
      key: "config",
      width: 340,
      render: (_, tool) => {
        const draft = toolDrafts[tool.tool_name] ?? [];
        const dirty = !sameSelection(draft, tool.allowed_roles);
        return (
          <div className={rolesStyles.draftEditor}>
            <Select
              mode="multiple"
              allowClear
              showSearch
              maxTagCount="responsive"
              optionFilterProp="label"
              style={{ minWidth: 200, flex: 1 }}
              placeholder={t("tenantMcp.inheritService")}
              value={draft}
              options={toolRoleOptions}
              disabled={!configService?.writable}
              onChange={(values: string[]) =>
                setToolDrafts((prev) => ({
                  ...prev,
                  [tool.tool_name]: values,
                }))
              }
            />
            {dirty && (
              <Button
                type="primary"
                size="small"
                loading={savingTools[tool.tool_name] === true}
                onClick={() => void saveToolRoles(tool)}
              >
                {t("common.save")}
              </Button>
            )}
          </div>
        );
      },
    },
  ];

  const columns: ColumnsType<TenantMcpService> = [
    {
      title: t("tenantMcp.service"),
      dataIndex: "display_name",
      render: (name: string, svc) => {
        const mismatches = mismatchesByService.get(svc.service_id) ?? [];
        return (
          <div className={rolesStyles.cellStack}>
            <span>
              {name}
              {svc.org_id === "*" && (
                <Tag color="blue" style={{ marginLeft: 8 }}>
                  {t("tenantMcp.platformShared")}
                </Tag>
              )}
              {mismatches.length > 0 && (
                <Tooltip
                  title={mismatches.map((entry, index) => (
                    <div key={index}>
                      {entry.allVisible
                        ? t("tenantMcp.mismatchAllVisible", {
                            name: entry.name,
                            roles: formatRoles(entry.roles),
                          })
                        : t("tenantMcp.mismatchEntry", {
                            name: entry.name,
                            roles: formatRoles(entry.roles),
                          })}
                    </div>
                  ))}
                >
                  <Tag color="orange" icon={<AlertTriangle size={11} />} style={{ marginLeft: 8 }}>
                    {t("tenantMcp.templatesMismatch")}
                  </Tag>
                </Tooltip>
              )}
            </span>
            <span className={rolesStyles.mono}>{svc.service_id}</span>
          </div>
        );
      },
    },
    {
      title: t("tenantMcp.status"),
      dataIndex: "status",
      width: 100,
      render: (status: TenantMcpService["status"]) => statusTag(status),
    },
    {
      title: t("tenantMcp.visibleRoles"),
      dataIndex: "allowed_roles",
      width: 220,
      render: (keys: string[]) =>
        keys.length === 0 ? (
          <span className={rolesStyles.cellSecondary}>{t("tenantMcp.noRoles")}</span>
        ) : (
          <span>
            {keys.map((key) => (
              <Tag key={key}>{roleLabels.get(key) ?? key}</Tag>
            ))}
          </span>
        ),
    },
    {
      title: t("tenantMcp.tools"),
      key: "tools",
      width: 90,
      render: (_, svc) => (
        <span className={rolesStyles.cellSecondary}>{svc.tools.length}</span>
      ),
    },
    {
      title: t("tenantMcp.connector"),
      key: "connector",
      width: 130,
      render: (_, svc) => (
        <Switch
          size="small"
          checked={svc.attached}
          loading={attachBusy === svc.service_id}
          disabled={svc.status !== "published"}
          onChange={(checked) => void (checked ? attach(svc) : detach(svc))}
        />
      ),
    },
    {
      title: t("tenantMcp.actions"),
      key: "actions",
      width: 150,
      render: (_, svc) => (
        <Button
          size="small"
          icon={<Settings2 size={13} />}
          disabled={svc.status === "disabled" && svc.tools.length === 0}
          onClick={() => openConfig(svc)}
        >
          {svc.writable
            ? t("tenantMcp.configureRoles")
            : t("tenantMcp.viewRoles")}
        </Button>
      ),
    },
  ];

  return (
    <PageShell.FillTabs
      title={t("pageShell.tenantMcpCatalog.title")}
      subtitle={t("pageShell.tenantMcpCatalog.subtitle")}
    >
      <div className={catalogStyles.page}>
        <div className={catalogStyles.listToolbar}>
          <span className={rolesStyles.cellSecondary}>
            {t("tenantMcp.hint")}
          </span>
          <div className={catalogStyles.toolbarRight}>
            <Button icon={<RefreshCw size={14} />} onClick={reload} loading={loading}>
              {t("common.refresh")}
            </Button>
          </div>
        </div>
        {loadError ? (
          <LoadError message={loadError} onRetry={reload} />
        ) : services.length === 0 && !loading ? (
          <EmptyState
            icon={<PlugZap size={28} />}
            title={t("tenantMcp.emptyTitle")}
            description={t("tenantMcp.emptyDescription")}
          />
        ) : (
          <Table<TenantMcpService>
            rowKey="service_id"
            size="small"
            loading={loading}
            dataSource={services}
            pagination={false}
            columns={columns}
            expandable={{
              defaultExpandedRowKeys: [],
              expandedRowRender: (svc) => (
                <Table<TenantMcpTool>
                  rowKey="tool_name"
                  size="small"
                  pagination={false}
                  dataSource={svc.tools}
                  columns={toolColumns}
                />
              ),
              rowExpandable: (svc) => svc.tools.length > 0,
            }}
          />
        )}
      </div>

      <Modal
        open={configService !== null}
        title={
          configService
            ? t("tenantMcp.configTitle", {
                name: configService.display_name || configService.service_id,
              })
            : ""
        }
        width={760}
        onCancel={() => setConfigService(null)}
        footer={
          configService?.writable
            ? [
                <Button key="close" onClick={() => setConfigService(null)}>
                  {t("common.close")}
                </Button>,
                <Button
                  key="save"
                  type="primary"
                  icon={<Plug size={13} />}
                  loading={savingService}
                  disabled={!serviceDraftDirty}
                  onClick={() => void saveServiceRoles()}
                >
                  {t("common.save")}
                </Button>,
              ]
            : [
                <Button key="close" onClick={() => setConfigService(null)}>
                  {t("common.close")}
                </Button>,
              ]
        }
      >
        {configService && (
          <>
            {!configService.writable && (
              <p className={rolesStyles.cellSecondary}>
                {t("tenantMcp.sharedReadOnly")}
              </p>
            )}
            <div className={rolesStyles.cellStack} style={{ marginBottom: 16 }}>
              <strong>{t("tenantMcp.serviceRolesTitle")}</strong>
              <span className={rolesStyles.cellSecondary}>
                {t("tenantMcp.serviceRolesHint")}
              </span>
              <Select
                mode="multiple"
                allowClear
                showSearch
                maxTagCount="responsive"
                optionFilterProp="label"
                style={{ width: "100%" }}
                placeholder={t("tenantMcp.pickServiceRoles")}
                value={serviceDraft}
                options={roleOptions}
                disabled={!configService.writable}
                onChange={(values: string[]) => setServiceDraft(values)}
              />
            </div>
            <div className={rolesStyles.cellStack}>
              <strong>{t("tenantMcp.toolsTitle")}</strong>
              <span className={rolesStyles.cellSecondary}>
                {t("tenantMcp.toolsHint")}
              </span>
              <Table<TenantMcpTool>
                rowKey="tool_name"
                size="small"
                pagination={false}
                dataSource={configService.tools}
                columns={toolColumns}
              />
            </div>
          </>
        )}
      </Modal>
    </PageShell.FillTabs>
  );
}

function applyServiceRoles(
  setter: React.Dispatch<React.SetStateAction<TenantMcpService[]>>,
  serviceId: string,
  roles: string[],
) {
  setter((prev) =>
    prev.map((svc) => {
      if (svc.service_id !== serviceId) return svc;
      return {
        ...svc,
        allowed_roles: roles,
        // the service default is the ceiling: re-derive effective roles
        tools: svc.tools.map((tool) => ({
          ...tool,
          effective_roles: intersect(tool.allowed_roles, roles),
        })),
      };
    }),
  );
}

function sameSelection(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  const left = [...a].sort();
  const right = [...b].sort();
  return left.every((key, index) => key === right[index]);
}

function intersect(a: string[], b: string[]): string[] {
  const set = new Set(b);
  return a.filter((key) => set.has(key));
}
