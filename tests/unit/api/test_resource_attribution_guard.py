"""tests/unit/api/test_resource_attribution_guard.py

The 2026-10-19 RLS 500: a non-admin caller without ``default_org_id`` had
``creator_org_id`` return ``None``, and the NULL-stamped INSERT died in the
migration-029 WITH CHECK (which no longer carries an ``org_id IS NULL``
branch). ``creator_org_id_checked`` rejects that caller up front with a
403 instead of letting RLS surface as a 500.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.resource_attribution import (
    creator_org_id,
    creator_org_id_checked,
)


def test_admin_gets_unattributed_without_raise() -> None:
    admin = SimpleNamespace(is_admin=True, org_id=None)
    assert creator_org_id_checked(admin) is None


def test_tenant_user_stamps_own_org() -> None:
    user = SimpleNamespace(is_admin=False, org_id="acme")
    assert creator_org_id_checked(user) == "acme"


def test_unattributed_non_admin_rejected_with_403() -> None:
    user = SimpleNamespace(is_admin=False, org_id=None)
    with pytest.raises(OctopError) as excinfo:
        creator_org_id_checked(user)
    err = excinfo.value
    assert err.code is ErrorCode.USER_NOT_ATTRIBUTED
    assert err.status == 403


def test_unattributed_error_localizes_to_zh() -> None:
    err = OctopError(ErrorCode.USER_NOT_ATTRIBUTED, "fallback")
    assert err.to_envelope(locale="zh")["error"]["message"] == (
        "无权限，请联系管理员归位。"
    )
    assert err.to_envelope(locale="en")["error"]["message"].startswith(
        "Permission denied."
    )


def test_plain_creator_org_id_keeps_legacy_behaviour() -> None:
    # The unchecked variant must stay raise-free for bootstrap paths.
    user = SimpleNamespace(is_admin=False, org_id=None)
    assert creator_org_id(user) is None
