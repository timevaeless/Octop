"""Audit-log tenant dimension tests (migration 024, T2-6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.audit import AuditRepo


@pytest.fixture
def repo(tmp_path: Path) -> AuditRepo:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return AuditRepo(pool)


def test_write_persists_org_id(repo: AuditRepo) -> None:
    repo.write(actor="alice", action="connector.instance.create", target="c1", org_id="org-a")

    (row,) = repo.query()
    assert row.org_id == "org-a"


def test_write_without_org_id_stays_null(repo: AuditRepo) -> None:
    repo.system_event("cron.run_ok", target="job-1")

    (row,) = repo.query()
    assert row.org_id is None


def test_query_filters_by_org_id(repo: AuditRepo) -> None:
    repo.write(actor="alice", action="a.one", org_id="org-a")
    repo.write(actor="bob", action="a.two", org_id="org-b")
    repo.system_event("a.system")

    org_a = repo.query(org_id="org-a")
    assert [r.action for r in org_a] == ["a.one"]

    org_b = repo.query(org_id="org-b")
    assert [r.action for r in org_b] == ["a.two"]

    everything = repo.query()
    assert len(everything) == 3


def test_empty_string_org_id_is_normalized_to_null(repo: AuditRepo) -> None:
    repo.write(actor="alice", action="a.one", org_id="")

    (row,) = repo.query()
    assert row.org_id is None


def test_convenience_events_pass_org_through(repo: AuditRepo) -> None:
    repo.user_event("alice", "auth.login", org_id="org-a")
    repo.admin_event("x.y", org_id="org-b")

    rows = repo.query()
    assert {r.org_id for r in rows} == {"org-a", "org-b"}
