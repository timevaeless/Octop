import { describe, expect, it } from "vitest";
import {
  NAV_MAX_WIDTH,
  NAV_MIN_WIDTH,
  SIDEBAR_GROUPED_NAV_KEYS,
  SIDEBAR_NAV_KEYS,
  buildNavSections,
  clampNavWidth,
  isGroupedNavKey,
} from "./sidebarNav";
import type { OctopUser } from "../api/modules/auth";

const adminUser = {
  id: 1,
  username: "admin",
  role: "admin",
  permissions: ["*"],
} as OctopUser;

describe("sidebarNav", () => {
  it("marks catalog keys as grouped", () => {
    for (const key of SIDEBAR_GROUPED_NAV_KEYS) {
      expect(isGroupedNavKey(key)).toBe(true);
    }
    expect(isGroupedNavKey("chat")).toBe(false);
    expect(isGroupedNavKey("experts")).toBe(false);
  });

  it("places grouped keys only under sections with groupKey", () => {
    const sections = buildNavSections(adminUser, { mobileEnabled: true });
    const flatKeys = new Set(
      sections
        .filter((s) => !s.groupKey)
        .flatMap((s) => s.items.map((i) => i.key)),
    );
    const groupedKeys = new Set(
      sections
        .filter((s) => s.groupKey)
        .flatMap((s) => s.items.map((i) => i.key)),
    );
    for (const key of groupedKeys) {
      expect(isGroupedNavKey(key)).toBe(true);
      expect(flatKeys.has(key)).toBe(false);
    }
    for (const key of flatKeys) {
      expect(isGroupedNavKey(key)).toBe(false);
    }
  });

  it("covers every catalog key from the admin nav", () => {
    const keys = buildNavSections(adminUser, { mobileEnabled: true }).flatMap(
      (section) => section.items.map((item) => item.key),
    );
    expect(new Set(keys)).toEqual(new Set(SIDEBAR_NAV_KEYS));
    const grouped = buildNavSections(adminUser, { mobileEnabled: true }).filter(
      (section) => section.id,
    );
    expect(grouped.map((section) => section.id)).toEqual([
      "settings",
      "control",
      "admin",
    ]);
  });
});

describe("clampNavWidth", () => {
  it("holds a dragged width inside the rail bounds", () => {
    expect(clampNavWidth(120, 1440)).toBe(NAV_MIN_WIDTH);
    expect(clampNavWidth(9999, 1440)).toBe(NAV_MAX_WIDTH);
    expect(clampNavWidth(300, 1440)).toBe(300);
  });

  it("caps the rail at half a narrow desktop viewport", () => {
    // 768px is the mobile breakpoint, so this is the narrowest desktop window.
    expect(clampNavWidth(NAV_MAX_WIDTH, 768)).toBe(384);
  });

  it("lets the floor yield when the viewport cannot hold it", () => {
    // Unreachable for the desktop rail, but the clamp must stay total instead
    // of returning a width below its own floor.
    expect(clampNavWidth(NAV_MAX_WIDTH, 320)).toBe(160);
  });

  it("rounds sub-pixel drag positions", () => {
    expect(clampNavWidth(247.6, 1440)).toBe(248);
  });
});
