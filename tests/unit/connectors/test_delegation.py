"""Unit tests for delegated-identity token minting and harness injection.

Covers the Octop side of ``docs/mcp-identity-passthrough-design.md`` (方案 C):
flag normalization, token claims, header injection, and the service-level
wiring in ``custom_harness_configs``.
"""

from __future__ import annotations

import time
from pathlib import Path

import jwt as pyjwt
import pytest

from octop.config import OctopConfig
from octop.infra.connectors.custom_mcp import (
    enabled_harness_configs,
    harness_spec_for_server,
    normalize_server_spec,
)
from octop.infra.connectors.delegation import (
    DELEGATE_IDENTITY_FLAG,
    DELEGATION_AUDIENCE,
    DELEGATION_REFRESH_MARGIN_SECONDS,
    DELEGATION_SECRET_KEY,
    get_or_create_delegation_secret,
    inject_delegated_auth_header,
    mint_delegated_token,
    spec_token_exp,
    spec_wants_delegated_identity,
)
from octop.infra.connectors.service import ConnectorService
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.connectors import ConnectorRepo
from octop.infra.db.repos.secrets import SecretRepo
from octop.infra.db.repos.settings import SettingsRepo
from octop.infra.users.context import AuthzContext

ORG = "cheryfs"


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


@pytest.fixture
def svc(db: SqlitePool) -> ConnectorService:
    return ConnectorService(
        repo=ConnectorRepo(db),
        secret_repo=SecretRepo(db),
        settings_repo=SettingsRepo(db),
        config=OctopConfig(),
    )


def _authz(uid: int = 42) -> AuthzContext:
    return AuthzContext(user_id=uid, org_id=ORG, role_keys=("expert_user",))


# ---------------------------------------------------------------- minting


def test_mint_token_claims():
    secret = b"k" * 32
    token = mint_delegated_token(
        secret, uid=42, org_id=ORG, role_keys=["expert_user"], session_id="chat-1"
    )
    claims = pyjwt.decode(token, secret, algorithms=["HS256"], audience=DELEGATION_AUDIENCE)
    assert claims["sub"] == "42"
    assert claims["oid"] == ORG
    assert claims["rls"] == ["expert_user"]
    assert claims["sid"] == "chat-1"
    assert claims["exp"] - claims["iat"] == 1800
    # No role claim: hub decides from rls; admin bypass does not carry over.
    assert "role" not in claims


def test_mint_token_optional_claims_omitted():
    token = mint_delegated_token(b"k" * 32, uid=7, org_id=None, role_keys=[])
    claims = pyjwt.decode(token, b"k" * 32, algorithms=["HS256"], audience=DELEGATION_AUDIENCE)
    assert claims["sub"] == "7"
    assert "oid" not in claims
    assert "rls" not in claims
    assert "sid" not in claims
    assert "ssu" not in claims


def test_mint_token_carries_sso_subject():
    """OIDC users carry their IdP subject as ``ssu`` for business-operator
    attribution downstream of the gateway (2026-10-06)."""
    token = mint_delegated_token(
        b"k" * 32, uid=42, org_id=ORG, role_keys=["expert_user"], sso_subject="209"
    )
    claims = pyjwt.decode(token, b"k" * 32, algorithms=["HS256"], audience=DELEGATION_AUDIENCE)
    assert claims["ssu"] == "209"
    # Blank/None never emits the claim (local-password users stay anonymous
    # to business services rather than carrying a misleading empty value).
    token = mint_delegated_token(
        b"k" * 32, uid=42, org_id=ORG, role_keys=["expert_user"], sso_subject=None
    )
    claims = pyjwt.decode(token, b"k" * 32, algorithms=["HS256"], audience=DELEGATION_AUDIENCE)
    assert "ssu" not in claims


def test_delegation_secret_persisted_and_stable(db: SqlitePool):
    repo = SecretRepo(db)
    first = get_or_create_delegation_secret(repo)
    again = get_or_create_delegation_secret(repo)
    assert first == again
    assert repo.get(DELEGATION_SECRET_KEY) == first


