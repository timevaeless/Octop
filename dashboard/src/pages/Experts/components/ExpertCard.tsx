// dashboard/src/pages/Experts/components/ExpertCard.tsx
import { memo } from "react";
import { Tooltip } from "antd";
import { useTranslation } from "react-i18next";
import { CheckCircle, Settings2 } from "lucide-react";
import { pickLocale } from "../../../utils/localizedText";
import { ExpertIcon, resolveExpertAvatarUrl } from "./iconForName";
import type { QuickPrompt } from "./welcomeManifest";
import styles from "../index.module.less";

export interface ExpertSummary {
  id: string;
  label: { zh?: string; en?: string };
  description: { zh?: string; en?: string };
  welcome_message?: { zh?: string; en?: string };
  quick_prompts?: QuickPrompt[];
  icon_name?: string | null;
  icon_url?: string | null;
  color?: string | null;
  files?: string[];
  task_examples?: { zh?: string[]; en?: string[] } | null;
}

interface ExpertCardProps {
  expert: ExpertSummary;
  lang: "zh" | "en";
  isInstalled: boolean;
  onCreate: (expert: ExpertSummary) => void;
  /** Platform-admin only: open the default-connectors editor. */
  canConfigureDefaults?: boolean;
  onConfigureDefaults?: (expert: ExpertSummary) => void;
}

export const ExpertCard = memo(function ExpertCard({
  expert,
  lang,
  isInstalled,
  onCreate,
  canConfigureDefaults = false,
  onConfigureDefaults,
}: ExpertCardProps) {
  const { t } = useTranslation();
  const label = pickLocale(expert.label, lang) || expert.id;
  const desc = pickLocale(expert.description, lang);
  const accent = expert.color || "var(--fn-color-brand)";
  const portraitUrl = resolveExpertAvatarUrl(expert.icon_url);
  const hasPortrait = Boolean(portraitUrl);

  return (
    <div
      className={styles.expertTemplateCard}
      onClick={() => onCreate(expert)}
      style={
        {
          "--expert-accent": accent,
        } as React.CSSProperties
      }
    >
      {/* Icon + title */}
      <div className={styles.expertTemplateHeader}>
        <div
          className={`${styles.agentCardIcon} ${
            hasPortrait ? styles.agentCardPortrait : ""
          }`}
          style={
            hasPortrait
              ? undefined
              : {
                  color: accent,
                  background: `${accent}18`,
                }
          }
        >
          <ExpertIcon
            iconUrl={portraitUrl}
            iconName={expert.icon_name}
            size={hasPortrait ? 44 : 30}
          />
        </div>
        <div className={styles.agentCardTitleBlock}>
          <div className={styles.agentCardName}>{label}</div>
        </div>
      </div>

      {/* Description */}
      <div className={styles.agentCardDesc}>{desc || "\u00a0"}</div>

      {/* Footer */}
      <div className={styles.expertCardFooter}>
        <div className={styles.expertCardHint}>
          {canConfigureDefaults && onConfigureDefaults && (
            <Tooltip title={t("experts.defaults.entry", "配置默认连接器")}>
              <button
                type="button"
                aria-label={t("experts.defaults.entry", "配置默认连接器")}
                onClick={(e) => {
                  e.stopPropagation();
                  onConfigureDefaults(expert);
                }}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  justifyContent: "center",
                  width: 22,
                  height: 22,
                  marginRight: 6,
                  padding: 0,
                  border: "none",
                  background: "transparent",
                  borderRadius: 6,
                  cursor: "pointer",
                  color: "var(--fn-text-tertiary)",
                }}
              >
                <Settings2 size={14} />
              </button>
            </Tooltip>
          )}
          {t("experts.createFromTemplate")}
        </div>
        {isInstalled && (
          <div className={styles.expertInstalledLabel}>
            <CheckCircle size={12} />
            {t("experts.installedBadge")}
          </div>
        )}
      </div>
    </div>
  );
});
