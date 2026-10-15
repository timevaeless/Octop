import { Button, Tooltip } from "antd";
import { CircleCheck, Copy } from "lucide-react";
import { useCallback } from "react";
import { useTranslation } from "react-i18next";
import type { McpPublishResult } from "../../../api/modules/mcpCatalog";
import { Modal } from "antd";
import { message } from "../../../utils/antdMessage";
import { copyText } from "../../../utils/copyText";
import styles from "./McpCatalog.module.less";

export interface PublishResultModalProps {
  open: boolean;
  result: McpPublishResult | null;
  onClose: () => void;
}

/**
 * 发布回执：把 ACL 投影、网关路由、插件实例三块结果读成「一眼能核对的凭据」，
 * 而不是一坨裸文本。footer 只有一个「完成」——这是终点，没有二次动作。
 * v3（Console 通道）：路由=RouteSpec 摘要（不再是 ingress YAML），插件=路由级实例。
 */
export default function PublishResultModal({
  open,
  result,
  onClose,
}: PublishResultModalProps) {
  const { t } = useTranslation();

  const copy = useCallback(
    async (value: string) => {
      const ok = await copyText(value);
      if (ok) message.success(t("common.copied"));
      else message.error(t("common.copyFailed"));
    },
    [t],
  );

  const aclEntries = Object.entries(result?.acl ?? {});
  const plugins = Object.entries(result?.plugins ?? {});
  const route = result?.route;
  const routeSummary = route
    ? [
        `route: ${route.name}`,
        `path: ${route.path}`,
        `service: ${route.service.name}:${route.service.port}`,
        `action: ${route.action ?? "-"}`,
      ].join("\n")
    : "";

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={
        <Button type="primary" onClick={onClose}>
          {t("common.confirm")}
        </Button>
      }
      width={760}
      title={t("mcpCatalog.resultModalTitle")}
      destroyOnClose
    >
      <div className={styles.resultBody}>
        <div className={styles.resultBanner}>
          <CircleCheck size={16} className={styles.resultBannerIcon} />
          <span>{t("mcpCatalog.resultBanner")}</span>
        </div>

        <section className={styles.resultSection}>
          <div className={styles.resultSectionHead}>
            <span className={styles.resultSectionTitle}>
              {t("mcpCatalog.aclTitle")}
            </span>
            {aclEntries.length > 0 ? (
              <span className={styles.resultCount}>
                {t("mcpCatalog.resultRoleCount", {
                  count: aclEntries.length,
                })}
              </span>
            ) : null}
          </div>
          {aclEntries.length > 0 ? (
            <div className={styles.aclTable}>
              {aclEntries.map(([role, tools]) => (
                <div key={role} className={styles.aclRow}>
                  <span className={styles.aclRole}>{role}</span>
                  <div className={styles.aclTools}>
                    {tools.map((tool) => (
                      <code key={tool} className={styles.diffChip}>
                        {tool}
                      </code>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className={styles.resultEmpty}>{t("mcpCatalog.aclEmpty")}</div>
          )}
        </section>

        <section className={styles.resultSection}>
          <div className={styles.resultSectionHead}>
            <span className={styles.resultSectionTitle}>
              {t("mcpCatalog.routeTitle")}
            </span>
            {routeSummary ? (
              <Tooltip title={t("mcpCatalog.copyRoute")}>
                <button
                  type="button"
                  className={styles.copyBtn}
                  onClick={() => void copy(routeSummary)}
                >
                  <Copy size={12} />
                </button>
              </Tooltip>
            ) : null}
          </div>
          {routeSummary ? (
            <pre className={styles.yamlBlock}>{routeSummary}</pre>
          ) : (
            <div className={styles.resultEmpty}>
              {t("mcpCatalog.routeEmpty")}
            </div>
          )}
        </section>

        {plugins.length > 0 ? (
          <section className={styles.resultSection}>
            <div className={styles.resultSectionHead}>
              <span className={styles.resultSectionTitle}>
                {t("mcpCatalog.pluginsTitle")}
              </span>
              <span className={styles.resultCount}>
                {t("mcpCatalog.resultPluginCount", {
                  count: plugins.length,
                })}
              </span>
            </div>
            <div className={styles.diffChips}>
              {plugins.map(([plugin, action]) => (
                <code key={plugin} className={styles.diffChip}>
                  {plugin} → {action}
                </code>
              ))}
            </div>
          </section>
        ) : null}
      </div>
    </Modal>
  );
}