def test_delegation_secret_env_override(
    db: SqlitePool, monkeypatch: pytest.MonkeyPatch
):
    """Env override wins and never touches the secrets table (2026-10-01)."""
    repo = SecretRepo(db)
    monkeypatch.setenv("OCTOP_MCP_DELEGATION_SECRET", "a" * 64)
    secret = get_or_create_delegation_secret(repo)
    assert secret == ("a" * 64).encode()
    # Used verbatim: hub signs with the same literal string's bytes.
    token = mint_delegated_token(secret, uid=1, org_id=ORG, role_keys=["member"])
    claims = pyjwt.decode(token, secret, algorithms=["HS256"], audience=DELEGATION_AUDIENCE)
    assert claims["sub"] == "1"
    # The DB row was never created — env is the sole source of truth.
    assert repo.get(DELEGATION_SECRET_KEY) is None


def test_delegation_secret_env_blank_falls_back_to_db(
    db: SqlitePool, monkeypatch: pytest.MonkeyPatch
):
    """Blank/unset env keeps the auto-generate-into-DB behaviour."""
    repo = SecretRepo(db)
    monkeypatch.setenv("OCTOP_MCP_DELEGATION_SECRET", "   ")
    first = get_or_create_delegation_secret(repo)
    assert repo.get(DELEGATION_SECRET_KEY) == first
    monkeypatch.delenv("OCTOP_MCP_DELEGATION_SECRET")
    assert get_or_create_delegation_secret(repo) == first


# ------------------------------------------------------- spec flag + inject


def test_normalize_accepts_delegate_identity_flag():
    spec = normalize_server_spec(
        "hub",
        {
            "transport": "streamable_http",
            "url": "http://192.168.181.1:8090/mcp",
            DELEGATE_IDENTITY_FLAG: True,
        },
    )
    assert spec[DELEGATE_IDENTITY_FLAG] is True
    assert spec_wants_delegated_identity(spec)
    # Default is off.
    plain = normalize_server_spec(
        "hub2", {"transport": "streamable_http", "url": "http://192.168.181.1:8090/mcp"}
    )
    assert not spec_wants_delegated_identity(plain)


_HTTP_BASE = {
    "transport": "streamable_http",
    "url": "http://192.168.181.1:8090/mcp",
    "enabled": True,
}


def test_harness_injection_flag_and_token():
    flagged = dict(_HTTP_BASE, delegate_identity=True)
    # Flag + token → header injected.
    out = harness_spec_for_server(flagged, delegate_token="tok-1")
    assert out["headers"]["Authorization"] == "Bearer tok-1"
    assert out["headers"]["Accept"] == "application/json, text/event-stream"
    # Meta flag never leaks into the harness payload.
    assert DELEGATE_IDENTITY_FLAG not in out
    # Flag without token → untouched (fail closed at hub's 401).
    out = harness_spec_for_server(flagged, delegate_token=None)
    assert "Authorization" not in out["headers"]
    # Token without flag → untouched (never leak the token to third parties).
    out = harness_spec_for_server(dict(_HTTP_BASE), delegate_token="tok-1")
    assert "Authorization" not in out["headers"]


def test_harness_injection_replaces_static_authorization():
    flagged = dict(_HTTP_BASE, delegate_identity=True, headers={"Authorization": "Bearer static"})
    out = harness_spec_for_server(flagged, delegate_token="tok-1")
    assert out["headers"]["Authorization"] == "Bearer tok-1"


def test_enabled_harness_configs_routes_token():
    servers = {
        "hub": dict(_HTTP_BASE, delegate_identity=True),
        "third_party": dict(_HTTP_BASE, url="https://mcp.example.com/mcp"),
    }
    configs = enabled_harness_configs(servers, delegate_token="tok-1")
    assert configs["hub"]["headers"]["Authorization"] == "Bearer tok-1"
    assert "Authorization" not in configs["third_party"].get("headers", {})


