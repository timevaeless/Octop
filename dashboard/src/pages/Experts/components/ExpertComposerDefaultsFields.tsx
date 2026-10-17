import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Alert, Form, Select, Tag } from "antd";
import type { DefaultOptionType } from "antd/es/select";

import { connectorsApi } from "../../../api/modules/connectors";
import { knowledgeBasesApi } from "../../../api/modules/knowledgeBases";
import { useCurrentUser } from "../../../hooks/useCurrentUser";
import { applyTemplateInstallGuard } from "./expertComposerDefaults";

interface ExpertComposerDefaultsFieldsProps {
  /**
   * Expert being edited. Omit when creating: the new expert's owner is the
   * caller, so the endpoint falls back to the caller's own scope.
   */
  agentId?: string | null;
}

/** Selected entries first (stable), so the dropdown opens on what's attached. */
function sortSelectedFirst<T extends { value: string }>(
  options: T[],
  selected: string[],
): T[] {
  return [
    ...options.filter((o) => selected.includes(o.value)),
    ...options.filter((o) => !selected.includes(o.value)),
  ];
}

export default function ExpertComposerDefaultsFields({
  agentId,
}: ExpertComposerDefaultsFieldsProps) {
  const { t } = useTranslation();
  const currentUserId = useCurrentUser()?.id ?? null;
  const form = Form.useFormInstance();
  // Live selection so the dropdown can mark/sort "attached" entries — this
  // disambiguates "options to pick from" vs "already attached to the expert".
  const selectedConnectors =
    (Form.useWatch("mcp_servers", form) as string[] | undefined) ?? [];
  const selectedKnowledgeBaseIds =
    (Form.useWatch("knowledge_base_ids", form) as string[] | undefined) ?? [];
  const [knowledgeBases, setKnowledgeBases] = useState<
    { id: string; name: string }[]
  >([]);
  const [knowledgeLoading, setKnowledgeLoading] = useState(false);
  const [connectors, setConnectors] = useState<
    { value: string; label: string }[]
  >([]);
  const [connectorsLoading, setConnectorsLoading] = useState(false);
  // Template-install guard (2026-10-01): a published template can carry
  // connector/KB references the installer has no role for — the backend
  // hard-rejects such installs (validate_mcp_servers_for_user), and the raw
  // server name used to leak into the Select as an unreadable code value.
  // In create mode we drop the unresolvable selections and say so; the
  // disabled ghost options below are the universal safety net so a raw id
  // can never render, in any mode.
  const [skipped, setSkipped] = useState<{
    connectors: number;
    knowledgeBases: number;
  } | null>(null);
  const isCreate = agentId === undefined;

  useEffect(() => {
    let cancelled = false;
    setKnowledgeLoading(true);
    // Scoped to the expert's owner, not the viewer: the save validates against
    // the owner, so offering anything wider would let the user pick an option
    // that then fails with "未找到知识库或文档。".
    knowledgeBasesApi
      .listAssignable(agentId ?? undefined)
      .then((bases) => {
        if (!cancelled) {
          setKnowledgeBases(
            bases.map((base) => ({ id: base.id, name: base.name })),
          );
        }
      })
      .catch(() => {
        if (!cancelled) setKnowledgeBases([]);
      })
      .finally(() => {
        if (!cancelled) setKnowledgeLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [agentId]);

  useEffect(() => {
    let cancelled = false;
    setConnectorsLoading(true);
    // Scoped to the expert's owner, not the viewer — same reason as the
    // knowledge-base picker above. The endpoint already returns only connectors
    // that pass the save-time check, so no extra filtering happens here.
    connectorsApi
      .listAssignable(agentId ?? undefined)
      .then((instances) => {
        if (cancelled) return;
        setConnectors(
          (instances ?? [])
            .filter((item) => item.status === "active" && item.has_credentials)
            .map((item) => ({
            value: item.mcp_server_name,
            label:
              currentUserId !== null && item.owner_user_id !== currentUserId
                ? `${item.display_name} · ${
                    item.owner_display_name ||
                    item.owner_username ||
                    item.owner_user_id
                  }`
                : item.display_name || item.mcp_server_name,
          })),
        );
      })
      .catch(() => {
        if (!cancelled) setConnectors([]);
      })
      .finally(() => {
        if (!cancelled) setConnectorsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [agentId, currentUserId]);

  // Drop template-prefilled references the installer cannot use — the save
  // would fail otherwise. Create mode only: editing an existing expert must
  // never silently alter its configuration. The guard itself (pure logic)
  // lives in expertComposerDefaults.ts; agentId may be absent in create mode,
  // where the assignable endpoints fall back to the caller's own scope.
  useEffect(() => {
    if (!isCreate || connectorsLoading || knowledgeLoading) return;
    const result = applyTemplateInstallGuard({
      selectedConnectors,
      assignableConnectorValues: connectors.map((option) => option.value),
      selectedKnowledgeBaseIds,
      assignableKnowledgeBaseIds: knowledgeBases.map((base) => base.id),
    });
    if (!result) return;
    form.setFieldsValue({
      mcp_servers: result.nextConnectors,
      knowledge_base_ids: result.nextKnowledgeBaseIds,
    });
    setSkipped(result.skipped);
  }, [
    isCreate,
    connectorsLoading,
    knowledgeLoading,
    connectors,
    knowledgeBases,
    selectedConnectors,
    selectedKnowledgeBaseIds,
    form,
  ]);

  const connectorOptions = useMemo(() => {
    const known = new Set(connectors.map((option) => option.value));
    const ghosts = selectedConnectors.filter((value) => !known.has(value));
    return [
      ...connectors,
      ...ghosts.map((value) => ({
        value,
        label: t("experts.unavailableConnectorLabel"),
        disabled: true,
      })),
    ];
  }, [connectors, selectedConnectors, t]);

  const knowledgeBaseOptions = useMemo(() => {
    const known = new Set(knowledgeBases.map((base) => base.id));
    const ghosts = selectedKnowledgeBaseIds.filter((id) => !known.has(id));
    return [
      ...knowledgeBases.map((base) => ({
        value: base.id,
        label: base.name,
      })),
      ...ghosts.map((id) => ({
        value: id,
        label: t("experts.unavailableKnowledgeBaseLabel"),
        disabled: true,
      })),
    ];
  }, [knowledgeBases, selectedKnowledgeBaseIds, t]);

  const renderOption = (selected: string[]) =>
    function OptionRow(option: DefaultOptionType) {
      const attached = selected.includes(String(option.value));
      return (
        <span
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 8,
          }}
        >
          <span style={{ minWidth: 0, overflow: "hidden" }}>
            {option.label}
          </span>
          {attached && (
            <Tag color="blue" style={{ marginInlineEnd: 0 }}>
              {t("experts.attachedBadge")}
            </Tag>
          )}
        </span>
      );
    };

  return (
    <>
      {skipped && (skipped.connectors > 0 || skipped.knowledgeBases > 0) && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message={t("experts.templateResourcesSkippedTitle")}
          description={t("experts.templateResourcesSkippedBody", {
            count: skipped.connectors + skipped.knowledgeBases,
          })}
        />
      )}
      <Form.Item
        name="knowledge_base_ids"
        label={t("experts.knowledgeBasesLabel")}
        extra={t("experts.knowledgeBasesHint")}
      >
        <Select
          mode="multiple"
          allowClear
          showSearch
          optionFilterProp="label"
          loading={knowledgeLoading}
          options={sortSelectedFirst(
            knowledgeBaseOptions,
            selectedKnowledgeBaseIds,
          )}
          optionRender={renderOption(selectedKnowledgeBaseIds)}
          placeholder={t("experts.knowledgeBasesPlaceholder")}
        />
      </Form.Item>
      <Form.Item
        name="mcp_servers"
        label={t("experts.connectorsLabel")}
        extra={t("experts.connectorsHint")}
      >
        <Select
          mode="multiple"
          allowClear
          showSearch
          optionFilterProp="label"
          loading={connectorsLoading}
          options={sortSelectedFirst(connectorOptions, selectedConnectors)}
          optionRender={renderOption(selectedConnectors)}
          placeholder={t("experts.connectorsPlaceholder")}
        />
      </Form.Item>
    </>
  );
}
