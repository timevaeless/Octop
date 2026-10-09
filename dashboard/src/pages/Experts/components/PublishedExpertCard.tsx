import { memo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { MenuInfo } from "rc-menu/lib/interface";
import { App, Dropdown, type MenuProps } from "antd";
import { MoreHorizontal } from "lucide-react";
import { ExpertIcon } from "./iconForName";
import {
  publishedExpertsApi,
  type PublishedExpert,
} from "../../../api/modules/publishedExperts";
import { apiErrorMessage } from "../../../utils/apiError";
import styles from "../index.module.less";

interface PublishedExpertCardProps {
  expert: PublishedExpert;
  canManage: boolean;
  canUpgrade?: boolean;
  onInstall: (expert: PublishedExpert) => void;
  onChanged: () => void;
}

export const PublishedExpertCard = memo(function PublishedExpertCard({
  expert,
  canManage,
  canUpgrade = false,
  onInstall,
  onChanged,
}: PublishedExpertCardProps) {
  const { t } = useTranslation();
  const { modal, message } = App.useApp();
  const [loading, setLoading] = useState(false);
  const accent = expert.color || "var(--fn-color-brand)";

  const confirmUnpublish = () => {
    modal.confirm({
      title: t("experts.published.unpublishConfirm"),
      okText: t("experts.published.unpublish"),
      cancelText: t("common.cancel"),
      okButtonProps: { danger: true },
      onOk: async () => {
        setLoading(true);
        try {
          await publishedExpertsApi.unpublish(expert.id);
          message.success(t("experts.published.unpublishSuccess"));
          onChanged();
        } catch (err) {
          message.error(
            apiErrorMessage(err, t("experts.published.unpublishFailed"), t),
          );
        } finally {
          setLoading(false);
        }
      },
    });
  };

  const confirmUpgrade = () => {
    modal.confirm({
      title: t("experts.published.upgradeConfirm"),
      okText: t("experts.published.upgrade"),
      cancelText: t("common.cancel"),
      onOk: async () => {
        setLoading(true);
        try {
          const report = await publishedExpertsApi.upgradeInstances(expert.id);
          if (report.env_disabled) {
            message.info(t("experts.published.upgradeDisabled"));
          } else if (report.touched_instances > 0) {
            const conflicts = report.instances.reduce(
              (sum, item) => sum + item.conflicts.length,
              0,
            );
            message.success(
              conflicts > 0
                ? t("experts.published.upgradeSuccessWithConflicts", {
                    instances: report.touched_instances,
                    conflicts,
                  })
                : t("experts.published.upgradeSuccess", {
                    instances: report.touched_instances,
                  }),
            );
          } else {
            message.info(t("experts.published.upgradeNoChange"));
          }
        } catch (err) {
          message.error(
            apiErrorMessage(err, t("experts.published.upgradeFailed"), t),
          );
        } finally {
          setLoading(false);
        }
      },
    });
  };

  const menuItems: MenuProps["items"] = [
    ...(canUpgrade
      ? [
          {
            key: "upgrade",
            label: t("experts.published.upgrade"),
            onClick: ({ domEvent }: MenuInfo) => {
              domEvent.stopPropagation();
              confirmUpgrade();
            },
          },
        ]
      : []),
    {
      key: "unpublish",
      danger: true,
      label: t("experts.published.unpublish"),
      onClick: ({ domEvent }: MenuInfo) => {
        domEvent.stopPropagation();
        confirmUnpublish();
      },
    },
  ];

  return (
    <div
      className={styles.expertTemplateCard}
      onClick={() => onInstall(expert)}
      style={{ "--expert-accent": accent } as React.CSSProperties}
    >
      <div className={styles.expertTemplateHeader}>
        <div
          className={styles.agentCardIcon}
          style={{ color: accent, background: `${accent}18` }}
        >
          <ExpertIcon
            iconUrl={expert.icon_url}
            iconName={expert.icon_name}
            size={expert.icon_url ? 28 : 20}
          />
        </div>
        <div className={styles.agentCardTitleBlock}>
          <div className={styles.agentCardName}>{expert.name}</div>
          <div className={styles.expertInstalledLabel}>
            {t("experts.published.badge")}
          </div>
        </div>
        {canManage && (
          <Dropdown
            menu={{ items: menuItems }}
            trigger={["click"]}
            disabled={loading}
          >
            <button
              type="button"
              className={styles.agentCard2NameActionBtn}
              aria-label={t("common.more", "More")}
              onClick={(event) => event.stopPropagation()}
            >
              <MoreHorizontal size={12} />
            </button>
          </Dropdown>
        )}
      </div>
      <div className={styles.agentCardDesc}>
        {expert.description || "\u00a0"}
      </div>
      <div className={styles.expertCardFooter}>
        <div className={styles.expertCardHint}>
          {t("experts.published.install")}
        </div>
        {expert.creator_username && (
          <div className={styles.expertInstalledLabel}>
            {t("experts.published.by", { name: expert.creator_username })}
          </div>
        )}
      </div>
    </div>
  );
});
