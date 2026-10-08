"""Checkpointer warm-up before thread deletion (pluggable bugfix module).

Root cause (2026-09-28, "对话删不掉" after a container rebuild):

* harness's default checkpointer (fallback #4 in ``octop_harness.agent``) is an
  ``AsyncSqliteSaver`` over a *lazily connected* ``aiosqlite.connect(...)`` —
  the real connection opens on first async use (any checkpoint write, i.e. the
  agent's first chat turn) and stays open for the process lifetime.
* langgraph-checkpoint-sqlite's ``adelete_thread`` is the *only* saver
  operation that does not call ``setup()`` / ``_ensure_connected()`` first
  (compare ``aput_writes``).  Deleting a thread before the agent's first turn
  therefore hits the un-opened connection and raises
  ``ValueError("no active connection")`` from aiosqlite's ``_conn`` property.
* Octop's delete route deliberately keeps the thread row when checkpoint
  deletion fails, so the failure surfaces as "对话删不掉" (persistent 500 on
  retry) until the agent has chatted once.

Fix: call the saver's idempotent ``setup()`` right before deletion — its first
call runs ``_ensure_connected``, opening the lazy connection.

Scope guard: only the lazy-sqlite shape is touched.  ``checkpointer`` False or
None → nothing to warm; ``octop_memory.Memory`` manages its own connection
and exposes neither ``setup`` nor ``conn``; anything whose ``setup`` is not a
coroutine function (test doubles, other savers) is skipped as well.

Deleting this module together with its registration in
``octop/ext/agent_patches.py`` (a ``before`` patch on
``AgentManager.delete_thread_checkpoint``) restores upstream behavior verbatim.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["warm_up_checkpointer"]


async def warm_up_checkpointer(harness: Any) -> None:
    """Open a lazy default-sqlite checkpointer connection before ``adelete_thread``.

    Never fabricates success: if warm-up itself fails the exception propagates
    so the caller keeps its fail-visible contract (thread row retained,
    retryable).
    """
    checkpointer = getattr(harness, "checkpointer", None)
    if not checkpointer:  # False (opt-out) or None — nothing to warm up
        return
    setup = getattr(checkpointer, "setup", None)
    if setup is None:  # Memory-backed checkpointer manages its own connection
        return
    if getattr(checkpointer, "conn", None) is None:  # not the sqlite saver shape
        return
    if not inspect.iscoroutinefunction(setup):  # test doubles / unknown savers
        return
    try:
        await setup()
    except Exception:
        logger.exception("checkpointer warm-up failed before thread deletion")
        raise
