from __future__ import annotations

import argparse

from .config import load_config, set_config_path
from . import logs
from . import respcache


def main():
    parser = argparse.ArgumentParser(
        description="LocalGateway — local OpenAI-compatible token gateway"
    )
    parser.add_argument("--config", "-c", default="config.json", help="Path to config.json")
    parser.add_argument("--host", default=None, help="Override host")
    parser.add_argument("--port", "-p", type=int, default=None, help="Override port")
    parser.add_argument(
        "--no-supervisor",
        action="store_true",
        help="Run the gateway worker directly (no web start/stop control)",
    )
    args = parser.parse_args()

    set_config_path(args.config)
    logs.set_db_path("data/usage.db")
    respcache.set_db_path("data/usage.db")
    config = load_config()
    host = args.host or config.server.host
    port = args.port or config.server.port

    if args.no_supervisor:
        from .worker import run_worker

        run_worker(args.config, host, port)
    else:
        from .supervisor import run_supervisor

        run_supervisor(args.config, host, port)


if __name__ == "__main__":
    main()
