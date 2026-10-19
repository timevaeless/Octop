declare const BASE_URL: string;

/**
 * Externally mounted API prefix, e.g. "/octop/api" when deployed behind
 * `location /octop/` (BASE_URL=/octop at build time), otherwise "/api".
 * URL constructors should use this; matchers should accept both this and the
 * legacy bare "/api" form (stored/historical URLs may lack the prefix).
 */
export const API_PREFIX = `${BASE_URL || ""}/api`;

/** The deploy path prefix itself (e.g. "/octop"), "" for same-origin. */
export const DEPLOY_PREFIX = BASE_URL || "";

/** True when *url* points at the octop API (with or without deploy prefix). */
export function isApiUrl(url: string): boolean {
  return url.startsWith(API_PREFIX) || url.startsWith("/api/");
}

/**
 * Strip the deploy prefix (BASE_URL, e.g. "/octop") from an API URL so
 * pattern matching sees the canonical "/api/..." form. Legacy unprefixed
 * URLs pass through unchanged.
 */
export function stripApiPrefix(url: string): string {
  return DEPLOY_PREFIX && url.startsWith(DEPLOY_PREFIX)
    ? url.slice(DEPLOY_PREFIX.length)
    : url;
}

/**
 * Normalize an Octop API URL down to the bare path form the request helpers
 * (``request``/``requestBlob``/``getApiUrl``) expect: no deploy prefix and no
 * ``/api`` prefix. Handles URLs that already carry the deploy prefix
 * (``/octop/api/agents/…``), the legacy bare ``/api/agents/…`` form, and
 * already-bare ``/agents/…`` paths — all become ``/agents/…``. Query and
 * hash are preserved. Without this, re-prefixed input would double the
 * prefix (``/octop/api/octop/api/…``) and 404 behind path deployments.
 */
export function toApiPath(url: string): string {
  const strip = (s: string): string =>
    stripApiPrefix(s).replace(/^\/api(?=\/|\?|#|$)/, "");
  if (/^https?:\/\//i.test(url)) {
    try {
      const parsed = new URL(url);
      return `${strip(parsed.pathname)}${parsed.search}${parsed.hash}`;
    } catch {
      return url;
    }
  }
  return strip(url);
}

/**
 * Get the full API URL with /api prefix
 * @param path - API path (e.g., "/models", "/skills")
 * @returns Full API URL (e.g., "http://localhost:8088/api/models" or "/api/models")
 */
export function getApiUrl(path: string): string {
  const base = BASE_URL || "";
  const apiPrefix = "/api";
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `${base}${apiPrefix}${normalizedPath}`;
}

/**
 * Get a WebSocket URL derived from the same origin as the API.
 * Converts http:// → ws:// and https:// → wss://.
 * If BASE_URL is empty (same-origin), uses the current page location.
 * @param path - API path (e.g., "/browser-stream/ws")
 * @returns Full WebSocket URL
 */
export function getWsUrl(path: string): string {
  const base = BASE_URL || "";
  const apiPrefix = "/api";
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const httpUrl = `${base}${apiPrefix}${normalizedPath}`;

  if (httpUrl.startsWith("http://")) {
    return httpUrl.replace("http://", "ws://");
  }
  if (httpUrl.startsWith("https://")) {
    return httpUrl.replace("https://", "wss://");
  }

  // Relative URL — derive from current page location
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}${httpUrl}`;
}
