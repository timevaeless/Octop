"""`octop agent-worker` — run an agent-role worker (microservice scaling stage 2).

Consumes turn jobs from the PostgreSQL queue (procrastinate) and executes
them with the local ``AgentManager`` via ``InProcessExecutor``, streaming
chunks back through the events table + ``NOTIFY``.

Note: the worker boots the runtime with ``leader=False`` (stage 3) — it never
contends for cluster leadership, so background singletons (cron / IM ingress /
proactive care / knowledge recovery) run on api nodes only. The runtime DOES
pre-start every agent harness here (review P2-7): the worker is the only
turn executor, so lazy per-turn init belongs at boot, not on the first turn.
"""

from __future__ import annotations

import asyncio
import logging
import os

import click


async def _run_worker(*, concurrency: int, debug: bool) -> None:
    from octop.infra.agents.executor import InProcessExecutor
    from octop.infra.cluster.remote import AgentWorker
    from octop.infra.server import OctopServer

    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")

    srv = OctopServer(leader=False)  # workers never run background singletons
    await srv.start()
    assert srv.app_runtime is not None
    dsn = srv.config.queue_dsn if srv.config is not None else None
    if not dsn:
        await srv.stop()
        raise click.ClickException(
            "no queue DSN configured; set OCTOP_QUEUE_DSN (worker consumes the turn queue)"
        )

    worker = AgentWorker(
        dsn,
        InProcessExecutor(srv.app_runtime.agent_registry),
        concurrency=concurrency,
        worker_name=f"octop-agent-{os.getpid()}",
    )
    try:
        await worker.run_forever()
    finally:
        await srv.stop()


@click.command("agent-worker")
@click.option(
    "--concurrency",
    default=4,
    type=int,
    show_default=True,
    help="How many turns this worker may execute in parallel.",
)
@click.option("--debug", is_flag=True, default=False, help="Enable debug logging to stderr.")
def agent_worker(concurrency: int, debug: bool) -> None:
    """Run an agent-role worker: consume the turn queue, execute agents locally."""
    try:
        asyncio.run(_run_worker(concurrency=concurrency, debug=debug))
    except click.ClickException:
        raise
    except KeyboardInterrupt:
        raise SystemExit(0) from None
