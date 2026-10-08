"""Unit tests for PublishedExpertRepo and migration 006."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.published_experts import PublishedExpertRepo


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


def test_published_experts_table_exists(db: SqlitePool) -> None:
    with db.connect() as conn:
        names = {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        v = conn.execute("SELECT version FROM _schema_version").fetchone()[0]
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(published_experts)").fetchall()}
    assert "published_experts" in names
    assert v >= 20
    assert "published_expert_id" in cols


def test_published_expert_repo_create_get_list_delete(db: SqlitePool) -> None:
    repo = PublishedExpertRepo(db)
    row = repo.create(
        id="01TESTEXPERT000000000000",
        slug="office-helper",
        name="Office Helper",
        description="Helps with office tasks",
        created_by="user-1",
        source_agent_id="agent-abc",
        icon_name="briefcase",
        color="#336699",
    )
    assert row.slug == "office-helper"
    assert row.pk >= 1
    assert row.name == "Office Helper"
    assert row.source_agent_id == "agent-abc"
    assert row.icon_name == "briefcase"
    assert row.color == "#336699"

    fetched = repo.get(row.id)
    assert fetched is not None
    assert fetched.slug == "office-helper"

    by_slug = repo.get_by_slug("office-helper")
    assert by_slug is not None
    assert by_slug.id == row.id

    # 2026-09-27: the created row is unattributed (org NULL = admin draft).
    # Tenant-facing listings never return drafts; the admin accessor does.
    assert repo.list_visible("cheryfs") == []
    listed = repo.list_all_orgs()
    assert len(listed) == 1
    assert listed[0].id == row.id
    assert listed[0].org_id is None

    repo.delete(row.id)
    assert repo.get(row.id) is None
    assert repo.list_all_orgs() == []


def test_published_expert_repo_unique_slug(db: SqlitePool) -> None:
    repo = PublishedExpertRepo(db)
    repo.create(
        id="01TESTEXPERT000000000001",
        slug="duplicate-slug",
        name="First",
        created_by="user-1",
    )
    with pytest.raises(sqlite3.IntegrityError):
        repo.create(
            id="01TESTEXPERT000000000002",
            slug="duplicate-slug",
            name="Second",
            created_by="user-2",
        )


def test_published_expert_repo_get_by_source_and_update_meta(db: SqlitePool) -> None:
    repo = PublishedExpertRepo(db)
    row = repo.create(
        id="01TESTEXPERT000000000003",
        slug="from-source",
        name="From Source",
        created_by="user-1",
        source_agent_id="agent-xyz",
        icon_name="old",
        color="#111111",
    )
    assert repo.get_by_source_agent_id("agent-xyz") is not None
    assert repo.get_by_source_agent_id("agent-xyz").id == row.id
    assert repo.get_by_source_agent_id("missing") is None

    updated = repo.update_snapshot_meta(
        row.id,
        icon_name="search",
        color="#abcdef",
    )
    assert updated.icon_name == "search"
    assert updated.color == "#abcdef"
    assert updated.updated_at >= row.updated_at
    assert updated.updated_at >= row.created_at


def test_legacy_text_pk_published_experts_keep_public_id(tmp_path: Path) -> None:
    pool = SqlitePool(tmp_path / "octop.db")
    migrations = Path(__file__).resolve().parents[3] / "src/octop/infra/db/migrations"
    with pool.connect() as conn:
        conn.executescript((migrations / "001_initial.sql").read_text())
        conn.executescript((migrations / "005_shared_experts_sso_knowledge.sql").read_text())
        conn.execute(
            "INSERT INTO published_experts("
            "id, slug, name, description, created_by, source_agent_id, "
            "icon_name, color, created_at, updated_at"
            ") VALUES (?, ?, ?, '', ?, NULL, '', '', 1, 1)",
            ("expABC", "office-helper", "Office Helper", "user-1"),
        )
        conn.execute("UPDATE _schema_version SET version = 5")
    run_migrations(pool)
    repo = PublishedExpertRepo(pool)
    row = repo.get("expABC")
    assert row is not None
    assert row.id == "expABC"
    assert row.pk >= 1
    assert row.slug == "office-helper"


# ── Multi-tenant (schema v16) ────────────────────────────────────────────────


def test_multi_tenant_schema_present(db: SqlitePool) -> None:
    with db.connect() as conn:
        tables = {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        user_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        expert_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(published_experts)").fetchall()
        }
        version = conn.execute("SELECT version FROM _schema_version").fetchone()[0]
    assert {
        "orgs",
        "idp_org_bindings",
        "org_memberships",
        "roles",
        "org_role_mappings",
    } <= tables
    assert {"default_org_id", "token_epoch"} <= user_cols
    assert {"org_id", "allowed_roles"} <= expert_cols
    assert version >= 16


def test_list_visible_is_tenant_scoped(db: SqlitePool) -> None:
    """Regression for the C2 leak: one tenant must never see another's rows."""
    repo = PublishedExpertRepo(db)
    repo.create(
        id="01TESTEXPERT000000000101",
        slug="tenant-a-expert",
        name="A Expert",
        created_by="user-a",
        org_id="cheryfs",
    )
    repo.create(
        id="01TESTEXPERT000000000102",
        slug="tenant-b-expert",
        name="B Expert",
        created_by="user-b",
        org_id="chery-dealer-001",
    )
    repo.create(
        id="01TESTEXPERT000000000103",
        slug="platform-expert",
        name="Platform Expert",
        created_by="user-p",
        # 2026-09-27: platform sharing is now explicit ('*'), not NULL.
        org_id="*",
    )

    assert {r.name for r in repo.list_visible("cheryfs")} == {"A Expert", "Platform Expert"}
    assert {r.name for r in repo.list_visible("chery-dealer-001")} == {
        "B Expert",
        "Platform Expert",
    }
    # A tenant that owns nothing still sees the explicit platform share.
    assert {r.name for r in repo.list_visible("chery-dealer-002")} == {"Platform Expert"}
    # No tenant context must NOT widen to every tenant.
    assert {r.name for r in repo.list_visible(None)} == {"Platform Expert"}
    # Unattributed drafts are invisible to every tenant scope.
    repo.create(
        id="01TESTEXPERT000000000104",
        slug="draft-expert",
        name="Draft Expert",
        created_by="user-p",
    )
    assert all(
        "Draft Expert" not in {r.name for r in repo.list_visible(org)}
        for org in ["cheryfs", "chery-dealer-001", "chery-dealer-002", None]
    )
    assert {r.name for r in repo.list_all_orgs()} >= {"Draft Expert"}
    # The explicit cross-tenant accessor still sees everything (now 4 rows).
    assert len(repo.list_all_orgs()) == 4

    assert repo.get_by_slug("tenant-b-expert") is not None


