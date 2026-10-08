"""Tests for the 2026-10-01 tenant-admin visibility lift (user decision).

The ``org_admin`` package holder is the tenant's maximally-privileged role:
inside their own tenant they see every resource (like a platform admin does
platform-wide), while the tenant boundary itself stays fully enforced.
"""

from __future__ import annotations

from octop.infra.users.context import AuthzContext, authz_context_from_user
from octop.infra.users.token_tenancy import can_access_resource
from octop.infra.users.tenancy import shared_resource_visible, viewer_authz
from octop.mcp_catalog.tenant_share import CATALOG_KIND_PREFIX  # noqa: F401 — kind stability guard


class _User:
    """Minimal user double for authz_context_from_user."""

    def __init__(self, role_keys, is_admin=False, org_id="org-a"):
        self.id = 1
        self.username = "u"
        self.org_id = org_id
        self.role_keys = list(role_keys)
        self.is_admin = is_admin


# ------------------------------------------------------------ AuthzContext.role_filter


def test_org_admin_role_filter_is_lifted():
    authz = AuthzContext(user_id=1, org_id="org-a", role_keys=("org_admin",), is_org_admin=True)
    assert authz.role_filter is None


def test_plain_member_role_filter_still_applies():
    authz = AuthzContext(user_id=2, org_id="org-a", role_keys=("r1",))
    assert authz.role_filter == ["r1"]
    assert AuthzContext(user_id=3, org_id="org-a", role_keys=()).role_filter == []


def test_platform_admin_unchanged():
    authz = AuthzContext(user_id=4, role_keys=("org_admin",), is_admin=True, is_org_admin=False)
    assert authz.role_filter is None


def test_authz_context_from_user_sets_org_admin_flag():
    lifted = authz_context_from_user(_User(("org_admin", "r1")))
    assert lifted.is_org_admin is True and lifted.role_filter is None
    plain = authz_context_from_user(_User(("r1",)))
    assert plain.is_org_admin is False and plain.role_filter == ["r1"]
    # a platform admin is NOT flagged org-admin (their lift is broader already)
    admin = authz_context_from_user(_User(("org_admin",), is_admin=True))
    assert admin.is_org_admin is False and admin.role_filter is None


def test_viewer_authz_sets_org_admin_flag():
    lifted = viewer_authz(_User(("org_admin",)))
    assert lifted.is_org_admin is True
    assert viewer_authz(_User(("r1",))).is_org_admin is False


# ------------------------------------------------------ shared_resource_visible

def test_org_admin_sees_tenant_and_shared_rows_regardless_of_roles():
    boss = viewer_authz(_User(("org_admin",), org_id="org-a"))
    assert shared_resource_visible("org-a", ["r-secret"], boss)
    assert shared_resource_visible("*", ["r-secret"], boss)
    # tenant boundary STILL binds: another tenant's rows stay invisible
    assert not shared_resource_visible("org-b", ["r-secret"], boss)


def test_member_role_gate_unchanged():
    member = viewer_authz(_User(("r1",), org_id="org-a"))
    assert shared_resource_visible("org-a", ["r1"], member)
    assert not shared_resource_visible("org-a", ["r2"], member)
    assert not shared_resource_visible("org-b", ["r1"], member)


# -------------------------------------------------------- can_access_resource


def test_token_path_org_admin_lift_within_org():
    user = viewer_authz(_User(("org_admin",), org_id="org-a"))
    assert can_access_resource(user, resource_org_id="org-a", allowed_roles=["r-secret"])
    assert can_access_resource(user, resource_org_id="*", allowed_roles=["r-secret"])
    assert not can_access_resource(user, resource_org_id="org-b", allowed_roles=["r-secret"])


def test_token_path_member_unchanged():
    user = viewer_authz(_User(("r1",), org_id="org-a"))
    assert can_access_resource(user, resource_org_id="org-a", allowed_roles=["r1"])
    assert not can_access_resource(user, resource_org_id="org-a", allowed_roles=["r2"])
    assert not can_access_resource(user, resource_org_id="org-b", allowed_roles=["r1"])
