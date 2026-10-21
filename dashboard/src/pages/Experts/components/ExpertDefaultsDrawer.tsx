import { useCallback, useEffect, useRef, useState } from "react";
import { Drawer, Select, Spin } from "antd";
import { message } from "@/utils/antdMessage";
import { useTranslation } from "react-i18next";
import {
  expertMarketApi,
  type LibraryExpertDefaultsOption,
} from "../../../api/modules/expertMarket";

interface ExpertDefaultsDrawerProps {
  expertId: string | null;
  expertLabel: string;
  onClose: () => void;
}

/**
 * Admin-only editor for a builtin expert's default MCP connector refs
 * (GET/PUT /experts/library/{id}/defaults). Options come pre-normalized to
 * semantic refs by the backend; picking persists into the package manifest.
 */
export default function ExpertDefaultsDrawer({
  expertId,
  expertLabel,
  onClose,
}: ExpertDefaultsDrawerProps) {
  const { t } = useTranslation();
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [values, setValues] = useState<string[]>([]);
  const [options, setOptions] = useState<LibraryExpertDefaultsOption[]>([]);
  // The caller passes an inline arrow; keep the load effect keyed on
  // expertId only so unrelated parent re-renders (agent polling, tab
  // state) never re-trigger the fetch while the drawer is open.
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!expertId) return;
    let cancelled = false;
    setLoading(true);
    setValues([]);
    setOptions([]);
    expertMarketApi
      .getLibraryExpertDefaults(expertId)
      .then((resp) => {
        if (cancelled) return;
        setValues(resp.default_mcp_servers ?? []);
        setOptions(resp.options ?? []);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        message.error(
          err instanceof Error
            ? err.message
            : t("experts.defaults.loadFailed", "加载默认连接器失败"),
        );
        onCloseRef.current();
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [expertId, t]);

  const save = useCallback(async () => {
    if (!expertId) return;
    setSaving(true);
    try {
      const resp = await expertMarketApi.putLibraryExpertDefaults(
        expertId,
        values,
      );
      setValues(resp.default_mcp_servers ?? []);
      setOptions(resp.options ?? []);
      message.success(t("experts.defaults.saveSuccess", "已保存默认连接器"));
      onClose();
    } catch (err) {
      message.error(
        err instanceof Error
          ? err.message
          : t("experts.defaults.saveFailed", "保存默认连接器失败"),
      );
    } finally {
      setSaving(false);
    }
  }, [expertId, onClose, t, values]);

  return (
    <Drawer
      title={t("experts.defaults.title", { label: expertLabel })}
      open={Boolean(expertId)}
      onClose={onClose}
      width={480}
      destroyOnHidden
      extra={
        <button
          type="button"
          disabled={loading || saving}
          onClick={() => void save()}
          style={{
            border: "none",
            borderRadius: 6,
            padding: "5px 14px",
            cursor: loading || saving ? "not-allowed" : "pointer",
            background: "var(--fn-color-brand, #2563eb)",
            color: "#fff",
            fontSize: 13,
          }}
        >
          {saving ? t("experts.defaults.saving", "保存中…") : t("experts.defaults.save", "保存")}
        </button>
      }
    >
      {loading ? (
        <Spin />
      ) : (
        <>
          <p style={{ fontSize: 13, color: "var(--fn-text-secondary)", marginTop: 0 }}>
            {t("experts.defaults.hint")}
          </p>
          <Select
            mode="multiple"
            allowClear
            style={{ width: "100%" }}
            placeholder={t("experts.defaults.placeholder")}
            value={values}
            onChange={(next) => setValues(next)}
            options={options.map((opt) => ({ value: opt.value, label: opt.label }))}
          />
          <p
            style={{
              fontSize: 12,
              color: "var(--fn-text-tertiary)",
              marginTop: 12,
            }}
          >
            {t("experts.defaults.footnote")}
          </p>
        </>
      )}
    </Drawer>
  );
}
