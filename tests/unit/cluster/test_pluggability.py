"""Pluggability contract tests (cluster plan §8).

The cluster refactoring must stay a **removable module**: a community build
with ``octop/infra/cluster`` and ``octop/api/cluster`` deleted still boots
and serves every community feature. Two complementary guards:

1. ``test_no_top_level_cluster_imports_outside_the_cluster_packages`` —
   static AST scan: outside the two cluster packages, no module may import
   cluster code at import time. Integration points must be lazy (function
   body) so a community interpreter never executes them.
2. ``test_community_boots_with_cluster_modules_blocked`` — the dynamic
   counterpart: purge cluster modules from ``sys.modules`` and block their
   import, then boot a full community server (SQLite) and build the API
   app. The boot must succeed via the ``_StandaloneBackgroundGate``
   fallback, expose the community route surface, and leave no cluster
   tables behind.
"""

from __future__ import annotations

import ast
import contextlib
import sys
from collections.abc import Iterator
from importlib.abc import MetaPathFinder
from pathlib import Path
from typing import Any

from tests.support.app import ensure_control_plane_bound

from octop.api.app import build_app
from octop.infra.server import OctopServer

SRC_OCTOP = Path(__file__).resolve().parents[3] / "src" / "octop"

#: Module prefixes that belong to the pluggable cluster module.
CLUSTER_ROOTS = (
    "octop.infra.cluster",
    "octop.api.cluster",
    "octop.infra.agents.remote",  # historical location; must not come back
)

_CLUSTER_PACKAGES = ("infra/cluster/", "api/cluster/")


def _imported_names(node: ast.stmt) -> Iterator[str]:
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield alias.name
    elif isinstance(node, ast.ImportFrom) and node.module:
        yield node.module


def test_no_top_level_cluster_imports_outside_the_cluster_packages() -> None:
    """Integration points import cluster code lazily, never at module load."""
    offenders: list[str] = []
    for path in sorted(SRC_OCTOP.rglob("*.py")):
        rel = path.relative_to(SRC_OCTOP).as_posix()
        if rel.startswith(_CLUSTER_PACKAGES):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for stmt in tree.body:  # module top level only — nested = lazy
            for name in _imported_names(stmt):
                if any(name == root or name.startswith(root + ".") for root in CLUSTER_ROOTS):
                    offenders.append(f"{rel}: {name}")
    assert offenders == [], (
        "cluster modules must only be imported lazily outside the cluster "
        f"packages (pluggability contract §8): {offenders}"
    )


class _ClusterModuleBlocker(MetaPathFinder):
    """Refuses every import under the cluster roots (simulates removal)."""

    def find_spec(self, fullname: str, path: object = None, target: object = None) -> object:
        if any(fullname == root or fullname.startswith(root + ".") for root in CLUSTER_ROOTS):
            raise ImportError(f"cluster module {fullname!r} blocked (pluggability contract test)")
        return None


def _purge_cluster_modules() -> dict[str, object]:
    saved: dict[str, object] = {}
    for name in list(sys.modules):
        if any(name == root or name.startswith(root + ".") for root in CLUSTER_ROOTS):
            saved[name] = sys.modules.pop(name)
    return saved


async def test_community_boots_with_cluster_modules_blocked(tmp_path: Path) -> None:
    """Full community boot with the cluster module physically unavailable."""

    def all_paths(app: Any) -> set[str]:
        """Route surface via the OpenAPI schema (version-robust: FastAPI
        ≥0.120 keeps included routers in a lazy ``_IncludedRouter`` whose
        paths ``app.routes`` does not flatten)."""
        return set(app.openapi().get("paths", {}).keys())

    saved = _purge_cluster_modules()
    blocker = _ClusterModuleBlocker()
    sys.meta_path.insert(0, blocker)
    srv: OctopServer | None = None
    try:
        srv = OctopServer(home=tmp_path)
        await srv.start()  # boots through the _StandaloneBackgroundGate fallback
        await ensure_control_plane_bound(srv)
        app = build_app(srv)

        paths = all_paths(app)
        assert len(paths) > 100, f"community route surface looks empty: {sorted(paths)[:20]}"
        # Community surface present…
        assert "/api/health" in paths
        assert "/api/health/ready" in paths  # generic readiness stays in core
        assert any(p.startswith("/api/agents") for p in paths)
        assert any(p.startswith("/api/auth") for p in paths)
        # …cluster-only surface absent.
        cluster_paths = {
            "/api/agents/{agent_id}/threads/{thread_id}/turn/stream",
        }
        assert not (cluster_paths & paths), "cluster routes leaked into community app"

        # No cluster tables in the community control plane.
        with srv.services.db.connect() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'octop_turn%'"
            ).fetchall()
        assert rows == [], f"cluster tables created in community mode: {rows}"
    finally:
        sys.meta_path.remove(blocker)
        if srv is not None:
            with contextlib.suppress(Exception):  # teardown must not mask the test
                await srv.stop()
        sys.modules.update(saved)
