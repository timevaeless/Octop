import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

function mockRegistration(opts: {
  controller?: { scriptURL: string } | null;
}) {
  const swListeners = new Map<string, Array<() => void>>();
  const registration = {
    waiting: null,
    installing: null,
    addEventListener: vi.fn(),
    update: vi.fn(),
  };
  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: {
      register: vi.fn().mockResolvedValue(registration),
      getRegistrations: vi.fn().mockResolvedValue([]),
      controller: opts.controller ?? null,
      addEventListener: (
        type: string,
        fn: () => void,
        _options?: { once?: boolean },
      ) => {
        const list = swListeners.get(type) ?? [];
        list.push(fn);
        swListeners.set(type, list);
      },
      removeEventListener: (type: string, fn: () => void) => {
        const list = swListeners.get(type) ?? [];
        swListeners.set(
          type,
          list.filter((listener) => listener !== fn),
        );
      },
      emit: (type: string) => {
        for (const fn of [...(swListeners.get(type) ?? [])]) fn();
      },
    },
  });
  return registration;
}

describe("registerProductionSW", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.useFakeTimers();
    vi.stubGlobal("location", {
      ...window.location,
      reload: vi.fn(),
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("registers with updateViaCache none", async () => {
    mockRegistration({ controller: { scriptURL: "https://x/sw.js" } });

    const { registerProductionSW } = await import("./sw-register");
    await registerProductionSW();

    expect(navigator.serviceWorker.register).toHaveBeenCalledWith("/sw.js", {
      scope: "/",
      updateViaCache: "none",
    });
  });

  it("reloads once when an updated worker takes control of a controlled page", async () => {
    mockRegistration({ controller: { scriptURL: "https://x/sw.js" } });

    const { registerProductionSW } = await import("./sw-register");
    await registerProductionSW();

    // New worker activates (skipWaiting) and claims the page.
    (
      navigator.serviceWorker as unknown as { emit: (type: string) => void }
    ).emit("controllerchange");
    expect(window.location.reload).toHaveBeenCalledOnce();

    // A stray second event must not reload again.
    (
      navigator.serviceWorker as unknown as { emit: (type: string) => void }
    ).emit("controllerchange");
    expect(window.location.reload).toHaveBeenCalledOnce();
  });

  it("does not reload on first-time control (fresh visit / shift-reload)", async () => {
    mockRegistration({ controller: null });

    const { registerProductionSW } = await import("./sw-register");
    await registerProductionSW();

    // clientsClaim fires controllerchange on the very first takeover.
    (
      navigator.serviceWorker as unknown as { emit: (type: string) => void }
    ).emit("controllerchange");
    expect(window.location.reload).not.toHaveBeenCalled();
  });

  it("checks for updates hourly and when the tab becomes visible", async () => {
    const registration = mockRegistration({
      controller: { scriptURL: "https://x/sw.js" },
    });

    const { registerProductionSW } = await import("./sw-register");
    await registerProductionSW();

    await vi.advanceTimersByTimeAsync(60 * 60 * 1000);
    expect(registration.update).toHaveBeenCalledTimes(1);

    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "visible",
    });
    document.dispatchEvent(new Event("visibilitychange"));
    expect(registration.update).toHaveBeenCalledTimes(2);
  });
});
