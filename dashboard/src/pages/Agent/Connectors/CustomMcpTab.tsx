import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Button,
  Checkbox,
  Input,
  Popconfirm,
  Segmented,
  Spin,
  Table,
  type TableProps,
} from "antd";
import { message } from "@/utils/antdMessage";

import { ArrowLeft, Globe, Plus, Terminal, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  connectorsApi,
  type ConnectorProbeResult,
  type CustomMcpConnector,
  type CustomMcpServers,
  type CustomMcpTransport,
} from "../../../api/modules/connectors";
import { apiErrorMessage } from "../../../utils/apiError";
import { CustomMcpServerCard } from "./CustomMcpServerCard";
import {
  PROBE_ON_SAVE_KEY,
  cardsToServers,
  hasHttpProbeTargets,
  mergeCustomMcpCards,
  newCard,
  notifyConnectorsChanged,
  oauthHintsFromServers,
  serversToCards,
  type EditorMode,
  type ServerCardState,
} from "./customMcpUtils";
import {
  isAuthPopupBlocked,
  navigateAuthWindow,
  tryOpenAuthPopup,
} from "./openAuthWindow";
import styles from "./index.module.less";

interface CustomMcpTabProps {
  focusServerName?: string | null;
}

export function CustomMcpTab({ focusServerName }: CustomMcpTabProps) {
  const { t } = useTranslation();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [probingKey, setProbingKey] = useState<string | null>(null);
  const [probeResults, setProbeResults] = useState<
    Record<string, { name: string; description: string }[]>
  >({});
  const [mode, setMode] = useState<EditorMode>("visual");
  const [cards, setCards] = useState<ServerCardState[]>([]);
  const [jsonText, setJsonText] = useState("{}");
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [probeOnSave, setProbeOnSave] = useState(
    () => localStorage.getItem(PROBE_ON_SAVE_KEY) === "1",
  );
  const [oauthAvailable, setOauthAvailable] = useState<Record<string, boolean>>(
    {},
  );
  const [authorizingKey, setAuthorizingKey] = useState<string | null>(null);
  const [persistedNames, setPersistedNames] = useState<Set<string>>(
    () => new Set(),
  );
  // T4 multi-doc: one connector = one document; the editor below edits the
  // ACTIVE connector only. `activeId === null` means a fresh draft.
  const [connectors, setConnectors] = useState<CustomMcpConnector[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [connName, setConnName] = useState("");
  // Two-level navigation: "list" = paginated connector table,
  // "detail" = the per-connector editor below.
  const [view, setView] = useState<"list" | "detail">("list");
  const pendingFocusRef = useRef<string | null>(null);
  // Mirror of activeId for async callbacks (OAuth finish runs long after the
  // click that captured the render-time value — a draft's real id must be
  // visible there).
  const activeIdRef = useRef<string | null>(null);
  useEffect(() => {
    activeIdRef.current = activeId;
  }, [activeId]);

  useEffect(() => {
    localStorage.setItem(PROBE_ON_SAVE_KEY, probeOnSave ? "1" : "0");
  }, [probeOnSave]);

  const activeDoc = useMemo(
    () => connectors.find((c) => c.instance_id === activeId) ?? null,
    [connectors, activeId],
  );

  const applySavedServers = (
    servers: CustomMcpServers,
    prevCards: ServerCardState[],
  ) => {
    let nextCards = mergeCustomMcpCards(servers, prevCards);
    const focus = pendingFocusRef.current;
    if (focus) {
      pendingFocusRef.current = null;
      nextCards = nextCards.map((card) =>
        card.name.trim() === focus ? { ...card, collapsed: false } : card,
      );
    }
    setCards(nextCards);
    setPersistedNames(new Set(Object.keys(servers)));
    setJsonText(JSON.stringify(servers, null, 2));
    setOauthAvailable(oauthHintsFromServers(servers, nextCards));
    return nextCards;
  };

  const refreshConnectors = useCallback(async () => {
    const { connectors: list } = await connectorsApi.listCustomMcpConnectors();
    setConnectors(list);
    return list;
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const list = await refreshConnectors();
      setActiveId((prev) => {
        if (prev && list.some((c) => c.instance_id === prev)) return prev;
        return list[0]?.instance_id ?? null;
      });
      setProbeResults({});
      setJsonError(null);
    } catch (e) {
      console.error(e);
      message.error(
        apiErrorMessage(
          e,
          t("connectors.customMcp.loadFailed", "加载自定义 MCP 失败"),
          t,
        ),
      );
    } finally {
      setLoading(false);
    }
  }, [t, refreshConnectors]);

  useEffect(() => {
    void load();
  }, [load]);

  // Apply the active connector's document whenever the selection changes
  // (or after the initial load). Saving/patching refreshes the document
  // explicitly instead of riding on this effect, so in-progress edits are
  // never clobbered by a background list refresh.
  useEffect(() => {
    if (loading) return;
    setConnName(activeDoc?.name ?? "");
    applySavedServers(activeDoc?.servers ?? {}, []);
    setJsonError(null);
    setProbeResults({});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId, loading]);

  const lastFocusRef = useRef<string | null>(null);
  useEffect(() => {
    if (loading) return;
    if (!focusServerName) {
      // Parent clears the focus (e.g. re-clicking the tab) — re-arm so the
      // same server can be focused again later.
      lastFocusRef.current = null;
      return;
    }
    // Consume-once: the parent keeps this prop set after the initial jump,
    // and every list refresh would otherwise re-run this effect and drag the
    // user back into the detail view from the list.
    if (lastFocusRef.current === focusServerName) return;
    lastFocusRef.current = focusServerName;
    setView("detail");
    setMode("visual");
    // If the focused server lives in another connector, switch to it —
    // the expansion lands via pendingFocusRef once that document applies.
    pendingFocusRef.current = focusServerName;
    const doc = connectors.find((c) =>
      Object.keys(c.servers).includes(focusServerName),
    );
    if (doc && doc.instance_id !== activeId) {
      setActiveId(doc.instance_id);
      return;
    }
    setCards((prev) =>
      prev.map((card) =>
        card.name.trim() === focusServerName
          ? { ...card, collapsed: false }
          : card,
      ),
    );
  }, [focusServerName, loading, connectors, activeId]);

  const persistServerPatch = async (
    card: ServerCardState,
    apiPatch: {
      enabled?: boolean;
      default_open?: boolean;
      shared?: boolean;
    },
    localPatch: Partial<ServerCardState>,
  ) => {
    const optimisticCards = cards.map((item) =>
      item.key === card.key ? { ...item, ...localPatch } : item,
    );
    updateCard(card.key, localPatch);
    const name = card.name.trim();
    if (!name || !persistedNames.has(name)) {
      return;
    }
    try {
      await connectorsApi.patchCustomMcpServer(name, apiPatch);
      const list = await refreshConnectors();
      const doc = list.find((c) => c.instance_id === activeId);
      applySavedServers(doc?.servers ?? {}, optimisticCards);
      notifyConnectorsChanged();
    } catch (e) {
      console.error(e);
      message.error(
        apiErrorMessage(e, t("connectors.customMcp.saveFailed", "保存失败"), t),
      );
      await load();
    }
  };

  const syncJsonFromCards = useCallback((nextCards: ServerCardState[]) => {
    try {
      const servers = cardsToServers(nextCards);
      setJsonText(JSON.stringify(servers, null, 2));
      setJsonError(null);
    } catch {
      // keep previous json while visual has incomplete names
    }
  }, []);

  const updateCard = (key: string, patch: Partial<ServerCardState>) => {
    setCards((prev) => {
      const next = prev.map((card) =>
        card.key === key ? { ...card, ...patch } : card,
      );
      syncJsonFromCards(next);
      return next;
    });
  };

  const handleModeChange = (nextMode: EditorMode) => {
    if (nextMode === mode) return;
    if (nextMode === "json") {
      try {
        const servers = cardsToServers(cards);
        setJsonText(JSON.stringify(servers, null, 2));
        setJsonError(null);
      } catch (e) {
        message.warning(
          t(
            "connectors.customMcp.visualInvalid",
            "可视化配置不完整，请先修正名称与必填项",
          ),
        );
        console.error(e);
        return;
      }
    } else {
      try {
        const parsed = JSON.parse(jsonText) as unknown;
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
          throw new Error("root must be object");
        }
        const servers = parsed as CustomMcpServers;
        setCards(serversToCards(servers));
        setJsonError(null);
      } catch {
        setJsonError(
          t(
            "connectors.customMcp.jsonInvalid",
            "JSON 格式无效，无法切换到可视化",
          ),
        );
        message.error(
          t(
            "connectors.customMcp.jsonInvalid",
            "JSON 格式无效，无法切换到可视化",
          ),
        );
        return;
      }
    }
    setMode(nextMode);
  };

  const handleAdd = (transport: CustomMcpTransport) => {
    setCards((prev) => {
      const next = [...prev, newCard(transport, prev.length)];
      syncJsonFromCards(next);
      return next;
    });
    setMode("visual");
  };

  const handleRemove = async (key: string) => {
    const card = cards.find((item) => item.key === key);
    const serverName = card?.name.trim() ?? "";
    const nextCards = cards.filter((item) => item.key !== key);
    setCards(nextCards);
    syncJsonFromCards(nextCards);
    setProbeResults((prev) => {
      if (!(key in prev)) return prev;
      const next = { ...prev };
      delete next[key];
      return next;
    });
    setOauthAvailable((prev) => {
      if (!(key in prev)) return prev;
      const next = { ...prev };
      delete next[key];
      return next;
    });

    if (!serverName || !persistedNames.has(serverName)) {
      return;
    }

    try {
      await connectorsApi.deleteInstance(`custom:${serverName}`);
      const list = await refreshConnectors();
      setConnectors(list);
      const doc = list.find((c) => c.instance_id === activeId);
      if (!doc) {
        // The last server of this connector was just removed — the row is
        // gone; fall back to the list head instead of a stale selection.
        setActiveId(list[0]?.instance_id ?? null);
      }
      applySavedServers(doc?.servers ?? {}, nextCards);
      notifyConnectorsChanged();
      message.success(t("connectors.deleteSuccess", "已删除"));
    } catch (e) {
      console.error(e);
      message.error(
        apiErrorMessage(e, t("connectors.deleteFailed", "删除失败"), t),
      );
      await load();
    }
  };

  const clearProbeResult = (key: string) => {
    setProbeResults((prev) => {
      if (!(key in prev)) return prev;
      const next = { ...prev };
      delete next[key];
      return next;
    });
    setOauthAvailable((prev) => {
      if (!(key in prev)) return prev;
      const next = { ...prev };
      delete next[key];
      return next;
    });
  };

  const applyProbeResult = (
    card: ServerCardState,
    result: ConnectorProbeResult,
    options: { fromSave?: boolean } = {},
  ) => {
    const fromSave = options.fromSave === true;
    if (result.ok) {
      const tools = result.tools ?? [];
      setProbeResults((prev) => ({ ...prev, [card.key]: tools }));
      setOauthAvailable((prev) => ({ ...prev, [card.key]: false }));
      if (!fromSave) {
        updateCard(card.key, { collapsed: false });
      }
      if (!fromSave) {
        if (tools.length === 0) {
          message.success(
            t("connectors.probeToolsEmpty", "连接正常，但未发现可用工具"),
          );
        } else {
          message.success(t("connectors.customMcp.probeComplete", "连接正常"));
        }
      }
      return;
    }
    setProbeResults((prev) => {
      const next = { ...prev };
      delete next[card.key];
      return next;
    });
    if (result.oauth?.available) {
      setOauthAvailable((prev) => ({ ...prev, [card.key]: true }));
      if (fromSave) {
        updateCard(card.key, { enabled: false, defaultOpen: false });
      } else {
        updateCard(card.key, {
          collapsed: false,
          enabled: false,
          defaultOpen: false,
        });
      }
      if (!fromSave) {
        message.warning(
          t(
            "connectors.customMcp.probeNeedsOAuth",
            "此 MCP 需要 OAuth 授权才能访问",
          ),
        );
      }
      return;
    }
    setOauthAvailable((prev) => ({ ...prev, [card.key]: false }));
    if (!fromSave) {
      message.error(result.error ?? t("connectors.probeFailed", "探测失败"));
    }
  };

  const runProbe = async (
    card: ServerCardState,
    options: { byName?: boolean; fromSave?: boolean } = {},
  ) => {
    const byName = options.byName === true;
    const fromSave = options.fromSave === true;
    setProbingKey(card.key);
    clearProbeResult(card.key);
    try {
      let result: ConnectorProbeResult;
      if (byName || card.oauthConfigured) {
        result = await connectorsApi.testCustomMcp({ name: card.name.trim() });
      } else {
        const map = cardsToServers([card]);
        const server = map[card.name.trim()];
        result = await connectorsApi.testCustomMcp({ server });
      }
      applyProbeResult(card, result, { fromSave });
    } catch (e) {
      console.error(e);
      message.error(
        apiErrorMessage(e, t("connectors.probeFailed", "探测失败"), t),
      );
    } finally {
      setProbingKey(null);
    }
  };

  const resolveServersForSave = (): CustomMcpServers | null => {
    if (mode === "json") {
      try {
        const parsed = JSON.parse(jsonText) as unknown;
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
          throw new Error("root must be object");
        }
        setJsonError(null);
        return parsed as CustomMcpServers;
      } catch {
        setJsonError(t("connectors.customMcp.jsonInvalid", "JSON 格式无效"));
        message.error(t("connectors.customMcp.jsonInvalid", "JSON 格式无效"));
        return null;
      }
    }
    try {
      return cardsToServers(cards);
    } catch (e) {
      const code = e instanceof Error ? e.message : "";
      if (code === "duplicate_name") {
        message.warning(
          t("connectors.customMcp.duplicateName", "服务器名称不能重复"),
        );
      } else if (code === "empty_name") {
        message.warning(
          t("connectors.customMcp.emptyName", "请填写服务器名称"),
        );
      } else {
        message.warning(
          apiErrorMessage(
            e,
            t("connectors.customMcp.visualInvalid", "请检查配置后重试"),
            t,
          ),
        );
      }
      return null;
    }
  };

  /** Persist the ACTIVE connector (create on draft, rename+save otherwise). */
  const persistActiveConnector = async (): Promise<CustomMcpConnector | null> => {
    const servers = resolveServersForSave();
    if (!servers) return null;
    const name = connName.trim();
    if (!name) {
      message.warning(
        t("connectors.customMcp.nameRequired", "请填写连接器名称"),
      );
      return null;
    }
    if (!activeId) {
      const view = await connectorsApi.createCustomMcpConnector({
        name,
        servers,
      });
      const list = await refreshConnectors();
      setActiveId(view.instance_id);
      setConnectors(list);
      applySavedServers(view.servers, cards);
      notifyConnectorsChanged();
      message.success(
        t("connectors.customMcp.createSuccess", "连接器已创建"),
      );
      return view;
    }
    if (name !== activeDoc?.name) {
      await connectorsApi.renameCustomMcpConnector(activeId, name);
    }
    const view = await connectorsApi.saveCustomMcpConnector(activeId, servers);
    const list = await refreshConnectors();
    if (!list.some((c) => c.instance_id === activeId)) {
      // Saving an EMPTY document deletes the connector server-side (legacy
      // semantics) — drop the stale selection instead of editing a ghost.
      setActiveId(list[0]?.instance_id ?? null);
    }
    applySavedServers(view?.servers ?? {}, cards);
    notifyConnectorsChanged();
    message.success(
      t("connectors.customMcp.saveSuccess", "自定义 MCP 已保存"),
    );
    return view;
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const view = await persistActiveConnector();
      if (view && probeOnSave && showProbeSection) {
        for (const card of cards) {
          if (card.transport !== "streamable_http" || !card.url.trim()) {
            continue;
          }
          await runProbe(card, { byName: true, fromSave: true });
        }
      }
    } catch (e) {
      console.error(e);
      message.error(
        apiErrorMessage(e, t("connectors.customMcp.saveFailed", "保存失败"), t),
      );
    } finally {
      setSaving(false);
    }
  };

  const handleCreateDraft = () => {
    setActiveId(null);
    setConnName("");
    applySavedServers({}, []);
    setProbeResults({});
    setMode("visual");
    setView("detail");
  };

  const handleDeleteConnector = async (instanceId?: string) => {
    const target = instanceId ?? activeId;
    if (!target) return;
    try {
      await connectorsApi.deleteCustomMcpConnector(target);
      const list = await refreshConnectors();
      setActiveId((prev) =>
        prev && list.some((c) => c.instance_id === prev)
          ? prev
          : list[0]?.instance_id ?? null,
      );
      setView("list");
      notifyConnectorsChanged();
      message.success(t("connectors.deleteSuccess", "已删除"));
    } catch (e) {
      console.error(e);
      message.error(
        apiErrorMessage(e, t("connectors.deleteFailed", "删除失败"), t),
      );
      await load();
    }
  };

  const openConnector = (instanceId: string) => {
    setActiveId(instanceId);
    setView("detail");
  };

  const formatUpdatedAt = (ts: number) => {
    if (!ts) return "—";
    return new Date(ts < 1e12 ? ts * 1000 : ts).toLocaleString();
  };

  const handleProbe = async (card: ServerCardState) => {
    try {
      cardsToServers([card]);
    } catch {
      message.warning(
        t("connectors.customMcp.probeNeedConfig", "请先填写完整配置再探测"),
      );
      return;
    }
    await runProbe(card);
  };

  const handleOAuth = async (card: ServerCardState) => {
    const serverName = card.name.trim();
    if (!serverName) {
      message.warning(t("connectors.customMcp.emptyName", "请填写服务器名称"));
      return;
    }
    const popup = tryOpenAuthPopup("", "octop-oauth", "width=520,height=720");
    if (isAuthPopupBlocked(popup)) {
      message.error(
        t(
          "connectors.oauthPopupBlocked",
          "授权窗口被浏览器拦截，请允许本站弹出窗口后重试",
        ),
      );
      return;
    }

    setAuthorizingKey(card.key);
    let settled = false;
    let pollTimer: ReturnType<typeof setInterval> | undefined;
    let timeoutTimer: ReturnType<typeof setTimeout> | undefined;
    let stateId = "";

    const cleanup = () => {
      if (pollTimer !== undefined) clearInterval(pollTimer);
      if (timeoutTimer !== undefined) clearTimeout(timeoutTimer);
      window.removeEventListener("message", onMessage);
    };

    const finish = async () => {
      if (settled) return;
      settled = true;
      cleanup();
      try {
        popup?.close();
      } catch {
        // ignore
      }
      setAuthorizingKey(null);
      try {
        const list = await refreshConnectors();
        const doc = list.find((c) => c.instance_id === activeIdRef.current);
        const nextCards = applySavedServers(doc?.servers ?? {}, cards);
        const refreshed = nextCards.find((c) => c.name.trim() === serverName);
        if (refreshed) {
          await runProbe(refreshed, { byName: true });
        }
        message.success(
          t("connectors.oauthConfigured", "已授权，可直接探测或保存"),
        );
      } catch (e) {
        console.error(e);
        message.error(
          apiErrorMessage(
            e,
            t("connectors.oauthFailed", "获取授权结果失败"),
            t,
          ),
        );
      }
    };

    const claimPending = async () => {
      if (!stateId || settled) return;
      try {
        const pending = await connectorsApi.oauthPending(stateId);
        if (pending.applied || pending.server_name) {
          await finish();
        }
      } catch {
        // keep polling until timeout
      }
    };

    const onMessage = (ev: MessageEvent) => {
      if (ev.data?.type !== "octop:connector-oauth") return;
      if (ev.data.state_id !== stateId) return;
      void claimPending();
    };

    try {
      const view = await persistActiveConnector();
      if (!view) {
        try {
          popup?.close();
        } catch {
          // ignore
        }
        setAuthorizingKey(null);
        return;
      }

      const { authorize_url, state_id } = await connectorsApi.oauthStart(
        { type: "custom_mcp", server_name: serverName },
        window.location.pathname,
      );
      stateId = state_id;
      window.addEventListener("message", onMessage);
      pollTimer = setInterval(() => {
        void claimPending();
      }, 1200);
      timeoutTimer = setTimeout(() => {
        if (settled) return;
        settled = true;
        cleanup();
        setAuthorizingKey(null);
        message.error(
          t("connectors.oauthTimedOut", "授权超时，请重试一键授权"),
        );
      }, 120_000);
      navigateAuthWindow(popup, authorize_url);
    } catch (e) {
      cleanup();
      setAuthorizingKey(null);
      try {
        popup?.close();
      } catch {
        // ignore
      }
      message.error(
        apiErrorMessage(
          e,
          t("connectors.oauthStartFailed", "无法启动 OAuth"),
          t,
        ),
      );
    }
  };

  const transportOptions = useMemo(
    () => [
      { value: "streamable_http", label: "streamable_http" },
      { value: "stdio", label: "stdio" },
    ],
    [],
  );

  const showProbeSection = useMemo(() => {
    if (mode === "visual") {
      return hasHttpProbeTargets(cards);
    }
    try {
      const parsed = JSON.parse(jsonText) as unknown;
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        return false;
      }
      return hasHttpProbeTargets(parsed as CustomMcpServers);
    } catch {
      return false;
    }
  }, [mode, cards, jsonText]);

  if (loading) {
    return (
      <div className={styles.loadingState}>
        <Spin />
      </div>
    );
  }

  if (view === "list") {
    const columns: TableProps<CustomMcpConnector>["columns"] = [
      {
        title: t("connectors.customMcp.colName", "名称"),
        dataIndex: "name",
        render: (name: string) => (
          <span className={styles.customMcpNameCell}>{name}</span>
        ),
      },
      {
        title: t("connectors.customMcp.colServers", "服务器数"),
        width: 100,
        render: (_, record) => Object.keys(record.servers ?? {}).length,
      },
      {
        title: t("connectors.customMcp.colRoles", "可见角色"),
        render: (_, record) => {
          // Server pairs raw keys with display names (same source as the
          // resource-ACL page); labels fall back to keys position-aligned.
          const shown =
            record.allowed_role_labels && record.allowed_role_labels.length > 0
              ? record.allowed_role_labels
              : record.allowed_roles;
          return shown.length > 0
            ? shown.join(", ")
            : t("connectors.customMcp.allRoles", "全部角色");
        },
      },
      {
        title: t("connectors.customMcp.colUpdated", "更新时间"),
        width: 190,
        render: (_, record) => formatUpdatedAt(record.updated_at),
      },
      {
        title: t("connectors.customMcp.colActions", "操作"),
        width: 150,
        render: (_, record) => (
          <div
            className={styles.customMcpRowActions}
            onClick={(e) => e.stopPropagation()}
          >
            <Button
              type="link"
              size="small"
              onClick={() => openConnector(record.instance_id)}
            >
              {t("connectors.customMcp.editConnector", "编辑")}
            </Button>
            <Popconfirm
              title={t(
                "connectors.customMcp.deleteConnectorConfirm",
                "删除该连接器及其全部服务器？",
              )}
              onConfirm={() => void handleDeleteConnector(record.instance_id)}
            >
              <Button type="link" size="small" danger>
                {t("connectors.customMcp.deleteRow", "删除")}
              </Button>
            </Popconfirm>
          </div>
        ),
      },
    ];
    return (
      <div className={styles.customMcpTab}>
        <div className={styles.customMcpListCard}>
          <div className={styles.customMcpListToolbar}>
            <div>
              <div className={styles.customMcpListHeading}>
                {t("connectors.customMcp.listHeading", "自定义连接器")}
              </div>
              <p className={styles.customMcpListHint}>
                {t(
                  "connectors.customMcp.listHint",
                  "每个连接器是一组自定义 MCP 服务，点击进入编辑；连接器较多时在此分页浏览。",
                )}
              </p>
            </div>
            <Button
              type="primary"
              icon={<Plus size={14} />}
              onClick={handleCreateDraft}
            >
              {t("connectors.customMcp.newConnector", "新建连接器")}
            </Button>
          </div>
          <Table<CustomMcpConnector>
            rowKey="instance_id"
            dataSource={connectors}
            columns={columns}
            pagination={{
              pageSize: 10,
              hideOnSinglePage: true,
              showSizeChanger: false,
            }}
            onRow={(record) => ({
              onClick: () => openConnector(record.instance_id),
              style: { cursor: "pointer" },
            })}
            locale={{
              emptyText: t(
                "connectors.customMcp.listEmpty",
                "还没有自定义连接器，点击右上角「新建连接器」创建。",
              ),
            }}
          />
        </div>
      </div>
    );
  }

  const saveFooter = (
    <div
      className={`${styles.customMcpFooter}${
        showProbeSection ? "" : ` ${styles.customMcpFooterSaveOnly}`
      }`}
    >
      {showProbeSection ? (
        <div className={styles.customMcpFooterMain}>
          <Checkbox
            checked={probeOnSave}
            onChange={(e) => setProbeOnSave(e.target.checked)}
          >
            {t("connectors.customMcp.probeOnSave", "保存后自动探测连接")}
          </Checkbox>
          <p className={styles.customMcpFooterHint}>
            {t(
              "connectors.customMcp.probeOnSaveHint",
              "将向您填写的 MCP 地址发起请求以验证可用性；若需登录，会引导您完成 OAuth。",
            )}
          </p>
        </div>
      ) : null}
      <Button
        type="primary"
        className={styles.customMcpFooterSave}
        loading={saving}
        onClick={() => void handleSave()}
      >
        {t("common.save")}
      </Button>
    </div>
  );

  return (
    <div className={styles.customMcpTab}>
      <div className={styles.customMcpHeader}>
        <div className={styles.customMcpHeaderRow}>
          <Button
            type="text"
            className={styles.customMcpBackBtn}
            icon={<ArrowLeft size={14} />}
            onClick={() => setView("list")}
          >
            {t("connectors.customMcp.backToList", "返回列表")}
          </Button>
          <div className={styles.customMcpHeaderSpacer} />
          {activeId ? (
            <Popconfirm
              title={t(
                "connectors.customMcp.deleteConnectorConfirm",
                "删除该连接器及其全部服务器？",
              )}
              onConfirm={() => void handleDeleteConnector()}
            >
              <Button danger type="text" icon={<Trash2 size={14} />}>
                {t("connectors.customMcp.deleteConnector", "删除连接器")}
              </Button>
            </Popconfirm>
          ) : null}
        </div>
        <div className={styles.customMcpHeaderRow}>
          <span className={styles.customMcpFieldLabel}>
            {t("connectors.customMcp.connectorName", "连接器名称")}
          </span>
          <Input
            className={styles.customMcpNameInput}
            value={connName}
            onChange={(e) => setConnName(e.target.value)}
            placeholder={t(
              "connectors.customMcp.namePlaceholder",
              "显示在资源授权页的名称",
            )}
          />
          <div className={styles.customMcpHeaderSpacer} />
          <Segmented
            value={mode}
            onChange={(value) => handleModeChange(value as EditorMode)}
            options={[
              {
                value: "visual",
                label: t("connectors.customMcp.modeVisual", "可视化"),
              },
              {
                value: "json",
                label: t("connectors.customMcp.modeJson", "</> JSON"),
              },
            ]}
          />
        </div>
        {!activeId ? (
          <div className={styles.customMcpDraftHint}>
            {t(
              "connectors.customMcp.draftHint",
              "正在新建连接器：填写名称并添加服务器后点「保存」完成创建。",
            )}
          </div>
        ) : null}
      </div>

      {mode === "json" ? (
        <>
          <div className={styles.customMcpPanel}>
            <div className={styles.customMcpJsonEditor}>
              <Input.TextArea
                value={jsonText}
                onChange={(e) => {
                  setJsonText(e.target.value);
                  setJsonError(null);
                }}
                autoSize={{ minRows: 16, maxRows: 32 }}
                className={styles.customMcpJsonArea}
                spellCheck={false}
              />
              {jsonError ? (
                <div className={styles.customMcpJsonError}>{jsonError}</div>
              ) : null}
            </div>
          </div>
          {saveFooter}
        </>
      ) : (
        <>
          <div className={styles.customMcpPanel}>
            <div className={styles.customMcpAddRow}>
              <button
                type="button"
                className={styles.customMcpAddBtn}
                onClick={() => handleAdd("streamable_http")}
              >
                <Globe size={18} />
                <span>
                  {t("connectors.customMcp.addHttp", "添加 HTTP Server")}
                </span>
                <Plus size={16} />
              </button>
              <button
                type="button"
                className={styles.customMcpAddBtn}
                onClick={() => handleAdd("stdio")}
              >
                <Terminal size={18} />
                <span>
                  {t("connectors.customMcp.addStdio", "添加 Stdio Server")}
                </span>
                <Plus size={16} />
              </button>
            </div>

            <div className={styles.customMcpListSection}>
              <div className={styles.customMcpListTitle}>
                {t("connectors.customMcp.listTitle", "已添加的服务器")}
                {cards.length > 0 ? (
                  <span className={styles.customMcpListCount}>
                    {cards.length}
                  </span>
                ) : null}
              </div>

              {cards.length === 0 ? (
                <div className={styles.customMcpEmpty}>
                  {t(
                    "connectors.customMcp.emptyList",
                    "尚未添加自定义 MCP，点击上方按钮开始配置",
                  )}
                </div>
              ) : (
                <div className={styles.customMcpGrid}>
                  {cards.map((card) => (
                    <CustomMcpServerCard
                      key={card.key}
                      card={card}
                      probing={probingKey === card.key}
                      authorizing={authorizingKey === card.key}
                      oauthAvailable={oauthAvailable[card.key] === true}
                      probeTools={probeResults[card.key]}
                      transportOptions={transportOptions}
                      onUpdate={updateCard}
                      onToggleEnabled={(enabled) => {
                        if (
                          oauthAvailable[card.key] &&
                          !card.oauthConfigured &&
                          enabled
                        ) {
                          message.warning(
                            t(
                              "connectors.customMcp.oauthBeforeEnable",
                              "请先完成 OAuth 授权后再启用",
                            ),
                          );
                          return;
                        }
                        void persistServerPatch(
                          card,
                          {
                            enabled,
                            ...(enabled ? {} : { default_open: false }),
                          },
                          {
                            enabled,
                            ...(enabled ? {} : { defaultOpen: false }),
                          },
                        );
                      }}
                      onDefaultOpenChange={(defaultOpen) => {
                        void persistServerPatch(
                          card,
                          { default_open: defaultOpen },
                          { defaultOpen },
                        );
                      }}
                      onSharedChange={(shared) => {
                        void persistServerPatch(card, { shared }, { shared });
                      }}
                      onRemove={() => handleRemove(card.key)}
                      onProbe={() => void handleProbe(card)}
                      onAuthorize={() => void handleOAuth(card)}
                    />
                  ))}
                </div>
              )}
            </div>
          </div>
          {saveFooter}
        </>
      )}
    </div>
  );
}
