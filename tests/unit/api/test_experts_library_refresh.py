"""POST /api/experts/library/refresh — admin-only re-scan without restart."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from octop.api.routers.experts import refresh_expert_library
from octop.infra.errors import ErrorCode, OctopError
from tests.support.app import octop_client


class _FakeCatalog:
    def __init__(self, bundled: int = 3, total: int | None = None) -> None:
        self.refresh_calls = 0
        self._bundled = bundled
        self._total = total if total is not None else bundled

    def refresh(self) -> None:
        self.refresh_calls += 1

    def list_summaries(self, *, include_market_cache: bool = False) -> list[Any]:
        return [object()] * (self._total if include_market_cache else self._bundled)


@pytest.mark.asyncio
async def test_refresh_requires_admin() -> None:
    catalog = _FakeCatalog()
    server = SimpleNamespace(expert_catalog=catalog, subagent_catalog=None)
    with pytest.raises(OctopError) as exc_info:
        await refresh_expert_library(
            user=SimpleNamespace(is_admin=False), server=server
        )
    assert exc_info.value.code is ErrorCode.FORBIDDEN
    assert catalog.refresh_calls == 0  # gate runs before any side effect


@pytest.mark.asyncio
async def test_refresh_rescans_both_catalogs_and_reports_counts() -> None:
    catalog = _FakeCatalog(bundled=3, total=5)
    subagents = SimpleNamespace(
        refresh=lambda: None, _subagents={"a": 1, "b": 2}
    )
    server = SimpleNamespace(expert_catalog=catalog, subagent_catalog=subagents)
    result = await refresh_expert_library(
        user=SimpleNamespace(is_admin=True), server=server
    )
    assert catalog.refresh_calls == 1
    assert result == {"experts": 3, "experts_total": 5, "subagents": 2}


@pytest.mark.asyncio
async def test_refresh_survives_missing_catalog_with_404() -> None:
    with pytest.raises(OctopError) as exc_info:
        await refresh_expert_library(
            user=SimpleNamespace(is_admin=True),
            server=SimpleNamespace(expert_catalog=None, subagent_catalog=None),
        )
    assert exc_info.value.code is ErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_refresh_against_real_server(tmp_path: Path) -> None:
    async with octop_client(tmp_path) as (_client, srv):
        result = await refresh_expert_library(
            user=SimpleNamespace(is_admin=True), server=srv
        )
        assert result["experts"] >= 1
        assert result["experts_total"] >= result["experts"]
        # Catalog remains usable after the re-scan.
        assert srv.expert_catalog.get("general-assistant") is not None
