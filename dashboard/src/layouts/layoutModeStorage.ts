/** App chrome layout: classic (dual rail) vs minimal (single nav with settings/records). */
export type LayoutMode = "classic" | "minimal";

/** Minimal-mode lower pane: merged settings nav vs agent/session records. */
export type MinimalNavPane = "settings" | "records";

export const LAYOUT_MODE_KEY = "octop:layout-mode";

export function loadLayoutMode(): LayoutMode {
  try {
    const raw = localStorage.getItem(LAYOUT_MODE_KEY);
    if (raw === "minimal" || raw === "classic") return raw;
  } catch {
    /* ignore */
  }
  return "minimal";
}

export function saveLayoutMode(mode: LayoutMode): void {
  try {
    localStorage.setItem(LAYOUT_MODE_KEY, mode);
  } catch {
    /* ignore */
  }
}
