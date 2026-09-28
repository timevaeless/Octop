"""PoC 1 orchestrator: harness cross-process checkpoint correctness.

Phases:
  1. cross-process handoff  -- process A runs turn 1 on thread T;
                               process B runs turn 2 on the SAME thread;
                               verify server-side that B's LLM request
                               carried A's messages (checkpoint round-trip),
                               and that B's agent reads A's marker from the
                               checkpointer via aget_history.
  2. concurrent, different threads -- two processes run turns on T2/T3 at the
                               same time (lazy-load / remote-executor case).
  3. concurrent, same thread -- two processes race turns on one thread to
                               observe real conflict behavior.

Usage:
  uv run python scripts/poc1/run_poc.py --dsn <postgres dsn>
  (or POC_PG_DSN env; without a DSN it tries embedded pgserver)
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
VENV_PY = ROOT / ".venv" / "bin" / "python"

sys.path.insert(0, str(HERE.parent / "support"))
from pg_python import resolve_pg_python  # noqa: E402

PROJECT_PY = Path(sys.executable)

WORKSPACE = ROOT / ".workbuddy" / "poc1-workspace"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class FakeLLM:
    def __init__(self, log_path: Path) -> None:
        self.port = free_port()
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> FakeLLM:
        self.proc = subprocess.Popen(
            [
                str(VENV_PY if VENV_PY.exists() else PROJECT_PY),
                str(HERE / "fake_llm.py"),
                "--port",
                str(self.port),
                "--log",
                str(self.log_path),
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base = f"http://127.0.0.1:{self.port}"
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


def run_worker(
    role: str,
    dsn: str,
    base_url: str,
    *,
    thread: str = "poc-thread",
    text: str = "",
    expect_marker: str = "",
    timeout: int = 180,
) -> dict:
    cmd = [
        str(VENV_PY if VENV_PY.exists() else PROJECT_PY),
        str(HERE / "worker.py"),
        "--dsn",
        dsn,
        "--workspace",
        str(WORKSPACE),
        "--base-url",
        base_url,
        "--role",
        role,
        "--thread",
        thread,
        "--text",
        text,
        "--expect-marker",
        expect_marker,
    ]
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    stdout = (proc.stdout or "").strip()
    lines = [ln for ln in stdout.splitlines() if ln.startswith("{")]
    if not lines:
        return {
            "ok": False,
            "role": role,
            "error": f"no report (rc={proc.returncode})",
            "stderr": (proc.stderr or "")[-2000:],
            "stdout": stdout[-2000:],
        }
    report = json.loads(lines[-1])
    report["_rc"] = proc.returncode
    if proc.returncode != 0:
        report["ok"] = False
        report["stderr"] = (proc.stderr or "")[-2000:]
    return report


def read_llm_log(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    entries = []
    for line in log_path.read_text().splitlines():
        line = line.strip()
        if line.startswith("{"):
            entries.append(json.loads(line))
    return entries


def user_texts(entry: dict) -> list[str]:
    return [m.get("content") or "" for m in entry.get("messages", []) if m.get("role") == "user"]


def start_embedded_pg() -> tuple[subprocess.Popen, str]:
    """Start pgserver (bundled PG binaries) via a pgserver-capable interpreter."""
    managed_py = resolve_pg_python()
    proc = subprocess.Popen(
        [str(managed_py), str(HERE / "start_pg.py"), str(ROOT / ".workbuddy" / "poc1-pgdata")],
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=os.environ.get("POC_PG_DSN", ""))
    args = parser.parse_args()

    pg_proc = None
    if not args.dsn:
        pg_proc, args.dsn = start_embedded_pg()

    WORKSPACE.mkdir(parents=True, exist_ok=True)
    llm_log = WORKSPACE / "llm_requests.log"
    if llm_log.exists():
        llm_log.unlink()

    results: dict = {"dsn": args.dsn.split("@")[-1], "phases": {}}

    try:
        with FakeLLM(llm_log) as llm:
            MARKER_1 = "CODEWORD-BANANA-42"
            MARKER_2 = "SECOND-TURN-NEBULA-77"

            # ---- Phase 1: cross-process handoff on one thread ----
            p1a = run_worker(
                "turn", args.dsn, llm.base_url,
                thread="T1", text=f"Please remember this codeword: {MARKER_1}. Just acknowledge.",
            )
            p1b = run_worker(
                "turn", args.dsn, llm.base_url,
                thread="T1", text=f"Another fact to remember: {MARKER_2}. Acknowledge.",
            )
            p1v = run_worker(
                "verify", args.dsn, llm.base_url,
                thread="T1", expect_marker=MARKER_1,
            )
            # server-side proof: the request of the 2nd turn must contain turn-1 text
            entries = read_llm_log(llm_log)
            turn_requests = [
                e for e in entries if any(MARKER_2 in t for t in user_texts(e))
            ]
            carried = any(
                any(MARKER_1 in t for t in user_texts(e)) for e in turn_requests
            )
            phase1 = {
                "workerA": p1a,
                "workerB": p1b,
                "verify_history": p1v,
                "server_saw_history_carry": carried,
            }
            phase1["ok"] = bool(
                p1a.get("ok") and p1b.get("ok") and p1v.get("ok") and carried
            )
            results["phases"]["1_cross_process_handoff"] = phase1

            # ---- Phase 2: concurrent different threads (lazy-load case) ----
            procs = {}
            for tid, marker in (("T2", "PARALLEL-ALPHA-01"), ("T3", "PARALLEL-BETA-02")):
                procs[tid] = (
                    marker,
                    subprocess.Popen(
                        [
                            str(VENV_PY if VENV_PY.exists() else PROJECT_PY),
                            str(HERE / "worker.py"),
                            "--dsn", args.dsn,
                            "--workspace", str(WORKSPACE),
                            "--base-url", llm.base_url,
                            "--role", "turn",
                            "--thread", tid,
                            "--text", f"Parallel marker: {marker}. Acknowledge.",
                        ],
                        cwd=ROOT,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    ),
                )
            phase2_workers = {}
            for tid, (marker, proc) in procs.items():
                out, err = proc.communicate(timeout=180)
                lines = [ln for ln in (out or "").splitlines() if ln.startswith("{")]
                rep = json.loads(lines[-1]) if lines else {
                    "ok": False,
                    "error": f"no report rc={proc.returncode}",
                    "stderr": (err or "")[-2000:],
                }
                rep["marker_seen"] = marker in json.dumps(rep)
                phase2_workers[tid] = rep
            phase2 = {"workers": phase2_workers}
            phase2["ok"] = all(w.get("ok") for w in phase2_workers.values())
            results["phases"]["2_concurrent_different_threads"] = phase2

            # ---- Phase 3: concurrent SAME thread (race observation) ----
            race_procs = []
            for i, marker in enumerate(("RACE-ONE-X9", "RACE-TWO-Y8")):
                race_procs.append(
                    subprocess.Popen(
                        [
                            str(VENV_PY if VENV_PY.exists() else PROJECT_PY),
                            str(HERE / "worker.py"),
                            "--dsn", args.dsn,
                            "--workspace", str(WORKSPACE),
                            "--base-url", llm.base_url,
                            "--role", "turn",
                            "--thread", "T-RACE",
                            "--text", f"Racing writer #{i}: {marker}. Acknowledge.",
                        ],
                        cwd=ROOT,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )
            phase3_workers = []
            for proc in race_procs:
                out, err = proc.communicate(timeout=180)
                lines = [ln for ln in (out or "").splitlines() if ln.startswith("{")]
                rep = json.loads(lines[-1]) if lines else {
                    "ok": False,
                    "error": f"no report rc={proc.returncode}",
                    "stderr": (err or "")[-2000:],
                }
                phase3_workers.append(rep)
            both_ok = all(w.get("ok") for w in phase3_workers)
            phase3 = {"workers": phase3_workers, "both_succeeded": both_ok}
            # Phase 3 records observed behavior; both_ok is NOT required to pass
            # (the design already mandates per-thread queueing_lock serialization).
            phase3["ok"] = True
            results["phases"]["3_concurrent_same_thread"] = phase3
    finally:
        if pg_proc:
            pg_proc.send_signal(signal.SIGTERM)
            try:
                pg_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pg_proc.kill()

    ok = all(p.get("ok") for p in results["phases"].values())
    results["verdict"] = "PASS" if ok else "FAIL"
    out_path = HERE / "result.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(json.dumps({"verdict": results["verdict"], "report": str(out_path)}))
    print(json.dumps(results["phases"], ensure_ascii=False, indent=2)[:6000])
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
