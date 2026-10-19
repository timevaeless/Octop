import { request } from "../request";

/** Backend router: src/octop/mcp_catalog/router.py, mounted at /api/admin/mcp-catalog. */
const BASE = "/admin/mcp-catalog";

export type McpServiceStatus = "draft" | "published" | "disabled";

export type McpParamLocation = "path" | "query" | "body";

export interface McpCatalogService {
  service_id: string;
  display_name: string;
  description: string | null;
  status: McpServiceStatus;
  nacos_namespace: string;
  nacos_group: string;
  nacos_service_name: string;
  backend_port: number;
  openapi_url: string | null;
  mcp_path: string;
  registry_name?: string;
  /** 033: service-level default roles, inherited by tools left empty. */
  allowed_roles?: string[];
  /**
   * 034 three-state attribution: ``null`` = platform-private (unattributed),
   * ``"*"`` = platform share, a tenant id = attributed to that tenant.
   * Decides WHO edits the ACL — the platform here, or the tenant admin via
   * the tenant resource-ACL page.
   */
  org_id?: string | null;
  /**
   * Number of tools in this service's catalog. Optional so the page still
   * renders against a backend that predates the field.
   */
  tool_count?: number;
}

export interface McpCatalogToolParam {
  name: string;
  location: McpParamLocation;
  required?: boolean;
  description?: string | null;
}

export type McpHttpMethod = "GET" | "POST" | "PUT" | "DELETE";

export interface McpCatalogTool {
  tool_name: string;
  description: string | null;
  http_method: McpHttpMethod;
  http_path: string;
  parameters: McpCatalogToolParam[];
  request_body: string | null;
  response_body: string | null;
  allowed_roles: string[];
  enabled: boolean;
  source?: string;
}

export interface McpImportResult {
  imported: string[];
  overwritten?: string[];
  skipped: { path: string; method: string; reason: string }[];
  deleted: string[];
  dry_run?: boolean;
}

/** v2 P4.3: tool-level diff between the catalog DB and the live gateway config. */
export interface McpPublishDiff {
  published: boolean;
  added: string[];
  updated: string[];
  removed: string[];
  unchanged: string[];
}

export interface McpPublishResult {
  dry_run: boolean;
  acl: Record<string, string[]>;
  /** v3 Console 通道：一条路由 + 路由级插件实例（不再有 ingress YAML）。 */
  route: {
    name: string;
    path: string;
    service: { name: string; port: number };
    action: string | null;
  };
  plugins: Record<string, string>;
}

export interface McpUnpublishResult {
  dry_run: boolean;
  written: boolean;
}

/** One Higress mcpbridge registry — the "服务来源" dropdown, live-read (v2). */
export interface McpSource {
  name: string;
  type: string;
  domain: string;
  port: number | null;
  /** nacos registries only: default namespace for form prefill. */
  nacos_namespace_id?: string;
  nacos_group?: string;
  /** P2.3: false = destination format not live-verified (「实验」tag, publish-gated). */
  verified?: boolean;
}

export interface McpPublishLogEntry {
  action: string;
  result: string;
  actor: string | null;
  created_at: string;
  detail: string | null;
}

export interface McpServiceInput {
  service_id: string;
  display_name: string;
  description?: string | null;
  nacos_namespace: string;
  nacos_group: string;
  nacos_service_name: string;
  backend_port: number;
  openapi_url?: string | null;
  mcp_path: string;
  registry_name?: string;
  allowed_roles?: string[];
  /** 034: ``"*"`` = platform share (default), ``null`` = platform-private. */
  org_id?: string | null;
}

export interface McpServicePatch {
  display_name?: string;
  description?: string | null;
  nacos_namespace?: string;
  nacos_group?: string;
  nacos_service_name?: string;
  backend_port?: number;
  openapi_url?: string | null;
  mcp_path?: string;
  registry_name?: string;
  /** 033: send [] to clear the defaults; absent = no change. */
  allowed_roles?: string[];
  /** 034: absent = no change; ``null`` = re-attribute to platform-private. */
  org_id?: string | null;
}

export interface McpToolInput {
  tool_name: string;
  description: string | null;
  http_method: McpHttpMethod;
  http_path: string;
  parameters: McpCatalogToolParam[];
  request_body: string | null;
  response_body: string | null;
  allowed_roles: string[];
  enabled: boolean;
}

function sid(serviceId: string): string {
  return encodeURIComponent(serviceId);
}

function tid(toolName: string): string {
  return encodeURIComponent(toolName);
}

