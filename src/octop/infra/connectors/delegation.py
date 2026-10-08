"""Delegated-identity tokens for outbound MCP calls (T3 identity passthrough).

Design: ``docs/mcp-identity-passthrough-design.md`` (方案 C, 三跳版).

Octop mints a short-lived, audience-limited JWT ("拜访证") when building
per-user MCP configs for a custom server that opted in with
``delegate_identity: true``. The token carries ``sub`` (= uid), ``oid``
(tenant), ``rls`` (role keys) and is verified *only* by mcp-hub with a
dedicated signing secret (secrets table key ``mcp_delegation``) — separate
from the main ``jwt`` secret on purpose: leaking it only allows minting
tool-call tokens, never admin access.

This module is deliberately self-contained so it can be maintained and
reviewed independently of upstream connector logic (same isolation strategy
as ``octop.infra.users.tenancy``).
"""

from __future__ import annotations

import logging
import os
import secrets as _secrets
import time
from typing import Any

import jwt

DELEGATION_SECRET_KEY = "mcp_delegation"
DELEGATION_AUDIENCE = "mcp-hub"
DELEGATION_ISSUER = "octop"

logger = logging.getLogger(__name__)

# Defaults. The effective values are resolved at call time so deployments can
# shorten the TTL via ``OCTOP_MCP_DELEGATION_TTL_SECONDS`` (e.g. 10s in a
# demo environment) without a code change.
DELEGATION_TTL_SECONDS = 1800  # 30 min
DELEGATION_REFRESH_MARGIN_SECONDS = 300  # refresh window for the default TTL

# Per-server opt-in flag stored in the custom-MCP server spec. Listed in
# ``_META_KEYS`` so it never leaks into harness payloads or API responses.
DELEGATE_IDENTITY_FLAG = "delegate_identity"

_TTL_ENV = "OCTOP_MCP_DELEGATION_TTL_SECONDS"
_SECRET_ENV = "OCTOP_MCP_DELEGATION_SECRET"
_MIN_TTL_SECONDS = 10


def effective_ttl_seconds() -> int:
    """Effective token TTL: env override or the 30-min default (floor 10s)."""
    raw = os.environ.get(_TTL_ENV, "").strip()
    if not raw:
        return DELEGATION_TTL_SECONDS
    try:
        return max(_MIN_TTL_SECONDS, int(raw))
    except ValueError:
        return DELEGATION_TTL_SECONDS


