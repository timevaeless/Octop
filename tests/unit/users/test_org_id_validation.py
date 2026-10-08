"""org_id shape validation must align with the filesystem sanitizer (P1-2).

``_safe_segment`` (infra/utils/paths.py) maps ``.`` to ``_``; an org id
containing a dot would collide with its underscored twin onto the same
``orgs/<id>`` directory — cross-tenant file mixing. Dots are therefore
rejected at the only org-creation entry point.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from octop.api.routers.roles import _ORG_ID_RE, _validate_org_id


@pytest.mark.parametrize("bad", ["team.a", "a.b", ".x", "x.", "a b", "a/b"])
def test_org_id_rejects_unsafe_segments(bad: str) -> None:
    with pytest.raises(HTTPException) as raised:
        _validate_org_id(bad)
    assert raised.value.status_code == 400


@pytest.mark.parametrize("good", ["team_b-9", "org-x", "01J9XY8ZQ3"])
def test_org_id_accepts_safe_segments(good: str) -> None:
    assert _ORG_ID_RE.match(good)


def test_org_id_strips_surrounding_whitespace() -> None:
    assert _validate_org_id("  org-x  ") == "org-x"
