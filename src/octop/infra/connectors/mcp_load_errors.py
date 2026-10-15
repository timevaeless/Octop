"""Capture MCP tool-load failure reasons without touching octop-harness.

The harness ``aload_mcp_tools`` swallows per-server load exceptions (logs a
warning, continues with zero tools), so the HTTP status / response body that
explain *why* a server was rejected (e.g. mcp-hub tool-ACL ``deny-all``) never
reach Octop or the frontend.

T4 pluggable patch (same isolation strategy as
``octop.infra.agents.harness_resume_patch``): wrap
``octop_harness.mcp.MultiServerMCPClient.get_tools`` to record the failure
reason per server name before re-raising, letting the harness's own
``except`` behave verbatim. Callers (AgentManager) read the recorded reason
right after a load comes back empty and attach it to the
``CONNECTOR_MCP_LOAD_FAILED`` error shown on the frontend.

Deleting this module together with its ``install()`` call restores upstream
behaviour verbatim (reasons simply stay unrecorded).
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

_REASON_TTL_SECONDS = 600
_MAX_RECORDED = 64
_BODY_LIMIT = 200

_reasons: OrderedDict[str, tuple[float, str]] = OrderedDict()
_installed = False


def _iter_leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for inner in exc.exceptions:
            leaves.extend(_iter_leaves(inner))
        return leaves
    return [exc]


def extract_reason(exc: BaseException) -> str:
    """Human-readable one-line reason, preferring an HTTP status + body."""
    for leaf in _iter_leaves(exc):
        response = getattr(leaf, "response", None)
        if response is None:
            continue
        status = getattr(response, "status_code", "?")
        try:
            body = (response.text or "").strip()
        except Exception:  # noqa: BLE001 - diagnostics must never raise
            body = ""
        if body:
            return f"HTTP {status}: {body[:_BODY_LIMIT]}"
        return f"HTTP {status}"
    text = f"{type(exc).__name__}: {exc}"
    return text[:_BODY_LIMIT]


def record_failure(server_name: str, exc: BaseException) -> None:
    reason = extract_reason(exc)
    _reasons[server_name] = (time.monotonic(), reason)
    _reasons.move_to_end(server_name)
    while len(_reasons) > _MAX_RECORDED:
        _reasons.popitem(last=False)
    logger.warning("MCP load failure recorded server=%s reason=%s", server_name, reason)


def recorded_reason(server_name: str) -> str | None:
    """Last recorded failure reason for this server, if recent enough."""
    entry = _reasons.get(server_name)
    if entry is None:
        return None
    seen_at, reason = entry
    if time.monotonic() - seen_at > _REASON_TTL_SECONDS:
        return None
    return reason


def _server_names(client: Any) -> str:
    connections = getattr(client, "connections", None)
    if isinstance(connections, dict) and connections:
        return ", ".join(str(n) for n in connections)
    return "<unknown>"


def install() -> bool:
    """Wrap the harness client so load failures are recorded. Idempotent."""
    global _installed
    if _installed:
        return True
    try:
        import octop_harness.mcp as harness_mcp
    except ImportError:  # pragma: no cover - harness always present here
        return False
    base = getattr(harness_mcp, "MultiServerMCPClient", None)
    if base is None or getattr(base, "__module__", "") == __name__:
        return False

    class _RecordingClient(base):  # type: ignore[misc, valid-type]
        async def get_tools(self) -> list[Any]:
            try:
                return await super().get_tools()
            except BaseException as exc:  # noqa: BLE001 - record, then re-raise
                record_failure(_server_names(self), exc)
                raise

    _RecordingClient.__module__ = __name__
    harness_mcp.MultiServerMCPClient = _RecordingClient
    _installed = True
    logger.info("MCP load-error recorder installed on octop_harness.mcp")
    return True


__all__ = ["extract_reason", "install", "record_failure", "recorded_reason"]
