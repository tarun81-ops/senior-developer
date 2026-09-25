"""``python -m backend.api`` — start the local API on 127.0.0.1.

    python -m backend.api --port 8765       # by hand: prints a token for curl
    python -m backend.api --port 0 --token-stdin --exit-with-stdin   # the desktop shell

The bind address is fixed at ``127.0.0.1`` on purpose: the API can run
model-authored code, so it must never be reachable from the network (D17).

Two ways to get a token:

* **By hand** (default): one is generated here and printed once, for curl.
* **``--token-stdin``** (the desktop shell, D25): the shell generates the token
  and writes it as the first line of this process's stdin, a pipe private to
  the two processes. Nothing secret is printed. The only stdout line the shell
  reads is ``SDA_READY {"port": N}``.

Either way the token is never written to disk and never read from argv or the
environment, where other processes of the same user could see it.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import threading
from pathlib import Path

import uvicorn

from backend.api.app import create_app
from backend.api.security import new_api_token

#: Fixed loopback address. Not a default — there is no flag to change it.
BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

#: What a token handed in on stdin must look like: URL-safe, at least 256 bits
#: when base64url-encoded (43 chars). A short or odd token is refused, never used.
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43,128}$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.api",
        description="Start the local FastAPI server for the desktop UI.",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"TCP port (default {DEFAULT_PORT}; 0 = any free port)",
    )
    parser.add_argument(
        "--root", type=Path, default=None,
        help="Project root holding config/ (and the prompt files it names), data/ and "
        "workspace/ (default: this repo)",
    )
    parser.add_argument(
        "--token-stdin", action="store_true",
        help="Read the API token from the first line of stdin instead of generating one",
    )
    parser.add_argument(
        "--exit-with-stdin", action="store_true",
        help="Shut down gracefully when stdin closes (the desktop shell's lifeline)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 0 <= args.port <= 65535:
        print("error: --port must be between 0 and 65535", file=sys.stderr)
        return 2
    if args.token_stdin:
        token = sys.stdin.readline().strip()
        if not TOKEN_PATTERN.match(token):
            print("error: --token-stdin needs a 43-128 char URL-safe token", file=sys.stderr)
            return 2
    else:
        token = new_api_token()
    app = create_app(root=args.root, token=token)

    # Bind *and listen* before announcing the port: a client that connects the
    # moment it reads the line waits in the backlog instead of being refused.
    # With --port 0 this is also the only way to know the real port.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((BIND_HOST, args.port))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))

    if args.exit_with_stdin:
        # When the shell exits or crashes its end of the pipe closes: stop the
        # way Ctrl+C would, so running commands are cancelled and their process
        # trees killed (D20) instead of orphaned by a hard kill.
        def watch() -> None:
            sys.stdin.read()
            server.should_exit = True

        threading.Thread(target=watch, daemon=True).start()

    if args.token_stdin:
        print(f"SDA_READY {json.dumps({'port': port})}", flush=True)
    else:
        print(f"API token for this launch: {token}", flush=True)
        print(f"Serving on http://{BIND_HOST}:{port} (loopback only)", flush=True)
    server.run(sockets=[sock])
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
