/**
 * Shared building blocks for the 「Roles & Access」 admin page.
 *
 * Kept deliberately small and dependency-free (antd + existing app utils) so
 * every tab in this folder renders loading / error / empty states identically.
 */
import { Alert, Button, Skeleton, Space, Tag, Tooltip } from "antd";
import type { TFunction } from "i18next";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { OrgInfo, RolePackage } from "../../../api/modules/roles";
import { rolesApi } from "../../../api/modules/roles";
import { EmptyState } from "../../../components/EmptyState";
import { useIsMobile } from "../../../hooks/useIsMobile";
import { apiErrorMessage } from "../../../utils/apiError";

/* ------------------------------------------------------------------ *
 * Ability-package labels and tags
 * ------------------------------------------------------------------ */

/** Palette for role tags — neutral antd preset colours, theme aware. */
const ROLE_TAG_COLORS = [
  "geekblue",
  "purple",
  "cyan",
  "magenta",
  "gold",
  "volcano",
  "green",
  "blue",
];

/** Stable colour per ``role_key`` so a package keeps the same tag colour. */
export function roleTagColor(roleKey: string): string {
  let hash = 0;
  for (let i = 0; i < roleKey.length; i += 1) {
    hash = (hash * 31 + roleKey.charCodeAt(i)) % 1000003;
  }
  return ROLE_TAG_COLORS[hash % ROLE_TAG_COLORS.length];
}

/** ``name_zh (role_key)`` — used in pickers where the key must stay visible. */
export function roleOptionLabel(role: RolePackage): string {
  return `${role.name_zh} (${role.role_key})`;
}

/**
 * Platform-owned system packages (seeded ``member`` / ``org_admin``).
 *
 * The tenant face hides them from *management surfaces* (package table, role
 * pickers): they are the platform's designation and JIT machinery, not
 * something a tenant admin configures. They stay in the **label pool** —
 * existing memberships and mappings reference their keys, and those rows must
 * render Chinese names, not raw keys.
 */
export function isPlatformSystemPackage(role: {
  is_system?: boolean;
  org_id?: string | null;
}): boolean {
  return role.is_system === true && (role.org_id ?? null) === null;
}

/**
 * ``role_key → short display label``.
 *
 * The backend only ships ``name_zh``; under a non-Chinese locale we fall back
 * to the canonical ASCII ``role_key`` so tags stay readable.
 */
export function useRoleLabels(roles: RolePackage[]): Map<string, string> {
  const { i18n } = useTranslation();
  const zh = i18n.language?.startsWith("zh") ?? false;
  return useMemo(() => {
    const map = new Map<string, string>();
    for (const role of roles) {
      map.set(role.role_key, zh ? role.name_zh : role.role_key);
    }
    return map;
  }, [roles, zh]);
}

interface RoleTagListProps {
  roleKeys: string[];
  labels: Map<string, string>;
  /** Copy used for the empty selection; defaults to「不限角色」. */
  emptyText?: string;
}

/**
 * Renders ``allowed_roles`` / ``role_keys`` as tags.
 *
 * An empty list is an explicit grey tag ("unrestricted") rather than blank
 * space, because "no restriction" and "not loaded" must not look alike.
 */
export function RoleTagList({ roleKeys, labels, emptyText }: RoleTagListProps) {
  const { t } = useTranslation();
  if (!roleKeys || roleKeys.length === 0) {
    return <Tag>{emptyText ?? t("adminRoles.acl.unrestricted")}</Tag>;
  }
  return (
    <Space size={4} wrap>
      {roleKeys.map((key) => (
        <Tooltip key={key} title={key}>
          <Tag color={roleTagColor(key)} style={{ marginInlineEnd: 0 }}>
            {labels.get(key) ?? key}
          </Tag>
        </Tooltip>
      ))}
    </Space>
  );
}

/* ------------------------------------------------------------------ *
 * Async data with loading / error / retry
 * ------------------------------------------------------------------ */

export interface AsyncData<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
  setData: (updater: (prev: T | null) => T | null) => void;
}

interface AsyncOptions {
  /** Message shown when the error carries no server detail. */
  fallbackError: string;
  t: TFunction;
  /** Changing this value triggers an automatic refetch (e.g. a tab's filter). */
  reloadKey?: unknown;
}

