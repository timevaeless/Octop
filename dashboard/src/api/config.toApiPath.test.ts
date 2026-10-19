import { describe, expect, it } from "vitest";
import { toApiPath } from "./config";

/**
 * Note: vitest defines BASE_URL="" (no deploy prefix), so the
 * ``/octop/api/...`` double-prefix case is exercised only in real
 * path-prefix builds (covered by stripApiPrefix + the /api strip here).
 */
describe("toApiPath", () => {
  it("passes already-bare agent paths through unchanged", () => {
    expect(toApiPath("/agents/a1/workspace/download?path=out%2Fx.xlsx")).toBe(
      "/agents/a1/workspace/download?path=out%2Fx.xlsx",
    );
  });

  it("strips the legacy bare /api prefix", () => {
    expect(toApiPath("/api/agents/a1/avatar")).toBe("/agents/a1/avatar");
    expect(toApiPath("/api/workspace/media?path=%2Fx")).toBe(
      "/workspace/media?path=%2Fx",
    );
  });

  it("preserves query strings and strips only the prefix", () => {
    expect(toApiPath("/api/agents/a1/media/preview?source=x/y.png")).toBe(
      "/agents/a1/media/preview?source=x/y.png",
    );
  });

  it("normalizes absolute http URLs to a bare API path with query", () => {
    expect(
      toApiPath("https://host.example/api/agents/a1/workspace/download?path=p"),
    ).toBe("/agents/a1/workspace/download?path=p");
  });

  it("does not strip /api inside path segments", () => {
    expect(toApiPath("/agents/api-keys")).toBe("/agents/api-keys");
  });

  it("handles root-relative edge inputs without throwing", () => {
    expect(toApiPath("/api")).toBe("");
    expect(toApiPath("/api?x=1")).toBe("?x=1");
  });
});
