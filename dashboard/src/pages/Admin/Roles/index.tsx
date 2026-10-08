import { Tabs } from "antd";
import { Boxes, Building2, ClockAlert, Pin, ShieldCheck, UserCheck, Users } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useCurrentUser } from "../../../hooks/useCurrentUser";
import TabLabel from "../../../components/TabLabel";
import PageShell from "../../../layouts/PageShell";
import MappingsTab from "./MappingsTab";
import MembershipTab from "./MembershipTab";
import MembersTab from "./MembersTab";
import OrgsTab from "./OrgsTab";
import PackagesTab from "./PackagesTab";
import PendingTab from "./PendingTab";
import ResourceAclTab from "./ResourceAclTab";
import styles from "./Roles.module.less";

/** Remembers the last visited tab so a reload lands where the user left off. */
const TAB_STORAGE_KEY = "octop:admin-roles-tab";

const TAB_KEYS = ["packages", "orgs", "members", "membership", "mappings", "acl", "pending"] as const;
type TabKey = (typeof TAB_KEYS)[number];

function readStoredTab(): TabKey {
  try {
    const stored = window.sessionStorage.getItem(TAB_STORAGE_KEY);
    if (stored && (TAB_KEYS as readonly string[]).includes(stored)) {
      return stored as TabKey;
    }
  } catch {
    /* storage disabled (private mode) — fall back to the first tab. */
  }
  return "packages";
}

export default function AdminRolesPage() {
  const { t } = useTranslation();
  const user = useCurrentUser();
  const isPlatformAdmin = user?.role === "admin";
  // 2026-09-30 three-state split:
  //   platform admin → all seven tabs (platform face);
  //   org_admin      → packages / members / mappings / acl, all scoped to
  //                    their own tenant server-side (tenant face);
  //   anyone else    → nothing (the backend would just spray 403s).
  const tenantOrgId = isPlatformAdmin ? undefined : (user?.org_id ?? undefined);
  const visibleKeys = useMemo<readonly TabKey[]>(
    () =>
      isPlatformAdmin
        ? TAB_KEYS
        : tenantOrgId
        ? (["packages", "members", "mappings", "acl"] as const)
        : [],
    [isPlatformAdmin, tenantOrgId],
  );
  const [activeKey, setActiveKey] = useState<TabKey>(readStoredTab);
  const effectiveKey = visibleKeys.includes(activeKey) ? activeKey : visibleKeys[0];

  /**
   * Cross-tab cache busting. antd Tabs keeps a pane mounted after its first
   * activation, so a package created in 「能力包」 would never show up in the
   * 「角色映射」/「成员管理」 pickers until a full page reload. Package writes
   * bump this counter; every consumer passes it down as a ``reloadKey``.
   */
  const [dataVersion, setDataVersion] = useState(0);
  const bumpData = useCallback(() => setDataVersion((v) => v + 1), []);

  const changeTab = useCallback((key: string) => {
    setActiveKey(key as TabKey);
    try {
      window.sessionStorage.setItem(TAB_STORAGE_KEY, key);
    } catch {
      /* ignore: remembering the tab is best-effort. */
    }
  }, []);

  const items = useMemo(
    () => buildRoleTabs(t, tenantOrgId, dataVersion, bumpData).filter((tab) => visibleKeys.includes(tab.key as TabKey)),
    [t, tenantOrgId, visibleKeys, dataVersion, bumpData],
  );

  return (
    /* PageShell supplies the page margin, the page title, and the content
       card — so each tab body starts already inset instead of touching the
       window edge. */
    <PageShell.FillTabs
      title={t("adminRoles.pageTitle")}
      subtitle={t("adminRoles.pageSubtitle")}
    >
      <Tabs activeKey={effectiveKey} onChange={changeTab} items={items} />
    </PageShell.FillTabs>
  );
}

/** All seven tabs, in display order. Callers filter by caller role. */
function buildRoleTabs(
  t: (key: string) => string,
  tenantOrgId?: string,
  dataVersion = 0,
  bumpData?: () => void,
) {
  return [
    {
      key: "packages",
      label: (
        <TabLabel icon={Boxes}>{t("adminRoles.packages.title")}</TabLabel>
      ),
      /* ``tabBody`` fills the pane so each panel can hand its leftover
         height to the table instead of overflowing the pane. */
      children: (
        <div className={styles.tabBody}>
          <PackagesTab tenantOrgId={tenantOrgId} onDataChanged={bumpData} />
        </div>
      ),
    },
    {
      key: "orgs",
      label: (
        <TabLabel icon={Building2}>{t("adminRoles.orgs.title")}</TabLabel>
      ),
      children: (
        <div className={styles.tabBody}>
          <OrgsTab onDataChanged={bumpData} />
        </div>
      ),
    },
    {
      /* T4-2: per-tenant member roster managed by the org_admin. */
      key: "members",
      label: <TabLabel icon={Users}>{t("adminRoles.members.title")}</TabLabel>,
      children: (
        <div className={styles.tabBody}>
          <MembersTab reloadKey={dataVersion} />
        </div>
      ),
    },
    {
      key: "membership",
      label: (
        <TabLabel icon={UserCheck}>
          {t("adminRoles.membership.title")}
        </TabLabel>
      ),
      children: (
        <div className={styles.tabBody}>
          <MembershipTab />
        </div>
      ),
    },
    {
      key: "mappings",
      label: (
        <TabLabel icon={Pin}>{t("adminRoles.mappings.title")}</TabLabel>
      ),
      children: (
        <div className={styles.tabBody}>
          <MappingsTab tenantOrgId={tenantOrgId} reloadKey={dataVersion} />
        </div>
      ),
    },
    {
      key: "acl",
      label: (
        <TabLabel icon={ShieldCheck}>
          {t("adminRoles.acl.title")}
        </TabLabel>
      ),
      children: (
        <div className={styles.tabBody}>
          <ResourceAclTab tenantOrgId={tenantOrgId} reloadKey={dataVersion} />
        </div>
      ),
    },
    {
      /* T5-4: SSO users the resolution chain could not place. */
      key: "pending",
      label: (
        <TabLabel icon={ClockAlert}>
          {t("adminRoles.pending.title")}
        </TabLabel>
      ),
      children: (
        <div className={styles.tabBody}>
          <PendingTab />
        </div>
      ),
    },
  ];
}
