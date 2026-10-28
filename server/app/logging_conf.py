"""Structured-ish logging setup shared by the web app, CLI and scheduler."""
from __future__ import annotations

import logging
import sys


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # APScheduler is chatty at INFO; keep it a notch quieter.
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
