"""Workspace-aware OCR tool bridge for the bundled ``ocr`` plugin.

Exposes the knowledge-base OCR chain (``octop.infra.knowledge.ocr``) as an
agent-callable tool without entangling it with the knowledge-base product
surface.

T4 pluggability: all logic lives in this module plus the ``bundled/ocr``
plugin directory. Upstream files carry a single two-line bind hook
(``server.py``). Removing the plugin directory, this module and its hook
lines restores the community tree.

Configuration semantics (Plan C, agreed 2026-10-09):

* engine selection follows the knowledge-base OCR settings keys
  (``knowledge_ocr_backend`` / ``knowledge_ocr_model`` /
  ``knowledge_ocr_provider_id``) — one engine configuration shared by both
  consumers, so admins tune the model in one place;
* availability is decoupled: the KB ``knowledge_ocr_enabled`` flag does NOT
  gate this tool (the plugin's own global enable switch does). The settings
  wrapper below forces the enabled read so ``require_ocr_extractor``
  performs only engine checks.

Runtime resolution: the tool runs inside a chat turn, so
``langgraph.config.get_config()`` exposes ``configurable.thread_id``; the
thread row maps to the agent row and the per-agent ``BackendWorkspace``
(local or S3-backed), which materializes the requested file. Tool fns run on
the harness worker loop where the request's RLS tenant scope is absent; the
bridge re-binds the thread owner's scope from storage before the (RLS-guarded)
agent lookup, so tenant isolation still holds.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger(__name__)

_SUPPORTED_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".pdf"})
_MAX_TEXT_CHARS = 20_000

# Bound once from server.py after AppRuntime exists; read lazily per call so
# wizard DB hot-swaps (replace_services) are always observed.
_server_ref: Any | None = None


def bind_ocr_server(server: Any) -> None:
    """Bind the live :class:`OctopServer` (called from the server.py hook)."""
    global _server_ref
    _server_ref = server


def _runtime() -> tuple[Any, Any]:
    """Return ``(services, agent_registry)`` or raise a user-facing error."""
    server = _server_ref
    if server is None:
        raise RuntimeError("OCR 工具未初始化（服务尚未完成启动）")
    services = getattr(server, "services", None)
    runtime = getattr(server, "app_runtime", None)
    registry = getattr(runtime, "agent_registry", None) if runtime is not None else None
    if services is None or registry is None:
        raise RuntimeError("OCR 工具未初始化（控制面尚未绑定）")
    return services, registry


def _safe_fragment(file_path: str) -> str:
    """Normalize a workspace-relative path; reject absolute and traversal forms."""
    raw = (file_path or "").strip().strip("'\"")
    if not raw:
        raise ValueError("缺少参数 file_path（工作区相对路径）")
    if raw.startswith(("/", "\\")) or (len(raw) > 1 and raw[1] == ":"):
        raise ValueError("file_path 必须是工作区相对路径，例如 inbound/photo.png")
    parts = [part for part in PurePosixPath(raw).parts if part != "."]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("file_path 不能包含 .. 或为空")
    fragment = "/".join(parts)
    if os.path.isabs(fragment):
        raise ValueError("file_path 必须是工作区相对路径")
    return fragment


def _tool_settings_get(real_get: Any) -> Any:
    """Wrap ``settings_repo.get`` so the KB enable flag never gates the tool."""

    def get(key: str, *args: Any, **kwargs: Any) -> Any:
        if key == "knowledge_ocr_enabled":
            return "true"
        return real_get(key, *args, **kwargs)

    return get


def _build_extractor() -> Any:
    """Build the KB OCR extractor honoring shared engine config (Plan C)."""
    from octop.infra.knowledge.ocr import require_ocr_extractor

    services, _ = _runtime()

    def settings_get(key: str, *args: Any, **kwargs: Any) -> Any:
        if key == "knowledge_ocr_enabled":
            return "true"
        return services.settings_repo.get(key, *args, **kwargs)

    services_ns = SimpleNamespace(
        settings_repo=SimpleNamespace(get=settings_get),
        provider_repo=services.provider_repo,
    )
    try:
        return require_ocr_extractor(services_ns)
    except RuntimeError as exc:
        message = str(exc)
        if "local OCR prerequisites" in message:
            raise RuntimeError(
                "本地 OCR 组件未安装：请管理员在知识库设置中启用一次 OCR（本地引擎）以安装组件，"
                "或改用 remote 后端并选择视觉模型"
            ) from exc
        if "remote OCR provider" in message:
            raise RuntimeError(
                "OCR 远程引擎未就绪：请管理员在知识库 OCR 设置中选择可用的视觉模型"
            ) from exc
        raise


def _bind_scope_from_thread(thread_row: Any) -> None:
    """Re-bind the RLS tenant scope inside the tool-execution context.

    Tool fns run on the harness worker loop, where the request-scoped tenant
    ContextVar (auth middleware) is absent. ``threads`` carries no RLS so the
    thread row resolves regardless, but ``agents`` is tenant-guarded — without
    a bound scope the agent lookup fail-closes. Re-resolve the thread owner's
    authz from storage and bind its scope, the same self-heal the MCP
    preparation path performs (``AgentManager``). The binding persists for the
    rest of the tool call; pooled connections never retain it because the pool
    transfers it with ``set_config(..., is_local=True)``.
    """
    services, _registry = _runtime()
    repos = getattr(services, "repos", None)
    user_repo = getattr(repos, "user_repo", None) if repos is not None else None
    org_repo = getattr(repos, "org_repo", None) if repos is not None else None
    uid = getattr(thread_row, "user_id", None)
    if user_repo is None or org_repo is None or uid is None:
        return
    try:
        from octop.infra.users.context import resolve_authz_context

        resolve_authz_context(user_repo, org_repo, uid)
    except Exception:  # noqa: BLE001 — best-effort scope; queries fail closed without it
        logger.debug("ocr_bridge: tenant scope re-bind failed", exc_info=True)


def _workspace_for_thread(thread_id: str) -> Any:
    services, registry = _runtime()
    thread_row = services.repos.thread_repo.get(thread_id)
    if thread_row is None:
        raise RuntimeError("无法定位当前会话（thread 不存在）")
    _bind_scope_from_thread(thread_row)
    agent_row = services.repos.agent_repo.get(thread_row.agent_id)
    if agent_row is None:
        raise RuntimeError("无法定位当前会话对应的 Agent")
    return registry._backend_workspace_for_row(agent_row)


def _thread_id() -> str:
    from langgraph.config import get_config

    configurable = (get_config() or {}).get("configurable") or {}
    return str(configurable.get("thread_id") or "")


def _materialize(workspace: Any, fragment: str, suffix: str) -> tuple[Path, Any]:
    """Return ``(local_path, cleanup)`` for a workspace-relative file."""
    local = workspace.materialize_local(fragment)
    if local is not None:
        return local, None
    data = workspace.download_bytes(fragment)
    if data is None:
        raise RuntimeError(f"工作区中不存在文件：{fragment}")
    descriptor, temp_name = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise

    def cleanup() -> None:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)

    return Path(temp_name), cleanup


async def ocr_recognize(file_path: str = "") -> str:
    """Transcribe visible text from a workspace image or scanned PDF."""
    try:
        fragment = _safe_fragment(file_path)
    except ValueError as exc:
        return f"OCR 失败：{exc}"

    suffix = PurePosixPath(fragment).suffix.lower()
    if suffix not in _SUPPORTED_SUFFIXES:
        return "OCR 失败：仅支持 png/jpg/jpeg/webp 图片与 pdf 文件"

    try:
        thread_id = _thread_id()
        if not thread_id:
            raise RuntimeError("无法定位当前会话（缺少 thread_id）")
        workspace = _workspace_for_thread(thread_id)
        local_path, cleanup = _materialize(workspace, fragment, suffix)
        try:
            extractor = _build_extractor()
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(None, extractor, local_path)
        finally:
            if cleanup is not None:
                cleanup()
    except RuntimeError as exc:
        return f"OCR 失败：{exc}"
    except Exception as exc:  # noqa: BLE001 - tool boundary: report, don't crash the turn
        logger.exception("ocr tool failed for %s", fragment)
        return f"OCR 失败：{exc}"

    text = (text or "").strip()
    if not text:
        return "OCR 完成，但未识别到文本"
    if len(text) > _MAX_TEXT_CHARS:
        text = text[:_MAX_TEXT_CHARS] + "\n…（内容过长已截断）"
    return text


def register_ocr_tool(ctx: Any) -> None:
    """Register the tool on a harness ``PluginContext``."""
    ctx.tool(
        "ocr_recognize",
        ocr_recognize,
        description=(
            "识别文字（OCR）。file_path 为工作区相对路径（如 inbound/photo.png），"
            "支持 png/jpg/jpeg/webp 图片与扫描版 PDF（逐页识别，页数多时较慢）。"
            "返回图片/页面中的全部文本。"
        ),
    )
