"""Resolve the externally visible HTTP origin for an incoming request."""

import os

from starlette.requests import Request


def _first_header_value(value: str | None) -> str | None:
    if value is None:
        return None
    first = value.split(",", 1)[0].strip()
    return first or None


def _base_path() -> str:
    """Externally mounted path prefix (e.g. ``/octop`` behind ``location /octop/``).

    Set ``OCTOP_PUBLIC_BASE_PATH`` when the dashboard/API is reverse-proxied
    under a path prefix: every externally constructed URL (SSO redirect_uri,
    OAuth callbacks, error-page redirects) must carry the same prefix, while
    the proxy strips it before traffic reaches Octop. Value must start with
    ``/`` and not end with ``/``; invalid values are ignored.
    """
    raw = (os.environ.get("OCTOP_PUBLIC_BASE_PATH") or "").strip().rstrip("/")
    if not raw or not raw.startswith("/"):
        return ""
    return raw


def base_path() -> str:
    """Public accessor for the deploy path prefix (empty when same-origin).

    Consumers that emit browser-facing artifacts tied to URL paths — notably
    cookies scoped via ``Path`` — must include the prefix, or the browser
    never sends them back on prefixed requests (state-cookie loss on SSO
    callbacks).
    """
    return _base_path()


def resolve_public_base(request: Request) -> str:
    """Resolve the externally visible origin for an incoming request.

    Trusts ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` when present (typical
    behind a reverse proxy). Prefer deploying Octop behind a trusted proxy and
    keeping the dashboard same-origin with the API for OIDC cookies.

    When ``OCTOP_PUBLIC_BASE_PATH`` is set (path-prefix deployment), the prefix
    is appended so externally visible URLs include it.
    """
    prefix = _base_path()
    forwarded_proto = _first_header_value(request.headers.get("x-forwarded-proto"))
    forwarded_host = _first_header_value(request.headers.get("x-forwarded-host"))
    if forwarded_proto:
        host = forwarded_host or request.headers.get("host")
        if host:
            return f"{forwarded_proto}://{host}{prefix}".rstrip("/")

    return f"{str(request.url.replace(path='', query='', fragment='')).rstrip('/')}{prefix}".rstrip("/")
