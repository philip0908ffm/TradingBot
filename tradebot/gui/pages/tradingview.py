"""TradingView: offizielles (kostenloses) Advanced-Chart-Widget von TradingView."""
from __future__ import annotations

import json
from urllib.parse import quote

from nicegui import app, ui

from tradebot.gui.components.market import broker_names
from tradebot.gui.components.tv_symbols import to_tv_symbol
from tradebot.gui.components.widgets import add_widget_css, is_dark
from tradebot.gui.layout import card, frame
from tradebot.gui.state import state

INTERVALS = {"1": "1 Min", "5": "5 Min", "15": "15 Min", "30": "30 Min", "60": "1 Std", "240": "4 Std",
             "D": "1 Tag", "W": "1 Woche", "M": "1 Monat"}
STUDIES = {
    "rsi": ("RSI", "RSI@tv-basicstudies"),
    "macd": ("MACD", "MACD@tv-basicstudies"),
    "bb": ("Bollinger", "BB@tv-basicstudies"),
    "ema": ("EMA", "MAExp@tv-basicstudies"),
}
STORE_KEY = "tradingview"

# Loads tv.js once (async, so the page also renders offline) and (re)creates the widget.
TV_JS = """
<script>
window.tbTV = function (opts) {
  // tv.js reads ?symbol=/?interval= from the page URL and would override our options
  if (window.location.search) history.replaceState(history.state, '', window.location.pathname);
  const show = (html) => { const el = document.getElementById(opts.container_id); if (el) el.innerHTML = html; };
  const fail = () => show(
    '<div style="height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;' +
    'color:var(--tb-muted);text-align:center;padding:24px">' +
    '<span class="material-icons" style="font-size:40px;opacity:.7">cloud_off</span>' +
    '<div style="font-size:.95rem;color:var(--tb-text);font-weight:600">TradingView ist nicht erreichbar</div>' +
    '<div style="font-size:.8rem;max-width:440px">Das TradingView-Widget benötigt eine Internetverbindung ' +
    '(s3.tradingview.com). Bitte Verbindung prüfen oder die Seite neu laden. Die eigenen Charts unter ' +
    '„Charts“ funktionieren unabhängig davon.</div></div>');
  const run = () => {
    const el = document.getElementById(opts.container_id);
    if (!el) return;
    el.innerHTML = '';
    try { new window.TradingView.widget(opts); } catch (e) { console.warn(e); fail(); }
  };
  if (window.TradingView && window.TradingView.widget) return run();
  let s = document.getElementById('tb-tvjs');
  if (s && s.dataset.failed) { s.remove(); s = null; }
  if (!s) {
    s = document.createElement('script');
    s.id = 'tb-tvjs'; s.async = true; s.src = 'https://s3.tradingview.com/tv.js';
    s.onerror = () => { s.dataset.failed = '1'; };
    document.head.appendChild(s);
  }
  show('<div style="height:100%;display:flex;align-items:center;justify-content:center;color:var(--tb-muted);' +
       'font-size:.85rem">TradingView wird geladen …</div>');
  const started = Date.now();
  const wait = () => {
    if (window.TradingView && window.TradingView.widget) return run();
    if (s.dataset.failed || Date.now() - started > 15000) return fail();
    setTimeout(wait, 150);
  };
  wait();
};
</script>
"""


def _prefs() -> dict:
    try:
        p = app.storage.user.get(STORE_KEY)
    except (RuntimeError, AssertionError):
        p = None
    return p if isinstance(p, dict) else {}


def _save(p: dict) -> None:
    try:
        app.storage.user[STORE_KEY] = p
    except (RuntimeError, AssertionError):
        pass


