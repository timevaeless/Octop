import { Image } from "antd";
import { X, FileText } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useAuthImageSrc } from "../../../hooks/useAuthImageSrc";
import type { ChatAttachment } from "../hooks/useChat";
import { inferKindFromNameAndMime } from "../utils/chatAttachments";
import { ChatMediaPlayer } from "./ChatMediaPlayer";
import styles from "../index.module.less";

interface ChatInputPreviewBarProps {
  attachments: ChatAttachment[];
  uploading: boolean;
  onRemoveAttachment: (index: number) => void;
}

function ComposerImagePreview({
  url,
  alt,
  className,
}: {
  url: string;
  alt: string;
  className?: string;
}) {
  const { t } = useTranslation();
  const { src, loadState } = useAuthImageSrc(url, alt);

  if (loadState === "loading") {
    return (
      <div
        aria-hidden
        className={className}
        style={{ background: "var(--fn-bg-secondary)" }}
      />
    );
  }

  if (loadState === "error" || !src) {
    return (
      <div
        className={className}
        role="img"
        aria-label={alt || t("chat.imageLoadFailed")}
        style={{
          background: "#fff1f0",
          color: "#cf1322",
          fontSize: 10,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          textAlign: "center",
          padding: 4,
        }}
      >
        {t("chat.imageLoadFailed")}
      </div>
    );
  }

  return (
    <Image
      src={src}
      alt={alt}
      width={64}
      height={64}
      style={{ objectFit: "cover", display: "block" }}
      preview={{ mask: false }}
    />
  );
}

export default function ChatInputPreviewBar({
  attachments,
  uploading,
  onRemoveAttachment,
}: ChatInputPreviewBarProps) {
  const hasContent = attachments.length > 0 || uploading;

  if (!hasContent) return null;

  const imageAttachments = attachments.filter(
    (attachment) =>
      inferKindFromNameAndMime(
        attachment.mediaType,
        attachment.filename,
        attachment.kind,
      ) === "image",
  );

  return (
    <div className={styles.imagePreviewBar}>
      {imageAttachments.length > 0 ? (
        <Image.PreviewGroup>
          {attachments.map((attachment, idx) =>
            inferKindFromNameAndMime(
              attachment.mediaType,
              attachment.filename,
              attachment.kind,
            ) === "image" ? (
              <div
                key={`${attachment.url}-${idx}`}
                className={styles.imagePreviewItem}
              >
                <ComposerImagePreview
                  url={attachment.url}
                  alt={attachment.filename || "preview"}
                  className={styles.imagePreviewThumb}
                />
                <button
                  className={styles.imagePreviewRemove}
                  onClick={() => onRemoveAttachment(idx)}
                  type="button"
                >
                  <X size={12} />
                </button>
              </div>
            ) : null,
          )}
        </Image.PreviewGroup>
      ) : null}
      {attachments.map((attachment, idx) => {
        const kind = inferKindFromNameAndMime(
          attachment.mediaType,
          attachment.filename,
          attachment.kind,
        );
        if (kind === "image") return null;
        if (kind === "video" || kind === "audio") {
          return (
            <div
              key={`${attachment.url}-${idx}`}
              className={
                kind === "video"
                  ? styles.composerMediaItem
                  : styles.attachmentPreviewCard
              }
            >
              <ChatMediaPlayer
                url={attachment.url}
                filename={attachment.filename}
                workspacePath={attachment.workspacePath}
                mediaType={attachment.mediaType}
                kind={kind}
                compact
              />
              <button
                className={styles.imagePreviewRemove}
                onClick={() => onRemoveAttachment(idx)}
                type="button"
              >
                <X size={12} />
              </button>
            </div>
          );
        }
        return (
          <div
            key={`${attachment.url}-${idx}`}
            className={styles.attachmentPreviewCard}
          >
            <div className={styles.attachmentPreviewMeta}>
              <FileText size={14} className={styles.attachmentPreviewIcon} />
              <span className={styles.attachmentPreviewName}>
                {attachment.filename || attachment.url}
              </span>
            </div>
            <button
              className={styles.imagePreviewRemove}
              onClick={() => onRemoveAttachment(idx)}
              type="button"
            >
              <X size={12} />
            </button>
          </div>
        );
      })}
      {uploading && (
        <div className={styles.imagePreviewItem}>
          <div className={styles.imagePreviewLoading}>
            <div className={styles.uploadSpinner} />
          </div>
        </div>
      )}
    </div>
  );
}
