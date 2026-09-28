"""Stage-3 production-readiness validation over real PostgreSQL.

Phases:
  load    40 concurrent remote streams through the full queue path
          (RemoteExecutor [shared NotifyHub + fetch pool] -> procrastinate ->
          2 agent workers -> fake LLM). Measures first-chunk latency p50/p95,
          total latency, throughput, and the PG connection ceiling observed
          via pg_stat_activity.
  prune   turn-events retention: expired rows deleted, fresh rows kept.
  leader  two LeaderRunners over real PG: single leader, automatic failover
          when the leader's session dies.

Usage:
  .venv/bin/python scripts/stage3/run_stage3.py
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psycopg

from octop.infra.cluster.leader import LeaderHandle, LeaderRunner
from octop.infra.cluster.remote import (
    EVENTS_TABLE,
    RemoteExecutor,
    ensure_turn_schema,
    prune_turn_events,
)

ROOT = Path(__file__).resolve().parents[2]
STAGE2 = ROOT / "scripts" / "stage2"
VENV_PY = ROOT / ".venv" / "bin" / "python"
START_PG = ROOT / "scripts" / "poc1" / "start_pg.py"

sys.path.insert(0, str(ROOT / "scripts" / "support"))
from pg_python import resolve_pg_python  # noqa: E402

WORKSPACE = ROOT / ".workbuddy" / "stage3-workspace"
AGENT_ID = "stage2-agent"  # same fixed agent the stage-2 worker boots

STREAMS = 40
WORKER_CONCURRENCY = 8


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def tag_dsn(dsn: str, name: str) -> str:
    """Tag a DSN with application_name so pg_stat_activity can attribute conns."""
    sep = "&" if "?" in dsn else "?"
    return f"{dsn}{sep}application_name={name}"


def clean_env() -> dict[str, str]:
    blocked = {"http_proxy", "https_proxy", "all_proxy"}
    return {k: v for k, v in os.environ.items() if k.lower() not in blocked}


def start_embedded_pg() -> tuple[subprocess.Popen[bytes], str]:
    pgserver_py = resolve_pg_python()
    pgdata = ROOT / ".workbuddy" / f"stage3-pgdata-{time.strftime('%H%M%S')}"
    proc = subprocess.Popen(
        [str(pgserver_py), str(START_PG), str(pgdata)],
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


class FakeLLM:
    def __init__(self, token_delay_ms: int) -> None:
        self.port = free_port()
        self.proc: subprocess.Popen[bytes] | None = None
        self._cmd = [
            str(VENV_PY),
            str(ROOT / "scripts" / "poc1" / "fake_llm.py"),
            "--port",
            str(self.port),
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
        raise RuntimeError("fake LLM failed to start")

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


def spawn_worker(dsn: str, base_url: str, name: str) -> subprocess.Popen[bytes]:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(VENV_PY),
        str(STAGE2 / "worker.py"),
        "--dsn",
        dsn,
        "--workspace",
        str(WORKSPACE),
        "--base-url",
        base_url,
        "--name",
        name,
        "--concurrency",
        str(WORKER_CONCURRENCY),
    ]
    return subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=(WORKSPACE / f"worker-{name}.log").open("wb"),
        env=clean_env(),
    )


async def sample_pg_connections(
    dsn: str, stop: asyncio.Event, per_source: dict[str, int], total: list[int]
) -> None:
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        while not stop.is_set():
            cur = await conn.execute(
                "SELECT coalesce(nullif(application_name, ''), 'other'), count(*) "
                "FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                "GROUP BY 1"
            )
            rows = await cur.fetchall()
            total.append(sum(int(n) for _, n in rows))
            for name, n in rows:
                if int(n) > per_source.get(name, 0):
                    per_source[name] = int(n)
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.1)
            except TimeoutError:
                pass


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    idx = min(int(len(ordered) * pct / 100), len(ordered) - 1)
    return round(ordered[idx], 1)


async def phase_load(dsn: str, executor: RemoteExecutor) -> dict[str, Any]:
    stop = asyncio.Event()
    per_source: dict[str, int] = {}
    total: list[int] = []
    sampler = asyncio.create_task(
        sample_pg_connections(dsn, stop, per_source, total)
    )

    async def one(i: int) -> dict[str, Any]:
        marker = f"LOAD-{i:03d}"
        text = f"Please remember this codeword: {marker}. Just acknowledge."
        t0 = time.perf_counter()
        first_ms: float | None = None
        n = 0
        reply_parts: list[str] = []
        async for chunk in executor.stream(
            AGENT_ID, {"thread_id": f"LOAD-{i:03d}", "messages": text}
        ):
            if first_ms is None:
                first_ms = (time.perf_counter() - t0) * 1000
            n += 1
            if chunk.get("type") == "token":
                reply_parts.append(str(chunk.get("content") or ""))
        total_ms = (time.perf_counter() - t0) * 1000
        reply = "".join(reply_parts)
        return {
            "first_ms": round(first_ms, 1) if first_ms else None,
            "total_ms": round(total_ms, 1),
            "n_chunks": n,
            "marker_echoed": marker in reply,
        }

    t_start = time.perf_counter()
    results = await asyncio.gather(*(one(i) for i in range(STREAMS)))
    wall = time.perf_counter() - t_start
    stop.set()
    await sampler

    firsts = [r["first_ms"] for r in results if r["first_ms"] is not None]
    totals = [r["total_ms"] for r in results]
    check = {
        "all_completed": len(results) == STREAMS,
        "all_markers_echoed": all(r["marker_echoed"] for r in results),
        "all_have_chunks": all(r["n_chunks"] > 0 for r in results),
    }
    hub = executor._hub
    hub_conns = getattr(hub, "connection_count", None) if hub else None
    return {
        "streams": STREAMS,
        "worker_concurrency_total": WORKER_CONCURRENCY * 2,
        "wall_seconds": round(wall, 1),
        "throughput_streams_per_min": round(STREAMS / wall * 60, 1),
        "first_chunk_ms_p50": percentile(firsts, 50),
        "first_chunk_ms_p95": percentile(firsts, 95),
        "total_ms_p50": percentile(totals, 50),
        "total_ms_p95": percentile(totals, 95),
        "pg_connections_max": max(total) if total else None,
        "pg_conns_by_source": per_source,
        "executor_conns_max": per_source.get("stage3-exec", 0),
        "hub_connections": hub_conns,
        **check,
    }


async def phase_prune(dsn: str) -> dict[str, Any]:
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute(
            f"INSERT INTO {EVENTS_TABLE} (job_id, payload, created_at) "
            f"VALUES ('prune-old', %s, now() - interval '2 hours')",
            (json.dumps({"c": "{}", "ts": 0}),),
        )
        await conn.execute(
            f"INSERT INTO {EVENTS_TABLE} (job_id, payload, created_at) "
            f"VALUES ('prune-old', %s, now() - interval '2 hours')",
            (json.dumps({"c": "{}", "ts": 0}),),
        )
        await conn.execute(
            f"INSERT INTO {EVENTS_TABLE} (job_id, payload) VALUES ('prune-new', %s)",
            (json.dumps({"c": "{}", "ts": 0}),),
        )
    deleted = await prune_turn_events(dsn, retention_seconds=1800.0)
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        cur = await conn.execute(
            f"SELECT count(*) FROM {EVENTS_TABLE} WHERE job_id = 'prune-old'"
        )
        old_left = (await cur.fetchone() or [0])[0]
        cur = await conn.execute(
            f"SELECT count(*) FROM {EVENTS_TABLE} WHERE job_id = 'prune-new'"
        )
        new_left = (await cur.fetchone() or [0])[0]
    return {"deleted": deleted, "old_left": old_left, "new_left": new_left}


class Recorder:
    def __init__(self, name: str) -> None:
        self.name = name
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


async def wait_until(cond: Any, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("leader condition timeout")
        await asyncio.sleep(0.05)


async def phase_leader(dsn: str) -> dict[str, Any]:
    rec_a, rec_b = Recorder("a"), Recorder("b")
    a = LeaderRunner(dsn, poll_interval=0.2, retry_delay=0.2)
    b = LeaderRunner(dsn, poll_interval=0.2, retry_delay=0.2)
    a.attach(LeaderHandle("a-svc", rec_a.start, rec_a.stop))
    b.attach(LeaderHandle("b-svc", rec_b.start, rec_b.stop))
    a.start()
    b.start()
    await wait_until(lambda: a.is_leader)
    await asyncio.sleep(0.5)
    b_was_follower = not b.is_leader
    await a.stop()  # leader session dies -> b must take over
    await wait_until(lambda: b.is_leader, timeout=15.0)
    await wait_until(lambda: rec_b.started >= 1)
    await b.stop()
    return {
        "a_became_leader": True,
        "b_was_follower": b_was_follower,
        "b_took_over_after_a_stopped": True,
        "a_service_started": rec_a.started >= 1,
        "b_service_started": rec_b.started >= 1,
    }


async def main() -> int:
    pg_proc, dsn = start_embedded_pg()
    llm = FakeLLM(token_delay_ms=4)
    workers: list[subprocess.Popen[bytes]] = []
    results: dict[str, dict[str, Any]] = {}
    executor: RemoteExecutor | None = None
    try:
        await ensure_turn_schema(dsn)
        llm.__enter__()
        workers.append(spawn_worker(tag_dsn(dsn, "stage3-w1"), llm.base_url, "w1"))
        workers.append(spawn_worker(tag_dsn(dsn, "stage3-w2"), llm.base_url, "w2"))
        await asyncio.sleep(10.0)  # worker boot + checkpointer warmup

        executor = RemoteExecutor(tag_dsn(dsn, "stage3-exec"))
        results["load"] = await phase_load(dsn, executor)
        results["prune"] = await phase_prune(dsn)
        results["leader"] = await phase_leader(dsn)
    finally:
        if executor is not None:
            await executor.aclose()
        for proc in workers:
            proc.terminate()
        llm.__exit__()
        pg_proc.terminate()

    checks: list[tuple[str, bool]] = []
    load = results["load"]
    checks.append(("load: all streams completed", load["all_completed"]))
    checks.append(("load: all markers echoed", load["all_markers_echoed"]))
    checks.append(("load: all have chunks", load["all_have_chunks"]))
    checks.append(("load: hub holds 1 connection", load["hub_connections"] == 1))
    if load.get("executor_conns_max") is not None:
        # API 节点侧（hub 1 + fetch 池 8 + procrastinate 提交池）必须有界，
        # 不随 40 路流线性增长 —— 这是 stage3 收敛改造的核心验收项。
        checks.append(
            (
                "load: executor-side conns bounded (<25, independent of 40 streams)",
                load["executor_conns_max"] < 25,
            )
        )
    prune = results["prune"]
    checks.append(("prune: expired rows deleted", prune["old_left"] == 0))
    checks.append(("prune: fresh rows kept", prune["new_left"] == 1))
    leader = results["leader"]
    checks.append(("leader: follower stayed standby", leader["b_was_follower"]))
    checks.append(("leader: failover worked", leader["b_took_over_after_a_stopped"]))
    checks.append(("leader: both services ran", leader["a_service_started"] and leader["b_service_started"]))

    lines = ["stage3 production-readiness results:"]
    for name, data in results.items():
        if name == "load":
            lines.append(
                f"  load: {data['streams']} streams, wall {data['wall_seconds']}s, "
                f"throughput {data['throughput_streams_per_min']} streams/min, "
                f"first-chunk p50 {data['first_chunk_ms_p50']}ms / p95 {data['first_chunk_ms_p95']}ms, "
                f"total p50 {data['total_ms_p50']}ms / p95 {data['total_ms_p95']}ms, "
                f"pg_conns_max {data['pg_connections_max']} by_source {data['pg_conns_by_source']}, "
                f"hub_conns {data['hub_connections']}"
            )
        else:
            lines.append(f"  {name}: {json.dumps(data, ensure_ascii=False)}")
    ok = all(passed for _, passed in checks)
    for name, passed in checks:
        lines.append(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    results["checks"] = {name: passed for name, passed in checks}
    results["verdict"] = "PASS" if ok else "FAIL"
    out = Path(__file__).parent / "result.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print("\n".join(lines))
    print(f"verdict: {results['verdict']}  report: {out}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
