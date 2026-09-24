"""``python -m backend.api`` — start the local API on 127.0.0.1.

    python -m backend.api --port 8765
    python -m backend.api --port 0          # let Windows pick a free port

The bind address is fixed at ``127.0.0.1`` on purpose: the API can run
model-authored code, so it must never be reachable from the network. The
per-launch token is printed once, on stdout, for the desktop shell to read; it
is never written to disk (D17).
"""

from __future__ import annotations

import argparse
import sys

import uvicorn

from backend.api.app import create_app
from backend.api.security import new_api_token

#: Fixed loopback address. Not a default — there is no flag to change it.
BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.api",
        description="Start the local FastAPI server for the desktop UI.",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"TCP port (default {DEFAULT_PORT})"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 0 <= args.port <= 65535:
        print("error: --port must be between 0 and 65535", file=sys.stderr)
        return 2
    token = new_api_token()
    app = create_app(token=token)
    print(f"API token for this launch: {token}", flush=True)
    print(f"Serving on http://{BIND_HOST}:{args.port} (loopback only)", flush=True)
    uvicorn.run(app, host=BIND_HOST, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
