"""Mount-name translation for published-expert installs (Octop extension).

A template stamps the **publisher's** ``mcp_servers`` list (see
``published_creation._agent_session_defaults``). For most connector families
that name is user-independent — a built-in kind mounts as
``kind__<instance>``, a catalog attachment as ``custom__<parent>__mcp-<svc>``
where the parent row belongs to the *tenant*, not to a person.

A **manually configured** custom MCP server is the exception: it lives inside
the owner's single ``custom-mcp`` document and mounts as
``custom__<parent_instance_id>__<server_name>`` — or as the bare
``<server_name>`` in the owner's own scope. Neither form resolves for another
user, so a template published from such an agent carries a name the installer
can never bind:

    publisher (admin)  → mcp_servers: ["wren-summer"]
    installer (tenant) → visible:     ["custom__01M52GPZ…__wren-summer"]

Without translation the install drawer pre-fills ``wren-summer`` verbatim and
``AgentManager.validate_mcp_servers`` rejects the whole install with
``CONNECTOR_NOT_BOUND`` — the tenant cannot install a template that was
perfectly valid for its author (verified live 2026-10-18).

Translation is deliberately narrow: a name is rewritten only when it is not
already visible AND exactly ONE visible custom-server mount carries the same
server name. Ambiguity (two shared parents exposing ``wren-summer``) keeps the
original value, so the caller's existing fail-fast / soft-filter behaviour
decides — this module never invents a connector.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence, Set

__all__ = ["rewrite_installer_mount_names", "custom_server_name"]

_CUSTOM_PREFIX = "custom__"


def custom_server_name(value: str) -> str | None:
    """The server name a mount key points at, or ``None`` for non-custom keys.

    ``custom__<parent>__<server>`` → ``<server>``; a bare name (the owner's own
    scope) → itself; ``custom:<parent>:<server>`` (the listing API's synthetic
    id) → ``<server>``. Built-in ``kind__<instance>`` names return ``None`` so
    they are never rewritten by suffix.
    """
    raw = value.strip()
    if not raw:
        return None
    if raw.startswith(_CUSTOM_PREFIX):
        _, _, server = raw[len(_CUSTOM_PREFIX):].rpartition("__")
        return server or None
    if raw.startswith("custom:"):
        server = raw.rsplit(":", 1)[-1]
        return server or None
    if "__" in raw:  # kind__<instance> — own family, not a custom child
        return None
    return raw


def rewrite_installer_mount_names(
    values: Sequence[str],
    visible: Set[str] | Iterable[str],
) -> list[str]:
    """Map publisher-scoped custom MCP names onto the installer's mount keys.

    Returns ``values`` in order, de-duplicated, with each name that is absent
    from *visible* but unambiguously matches exactly one visible custom mount
    of the same server name replaced by that mount key. Everything else is
    passed through untouched.
    """
    seen_visible = set(visible)
    by_server: dict[str, list[str]] = {}
    for name in seen_visible:
        if not name.startswith(_CUSTOM_PREFIX):
            continue
        server = custom_server_name(name)
        if server is not None:
            by_server.setdefault(server, []).append(name)

    out: list[str] = []
    for value in values:
        resolved = value
        if value not in seen_visible:
            server = custom_server_name(value)
            candidates = by_server.get(server, []) if server is not None else []
            if len(candidates) == 1:
                resolved = candidates[0]
        if resolved not in out:
            out.append(resolved)
    return out
