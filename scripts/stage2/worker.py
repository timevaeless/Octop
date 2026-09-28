"""Stage-2 integration worker: agent-role process for run_stage2.py.

Executes queued turns with a REAL HarnessAgent (postgres memory backend =
LangGraph checkpointer, shared workspace dir, fake OpenAI-protocol LLM) via
``AgentWorker`` — the production code path (queue consume → local executor →
chunk publish), deliberately WITHOUT a local per-thread lock so that
serialization must come from the procrastinate job lock.

Runs until SIGTERM. stderr carries diagnostics; readiness is implied by the
queue being consumed (the orchestrator waits for turn results).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from harness_agent import HarnessAgent, HarnessAgentConfig
from harness_agent.config import ModelConfig, ProviderConfig
from harness_agent.request import ChatRequest

AGENT_ID = "stage2-agent"


def build_agent(args: argparse.Namespace) -> HarnessAgent:
    cfg = HarnessAgentConfig(
        name=AGENT_ID,
        workspace_dir=Path(args.workspace),
        providers=[
            ProviderConfig(
                id="fake",
                base_url=f"{args.base_url}/v1",
                api_key="poc-key",
                protocol="openai",
                models=[ModelConfig(id="fake-model")],
            )
        ],
        default_model="fake/fake-model",
        memory_enabled=True,
        memory_backend={"type": "postgres", "dsn": args.dsn},
        memory_namespace=AGENT_ID,
        memory_aux_model_enabled=False,
        memory_extract_on_session_end=False,
        web_search_tools=False,
        ask_user_enabled=False,
        team_enabled=False,
        session_log_enabled=False,
        pii_enabled=False,
        model_retry_enabled=False,
    )
    return HarnessAgent(cfg, agent_id=AGENT_ID)


class HarnessLocalExecutor:
    """AgentExecutor over a real HarnessAgent (no local per-thread lock)."""

    def __init__(self, agent: HarnessAgent) -> None:
        self._agent = agent

    def _request(self, request: dict[str, Any]) -> ChatRequest:
        return ChatRequest(
            messages=str(request.get("messages") or ""),
            thread_id=str(request.get("thread_id") or ""),
            source="stage2",
        )

    async def stream(self, agent_id: str, request: dict[str, Any]) -> AsyncIterator[Any]:
        if agent_id != AGENT_ID:
            raise ValueError(f"unknown agent {agent_id!r} on this worker")
        async for chunk in self._agent.stream(self._request(request)):
            yield chunk

    async def call(self, agent_id: str, request: dict[str, Any]) -> dict[str, Any]:
        if agent_id != AGENT_ID:
            raise ValueError(f"unknown agent {agent_id!r} on this worker")
        result = await self._agent.call(self._request(request))
        return result if isinstance(result, dict) else {"result": result}

    async def resume_hitl(
        self,
        agent_id: str,
        thread_id: str,
        decisions: list[dict[str, Any]],
    ) -> AsyncIterator[Any]:
        raise NotImplementedError("HITL resume is not exercised by the stage-2 script")

    def cancel_stream(self, agent_id: str, thread_id: str) -> None:
        self._agent.cancel(thread_id)


async def run(args: argparse.Namespace) -> None:
    from octop.infra.cluster.remote import AgentWorker

    agent = build_agent(args)

    async def warmup() -> None:
        """Pre-create the langgraph checkpoint tables before any job runs.

        ``PostgresSaver.setup()`` executes ``CREATE INDEX CONCURRENTLY``,
        which waits for ALL open transactions. procrastinate keeps its job
        fetch transaction open while a job executes, so lazy per-turn setup
        would deadlock. Serialized across workers via an advisory lock.
        """
        import psycopg

        memory = agent.memory
        if memory is None:
            return

        def _setup() -> None:
            conn = psycopg.connect(args.dsn, autocommit=True)
            try:
                # try-lock + sleep loop: a blocking pg_advisory_lock() keeps an
                # implicit transaction open while waiting, and that very
                # transaction would block the other worker's
                # CREATE INDEX CONCURRENTLY (deadlock).
                deadline = time.time() + 120
                while True:
                    got = conn.execute(
                        "SELECT pg_try_advisory_lock(%s)", (918273646,)
                    ).fetchone()[0]
                    if got:
                        break
                    if time.time() > deadline:
                        raise TimeoutError("warmup advisory lock timeout")
                    time.sleep(0.2)
                try:
                    memory._ensure_checkpointer()
                finally:
                    conn.execute("SELECT pg_advisory_unlock(%s)", (918273646,))
            finally:
                conn.close()

        await asyncio.to_thread(_setup)

    worker = AgentWorker(
        args.dsn,
        HarnessLocalExecutor(agent),
        concurrency=args.concurrency,
        worker_name=f"stage2-worker-{args.name}",
        warmup=warmup,
    )
    try:
        await worker.run_forever()
    finally:
        await agent.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--name", required=True, help="worker label (w1/w2)")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--debug", action="store_true", help="debug logging")
    parser.add_argument(
        "--stack-dump",
        type=int,
        default=0,
        help="Dump all thread stacks every N seconds (diagnostics).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.stack_dump > 0:
        import faulthandler

        faulthandler.dump_traceback_later(args.stack_dump, repeat=True, file=sys.stderr)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
