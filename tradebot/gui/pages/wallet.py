"""Wallet page (placeholder – being implemented)."""
from nicegui import ui

from tradebot.gui.layout import card, frame


@ui.page("/wallet")
def page() -> None:
    with frame("Wallet"):
        with card():
            ui.label("Diese Seite wird gerade gebaut.").classes("tb-muted")
