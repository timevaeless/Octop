/**
 * Service Worker registration and lifecycle management.
 *
 * Deployment model: the frontend bundle ships with the server-side release
 * (users reach Octop over the web), so updates apply without any user
 * confirmation step.
 *
 * Production (all browsers including Chrome / Safari):
 *   - Register `/sw.js` with workbox `skipWaiting: true` + `clientsClaim: true`
 *     (vite.config): an updated worker activates as soon as it finishes
 *     installing.
 *   - If the page was already controlled at load time, a controllerchange
 *     means a NEW worker took over — reload exactly once so the session
 *     switches to the fresh bundle. The "page was controlled" guard is what
 *     prevents the full-page refresh loops WebKit used to exhibit: first-time
 *     control (fresh visit / shift-reload / first install) never reloads, and
 *     the flag disables itself after one reload.
 *
 * Dev:
 *   - Do not register. Clear any leftover production SW on the same origin
 *     so Vite HMR and an old worker do not fight over navigations.
 */

async function unregisterAllServiceWorkers(): Promise<void> {
  const regs = await navigator.serviceWorker.getRegistrations();
  await Promise.all(regs.map((r) => r.unregister()));
  if ("caches" in window) {
    const keys = await caches.keys();
    await Promise.all(keys.map((k) => caches.delete(k)));
  }
}

/** Production registration. Exported for unit tests (vitest always sets DEV). */
export async function registerProductionSW(): Promise<void> {
  const registration = await navigator.serviceWorker.register("/sw.js", {
    scope: "/",
    updateViaCache: "none",
  });

  // Reload once when an updated worker takes control of an already-controlled
  // page. With skipWaiting the worker activates itself, so controllerchange is
  // the single reliable signal that the new bundle is live.
  const wasControlled = Boolean(navigator.serviceWorker.controller);
  let updateReloaded = false;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (!wasControlled || updateReloaded) return;
    updateReloaded = true;
    window.location.reload();
  });

  setInterval(
    () => {
      void registration.update();
    },
    60 * 60 * 1000,
  );

  // Mobile Chrome often keeps a waiting worker forever unless something
  // polls; check again when the tab becomes visible.
  const onVisible = () => {
    if (document.visibilityState === "visible") {
      void registration.update();
    }
  };
  document.addEventListener("visibilitychange", onVisible);
  window.addEventListener("pageshow", onVisible);
}

export async function registerSW(): Promise<void> {
  if (!("serviceWorker" in navigator)) return;

  if (import.meta.env.DEV) {
    try {
      await unregisterAllServiceWorkers();
    } catch (err) {
      console.warn("[SW] Dev cleanup failed:", err);
    }
    return;
  }

  try {
    await registerProductionSW();
  } catch (err) {
    console.error("[SW] Registration failed:", err);
  }
}
