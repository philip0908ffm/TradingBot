"""Shared page frame: header, navigation drawer, global event watcher (notifications + sounds).

Every page does::

    @ui.page('/route')
    def page():
        with frame('Titel', 'Untertitel'):
            ...content...
"""
from __future__ import annotations

from contextlib import contextmanager

from nicegui import ui

from tradebot.core.models import TradingMode
from tradebot.gui.state import FeedItem, state
from tradebot.gui.theme import apply_theme

NAV = [
    ("space_dashboard", "Übersicht", "/"),
    ("show_chart", "Charts", "/charts"),
    ("candlestick_chart", "TradingView", "/tradingview"),
    ("psychology", "Strategien", "/strategies"),
    ("history", "Backtest", "/backtest"),
    ("science", "Test-Trading", "/test"),
    ("bolt", "Live-Trading", "/live"),
    ("account_balance_wallet", "Wallet", "/wallet"),
    ("tune", "Einstellungen", "/settings"),
    ("receipt_long", "Protokoll", "/log"),
]

SOUND_FOR_KIND = {
    "buy": "buy",
    "take_profit": "take_profit",
    "stop_loss": "stop_loss",
    "trailing_stop": "stop_loss",
    "error": "error",
    "kill_switch": "error",
}
TRADE_KINDS = {"buy", "sell", "take_profit", "stop_loss", "trailing_stop", "max_bars", "manual"}


def sound_for(item: FeedItem) -> str | None:
    """Map a feed item to a sound key (matching AppSettings.sound_events keys)."""
    if item.kind in SOUND_FOR_KIND:
        return SOUND_FOR_KIND[item.kind]
    if item.kind in ("sell", "manual", "max_bars", "signal"):
        pnl = (item.data or {}).get("pnl")
        return "sell_loss" if pnl is not None and pnl < 0 else "sell_profit"
    if item.level == "error":
        return "error"
    return None


def play_sound(sound: str, force: bool = False) -> None:
    """Play a sound in the current client if enabled in settings (or force=True for previews)."""
    s = state().settings
    enabled = getattr(s, "sound_enabled", True)
    events = getattr(s, "sound_events", {}) or {}
    if force or (enabled and events.get(sound, True)):
        vol = float(getattr(s, "sound_volume", 0.6))
        ui.run_javascript(f"window.tbSound && window.tbSound({sound!r}, {vol})")


def notify_item(item: FeedItem) -> None:
    color = {"error": "negative", "warning": "warning"}.get(item.level)
    if item.kind in TRADE_KINDS:
        pnl = (item.data or {}).get("pnl")
        color = "positive" if item.kind == "buy" or (pnl is not None and pnl >= 0) else "negative"
    prefix = "LIVE · " if item.mode == "live" else "TEST · " if item.mode == "test" else ""
    ui.notify(prefix + item.message, color=color, position="bottom-right", timeout=6000,
              icon="bolt" if item.mode == "live" else None)


def _mode_chip(mode: TradingMode) -> None:
    running = state().manager.is_running(mode)
    live = mode == TradingMode.LIVE
    color = ("#ea3943" if live else "#16c784") if running else "#4b5563"
    with ui.row().classes("items-center gap-2 tb-chip cursor-pointer").on(
            "click", lambda: ui.navigate.to("/live" if live else "/test")):
        ui.element("span").classes("tb-live-dot" + (" tb-pulse" if running and live else "")).style(
            f"background:{color}")
        ui.label(("LIVE" if live else "TEST") + (" läuft" if running else " aus")).classes("text-xs")


@contextmanager
def frame(title: str, subtitle: str | None = None, active: str | None = None):
    st = state()
    settings = st.settings
    dark = getattr(settings, "theme", "dark") != "light"
    apply_theme(dark)
    ui.add_head_html('<script src="/tb-static/sounds.js"></script>')
    ui.page_title(f"{title} · TradingBot")
    route = active or ui.context.client.page.path

    with ui.header().classes("items-center justify-between px-4 py-2"):
        with ui.row().classes("items-center gap-3"):
            ui.button(icon="menu", on_click=lambda: drawer.toggle()).props("flat round dense color=grey-6")
            ui.icon("insights", size="26px").classes("text-primary")
            ui.label("TradingBot").classes("text-lg font-semibold tracking-tight")

        @ui.refreshable
        def chips() -> None:
            with ui.row().classes("items-center gap-2"):
                _mode_chip(TradingMode.TEST)
                _mode_chip(TradingMode.LIVE)

        with ui.row().classes("items-center gap-1"):
            chips()

            def toggle_sound() -> None:
                s = st.settings
                s.sound_enabled = not getattr(s, "sound_enabled", True)
                st.save_settings(s)
                sound_btn.props(f"icon={'volume_up' if s.sound_enabled else 'volume_off'}")
                if s.sound_enabled:
                    play_sound("info", force=True)

            sound_btn = ui.button(icon="volume_up" if getattr(settings, "sound_enabled", True) else "volume_off",
                                  on_click=toggle_sound).props("flat round dense color=grey-6").tooltip("Töne an/aus")

            def toggle_theme() -> None:
                s = st.settings
                s.theme = "light" if getattr(s, "theme", "dark") != "light" else "dark"
                st.save_settings(s)
                ui.navigate.reload()

            ui.button(icon="dark_mode" if dark else "light_mode", on_click=toggle_theme).props(
                "flat round dense color=grey-6").tooltip("Hell/Dunkel")

    with ui.left_drawer(value=True, fixed=True).props("width=232 breakpoint=900").classes("p-0") as drawer:
        with ui.column().classes("w-full gap-0 py-3 tb-nav"):
            for icon, label, path in NAV:
                is_active = route == path
                with ui.item(on_click=lambda p=path: ui.navigate.to(p)).classes(
                        "tb-active" if is_active else ""):
                    with ui.item_section().props("avatar").classes("min-w-0 pr-3"):
                        ui.icon(icon, size="20px")
                    with ui.item_section():
                        ui.label(label).classes("text-sm font-medium")
        ui.space()
        ui.label("Kein Anlagerat. Trading birgt Verlustrisiken.").classes("tb-muted text-xs px-5 pb-4")

    # global watcher: new bot events -> toast + sound; refresh header chips
    last = {"seq": st.feed.last_seq()}

    def watch() -> None:
        items = st.feed.since(last["seq"])
        if items:
            last["seq"] = items[-1].seq
            sounds_played = set()
            for item in items[-5:]:
                if item.kind in TRADE_KINDS or item.level in ("error", "warning") or item.kind in SOUND_FOR_KIND:
                    notify_item(item)
                snd = sound_for(item)
                if snd and snd not in sounds_played:
                    sounds_played.add(snd)
                    play_sound(snd)
        chips.refresh()

    ui.timer(1.5, watch)

    with ui.column().classes("w-full max-w-[1500px] mx-auto px-6 py-5 gap-5"):
        with ui.column().classes("gap-0"):
            ui.label(title).classes("tb-title")
            if subtitle:
                ui.label(subtitle).classes("tb-muted text-sm")
        yield


def card(classes: str = ""):
    return ui.card().classes(f"tb-card w-full {classes}")


def kpi(label: str, value: str, sub: str | None = None, value_classes: str = ""):
    """A small KPI tile; returns (value_label, sub_label) for later updates."""
    with ui.card().classes("tb-card gap-1 min-w-[170px] flex-1"):
        ui.label(label).classes("tb-kpi-label")
        v = ui.label(value).classes(f"tb-kpi-value {value_classes}")
        s = ui.label(sub or "").classes("tb-muted text-xs")
    return v, s
