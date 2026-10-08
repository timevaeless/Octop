import { Modal, Tag, Tooltip } from "antd";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type {
  McpCatalogService,
  McpPublishLogEntry,
} from "../../../api/modules/mcpCatalog";
import { mcpCatalogApi } from "../../../api/modules/mcpCatalog";
import { ResizableTable } from "../../../components/ResizableTable";
import { message } from "../../../utils/antdMessage";
import { apiErrorMessage } from "../../../utils/apiError";

export interface PublishLogModalProps {
  open: boolean;
  service: McpCatalogService | null;
  onClose: () => void;
}

function formatTime(value: string): string {
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}

export default function PublishLogModal({
  open,
  service,
  onClose,
}: PublishLogModalProps) {
  const { t } = useTranslation();
  const [log, setLog] = useState<McpPublishLogEntry[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open || !service) return;
    setLoading(true);
    mcpCatalogApi
      .publishLog(service.service_id)
      .then((data) => setLog(data.log ?? []))
      .catch((err) => {
        message.error(apiErrorMessage(err, t("mcpCatalog.loadFailed"), t));
      })
      .finally(() => setLoading(false));
  }, [open, service, t]);

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width={760}
      title={t("mcpCatalog.publishLog")}
      destroyOnClose
    >
      <ResizableTable
        storageKey="admin-mcp-catalog-log"
        rowKey={(row) =>
          `${row.action}-${row.created_at}-${row.result ?? ""}`
        }
        size="small"
        loading={loading}
        dataSource={log}
        pagination={false}
        scroll={{ x: 680 }}
        columns={[
          {
            title: t("mcpCatalog.logTime"),
            dataIndex: "created_at",
            width: 170,
            render: (v: string) => formatTime(v),
          },
          {
            title: t("mcpCatalog.logAction"),
            dataIndex: "action",
            width: 110,
          },
          {
            title: t("mcpCatalog.logResult"),
            dataIndex: "result",
            width: 90,
            render: (v: string) =>
              v === "ok" ? (
                <Tag color="success">{t("mcpCatalog.resultOk")}</Tag>
              ) : (
                <Tag color="error">{t("mcpCatalog.resultError")}</Tag>
              ),
          },
          {
            title: t("mcpCatalog.logActor"),
            dataIndex: "actor",
            width: 130,
            ellipsis: true,
            render: (v: string | null) => v || "—",
          },
          {
            title: t("mcpCatalog.logDetail"),
            dataIndex: "detail",
            ellipsis: true,
            render: (v: string | null) =>
              v ? (
                <Tooltip title={v} placement="topLeft">
                  <span>{v}</span>
                </Tooltip>
              ) : (
                "—"
              ),
          },
        ]}
      />
    </Modal>
  );
}
