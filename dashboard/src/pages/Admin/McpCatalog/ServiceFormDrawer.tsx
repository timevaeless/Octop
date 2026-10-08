import {
  AutoComplete,
  Button,
  Collapse,
  Drawer,
  Form,
  Input,
  InputNumber,
  Select,
  Spin,
  Tag,
  Typography,
} from "antd";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { OrgInfo } from "../../../api/modules/roles";
import type {
  McpCatalogService,
  McpServiceInput,
  McpServicePatch,
  McpSource,
} from "../../../api/modules/mcpCatalog";
import { mcpCatalogApi } from "../../../api/modules/mcpCatalog";
import { rolesApi } from "../../../api/modules/roles";
import { message } from "../../../utils/antdMessage";
import { apiErrorMessage } from "../../../utils/apiError";

export interface ServiceFormDrawerProps {
  open: boolean;
  /** Null means creating a new service. */
  service: McpCatalogService | null;
  onClose: () => void;
  onSaved: () => void;
}

interface FormValues {
  service_id: string;
  display_name: string;
  description?: string;
  nacos_namespace: string;
  nacos_group: string;
  nacos_service_name: string;
  backend_port: number;
  openapi_url?: string;
  mcp_path: string;
  allowed_roles?: string[];
  /** 034: Select cannot hold null — the platform-private option carries the sentinel. */
  org_id: string;
}

/** Sentinel for the unattributed (platform-private) option in the Select. */
const ORG_PRIVATE = "__platform_private__";

function slugify(s: string): string {
  // service_id whitelist is ^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$ (router); Chinese
  // display names slug to "" → no auto-suggest, the admin types the id.
  return s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60)
    .replace(/^-+|-+$/g, "");
}

/** Registry types with a naming backend the form can query (P3.1). */
const NAMING_TYPES = new Set(["nacos2", "nacos3", "eureka"]);

