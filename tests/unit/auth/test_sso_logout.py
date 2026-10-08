"""Tests for provider-agnostic SSO single-logout (octop/infra/auth/sso/logout.py)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from octop.config import OctopConfig
from octop.infra.auth.sso.logout import (
    build_slo_redirect_url,
    consume_logout_hint,
    pop_raw_id_token,
    resolve_end_session_endpoint,
    stash_raw_id_token,
    store_logout_hint,
)
from octop.infra.auth.sso.service import SsoService
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.users.manager import UserManager
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def services(tmp_path):
    paths = PathLayout(tmp_path / ".octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    return build_shared_services(db=db, paths=paths, config=OctopConfig())


class _FakeDiscovery:
    def __init__(self, doc: dict[str, object]) -> None:
        self._doc = doc

    def get(self, issuer: str, httpx_client: object = None) -> dict[str, object]:
        return self._doc


class _FakeSsoService:
    def __init__(self, doc: dict[str, object]) -> None:
        self.discovery = _FakeDiscovery(doc)


def _provider(**extra: object) -> SimpleNamespace:
    return SimpleNamespace(issuer="https://issuer.example", extra=dict(extra))


def test_hint_roundtrip_is_one_shot(services) -> None:
    store_logout_hint(services.db, user_id=7, provider_id=1, id_token="tok-a")
    store_logout_hint(services.db, user_id=7, provider_id=1, id_token="tok-b")

    assert consume_logout_hint(services.db, 7) == (1, "tok-b")
    assert consume_logout_hint(services.db, 7) is None
    assert consume_logout_hint(services.db, 404) is None


def test_raw_id_token_stash_roundtrip() -> None:
    claims: dict[str, object] = {"sub": "s1"}
    stash_raw_id_token(claims, "raw-token")
    assert pop_raw_id_token(claims) == "raw-token"
    assert pop_raw_id_token(claims) is None
    assert claims == {"sub": "s1"}


def test_resolve_prefers_explicit_override_over_discovery() -> None:
    fake = _FakeSsoService({"end_session_endpoint": "https://from.discovery/logout"})
    provider = _provider(end_session_endpoint="https://admin.configured/logout")
    assert resolve_end_session_endpoint(fake, provider) == "https://admin.configured/logout"


def test_resolve_falls_back_to_discovery() -> None:
    fake = _FakeSsoService({"end_session_endpoint": "https://from.discovery/logout"})
    assert resolve_end_session_endpoint(fake, _provider()) == "https://from.discovery/logout"


def test_resolve_degrades_to_none_without_discovery_or_override() -> None:
    fake = _FakeSsoService({"token_endpoint": "https://issuer.example/token"})
    assert resolve_end_session_endpoint(fake, _provider()) is None


def test_resolve_survives_discovery_failure() -> None:
    class _Broken:
        def get(self, issuer: str, httpx_client: object = None) -> dict[str, object]:
            raise httpx.ConnectError("boom")

    fake = SimpleNamespace(discovery=_Broken())
    assert resolve_end_session_endpoint(fake, _provider()) is None


def test_build_url_includes_hint_and_optional_post_logout() -> None:
    fake = _FakeSsoService({"end_session_endpoint": "https://issuer.example/logout"})

    url = build_slo_redirect_url(fake, _provider(), "tok-123")
    assert url is not None
    parts = urlsplit(url)
    assert parts.scheme == "https" and parts.path == "/logout"
    assert parse_qs(parts.query)["id_token_hint"] == ["tok-123"]
    assert "post_logout_redirect_uri" not in parse_qs(parts.query)

    configured = _provider(post_logout_redirect_uri="https://octop.example/login")
    url2 = build_slo_redirect_url(fake, configured, "tok-123")
    assert url2 is not None
    query = parse_qs(urlsplit(url2).query)
    assert query["post_logout_redirect_uri"] == ["https://octop.example/login"]


def test_build_url_returns_none_when_provider_cannot_slo() -> None:
    fake = _FakeSsoService({})
    assert build_slo_redirect_url(fake, _provider(), "tok") is None


@pytest.mark.asyncio
async def test_login_stores_logout_hint_for_real_callback(tmp_path) -> None:
    """Full callback flow persists the verified id_token for later SLO use."""
    paths = PathLayout(tmp_path / ".octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    service = SsoService(services, UserManager(services))

    services.user_repo.create(username="admin", role="admin")
    services.sso_repo.upsert_provider(
        enabled=1,
        display_name="Example ID",
        issuer="https://issuer.example/",
        client_id="client-id",
        client_secret_enc=None,
        scopes="openid profile email",
        dashboard_origin=None,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": "https://issuer.example",
                    "authorization_endpoint": "https://issuer.example/authorize",
                    "token_endpoint": "https://issuer.example/token",
                    "jwks_uri": "https://issuer.example/jwks",
                },
            )
        return httpx.Response(200, json={"id_token": "signed-id-token"})

    real_client = httpx.Client

    def factory(*args: object, **kwargs: object) -> httpx.Client:
        kwargs.pop("transport", None)
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    with (
        patch("octop.infra.auth.sso.service.httpx.Client", side_effect=factory),
        patch(
            "octop.infra.auth.sso.service.verify_id_token",
            return_value={"sub": "subject-123", "preferred_username": "member"},
        ),
    ):
        started = service.start_login(
            redirect_after="/chat", public_base="https://octop.example"
        )
        state = httpx.QueryParams(started["authorization_url"].split("?", 1)[1])["state"]
        await service.handle_callback(
            code="authorization-code", state=state, error=None,
            public_base="https://octop.example",
        )

    user = services.user_repo.get_by_username("member")
    assert user is not None
    # The hint row exists and yields the raw token exactly once.
    assert consume_logout_hint(services.db, user.id) == (1, "signed-id-token")
    assert consume_logout_hint(services.db, user.id) is None
