import { afterEach, describe, expect, it } from "vitest";
import {
  LAYOUT_MODE_KEY,
  loadLayoutMode,
  saveLayoutMode,
} from "../../src/layouts/layoutModeStorage";

describe("layoutModeStorage", () => {
  afterEach(() => {
    localStorage.removeItem(LAYOUT_MODE_KEY);
  });

  it("defaults to minimal layout", () => {
    expect(loadLayoutMode()).toBe("minimal");
  });

  it("persists layout mode", () => {
    saveLayoutMode("minimal");
    expect(loadLayoutMode()).toBe("minimal");
  });

  it("ignores invalid stored values", () => {
    localStorage.setItem(LAYOUT_MODE_KEY, "weird");
    expect(loadLayoutMode()).toBe("minimal");
  });
});
