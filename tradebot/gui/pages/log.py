"""Protokoll page (placeholder – being implemented)."""
from nicegui import ui

from tradebot.gui.layout import card, frame


@ui.page("/log")
def page() -> None:
    with frame("Protokoll"):
        with card():
            ui.label("Diese Seite wird gerade gebaut.").classes("tb-muted")
