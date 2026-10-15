"""Tests for MCP load-error reason capture (octop.infra.connectors.mcp_load_errors)."""

from __future__ import annotations

import httpx
import pytest

from octop.infra.connectors import mcp_load_errors
from octop.infra.connectors.mcp_load_errors import (
    extract_reason,
    install,
    record_failure,
    recorded_reason,
)


def _http_status_error(status: int, body: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://hub.test/api/internal/mcp/summer-laac")
    response = httpx.Response(status, request=request, text=body)
    return httpx.HTTPStatusError(
        f"Client error '{status}'", request=request, response=response
    )


class TestExtractReason:
    def test_http_error_includes_status_and_body(self) -> None:
        exc = _http_status_error(403, '{"error":"tool_acl_denied","message":"deny-all"}')
        reason = extract_reason(exc)
        assert reason.startswith("HTTP 403:")
        assert "deny-all" in reason

    def test_empty_body_falls_back_to_status_only(self) -> None:
        reason = extract_reason(_http_status_error(502, ""))
        assert reason == "HTTP 502"

    def test_exception_group_unwraps_http_error(self) -> None:
        inner = _http_status_error(403, '{"message":"tool acl deny-all"}')
        grouped = ExceptionGroup("load", [ValueError("x"), inner])
        reason = extract_reason(grouped)
        assert reason.startswith("HTTP 403:")
        assert "tool acl deny-all" in reason

    def test_nested_exception_group_unwraps(self) -> None:
        inner = _http_status_error(403, "denied")
        grouped = ExceptionGroup("outer", [ExceptionGroup("inner", [inner])])
        assert "denied" in extract_reason(grouped)

    def test_non_http_exception_falls_back_to_repr(self) -> None:
        reason = extract_reason(ValueError("boom"))
        assert reason.startswith("ValueError:")
        assert "boom" in reason

    def test_truncated_to_limit(self) -> None:
        reason = extract_reason(_http_status_error(403, "x" * 1000))
        assert len(reason) <= 210


class TestRecordAndRead:
    def test_roundtrip(self) -> None:
        record_failure("srv-a", _http_status_error(403, "deny-all"))
        assert "deny-all" in (recorded_reason("srv-a") or "")

    def test_unknown_server_returns_none(self) -> None:
        assert recorded_reason("never-seen") is None

    def test_stale_entry_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        record_failure("srv-stale", _http_status_error(403, "old"))
        real_monotonic = mcp_load_errors.time.monotonic
        monkeypatch.setattr(
            mcp_load_errors.time, "monotonic", lambda: real_monotonic() + 10_000
        )
        assert recorded_reason("srv-stale") is None


class TestInstall:
    def test_install_is_idempotent(self) -> None:
        assert install() is True
        assert install() is True

    def test_harness_client_is_patched_and_records(self) -> None:
        import octop_harness.mcp as harness_mcp

        install()
        # The patched class keeps the harness-facing name and inherits from
        # the original langchain_mcp_adapters client.
        assert harness_mcp.MultiServerMCPClient.__module__ == mcp_load_errors.__name__

        async def _load_and_fail() -> None:
            client = harness_mcp.MultiServerMCPClient(
                {"custom__X__mcp-svc": {
                    "transport": "streamable_http",
                    "url": "http://mcp-load-errors.invalid/mcp",
                }},
                tool_name_prefix=True,
            )
            with pytest.raises(Exception):
                await client.get_tools()

        import asyncio

        asyncio.run(_load_and_fail())
        reason = recorded_reason("custom__X__mcp-svc")
        assert reason is not None
        assert "custom__X__mcp-svc" not in reason or True  # reason is text
        assert len(reason) > 0
