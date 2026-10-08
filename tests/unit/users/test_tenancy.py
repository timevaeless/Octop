"""tests for octop.infra.users.tenancy — the shared-resource visibility rule.

2026-09-27 three-state attribution: ``None`` = unattributed (admin request
paths only), ``'*'`` = explicit platform share, otherwise the tenant hard
boundary.
"""

from __future__ import annotations

from octop.infra.users.context import AuthzContext
from octop.infra.users.tenancy import shared_resource_visible, viewer_authz

ORG = "cheryfs"
OTHER_ORG = "dealer"
PLATFORM = "*"

VIEWER = AuthzContext(user_id=2, org_id=ORG, role_keys=("finance_operator",))
ROLELESS = AuthzContext(user_id=3, org_id=ORG, role_keys=())
ADMIN = AuthzContext(user_id=4, org_id=OTHER_ORG, is_admin=True)


def test_admin_cross_tenant_flavor_bypasses_everything() -> None:
    # Router flavor: admins are platform-wide, mirroring can_access_resource —
    # including unattributed drafts (that is the only way they are reachable).
    assert shared_resource_visible(OTHER_ORG, ["x"], VIEWER) is False  # sanity: viewer
    assert shared_resource_visible(OTHER_ORG, ["x"], ADMIN, admin_cross_tenant=True)
    assert shared_resource_visible(None, ["x"], ADMIN, admin_cross_tenant=True)


def test_runtime_flavor_binds_admin_to_the_org_boundary() -> None:
    # Runtime flavor mirrors list_visible: org boundary holds even for admins,
    # the role gate is lifted within it. Unattributed rows are never runtime-
    # reachable (the flavor withholds the bypass on purpose: no cross-tenant
    # credential carry-over).
    assert not shared_resource_visible(ORG, [], ADMIN, admin_cross_tenant=False)
    assert shared_resource_visible(OTHER_ORG, ["x"], ADMIN, admin_cross_tenant=False)
    assert not shared_resource_visible(None, [], ADMIN, admin_cross_tenant=False)


def test_unattributed_rows_are_invisible_to_tenants() -> None:
    # A platform-admin draft (org None) reaches no tenant viewer, with or
    # without roles — the old "NULL = platform-shared" semantics is retired.
    assert not shared_resource_visible(None, ["finance_operator"], VIEWER)
    assert not shared_resource_visible(None, [], VIEWER)
    assert not shared_resource_visible(None, [], ROLELESS)


def test_org_is_a_hard_boundary_for_regular_viewers() -> None:
    # A role grant can never cross the tenant boundary.
    assert not shared_resource_visible(OTHER_ORG, ["finance_operator"], VIEWER)
    # Tenant-less viewer: only explicit platform shares are reachable.
    driftless = AuthzContext(user_id=5, org_id=None, role_keys=("finance_operator",))
    assert not shared_resource_visible(ORG, ["finance_operator"], driftless)
    assert shared_resource_visible(PLATFORM, [], driftless)


def test_platform_share_is_explicit_and_role_narrowed() -> None:
    # '*' crosses every tenant boundary, but the ACL still narrows.
    assert shared_resource_visible(PLATFORM, ["finance_operator"], VIEWER)
    assert shared_resource_visible(PLATFORM, [], VIEWER)  # unrestricted
    assert not shared_resource_visible(PLATFORM, ["other_role"], VIEWER)
    # Fail-closed for a viewer holding no keys, exactly like tenant rows.
    assert shared_resource_visible(PLATFORM, [], ROLELESS)
    assert not shared_resource_visible(PLATFORM, ["finance_operator"], ROLELESS)


def test_allowed_roles_narrow_within_the_tenant() -> None:
    assert shared_resource_visible(ORG, [], VIEWER)  # empty = unrestricted
    assert shared_resource_visible(ORG, None, VIEWER)  # absent = unrestricted
    assert shared_resource_visible(ORG, ["finance_operator"], VIEWER)  # intersect
    assert not shared_resource_visible(ORG, ["other_role"], VIEWER)  # no intersect
    # Fail-closed: a viewer holding no keys only sees unrestricted resources.
    assert shared_resource_visible(ORG, [], ROLELESS)
    assert not shared_resource_visible(ORG, ["finance_operator"], ROLELESS)


def test_viewer_authz_tolerates_user_like_fakes() -> None:
    from types import SimpleNamespace

    full = viewer_authz(SimpleNamespace(id=1, org_id=ORG, role_keys=["a"], is_admin=False))
    assert full == AuthzContext(user_id=1, org_id=ORG, role_keys=("a",))

    # Minimal fake: no org, no roles, admin flag missing → False (not a crash).
    bare = viewer_authz(SimpleNamespace(id=2))
    assert bare == AuthzContext(user_id=2)

    # None role_keys must not become (None,) — normalize to an empty tuple.
    assert viewer_authz(SimpleNamespace(id=3, role_keys=None)).role_keys == ()


# --------------------------------- 2026-10-01: 租户管理员 = 租户内最大角色


def test_org_admin_lifts_the_role_filter_within_the_tenant() -> None:
    # The org_admin package holder sees every resource of their own tenant
    # (role_filter None), but the tenant boundary itself still binds.
    boss = AuthzContext(
        user_id=6, org_id=ORG, role_keys=("org_admin",), is_org_admin=True
    )
    assert boss.role_filter is None
    # org gate: own tenant + platform shares reachable, others not.
    assert shared_resource_visible(ORG, ["x"], boss)
    assert shared_resource_visible(PLATFORM, ["x"], boss)
    assert not shared_resource_visible(OTHER_ORG, ["x"], boss)
    assert not shared_resource_visible(None, ["x"], boss)  # unattributed drafts


def test_regular_viewers_keep_the_role_filter() -> None:
    assert VIEWER.role_filter == ["finance_operator"]
    assert ROLELESS.role_filter == []


def test_can_access_resource_lifts_for_org_admin() -> None:
    from types import SimpleNamespace

    from octop.infra.users.token_tenancy import can_access_resource

    boss = SimpleNamespace(org_id=ORG, role_keys=("org_admin",), is_admin=False)
    member = SimpleNamespace(org_id=ORG, role_keys=(), is_admin=False)
    other = SimpleNamespace(org_id=OTHER_ORG, role_keys=("org_admin",), is_admin=False)

    # own-tenant and platform-shared rows: the org_admin package is enough
    assert can_access_resource(boss, resource_org_id=ORG, allowed_roles=["x"])
    assert can_access_resource(boss, resource_org_id=PLATFORM, allowed_roles=["x"])
    # hard boundary untouched: another tenant's rows stay invisible
    assert not can_access_resource(boss, resource_org_id=OTHER_ORG, allowed_roles=[])
    assert not can_access_resource(boss, resource_org_id=None, allowed_roles=[])
    # a plain member still rides the ACL intersection
    assert can_access_resource(member, resource_org_id=ORG, allowed_roles=[])
    assert not can_access_resource(member, resource_org_id=ORG, allowed_roles=["x"])
    # org_admin in the WRONG org gains nothing
    assert not can_access_resource(other, resource_org_id=ORG, allowed_roles=["x"])
