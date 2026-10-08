"""Owner-default-org backfill for knowledge_bases and connectors (P1-4).

The 019 migration backfilled only ``agents``. Rows in the other two resource
tables kept ``org_id IS NULL`` — which (pre-2026-09-27) the RLS policy and
``shared_resource_visible`` both read as *platform-shared*, silently widening
existing shared resources to every tenant after an upgrade.

Since the three-state attribution refactor (2026-09-27) an explicit
``org_id IS NULL`` means *unattributed* (admin-only), so the backfill is
one-shot: it runs only on the startup that actually adds the ``org_id``
column (see ``ensure_multi_tenant_schema``), never on later boots.
"""

from pathlib import Path

from octop.infra.db.migrate import _ensure_resource_acl_schema, run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.connectors import ConnectorRepo
from octop.infra.db.repos.knowledge import KnowledgeRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.tenancy_schema import (
    backfill_resource_org_ownership,
    ensure_multi_tenant_schema,
)

ORG = "org-backfill"


def _seed(tmp_path: Path) -> tuple[SqlitePool, int, int]:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    users = UserRepo(pool)
    tenant_user = users.create(username="tenant-user", password_hash="h", role="user")
    orgless_admin = users.create(username="orgless-admin", password_hash="h", role="admin")
    with pool.connect() as conn:
        conn.execute(
            "UPDATE users SET default_org_id = ? WHERE username = 'tenant-user'", (ORG,)
        )
    return pool, tenant_user, orgless_admin


def test_backfill_assigns_owner_org_to_kb_and_connectors(tmp_path: Path) -> None:
    """One-shot backfill: legacy NULL rows are adopted when the column lands."""
    pool, tenant_user, orgless_admin = _seed(tmp_path)
    kb_repo = KnowledgeRepo(pool)
    kb = kb_repo.create_base(owner_user_id=tenant_user, name="Docs", shared=True)
    connector_repo = ConnectorRepo(pool)
    instance_id = connector_repo.create(
        instance_id="inst-backfill-1",
        user_id=tenant_user,
        kind="custom_mcp",
        display_name="D",
        mcp_server_name="mcp_d",
        org_id=None,
    )
    platform_kb = kb_repo.create_base(
        owner_user_id=orgless_admin, name="Platform", shared=True
    )

    backfill_resource_org_ownership(pool, ["knowledge_bases", "connectors"])

    assert kb_repo.get_base(kb.id) is not None
    assert kb_repo.get_base(kb.id).org_id == ORG
    adopted = connector_repo.get(instance_id)
    assert adopted is not None
    assert adopted.org_id == ORG
    # An orgless owner has no default org: the row stays unattributed on
    # purpose (org-less admins publish platform rows intentionally).
    assert kb_repo.get_base(platform_kb.id) is not None
    assert kb_repo.get_base(platform_kb.id).org_id is None


def test_backfill_is_one_shot_later_boots_keep_explicit_null(tmp_path: Path) -> None:
    """Regression (review 2026-10-02 P0-2): the every-boot backfill used to
    re-adopt org_id=NULL rows the three-state model treats as authoritative
    (NULL = unattributed, admin-only) — one restart silently widened them to
    the owner's tenant. Schema-ensure calls on a database that already has
    the column must not touch NULL rows."""
    pool, tenant_user, _ = _seed(tmp_path)
    kb_repo = KnowledgeRepo(pool)
    kb = kb_repo.create_base(owner_user_id=tenant_user, name="Docs", shared=True)
    connector_repo = ConnectorRepo(pool)
    instance_id = connector_repo.create(
        instance_id="inst-backfill-2",
        user_id=tenant_user,
        kind="custom_mcp",
        display_name="D",
        mcp_server_name="mcp_d2",
        org_id=None,
    )

    # The every-boot tail helpers (both idempotent re-apply paths).
    _ensure_resource_acl_schema(pool)
    ensure_multi_tenant_schema(pool)

    assert kb_repo.get_base(kb.id).org_id is None
    adopted = connector_repo.get(instance_id)
    assert adopted is not None
    assert adopted.org_id is None
