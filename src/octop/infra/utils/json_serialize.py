"""JSON fallback serializer shared by api frame formatters and infra encoders.

Lives in ``infra`` (not ``api``) because the remote-turn event encoder
(``octop.infra.cluster.remote.encode_chunk``) needs the same fallback for
worker-side chunk persistence — a layering violation the other way
(cluster plan D11). The api side imports it from here.
"""

from __future__ import annotations

from typing import Any

__all__ = ["json_chunk_default"]


def json_chunk_default(obj: Any) -> Any:
    """Fallback serializer for types json.dumps can't handle natively."""
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    if hasattr(obj, "dict"):
        try:
            return obj.dict()
        except Exception:
            pass
    return repr(obj)