export const mcpCatalogApi = {
  /** Live-read Higress mcpbridges.default registries (read-only, v2 design). */
  listSources(): Promise<{ sources: McpSource[] }> {
    return request<{ sources: McpSource[] }>(`${BASE}/sources`);
  },

  /** P3.1: registered service names in one registry (AutoComplete source). */
  listSourceServices(
    registryName: string,
  ): Promise<{ services: string[]; registry_type: string }> {
    return request(
      `${BASE}/sources/${encodeURIComponent(registryName)}/services`,
    );
  },

  /** P3.2: one service's instance (ip/port) for form auto-fill. */
  getSourceInstance(
    registryName: string,
    serviceName: string,
  ): Promise<{
    instance: {
      ip: string;
      port: number | null;
      namespace: string | null;
      group: string | null;
    };
  }> {
    return request(
      `${BASE}/sources/${encodeURIComponent(
        registryName,
      )}/services/${encodeURIComponent(serviceName)}/instance`,
    );
  },

  /** P4.3: tool-level diff before publish (DB vs live gateway config). */
  publishDiff(serviceId: string): Promise<McpPublishDiff> {
    return request(`${BASE}/services/${sid(serviceId)}/publish-diff`);
  },

  listServices(): Promise<{ services: McpCatalogService[] }> {
    return request<{ services: McpCatalogService[] }>(`${BASE}/services`);
  },

  createService(body: McpServiceInput): Promise<{ service_id: string }> {
    return request(`${BASE}/services`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  updateService(
    serviceId: string,
    patch: McpServicePatch,
  ): Promise<{ service_id: string }> {
    return request(`${BASE}/services/${sid(serviceId)}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
  },

  deleteService(serviceId: string): Promise<{ deleted: string }> {
    return request(`${BASE}/services/${sid(serviceId)}`, { method: "DELETE" });
  },

  listTools(serviceId: string): Promise<{ tools: McpCatalogTool[] }> {
    return request<{ tools: McpCatalogTool[] }>(
      `${BASE}/services/${sid(serviceId)}/tools`,
    );
  },

  putTool(
    serviceId: string,
    body: McpToolInput,
  ): Promise<{ tool_name: string; allowed_roles: string[] }> {
    return request(
      `${BASE}/services/${sid(serviceId)}/tools/${tid(body.tool_name)}`,
      { method: "PUT", body: JSON.stringify(body) },
    );
  },

  deleteTool(
    serviceId: string,
    toolName: string,
  ): Promise<{ deleted: string }> {
    return request(
      `${BASE}/services/${sid(serviceId)}/tools/${tid(toolName)}`,
      { method: "DELETE" },
    );
  },

  /** Batch delete selected tools; the response lists what actually went away
   * (unknown names are skipped server-side, not an error). */
  batchDeleteTools(
    serviceId: string,
    toolNames: string[],
  ): Promise<{ deleted: string[] }> {
    return request(`${BASE}/services/${sid(serviceId)}/tools/batch-delete`, {
      method: "POST",
      body: JSON.stringify({ tool_names: toolNames }),
    });
  },

  /** P1.4: preview the tools[] entry this draft renders into the plugin config. */
  previewTool(
    serviceId: string,
    body: McpToolInput,
  ): Promise<{ plugin_tool: Record<string, unknown> }> {
    return request(`${BASE}/services/${sid(serviceId)}/tools/preview`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  importOpenApi(
    serviceId: string,
    opts: {
      openapiUrl?: string;
      /** P4.1: paste/upload channel — raw OpenAPI JSON document. */
      openapiBody?: string;
      /** P4.2: preview — compute imported/overwritten/deleted without writing. */
      dryRun?: boolean;
    } = {},
  ): Promise<McpImportResult> {
    return request(`${BASE}/services/${sid(serviceId)}/import-openapi`, {
      method: "POST",
      body: JSON.stringify({
        openapi_url: opts.openapiUrl,
        openapi_body: opts.openapiBody,
        dry_run: opts.dryRun ?? false,
      }),
    });
  },

  publish(serviceId: string, dryRun = false): Promise<McpPublishResult> {
    return request(`${BASE}/services/${sid(serviceId)}/publish`, {
      method: "POST",
      body: JSON.stringify({ dry_run: dryRun }),
    });
  },

  unpublish(serviceId: string, dryRun = false): Promise<McpUnpublishResult> {
    return request(`${BASE}/services/${sid(serviceId)}/unpublish`, {
      method: "POST",
      body: JSON.stringify({ dry_run: dryRun }),
    });
  },

  reconcile(serviceId: string): Promise<{ reconciled: number }> {
    return request(`${BASE}/services/${sid(serviceId)}/reconcile`, {
      method: "POST",
    });
  },

  publishLog(serviceId: string): Promise<{ log: McpPublishLogEntry[] }> {
    return request<{ log: McpPublishLogEntry[] }>(
      `${BASE}/services/${sid(serviceId)}/publish-log`,
    );
  },

  // v2 追加：Higress 入口地址（部署拓扑相关，存服务端 settings KV）
  // hub_endpoint：发布时注入 ext-auth 的 mcp-hub 端点（host:port；空串=回默认）
  getConnectorSettings(): Promise<{
    higress_base: string;
    hub_endpoint?: string;
  }> {
    return request(`${BASE}/connector-settings`);
  },

  putConnectorSettings(
    higressBase: string,
    hubEndpoint?: string,
  ): Promise<{ higress_base: string; hub_endpoint?: string }> {
    return request(`${BASE}/connector-settings`, {
      method: "PUT",
      body: JSON.stringify({
        higress_base: higressBase,
        ...(hubEndpoint !== undefined ? { hub_endpoint: hubEndpoint } : {}),
      }),
    });
  },

  // 一键把已发布服务挂成当前用户的自定义 MCP 连接器（delegate_identity）
  attachConnector(
    serviceId: string,
    displayName?: string,
  ): Promise<{
    server_name: string;
    url: string;
    action: "created" | "updated";
    enabled_servers: string[];
  }> {
    return request(`${BASE}/services/${sid(serviceId)}/attach-connector`, {
      method: "POST",
      body: JSON.stringify({
        display_name: displayName ?? null,
        enabled: true,
      }),
    });
  },
};
