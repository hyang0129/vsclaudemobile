"""Entry point for the interceptor component."""

import argparse
import asyncio
import logging

from .client import InterceptorClient


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Interceptor — bridges Claude Code sessions to the hub server"
    )
    parser.add_argument(
        "--hub-host",
        default="localhost",
        help="Hub server hostname (default: localhost)",
    )
    parser.add_argument(
        "--hub-port",
        type=int,
        default=8420,
        help="Hub server port (default: 8420)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    client = InterceptorClient(hub_host=args.hub_host, hub_port=args.hub_port)
    asyncio.run(client.run())


if __name__ == "__main__":
    main()
