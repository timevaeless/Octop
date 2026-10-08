import { Modal, Tag } from "antd";
import { useTranslation } from "react-i18next";
import type { McpPublishDiff } from "../../../api/modules/mcpCatalog";
import styles from "./McpCatalog.module.less";

export interface PublishDiffModalProps {
  open: boolean;
  serviceId: string | null;
  diff: McpPublishDiff | null;
  confirmLoading: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

type DiffTone = "added" | "updated" | "removed" | "unchanged";

function DiffGroup({
  label,
  names,
  tone,
}: {
  label: string;
  names: string[];
  tone: DiffTone;
}) {
  if (!names.length) return null;
  return (
    <div className={styles.diffGroup}>
      <div className={styles.diffHead}>
        <span className={`${styles.diffDot} ${styles[`diffDot_${tone}`]}`} />
        <span className={styles.diffLabel}>{label}</span>
        <span className={`${styles.diffCount} ${styles[`diffCount_${tone}`]}`}>
          {names.length}
        </span>
      </div>
      <div className={styles.diffChips}>
        {names.map((name) => (
          <code key={name} className={styles.diffChip}>
            {name}
          </code>
        ))}
      </div>
    </div>
  );
}

/**
 * P4.3 发布确认：把「本次会下发什么」摊开成可读的变更清单。
 * 确认前零写入——这个弹窗本身不产生任何副作用。
 */
export default function PublishDiffModal({
  open,
  serviceId,
  diff,
  confirmLoading,
  onCancel,
  onConfirm,
}: PublishDiffModalProps) {
  const { t } = useTranslation();

  const changed =
    diff !== null &&
    diff.added.length + diff.updated.length + diff.removed.length > 0;

  return (
    <Modal
      open={open}
      title={t("mcpCatalog.diffModalTitle")}
      onCancel={onCancel}
      onOk={onConfirm}
      okText={t("mcpCatalog.publish")}
      confirmLoading={confirmLoading}
      okButtonProps={{ disabled: !changed }}
      width={600}
      destroyOnClose
    >
      <div className={styles.diffBody}>
        <div className={styles.diffIntro}>
          {t("mcpCatalog.diffModalSubtitle")}
          {serviceId ? (
            <code className={styles.diffServiceId}>{serviceId}</code>
          ) : null}
        </div>

        {diff?.published === false ? (
          <Tag color="blue" className={styles.diffFirstTag}>
            {t("mcpCatalog.diffFirstPublish")}
          </Tag>
        ) : null}

        {diff && changed ? (
          <div className={styles.diffList}>
            <DiffGroup
              label={t("mcpCatalog.diffAdded")}
              names={diff.added}
              tone="added"
            />
            <DiffGroup
              label={t("mcpCatalog.diffUpdated")}
              names={diff.updated}
              tone="updated"
            />
            <DiffGroup
              label={t("mcpCatalog.diffRemoved")}
              names={diff.removed}
              tone="removed"
            />
          </div>
        ) : null}

        {diff && !changed ? (
          <div className={styles.diffEmpty}>
            <span className={styles.diffEmptyTitle}>
              {t("mcpCatalog.diffEmptyTitle")}
            </span>
            <span className={styles.diffEmptyHint}>
              {t("mcpCatalog.diffEmptyHint")}
            </span>
          </div>
        ) : null}

        {diff && diff.unchanged.length > 0 ? (
          <div className={styles.diffUnchanged}>
            <DiffGroup
              label={t("mcpCatalog.diffUnchanged")}
              names={diff.unchanged}
              tone="unchanged"
            />
          </div>
        ) : null}
      </div>
    </Modal>
  );
}
