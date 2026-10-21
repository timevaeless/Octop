import { useCallback, useState } from "react";
import { Checkbox, Modal, Spin, Upload } from "antd";
import type { UploadFile } from "antd";
import { message } from "@/utils/antdMessage";
import { FileArchive, Upload as UploadIcon } from "lucide-react";
import { useTranslation } from "react-i18next";
import { expertMarketApi } from "../../../api/modules/expertMarket";
import styles from "../index.module.less";

interface UploadExpertPackageButtonProps {
  isAdmin: boolean;
  /** Called after a package is installed into the library. */
  onSuccess: () => void;
}

/**
 * Admin-only entry that installs an expert package zip into the built-in
 * expert library (POST /experts/library/upload).
 */
export default function UploadExpertPackageButton({
  isAdmin,
  onSuccess,
}: UploadExpertPackageButtonProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<UploadFile | null>(null);
  const [force, setForce] = useState(false);
  const [uploading, setUploading] = useState(false);

  const submit = useCallback(async () => {
    if (!file || !file.originFileObj) return;
    setUploading(true);
    try {
      const result = await expertMarketApi.uploadLibraryExpert(
        file.originFileObj,
        force,
      );
      message.success(
        t("experts.uploadSuccess", {
          id: result.id,
          count: result.files,
        }),
      );
      (result.warnings ?? []).forEach((warn) => message.warning(warn));
      setOpen(false);
      setFile(null);
      setForce(false);
      onSuccess();
    } catch (err) {
      message.error(
        err instanceof Error
          ? err.message
          : t("experts.uploadFailed", "上传失败"),
      );
    } finally {
      setUploading(false);
    }
  }, [file, force, onSuccess, t]);

  if (!isAdmin) return null;

  return (
    <>
      <button
        className={styles.toolbarIconBtn}
        onClick={() => setOpen(true)}
        type="button"
        title={t("experts.uploadExpertPackage", "上传专家包")}
      >
        <UploadIcon size={14} />
      </button>
      <Modal
        open={open}
        title={t("experts.uploadExpertPackage", "上传专家包")}
        okText={t("experts.uploadInstall", "安装模板")}
        okButtonProps={{
          disabled: !file?.originFileObj,
          loading: uploading,
        }}
        onOk={() => void submit()}
        onCancel={() => {
          if (uploading) return;
          setOpen(false);
          setFile(null);
          setForce(false);
        }}
        destroyOnHidden
      >
        <Upload.Dragger
          accept=".zip"
          maxCount={1}
          fileList={file ? [file] : []}
          beforeUpload={(_file, fileList) => {
            const first = fileList[0];
            if (first) {
              setFile({
                uid: first.uid,
                name: first.name,
                size: first.size,
                type: first.type,
                originFileObj: first,
              });
            }
            return false;
          }}
          onRemove={() => setFile(null)}
          disabled={uploading}
        >
          <p className="ant-upload-drag-icon">
            <FileArchive size={36} />
          </p>
          <p className="ant-upload-text">
            {t("experts.uploadExpertHint", "点击或拖入专家包 zip")}
          </p>
          <p className="ant-upload-hint">
            {t(
              "experts.uploadExpertHintDetail",
              "zip 根需包含 manifest.json（+ SOUL.md / skills/），安装到内置专家库",
            )}
          </p>
        </Upload.Dragger>
        <Checkbox
          checked={force}
          onChange={(e) => setForce(e.target.checked)}
          style={{ marginTop: 12 }}
        >
          {t("experts.uploadForce", "覆盖同名模板")}
        </Checkbox>
        {uploading && (
          <div style={{ marginTop: 12 }}>
            <Spin size="small" /> {t("experts.uploadInstalling", "安装中…")}
          </div>
        )}
      </Modal>
    </>
  );
}
