"""PoC 3 orchestrator: cancellation + SSE reconnect replay semantics.

Phase 1 (mid-stream cancel, real agent):
  worker streams a turn against the slow fake LLM, cancels at N chunks;
  assert the stream terminates cleanly, the worker exits 0 (aclose OK),
  and a second process reads the thread history without corruption.

Phase 2 (disconnect / reconnect replay, channel level):
  producer writes tokens to PG (INSERT+NOTIFY per 50ms flush);
  a consumer "dies" twice mid-stream; a fresh consumer reconnects and
  resumes from seq > last_seen. Assert: exactly-once (no gap, no dup),
  and replay resumes at last_seq + 1 -- the semantics `list_from_seq`
  provides to the SSE Last-Event-ID path.

Usage: uv run python scripts/poc3/run_poc3.py  (or POC_PG_DSN=...)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
VENV_PY = ROOT / ".venv" / "bin" / "python"
WORKSPACE = ROOT / ".workbuddy" / "poc3-workspace"

sys.path.insert(0, str(HERE.parent / "support"))
from pg_python import resolve_pg_python  # noqa: E402

CHANNEL = "poc3_chunks"
END_TOKEN = "__END__"


# ----------------------------------------------------------------------
# embedded PG (same helper as poc2)
# ----------------------------------------------------------------------


def start_embedded_pg(data_dir: Path) -> tuple[subprocess.Popen, str]:
    managed_py = resolve_pg_python()
    proc = subprocess.Popen(
        [str(managed_py), str(HERE / ".." / "poc1" / "start_pg.py"), str(data_dir)],
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


# ----------------------------------------------------------------------
# Phase 1 helpers
# ----------------------------------------------------------------------


class FakeLLM:
    def __init__(self, token_delay_ms: int) -> None:
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = int(s.getsockname()[1])
        self.token_delay_ms = token_delay_ms
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> FakeLLM:
        self.proc = subprocess.Popen(
            [
                str(VENV_PY),
                str(HERE / ".." / "poc1" / "fake_llm.py"),
                "--port", str(self.port),
                "--token-delay-ms", str(self.token_delay_ms),
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        import socket as _s

        for _ in range(50):
            try:
                with _s.create_connection(("127.0.0.1", self.port), timeout=0.5):
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


def run_worker(role: str, dsn: str, base_url: str, **kw: object) -> tuple[dict, int]:
    cmd = [
        str(VENV_PY), str(HERE / "worker.py"),
        "--dsn", dsn, "--workspace", str(WORKSPACE),
        "--base-url", base_url, "--role", role,
    ]
    for k, v in kw.items():
        cmd += [f"--{k.replace('_', '-')}", str(v)]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=180)
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.startswith("{")]
    if not lines:
        return {"ok": False, "error": "no report", "stderr": (proc.stderr or "")[-2000:]}, proc.returncode
    rep = json.loads(lines[-1])
    return rep, proc.returncode


def phase1(dsn: str) -> dict:
    MARKER = "CANCEL-TEST-GAMMA-55"
    with FakeLLM(token_delay_ms=15) as llm:
        rep, rc = run_worker(
            "cancel", dsn, llm.base_url,
            thread="T-CANCEL", text=f"Please remember {MARKER}. Acknowledge.", cancel_after=15,
        )
        verify, vrc = run_worker(
            "verify", dsn, llm.base_url, thread="T-CANCEL", expect_marker=MARKER,
        )
    ok = bool(
        rep.get("ok") and rep.get("clean_end") and rc == 0 and verify.get("ok") and vrc == 0
    )
    return {"cancel_report": rep, "cancel_rc": rc, "verify": verify, "verify_rc": vrc, "ok": ok}


# ----------------------------------------------------------------------
# Phase 2 helpers
# ----------------------------------------------------------------------


async def producer(dsn: str, n_tokens: int, rate: float, flush_s: float) -> None:
    conn = await psycopg.AsyncConnection.connect(dsn)
    interval = 1.0 / rate
    start = time.perf_counter()
    batch: list[tuple[str, float]] = []
    last_flush = start
    try:
        for i in range(n_tokens):
            due = start + i * interval
            now = time.perf_counter()
            if now < due:
                await asyncio.sleep(due - now)
            batch.append((f"tok-{i}", time.time()))
            if (time.perf_counter() - last_flush) >= flush_s:
                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.executemany(
                            "INSERT INTO poc3_chunks (payload, produced_at) VALUES (%s, %s)", batch
                        )
                    await conn.execute("SELECT pg_notify(%s, '')", (CHANNEL,))
                batch = []
                last_flush = time.perf_counter()
        if batch:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.executemany(
                        "INSERT INTO poc3_chunks (payload, produced_at) VALUES (%s, %s)", batch
                    )
                await conn.execute("SELECT pg_notify(%s, '')", (CHANNEL,))
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO poc3_chunks (payload, produced_at) VALUES (%s, %s)", (END_TOKEN, time.time())
            )
            await conn.execute("SELECT pg_notify(%s, '')", (CHANNEL,))
    finally:
        await conn.close()


async def phase2(dsn: str) -> dict:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS poc3_chunks")
        conn.execute(
            """
            CREATE TABLE poc3_chunks (
                seq BIGSERIAL PRIMARY KEY,
                payload TEXT NOT NULL,
                produced_at DOUBLE PRECISION NOT NULL
            )
            """
        )

    N_TOKENS = 120
    RATE = 30.0
    FLUSH = 0.05
    DISCONNECT_AT = [6, 15]  # die after 6 rows, then after 15 more rows

    producer_task = asyncio.create_task(producer(dsn, N_TOKENS, RATE, FLUSH))

    received: list[int] = []
    events: list[dict] = []
    last_seq = 0
    disconnect_idx = 0
    rows_since_reconnect = 0
    end_seen = False

    while not end_seen:
        listener = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
        await listener.execute(f"LISTEN {CHANNEL}")
        fetcher = await psycopg.AsyncConnection.connect(dsn, autocommit=True)

        async def drain() -> bool:
            nonlocal last_seq, end_seen, rows_since_reconnect
            async with fetcher.cursor() as cur:
                await cur.execute(
                    "SELECT seq, payload FROM poc3_chunks WHERE seq > %s ORDER BY seq",
                    (last_seq,),
                )
                rows = await cur.fetchall()
            for seq, payload in rows:
                last_seq = max(last_seq, seq)
                rows_since_reconnect += 1
                received.append(seq)
                if payload == END_TOKEN:
                    end_seen = True
            return end_seen

        if disconnect_idx > 0:
            # catch-up fetch on reconnect: must resume exactly at last_seq+1
            await drain()
        if end_seen:
            await listener.close()
            await fetcher.close()
            break

        broke_off = False
        async for _notify in listener.notifies():
            if await drain():
                break
            if (
                disconnect_idx < len(DISCONNECT_AT)
                and rows_since_reconnect >= DISCONNECT_AT[disconnect_idx]
            ):
                events.append(
                    {
                        "event": "disconnect",
                        "died_after_rows": rows_since_reconnect,
                        "last_seq": last_seq,
                        "expected_resume_seq": last_seq + 1,
                    }
                )
                disconnect_idx += 1
                rows_since_reconnect = 0
                broke_off = True
                break
        await listener.close()
        await fetcher.close()
        if broke_off:
            await asyncio.sleep(0.2)  # node down window
            continue
        break  # end_seen

    await producer_task

    expected = list(range(1, last_seq + 1))
    no_gap_no_dup = received == expected
    resume_ok = all(
        e["expected_resume_seq"] == e["last_seq"] + 1 for e in events
    )
    total_rows = last_seq  # includes END token
    return {
        "produced_rows_total": total_rows,
        "received_count": len(received),
        "received_unique": len(set(received)),
        "no_gap_no_dup": no_gap_no_dup,
        "disconnect_events": events,
        "resume_ok": resume_ok,
        "ok": bool(no_gap_no_dup and resume_ok and end_seen),
    }


async def main_async() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=os.environ.get("POC_PG_DSN", ""))
    args = parser.parse_args()

    pg = None
    if not args.dsn:
        pg, args.dsn = start_embedded_pg(ROOT / ".workbuddy" / "poc3-pgdata")
    WORKSPACE.mkdir(parents=True, exist_ok=True)

    try:
        results = {
            "1_mid_stream_cancel": await asyncio.to_thread(phase1, args.dsn),
            "2_disconnect_replay": await phase2(args.dsn),
        }
    finally:
        if pg:
            pg.send_signal(signal.SIGTERM)
            try:
                pg.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pg.kill()

    ok = all(p.get("ok") for p in results.values())
    out = {"verdict": "PASS" if ok else "FAIL", "results": results}
    (HERE / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))
