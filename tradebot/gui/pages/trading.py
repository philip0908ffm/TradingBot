"""Test-/Live-Trading pages (placeholder – being implemented)."""
from nicegui import ui

from tradebot.gui.layout import card, frame


@ui.page("/test")
def test_page() -> None:
    with frame("Test-Trading"):
        with card():
            ui.label("Diese Seite wird gerade gebaut.").classes("tb-muted")


@ui.page("/live")
def live_page() -> None:
    with frame("Live-Trading"):
        with card():
            ui.label("Diese Seite wird gerade gebaut.").classes("tb-muted")
