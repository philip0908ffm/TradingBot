"""TradingBot – start the GUI.

    python main.py              # opens the app in your browser (http://localhost:8080)
    python main.py --native     # opens the app in its own desktop window (needs pywebview)
    python main.py --port 8090  # use another port
    python main.py --selftest   # quick self check (used by the Windows build), exits 0/1
"""
from __future__ import annotations

import argparse
import multiprocessing
import sys


def selftest() -> int:
    """Import every module and run a tiny backtest on synthetic data."""
    try:
        import numpy as np
        import pandas as pd

        from tradebot.backtest.engine import run_backtest
        from tradebot.brokers import BROKERS  # noqa: F401
        from tradebot.engine.manager import BotManager  # noqa: F401
        from tradebot.gui import app as gui_app  # noqa: F401
        from tradebot.indicators import REGISTRY
        from tradebot.strategy.presets import PRESETS

        idx = pd.date_range("2024-01-01", periods=400, freq="h", tz="UTC")
        close = 100 + np.cumsum(np.random.default_rng(1).normal(0, 1, len(idx)))
        df = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close,
                           "volume": 1000.0}, index=idx)
        run_backtest(PRESETS[0], df)
        assert len(REGISTRY) >= 40
        print(f"Selftest OK – {len(REGISTRY)} Indikatoren, {len(PRESETS)} Strategie-Vorlagen")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"Selftest FEHLGESCHLAGEN: {exc!r}", file=sys.stderr)
        return 1


def main() -> None:
    # windowed .exe builds have no console: give print/logging somewhere to write
    import os
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")  # noqa: SIM115
    parser = argparse.ArgumentParser(description="TradingBot")
    parser.add_argument("--native", action="store_true", help="eigenes Fenster statt Browser")
    parser.add_argument("--browser", action="store_true", help="im Browser öffnen (auch in der .exe)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--no-show", action="store_true", help="Browser nicht automatisch öffnen")
    parser.add_argument("--selftest", action="store_true")
    args, _ = parser.parse_known_args()

    if args.selftest:
        sys.exit(selftest())

    frozen = getattr(sys, "frozen", False)
    native = (args.native or frozen) and not args.browser
    if native:
        try:
            import webview  # noqa: F401
        except ImportError:
            print("pywebview nicht installiert – starte im Browser.")
            native = False

    # keep NiceGUI's per-browser storage next to the other app data, not in the working dir
    from tradebot.core.settings import default_home
    os.environ.setdefault("NICEGUI_STORAGE_PATH", str(default_home() / "gui-storage"))

    from tradebot.gui.app import run
    run(native=native, port=args.port, show=not args.no_show)


if __name__ in {"__main__", "__mp_main__"}:
    multiprocessing.freeze_support()
    # child processes (native window, process pool) import this module too – only the
    # main process starts the app
    if multiprocessing.current_process().name == "MainProcess":
        main()
