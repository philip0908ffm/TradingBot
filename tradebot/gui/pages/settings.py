"""Einstellungen page (placeholder – being implemented)."""
from nicegui import ui

from tradebot.gui.layout import card, frame


@ui.page("/settings")
def page() -> None:
    with frame("Einstellungen"):
        with card():
            ui.label("Diese Seite wird gerade gebaut.").classes("tb-muted")
