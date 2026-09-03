"""Logging configuration: readable in a terminal, greppable in a file."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from analyst.core.config import PROJECT_ROOT

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-28s | %(message)s"


def configure(level: str = "INFO", to_file: bool = True) -> None:
    root = logging.getLogger()
    if root.handlers:  # idempotent: CLI and scheduler may both call this
        return
    root.setLevel(level.upper())

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(_FORMAT, "%H:%M:%S"))
    root.addHandler(console)

    if to_file:
        log_dir = Path(PROJECT_ROOT) / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "analyst.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(handler)

    # third-party noise that adds nothing at INFO
    for noisy in ("httpx", "httpcore", "yfinance", "peewee", "urllib3", "apscheduler.executors"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
