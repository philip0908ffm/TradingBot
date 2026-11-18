"""Strategien page (placeholder – being implemented)."""
from nicegui import ui

from tradebot.gui.layout import card, frame


@ui.page("/strategies")
def page() -> None:
    with frame("Strategien"):
        with card():
            ui.label("Diese Seite wird gerade gebaut.").classes("tb-muted")
