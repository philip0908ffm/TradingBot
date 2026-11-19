"""Charts: Kerzen-Chart (TradingView Lightweight Charts) mit Indikatoren und Bot-Aktivität."""
from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import urlencode

from nicegui import app, run, ui

from tradebot.brokers import BROKERS
from tradebot.core.models import TIMEFRAMES
from tradebot.gui.components.bot_overlay import overlay, statuses
from tradebot.gui.components.indicator_picker import PICKER_CSS, IndicatorPicker, german_name, indicator_label, sanitize
from tradebot.gui.components.market import broker_names, friendly_error, is_crypto, load_candles
from tradebot.gui.components.tv_chart import TVChart, build_payload
from tradebot.gui.components.widgets import add_widget_css, is_dark
from tradebot.gui.layout import card, frame
from tradebot.gui.state import state
from tradebot.gui.theme import fmt_pct, fmt_price, pnl_class

log = logging.getLogger(__name__)

TF_LABELS = {"1m": "1 Minute", "5m": "5 Minuten", "15m": "15 Minuten", "30m": "30 Minuten", "1h": "1 Stunde",
             "4h": "4 Stunden", "1d": "1 Tag", "1w": "1 Woche"}
CANDLE_COUNTS = [200, 300, 500, 1000, 1500, 2000]
AUTO_REFRESH = 15.0
DEFAULT_INDICATORS = [{"key": "ema", "params": {"period": 50}}, {"key": "rsi", "params": {"period": 14}}]
STORE_KEY = "charts"


def _prefs() -> dict:
    try:
        prefs = app.storage.user.get(STORE_KEY)
    except (RuntimeError, AssertionError):  # storage not available
        prefs = None
    if not isinstance(prefs, dict):
        prefs = {"indicators": DEFAULT_INDICATORS}
    prefs["indicators"] = sanitize(prefs.get("indicators", DEFAULT_INDICATORS))
    return prefs


def _save_prefs(prefs: dict) -> None:
    try:
        app.storage.user[STORE_KEY] = prefs
    except (RuntimeError, AssertionError):
        pass


