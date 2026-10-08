"""Declarative before/after patch registry (aspect-style isolation).

Octop adds behaviour to (mostly upstream) callables. The house style is a
one-line lazy hook woven into the upstream file ("delete this hook together
with its module to restore upstream behaviour"). That works well for code we
own a fork of, but every woven line is a merge-conflict surface. This module
offers an alternative for the subset of changes that are pure *before* or
*after* effects — audit, stamps, warm-ups, notifications — with no weave at
all:

    # octop/ext/agent_patches.py
    @before("octop.infra.agents.manager:AgentManager.delete_thread_checkpoint")
    async def warm(self, agent_id, thread_id) -> None: ...

    # app startup
    install_all()

Design decisions:

* **Fail loud, never silent.** A target whose module or attribute is missing
  raises :class:`PatchError` at startup. An upstream rename must break the
  boot loudly — a silently skipped patch would shadow nothing and the missing
  behaviour would surface later as a mystery.
* **Class-level, argument-preserving.** Patches wrap the resolved callable and
  pass the original arguments through untouched; hooks may be sync or async
  (an async hook against a sync target is rejected at install time, not at
  call time). Return values of ``before``/``after`` hooks are ignored — the
  original result flows through. Data-flow rewrites (changing arguments or
  results) are deliberately out of scope; those must stay as explicit code.
* **Install once.** :func:`install_all` is idempotent; each wrapped callable
  is marked so a second install can never double-wrap.
"""

from __future__ import annotations

import functools
import importlib
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

__all__ = [
    "PatchError",
    "after",
    "before",
    "install_all",
    "registered_patches",
    "reset_install",
]

_REGISTRATIONS_MODULE = "octop.ext.agent_patches"


class PatchError(RuntimeError):
    """A declared patch cannot be applied — fail loud at startup."""


@dataclass(frozen=True)
class _Patch:
    phase: str  # "before" | "after"
    target: str  # "package.module:Owner.attr"
    hook: Callable[..., Any]
    source: str  # module declaring the hook, for diagnostics


_REGISTRY: list[_Patch] = []
_INSTALLED = False


def before(target: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register *hook* to run before *target*; its return value is ignored."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        _REGISTRY.append(_Patch("before", target, fn, fn.__module__))
        return fn

    return decorator


def after(target: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register *hook* to run after *target*; its return value is ignored."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        _REGISTRY.append(_Patch("after", target, fn, fn.__module__))
        return fn

    return decorator


def registered_patches() -> list[tuple[str, str, str]]:
    """Snapshot of declared patches — ``(phase, target, source_module)``."""
    return [(p.phase, p.target, p.source) for p in _REGISTRY]


def _resolve(target: str) -> tuple[Any, str]:
    """Resolve ``package.module:Owner.attr`` to ``(owner_object, attr_name)``."""
    module_path, sep, attr_path = target.partition(":")
    if not sep or not module_path or not attr_path:
        raise PatchError(
            f"invalid patch target {target!r}: expected 'package.module:Owner.attr'"
        )
    try:
        obj: Any = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        raise PatchError(
            f"patch target module {module_path!r} is not importable: {exc}"
        ) from exc
    parts = attr_path.split(".")
    owner = obj
    for name in parts[:-1]:
        try:
            owner = getattr(owner, name)
        except AttributeError as exc:
            raise PatchError(
                f"patch target {target!r}: {name!r} missing — upstream moved it; "
                "fix or drop the patch in its declaring module"
            ) from exc
    attr = parts[-1]
    if not hasattr(owner, attr):
        raise PatchError(
            f"patch target {target!r}: attribute {attr!r} missing — upstream "
            "renamed or removed it; fix or drop the patch in its declaring module"
        )
    return owner, attr


def _check_hook_sync(original: Callable[..., Any], patch: _Patch) -> None:
    if inspect.iscoroutinefunction(original) and not inspect.iscoroutinefunction(patch.hook):
        return  # sync hook against an async target: the plain call is safe
    if not inspect.iscoroutinefunction(original) and inspect.iscoroutinefunction(patch.hook):
        raise PatchError(
            f"{patch.phase}-patch {patch.source}.{patch.hook.__name__} is async but "
            f"target {patch.target} is sync — declare the hook as a plain function"
        )


def _wrap(original: Callable[..., Any], patch: _Patch) -> Callable[..., Any]:
    _check_hook_sync(original, patch)
    hook = patch.hook

    if patch.phase == "before":
        if inspect.iscoroutinefunction(original):

            @functools.wraps(original)
            async def before_async(*args: Any, **kwargs: Any) -> Any:
                result = hook(*args, **kwargs)
                if inspect.isawaitable(result):
                    await result
                return await original(*args, **kwargs)

            return before_async

        @functools.wraps(original)
        def before_sync(*args: Any, **kwargs: Any) -> Any:
            hook(*args, **kwargs)
            return original(*args, **kwargs)

        return before_sync

    if inspect.iscoroutinefunction(original):

        @functools.wraps(original)
        async def after_async(*args: Any, **kwargs: Any) -> Any:
            value = await original(*args, **kwargs)
            result = hook(*args, **kwargs)
            if inspect.isawaitable(result):
                await result
            return value

        return after_async

    @functools.wraps(original)
    def after_sync(*args: Any, **kwargs: Any) -> Any:
        value = original(*args, **kwargs)
        hook(*args, **kwargs)
        return value

    return after_sync


def install_all(*, _registrations: str = _REGISTRATIONS_MODULE) -> None:
    """Apply every declared patch. Idempotent; fail loud on any bad target."""
    global _INSTALLED
    if _INSTALLED:
        return
    importlib.import_module(_registrations)  # load the declarations
    for patch in _REGISTRY:
        owner, attr = _resolve(patch.target)
        original = getattr(owner, attr)
        if getattr(original, "_octop_patch", None) is not None:
            continue  # already wrapped (e.g. install_all called twice)
        wrapped = _wrap(original, patch)
        wrapped._octop_patch = patch  # type: ignore[attr-defined]
        setattr(owner, attr, wrapped)
        logger.info(
            "installed %s patch on %s (declared in %s)",
            patch.phase,
            patch.target,
            patch.source,
        )
    _INSTALLED = True


def reset_install() -> None:
    """Test-only: allow :func:`install_all` to run again after re-registration."""
    global _INSTALLED
    _INSTALLED = False
