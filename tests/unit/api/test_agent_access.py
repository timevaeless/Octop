"""tests/unit/api/test_agent_access.py"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from octop.api.common.agent import (
    agent_is_shared,
    assert_agent_access_row,
    assert_agent_owner,
    require_agent_row,
    user_owns_agent,
)
from octop.api.common.agent_tenancy import agent_owner_org, assert_shared_agent_within_tenant
from octop.infra.errors import ErrorCode, OctopError


def test_owner_may_access() -> None:
    row = SimpleNamespace(user_id=1)
    user = SimpleNamespace(id=1, is_admin=False)
    assert_agent_owner(row, user)


def test_non_owner_forbidden() -> None:
    row = SimpleNamespace(user_id=1)
    user = SimpleNamespace(id=2, is_admin=False)
    with pytest.raises(OctopError) as ei:
        assert_agent_owner(row, user)
    assert ei.value.code is ErrorCode.FORBIDDEN


def test_shared_agent_non_admin_forbidden() -> None:
    row = SimpleNamespace(user_id=None)
    user = SimpleNamespace(id=1, is_admin=False)
    with pytest.raises(OctopError) as ei:
        assert_agent_owner(row, user)
    assert ei.value.code is ErrorCode.FORBIDDEN


def test_shared_agent_admin_allowed() -> None:
    row = SimpleNamespace(user_id=None)
    user = SimpleNamespace(id=1, is_admin=True)
    assert_agent_owner(row, user)


def test_shared_agent_accessible_to_non_owner() -> None:
    row = SimpleNamespace(user_id=1, is_shared=1)
    user = SimpleNamespace(id=2, is_admin=False)
    assert_agent_access_row(row, user)


def test_private_agent_forbidden_to_non_owner() -> None:
    row = SimpleNamespace(user_id=1, is_shared=0)
    user = SimpleNamespace(id=2, is_admin=False)
    with pytest.raises(OctopError) as ei:
        assert_agent_access_row(row, user)
    assert ei.value.code == ErrorCode.FORBIDDEN


def test_shared_non_owner_still_fails_owner_assert() -> None:
    row = SimpleNamespace(user_id=1, is_shared=1)
    user = SimpleNamespace(id=2, is_admin=False)
    with pytest.raises(OctopError):
        assert_agent_owner(row, user)


def test_agent_is_shared_true_when_flag_set() -> None:
    row = SimpleNamespace(is_shared=1)
    assert agent_is_shared(row) is True


def test_agent_is_shared_false_when_missing_or_zero() -> None:
    assert agent_is_shared(SimpleNamespace(is_shared=0)) is False
    assert agent_is_shared(SimpleNamespace()) is False


def test_user_owns_agent_only_for_matching_owner() -> None:
    row = SimpleNamespace(user_id=1)
    owner = SimpleNamespace(id=1, is_admin=False)
    other = SimpleNamespace(id=2, is_admin=False)
    admin = SimpleNamespace(id=99, is_admin=True)
    assert user_owns_agent(row, owner) is True
    assert user_owns_agent(row, other) is False
    assert user_owns_agent(row, admin) is False


# ---------------------------------------------- shared-agent tenancy (016)
#
# Sharing is tenant-scoped: a shared agent's MCP configs are built from the
# *owner's* connectors, so invoking it cross-tenant would reach another
# tenant's business systems. Listing already filters; these tests lock the
# by-id gate (chat turn / history / cron / workspace all funnel through
# require_agent_row).


def _owner_repo_with(org_by_id: dict[int, str | None]) -> SimpleNamespace:
    class _Repo:
        def get(self, uid: int):
            if uid not in org_by_id:
                return None
            return SimpleNamespace(id=uid, default_org_id=org_by_id[uid])

    return SimpleNamespace(user_repo=_Repo())


def _server_with(row: SimpleNamespace, org_by_id: dict[int, str | None]) -> SimpleNamespace:
    return SimpleNamespace(
        app_runtime=SimpleNamespace(agent_registry=SimpleNamespace(get_row=lambda _: row)),
        services=_owner_repo_with(org_by_id),
    )


OWNER_ID = 1
PEER_ID = 2  # same tenant as owner
OUTSIDER_ID = 3  # another tenant
ADMIN_ID = 9

ORG_BY_ID: dict[int, str | None] = {
    OWNER_ID: "cheryfs",
    PEER_ID: "cheryfs",
    OUTSIDER_ID: "chery-dealer-001",
    ADMIN_ID: "chery-dealer-001",
}


def _user(uid: int) -> SimpleNamespace:
    return SimpleNamespace(id=uid, org_id=ORG_BY_ID[uid], is_admin=uid == ADMIN_ID)


def _shared_row() -> SimpleNamespace:
    # 2026-09-27: the *row's* org_id is the gating attribution — no org attr
    # would read as unattributed (admin-only), so give the shared row one.
    return SimpleNamespace(user_id=OWNER_ID, is_shared=1, org_id="cheryfs")


def test_shared_agent_invokable_within_owner_tenant() -> None:
    server = _server_with(_shared_row(), ORG_BY_ID)
    row = require_agent_row("a1", user=_user(PEER_ID), as_user=None, server=server)
    assert row is not None


def test_shared_agent_forbidden_from_another_tenant() -> None:
    server = _server_with(_shared_row(), ORG_BY_ID)
    with pytest.raises(OctopError) as ei:
        require_agent_row("a1", user=_user(OUTSIDER_ID), as_user=None, server=server)
    assert ei.value.code is ErrorCode.FORBIDDEN


def test_shared_agent_owner_and_admin_bypass_tenant_gate() -> None:
    server = _server_with(_shared_row(), ORG_BY_ID)
    assert require_agent_row("a1", user=_user(OWNER_ID), as_user=None, server=server)
    assert require_agent_row("a1", user=_user(ADMIN_ID), as_user=None, server=server)


def test_unattributed_shared_agent_is_admin_only() -> None:
    """2026-09-27: an unattributed (org None) shared row is an admin draft.

    Tenants — even same-tenant peers — must not reach it; the platform admin
    request path is the only consumer until attribution is assigned.
    """
    row = SimpleNamespace(user_id=None, is_shared=1, org_id=None)
    server = _server_with(row, ORG_BY_ID)
    with pytest.raises(OctopError) as ei:
        require_agent_row("a1", user=_user(OUTSIDER_ID), as_user=None, server=server)
    assert ei.value.code is ErrorCode.FORBIDDEN
    # The admin request flavor reaches it (bypass).
    assert require_agent_row("a1", user=_user(ADMIN_ID), as_user=None, server=server)


def test_platform_shared_agent_reaches_every_tenant() -> None:
    """'*' = explicit platform share: any tenant may invoke, roles permitting."""
    row = SimpleNamespace(user_id=None, is_shared=1, org_id="*", allowed_roles=[])
    server = _server_with(row, ORG_BY_ID)
    assert require_agent_row("a1", user=_user(OUTSIDER_ID), as_user=None, server=server)


def test_tenant_less_viewer_cannot_invoke_tenant_scoped_shared_agent() -> None:
    """Fail-closed: no tenant context only reaches explicit platform shares."""
    viewer = SimpleNamespace(id=PEER_ID, org_id=None, is_admin=False)
    server = _server_with(_shared_row(), ORG_BY_ID)
    with pytest.raises(OctopError) as ei:
        require_agent_row("a1", user=viewer, as_user=None, server=server)
    assert ei.value.code is ErrorCode.FORBIDDEN


def test_private_agent_gate_is_owner_only_even_same_tenant() -> None:
    row = SimpleNamespace(user_id=OWNER_ID, is_shared=0)
    server = _server_with(row, ORG_BY_ID)
    with pytest.raises(OctopError) as ei:
        require_agent_row("a1", user=_user(PEER_ID), as_user=None, server=server)
    assert ei.value.code is ErrorCode.FORBIDDEN


def test_assert_shared_agent_within_tenant_matrix() -> None:
    # Same tenant: allowed; cross tenant: forbidden.
    assert_shared_agent_within_tenant(_shared_row(), _user(PEER_ID), owner_org="cheryfs")
    with pytest.raises(OctopError):
        assert_shared_agent_within_tenant(_shared_row(), _user(OUTSIDER_ID), owner_org="cheryfs")
    # 2026-09-27: unattributed (org None) = admin draft — forbidden for a
    # tenant viewer even when a derived owner-org would have said otherwise
    # (the gate reads the ROW's attribution, never the derived owner org).
    draft = SimpleNamespace(user_id=OWNER_ID, is_shared=1, org_id=None)
    with pytest.raises(OctopError):
        assert_shared_agent_within_tenant(draft, _user(OUTSIDER_ID), owner_org="cheryfs")
    # Explicit platform share ('*') crosses tenants.
    platform = SimpleNamespace(user_id=None, is_shared=1, org_id="*", allowed_roles=[])
    assert_shared_agent_within_tenant(platform, _user(OUTSIDER_ID), owner_org=None)
    # Admin bypasses even cross-tenant (request layer owns the full bypass).
    assert_shared_agent_within_tenant(_shared_row(), _user(ADMIN_ID), owner_org="cheryfs")
    # Owner and non-shared rows are out of scope of this gate.
    assert_shared_agent_within_tenant(_shared_row(), _user(OWNER_ID), owner_org="cheryfs")
    assert_shared_agent_within_tenant(
        SimpleNamespace(user_id=OUTSIDER_ID, is_shared=0),
        _user(PEER_ID),
        owner_org="cheryfs",
    )


def test_agent_owner_org_resolves_from_user_repo() -> None:
    server = _server_with(_shared_row(), ORG_BY_ID)
    assert agent_owner_org(server, SimpleNamespace(user_id=OWNER_ID)) == "cheryfs"
    # Unknown owner / NULL owner degrade to None (unattributed), not errors.
    assert agent_owner_org(server, SimpleNamespace(user_id=42)) is None
    assert agent_owner_org(server, SimpleNamespace(user_id=None)) is None
