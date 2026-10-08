"""Provider-agnostic SSO single-logout (RP-Initiated Logout, OIDC Session Management).

Design goal: **nothing here is tied to one identity provider**. The logout
endpoint is resolved per provider, in this order:

1. ``provider.extra["end_session_endpoint"]`` — explicit override for IdPs
   that do not publish discovery metadata or use a non-standard logout flow.
2. ``end_session_endpoint`` from the provider's OIDC discovery document —
   the standard mechanism (Spring Authorization Server, Keycloak, Auth0,
   Okta, ... all publish it).
3. Nothing resolvable → SLO is unsupported for this provider and logout
   degrades gracefully to local-only sign-out (previous behaviour).

``id_token_hint`` is captured at login time into ``sso_logout_hints`` and
consumed exactly once at logout. ``post_logout_redirect_uri`` is optional
per-provider config (the IdP must whitelist it, so we never send it unless
explicitly configured).
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import httpx

_ID_TOKEN_CLAIM_KEY = "_octop_raw_id_token"


def stash_raw_id_token(claims: dict[str, Any], id_token: str) -> None:
    """Attach the raw id_token to verified claims for later SLO storage."""
    if id_token:
        claims[_ID_TOKEN_CLAIM_KEY] = id_token


def pop_raw_id_token(claims: dict[str, Any]) -> str | None:
    """Extract (and remove) the raw id_token stashed by :func:`stash_raw_id_token`."""
    raw = claims.pop(_ID_TOKEN_CLAIM_KEY, None)
    return raw if isinstance(raw, str) and raw else None


def store_logout_hint(db: Any, user_id: int, provider_id: int, id_token: str) -> None:
    """Upsert the latest id_token for a user (one row per user, replace semantics)."""
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO sso_logout_hints (user_id, provider_id, id_token, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (user_id) DO UPDATE
            SET provider_id = excluded.provider_id,
                id_token = excluded.id_token,
                updated_at = excluded.updated_at
            """,
            (user_id, provider_id, id_token, int(time.time())),
        )


def consume_logout_hint(db: Any, user_id: int) -> tuple[int, str] | None:
    """Return and delete the stored (provider_id, id_token) hint for a user."""
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT provider_id, id_token FROM sso_logout_hints WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM sso_logout_hints WHERE user_id = ?", (user_id,))
    provider_id, id_token = int(row[0]), row[1]
    if not id_token:
        return None
    return provider_id, id_token


def _extra_str(provider: Any, key: str) -> str | None:
    value = (provider.extra or {}).get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def resolve_end_session_endpoint(
    sso_service: Any, provider: Any, *, httpx_client: httpx.Client | None = None
) -> str | None:
    """Resolve the provider's logout endpoint (override → discovery → None)."""
    override = _extra_str(provider, "end_session_endpoint")
    if override:
        return override
    try:
        discovery = sso_service.discovery.get(provider.issuer, httpx_client=httpx_client)
    except Exception:  # noqa: BLE001 - discovery failures must not break logout
        return None
    endpoint = discovery.get("end_session_endpoint")
    return endpoint if isinstance(endpoint, str) and endpoint else None


def build_slo_redirect_url(
    sso_service: Any,
    provider: Any,
    id_token: str,
    *,
    httpx_client: httpx.Client | None = None,
) -> str | None:
    """Build the IdP logout URL, or None when the provider cannot do SLO."""
    endpoint = resolve_end_session_endpoint(
        sso_service, provider, httpx_client=httpx_client
    )
    if not endpoint:
        return None
    params = {"id_token_hint": id_token}
    post_logout = _extra_str(provider, "post_logout_redirect_uri")
    if post_logout:
        params["post_logout_redirect_uri"] = post_logout
        state = _extra_str(provider, "post_logout_state")
        if state:
            params["state"] = state
    separator = "&" if "?" in endpoint else "?"
    return f"{endpoint}{separator}{urlencode(params)}"


def logout_response(server: Any, provider: Any, id_token: str) -> str | None:
    """Blocking helper for route handlers: resolve the SLO redirect URL or None."""
    sso_service = getattr(server, "sso_service", None)
    if sso_service is None:
        return None
    with httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
        return build_slo_redirect_url(sso_service, provider, id_token, httpx_client=client)


__all__ = [
    "build_slo_redirect_url",
    "consume_logout_hint",
    "logout_response",
    "pop_raw_id_token",
    "resolve_end_session_endpoint",
    "stash_raw_id_token",
    "store_logout_hint",
]
