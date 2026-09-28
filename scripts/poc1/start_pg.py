"""Start an embedded PostgreSQL via pgserver and print its URI, then idle.

Used by the PoC orchestrator. Requires pgserver installed in the *managed*
python env (kept out of the project venv on purpose).
"""

from __future__ import annotations

import signal
import sys

import pgserver


def main() -> None:
    data_dir = sys.argv[1]
    server = pgserver.get_server(data_dir)
    print(server.get_uri(), flush=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.pause()


if __name__ == "__main__":
    main()
