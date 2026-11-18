"""TradingView page (placeholder – being implemented)."""
from nicegui import ui

from tradebot.gui.layout import card, frame


@ui.page("/tradingview")
def page() -> None:
    with frame("TradingView"):
        with card():
            ui.label("Diese Seite wird gerade gebaut.").classes("tb-muted")
