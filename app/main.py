"""Entrypoint for the SMTP-to-Graph relay."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading

from aiosmtpd.controller import Controller

from .config import load_config
from .graph import GraphClient
from .handler import RelayHandler


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SMTP -> Microsoft Graph relay")
    parser.add_argument(
        "-c",
        "--config",
        default=os.environ.get("RELAY_CONFIG", "/etc/smtp-relay/config.yaml"),
        help="Path to YAML config (env: RELAY_CONFIG)",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    _configure_logging(cfg.log_level)
    log = logging.getLogger("relay")

    if not cfg.allowed_networks:
        log.warning("No allowed_networks configured - the relay will reject ALL clients")
    if not cfg.allowed_senders:
        log.warning("No allowed_senders configured - the relay will reject ALL mail")

    graph = GraphClient(cfg.azure)
    handler = RelayHandler(cfg, graph)

    controller = Controller(
        handler,
        hostname=cfg.smtp.host,
        port=cfg.smtp.port,
        server_hostname=cfg.smtp.hostname,
        data_size_limit=cfg.smtp.max_message_size,
    )

    log.info(
        "Starting SMTP relay on %s:%d (hostname=%s, %d networks, %d senders)",
        cfg.smtp.host,
        cfg.smtp.port,
        cfg.smtp.hostname,
        len(cfg.allowed_networks),
        len(cfg.allowed_senders),
    )
    controller.start()

    # Controller runs its own thread + asyncio loop; main thread waits for a
    # signal here.
    stop = threading.Event()

    def _shutdown(signum, _frame):
        log.info("Received signal %s, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        stop.wait()
    finally:
        try:
            controller.stop()
        finally:
            graph.close()
        log.info("Shutdown complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