def test_inject_helper_direct():
    out = inject_delegated_auth_header({"transport": "streamable_http"}, "t")
    assert out["headers"]["Authorization"] == "Bearer t"


# ------------------------------------------------------------- expiry probe


def test_spec_token_exp_reads_delegated_token():
    token = mint_delegated_token(b"k" * 32, uid=1, org_id=ORG, role_keys=["r"], ttl_seconds=1234)
    spec = {"transport": "streamable_http", "headers": {"Authorization": f"Bearer {token}"}}
    exp = spec_token_exp(spec)
    assert exp is not None
    assert 1200 < exp - time.time() <= 1234


def test_spec_token_exp_none_without_delegated_token():
    assert spec_token_exp({}) is None
    assert spec_token_exp({"headers": {}}) is None
    # Static bearer / basic auth / garbage are not delegated JWTs.
    assert spec_token_exp({"headers": {"Authorization": "Bearer static-token"}}) is None
    assert spec_token_exp({"headers": {"Authorization": "Basic abc"}}) is None
    assert spec_token_exp({"headers": {"Authorization": "Bearer not.a.jwt"}}) is None
    assert spec_token_exp("not-a-spec") is None


def test_refresh_margin_is_shorter_than_ttl():
    # The refresh margin must leave room to swap the connection *before*
    # the hub starts rejecting the token.
    assert 0 < DELEGATION_REFRESH_MARGIN_SECONDS < 1800


def test_ttl_env_override_and_margin_scaling(monkeypatch):
    from octop.infra.connectors import delegation as d

    # Default: no env → 30 min TTL, 5 min margin.
    monkeypatch.delenv("OCTOP_MCP_DELEGATION_TTL_SECONDS", raising=False)
    assert d.effective_ttl_seconds() == 1800
    assert d.refresh_margin_seconds() == 300

    # Demo override: 10s TTL → margin scales down to a third (3s), so the
    # refresh check still fires *before* the hub rejects the token.
    monkeypatch.setenv("OCTOP_MCP_DELEGATION_TTL_SECONDS", "10")
    assert d.effective_ttl_seconds() == 10
    assert d.refresh_margin_seconds() == 3

    # Floor: absurdly small values clamp to 10s; garbage falls back to default.
    monkeypatch.setenv("OCTOP_MCP_DELEGATION_TTL_SECONDS", "1")
    assert d.effective_ttl_seconds() == 10
    monkeypatch.setenv("OCTOP_MCP_DELEGATION_TTL_SECONDS", "not-a-number")
    assert d.effective_ttl_seconds() == 1800


def test_mint_uses_env_ttl(monkeypatch):
    import time as _time

    from octop.infra.connectors import delegation as d

    monkeypatch.setenv("OCTOP_MCP_DELEGATION_TTL_SECONDS", "10")
    tok = d.mint_delegated_token(b"k" * 32, uid=1, org_id=ORG, role_keys=["r"])
    claims = pyjwt.decode(tok, options={"verify_signature": False})
    assert claims["exp"] - claims["iat"] == 10
    assert claims["exp"] - _time.time() <= 10


# ------------------------------------------------------------ service wiring


def _put_servers(svc: ConnectorService, uid: int, servers: dict) -> None:
    svc.put_custom_servers(uid, servers)


def test_service_injects_only_for_flagged_server(db: SqlitePool, svc: ConnectorService):
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) "
            "VALUES ('u1', 'x', 'user', 1)"
        )
        uid = int(conn.execute("SELECT id FROM users WHERE username = 'u1'").fetchone()["id"])
    _put_servers(
        svc,
        uid,
        {
            "hub": dict(_HTTP_BASE, delegate_identity=True),
            "plain": dict(_HTTP_BASE, url="http://192.168.181.1:9999/mcp"),
        },
    )
    configs = svc.custom_harness_configs(uid, authz=_authz(uid))
    assert configs["hub"]["headers"]["Authorization"].startswith("Bearer ")
    assert "Authorization" not in configs["plain"].get("headers", {})
    # Token verifies with the service's own persisted secret.
    secret = get_or_create_delegation_secret(SecretRepo(db))
    token = configs["hub"]["headers"]["Authorization"].removeprefix("Bearer ")
    claims = pyjwt.decode(token, secret, algorithms=["HS256"], audience=DELEGATION_AUDIENCE)
    assert claims["sub"] == str(uid)
    assert claims["oid"] == ORG


