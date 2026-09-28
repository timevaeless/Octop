"""PoC 3 worker: mid-stream cancellation against a real HarnessAgent.

Roles:
  cancel -- start streaming a turn on T-CANCEL against the slow fake LLM;
           after N chunks call ``agent.cancel(thread_id)``; the stream must
           terminate cleanly (no exception, bounded time); then aclose.
  verify -- reopen the agent and confirm the thread history is readable and
           contains the user marker (no corruption after cancellation).

Output: exactly one JSON line on stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from harness_agent import HarnessAgent, HarnessAgentConfig
from harness_agent.config import ModelConfig, ProviderConfig
from harness_agent.request import ChatRequest


def build_agent(args: argparse.Namespace) -> HarnessAgent:
    cfg = HarnessAgentConfig(
        name="poc-cross-agent",
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
        memory_namespace="poc-cross-agent",
        memory_aux_model_enabled=False,
        memory_extract_on_session_end=False,
        web_search_tools=False,
        ask_user_enabled=False,
        team_enabled=False,
        session_log_enabled=False,
        pii_enabled=False,
        model_retry_enabled=False,
    )
    return HarnessAgent(cfg, agent_id="poc-agent")


async def role_cancel(args: argparse.Namespace) -> dict:
    agent = build_agent(args)
    try:
        n_chunks = 0
        cancelled_at = None
        t0 = time.perf_counter()
        request = ChatRequest(
            messages=args.text, thread_id=args.thread, source="poc"
        )
        clean_end = False
        async for chunk in agent.stream(request):
            n_chunks += 1
            if n_chunks == args.cancel_after:
                cancelled_at = round((time.perf_counter() - t0) * 1000, 1)
                agent.cancel(args.thread)
        clean_end = True
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {
            "ok": clean_end,
            "role": "cancel",
            "thread": args.thread,
            "n_chunks_received": n_chunks,
            "cancelled_at_ms": cancelled_at,
            "elapsed_ms": elapsed_ms,
            "clean_end": clean_end,
        }
    finally:
        await agent.aclose()


async def role_verify(args: argparse.Namespace) -> dict:
    agent = build_agent(args)
    try:
        history = await agent.aget_history(args.thread)
        contents = []
        for msg in history:
            content = getattr(msg, "content", msg)
            if isinstance(content, (list, tuple)):
                content = " ".join(str(p) for p in content if isinstance(p, str))
            contents.append(str(content))
        found = any(args.expect_marker in c for c in contents)
        return {
            "ok": found,
            "role": "verify",
            "thread": args.thread,
            "history_len": len(history),
            "expect_marker": args.expect_marker,
            "found": found,
        }
    finally:
        await agent.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--role", required=True, choices=["cancel", "verify"])
    parser.add_argument("--thread", default="T-CANCEL")
    parser.add_argument("--text", default="hello")
    parser.add_argument("--expect-marker", default="")
    parser.add_argument("--cancel-after", type=int, default=15)
    args = parser.parse_args()

    try:
        report = asyncio.run(role_cancel(args) if args.role == "cancel" else role_verify(args))
        print(json.dumps(report, ensure_ascii=False), flush=True)
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {"ok": False, "role": args.role, "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            ),
            flush=True,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
