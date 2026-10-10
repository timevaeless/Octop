"""Tests for the OCR bundled-plugin bridge (T4: all logic in ocr_bridge)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from octop.infra.agents.plugins import ocr_bridge
from octop.infra.agents.plugins.ocr_bridge import (
    bind_ocr_server,
    ocr_recognize,
    register_ocr_tool,
)


class _FakeWorkspace:
    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files = files or {}

    def exists(self, path: str) -> bool:
        return path in self.files

    def materialize_local(self, path: str):
        return None  # force the download_bytes fallback (temp-file path)

    def download_bytes(self, path: str):
        return self.files.get(path)


@pytest.fixture(autouse=True)
def _reset_binding():
    yield
    bind_ocr_server(None)


def _stub_resolution(monkeypatch: pytest.MonkeyPatch, workspace) -> None:
    monkeypatch.setattr(ocr_bridge, "_thread_id", lambda: "t-1", raising=False)
    monkeypatch.setattr(
        ocr_bridge, "_workspace_for_thread", lambda tid: workspace, raising=False
    )


# ---------------------------------------------------------------- path guard


@pytest.mark.asyncio
async def test_rejects_absolute_path():
    result = await ocr_recognize("/etc/passwd")
    assert result.startswith("OCR 失败")
    assert "相对路径" in result


@pytest.mark.asyncio
async def test_rejects_traversal():
    result = await ocr_recognize("inbound/../../etc/passwd")
    assert result.startswith("OCR 失败")
    assert ".." in result


@pytest.mark.asyncio
async def test_rejects_unsupported_suffix():
    result = await ocr_recognize("inbound/doc.docx")
    assert "仅支持" in result


# ------------------------------------------------------------ happy path


@pytest.mark.asyncio
async def test_recognizes_image_via_download_fallback(monkeypatch):
    calls: list[str] = []

    def fake_extractor(path):
        calls.append(str(path))
        return "  你好世界\n"

    _stub_resolution(monkeypatch, _FakeWorkspace({"inbound/a.png": b"png-bytes"}))
    monkeypatch.setattr(
        ocr_bridge, "_build_extractor", lambda: fake_extractor, raising=False
    )

    result = await ocr_recognize("inbound/a.png")

    assert result == "你好世界"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_empty_transcription_message(monkeypatch):
    _stub_resolution(monkeypatch, _FakeWorkspace({"inbound/a.png": b"png-bytes"}))
    monkeypatch.setattr(
        ocr_bridge, "_build_extractor", lambda: lambda path: "  \n", raising=False
    )

    result = await ocr_recognize("inbound/a.png")

    assert result.startswith("OCR 完成")
    assert "未识别到文本" in result


@pytest.mark.asyncio
async def test_long_text_truncated(monkeypatch):
    _stub_resolution(monkeypatch, _FakeWorkspace({"inbound/a.pdf": b"pdf"}))
    monkeypatch.setattr(
        ocr_bridge,
        "_build_extractor",
        lambda: lambda path: "字" * (ocr_bridge._MAX_TEXT_CHARS + 10),
        raising=False,
    )

    result = await ocr_recognize("inbound/a.pdf")

    assert len(result) == ocr_bridge._MAX_TEXT_CHARS + len("\n…（内容过长已截断）")


# ------------------------------------------------- plan C: engine vs enable


def test_plan_c_enable_flag_is_forced_true(monkeypatch):
    """KB enabled=false must not gate the tool; engine config keys stay shared."""
    seen: dict[str, object] = {}
    services = SimpleNamespace(
        settings_repo=SimpleNamespace(
            get=lambda key, *a, **k: seen.setdefault(key, {"knowledge_ocr_enabled": "false"}.get(key))
        ),
        provider_repo=SimpleNamespace(),
    )
    import octop.infra.knowledge.ocr as kb_ocr

    monkeypatch.setattr(kb_ocr, "local_ocr_deps_available", lambda: True)
    captured = {}

    def fake_require(services_ns):
        captured["enabled"] = services_ns.settings_repo.get("knowledge_ocr_enabled")
        captured["backend"] = services_ns.settings_repo.get("knowledge_ocr_backend")
        return object()

    monkeypatch.setattr(kb_ocr, "require_ocr_extractor", fake_require)
    monkeypatch.setattr(
        ocr_bridge, "_runtime", lambda: (services, SimpleNamespace()), raising=False
    )

    extractor = ocr_bridge._build_extractor()

    assert extractor is not None
    assert captured["enabled"] == "true"  # forced: plugin switch owns availability
    assert captured["backend"] is None  # engine key passes through unchanged


def test_plan_c_local_deps_missing_maps_to_friendly_error(monkeypatch):
    import octop.infra.knowledge.ocr as kb_ocr

    services = SimpleNamespace(
        settings_repo=SimpleNamespace(get=lambda key, *a, **k: None),
        provider_repo=SimpleNamespace(),
    )
    monkeypatch.setattr(kb_ocr, "local_ocr_deps_available", lambda: False)
    monkeypatch.setattr(
        ocr_bridge, "_runtime", lambda: (services, SimpleNamespace()), raising=False
    )

    with pytest.raises(RuntimeError) as exc_info:
        ocr_bridge._build_extractor()

    assert "本地 OCR 组件未安装" in str(exc_info.value)


# --------------------------------------------------------- runtime binding


@pytest.mark.asyncio
async def test_unbound_server_reports_not_initialized(monkeypatch):
    bind_ocr_server(None)
    monkeypatch.setattr(ocr_bridge, "_thread_id", lambda: "t-1", raising=False)
    result = await ocr_recognize("inbound/a.png")
    assert "未初始化" in result


# ------------------------------------------------------------- registration


def test_register_ocr_tool():
    registered: dict[str, object] = {}

    class _Ctx:
        def tool(self, name, fn, *, description=""):
            registered["name"] = name
            registered["fn"] = fn
            registered["description"] = description

    register_ocr_tool(_Ctx())

    assert registered["name"] == "ocr_recognize"
    assert registered["fn"] is ocr_recognize
    assert "工作区相对路径" in str(registered["description"])


# --------------------------------------------------- RLS scope re-bind


def _server_with_repos(thread_row, agent_row, *, with_user_repos=True):
    repos = SimpleNamespace(
        thread_repo=SimpleNamespace(get=lambda tid: thread_row),
        agent_repo=SimpleNamespace(get=lambda aid: agent_row),
    )
    if with_user_repos:
        repos.user_repo = SimpleNamespace(
            get=lambda uid: SimpleNamespace(
                id=uid, default_org_id="org-1", role="admin"
            )
        )
        repos.org_repo = SimpleNamespace()
    registry = SimpleNamespace(_backend_workspace_for_row=lambda row: "workspace-ok")
    server = SimpleNamespace(
        services=SimpleNamespace(repos=repos),
        app_runtime=SimpleNamespace(agent_registry=registry),
    )
    bind_ocr_server(server)


def test_workspace_resolution_binds_owner_scope(monkeypatch):
    import octop.infra.users.context as users_context

    resolved: list[int] = []

    def fake_resolve(user_repo, org_repo, uid):
        resolved.append(uid)
        return SimpleNamespace(user_id=uid, org_id="org-1", is_admin=True)

    monkeypatch.setattr(users_context, "resolve_authz_context", fake_resolve)
    _server_with_repos(
        SimpleNamespace(agent_id="a1", user_id=7),
        SimpleNamespace(agent_id="a1"),
    )

    assert ocr_bridge._workspace_for_thread("t-1") == "workspace-ok"
    assert resolved == [7]


def test_workspace_resolution_tolerates_missing_user_repos(monkeypatch):
    _server_with_repos(
        SimpleNamespace(agent_id="a1", user_id=7),
        SimpleNamespace(agent_id="a1"),
        with_user_repos=False,
    )

    # user/org repos absent (partial test doubles): binding skipped, lookup proceeds
    assert ocr_bridge._workspace_for_thread("t-1") == "workspace-ok"