def test_service_without_authz_mints_nothing(db: SqlitePool, svc: ConnectorService):
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) "
            "VALUES ('u1', 'x', 'user', 1)"
        )
        uid = int(conn.execute("SELECT id FROM users WHERE username = 'u1'").fetchone()["id"])
    _put_servers(svc, uid, {"hub": dict(_HTTP_BASE, delegate_identity=True)})
    configs = svc.custom_harness_configs(uid, authz=None)
    assert "Authorization" not in configs["hub"].get("headers", {})
    # And no secret was even created (mint never runs without authz).
    assert SecretRepo(db).get(DELEGATION_SECRET_KEY) is None


def test_service_token_carries_sso_subject_from_authz(db: SqlitePool, svc: ConnectorService):
    """Authz context's ``sso_subject`` flows into the minted token's ``ssu``."""
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) "
            "VALUES ('u1', 'x', 'user', 1)"
        )
        uid = int(conn.execute("SELECT id FROM users WHERE username = 'u1'").fetchone()["id"])
    _put_servers(svc, uid, {"hub": dict(_HTTP_BASE, delegate_identity=True)})
    authz = AuthzContext(
        user_id=uid, org_id=ORG, role_keys=("expert_user",), sso_subject="209"
    )
    configs = svc.custom_harness_configs(uid, authz=authz)
    secret = get_or_create_delegation_secret(SecretRepo(db))
    token = configs["hub"]["headers"]["Authorization"].removeprefix("Bearer ")
    claims = pyjwt.decode(token, secret, algorithms=["HS256"], audience=DELEGATION_AUDIENCE)
    assert claims["ssu"] == "209"


# ------------------------------------------------- spec_token_exp shape (P1-6)


def test_spec_token_exp_accepts_only_delegated_shape():
    import time as _time

    from octop.infra.connectors import delegation as d

    tok = d.mint_delegated_token(b"k" * 32, uid=1, org_id=ORG, role_keys=["r"])
    spec = {
        "transport": "streamable_http",
        "url": "http://x/mcp",
        "headers": {"Authorization": f"Bearer {tok}"},
    }
    exp = d.spec_token_exp(spec)
    assert exp is not None
    assert exp > _time.time()

    # An IdP-signed OAuth access token is a JWT too — but its iss/aud are
    # not ours, so it must NOT be booked for renewal (P1-6).
    idp_claims = {
        "sub": "u1",
        "iss": "https://idp.example.com",
        "aud": "some-api",
        "iat": int(_time.time()),
        "exp": int(_time.time()) + 3600,
    }
    idp_tok = pyjwt.encode(idp_claims, b"idp-key", algorithm="HS256")
    idp_spec = {
        "transport": "streamable_http",
        "url": "http://x/mcp",
        "headers": {"Authorization": f"Bearer {idp_tok}"},
    }
    assert d.spec_token_exp(idp_spec) is None

    # Our issuer but a foreign audience is also rejected.
    wrong_aud = dict(idp_claims, iss=d.DELEGATION_ISSUER)
    wrong_tok = pyjwt.encode(wrong_aud, b"k", algorithm="HS256")
    assert (
        d.spec_token_exp(
            {
                "headers": {"Authorization": f"Bearer {wrong_tok}"},
            }
        )
        is None
    )

    # Audience may be a list containing ours.
    list_aud = pyjwt.encode(
        dict(idp_claims, iss=d.DELEGATION_ISSUER, aud=["other", d.DELEGATION_AUDIENCE]),
        b"k",
        algorithm="HS256",
    )
    assert (
        d.spec_token_exp({"headers": {"Authorization": f"Bearer {list_aud}"}}) is not None
    )