@ui.page("/tradingview")
async def page(symbol: str | None = None, broker: str | None = None, tv: str | None = None) -> None:
    add_widget_css()
    ui.add_head_html(TV_JS)
    st = state()
    names = broker_names()
    prefs = _prefs()
    watch = st.settings.watchlist
    dark = is_dark()

    if tv:
        tv_symbol = tv.strip().upper()
    elif symbol:
        brk = broker or next((w.broker for w in watch if w.symbol == symbol), None)
        tv_symbol = to_tv_symbol(symbol, brk)
    else:
        tv_symbol = prefs.get("symbol") or (to_tv_symbol(watch[0].symbol, watch[0].broker) if watch
                                            else "BINANCE:BTCUSDT")
    ctl = {
        "symbol": tv_symbol,
        "interval": prefs.get("interval") if prefs.get("interval") in INTERVALS else "60",
        "studies": [s for s in prefs.get("studies", ["rsi", "ema"]) if s in STUDIES],
    }
    watch_opts = {to_tv_symbol(w.symbol, w.broker): f"{w.symbol} · {names.get(w.broker, w.broker)}"
                  for w in watch}

    with frame("TradingView", "Offizielles TradingView-Chart mit allen Zeichenwerkzeugen und Indikatoren"):
        with card("gap-3").style("padding:14px 16px"):
            with ui.row().classes("w-full items-center gap-3"):
                wl = ui.select(watch_opts, label="Watchlist",
                               value=ctl["symbol"] if ctl["symbol"] in watch_opts else None).props(
                    "outlined dense options-dense").classes("w-56")
                sym_in = ui.input("TradingView-Symbol", value=ctl["symbol"],
                                  placeholder="z. B. NASDAQ:AAPL").props("outlined dense").classes("w-56")
                iv = ui.select(INTERVALS, label="Intervall", value=ctl["interval"]).props(
                    "outlined dense").classes("w-32")
                apply_btn = ui.button("Anzeigen", icon="play_arrow").props("unelevated no-caps color=primary")
                ui.space()
                with ui.row().classes("items-center gap-1"):
                    ui.label("Standard-Studien").classes("tb-section mr-2")
                    boxes = {}
                    for key, (label, _sid) in STUDIES.items():
                        boxes[key] = ui.checkbox(label, value=key in ctl["studies"]).props("dense").classes("mr-2")
            with ui.row().classes("w-full items-center gap-2 tb-divider pt-3 tb-muted text-xs no-wrap"):
                ui.icon("info", size="16px")
                ui.label("Kursdaten und Chart stammen direkt von TradingView und benötigen eine "
                         "Internetverbindung. Kurse können leicht von deiner Börse abweichen – der Bot handelt "
                         "immer mit den Daten des eingestellten Brokers.").classes("flex-1")
                open_link = ui.link("Auf tradingview.com öffnen", "#", new_tab=True).classes(
                    "text-primary whitespace-nowrap no-underline")

        with card("p-0").style("padding:0; overflow:hidden"):
            container = ui.element("div").classes("w-full").style(
                "height: calc(100vh - 300px); min-height: 480px")

    def render() -> None:
        opts = {
            "container_id": container.html_id,
            "autosize": True,
            "symbol": ctl["symbol"],
            "interval": ctl["interval"],
            "timezone": "Europe/Berlin",
            "theme": "dark" if dark else "light",
            "style": "1",
            "locale": "de_DE",
            "toolbar_bg": "#121620" if dark else "#ffffff",
            "enable_publishing": False,
            "allow_symbol_change": True,
            "withdateranges": True,
            "hide_side_toolbar": False,
            "details": False,
            "save_image": True,
            "studies": [STUDIES[s][1] for s in ctl["studies"]],
        }
        if dark:
            opts["backgroundColor"] = "#0f1218"
            opts["gridColor"] = "rgba(42, 46, 57, 0.35)"
        ui.run_javascript(f"window.tbTV && window.tbTV({json.dumps(opts)})")
        open_link.props(f'href="https://www.tradingview.com/chart/?symbol={quote(ctl["symbol"])}"')
        _save(dict(ctl))

    def apply() -> None:
        sym = (sym_in.value or "").strip().upper()
        if not sym:
            ui.notify("Bitte ein Symbol eingeben", type="warning")
            return
        ctl["symbol"] = sym
        ctl["interval"] = iv.value or "60"
        ctl["studies"] = [k for k, b in boxes.items() if b.value]
        wl.set_value(sym if sym in watch_opts else None)
        render()

    def on_watch(e) -> None:
        if e.value and e.value != ctl["symbol"]:
            sym_in.set_value(e.value)
            apply()

    wl.on_value_change(on_watch)
    apply_btn.on_click(apply)
    sym_in.on("keydown.enter", apply)
    iv.on_value_change(lambda _: apply())
    for b in boxes.values():
        b.on_value_change(lambda _: apply())

    await ui.context.client.connected()
    render()
