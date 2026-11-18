"""GUI application bootstrap (NiceGUI)."""
from __future__ import annotations

import logging
from pathlib import Path

from nicegui import app, ui

STATIC = Path(__file__).resolve().parent / "static"

log = logging.getLogger(__name__)


def create_app() -> None:
    """Register static files, pages and lifecycle hooks (idempotent per process)."""
    from tradebot.core.logging_setup import setup_logging
    from tradebot.gui.state import state

    st = state()
    setup_logging(st.store)
    app.add_static_files("/tb-static", str(STATIC))

    # importing the page modules registers their @ui.page routes
    from tradebot.gui.pages import (  # noqa: F401
        backtest, charts, dashboard, log as log_page, settings, strategies, trading, tradingview, wallet,
    )

    async def shutdown() -> None:
        try:
            await st.manager.stop_all()
        except Exception:  # noqa: BLE001
            log.exception("Fehler beim Stoppen der Bots")

    app.on_shutdown(shutdown)


def run(native: bool = False, port: int = 8080, show: bool = True, reload: bool = False) -> None:
    create_app()
    kwargs = dict(title="TradingBot", favicon="📈", port=port, show=show, reload=reload,
                  language="de", reconnect_timeout=10.0, storage_secret="tradebot-local")
    if native:
        kwargs.update(native=True, window_size=(1440, 920))
        if port == 8080:  # default: let NiceGUI pick a free port for the desktop window
            from nicegui import native as native_mod
            kwargs["port"] = native_mod.find_open_port()
    ui.run(**kwargs)