/**
 * Fetch-on-mount helper that surfaces ``loading`` / ``error`` separately so
 * callers can render a skeleton or a retry affordance instead of a toast.
 *
 * ``loader`` is read through a ref, so callers may pass an inline closure
 * without re-triggering the effect on every render.
 */
export function useAsyncData<T>(
  loader: () => Promise<T>,
  options: AsyncOptions,
): AsyncData<T> {
  const { fallbackError, t, reloadKey } = options;
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loaderRef = useRef(loader);
  loaderRef.current = loader;
  const tRef = useRef(t);
  tRef.current = t;
  /** Monotonic request id — a late response must never clobber a newer one. */
  const seqRef = useRef(0);

  const reload = useCallback(() => {
    const seq = (seqRef.current += 1);
    const isCurrent = () => seq === seqRef.current;
    setLoading(true);
    setError(null);
    void loaderRef
      .current()
      .then((next) => {
        if (isCurrent()) setData(next);
      })
      .catch((err: unknown) => {
        if (isCurrent()) {
          setError(apiErrorMessage(err, fallbackError, tRef.current));
        }
      })
      .finally(() => {
        if (isCurrent()) setLoading(false);
      });
  }, [fallbackError]);

  const update = useCallback((updater: (prev: T | null) => T | null) => {
    setData((prev) => updater(prev));
  }, []);

  useEffect(() => {
    reload();
  }, [reload, reloadKey]);

  return { data, loading, error, reload, setData: update };
}

/* ------------------------------------------------------------------ *
 * Reference data (tenants + ability packages)
 * ------------------------------------------------------------------ */

export interface OrgRoleRefs {
  orgs: OrgInfo[];
  roles: RolePackage[];
}

/**
 * Tenants + ability packages, loaded once per tab.
 *
 * Every tab that renders a picker needs both; centralizing the fetch keeps the
 * loading/error handling identical across tabs. ``enabled=false`` skips the
 * fetch entirely (both endpoints are require_admin) — the org_admin face
 * feeds its pickers from the per-tenant ``/org/{id}/refs`` endpoint instead.
 *
 * ``reloadKey`` lets a sibling tab's write (e.g. creating a package) bust this
 * cache without a page reload — pass a monotonically bumped number.
 */
export function useOrgRoleRefs(
  enabled = true,
  reloadKey?: number | string,
): AsyncData<OrgRoleRefs> {
  const { t } = useTranslation();
  return useAsyncData<OrgRoleRefs>(
    async () => {
      if (!enabled) return { orgs: [], roles: [] };
      const [orgs, roles] = await Promise.all([
        rolesApi.listOrgs(),
        rolesApi.list(),
      ]);
      return { orgs, roles };
    },
    // String key: stable identity per (enabled, reloadKey) pair — an array or
    // object literal would change identity every render and refetch forever.
    {
      fallbackError: t("adminRoles.loadFailed"),
      t,
      reloadKey: `${enabled}:${reloadKey ?? 0}`,
    },
  );
}

/* ------------------------------------------------------------------ *
 * Loading skeleton / error panel / empty state
 * ------------------------------------------------------------------ */

/** Row-shaped placeholder shown on a list's very first load. */
export function ListSkeleton({ rows = 5 }: { rows?: number }) {
  return (
    <div style={{ padding: "4px 0" }}>
      <Skeleton.Input active size="small" style={{ width: 260 }} />
      <div style={{ marginTop: 16 }}>
        <Skeleton active title={false} paragraph={{ rows, width: "100%" }} />
      </div>
    </div>
  );
}

interface LoadErrorProps {
  message: string;
  onRetry: () => void;
}

/** Inline error panel with a retry entry point (replaces toast-only failures). */
export function LoadError({ message, onRetry }: LoadErrorProps) {
  const { t } = useTranslation();
  return (
    <Alert
      type="error"
      showIcon
      message={t("adminRoles.loadFailed")}
      description={message}
      action={
        <Button size="small" danger icon={null} onClick={onRetry}>
          {t("adminRoles.retry")}
        </Button>
      }
    />
  );
}

interface TableEmptyProps {
  /** True when a search/filter is active but matched nothing. */
  filtered?: boolean;
  onClear?: () => void;
  /** Override the default「暂无数据」title (per-tab guidance). */
  title?: string;
  /** Override the default description. */
  description?: string;
}