def test_published_expert_round_trips_org_and_allowed_roles(db: SqlitePool) -> None:
    repo = PublishedExpertRepo(db)
    row = repo.create(
        id="01TESTEXPERT000000000104",
        slug="scoped-expert",
        name="Scoped Expert",
        created_by="user-a",
        org_id="chery-dealer-003",
        allowed_roles=["expert_manager", "intake_submitter"],
    )
    assert row.org_id == "chery-dealer-003"
    assert row.allowed_roles == ["expert_manager", "intake_submitter"]
    fetched = repo.get(row.id)
    assert fetched is not None
    assert fetched.allowed_roles == ["expert_manager", "intake_submitter"]


def _seed_orgs(db: SqlitePool) -> None:
    with db.connect() as conn:
        for org_id, name in (("cheryfs", "Chery FS"), ("chery-dealer-001", "Dealer 001")):
            conn.execute(
                "INSERT INTO orgs(org_id, name, created_at, updated_at) VALUES (?, ?, 1, 1)",
                (org_id, name),
            )


def test_idp_org_bindings_accepts_namespaced_duplicate_values(db: SqlitePool) -> None:
    """The same upstream value from two platforms must map to two different orgs."""
    _seed_orgs(db)
    with db.connect() as conn:
        for alias, org_id in (("summer-cloud", "cheryfs"), ("new-platform", "chery-dealer-001")):
            conn.execute(
                "INSERT INTO idp_org_bindings("
                "idp_alias, match_type, match_value, org_id, created_at, updated_at"
                ") VALUES (?, 'claim', '1', ?, 1, 1)",
                (alias, org_id),
            )
        rows = conn.execute(
            "SELECT idp_alias, org_id FROM idp_org_bindings WHERE match_value = '1' "
            "ORDER BY idp_alias"
        ).fetchall()
    assert [(r["idp_alias"], r["org_id"]) for r in rows] == [
        ("new-platform", "chery-dealer-001"),
        ("summer-cloud", "cheryfs"),
    ]


def test_idp_org_bindings_rejects_second_static_binding_for_same_idp(
    db: SqlitePool,
) -> None:
    """``match_value`` is NOT NULL so this pair cannot silently duplicate.

    A nullable column would let both rows in (SQL treats NULLs as distinct),
    and the resolution chain would then hit an ambiguous static binding.
    """
    _seed_orgs(db)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO idp_org_bindings("
            "idp_alias, match_type, match_value, org_id, created_at, updated_at"
            ") VALUES ('summer-cloud', 'static', '', 'cheryfs', 1, 1)"
        )
    with pytest.raises(sqlite3.IntegrityError), db.connect() as conn:
        conn.execute(
            "INSERT INTO idp_org_bindings("
            "idp_alias, match_type, match_value, org_id, created_at, updated_at"
            ") VALUES ('summer-cloud', 'static', '', 'chery-dealer-001', 1, 1)"
        )


