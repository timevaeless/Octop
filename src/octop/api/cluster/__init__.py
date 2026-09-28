"""Cluster-only API surface (D3 continuity).

Pluggability contract (cluster plan §8): everything here is mounted /
imported only when ``execution_mode == "remote"``. The community edition
never imports this package — the WS subscribe path reaches the relay
through a lazy, executor-guarded import, and the SSE attach router is
conditionally mounted in ``api/app.py``.
"""

from octop.api.cluster.routes import router

__all__ = ["router"]
