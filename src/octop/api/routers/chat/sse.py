"""SSE / WebSocket frame formatting helpers."""

from __future__ import annotations

import json
from typing import Any

# Canonical implementation lives in infra (D11): the worker-side remote-turn
# encoder needs the same fallback and infra must not import api.
from octop.infra.utils.json_serialize import json_chunk_default

__all__ = ["format_sse", "json_chunk_default"]


def format_sse(event: str, data: Any) -> str:
    if isinstance(data, str):
        payload = data
    else:
        try:
            payload = json.dumps(data, ensure_ascii=False, default=json_chunk_default)
        except Exception:
            payload = json.dumps({"type": "error", "message": "chunk not serializable"})
    return f"event: {event}\ndata: {payload}\n\n"