def test_idp_org_bindings_enforces_match_value_shape(db: SqlitePool) -> None:
    _seed_orgs(db)
    # A 'claim' binding without a value could never match a token claim.
    with pytest.raises(sqlite3.IntegrityError), db.connect() as conn:
        conn.execute(
            "INSERT INTO idp_org_bindings("
            "idp_alias, match_type, match_value, org_id, created_at, updated_at"
            ") VALUES ('summer-cloud', 'claim', '', 'cheryfs', 1, 1)"
        )
    # An unknown match_type is rejected instead of being ignored at resolve time.
    with pytest.raises(sqlite3.IntegrityError), db.connect() as conn:
        conn.execute(
            "INSERT INTO idp_org_bindings("
            "idp_alias, match_type, match_value, org_id, created_at, updated_at"
            ") VALUES ('summer-cloud', 'guess', '1', 'cheryfs', 1, 1)"
        )


def test_org_memberships_rejects_unknown_status(db: SqlitePool) -> None:
    _seed_orgs(db)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) "
            "VALUES ('member-1', 'x', 'user', 1)"
        )
        user_id = conn.execute("SELECT id FROM users WHERE username = 'member-1'").fetchone()[0]
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO org_memberships(user_id, org_id, status, created_at, updated_at) "
            "VALUES (?, 'cheryfs', 'pending', 1, 1)",
            (user_id,),
        )
    with pytest.raises(sqlite3.IntegrityError), db.connect() as conn:
        conn.execute(
            "INSERT INTO org_memberships(user_id, org_id, status, created_at, updated_at) "
            "VALUES (?, 'chery-dealer-001', 'maybe', 1, 1)",
            (user_id,),
        )


def test_published_expert_defaults_persisted_and_roundtrip(db: SqlitePool) -> None:
    """Migration 032 columns: publisher defaults round-trip through the repo."""
    from octop.infra.db.repos.published_experts import _parse_id_list

    repo = PublishedExpertRepo(db)
    row = repo.create(
        id="01TESTDEFAULTS00000000000",
        slug="intake-assistant",
        name="Intake Assistant",
        created_by="user-7",
        source_agent_id="agent-src",
        default_knowledge_base_ids=["kb-1", "kb-2"],
        default_mcp_servers=["custom__p__mcp-summer-cmc"],
        default_skill_package_ids=["pkg-9"],
    )
    assert row.default_knowledge_base_ids == ["kb-1", "kb-2"]
    assert row.default_mcp_servers == ["custom__p__mcp-summer-cmc"]
    assert row.default_skill_package_ids == ["pkg-9"]

    fetched = repo.get(row.id)
    assert fetched is not None
    assert fetched.default_knowledge_base_ids == ["kb-1", "kb-2"]
    assert fetched.default_mcp_servers == ["custom__p__mcp-summer-cmc"]
    assert fetched.default_skill_package_ids == ["pkg-9"]

    updated = repo.set_session_defaults(
        row.id,
        knowledge_base_ids=["kb-3"],
        mcp_servers=[],
        skill_package_ids=["pkg-1", "pkg-2"],
    )
    assert updated.default_knowledge_base_ids == ["kb-3"]
    assert updated.default_mcp_servers == []
    assert updated.default_skill_package_ids == ["pkg-1", "pkg-2"]

    # create() without defaults reads back as empty lists, never None.
    bare = repo.create(
        id="01TESTDEFAULTS00000000001",
        slug="bare-template",
        name="Bare Template",
        created_by="user-7",
    )
    assert bare.default_knowledge_base_ids == []
    assert bare.default_mcp_servers == []
    assert bare.default_skill_package_ids == []

    # Defensive parsing: malformed JSON and non-list shapes degrade to [].
    assert _parse_id_list(None) == []
    assert _parse_id_list("") == []
    assert _parse_id_list("{not-json") == []
    assert _parse_id_list('{"a":1}') == []
    assert _parse_id_list('["a","b"]') == ["a", "b"]
    assert _parse_id_list(["x", None, 3]) == ["x", "3"]


def test_agent_session_defaults_parsed_from_row() -> None:
    """_agent_session_defaults reads the agent profile columns defensively."""
    from types import SimpleNamespace

    from octop.infra.agents.experts.published_creation import _agent_session_defaults

    registry = SimpleNamespace(
        get_row=lambda agent_id: SimpleNamespace(
            knowledge_base_ids='["kb-1"]',
            mcp_servers='["custom__p__mcp-summer-cmc"]',
            skill_package_ids='["pkg-7"]',
        ),
    )
    kb, mcp, pkgs = _agent_session_defaults(registry, "agent-1")  # type: ignore[arg-type]
    assert kb == ["kb-1"]
    assert mcp == ["custom__p__mcp-summer-cmc"]
    assert pkgs == ["pkg-7"]

    missing = SimpleNamespace(get_row=lambda agent_id: None)
    assert _agent_session_defaults(missing, "agent-x") == ([], [], [])  # type: ignore[arg-type]

    malformed = SimpleNamespace(
        get_row=lambda agent_id: SimpleNamespace(
            knowledge_base_ids="{broken",
            mcp_servers=None,
            skill_package_ids=["already", "list"],
        ),
    )
    kb2, mcp2, pkgs2 = _agent_session_defaults(malformed, "agent-1")  # type: ignore[arg-type]
    assert kb2 == []
    assert mcp2 == []
    assert pkgs2 == ["already", "list"]
