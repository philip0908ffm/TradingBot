"""Backtest page (placeholder – being implemented)."""
from nicegui import ui

from tradebot.gui.layout import card, frame


@ui.page("/backtest")
def page() -> None:
    with frame("Backtest"):
        with card():
            ui.label("Diese Seite wird gerade gebaut.").classes("tb-muted")
