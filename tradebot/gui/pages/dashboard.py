"""Übersicht: Kontostände TEST/LIVE, Markt-Überblick der Watchlist und letzte Ereignisse."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from urllib.parse import urlencode

from nicegui import run, ui

from tradebot.core.models import TradingMode, WatchItem
from tradebot.gui.components.market import broker_names, friendly_error, snapshot
from tradebot.gui.components.widgets import (
    add_widget_css,
    badge,
    empty_state,
    kind_icon,
    kind_label,
    local_time,
    mode_badge,
    pill,
    sparkline_svg,
)
from tradebot.gui.layout import card, frame
from tradebot.gui.state import state
from tradebot.gui.theme import fmt_money, fmt_pct, fmt_price, pnl_class

log = logging.getLogger(__name__)

MARKET_REFRESH = 20.0
STATUS_REFRESH = 5.0


class AccountCard:
    """KPI card for one trading mode (updated in place)."""

    def __init__(self, mode: TradingMode) -> None:
        self.mode = mode
        live = mode == TradingMode.LIVE
        with card("gap-4"):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                with ui.row().classes("items-center gap-3 no-wrap"):
                    color = "#ea3943" if live else "#16c784"
                    with ui.element("div").classes("tb-icon-box").style(
                            f"background: color-mix(in srgb, {color} 15%, transparent)"):
                        ui.icon("bolt" if live else "science", size="22px").style(f"color:{color}")
                    with ui.column().classes("gap-0"):
                        ui.label("Live-Konto" if live else "Test-Konto").classes("text-base font-semibold")
                        ui.label("Echtes Geld über den Broker" if live else
                                 "Papierhandel mit virtuellem Geld").classes("tb-muted text-xs")
                with ui.row().classes("items-center gap-2 no-wrap"):
                    self.kill = pill("Notschalter aktiv", "neg")
                    self.kill.visible = False
                    self.status = pill("…", "neutral")
            with ui.element("div").classes("grid grid-cols-2 xl:grid-cols-4 gap-x-4 gap-y-3 w-full"):
                self.equity = self._kpi("Kontowert")
                self.pnl = self._kpi("Gewinn / Verlust")
                self.positions = self._kpi("Positionen")
                self.realized = self._kpi("Realisiert")
            self.spark = ui.html("").classes("w-full")
            with ui.row().classes("w-full items-center justify-between tb-divider pt-3 no-wrap"):
                self.strategy = ui.label("").classes("tb-muted text-xs truncate")
                ui.button("Live-Trading" if live else "Test-Trading", icon="arrow_forward",
                          on_click=lambda: ui.navigate.to("/live" if live else "/test")).props(
                    "flat dense no-caps color=primary size=sm")

    @staticmethod
    def _kpi(label: str):
        with ui.column().classes("gap-0 min-w-0"):
            ui.label(label).classes("tb-kpi-label")
            v = ui.label("–").classes("text-lg font-semibold tracking-tight")
            s = ui.label("").classes("tb-muted text-xs")
        return v, s

    def update(self, st: dict | None, error: str | None = None) -> None:
        if st is None:
            self.status.set_text("Unbekannt")
            self.strategy.set_text(error or "")
            return
        running = bool(st.get("running"))
        self.status.set_text("Läuft" if running else "Gestoppt")
        tone = ("neg" if self.mode == TradingMode.LIVE else "pos") if running else "neutral"
        self.status.classes(replace=f"tb-pill tb-pill-{tone}")
        self.kill.visible = bool(st.get("kill_switch"))

        eq = st.get("equity")
        self.equity[0].set_text(fmt_money(eq) if eq is not None else "–")
        start = st.get("start_equity")
        self.equity[1].set_text(f"Start {fmt_money(start)}" if start else "noch keine Daten")
        pnl, pct = st.get("pnl"), st.get("pnl_pct")
        self.pnl[0].set_text(fmt_money(pnl) if pnl is not None else "–")
        self.pnl[0].classes(replace=f"text-lg font-semibold tracking-tight {pnl_class(pnl)}")
        self.pnl[1].set_text(fmt_pct(pct) if pct is not None else "")
        self.pnl[1].classes(replace=f"text-xs {pnl_class(pct) or 'tb-muted'}")
        positions = st.get("positions") or []
        mx = st.get("max_open_positions")
        self.positions[0].set_text(str(len(positions)))
        upnl = sum((p.get("unrealized_pnl") or 0.0) for p in positions)
        self.positions[1].set_text((f"max. {mx} · " if mx else "") +
                                   (f"offen {fmt_money(upnl)}" if positions else "keine offen"))
        realized = st.get("realized_pnl") or 0.0
        n_trades = len(st.get("closed_trades") or [])
        self.realized[0].set_text(fmt_money(realized))
        self.realized[0].classes(replace=f"text-lg font-semibold tracking-tight {pnl_class(realized)}")
        self.realized[1].set_text(f"{n_trades} Trade" if n_trades == 1 else f"{n_trades} Trades")
        hist = [x[1] for x in (st.get("equity_history") or []) if len(x) > 1 and x[1] is not None][-200:]
        self.spark.set_content(sparkline_svg(hist, height=42) if len(hist) > 1 else (
            '<div style="height:42px;display:flex;align-items:center;justify-content:center;'
            'border:1px dashed var(--tb-border);border-radius:10px" class="tb-muted text-xs">'
            'Noch kein Kontoverlauf</div>'))
        strat = st.get("strategy")
        self.strategy.set_text(f"Strategie: {strat}" if strat else "Keine Strategie zugeordnet")


class MarketCard:
    def __init__(self, item: WatchItem, broker_label: str) -> None:
        self.item = item
        q = urlencode({"symbol": item.symbol, "broker": item.broker})
        with card("gap-2 tb-hover p-4").style("padding:16px"):
            with ui.row().classes("w-full items-start justify-between no-wrap"):
                with ui.column().classes("gap-1 min-w-0"):
                    ui.label(item.symbol).classes("text-base font-semibold truncate")
                    with ui.row().classes("gap-1"):
                        badge(broker_label)
                        badge({"crypto": "Krypto", "stock": "Aktie", "etf": "ETF"}.get(
                            item.asset_class.value, item.asset_class.value))
                self.change = pill("", "neutral")
                self.change.visible = False
                self.change_skel = ui.skeleton(type="QChip", width="64px", height="22px").classes("tb-skel")
            with ui.column().classes("gap-0 w-full"):
                self.price = ui.label("").classes("text-xl font-semibold tracking-tight")
                self.price_skel = ui.skeleton(type="text", width="45%", height="30px").classes("tb-skel")
                self.price.visible = False
                self.sub = ui.label("").classes("tb-muted text-xs")
            self.spark = ui.html("").classes("w-full")
            self.spark_skel = ui.skeleton(type="rect", height="48px").classes("w-full tb-skel")
            with ui.row().classes("w-full gap-2 pt-1"):
                ui.button("Chart", icon="show_chart", on_click=lambda: ui.navigate.to(f"/charts?{q}")).props(
                    "outline dense no-caps size=sm color=primary").classes("px-2")
                ui.button("Backtest", icon="history", on_click=lambda: ui.navigate.to(f"/backtest?{q}")).props(
                    "flat dense no-caps size=sm color=grey-6").classes("px-2")

    def _loaded(self) -> None:
        for sk in (self.change_skel, self.price_skel, self.spark_skel):
            sk.visible = False
        self.price.visible = True
        self.change.visible = True

    def update(self, snap: dict | None, error: str | None) -> None:
        self._loaded()
        if snap is None:
            if self.price.text:  # keep last good values, just mark as stale
                self.sub.set_text("Aktualisierung fehlgeschlagen")
                self.sub.tooltip(error or "")
                return
            self.price.set_text("–")
            self.change.set_text("keine Daten")
            self.change.classes(replace="tb-pill tb-pill-neutral")
            self.sub.set_text(error or "Keine Daten")
            self.spark.set_content(
                '<div style="height:48px;display:flex;align-items:center" class="tb-muted text-xs">'
                '<span class="material-icons" style="font-size:16px;margin-right:6px">cloud_off</span>'
                "Daten nicht verfügbar</div>")
            return
        ch = snap["change_pct"]
        self.price.set_text(fmt_price(snap["price"]))
        self.change.set_text(("▲ " if (ch or 0) >= 0 else "▼ ") + fmt_pct(ch))
        self.change.classes(replace=f"tb-pill tb-pill-{'pos' if (ch or 0) >= 0 else 'neg'}")
        self.sub.set_text(f"48 h: H {fmt_price(snap['high'])} · T {fmt_price(snap['low'])} · "
                          f"{local_time(snap['time'], '%d.%m. %H:%M')}")
        self.spark.set_content(sparkline_svg(snap["spark"], height=48))


async def _status(mode: TradingMode) -> dict:
    return await run.io_bound(state().manager.status, mode)


@ui.page("/")
async def page() -> None:
    add_widget_css()
    st = state()
    names = broker_names()
    with frame("Übersicht", "Konten, Märkte und Bot-Aktivität auf einen Blick"):
        with ui.element("div").classes("grid grid-cols-1 lg:grid-cols-2 gap-5 w-full"):
            accounts = {m: AccountCard(m) for m in (TradingMode.TEST, TradingMode.LIVE)}

        # ---- market overview -----------------------------------------------------------
        items = [w for w in st.settings.watchlist if w.enabled]
        with ui.row().classes("w-full items-end justify-between mt-1"):
            with ui.column().classes("gap-0"):
                ui.label("Markt-Überblick").classes("text-base font-semibold")
                ui.label("Aktive Werte der Watchlist · Stundenkerzen der letzten 48 Stunden").classes(
                    "tb-muted text-xs")
            with ui.row().classes("items-center gap-2"):
                updated = ui.label("").classes("tb-muted text-xs")
                refresh_btn = ui.button(icon="refresh").props("flat round dense color=grey-6").tooltip(
                    "Jetzt aktualisieren")
                ui.button("Watchlist", icon="edit", on_click=lambda: ui.navigate.to("/settings?tab=watchlist")
                          ).props("flat dense no-caps size=sm color=grey-6")
        market_cards: list[MarketCard] = []
        if items:
            with ui.element("div").classes(
                    "grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4 gap-4 w-full"):
                for it in items:
                    market_cards.append(MarketCard(it, names.get(it.broker, it.broker)))
        else:
            with card():
                empty_state("playlist_add", "Die Watchlist ist leer",
                            "Füge in den Einstellungen Symbole hinzu, um hier Kurse zu sehen.")

        # ---- recent events ------------------------------------------------------------
        with card("gap-2"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("Letzte Ereignisse").classes("text-base font-semibold")
                ui.button("Alle anzeigen", icon="arrow_forward", on_click=lambda: ui.navigate.to("/log")).props(
                    "flat dense no-caps size=sm color=primary")
            events_box = ui.column().classes("w-full gap-0")

    shown = {"seq": -1}

    def render_events() -> None:
        last = st.feed.last_seq()
        if last == shown["seq"]:
            return
        shown["seq"] = last
        events_box.clear()
        items_ = list(reversed(st.feed.recent(12)))
        with events_box:
            if not items_:
                empty_state("notifications_none", "Noch keine Ereignisse",
                            "Sobald ein Bot im Test- oder Live-Modus läuft, erscheinen hier Käufe, "
                            "Verkäufe und Meldungen.")
                return
            for i, ev in enumerate(items_):
                icon, color = kind_icon(ev.kind, ev.level)
                pnl = (ev.data or {}).get("pnl")
                with ui.row().classes("w-full items-center gap-3 py-2 no-wrap" + ("" if i == 0 else " tb-divider")):
                    ui.icon(icon, size="18px").style(f"color:{color}")
                    ui.label(local_time(ev.time, "%d.%m. %H:%M:%S")).classes("tb-muted text-xs tb-mono w-[118px]")
                    mode_badge(ev.mode)
                    ui.label(kind_label(ev.kind)).classes("text-xs font-semibold w-[92px] truncate")
                    if ev.symbol:
                        ui.label(ev.symbol).classes("text-xs font-medium w-[86px] truncate")
                    ui.label(ev.message).classes("text-sm truncate flex-1 min-w-0")
                    if pnl is not None:
                        ui.label(fmt_money(pnl)).classes(f"text-xs font-semibold {pnl_class(pnl)}")

    async def refresh_status() -> None:
        for mode, ac in accounts.items():
            try:
                ac.update(await _status(mode))
            except Exception as exc:  # noqa: BLE001
                log.debug("Status %s nicht verfügbar: %s", mode, exc)
                ac.update(None, friendly_error(exc))
        render_events()

    busy = {"market": False}

    async def refresh_market() -> None:
        if busy["market"] or not market_cards:
            return
        busy["market"] = True
        refresh_btn.props("loading")
        try:
            async def one(mc: MarketCard) -> None:
                try:
                    snap = await run.io_bound(snapshot, mc.item)
                    mc.update(snap, None)
                except Exception as exc:  # noqa: BLE001
                    log.debug("Kurs %s: %s", mc.item.symbol, exc)
                    mc.update(None, friendly_error(exc))

            await asyncio.gather(*(one(mc) for mc in market_cards))
            updated.set_text(f"Stand {datetime.now().strftime('%H:%M:%S')}")
        finally:
            busy["market"] = False
            refresh_btn.props(remove="loading")

    refresh_btn.on_click(refresh_market)
    ui.timer(0.05, refresh_status, once=True)
    ui.timer(0.1, refresh_market, once=True)
    ui.timer(STATUS_REFRESH, refresh_status)
    ui.timer(MARKET_REFRESH, refresh_market)
