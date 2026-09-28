"""Stage-2 integration: two-process topology over real PostgreSQL.

Topology (``docs/microservice-scaling-1000.md`` §4, exit criteria of stage 2,
mechanism level — HTTP/SSE-layer e2e belongs to stage 3):

    api side (this process)                    agent side (2 worker procs)
    RemoteExecutor (procrastinate defer)  -->  AgentWorker ×2 (real HarnessAgent)
    TurnStreamReader (LISTEN + poll)      <--  TurnEventPublisher (50ms flush)

Scenarios:
  1 stream_e2e               chunks flow worker → api, in order, clean end
  2 call_e2e                 non-streaming call returns the result dict
  3 parallel_threads         two threads concurrently (both workers busy)
  4 same_thread_serialization  two same-thread turns: queue lock enforces order
  5 cancel_e2e               mid-stream cancel via cancel_stream; stream ends
                             promptly; worker stays healthy afterwards
  6 error_propagation        worker-side failure surfaces as RemoteTurnError

Usage:
  .venv/bin/python scripts/stage2/run_stage2.py            # embedded pgserver
  POC_PG_DSN=postgres://... .venv/bin/python scripts/stage2/run_stage2.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
VENV_PY = ROOT / ".venv" / "bin" / "python"
POC1 = ROOT / "scripts" / "poc1"

sys.path.insert(0, str(HERE.parent / "support"))
from pg_python import resolve_pg_python  # noqa: E402

WORKSPACE = ROOT / ".workbuddy" / "stage2-workspace"
AGENT_ID = "stage2-agent"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class FakeLLM:
    """OpenAI-protocol fake LLM with slow mode (token delay) for cancel tests."""

    def __init__(self, log_path: Path, token_delay_ms: int) -> None:
        self.port = free_port()
        self.proc: subprocess.Popen[bytes] | None = None
        self._cmd = [
            str(VENV_PY),
            str(POC1 / "fake_llm.py"),
            "--port",
            str(self.port),
            "--log",
            str(log_path),
            "--token-delay-ms",
            str(token_delay_ms),
        ]

    def __enter__(self) -> FakeLLM:
        self.proc = subprocess.Popen(
            self._cmd, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return self
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("fake LLM server failed to start")

    def __exit__(self, *exc: object) -> None:
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def start_embedded_pg(pgdata: Path) -> tuple[subprocess.Popen[bytes], str]:
    # pgserver lives outside the project venv; resolve an interpreter that
    # carries it (OCTOP_PG_PYTHON env or the current interpreter — D12).
    managed_py = resolve_pg_python()
    proc = subprocess.Popen(
        [
            str(managed_py),
            str(POC1 / "start_pg.py"),
            str(pgdata),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline().decode().strip()
    if not line.startswith("postgresql"):
        err = proc.stderr.read().decode() if proc.stderr else ""
        proc.kill()
        raise RuntimeError(f"pgserver failed: {err[-2000:]}")
    return proc, line


def _clean_env() -> dict[str, str]:
    """Strip proxy vars: the fake LLM is local; harness env-proxy detection
    would otherwise route 127.0.0.1 traffic into an unreachable proxy."""
    blocked = {"http_proxy", "https_proxy", "all_proxy"}
    return {k: v for k, v in os.environ.items() if k.lower() not in blocked}


def spawn_worker(args: argparse.Namespace, base_url: str, name: str) -> subprocess.Popen[bytes]:
    cmd = [
        str(VENV_PY),
        str(HERE / "worker.py"),
        "--dsn",
        args.dsn,
        "--workspace",
        str(WORKSPACE),
        "--base-url",
        base_url,
        "--name",
        name,
        "--debug",
        "--stack-dump",
        "30",
    ]
    log_path = WORKSPACE / f"worker-{name}.log"
    return subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=log_path.open("wb"),
        env=_clean_env(),
    )


async def run_phase1_stream(executor: Any, marker: str) -> dict:
    text = f"Please remember this codeword: {marker}. Just acknowledge."
    t0 = time.perf_counter()
    chunks = [
        chunk
        async for chunk in executor.stream(AGENT_ID, {"thread_id": "S1", "messages": text})
    ]
    elapsed = round((time.perf_counter() - t0) * 1000, 1)
    reply = "".join(str(c.get("content") or "") for c in chunks if c.get("type") == "token")
    return {
        "n_chunks": len(chunks),
        "elapsed_ms": elapsed,
        "reply_head": reply[:80],
        "marker_echoed": marker in reply,
        "all_dicts": all(isinstance(c, dict) for c in chunks),
    }


async def run_phase2_call(executor: Any, marker: str) -> dict:
    result = await executor.call(
        AGENT_ID,
        {"thread_id": "S2", "messages": f"Call-phase marker: {marker}. Acknowledge."},
    )
    return {
        "result_keys": sorted(result.keys()) if isinstance(result, dict) else None,
        "result_head": json.dumps(result, ensure_ascii=False)[:120],
        "marker_echoed": marker in json.dumps(result, ensure_ascii=False),
    }


async def run_phase3_parallel(executor: Any) -> dict:
    async def one(tid: str, marker: str) -> dict:
        chunks = [
            chunk
            async for chunk in executor.stream(
                AGENT_ID,
                {"thread_id": tid, "messages": f"Parallel marker: {marker}. Acknowledge."},
            )
        ]
        reply = "".join(
            str(c.get("content") or "") for c in chunks if c.get("type") == "token"
        )
        return {"marker_echoed": marker in reply, "n_chunks": len(chunks)}

    p1, p2 = await asyncio.gather(
        one("S3-P1", "PARALLEL-ALPHA-01"),
        one("S3-P2", "PARALLEL-BETA-02"),
    )
    return {"p1": p1, "p2": p2}


async def run_phase4_serialization(executor: Any) -> dict:
    """Two same-thread turns deferred together must run back-to-back."""
    long_text = "SER-TURN-ONE " * 15  # echoed reply ~200 chars → many token pieces
    t0 = time.perf_counter()

    async def turn_a() -> float:
        _ = [
            chunk
            async for chunk in executor.stream(
                AGENT_ID, {"thread_id": "S4-SAME", "messages": long_text}
            )
        ]
        return time.perf_counter() - t0

    async def turn_b() -> float:
        _ = [
            chunk
            async for chunk in executor.stream(
                AGENT_ID,
                {"thread_id": "S4-SAME", "messages": "SER-TURN-TWO short. Acknowledge."},
            )
        ]
        return time.perf_counter() - t0

    a_end, b_end = await asyncio.gather(turn_a(), turn_b())
    gap_ms = round((b_end - a_end) * 1000, 1)
    return {
        "turn_a_end_ms": round(a_end * 1000, 1),
        "turn_b_end_ms": round(b_end * 1000, 1),
        "gap_ms": gap_ms,
        "serialized": b_end >= a_end and gap_ms >= 250.0,
    }


async def run_phase5_cancel(executor: Any) -> dict:
    thread = "S5-CANCEL"
    text = "CANCEL-MARKER-Z9 " * 20  # long echo → long slow stream
    stream = executor.stream(AGENT_ID, {"thread_id": thread, "messages": text})
    chunks: list[Any] = []
    t0 = time.perf_counter()
    cancelled_at_ms: float | None = None
    async for chunk in stream:
        chunks.append(chunk)
        if len(chunks) == 3:
            cancelled_at_ms = round((time.perf_counter() - t0) * 1000, 1)
            executor.cancel_stream(AGENT_ID, thread)
    elapsed = round((time.perf_counter() - t0) * 1000, 1)

    # worker must stay healthy: follow-up turn on a fresh thread
    followup = [
        chunk
        async for chunk in executor.stream(
            AGENT_ID, {"thread_id": "S5-AFTER", "messages": "post-cancel health check"}
        )
    ]
    followup_ok = len(followup) > 0
    return {
        "n_chunks_before_cancel": len(chunks),
        "cancelled_at_ms": cancelled_at_ms,
        "total_elapsed_ms": elapsed,
        "cancel_effective": elapsed < 5000.0,
        "worker_healthy_after": followup_ok,
    }


async def run_phase6_error(executor: Any) -> dict:
    from octop.infra.cluster.remote import RemoteTurnError

    try:
        _ = [
            chunk
            async for chunk in executor.stream(
                "no-such-agent", {"thread_id": "S6-ERR", "messages": "hi"}
            )
        ]
    except RemoteTurnError as exc:
        return {"raised": True, "message_head": str(exc)[:100]}
    return {"raised": False}


def stop_workers(workers: list[subprocess.Popen[bytes]]) -> None:
    for proc in workers:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
    deadline = time.time() + 10
    for proc in workers:
        try:
            proc.wait(timeout=max(0.5, deadline - time.time()))
        except subprocess.TimeoutExpired:
            proc.kill()


async def scenarios(args: argparse.Namespace, base_url: str) -> dict:
    from octop.infra.cluster.remote import RemoteExecutor

    workers = [spawn_worker(args, base_url, name) for name in ("w1", "w2")]
    await asyncio.sleep(4.0)  # worker boot (harness + queue listener)
    results: dict[str, dict] = {}
    executor = RemoteExecutor(dsn=args.dsn, turn_timeout=120.0)
    try:
        results["1_stream_e2e"] = await run_phase1_stream(executor, "CODEWORD-BANANA-42")
        results["2_call_e2e"] = await run_phase2_call(executor, "CALL-PHASE-NEBULA")
        results["3_parallel_threads"] = await run_phase3_parallel(executor)
        results["4_same_thread_serialization"] = await run_phase4_serialization(executor)
        results["5_cancel_e2e"] = await run_phase5_cancel(executor)
        results["6_error_propagation"] = await run_phase6_error(executor)
    finally:
        await executor.aclose()
        stop_workers(workers)
    return results


def verdict(results: dict) -> tuple[bool, list[str]]:
    checks: list[str] = []
    ok = True

    p1 = results["1_stream_e2e"]
    checks.append(("1 stream_e2e", p1["marker_echoed"] and p1["all_dicts"]))
    p2 = results["2_call_e2e"]
    checks.append(("2 call_e2e", bool(p2["marker_echoed"])))
    p3 = results["3_parallel_threads"]
    checks.append(("3 parallel_threads", p3["p1"]["marker_echoed"] and p3["p2"]["marker_echoed"]))
    p4 = results["4_same_thread_serialization"]
    checks.append(("4 same_thread_serialization", bool(p4["serialized"])))
    p5 = results["5_cancel_e2e"]
    checks.append(
        ("5 cancel_e2e", p5["cancel_effective"] and p5["worker_healthy_after"])
    )
    p6 = results["6_error_propagation"]
    checks.append(("6 error_propagation", bool(p6["raised"])))

    lines = []
    for name, passed in checks:
        lines.append(f"{'PASS' if passed else 'FAIL'}  {name}")
        ok = ok and passed
    return ok, lines


async def main_async() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=os.environ.get("POC_PG_DSN", ""))
    args = parser.parse_args()

    pg_proc = None
    if not args.dsn:
        # Fresh pgdata per run: stale procrastinate 'todo' jobs with the same
        # per-thread lock would block newly deferred jobs indefinitely.
        pgdata = ROOT / ".workbuddy" / f"stage2-pgdata-{time.strftime('%H%M%S')}"
        pg_proc, args.dsn = start_embedded_pg(pgdata)

    WORKSPACE.mkdir(parents=True, exist_ok=True)
    llm_log = WORKSPACE / "llm_requests.log"
    if llm_log.exists():
        llm_log.unlink()

    results: dict = {"dsn": args.dsn.split("@")[-1]}
    try:
        # One fake LLM in slow mode: every turn streams many token pieces,
        # which makes cancel/serialization timing observable while keeping
        # ordinary turns fast (~0.5s).
        with FakeLLM(llm_log, token_delay_ms=40) as llm:
            results["phases"] = await scenarios(args, llm.base_url)
            ok, lines = verdict(results["phases"])
    finally:
        if pg_proc:
            pg_proc.send_signal(signal.SIGTERM)
            try:
                pg_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pg_proc.kill()

    results["verdict"] = "PASS" if ok else "FAIL"
    out_path = HERE / "result.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print("\n".join(lines))
    print(f"verdict: {results['verdict']}  report: {out_path}")
    if not ok:
        for name in ("w1", "w2"):
            log_path = WORKSPACE / f"worker-{name}.log"
            if log_path.exists():
                print(f"\n----- worker {name} log tail -----\n{log_path.read_text()[-3000:]}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))
