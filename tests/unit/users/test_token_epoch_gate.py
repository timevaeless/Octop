"""Round-10 review regression tests for the ``ver`` gate (T4-3).

Live finding being locked down: the gate used to compare the token's ``ver``
against the *cached* ``User`` object. On bump paths that don't refresh the
manager cache (``org_members.set_member_roles``, ``pending_users.assign``,
``bump_org_epochs``) that accepted stale tokens and rejected freshly re-issued
ones. The gate is now DB-authoritative.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from octop.api.deps import resolve_user_from_token
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.identity import Role, User
from octop.infra.users.token_tenancy import assert_token_epoch

SECRET = b"unit-test-secret"


def _row(*, token_epoch: int, disabled: int = 0) -> SimpleNamespace:
    return SimpleNamespace(id=7, username="admin_2", disabled=disabled, token_epoch=token_epoch)


def _server(row: SimpleNamespace | None) -> SimpleNamespace:
    services = SimpleNamespace(
        secret_repo=SimpleNamespace(get=lambda key: SECRET),
        user_repo=SimpleNamespace(get=lambda user_id: row),
    )
    return SimpleNamespace(services=services)


def _cached_user(token_epoch: int) -> User:
    return User(
        id=7,
        username="admin_2",
        role=Role.USER,
        display_name="孙悟空",
        token_epoch=token_epoch,
    )


# ------------------------------------------------------------------ #
# assert_token_epoch directly
# ------------------------------------------------------------------ #


def test_gate_is_db_authoritative_when_cache_is_stale() -> None:
    """The reviewer's live case: cache thinks ver=1, DB says 3.

    A token signed with the *DB* value must pass; the pre-change token (old
    ver) must be rejected — previously exactly inverted.
    """
    server = _server(_row(token_epoch=3))
    fresh = {"sub": "7", "ver": 3}
    stale = {"sub": "7", "ver": 1}

    assert_token_epoch(server, fresh, _cached_user(token_epoch=1))

    with pytest.raises(OctopError) as exc:
        assert_token_epoch(server, stale, _cached_user(token_epoch=1))
    assert exc.value.code is ErrorCode.TOKEN_EXPIRED


def test_gate_rejects_when_db_row_disappeared() -> None:
    server = _server(None)
    with pytest.raises(OctopError) as exc:
        assert_token_epoch(server, {"sub": "7", "ver": 0}, _cached_user(token_epoch=0))
    assert exc.value.code is ErrorCode.USER_DISABLED


def test_gate_rejects_disabled_row_even_if_cache_is_fresh() -> None:
    server = _server(_row(token_epoch=2, disabled=1))
    with pytest.raises(OctopError) as exc:
        assert_token_epoch(server, {"sub": "7", "ver": 2}, _cached_user(token_epoch=2))
    assert exc.value.code is ErrorCode.USER_DISABLED


def test_gate_treats_missing_ver_as_zero() -> None:
    """Community-era tokens carry no ``ver``; they live only while DB epoch is 0."""
    server = _server(_row(token_epoch=0))
    assert_token_epoch(server, {"sub": "7"}, _cached_user(token_epoch=0))

    server = _server(_row(token_epoch=2))
    with pytest.raises(OctopError) as exc:
        assert_token_epoch(server, {"sub": "7"}, _cached_user(token_epoch=2))
    assert exc.value.code is ErrorCode.TOKEN_EXPIRED


# ------------------------------------------------------------------ #
# Through resolve_user_from_token (the actual request path)
# ------------------------------------------------------------------ #


def _issue(secret: bytes, sub: str, ver: int) -> str:
    import time

    import jwt

    now = int(time.time())
    payload = {"sub": sub, "uname": "admin_2", "role": "user", "ver": ver,
               "iat": now, "exp": now + 600}
    return jwt.encode(payload, secret, algorithm="HS256")


def test_resolve_rejects_stale_token_after_bump_without_cache_refresh() -> None:
    """org_members path: bump lands in DB only; the old token dies on next request."""
    row = _row(token_epoch=3)
    server = _server(row)
    server.user_manager = SimpleNamespace(
        get_by_id=lambda user_id: _cached_user(token_epoch=1)
    )
    with pytest.raises(OctopError) as exc:
        resolve_user_from_token(server, _issue(SECRET, "7", 1))
    assert exc.value.code is ErrorCode.TOKEN_EXPIRED


def test_resolve_accepts_token_signed_with_db_epoch() -> None:
    """And the re-issued (post-bump) token passes — the reviewer's 401 case."""
    row = _row(token_epoch=3)
    server = _server(row)
    server.user_manager = SimpleNamespace(
        get_by_id=lambda user_id: _cached_user(token_epoch=1)
    )
    user = resolve_user_from_token(server, _issue(SECRET, "7", 3))
    assert user.id == 7
