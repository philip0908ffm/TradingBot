"""Logging: rotating file log (logs/tradebot.log) + console."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from tradebot.core.settings import SettingsStore

_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_MARK = "_tradebot_handler"


def setup_logging(store: SettingsStore, level: int = logging.INFO, console: bool = True) -> None:
    """Configure root logging once (idempotent)."""
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        if getattr(h, _MARK, False):
            root.removeHandler(h)
            h.close()
    fmt = logging.Formatter(_FMT)
    store.logs_dir.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(store.logs_dir / "tradebot.log", maxBytes=2_000_000,
                             backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    setattr(fh, _MARK, True)
    root.addHandler(fh)
    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(fmt)
        setattr(ch, _MARK, True)
        root.addHandler(ch)
    # third-party libraries are chatty
    for noisy in ("urllib3", "ccxt", "yfinance", "peewee", "httpx", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
