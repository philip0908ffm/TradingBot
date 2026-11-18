"""Charts page (placeholder – being implemented)."""
from nicegui import ui

from tradebot.gui.layout import card, frame


@ui.page("/charts")
def page() -> None:
    with frame("Charts"):
        with card():
            ui.label("Diese Seite wird gerade gebaut.").classes("tb-muted")
