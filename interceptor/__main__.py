"""Entry point for the interceptor component."""

import argparse
import asyncio
import sys

from loguru import logger

from .client import InterceptorClient

LOG_DIR = "/tmp/vsclaudemobile"


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
        default="DEBUG",
        choices=["TRACE", "DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: DEBUG)",
    )
    args = parser.parse_args()

    # Remove default stderr handler and re-add with chosen level
    logger.remove()
    logger.add(sys.stderr, level=args.log_level, format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> — "
        "<level>{message}</level>"
    ))
    # Always log everything to file at TRACE level for troubleshooting
    logger.add(
        f"{LOG_DIR}/interceptor.log",
        level="TRACE",
        rotation="10 MB",
        retention="3 days",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} — {message}",
    )

    logger.info("Interceptor starting: hub={}:{}, log_level={}, log_file={}/interceptor.log",
                 args.hub_host, args.hub_port, args.log_level, LOG_DIR)

    client = InterceptorClient(hub_host=args.hub_host, hub_port=args.hub_port)
    asyncio.run(client.run())


if __name__ == "__main__":
    main()
