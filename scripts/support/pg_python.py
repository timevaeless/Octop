"""Shared helper for the PoC / stage benchmark scripts (plan D12).

Resolves the interpreter that runs ``poc1/start_pg.py`` (embedded PG via the
``pgserver`` package). The project venv deliberately does NOT carry pgserver,
so the benchmark scripts previously hardcoded a machine-specific managed-env
path. Resolution order now:

1. ``OCTOP_PG_PYTHON`` — explicit interpreter with pgserver installed;
2. the current interpreter (``sys.executable``) if it can import pgserver;
3. error with setup instructions (loud, actionable, portable).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

__all__ = ["resolve_pg_python"]


def _has_pgserver(python: Path) -> bool:
    """True if *python* can import pgserver (cheap probe, no import)."""
    try:
        proc = subprocess.run(
            [str(python), "-c", "import pgserver"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def resolve_pg_python() -> Path:
    env = os.environ.get("OCTOP_PG_PYTHON", "").strip()
    if env:
        p = Path(env).expanduser()
        if not p.exists():
            raise RuntimeError(f"OCTOP_PG_PYTHON set but missing: {p}")
        return p

    cur = Path(sys.executable)
    if _has_pgserver(cur):
        return cur

    raise RuntimeError(
        "No pgserver-capable python found. Install pgserver somewhere and "
        "point OCTOP_PG_PYTHON at that interpreter, e.g.: "
        "`pip install pgserver` in a venv, then "
        "OCTOP_PG_PYTHON=/path/to/venv/bin/python"
    )
