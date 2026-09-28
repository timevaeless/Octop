"""PoC 1 worker: run real harness_agent turns in an isolated process.

Each worker constructs a real ``HarnessAgent`` with the postgres memory
backend (``Memory`` doubles as the LangGraph checkpointer), pointed at the
fake LLM server, sharing the same workspace directory as its peers.

Roles:
  turn    -- run one turn on the given thread; report chunk count + reply
  verify  -- open the agent and read the thread history via checkpointer;
             assert the expected marker is present (cross-process read)
  nocheck -- construct the agent twice-open check and exit (smoke)

Output: exactly one JSON line on stdout (the report); diagnostics on stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from harness_agent import HarnessAgent, HarnessAgentConfig
from harness_agent.config import ModelConfig, ProviderConfig
from harness_agent.request import ChatRequest


def build_config(args: argparse.Namespace) -> HarnessAgentConfig:
    return HarnessAgentConfig(
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
        memory_aux_model_enabled=False,  # no extraction LLM; capture still on
        memory_extract_on_session_end=False,
        web_search_tools=False,
        ask_user_enabled=False,
        team_enabled=False,
        session_log_enabled=False,  # avoid JSONL write races in shared workspace
        pii_enabled=False,  # keep markers verbatim end to end
        model_retry_enabled=False,  # fail fast: surface real conflicts
    )


def build_agent(args: argparse.Namespace) -> HarnessAgent:
    return HarnessAgent(build_config(args), agent_id="poc-agent")


async def role_turn(args: argparse.Namespace) -> dict:
    agent = build_agent(args)
    try:
        reply_parts: list[str] = []
        n_chunks = 0
        request = ChatRequest(messages=args.text, thread_id=args.thread, source="poc")
        async for chunk in agent.stream(request):
            n_chunks += 1
            if isinstance(chunk, dict) and chunk.get("type") == "token":
                reply_parts.append(str(chunk.get("content") or ""))
        reply = "".join(reply_parts)
        history = await agent.aget_history(args.thread)
        return {
            "ok": True,
            "role": "turn",
            "thread": args.thread,
            "n_chunks": n_chunks,
            "reply": reply[:300],
            "history_len": len(history),
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
                content = " ".join(
                    str(part) for part in content if isinstance(part, str)
                )
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
    parser.add_argument("--role", required=True, choices=["turn", "verify"])
    parser.add_argument("--thread", default="poc-thread")
    parser.add_argument("--text", default="hello")
    parser.add_argument("--expect-marker", default="")
    args = parser.parse_args()

    try:
        if args.role == "turn":
            report = asyncio.run(role_turn(args))
        else:
            report = asyncio.run(role_verify(args))
        print(json.dumps(report, ensure_ascii=False), flush=True)
    except Exception as exc:  # noqa: BLE001 - report everything to orchestrator
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
