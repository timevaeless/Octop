import {
  Button,
  Checkbox,
  Collapse,
  Drawer,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Tabs,
  Tag,
  Tooltip,
} from "antd";
import {
  CloudUpload,
  ClipboardPaste,
  Eye,
  FileUp,
  ListChecks,
  Plus,
  Trash2,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type Key } from "react";
import { useTranslation } from "react-i18next";
import type {
  McpCatalogService,
  McpCatalogTool,
  McpImportResult,
  McpToolInput,
} from "../../../api/modules/mcpCatalog";
import { mcpCatalogApi } from "../../../api/modules/mcpCatalog";
import type { RolePackage } from "../../../api/modules/roles";
import { rolesApi } from "../../../api/modules/roles";
import { ResizableTable } from "../../../components/ResizableTable";
import { message } from "../../../utils/antdMessage";
import { apiErrorMessage } from "../../../utils/apiError";
import { useRoleLabels } from "../Roles/shared";
import styles from "./McpCatalog.module.less";

export interface ToolsDrawerProps {
  open: boolean;
  service: McpCatalogService | null;
  onClose: () => void;
  /** Re-emit the publish action to the page-level handler. */
  onPublish: (service: McpCatalogService) => void;
  onToolsChanged: () => void;
}

interface ToolFormValues {
  tool_name: string;
  description?: string;
  http_method: "GET" | "POST" | "PUT" | "DELETE";
  http_path: string;
  parameters?: {
    name: string;
    location: "path" | "query" | "body";
    required?: boolean;
    description?: string;
  }[];
  request_body?: string;
  response_body?: string;
  allowed_roles?: string[];
  enabled: boolean;
}

/**
 * Which tab owns each field.
 *
 * The form used to be one long scroll; splitting it into tabs cuts the height
 * roughly in half, but hides fields — so on a failed validation we have to jump
 * to the tab holding the first error, otherwise the OK button just appears dead.
 */
const FIELD_TAB: Record<string, string> = {
  tool_name: "basic",
  description: "basic",
  http_method: "mapping",
  http_path: "mapping",
  parameters: "mapping",
  request_body: "template",
  response_body: "template",
  allowed_roles: "access",
  enabled: "access",
};

const TOOL_PAGE_SIZE_OPTIONS = ["10", "20", "50"];

