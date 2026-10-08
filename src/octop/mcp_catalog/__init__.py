"""MCP catalog — tool definitions as the single source of truth.

Per the §8 division of labor (docs/mcp-identity-passthrough-design.md):
this package owns the full lifecycle of MCP tool definitions —
onboarding business systems, importing OpenAPI, and publishing the derived
configs (ingress / mcp-server plugin / ext-auth matchRule) into nacos.

Isolation contract: everything lives inside this package; community-owned
files are never modified except the two-line router mount in
``octop/api/app.py``. The repo attaches lazily via ``RepoBundle.db`` so
``infra/db/services.py`` stays untouched as well.
"""