def refresh_margin_seconds(ttl: int | None = None) -> int:
    """Refresh window: how early before ``exp`` a reload is triggered.

    Scaled with the TTL (a third of it, capped at the 5-minute default) so a
    demo-grade 10s TTL yields a 3s window instead of "always expiring".
    """
    effective = ttl if ttl is not None else effective_ttl_seconds()
    return max(1, min(DELEGATION_REFRESH_MARGIN_SECONDS, effective // 3))


def get_or_create_delegation_secret(secret_repo: Any) -> bytes:
    """Dedicated HS256 signing key: env override, else generated once & persisted.

    Deployment override (2026-10-01): when ``OCTOP_MCP_DELEGATION_SECRET`` is
    set (non-blank), its *raw string bytes* are the signing key and the
    secrets table is never touched. This is how the key is kept in sync with
    mcp-hub's ``MCP_DELEGATION_SECRET`` — hub and Octop must share the exact
    same byte string, and a per-install random DB value would break that the
    moment either side is reset (hub's is compose-injected, Octop's was
    auto-generated). Unset/blank → previous behaviour (auto-generate into the
    secrets table), so community installs keep working unchanged.

    Note the env value is used verbatim (no hex decode) — hub signs with
    ``secret.getBytes()`` on the same literal, and the DB fallback also
    stores the hex string as ASCII bytes.
    """
    raw = os.environ.get(_SECRET_ENV, "").strip()
    if raw:
        return raw.encode()
    return bytes(secret_repo.get_or_create(DELEGATION_SECRET_KEY, lambda: _secrets.token_hex(32).encode()))


def mint_delegated_token(
    secret: bytes,
    *,
    uid: int,
    org_id: str | None,
    role_keys: list[str] | tuple[str, ...] | None,
    session_id: str | None = None,
    ttl_seconds: int | None = None,
    audience: str = DELEGATION_AUDIENCE,
    sso_subject: str | None = None,
) -> str:
    """Sign a delegated identity token for one outbound MCP connection.

    Claims follow the design doc §4: ``sub`` = uid (hub reads it back as
    ``uid``), ``oid`` = tenant, ``rls`` = role keys, optional ``sid`` =
    chat/session scope. No ``role`` claim: hub-side authorization is decided
    from ``rls`` explicitly — the global admin bypass does *not* carry over
    (拍板：admin 不继承绕过).

    ``ssu`` (2026-10-06): the user's SSO subject at the identity provider
    (e.g. the portal's ``system_users.id`` for OIDC logins). Business MCP
    services downstream of the gateway use it to resolve the *real business
    operator* for audit attribution — the octop ``uid`` lives in a different
    id space and is meaningless to them. Absent for local-password users
    (those flows are out of scope for such services by design).
    """
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": str(uid),
        "iat": now,
        "exp": now + int(ttl_seconds if ttl_seconds is not None else effective_ttl_seconds()),
        "aud": audience,
        # review 3.11 (2026-09-25): hub can requireIssuer("octop") — an extra
        # "who signed this" layer on top of the dedicated signing secret
        "iss": DELEGATION_ISSUER,
    }
    if org_id is not None:
        payload["oid"] = org_id
    if role_keys:
        payload["rls"] = list(role_keys)
    if session_id:
        payload["sid"] = session_id
    if sso_subject:
        payload["ssu"] = str(sso_subject)
    return jwt.encode(payload, bytes(secret), algorithm="HS256")


def mint_identity_token(secret_repo: Any, user_id: int, authz: Any) -> str | None:
    """Mint the delegated-identity JWT for harness-spec building (T3).

    Identity = the user whose config namespace is being built (``user_id``
    + ``authz``). ``authz=None`` (internal/test callers) mints nothing —
    a hub-connected server then fails closed at the hub's 401 instead of
    silently calling with a forged/absent identity. Minting is a local
    HMAC (microseconds); the token only leaves the process when a spec
    actually opted in via ``delegate_identity: true``.

    Pluggable hook target: ``ConnectorService._delegated_identity_token_for``
    delegates here; deleting it together with that hook restores community
    behaviour verbatim (no delegated identity on outbound MCP specs).
    """
    if authz is None:
        return None
    try:
        secret = get_or_create_delegation_secret(secret_repo)
        return mint_delegated_token(
            secret,
            uid=user_id,
            org_id=getattr(authz, "org_id", None),
            role_keys=list(getattr(authz, "role_keys", None) or ()),
            sso_subject=getattr(authz, "sso_subject", None),
        )
    except Exception:
        logger.exception("delegated identity token mint failed for user %s", user_id)
        return None


def spec_wants_delegated_identity(spec: dict[str, Any]) -> bool:
    return isinstance(spec, dict) and spec.get(DELEGATE_IDENTITY_FLAG) is True


def inject_delegated_auth_header(spec: dict[str, Any], token: str) -> dict[str, Any]:
    """Return a copy of a streamable-HTTP harness spec with the token attached.

    The flag is an explicit opt-in, so the injected token *replaces* any
    statically configured ``Authorization`` header for that server (a static
    header plus this flag is a configuration contradiction; documented in the
    design doc §6).
    """
    out = dict(spec)
    headers = {str(k): str(v) for k, v in dict(out.get("headers") or {}).items()}
    headers["Authorization"] = f"Bearer {token}"
    out["headers"] = headers
    return out


def spec_token_exp(spec: dict[str, Any]) -> float | None:
    """Expiry (epoch seconds) of the delegated token embedded in a spec, if any.

    Signature is deliberately NOT verified: this is a renewal-scheduling
    hint for a token minted in-process, and mcp-hub still verifies the
    signature (and expiry) on every request regardless. Specs without a
    delegated bearer token — static auth, no auth, or a non-JWT header —
    return ``None`` and are never refreshed.

    P1-6 (review 2026-09-26): the issuer/audience claims must match this
    module's minted shape. OAuth access tokens are usually JWTs too, and an
    IdP-signed static token would otherwise be booked for "renewal" it can
    never get — a pointless reload every turn once its (IdP) ``exp`` enters
    the margin, forever.
    """
    if not isinstance(spec, dict):
        return None
    headers = spec.get("headers")
    if not isinstance(headers, dict):
        return None
    auth = headers.get("Authorization")
    if not isinstance(auth, str) or not auth.startswith("Bearer "):
        return None
    try:
        payload = jwt.decode(auth[len("Bearer ") :], options={"verify_signature": False})
    except jwt.PyJWTError:
        return None
    if not isinstance(payload, dict) or payload.get("iss") != DELEGATION_ISSUER:
        return None
    aud = payload.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if DELEGATION_AUDIENCE not in auds:
        return None
    exp = payload.get("exp")
    return float(exp) if isinstance(exp, (int, float)) else None