export default function ToolsDrawer({
  open,
  service,
  onClose,
  onPublish,
  onToolsChanged,
}: ToolsDrawerProps) {
  const { t } = useTranslation();
  const [tools, setTools] = useState<McpCatalogTool[]>([]);
  const [loading, setLoading] = useState(false);
  const [importUrl, setImportUrl] = useState("");
  const [importing, setImporting] = useState(false);
  const [formOpen, setFormOpen] = useState(false);
  const [editingTool, setEditingTool] = useState<McpCatalogTool | null>(null);
  const [saving, setSaving] = useState(false);
  // P1.4: 下发配置预览 —— 复用后端 build_plugin_tool 权威渲染（前端不复制
  // 映射逻辑防漂移），让管理员在保存/发布前看到 tools[] 条目的真实形状。
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewJson, setPreviewJson] = useState<string | null>(null);
  // P4.1/4.2: 粘贴 OpenAPI → dry-run 预览 → 确认导入
  const [pasteOpen, setPasteOpen] = useState(false);
  const [pasteBody, setPasteBody] = useState("");
  const [pastePreview, setPastePreview] = useState<McpImportResult | null>(
    null,
  );
  const [pasteLoading, setPasteLoading] = useState(false);
  // 角色表原样留一份：下拉要「键（名）」标签，列表要「名」——同一份数据两个视图，
  // 拆成两份 state 会漂移。
  const [roles, setRoles] = useState<RolePackage[]>([]);
  const [rolesLoading, setRolesLoading] = useState(false);
  const [form] = Form.useForm<ToolFormValues>();
  // 工具表单分 tab 承载（原来一屏滚不完），activeKey 受控以便校验失败时跳转
  const [activeTab, setActiveTab] = useState("basic");
  // 工具列表分页（此前 pagination={false}，工具多的服务一屏看不完）
  const [toolPage, setToolPage] = useState(1);
  const [toolPageSize, setToolPageSize] = useState(10);
  // 需求二：工具列表批量设置角色（纯 UI 兜底，不改权限语义）
  const [selectedKeys, setSelectedKeys] = useState<Key[]>([]);
  const [batchOpen, setBatchOpen] = useState(false);
  const [batchRoles, setBatchRoles] = useState<string[]>([]);
  const [batchSaving, setBatchSaving] = useState(false);

  // v2 设计 §4.2：角色改下拉多选（数据源 GET /admin/roles，数据现成且本页
  // admin-only）。加载失败不阻塞编辑——tags 模式仍可手输兜底，但默认走下拉，
  // 杜绝手拼错角色名导致 hub ACL 静默不匹配（G2 验收实锤坑）。
  const loadRoles = useCallback(async () => {
    setRolesLoading(true);
    try {
      setRoles(await rolesApi.list());
    } catch {
      setRoles([]);
    } finally {
      setRolesLoading(false);
    }
  }, []);

  /**
   * Picker labels keep the key visible — the admin picks by key semantics.
   *
   * 子集规则（与后端 validate_tool_roles_subset 对齐，2026-10-05 前端对齐）：
   * 工具角色只能是服务角色的子集——服务含 `*`（通配）时放开全量；服务未配
   * 角色时无可选项（工具只能保持为空）。tags 模式保留：roles 拉取失败或
   * 历史数据超集时仍可显示/手输，越界值由后端 400 兜底。
   */
  const roleOptions = useMemo(() => {
    const toOption = (r: RolePackage) => ({
      value: r.role_key,
      label: r.name_zh ? `${r.role_key}（${r.name_zh}）` : r.role_key,
    });
    const svcRoles = service?.allowed_roles ?? [];
    if (svcRoles.includes("*")) return roles.map(toOption);
    if (svcRoles.length === 0) return [];
    const wanted = new Set(svcRoles);
    return roles.filter((r) => wanted.has(r.role_key)).map(toOption);
  }, [roles, service]);

  /** Table column shows the display name; the raw key stays in the tooltip. */
  const roleLabels = useRoleLabels(roles);

  useEffect(() => {
    if (open) void loadRoles();
  }, [open, loadRoles]);

  const reload = useCallback(async () => {
    if (!service) return;
    setLoading(true);
    try {
      const data = await mcpCatalogApi.listTools(service.service_id);
      setTools(data.tools ?? []);
    } catch (err) {
      message.error(apiErrorMessage(err, t("mcpCatalog.loadFailed"), t));
    } finally {
      setLoading(false);
    }
  }, [service, t]);

  useEffect(() => {
    if (open) {
      setImportUrl("");
      setSelectedKeys([]);
      setToolPage(1);
      setToolPageSize(10);
      void reload();
    }
  }, [open, reload]);

  const selectedTools = tools.filter((tl) =>
    selectedKeys.includes(tl.tool_name),
  );

  const applyBatchRoles = async () => {
    if (!service || !selectedTools.length) return;
    setBatchSaving(true);
    try {
      for (const tool of selectedTools) {
        await mcpCatalogApi.putTool(service.service_id, {
          tool_name: tool.tool_name,
          description: tool.description,
          http_method: tool.http_method,
          http_path: tool.http_path,
          parameters: tool.parameters ?? [],
          request_body: tool.request_body,
          response_body: tool.response_body,
          allowed_roles: batchRoles,
          enabled: tool.enabled,
        });
      }
      message.success(
        t("mcpCatalog.tool.batchRolesOkCount", { count: selectedTools.length }),
      );
      setBatchOpen(false);
      setBatchRoles([]);
      setSelectedKeys([]);
      void reload();
      onToolsChanged();
    } catch (err) {
      message.error(apiErrorMessage(err, t("mcpCatalog.loadFailed"), t));
    } finally {
      setBatchSaving(false);
    }
  };

  const removeSelected = async () => {
    if (!service || !selectedTools.length) return;
    try {
      // 未知名（并发编辑后的过期选择）由服务端跳过，返回实际删除清单
      const result = await mcpCatalogApi.batchDeleteTools(
        service.service_id,
        selectedTools.map((tl) => tl.tool_name),
      );
      message.success(
        t("mcpCatalog.tool.batchDeleteOkCount", { count: result.deleted.length }),
      );
      setSelectedKeys([]);
      void reload();
      onToolsChanged();
    } catch (err) {
      message.error(apiErrorMessage(err, t("mcpCatalog.loadFailed"), t));
    }
  };

  /**
   * Validate every field (all tabs are force-rendered, so hidden ones count too)
   * and return the values, or null after jumping to the tab with the first error.
   */
  const validateAll = async (): Promise<ToolFormValues | null> => {
    try {
      return await form.validateFields();
    } catch (err) {
      const first = (err as { errorFields?: { name: (string | number)[] }[] })
        .errorFields?.[0]?.name?.[0];
      if (typeof first === "string" && FIELD_TAB[first]) {
        setActiveTab(FIELD_TAB[first]);
      }
      return null;
    }
  };

  const openCreate = () => {
    setEditingTool(null);
    form.resetFields();
    form.setFieldsValue({
      http_method: "GET",
      enabled: true,
      parameters: [],
      allowed_roles: [],
    });
    setPreviewJson(null);
    setActiveTab("basic");
    setFormOpen(true);
  };

  const openEdit = (tool: McpCatalogTool) => {
    setEditingTool(tool);
    form.setFieldsValue({
      tool_name: tool.tool_name,
      description: tool.description ?? undefined,
      http_method: tool.http_method,
      http_path: tool.http_path,
      parameters: (tool.parameters ?? []).map((p) => ({
        name: p.name,
        location: p.location,
        required: Boolean(p.required),
        description: p.description ?? undefined,
      })),
      request_body: tool.request_body ?? undefined,
      response_body: tool.response_body ?? undefined,
      allowed_roles: tool.allowed_roles ?? [],
      enabled: tool.enabled,
    });
    setPreviewJson(null);
    setActiveTab("basic");
    setFormOpen(true);
  };

  const runPreview = async () => {
    if (!service) return;
    const values = await validateAll();
    if (!values) return; // 校验错误已跳到对应 tab
    setPreviewLoading(true);
    try {
      const result = await mcpCatalogApi.previewTool(service.service_id, {
        tool_name: values.tool_name,
        description: values.description || null,
        http_method: values.http_method,
        http_path: values.http_path,
        parameters: (values.parameters ?? []).map((p) => ({
          name: p.name,
          location: p.location,
          required: Boolean(p.required),
          description: p.description || null,
        })),
        request_body: values.request_body || null,
        response_body: values.response_body || null,
        allowed_roles: values.allowed_roles ?? [],
        enabled: values.enabled,
      });
      setPreviewJson(JSON.stringify(result.plugin_tool, null, 2));
    } catch (err) {
      // 校验不过（400 带原因）也走这里 —— 模板错误必须让管理员当场看到
      message.error(apiErrorMessage(err, t("mcpCatalog.loadFailed"), t));
    } finally {
      setPreviewLoading(false);
    }
  };

  const saveTool = async () => {
    if (!service) return;
    const values = await validateAll();
    if (!values) return; // 校验错误已跳到对应 tab
    setSaving(true);
    try {
      const body: McpToolInput = {
        tool_name: values.tool_name,
        description: values.description || null,
        http_method: values.http_method,
        http_path: values.http_path,
        parameters: (values.parameters ?? []).map((p) => ({
          name: p.name,
          location: p.location,
          required: Boolean(p.required),
          description: p.description || null,
        })),
        request_body: values.request_body || null,
        response_body: values.response_body || null,
        allowed_roles: values.allowed_roles ?? [],
        enabled: values.enabled,
      };
      await mcpCatalogApi.putTool(service.service_id, body);
      message.success(t("mcpCatalog.toolSaveOk"));
      setFormOpen(false);
      void reload();
      onToolsChanged();
    } catch (err) {
      message.error(apiErrorMessage(err, t("mcpCatalog.loadFailed"), t));
    } finally {
      setSaving(false);
    }
  };

  const removeTool = async (tool: McpCatalogTool) => {
    if (!service) return;
    try {
      await mcpCatalogApi.deleteTool(service.service_id, tool.tool_name);
      message.success(t("mcpCatalog.toolDeleteOk"));
      void reload();
      onToolsChanged();
    } catch (err) {
      message.error(apiErrorMessage(err, t("mcpCatalog.loadFailed"), t));
    }
  };

  const toggleEnabled = async (tool: McpCatalogTool, enabled: boolean) => {
    if (!service) return;
    try {
      await mcpCatalogApi.putTool(service.service_id, {
        tool_name: tool.tool_name,
        description: tool.description,
        http_method: tool.http_method,
        http_path: tool.http_path,
        parameters: tool.parameters ?? [],
        request_body: tool.request_body,
        response_body: tool.response_body,
        allowed_roles: tool.allowed_roles ?? [],
        enabled,
      });
      void reload();
      onToolsChanged();
    } catch (err) {
      message.error(apiErrorMessage(err, t("mcpCatalog.loadFailed"), t));
    }
  };

  const runImport = async () => {
    if (!service) return;
    setImporting(true);
    try {
      const result = await mcpCatalogApi.importOpenApi(service.service_id, {
        openapiUrl: importUrl.trim() || undefined,
      });
      const imported = result.imported?.length ?? 0;
      const skipped = result.skipped?.length ?? 0;
      const deleted = result.deleted?.length ?? 0;
      message.success(t("mcpCatalog.importOk", { imported, skipped, deleted }));
      setImportUrl("");
      void reload();
      onToolsChanged();
    } catch (err) {
      message.error(apiErrorMessage(err, t("mcpCatalog.loadFailed"), t));
    } finally {
      setImporting(false);
    }
  };

  const renderImportLine = (label: string, names: string[], color?: string) =>
    names.length > 0 && (
      <div style={{ marginBottom: 6 }}>
        <Tag color={color}>{label}</Tag>
        <span style={{ fontSize: 12 }}>{names.join(", ")}</span>
      </div>
    );

  // P4.2: dry-run 预览——确认前零落库
  const runPastePreview = async () => {
    if (!service || !pasteBody.trim()) return;
    setPasteLoading(true);
    try {
      const result = await mcpCatalogApi.importOpenApi(service.service_id, {
        openapiBody: pasteBody,
        dryRun: true,
      });
      setPastePreview(result);
    } catch (err) {
      message.error(apiErrorMessage(err, t("mcpCatalog.loadFailed"), t));
    } finally {
      setPasteLoading(false);
    }
  };

  const runPasteImport = async () => {
    if (!service || !pastePreview) return;
    setPasteLoading(true);
    try {
      const result = await mcpCatalogApi.importOpenApi(service.service_id, {
        openapiBody: pasteBody,
      });
      message.success(
        t("mcpCatalog.importOk", {
          imported: result.imported?.length ?? 0,
          skipped: result.skipped?.length ?? 0,
          deleted: result.deleted?.length ?? 0,
        }),
      );
      setPasteOpen(false);
      setPasteBody("");
      setPastePreview(null);
      void reload();
      onToolsChanged();
    } catch (err) {
      message.error(apiErrorMessage(err, t("mcpCatalog.loadFailed"), t));
    } finally {
      setPasteLoading(false);
    }
  };

  return (
    <Drawer
      open={open}
      onClose={onClose}
      // 880 时「操作」列（fixed: right）会盖住「启用」列 —— 加宽到能放下 7 列
      width={1080}
      destroyOnClose
      title={t("mcpCatalog.toolsTitle", {
        name: service?.display_name || service?.service_id || "",
      })}
      footer={null}
    >
      <div className={styles.toolbar}>
        <Space.Compact style={{ flex: 1, maxWidth: 360 }}>
          <Input
            value={importUrl}
            onChange={(e) => setImportUrl(e.target.value)}
            placeholder={t("mcpCatalog.importUrlPlaceholder")}
            allowClear
          />
          <Button
            icon={<FileUp size={14} />}
            loading={importing}
            onClick={() => void runImport()}
          >
            {t("mcpCatalog.importOpenapi")}
          </Button>
        </Space.Compact>
        <Button
          icon={<ClipboardPaste size={14} />}
          onClick={() => {
            setPastePreview(null);
            setPasteOpen(true);
          }}
        >
          {t("mcpCatalog.importPaste")}
        </Button>
        <Button
          icon={<ListChecks size={14} />}
          disabled={selectedKeys.length === 0}
          onClick={() => {
            setBatchRoles([]);
            setBatchOpen(true);
          }}
        >
          {t("mcpCatalog.tool.batchSetRoles")}
          {selectedKeys.length > 0 ? ` (${selectedKeys.length})` : ""}
        </Button>
        <Button
          danger
          icon={<Trash2 size={14} />}
          disabled={selectedKeys.length === 0}
          onClick={() =>
            Modal.confirm({
              title: t("mcpCatalog.tool.batchDeleteTitle"),
              content: t("mcpCatalog.tool.batchDeleteConfirm", {
                count: selectedKeys.length,
              }),
              okText: t("common.delete"),
              okButtonProps: { danger: true },
              cancelText: t("common.cancel"),
              onOk: () => void removeSelected(),
            })
          }
        >
          {t("mcpCatalog.tool.batchDelete")}
          {selectedKeys.length > 0 ? ` (${selectedKeys.length})` : ""}
        </Button>
        <Button
          type="primary"
          ghost
          icon={<Plus size={14} />}
          onClick={openCreate}
        >
          {t("mcpCatalog.newTool")}
        </Button>
        <Button
          type="primary"
          icon={<CloudUpload size={14} />}
          onClick={() => service && onPublish(service)}
        >
          {t("mcpCatalog.publish")}
        </Button>
      </div>

      <ResizableTable
        // v2: scroll.x 与「允许角色」列宽都调过，并新增分页 ——
        // 换 key 作废旧 localStorage 宽度，否则新默认值不生效
        storageKey="admin-mcp-catalog-tools-v2"
        rowKey="tool_name"
        size="middle"
        loading={loading}
        dataSource={tools}
        pagination={{
          current: toolPage,
          pageSize: toolPageSize,
          pageSizeOptions: TOOL_PAGE_SIZE_OPTIONS,
          showSizeChanger: true,
          size: "small",
          showTotal: (total) => t("mcpCatalog.filterTotal", { count: total }),
          // pageSize 受控就必须自己接住它，否则选择器会「弹回」10 条
          onChange: (nextPage, nextSize) => {
            if (nextSize !== toolPageSize) {
              setToolPageSize(nextSize);
              setToolPage(1);
              return;
            }
            setToolPage(nextPage);
          },
        }}
        scroll={{ x: 980 }}
        rowSelection={{
          selectedRowKeys: selectedKeys,
          onChange: (keys) => setSelectedKeys(keys),
        }}
        columns={[
          {
            title: t("mcpCatalog.colToolName"),
            dataIndex: "tool_name",
            width: 200,
            render: (v: string, row: McpCatalogTool) => (
              <Space size={6}>
                <span>{v}</span>
                {!row.enabled && <Tag>{t("mcpCatalog.statusDisabled")}</Tag>}
              </Space>
            ),
          },
          {
            title: t("mcpCatalog.colMethod"),
            dataIndex: "http_method",
            width: 90,
            render: (v: string) => (
              <Tag
                color={
                  v === "GET"
                    ? "blue"
                    : v === "POST"
                    ? "orange"
                    : v === "PUT"
                    ? "purple"
                    : "red"
                }
              >
                {v}
              </Tag>
            ),
          },
          {
            title: t("mcpCatalog.colHttpPath"),
            dataIndex: "http_path",
            width: 200,
            ellipsis: true,
          },
          {
            title: t("mcpCatalog.colRoles"),
            dataIndex: "allowed_roles",
            width: 200,
            // 一排标签，用与主列表「默认角色」相同的 chip 样式；
            // 因此不能再加 ellipsis —— 单行省略会把标签裁掉，靠 flex-wrap 换行
            render: (roleKeys: string[] | undefined) =>
              (roleKeys ?? []).length ? (
                <div className={styles.roleChips}>
                  {(roleKeys ?? []).map((key) => (
                    <Tooltip key={key} title={key}>
                      <span className={styles.roleChip}>
                        {roleLabels.get(key) ?? key}
                      </span>
                    </Tooltip>
                  ))}
                </div>
              ) : (
                <span className={styles.muted}>—</span>
              ),
          },
          {
            title: t("mcpCatalog.colEnabled"),
            dataIndex: "enabled",
            width: 90,
            render: (_: unknown, row: McpCatalogTool) => (
              <Switch
                size="small"
                checked={row.enabled}
                onChange={(v) => void toggleEnabled(row, v)}
              />
            ),
          },
          {
            title: t("mcpCatalog.colActions"),
            key: "actions",
            width: 140,
            // 与主列表一致：内容贴右，表头也跟着右对齐
            align: "right",
            fixed: "right",
            render: (_: unknown, row: McpCatalogTool) => (
              <Space size={4}>
                <Button type="link" size="small" onClick={() => openEdit(row)}>
                  {t("common.edit")}
                </Button>
                <Popconfirm
                  title={t("mcpCatalog.toolDeleteConfirm", {
                    name: row.tool_name,
                  })}
                  onConfirm={() => void removeTool(row)}
                >
                  <Button type="link" size="small" danger>
                    {t("common.delete")}
                  </Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />

      <Modal
        open={formOpen}
        title={editingTool ? t("mcpCatalog.editTool") : t("mcpCatalog.newTool")}
        width={720}
        onCancel={() => setFormOpen(false)}
        onOk={() => void saveTool()}
        confirmLoading={saving}
        destroyOnClose
        maskClosable={false}
        // 参数多的工具 tab 仍可能超屏 —— 让 body 自己滚，页脚按钮永远可见
        styles={{ body: { maxHeight: "58vh", overflowY: "auto" } }}
      >
        <Form form={form} layout="vertical" requiredMark={false}>
          {/* 原先是 4 段分割线的单页长表单，一屏滚不完；按同样的 4 段拆成 tab。
              每个 tab 都 forceRender，否则未访问过的 tab 里的字段不参与校验。 */}
          <Tabs
            activeKey={activeTab}
            onChange={setActiveTab}
            size="small"
            items={[
              {
                key: "basic",
                label: t("mcpCatalog.tool.sectionBasic"),
                forceRender: true,
                children: (
                  <>
                    <Form.Item
                      name="tool_name"
                      label={t("mcpCatalog.tool.name")}
                      extra={t("mcpCatalog.tool.nameHelp")}
                      rules={[{ required: true }]}
                    >
                      <Input disabled={editingTool !== null} maxLength={64} />
                    </Form.Item>
                    <Form.Item
                      name="description"
                      label={t("mcpCatalog.tool.desc")}
                    >
                      <Input.TextArea rows={2} />
                    </Form.Item>
                  </>
                ),
              },
              {
                key: "mapping",
                label: t("mcpCatalog.tool.sectionMapping"),
                forceRender: true,
                children: (
                  <>
                    <Space style={{ display: "flex" }} align="start">
                      <Form.Item
                        name="http_method"
                        label={t("mcpCatalog.tool.method")}
                        rules={[{ required: true }]}
                        style={{ width: 110 }}
                      >
                        <Select
                          options={[
                            { value: "GET" },
                            { value: "POST" },
                            { value: "PUT" },
                            { value: "DELETE" },
                          ]}
                        />
                      </Form.Item>
                      <Form.Item
                        name="http_path"
                        label={t("mcpCatalog.tool.path")}
                        extra={t("mcpCatalog.tool.pathHelp")}
                        rules={[{ required: true }]}
                        style={{ flex: 1, minWidth: 400 }}
                      >
                        <Input maxLength={256} />
                      </Form.Item>
                    </Space>

                    <Form.Item label={t("mcpCatalog.tool.params")}>
                      <Form.List name="parameters">
                        {(fields, { add, remove }) => (
                          <>
                            {fields.map((field) => (
                              <div key={field.key} className={styles.paramRow}>
                                <Form.Item
                                  name={[field.name, "name"]}
                                  noStyle
                                  rules={[{ required: true }]}
                                >
                                  <Input
                                    className={styles.paramName}
                                    placeholder={t("mcpCatalog.tool.paramName")}
                                    maxLength={64}
                                  />
                                </Form.Item>
                                <Form.Item
                                  name={[field.name, "location"]}
                                  noStyle
                                  initialValue="query"
                                  rules={[{ required: true }]}
                                >
                                  <Select
                                    className={styles.paramLocation}
                                    options={[
                                      { value: "path" },
                                      { value: "query" },
                                      { value: "body" },
                                    ]}
                                  />
                                </Form.Item>
                                <Form.Item
                                  name={[field.name, "required"]}
                                  noStyle
                                  valuePropName="checked"
                                >
                                  <Checkbox className={styles.paramRequired}>
                                    {t("mcpCatalog.tool.paramRequired")}
                                  </Checkbox>
                                </Form.Item>
                                <Form.Item
                                  name={[field.name, "description"]}
                                  noStyle
                                >
                                  <Input
                                    className={styles.paramDesc}
                                    placeholder={t("mcpCatalog.tool.paramDesc")}
                                  />
                                </Form.Item>
                                <Button
                                  type="text"
                                  danger
                                  size="small"
                                  className={styles.paramRemove}
                                  icon={<Trash2 size={14} />}
                                  onClick={() => remove(field.name)}
                                />
                              </div>
                            ))}
                            <Button
                              type="dashed"
                              size="small"
                              icon={<Plus size={14} />}
                              onClick={() =>
                                add({ location: "query", required: false })
                              }
                            >
                              {t("mcpCatalog.tool.addParam")}
                            </Button>
                          </>
                        )}
                      </Form.List>
                    </Form.Item>
                  </>
                ),
              },
              {
                key: "template",
                label: t("mcpCatalog.tool.sectionTemplate"),
                forceRender: true,
                children: (
                  <>
                    <Form.Item
                      name="request_body"
                      label={t("mcpCatalog.tool.requestBody")}
                      extra={t("mcpCatalog.tool.requestBodyHelp")}
                    >
                      <Input.TextArea
                        rows={4}
                        placeholder={t(
                          "mcpCatalog.tool.requestBodyPlaceholder",
                        )}
                      />
                    </Form.Item>
                    <Form.Item
                      name="response_body"
                      label={t("mcpCatalog.tool.responseBody")}
                      extra={t("mcpCatalog.tool.responseBodyHelp")}
                    >
                      <Input.TextArea
                        rows={3}
                        placeholder={t(
                          "mcpCatalog.tool.responseBodyPlaceholder",
                        )}
                      />
                    </Form.Item>
                  </>
                ),
              },
              {
                key: "access",
                label: t("mcpCatalog.tool.sectionAccess"),
                forceRender: true,
                children: (
                  <>
                    <Form.Item
                      name="allowed_roles"
                      label={t("mcpCatalog.tool.roles")}
                      extra={t("mcpCatalog.tool.rolesHelp")}
                    >
                      <Select
                        mode="tags"
                        options={roleOptions}
                        loading={rolesLoading}
                        showSearch
                        tokenSeparators={[",", " "]}
                        placeholder={t("mcpCatalog.tool.rolesPlaceholder")}
                      />
                    </Form.Item>
                    <Form.Item
                      name="enabled"
                      label={t("mcpCatalog.tool.enabled")}
                      valuePropName="checked"
                    >
                      <Switch />
                    </Form.Item>
                  </>
                ),
              },
            ]}
          />

          {/* 预览留在 tab 之外：它是「保存前验证」的入口，任何 tab 下都应一点可达 */}
          <Collapse
            ghost
            items={[
              {
                key: "preview",
                label: (
                  <Space size={4}>
                    <Eye size={14} />
                    {t("mcpCatalog.tool.previewTitle")}
                  </Space>
                ),
                extra: (
                  <Button
                    size="small"
                    loading={previewLoading}
                    onClick={(e) => {
                      e.stopPropagation();
                      void runPreview();
                    }}
                  >
                    {t("mcpCatalog.tool.previewBtn")}
                  </Button>
                ),
                children:
                  previewJson !== null ? (
                    <pre className={styles.previewJson}>{previewJson}</pre>
                  ) : (
                    <span className={styles.previewHint}>
                      {t("mcpCatalog.tool.previewEmpty")}
                    </span>
                  ),
              },
            ]}
          />
        </Form>
      </Modal>

      {/* 需求二：批量设置选中工具的 allowed_roles（逐个 PUT，覆盖式写入） */}
      <Modal
        open={batchOpen}
        title={t("mcpCatalog.tool.batchRolesTitle")}
        onCancel={() => setBatchOpen(false)}
        onOk={() => void applyBatchRoles()}
        okText={t("mcpCatalog.tool.batchRolesOk")}
        okButtonProps={{ disabled: selectedTools.length === 0 }}
        confirmLoading={batchSaving}
        width={520}
        destroyOnClose
        maskClosable={false}
      >
        <div style={{ marginBottom: 12, color: "var(--fn-text-tertiary)" }}>
          {t("mcpCatalog.tool.batchRolesHelp", { count: selectedTools.length })}
        </div>
        <Select
          mode="tags"
          style={{ width: "100%" }}
          options={roleOptions}
          loading={rolesLoading}
          showSearch
          tokenSeparators={[",", " "]}
          value={batchRoles}
          onChange={(v) => setBatchRoles(v)}
          placeholder={t("mcpCatalog.tool.rolesPlaceholder")}
        />
      </Modal>

      {/* P4.1/4.2: 粘贴 OpenAPI 文档 → 预览变更清单 → 确认导入 */}
      <Modal
        open={pasteOpen}
        title={t("mcpCatalog.importPasteTitle")}
        width={680}
        onCancel={() => setPasteOpen(false)}
        footer={
          <Space>
            <Button onClick={() => setPasteOpen(false)}>
              {t("common.cancel")}
            </Button>
            <Button
              loading={pasteLoading}
              disabled={!pasteBody.trim()}
              onClick={() => void runPastePreview()}
            >
              {t("mcpCatalog.importPreviewBtn")}
            </Button>
            <Button
              type="primary"
              loading={pasteLoading}
              disabled={!pastePreview}
              onClick={() => void runPasteImport()}
            >
              {t("mcpCatalog.importConfirmBtn")}
            </Button>
          </Space>
        }
        destroyOnClose
      >
        <Input.TextArea
          rows={10}
          value={pasteBody}
          onChange={(e) => {
            setPasteBody(e.target.value);
            setPastePreview(null);
          }}
          placeholder={t("mcpCatalog.importPastePlaceholder")}
        />
        {pastePreview && (
          <div style={{ marginTop: 12 }}>
            <div style={{ marginBottom: 6, fontWeight: 600 }}>
              {t("mcpCatalog.importPreviewTitle")}
            </div>
            {renderImportLine(
              t("mcpCatalog.diffAdded"),
              pastePreview.imported ?? [],
              "green",
            )}
            {renderImportLine(
              t("mcpCatalog.diffUpdated"),
              pastePreview.overwritten ?? [],
              "orange",
            )}
            {renderImportLine(
              t("mcpCatalog.diffRemoved"),
              pastePreview.deleted ?? [],
              "red",
            )}
            {(pastePreview.skipped ?? []).length > 0 && (
              <Tooltip
                title={(pastePreview.skipped ?? [])
                  .map((s) => `${s.method} ${s.path}: ${s.reason}`)
                  .join("\n")}
              >
                <div style={{ fontSize: 12 }}>
                  <Tag color="red">{t("mcpCatalog.diffSkipped")}</Tag>
                  {(pastePreview.skipped ?? []).length}
                </div>
              </Tooltip>
            )}
            {!(pastePreview.imported ?? []).length &&
              !(pastePreview.overwritten ?? []).length &&
              !(pastePreview.deleted ?? []).length && (
                <div style={{ color: "var(--fn-text-tertiary)" }}>
                  {t("mcpCatalog.diffNothing")}
                </div>
              )}
          </div>
        )}
      </Modal>
    </Drawer>
  );
}