@ui.page("/charts")
async def page(symbol: str | None = None, broker: str | None = None) -> None:
    add_widget_css()
    ui.add_css(PICKER_CSS)
    ui.add_css(".tb-chart-ctrl .q-field__control { min-height: 40px; }")
    st = state()
    prefs = _prefs()
    names = broker_names()
    watch = st.settings.watchlist

    if symbol:
        sym = symbol.strip()
        brk = broker if broker in BROKERS else next(
            (w.broker for w in watch if w.symbol == sym), st.settings.default_broker)
    elif prefs.get("symbol"):
        sym, brk = prefs["symbol"], prefs.get("broker") or st.settings.default_broker
    elif watch:
        sym, brk = watch[0].symbol, watch[0].broker
    else:
        sym, brk = "BTC/USDT", "binance"
    if brk not in BROKERS:
        brk = "binance"
    ctl = {
        "symbol": sym, "broker": brk,
        "timeframe": prefs.get("timeframe") if prefs.get("timeframe") in TIMEFRAMES else "1h",
        "candles": int(prefs.get("candles") or 500),
        "volume": bool(prefs.get("volume", True)),
        "auto": bool(prefs.get("auto", False)),
        "indicators": prefs["indicators"],
    }

    def persist() -> None:
        _save_prefs({k: ctl[k] for k in ("symbol", "broker", "timeframe", "candles", "volume", "auto", "indicators")})

    watch_opts = {f"{w.broker}|{w.symbol}": f"{w.symbol} · {names.get(w.broker, w.broker)}" for w in watch}

    with frame("Charts", "Kerzen-Chart mit Indikatoren und Bot-Aktivität"):
        # ---- controls -----------------------------------------------------------------
        with card("gap-3 tb-chart-ctrl").style("padding:14px 16px"):
            with ui.row().classes("w-full items-center gap-3"):
                wl = ui.select(watch_opts, label="Watchlist",
                               value=f"{brk}|{sym}" if f"{brk}|{sym}" in watch_opts else None).props(
                    "outlined dense options-dense").classes("w-52")
                sym_in = ui.input("Symbol", value=sym).props("outlined dense").classes("w-32")
                brk_sel = ui.select(names, label="Datenquelle", value=brk).props("outlined dense").classes("w-36")
                tf_sel = ui.select(TF_LABELS, label="Zeitrahmen", value=ctl["timeframe"]).props(
                    "outlined dense").classes("w-32")
                n_sel = ui.select({n: f"{n}" for n in CANDLE_COUNTS}, label="Kerzen",
                                  value=ctl["candles"] if ctl["candles"] in CANDLE_COUNTS else 500).props(
                    "outlined dense").classes("w-28")
                load_btn = ui.button("Laden", icon="refresh").props("unelevated no-caps color=primary")
            with ui.row().classes("w-full items-center gap-2 tb-divider pt-3"):
                ui.label("Indikatoren").classes("tb-section mr-1")
                chips_box = ui.row().classes("items-center gap-1")
                add_btn = ui.button("Indikator", icon="add").props("flat dense no-caps color=primary")
                ui.space()
                vol_sw = ui.switch("Volumen", value=ctl["volume"]).props("dense")
                auto_sw = ui.switch("Auto-Aktualisierung (15 s)", value=ctl["auto"]).props("dense")

        # ---- chart --------------------------------------------------------------------
        with card("gap-0 p-0").style("padding:0; overflow:hidden"):
            with ui.row().classes("w-full items-center gap-4 px-4 py-3 no-wrap"):
                title = ui.label("").classes("text-base font-semibold whitespace-nowrap")
                price_lbl = ui.label("").classes("text-base font-semibold tracking-tight whitespace-nowrap")
                change_lbl = ui.label("").classes("text-sm font-medium whitespace-nowrap")
                change_lbl.tooltip("Veränderung über den angezeigten Zeitraum")
                range_lbl = ui.label("").classes("tb-muted text-xs whitespace-nowrap hidden xl:block")
                ui.space()
                bot_lbl = ui.label("").classes("tb-muted text-xs whitespace-nowrap truncate")
                status = ui.label("").classes("tb-muted text-xs whitespace-nowrap")
                spinner = ui.spinner(size="sm")
                ui.button("In TradingView öffnen", icon="open_in_new",
                          on_click=lambda: ui.navigate.to(
                              "/tradingview?" + urlencode({"symbol": ctl["symbol"], "broker": ctl["broker"]}))
                          ).props("outline dense no-caps size=sm color=primary").classes("px-2 shrink-0")
                ui.button("Backtest", icon="history",
                          on_click=lambda: ui.navigate.to(
                              "/backtest?" + urlencode({"symbol": ctl["symbol"], "broker": ctl["broker"]}))
                          ).props("flat dense no-caps size=sm color=grey-6").classes("px-2 shrink-0")
            with ui.element("div").classes("w-full relative").style(
                    "height: calc(100vh - 395px); min-height: 440px; border-top: 1px solid var(--tb-border)"):
                chart = TVChart(theme="dark" if is_dark() else "light").classes("w-full h-full")
                with ui.column().classes("absolute inset-0 items-center justify-center gap-2").style(
                        "pointer-events:none; z-index:10; background: color-mix(in srgb, var(--tb-surface) 82%, transparent)") as msg_box:
                    msg_icon = ui.icon("cloud_off", size="38px").classes("tb-muted")
                    msg = ui.label("").classes("text-sm tb-muted text-center max-w-[460px]")
                msg_box.visible = False
            with ui.row().classes("w-full items-center gap-4 px-4 py-2 tb-divider text-xs tb-muted"):
                for color, text in (("#16c784", "Kauf"), ("#ea3943", "Verkauf / SL"), ("#3b82f6", "Einstieg"),
                                    ("#16c784", "Take-Profit"), ("#f59e0b", "Trailing-Stop")):
                    with ui.row().classes("items-center gap-1"):
                        ui.element("span").style(
                            f"width:10px;height:3px;border-radius:2px;background:{color};display:inline-block")
                        ui.label(text)
                ui.space()
                ui.label("Markierungen zeigen Trades der Bots (TEST/LIVE) für dieses Symbol.")

    picker = IndicatorPicker(lambda key, params: add_indicator(key, params))
    add_btn.on_click(picker.open)

    # ---- behaviour ------------------------------------------------------------------------
    def render_chips() -> None:
        chips_box.clear()
        with chips_box:
            if not ctl["indicators"]:
                ui.label("keine").classes("tb-muted text-xs")
            for i, ind in enumerate(ctl["indicators"]):
                chip = ui.chip(indicator_label(ind["key"], ind["params"]), removable=True, color=None).props(
                    "dense outline square").classes("text-xs")
                chip.tooltip(german_name(ind["key"]))
                chip.on_value_change(lambda e, i=i: remove_indicator(i) if not e.value else None)

    def add_indicator(key: str, params: dict) -> None:
        ctl["indicators"].append({"key": key, "params": params})
        persist()
        render_chips()
        ui.notify(f"{indicator_label(key, params)} hinzugefügt", type="positive", position="bottom-right")
        background_reload(keep=True)

    def remove_indicator(i: int) -> None:
        if 0 <= i < len(ctl["indicators"]):
            ctl["indicators"].pop(i)
            persist()
            render_chips()
            background_reload(keep=True)

    busy = {"on": False, "pending": False}
    cache: dict = {"df": None, "sig": None}

    def show_msg(text: str | None, icon: str = "cloud_off") -> None:
        msg_box.visible = bool(text)
        msg.set_text(text or "")
        msg_icon.props(f"name={icon}")

    async def reload(keep: bool = False, fetch: bool = True) -> None:
        if busy["on"]:
            busy["pending"] = True
            return
        busy["on"] = True
        spinner.visible = True
        status.set_text("Lade …")
        try:
            sym_, brk_, tf_, n_ = ctl["symbol"], ctl["broker"], ctl["timeframe"], int(ctl["candles"])
            sig = (sym_, brk_, tf_, n_)
            if not sym_:
                show_msg("Bitte ein Symbol eingeben.", "search")
                return
            title.set_text(f"{sym_} · {names.get(brk_, brk_)} · {tf_}")
            item = next((w for w in watch if w.symbol == sym_ and w.broker == brk_), None)
            crypto = is_crypto(sym_, brk_, item.asset_class if item else None)
            try:
                if fetch or cache["sig"] != sig or cache["df"] is None:
                    df = await run.io_bound(load_candles, brk_, sym_, tf_, n_, crypto)
                    cache.update(df=df, sig=sig)
                df = cache["df"]
            except Exception as exc:  # noqa: BLE001
                log.info("Chartdaten %s/%s: %s", brk_, sym_, exc)
                if not keep:
                    chart.set_payload({"candles": []})
                    price_lbl.set_text("")
                    change_lbl.set_text("")
                    range_lbl.set_text("")
                show_msg(f"Kursdaten konnten nicht geladen werden: {friendly_error(exc)}")
                status.set_text("Fehler")
                return
            if df is None or df.empty:
                chart.set_payload({"candles": []})
                show_msg(f"Keine Kursdaten für {sym_} ({tf_}) verfügbar.", "search_off")
                status.set_text("")
                return
            stats = await run.io_bound(statuses)
            markers, plines, summary = overlay(sym_, brk_, df, tf_, stats)
            specs = [(i["key"], i["params"]) for i in ctl["indicators"]]
            try:
                payload = await run.io_bound(build_payload, df, specs, markers, plines, None, ctl["volume"], keep)
            except Exception as exc:  # noqa: BLE001 - e.g. too few candles for an indicator
                log.warning("Indikatorberechnung fehlgeschlagen: %s", exc)
                ui.notify(f"Indikator konnte nicht berechnet werden: {exc}", type="warning")
                payload = await run.io_bound(build_payload, df, [], markers, plines, None, ctl["volume"], keep)
            chart.set_payload(payload)
            show_msg(None)
            last, first = float(df["close"].iloc[-1]), float(df["open"].iloc[0])
            ch = (last / first - 1) * 100 if first else None
            price_lbl.set_text(fmt_price(last))
            change_lbl.set_text(fmt_pct(ch))
            change_lbl.classes(replace=f"text-sm font-medium whitespace-nowrap {pnl_class(ch)}")
            range_lbl.set_text(f"{len(df)} Kerzen · H {fmt_price(float(df['high'].max()))} · "
                               f"T {fmt_price(float(df['low'].min()))}")
            parts = []
            if summary["trades"]:
                parts.append(f"{summary['trades']} Bot-Trades")
            if summary["positions"]:
                parts.append(f"{summary['positions']} offene Position(en)")
            bot_lbl.set_text(" · ".join(parts))
            status.set_text(f"Stand {datetime.now().strftime('%H:%M:%S')}")
        finally:
            busy["on"] = False
            spinner.visible = False
            if busy["pending"]:
                busy["pending"] = False
                background_reload(keep=False)

    def background_reload(keep: bool = False, fetch: bool = True) -> None:
        ui.timer(0, lambda: reload(keep=keep, fetch=fetch), once=True)

    def apply_inputs() -> None:
        new_sym = (sym_in.value or "").strip()
        if not new_sym:
            ui.notify("Bitte ein Symbol eingeben", type="warning")
            return
        if "/" not in new_sym and brk_sel.value in ("binance", "kraken"):
            ui.notify("Krypto-Börsen erwarten Paare wie BTC/USDT", type="warning")
        ctl.update(symbol=new_sym.upper(),
                   broker=brk_sel.value, timeframe=tf_sel.value, candles=int(n_sel.value or 500))
        key = f"{ctl['broker']}|{ctl['symbol']}"
        wl.set_value(key if key in watch_opts else None)
        persist()
        background_reload()

    def on_watch(e) -> None:
        if not e.value:
            return
        b, s = e.value.split("|", 1)
        if s == ctl["symbol"] and b == ctl["broker"]:
            return
        sym_in.set_value(s)
        brk_sel.set_value(b)
        apply_inputs()

    wl.on_value_change(on_watch)
    load_btn.on_click(apply_inputs)
    sym_in.on("keydown.enter", apply_inputs)
    tf_sel.on_value_change(lambda _: apply_inputs())
    n_sel.on_value_change(lambda _: apply_inputs())

    def on_volume(e) -> None:
        ctl["volume"] = bool(e.value)
        persist()
        background_reload(keep=True, fetch=False)

    def on_auto(e) -> None:
        ctl["auto"] = bool(e.value)
        persist()
        timer.active = ctl["auto"]

    vol_sw.on_value_change(on_volume)
    auto_sw.on_value_change(on_auto)
    timer = ui.timer(AUTO_REFRESH, lambda: reload(keep=True), active=ctl["auto"])

    render_chips()
    persist()
    ui.timer(0.05, reload, once=True)
