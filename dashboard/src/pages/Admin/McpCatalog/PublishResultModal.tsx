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
 * 发布回执：把 ACL 投影、ingress YAML、下发资源三块结果读成「一眼能核对的凭据」，
 * 而不是一坨裸文本。footer 只有一个「完成」——这是终点，没有二次动作。
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
  const resources = result?.resources ?? [];
  const ingress = result?.ingress ?? "";

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
              {t("mcpCatalog.ingressTitle")}
            </span>
            {ingress ? (
              <Tooltip title={t("mcpCatalog.copyYaml")}>
                <button
                  type="button"
                  className={styles.copyBtn}
                  onClick={() => void copy(ingress)}
                >
                  <Copy size={12} />
                </button>
              </Tooltip>
            ) : null}
          </div>
          {ingress ? (
            <pre className={styles.yamlBlock}>{ingress}</pre>
          ) : (
            <div className={styles.resultEmpty}>
              {t("mcpCatalog.noIngress")}
            </div>
          )}
        </section>

        {resources.length > 0 ? (
          <section className={styles.resultSection}>
            <div className={styles.resultSectionHead}>
              <span className={styles.resultSectionTitle}>
                {t("mcpCatalog.resourcesTitle")}
              </span>
              <span className={styles.resultCount}>
                {t("mcpCatalog.resultResourceCount", {
                  count: resources.length,
                })}
              </span>
            </div>
            <div className={styles.diffChips}>
              {resources.map((r) => (
                <code key={r} className={styles.diffChip}>
                  {r}
                </code>
              ))}
            </div>
          </section>
        ) : null}
      </div>
    </Modal>
  );
}
