"""PoC 2: chunk stream round-trip latency over PostgreSQL LISTEN/NOTIFY.

Models the microservice design's channel A:
  agent worker produces tokens -> 50ms batch flush -> INSERT + pg_notify in
  one transaction -> api process wakes on NOTIFY -> incremental SELECT
  (seq > last_seen) -> chunk handed to the SSE layer.

Latency per token = consumer_recv_time - producer_produce_time (same host,
time.time()). Scenario matrix:
  A  notify per token (flush=0)      -- upper bound of eager mode
  B  50ms flush at 30 tok/s          -- design default, realistic chat
  C  50ms flush at 100 tok/s         -- high-pressure turn

Usage:
  uv run python scripts/poc2/run_poc2.py            # embedded pgserver
  POC_PG_DSN=postgres://... uv run python scripts/poc2/run_poc2.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
VENV_PY = ROOT / ".venv" / "bin" / "python"

sys.path.insert(0, str(HERE.parent / "support"))
from pg_python import resolve_pg_python  # noqa: E402

CHANNEL = "poc_chunks"
END_TOKEN = "__END__"


def setup_schema(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            """
            DROP TABLE IF EXISTS poc_chunks;
            CREATE TABLE poc_chunks (
                seq BIGSERIAL PRIMARY KEY,
                payload TEXT NOT NULL,
                produced_at DOUBLE PRECISION NOT NULL
            )
            """
        )


async def _flush(conn: psycopg.AsyncConnection, batch: list[tuple[str, float]]) -> None:
    """One transaction: multi-row INSERT + NOTIFY (delivered at COMMIT)."""
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO poc_chunks (payload, produced_at) VALUES (%s, %s)",
                batch,
            )
        await conn.execute("SELECT pg_notify(%s, '')", (CHANNEL,))


async def produce(
    dsn: str,
    *,
    n_tokens: int,
    rate: float,
    flush_seconds: float,
) -> None:
    """Emit n_tokens at `rate` tokens/sec, flushing every flush_seconds."""
    conn = await psycopg.AsyncConnection.connect(dsn)
    interval = 1.0 / rate if rate > 0 else 0.0
    start = time.perf_counter()
    last_flush = start
    batch: list[tuple[str, float]] = []
    try:
        for i in range(n_tokens):
            due = start + i * interval
            now = time.perf_counter()
            if now < due:
                await asyncio.sleep(due - now)
            batch.append((f"tok-{i}", time.time()))
            if (time.perf_counter() - last_flush) >= flush_seconds:
                await _flush(conn, batch)
                batch = []
                last_flush = time.perf_counter()
        if batch:
            await _flush(conn, batch)
        # sentinel: consumer stops after seeing this
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO poc_chunks (payload, produced_at) VALUES (%s, %s)",
                (END_TOKEN, time.time()),
            )
            await conn.execute("SELECT pg_notify(%s, '')", (CHANNEL,))
    finally:
        await conn.close()


async def consume(dsn: str) -> list[float]:
    """LISTEN on a dedicated connection; fetch rows on a second connection."""
    listener = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    await listener.execute(f"LISTEN {CHANNEL}")
    fetcher = await psycopg.AsyncConnection.connect(dsn, autocommit=True)

    latencies: list[float] = []
    last_seq = 0

    async def drain() -> bool:
        nonlocal last_seq
        async with fetcher.cursor() as cur:
            await cur.execute(
                "SELECT seq, payload, produced_at FROM poc_chunks WHERE seq > %s ORDER BY seq",
                (last_seq,),
            )
            rows = await cur.fetchall()
        end_seen = False
        for seq, payload, produced_at in rows:
            last_seq = max(last_seq, seq)
            latencies.append(time.time() - produced_at)
            if payload == END_TOKEN:
                end_seen = True
        return end_seen

    received_end = False
    try:
        async for _notify in listener.notifies():  # event-driven wakeup
            if await drain():
                received_end = True
                break
        # safety: drain any residue
        if not received_end:
            await drain()
    finally:
        await listener.close()
        await fetcher.close()
    return latencies


def report(name: str, latencies_ms: list[float]) -> dict:
    lat = sorted(latencies_ms)
    if not lat:
        return {"scenario": name, "n": 0, "ok": False}
    def pct(p: float) -> float:
        return lat[min(len(lat) - 1, int(p * len(lat)))]
    return {
        "scenario": name,
        "n": len(lat),
        "p50_ms": round(pct(0.50), 2),
        "p95_ms": round(pct(0.95), 2),
        "p99_ms": round(pct(0.99), 2),
        "max_ms": round(lat[-1], 2),
        "mean_ms": round(statistics.mean(lat), 2),
    }


def start_embedded_pg() -> tuple[subprocess.Popen, str]:
    managed_py = resolve_pg_python()
    proc = subprocess.Popen(
        [str(managed_py), str(HERE / ".." / "poc1" / "start_pg.py"), str(ROOT / ".workbuddy" / "poc2-pgdata")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    line = proc.stdout.readline().strip()  # type: ignore[union-attr]
    if not line.startswith("postgresql"):
        err = proc.stderr.read() if proc.stderr else ""
        proc.kill()
        raise RuntimeError(f"pgserver failed: {err[-2000:]}")
    return proc, line


async def run_scenario(
    dsn: str,
    name: str,
    *,
    n_tokens: int,
    rate: float,
    flush_seconds: float,
) -> dict:
    setup_schema(dsn)
    consumer = asyncio.create_task(consume(dsn))
    producer = subprocess.Popen(
        [
            str(VENV_PY),
            str(Path(__file__).resolve()),
            "--role",
            "producer",
            "--dsn",
            dsn,
            "--n-tokens",
            str(n_tokens),
            "--rate",
            str(rate),
            "--flush-ms",
            str(int(flush_seconds * 1000)),
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    latencies = await asyncio.wait_for(consumer, timeout=180)
    rc = producer.wait(timeout=30)
    if rc != 0:
        err = producer.stderr.read()[-1500:] if producer.stderr else ""
        return {"scenario": name, "ok": False, "producer_error": err}
    rep = report(name, [x * 1000 for x in latencies])
    rep["ok"] = rep.get("n", 0) >= n_tokens  # every token must arrive
    return rep


async def main_async() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", default="main", choices=["main", "producer"])
    parser.add_argument("--dsn", default=os.environ.get("POC_PG_DSN", ""))
    parser.add_argument("--n-tokens", type=int, default=300)
    parser.add_argument("--rate", type=float, default=30.0)
    parser.add_argument("--flush-ms", type=int, default=50)
    args = parser.parse_args()

    if args.role == "producer":
        await produce(
            args.dsn,
            n_tokens=args.n_tokens,
            rate=args.rate,
            flush_seconds=args.flush_ms / 1000,
        )
        return 0

    pg_proc = None
    if not args.dsn:
        pg_proc, args.dsn = start_embedded_pg()
    try:
        results = []
        results.append(await run_scenario(args.dsn, "A_notify_per_token_30tps", n_tokens=300, rate=30.0, flush_seconds=0.0))
        results.append(await run_scenario(args.dsn, "B_flush50ms_30tps", n_tokens=300, rate=30.0, flush_seconds=0.05))
        results.append(await run_scenario(args.dsn, "C_flush50ms_100tps", n_tokens=600, rate=100.0, flush_seconds=0.05))
    finally:
        if pg_proc:
            pg_proc.send_signal(signal.SIGTERM)
            try:
                pg_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pg_proc.kill()

    ok = all(r.get("ok") for r in results)
    # channel A target: p95 <= 100ms increment
    passed = ok and all(r.get("p95_ms", 999) <= 100 for r in results if "flush50" in r["scenario"])
    out = {
        "verdict": "PASS" if passed else "FAIL",
        "target": "p95 <= 100ms (flush scenarios)",
        "results": results,
    }
    (HERE / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if passed else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))
