"""Übersicht page (placeholder – being implemented)."""
from nicegui import ui

from tradebot.gui.layout import card, frame


@ui.page("/")
def page() -> None:
    with frame("Übersicht"):
        with card():
            ui.label("Diese Seite wird gerade gebaut.").classes("tb-muted")