/** Table ``locale.emptyText`` — distinguishes "no data" from "no match". */
export function TableEmpty({
  filtered = false,
  onClear,
  title,
  description,
}: TableEmptyProps) {
  const { t } = useTranslation();
  if (title || description) {
    return (
      <EmptyState
        title={title}
        description={description}
        actionLabel={onClear ? t("adminRoles.clearFilters") : undefined}
        onAction={onClear}
      />
    );
  }
  if (filtered) {
    return (
      <EmptyState
        title={t("adminRoles.noMatch")}
        description={t("adminRoles.noMatchHint")}
        actionLabel={onClear ? t("adminRoles.clearFilters") : undefined}
        onAction={onClear}
      />
    );
  }
  return (
    <EmptyState
      title={t("adminRoles.noData")}
      description={t("adminRoles.noDataHint")}
    />
  );
}

/* ------------------------------------------------------------------ *
 * Table area: fill the tab pane, never grow a second scrollbar
 * ------------------------------------------------------------------ */

/**
 * Vertical space a table reserves for its own chrome: the fixed header row
 * plus the pagination strip underneath the rows.
 */
const TABLE_CHROME = 80;

export interface TableArea {
  /** Attach to the flex wrapper that hosts the ``<Table>``. */
  areaRef: (node: HTMLDivElement | null) => void;
  /** Value for antd's ``scroll.y``; ``undefined`` until it can be measured. */
  scrollY: number | undefined;
}

/**
 * Measures the height left for a table and returns it as antd's ``scroll.y``.
 *
 * Why: the tab pane in ``PageShell.FillTabs`` scrolls when its content is
 * taller than the pane. A 10-row list plus its toolbar is exactly that tall on
 * a laptop, so the pane grew a page-level scrollbar that dragged the stats and
 * the toolbar along with the rows. Capping the row area instead keeps the
 * panel inside the pane, so the only thing that can ever scroll is the rows —
 * and only when they genuinely do not fit.
 *
 * Why a *callback* ref rather than a ``useRef`` object: every tab renders the
 * table area only after its first load (``firstLoad ? <ListSkeleton/> : …``),
 * so on mount there is no node to measure. An effect keyed on ``[isMobile]``
 * would run once against ``null`` and never again, leaving ``scroll.y``
 * ``undefined`` forever — and because ``.tableArea`` is ``overflow: hidden``
 * the overflow then got *clipped* instead of scrolled (rows and the pagination
 * silently cut off). A callback ref attaches the observer the moment the node
 * actually appears.
 *
 * Mobile (and jsdom) return ``undefined``: the pane there is auto-height, so
 * clamping the body would invent a scrollbar instead of removing one.
 */
export function useTableArea(): TableArea {
  const [scrollY, setScrollY] = useState<number | undefined>(undefined);
  const isMobile = useIsMobile();
  const observerRef = useRef<ResizeObserver | null>(null);

  const areaRef = useCallback(
    (node: HTMLDivElement | null) => {
      observerRef.current?.disconnect();
      observerRef.current = null;
      if (!node || isMobile) {
        setScrollY(undefined);
        return;
      }
      const measure = () => {
        const available = Math.floor(node.clientHeight);
        // 0 means "no layout yet" (first paint, jsdom) — leave the table natural.
        if (available <= 0) return;
        const next = Math.max(120, available - TABLE_CHROME);
        setScrollY((prev) =>
          prev !== undefined && Math.abs(prev - next) <= 2 ? prev : next,
        );
      };
      measure();
      const observer = new ResizeObserver(measure);
      observer.observe(node);
      observerRef.current = observer;
    },
    [isMobile],
  );

  // The node itself does not re-mount when the breakpoint flips, so drop the
  // measurement explicitly: the pane is auto-height on mobile.
  useEffect(() => {
    if (isMobile) setScrollY(undefined);
  }, [isMobile]);

  return { areaRef, scrollY };
}

/* ------------------------------------------------------------------ *
 * Table pagination
 * ------------------------------------------------------------------ */

/** Shared pagination config: size changer + "showing N of M" summary. */
export function useTablePagination(pageSize = 10) {
  const { t } = useTranslation();
  return useMemo(
    () => ({
      pageSize,
      showSizeChanger: true,
      pageSizeOptions: ["10", "20", "50"],
      showTotal: (total: number) => t("adminRoles.totalRows", { count: total }),
    }),
    [pageSize, t],
  );
}