export default function ServiceFormDrawer({
  open,
  service,
  onClose,
  onSaved,
}: ServiceFormDrawerProps) {
  const { t } = useTranslation();
  const [form] = Form.useForm<FormValues>();
  const editing = service !== null;
  const [saving, setSaving] = useState(false);
  const [sources, setSources] = useState<McpSource[]>([]);
  const [sourcesLoading, setSourcesLoading] = useState(false);
  const [sourcesFailed, setSourcesFailed] = useState(false);
  // P2: the selected source IS the persisted registry_name (create only).
  const [sourceName, setSourceName] = useState<string | null>(null);
  // P3: AutoComplete options + instance auto-fill.
  const [serviceOptions, setServiceOptions] = useState<string[]>([]);
  const [servicesLoading, setServicesLoading] = useState(false);
  const [instanceLoading, setInstanceLoading] = useState(false);
  const registryNameRef = useRef<string>("nacos");
  // 033: 服务级默认角色 —— 与工具级角色同一选项源（GET /admin/roles）
  const [roleOptions, setRoleOptions] = useState<
    { label: string; value: string }[]
  >([]);
  const [rolesLoading, setRolesLoading] = useState(false);
  // 034: 归属下拉的租户选项（GET /admin/orgs）
  const [orgs, setOrgs] = useState<OrgInfo[]>([]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    rolesApi
      .listOrgs()
      .then((rows) => {
        if (!cancelled) setOrgs(rows.filter((o) => o.org_id !== "*"));
      })
      .catch(() => {
        if (!cancelled) setOrgs([]);
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  const orgOptions = useMemo(
    () => [
      { value: "*", label: t("mcpCatalog.form.orgShared") },
      { value: ORG_PRIVATE, label: t("mcpCatalog.form.orgPrivate") },
      ...orgs.map((o) => ({
        value: o.org_id,
        label: `${o.name} (${o.org_id})`,
      })),
    ],
    [orgs, t],
  );

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setRolesLoading(true);
    rolesApi
      .list()
      .then((roles) => {
        if (!cancelled) {
          setRoleOptions(
            roles.map((r) => ({
              value: r.role_key,
              label: r.name_zh ? `${r.role_key}（${r.name_zh}）` : r.role_key,
            })),
          );
        }
      })
      .catch(() => {
        if (!cancelled) setRoleOptions([]);
      })
      .finally(() => {
        if (!cancelled) setRolesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    if (service) {
      form.setFieldsValue({
        service_id: service.service_id,
        display_name: service.display_name,
        description: service.description ?? undefined,
        nacos_namespace: service.nacos_namespace,
        nacos_group: service.nacos_group,
        nacos_service_name: service.nacos_service_name,
        backend_port: service.backend_port,
        openapi_url: service.openapi_url ?? undefined,
        mcp_path: service.mcp_path,
        allowed_roles: service.allowed_roles ?? [],
        org_id: service.org_id ?? ORG_PRIVATE,
      });
      registryNameRef.current = service.registry_name || "nacos";
    } else {
      form.resetFields();
      form.setFieldsValue({
        nacos_namespace: "local",
        nacos_group: "DEFAULT_GROUP",
        // 034: 建服务时点不知道调用者 —— 默认平台共享 + 全放行，租户层再收窄
        org_id: "*",
        allowed_roles: ["*"],
      });
      registryNameRef.current = "nacos";
    }
    setSourceName(editing ? service?.registry_name || "nacos" : null);
    setServiceOptions([]);
  }, [open, service, form, editing]);

  // v2 设计 §4.1：「服务来源」下拉实时读 Higress mcpbridges（只读）。
  // 读取失败显式提示、不造假数据——手填四元组的老路已删，来源只能来自 Higress。
  useEffect(() => {
    if (!open || editing) return;
    let cancelled = false;
    setSourcesLoading(true);
    setSourcesFailed(false);
    mcpCatalogApi
      .listSources()
      .then((data) => {
        if (!cancelled) setSources(data.sources ?? []);
      })
      .catch(() => {
        if (!cancelled) setSourcesFailed(true);
      })
      .finally(() => {
        if (!cancelled) setSourcesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, editing]);

  // display_name → service_id 自动建议（slug 合法且用户没手改过才写）
  const displayName = Form.useWatch("display_name", form);
  useEffect(() => {
    if (!open || editing || !displayName) return;
    if (form.isFieldTouched("service_id")) return;
    const slug = slugify(String(displayName));
    if (slug.length < 3) return;
    form.setFieldValue("service_id", slug);
  }, [displayName, editing, open, form]);

  // service_id → mcp_path 自动建议（缺省规则 /api/internal/mcp/{id}）
  const serviceId = Form.useWatch("service_id", form);
  useEffect(() => {
    if (!open || editing || !serviceId) return;
    if (form.isFieldTouched("mcp_path")) return;
    form.setFieldValue("mcp_path", `/api/internal/mcp/${serviceId}`);
  }, [serviceId, editing, open, form]);

  const loadServiceNames = (src: McpSource) => {
    if (!NAMING_TYPES.has(src.type)) return;
    setServicesLoading(true);
    mcpCatalogApi
      .listSourceServices(src.name)
      .then((data) => setServiceOptions(data.services ?? []))
      .catch(() => {
        // P3 验收口径：naming 失败退化手输——清空选项即可，不打断表单
        setServiceOptions([]);
      })
      .finally(() => setServicesLoading(false));
  };

  // 点击/聚焦即拉取（此前仅在切换来源时拉一次，失败或清空后点不出下拉）
  const onServiceNameFocus = () => {
    const src = sources.find((s) => s.name === sourceName);
    if (src && serviceOptions.length === 0) loadServiceNames(src);
  };

  const onSourceChange = (name: string) => {
    setSourceName(name || null);
    registryNameRef.current = name || "nacos";
    setServiceOptions([]);
    const src = sources.find((s) => s.name === name);
    if (!src) return;
    // 选中 nacos 来源时预填其默认 namespace/group（registry 自带）
    if (
      (src.type === "nacos2" || src.type === "nacos3") &&
      !form.isFieldTouched("nacos_namespace")
    ) {
      form.setFieldValue("nacos_namespace", src.nacos_namespace_id || "local");
    }
    if (
      (src.type === "nacos2" || src.type === "nacos3") &&
      src.nacos_group &&
      !form.isFieldTouched("nacos_group")
    ) {
      form.setFieldValue("nacos_group", src.nacos_group);
    }
    if (NAMING_TYPES.has(src.type)) loadServiceNames(src);
  };

  // P3.2: 选中注册中心里的服务 → 端口/ns/group 自动带出
  const onServiceNameSelect = (name: string) => {
    const src = sources.find((s) => s.name === sourceName);
    if (!src || !NAMING_TYPES.has(src.type) || !name) return;
    setInstanceLoading(true);
    mcpCatalogApi
      .getSourceInstance(src.name, name)
      .then(({ instance }) => {
        if (instance.port && !form.isFieldTouched("backend_port")) {
          form.setFieldValue("backend_port", instance.port);
        }
        if (
          instance.namespace &&
          !form.isFieldTouched("nacos_namespace") &&
          src.type !== "eureka"
        ) {
          form.setFieldValue("nacos_namespace", instance.namespace);
        }
        if (
          instance.group &&
          !form.isFieldTouched("nacos_group") &&
          src.type !== "eureka"
        ) {
          form.setFieldValue("nacos_group", instance.group);
        }
      })
      .catch(() => {
        // 拿不到实例（未注册/网络抖动）→ 保留手输值，静默
      })
      .finally(() => setInstanceLoading(false));
  };

  const submit = async () => {
    const values = await form.validateFields();
    setSaving(true);
    // 034: Select 哨兵映射回 null（平台私有）
    const orgId: string | null =
      values.org_id === ORG_PRIVATE ? null : values.org_id;
    try {
      if (editing && service) {
        const patch: McpServicePatch = {
          display_name: values.display_name,
          description: values.description || null,
          nacos_namespace: values.nacos_namespace,
          nacos_group: values.nacos_group,
          nacos_service_name: values.nacos_service_name,
          backend_port: values.backend_port,
          openapi_url: values.openapi_url || null,
          mcp_path: values.mcp_path,
          allowed_roles: values.allowed_roles ?? [],
          org_id: orgId,
        };
        await mcpCatalogApi.updateService(service.service_id, patch);
        message.success(t("mcpCatalog.updateOk"));
      } else {
        const body: McpServiceInput = {
          service_id: values.service_id,
          display_name: values.display_name,
          description: values.description || null,
          nacos_namespace: values.nacos_namespace,
          nacos_group: values.nacos_group,
          nacos_service_name: values.nacos_service_name,
          backend_port: values.backend_port,
          openapi_url: values.openapi_url || null,
          mcp_path: values.mcp_path,
          // P2: 来源下拉即 registry_name（未选=默认 nacos 遗留路径）
          registry_name: sourceName || "nacos",
          // 033: 服务级默认角色（工具未单独配置时继承）
          allowed_roles: values.allowed_roles ?? [],
          // 034: 归属（默认平台共享）
          org_id: orgId,
        };
        await mcpCatalogApi.createService(body);
        message.success(t("mcpCatalog.createOk"));
      }
      onClose();
      onSaved();
    } catch (err) {
      message.error(apiErrorMessage(err, t("mcpCatalog.loadFailed"), t));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Drawer
      open={open}
      onClose={onClose}
      title={editing ? t("mcpCatalog.editService") : t("mcpCatalog.newService")}
      width={640}
      destroyOnClose
      footer={
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <Button onClick={onClose}>{t("common.cancel")}</Button>
          <Button type="primary" loading={saving} onClick={() => form.submit()}>
            {t("common.save")}
          </Button>
        </div>
      }
    >
      <Form
        form={form}
        layout="vertical"
        onFinish={submit}
        requiredMark={false}
      >
        <Form.Item
          name="service_id"
          label={t("mcpCatalog.form.serviceId")}
          extra={t("mcpCatalog.form.serviceIdHelp")}
          rules={[{ required: true }]}
        >
          <Input disabled={editing} maxLength={64} />
        </Form.Item>
        <Form.Item
          name="display_name"
          label={t("mcpCatalog.form.displayName")}
          rules={[{ required: true }]}
        >
          <Input maxLength={128} />
        </Form.Item>
        <Form.Item name="description" label={t("mcpCatalog.form.description")}>
          <Input.TextArea rows={2} />
        </Form.Item>
        {!editing && (
          <Form.Item
            name="source"
            label={t("mcpCatalog.form.source")}
            extra={
              sourcesFailed
                ? t("mcpCatalog.form.sourceLoadFailed")
                : t("mcpCatalog.form.sourceHelp")
            }
            validateStatus={sourcesFailed ? "warning" : undefined}
          >
            <Select
              loading={sourcesLoading}
              allowClear
              placeholder={t("mcpCatalog.form.sourcePlaceholder")}
              options={sources.map((s) => ({
                value: s.name,
                label: (
                  <span>
                    {`${s.name}（${s.type} · ${s.domain}${
                      s.port ? `:${s.port}` : ""
                    }）`}
                    {s.verified === false && (
                      <Tag color="orange" style={{ marginLeft: 6 }}>
                        {t("mcpCatalog.form.experimental")}
                      </Tag>
                    )}
                  </span>
                ),
              }))}
              onChange={(v) => onSourceChange(String(v))}
            />
          </Form.Item>
        )}
        <Form.Item
          name="nacos_service_name"
          label={t("mcpCatalog.form.nacosServiceName")}
          extra={
            serviceOptions.length
              ? t("mcpCatalog.form.nacosServiceNameLive", {
                  count: serviceOptions.length,
                })
              : undefined
          }
          rules={[{ required: true }]}
        >
          <AutoComplete
            options={serviceOptions.map((n) => ({ value: n }))}
            filterOption={(input, option) =>
              String(option?.value ?? "")
                .toLowerCase()
                .includes(input.toLowerCase())
            }
            onSelect={(v) => onServiceNameSelect(String(v))}
            onFocus={onServiceNameFocus}
            disabled={instanceLoading}
            notFoundContent={
              servicesLoading ? (
                <Spin
                  size="small"
                  style={{ display: "block", margin: "12px auto" }}
                />
              ) : !sourceName ? (
                <Typography.Text type="secondary">
                  {t("mcpCatalog.form.pickSourceFirst")}
                </Typography.Text>
              ) : NAMING_TYPES.has(
                  sources.find((s) => s.name === sourceName)?.type ?? "",
                ) ? (
                <Typography.Text type="secondary">
                  {t("mcpCatalog.form.noMatchingService")}
                </Typography.Text>
              ) : (
                <Typography.Text type="secondary">
                  {t("mcpCatalog.form.noNamingBackend")}
                </Typography.Text>
              )
            }
          >
            <Input
              maxLength={128}
              placeholder={t("mcpCatalog.form.nacosServiceNamePlaceholder")}
            />
          </AutoComplete>
        </Form.Item>
        <Form.Item
          name="backend_port"
          label={t("mcpCatalog.form.backendPort")}
          rules={[{ required: true }]}
        >
          <InputNumber min={1} max={65535} style={{ width: "100%" }} />
        </Form.Item>
        <Collapse
          ghost
          size="small"
          items={[
            {
              key: "advanced",
              label: t("mcpCatalog.form.advanced"),
              children: (
                <>
                  <Form.Item
                    name="nacos_namespace"
                    label={t("mcpCatalog.form.nacosNamespace")}
                  >
                    <Input maxLength={64} />
                  </Form.Item>
                  <Form.Item
                    name="nacos_group"
                    label={t("mcpCatalog.form.nacosGroup")}
                  >
                    <Input maxLength={64} />
                  </Form.Item>
                </>
              ),
            },
          ]}
        />
        <Form.Item
          name="mcp_path"
          label={t("mcpCatalog.form.mcpPath")}
          extra={t("mcpCatalog.form.mcpPathHelp")}
          rules={[{ required: true }]}
        >
          <Input maxLength={128} />
        </Form.Item>
        <Form.Item
          name="org_id"
          label={t("mcpCatalog.form.attribution")}
          extra={t("mcpCatalog.form.attributionHelp")}
          rules={[{ required: true }]}
        >
          <Select
            showSearch
            optionFilterProp="label"
            options={orgOptions}
            notFoundContent={t("mcpCatalog.form.orgLoadFailed")}
          />
        </Form.Item>
        <Form.Item
          name="allowed_roles"
          label={t("mcpCatalog.form.defaultRoles")}
          extra={t("mcpCatalog.form.defaultRolesHelp")}
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
        <Form.Item name="openapi_url" label={t("mcpCatalog.form.openapiUrl")}>
          <Input
            maxLength={512}
            placeholder={t("mcpCatalog.form.openapiUrlPlaceholder")}
          />
        </Form.Item>
      </Form>
    </Drawer>
  );
}
