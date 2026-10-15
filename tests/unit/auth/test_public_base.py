"""Tests for public OIDC callback URL helpers."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from octop.api.common.public_base import resolve_public_base
from octop.infra.auth.sso.public_base import build_redirect_uri


def _request(*, scheme: str = "http", headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "scheme": scheme,
            "server": ("internal.example", 8080),
            "path": "/",
            "headers": headers or [(b"host", b"internal.example:8080")],
        }
    )


def test_resolve_public_base_uses_forwarded_proxy_headers() -> None:
    request = _request(
        headers=[
            (b"host", b"internal.example:8080"),
            (b"x-forwarded-proto", b"https"),
            (b"x-forwarded-host", b"octop.example"),
        ]
    )

    assert resolve_public_base(request) == "https://octop.example"


def test_resolve_public_base_uses_request_url_without_proxy_headers() -> None:
    request = _request(scheme="https", headers=[(b"host", b"octop.example")])

    assert resolve_public_base(request) == "https://octop.example"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/octop", "https://octop.example/octop"),
        ("/octop/", "https://octop.example/octop"),
        ("octop", "https://octop.example"),
        ("/", "https://octop.example"),
        ("", "https://octop.example"),
    ],
)
def test_resolve_public_base_appends_base_path_env(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
) -> None:
    monkeypatch.setenv("OCTOP_PUBLIC_BASE_PATH", raw)
    request = _request(
        headers=[
            (b"host", b"internal.example:8080"),
            (b"x-forwarded-proto", b"https"),
            (b"x-forwarded-host", b"octop.example"),
        ]
    )

    assert resolve_public_base(request) == expected


def test_resolve_public_base_base_path_env_unset_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OCTOP_PUBLIC_BASE_PATH", raising=False)
    request = _request(
        headers=[
            (b"host", b"internal.example:8080"),
            (b"x-forwarded-proto", b"https"),
            (b"x-forwarded-host", b"octop.example"),
        ]
    )

    assert resolve_public_base(request) == "https://octop.example"


def test_resolve_public_base_base_path_with_request_url_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OCTOP_PUBLIC_BASE_PATH", "/octop")
    request = _request(scheme="https", headers=[(b"host", b"octop.example")])

    assert resolve_public_base(request) == "https://octop.example/octop"


def test_build_redirect_uri_carries_base_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCTOP_PUBLIC_BASE_PATH", "/octop")
    request = _request(
        headers=[
            (b"host", b"internal.example:8080"),
            (b"x-forwarded-proto", b"https"),
            (b"x-forwarded-host", b"octop.example"),
        ]
    )

    assert (
        build_redirect_uri(resolve_public_base(request))
        == "https://octop.example/octop/api/auth/oidc/callback"
    )


def test_build_redirect_uri_uses_oidc_callback_path() -> None:
    assert (
        build_redirect_uri("https://octop.example/")
        == "https://octop.example/api/auth/oidc/callback"
    )


def test_build_redirect_uri_accepts_oauth_callback_path() -> None:
    assert (
        build_redirect_uri("https://octop.example", "/api/auth/oauth/callback")
        == "https://octop.example/api/auth/oauth/callback"
    )
