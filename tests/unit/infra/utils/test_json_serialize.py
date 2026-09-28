"""Unit tests for the shared JSON fallback serializer (cluster plan D11).

The canonical ``json_chunk_default`` moved from ``api/routers/chat/sse.py``
down to ``infra/utils`` because the worker-side remote-turn encoder
(``octop.infra.cluster.remote.encode_chunk``) needs it too — and ``infra``
must never import ``api`` (AGENTS.md §5). These tests pin the canonical
location and the api-side re-export.
"""

from __future__ import annotations

import json

import pytest

from octop.api.routers.chat import sse as chat_sse
from octop.infra.utils.json_serialize import json_chunk_default


class _Pydanticish:
    def model_dump(self) -> dict[str, int]:
        return {"n": 1}


class _LegacyDict:
    def dict(self) -> dict[str, str]:
        return {"s": "x"}


class _Opaque:
    pass


def test_model_dump_objects_serialize() -> None:
    assert json.loads(json.dumps({"o": _Pydanticish()}, default=json_chunk_default)) == {
        "o": {"n": 1}
    }


def test_legacy_dict_objects_serialize() -> None:
    assert json.loads(json.dumps({"o": _LegacyDict()}, default=json_chunk_default)) == {
        "o": {"s": "x"}
    }


def test_unsupported_objects_fall_back_to_repr() -> None:
    value = _Opaque()
    parsed = json.loads(json.dumps({"o": value}, default=json_chunk_default))
    assert parsed["o"] == repr(value)


def test_api_sse_module_reexports_the_infra_implementation() -> None:
    assert chat_sse.json_chunk_default is json_chunk_default


def test_remote_encoder_uses_the_infra_implementation_without_api_import() -> None:
    # The worker side must be able to encode chunks without the api package.
    # (cluster module absent in community builds — nothing to assert there.)
    pytest.importorskip("octop.infra.cluster.remote")
    import sys

    from octop.infra.cluster import remote as agents_remote

    saved = sys.modules.pop("octop.api", None)
    saved_routers = sys.modules.pop("octop.api.routers", None)
    saved_sse = sys.modules.pop("octop.api.routers.chat.sse", None)
    try:
        assert agents_remote.encode_chunk({"o": _Pydanticish()}) == '{"o": {"n": 1}}'
    finally:
        for name, module in (
            ("octop.api", saved),
            ("octop.api.routers", saved_routers),
            ("octop.api.routers.chat.sse", saved_sse),
        ):
            if module is not None:
                sys.modules[name] = module
