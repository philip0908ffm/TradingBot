"""Backtest page: run a strategy on historical data, show verdict, KPIs, charts, trades; optimizer tab."""
from __future__ import annotations

import copy
import math
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import pandas as pd
from nicegui import app, run, ui

from tradebot.backtest.engine import run_backtest
from tradebot.backtest.metrics import METRIC_LABELS
from tradebot.backtest.optimize import RESULT_METRICS
from tradebot.brokers import BrokerError, create_broker, fetch_history
from tradebot.core.models import TIMEFRAMES
from tradebot.gui.components.optimizer import (
    ParamCandidate,
    apply_values,
    count_combinations,
    grid_values,
    param_candidates,
    run_grid,
)
from tradebot.gui.components.strategy_editor import help_icon
from tradebot.gui.components.strategy_labels import (
    EXIT_REASON_LABELS,
    TIMEFRAME_LABELS,
    TIMEFRAME_OPTIONS,
    currency_symbol,
    fmt_de,
    quote_currency,
)
from tradebot.gui.components.tv_chart import TVChart, build_payload
from tradebot.gui.layout import card, frame
from tradebot.gui.state import state
from tradebot.gui.theme import fmt_money, fmt_num, fmt_pct, fmt_price
from tradebot.strategy.engine import required_indicators, validate, warmup_bars
from tradebot.strategy.models import Strategy
from tradebot.strategy.presets import PRESETS

FORM_KEY = "backtest_form"
DRAFT_KEY = "strategy_draft"
MAX_RUNS = 500
DATA_SOURCES = {"binance": "Binance", "kraken": "Kraken", "yahoo": "Yahoo Finance", "alpaca": "Alpaca"}
QUICK_RANGES = [("1M", 30), ("3M", 91), ("6M", 182), ("1J", 365), ("2J", 730), ("5J", 1826)]
# Yahoo intraday history limits (days)
YAHOO_LIMITS = {"1m": 29, "5m": 59, "15m": 59, "30m": 59, "1h": 729, "4h": 729}
MAIN_KPIS = ["total_return_pct", "buy_hold_return_pct", "max_drawdown_pct", "win_rate_pct", "profit_factor",
             "num_trades", "sharpe"]
KPI_HELP = {
    "total_return_pct": "Gewinn oder Verlust der Strategie im Zeitraum, nach Gebühren und Slippage.",
    "buy_hold_return_pct": "Vergleich: einmal zu Beginn kaufen und bis zum Ende halten.",
    "max_drawdown_pct": "Größter zwischenzeitlicher Verlust vom bisherigen Höchststand des Kapitals.",
    "win_rate_pct": "Anteil der Trades mit Gewinn.",
    "profit_factor": "Summe der Gewinne geteilt durch Summe der Verluste. Über 1 = profitabel, ab 1,5 gut.",
    "num_trades": "Abgeschlossene Trades. Unter 20 Trades ist das Ergebnis statistisch wenig aussagekräftig.",
    "sharpe": "Rendite im Verhältnis zur Schwankung (annualisiert). Über 1 gut, über 2 sehr gut.",
}

PAGE_CSS = """
.tb-verdict { border-radius: 16px; padding: 22px 24px; border: 1px solid var(--tb-border); }
.tb-verdict.pos { background: linear-gradient(135deg, color-mix(in srgb, var(--tb-pos) 16%, var(--tb-surface)),
                  var(--tb-surface) 70%); border-color: color-mix(in srgb, var(--tb-pos) 40%, transparent); }
.tb-verdict.neg { background: linear-gradient(135deg, color-mix(in srgb, var(--tb-neg) 16%, var(--tb-surface)),
                  var(--tb-surface) 70%); border-color: color-mix(in srgb, var(--tb-neg) 40%, transparent); }
.tb-verdict-title { font-size: 1.35rem; font-weight: 650; letter-spacing: -0.01em; line-height: 1.3; }
.tb-kpi-grid { display: grid; gap: 12px; grid-template-columns: repeat(2, minmax(0, 1fr)); }
@media (min-width: 900px) { .tb-kpi-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); } }
@media (min-width: 1700px) { .tb-kpi-grid { grid-template-columns: repeat(7, minmax(0, 1fr)); } }
.tb-kpi-tile { background: var(--tb-surface); border: 1px solid var(--tb-border); border-radius: 12px;
               padding: 12px 14px; }
.tb-kpi-tile .tb-kpi-value { font-size: 1.3rem; }
.tb-metric-row { display: flex; justify-content: space-between; gap: 12px; padding: 7px 0;
                 border-bottom: 1px dashed var(--tb-border); font-size: .85rem; }
.tb-quick { border: 1px solid var(--tb-border); border-radius: 10px; padding: 2px; height: 40px; }
.tb-quick .q-btn { color: var(--tb-muted) !important; min-width: 38px; padding: 0 6px; border-radius: 8px; font-weight: 600; }
.tb-warn { border: 1px solid color-mix(in srgb, #f59e0b 45%, transparent);
           background: color-mix(in srgb, #f59e0b 8%, var(--tb-surface)); }
.tb-param-row { display: grid; grid-template-columns: 28px minmax(170px,1fr) 56px 86px 86px 86px 40px;
                gap: 8px; align-items: center; padding: 6px 4px; border-bottom: 1px solid var(--tb-border); }
.tb-param-row.head { font-size: .72rem; font-weight: 600; text-transform: uppercase; letter-spacing: .04em;
                     color: var(--tb-muted); border-bottom: 1px solid var(--tb-border); }
.tb-param-row.off .tb-param-in { opacity: .45; }
.tb-legend-dot { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }
.tb-table .q-table td, .tb-table .q-table th { padding: 6px 10px; }
.tb-table .q-table tbody td { font-size: .82rem; }
.tb-opt-table .q-table th { white-space: normal; line-height: 1.25; vertical-align: bottom; }
"""

CELL_SLOT = r"""
<q-td :props="props"
      :class="props.col.cls ? ((props.row[props.col.name] > 0) ? 'tb-pos' : ((props.row[props.col.name] < 0) ? 'tb-neg' : '')) : ''">
  {{ props.row[props.col.name + '_s'] !== undefined ? props.row[props.col.name + '_s'] : props.value }}
</q-td>
"""


# ------------------------------------------------------------------------------------------
# helpers (no UI)
# ------------------------------------------------------------------------------------------

def fmt_metric(key: str, v, cur: str = "") -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "–"
    kind = METRIC_LABELS.get(key, ("", "num"))[1]
    if key == "max_drawdown_pct":
        return fmt_pct(-abs(v), 2) if v else "0,00 %"
    if kind == "pct":
        signed = key in ("total_return_pct", "buy_hold_return_pct", "cagr_pct", "avg_trade_pct", "avg_win_pct",
                         "avg_loss_pct", "best_trade_pct", "worst_trade_pct")
        return fmt_pct(v, 2, sign=signed)
    if kind == "money":
        return fmt_money(v, cur)
    if kind == "int":
        return fmt_num(v, 0)
    return fmt_num(v, 2)


def metric_class(key: str, v) -> str:
    if v is None:
        return ""
    if key in ("total_return_pct", "buy_hold_return_pct", "cagr_pct", "avg_trade_pct", "expectancy"):
        return "tb-pos" if v > 0 else "tb-neg" if v < 0 else ""
    if key == "max_drawdown_pct":
        return "tb-neg" if v > 0 else ""
    if key == "profit_factor":
        return "tb-pos" if v >= 1 else "tb-neg"
    return ""


def fmt_dt(t) -> str:
    t = pd.Timestamp(t)
    if t.tzinfo is not None:
        t = t.tz_convert("UTC")
    return t.strftime("%d.%m.%Y %H:%M") if (t.hour or t.minute) else t.strftime("%d.%m.%Y")


def fmt_duration(delta: timedelta) -> str:
    s = delta.total_seconds()
    if s < 3600:
        return f"{int(round(s / 60))} min"
    if s < 86400:
        h = s / 3600
        return f"{fmt_de(round(h, 1))} h"
    d = int(s // 86400)
    h = int(round((s - d * 86400) / 3600))
    return f"{d} T" + (f" {h} h" if h else "")


def _utc(d: str | date) -> datetime:
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def load_strategy(key: str) -> Strategy:
    """'s:<name>' (saved) or 'p:<name>' (preset)."""
    kind, _, name = (key or "").partition(":")
    if kind == "p":
        for p in PRESETS:
            if p.name == name:
                return copy.deepcopy(p)
        raise KeyError(f"Vorlage „{name}“ nicht gefunden")
    return state().store.load_strategy(name)


def load_data(broker_key: str, symbol: str, timeframe: str, start: datetime, end: datetime,
              warm_bars: int) -> tuple[pd.DataFrame, datetime]:
    """Fetch candles incl. warm-up before ``start``. Returns (df, fetch_start)."""
    st = state()
    creds = st.store.get_credentials(broker_key) or None
    opts = (st.settings.broker_options or {}).get(broker_key) or {}
    broker = create_broker(broker_key, creds, opts)
    tf_sec = TIMEFRAMES[timeframe]
    factor = 1.0
    if broker_key in ("yahoo", "alpaca") and not ("-" in symbol or "/" in symbol):
        factor = 1.6 if timeframe in ("1d", "1w") else 5.0  # weekends / exchange hours
    fetch_start = start - timedelta(seconds=tf_sec * (warm_bars + 5) * factor)
    if broker_key == "yahoo" and timeframe in YAHOO_LIMITS:
        earliest = datetime.now(timezone.utc) - timedelta(days=YAHOO_LIMITS[timeframe])
        fetch_start = max(fetch_start, earliest)
        if start < earliest:
            raise BrokerError(f"Yahoo liefert für {TIMEFRAME_LABELS.get(timeframe, timeframe)} nur die letzten "
                              f"{YAHOO_LIMITS[timeframe]} Tage. Bitte Zeitraum verkürzen oder eine größere "
                              "Zeiteinheit wählen.")
    df = fetch_history(broker, symbol, timeframe, fetch_start, end, cache_dir=st.store.cache_dir)
    return df, fetch_start


def trim_payload(payload: dict, t0: int) -> dict:
    """Drop warm-up points (time < t0) from a TVChart payload."""
    def cut(points):
        return [p for p in points if p.get("time", 0) >= t0]

    for k in ("candles", "volume"):
        if k in payload:
            payload[k] = cut(payload[k])
    for o in payload.get("overlays", []):
        o["data"] = cut(o["data"])
    for pane in payload.get("panes", []):
        for s in pane.get("series", []):
            s["data"] = cut(s["data"])
    return payload


SHORT_REASONS = {"take_profit": "TP", "stop_loss": "SL", "trailing_stop": "Trail", "max_bars": "Zeit"}


def chart_markers(markers: list[dict]) -> list[dict]:
    """Arrows without text for plain buys/signal sells; short labels only for protective exits."""
    out = []
    for m in markers or []:
        m = dict(m)
        m["text"] = SHORT_REASONS.get(m.get("reason"), " ") if m.get("type") == "sell" else " "
        out.append(m)
    return out


def series_line(s: pd.Series) -> list[dict]:
    idx = pd.DatetimeIndex(s.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    ts = idx.as_unit("s").asi8
    return [{"time": int(t), "value": float(v)} for t, v in zip(ts, s.to_numpy(dtype=float)) if math.isfinite(v)]


# ------------------------------------------------------------------------------------------
# page
# ------------------------------------------------------------------------------------------

@ui.page("/backtest")
def page(strategy: str | None = None, symbol: str | None = None, broker: str | None = None) -> None:
    st = state()
    store = st.store
    settings = st.settings
    theme = "light" if getattr(settings, "theme", "dark") == "light" else "dark"
    ui.add_css(PAGE_CSS)

    saved_form = dict(app.storage.user.get(FORM_KEY) or {})

    # strategy options
    def strategy_options() -> dict[str, str]:
        opts = {f"s:{n}": n for n in store.list_strategies()}
        opts.update({f"p:{p.name}": f"Vorlage · {p.name}" for p in PRESETS})
        return opts

    s_opts = strategy_options()
    watch = {w.symbol: w.broker for w in (settings.watchlist or [])}

    today = datetime.now(timezone.utc).date()
    form = {
        "strategy": saved_form.get("strategy"),
        "symbol": saved_form.get("symbol") or "BTC/USDT",
        "broker": saved_form.get("broker") or watch.get(saved_form.get("symbol") or "BTC/USDT", "binance"),
        "timeframe": saved_form.get("timeframe"),
        "start": saved_form.get("start") or (today - timedelta(days=182)).isoformat(),
        "end": saved_form.get("end") or today.isoformat(),
        "capital": saved_form.get("capital") or 10_000.0,
        "fee": None,
        "slippage": None,
        "override_costs": bool(saved_form.get("override_costs", False)),
    }
    if strategy:
        key = f"s:{strategy}" if f"s:{strategy}" in s_opts else f"p:{strategy}" if f"p:{strategy}" in s_opts else None
        if key:
            form["strategy"] = key
            form["timeframe"] = None  # use strategy default
    if form["strategy"] not in s_opts:
        form["strategy"] = next(iter(s_opts), None)
    if symbol:
        form["symbol"] = symbol
        form["broker"] = broker or watch.get(symbol, form["broker"])
    elif broker:
        form["broker"] = broker
    if form["broker"] not in DATA_SOURCES:
        form["broker"] = "binance"

    ctx: dict = {"result": None, "df": None, "data_key": None, "strategy": None, "busy": False,
                 "opt_progress": (0, 0), "opt_df": None, "opt_meta": None}

    def current_strategy() -> Strategy | None:
        try:
            return load_strategy(form["strategy"])
        except Exception:  # noqa: BLE001
            return None

    def persist() -> None:
        app.storage.user[FORM_KEY] = {k: form[k] for k in ("strategy", "symbol", "broker", "timeframe", "start",
                                                           "end", "capital", "override_costs")}

    with frame("Backtest", "Strategie mit historischen Kursdaten durchspielen – ohne echtes Geld"):
        # ------------------------------------------------------------------ form
        with card("gap-4"):
            with ui.element("div").classes(
                    "grid w-full gap-4 grid-cols-1 md:grid-cols-2 xl:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)_200px_190px]"):
                strat_sel = ui.select(s_opts, label="Strategie", value=form["strategy"], with_input=True).props(
                    "outlined dense options-dense").classes("w-full")
                sym_opts = sorted(set(list(watch) + [form["symbol"]]))
                sym_sel = ui.select(sym_opts, label="Symbol", value=form["symbol"], with_input=True,
                                    new_value_mode="add-unique").props(
                    "outlined dense options-dense hide-dropdown-icon=false").classes("w-full")
                sym_sel.tooltip("Aus der Watchlist wählen oder ein eigenes Symbol eintippen und Enter drücken "
                                "(z. B. ETH/USDT, AAPL, SAP.DE, BTC-USD).")
                src_sel = ui.select(DATA_SOURCES, label="Datenquelle", value=form["broker"]).props(
                    "outlined dense options-dense").classes("w-full")
                tf_sel = ui.select(TIMEFRAME_OPTIONS, label="Zeiteinheit", value=form["timeframe"]).props(
                    "outlined dense options-dense").classes("w-full")

            with ui.row().classes("w-full items-center gap-4"):
                start_in = ui.date_input("Von", value=form["start"]).props("outlined dense").classes("w-44")
                end_in = ui.date_input("Bis", value=form["end"]).props("outlined dense").classes("w-44")
                with ui.row().classes("tb-quick items-center gap-0"):
                    def quick(days: int) -> None:
                        end_in.set_value(today.isoformat())
                        start_in.set_value((today - timedelta(days=days)).isoformat())

                    for lbl, days in QUICK_RANGES:
                        ui.button(lbl, on_click=lambda d=days: quick(d)).props("flat dense no-caps")
                cap_in = ui.number("Startkapital", value=form["capital"], min=1, step=1000, format="%.0f").props(
                    "outlined dense").classes("w-40")
                cur_lbl = ui.label().classes("tb-muted text-sm -ml-2")
            with ui.row().classes("w-full items-center gap-3"):
                run_btn = ui.button("Backtest starten", icon="play_arrow").props("unelevated no-caps color=primary")
                edit_btn = ui.button("Strategie bearbeiten", icon="edit").props("flat no-caps color=grey-6")
                progress_row = ui.row().classes("items-center gap-2")
                with progress_row:
                    ui.spinner(size="20px")
                    progress_lbl = ui.label().classes("text-sm tb-muted")
                progress_row.set_visibility(False)
                ui.space()
                cost_sw = ui.switch("Kosten anpassen", value=form["override_costs"]).props("dense")
                cost_sw.tooltip("Gebühr und Slippage für diesen Lauf abweichend von der Strategie festlegen.")
                fee_in = ui.number("Gebühr", min=0, max=5, step=0.01).props(
                    'outlined dense suffix="%"').classes("w-28")
                slip_in = ui.number("Slippage", min=0, max=5, step=0.01).props(
                    'outlined dense suffix="%"').classes("w-28")

            info_lbl = ui.label().classes("tb-hint -mt-2")

        # ------------------------------------------------------------------ tabs
        with ui.tabs().props("dense no-caps align=left inline-label").classes("w-full") as tabs:
            t_res = ui.tab("res", label="Ergebnis", icon="insights")
            t_opt = ui.tab("opt", label="Optimierung", icon="tune")
        with ui.tab_panels(tabs, value=t_res).props("animated=false").classes("w-full bg-transparent"):
            with ui.tab_panel(t_res).classes("p-0 gap-4"):
                results = ui.column().classes("w-full gap-4")
                with results, card("items-center gap-2 py-10"):
                    ui.icon("query_stats", size="40px").classes("tb-muted")
                    ui.label("Noch kein Ergebnis").classes("text-base font-semibold")
                    ui.label("Strategie, Symbol und Zeitraum wählen und „Backtest starten“ klicken. Kursdaten "
                             "werden geladen und lokal zwischengespeichert.").classes("tb-muted text-sm text-center")
            with ui.tab_panel(t_opt).classes("p-0 gap-4"):
                opt_box = ui.column().classes("w-full gap-4")

    # ------------------------------------------------------------------ form logic
    def currency() -> str:
        return quote_currency(form["symbol"], form["broker"])

    def refresh_form_info() -> None:
        s = current_strategy()
        ctx["strategy"] = s
        cur_lbl.set_text(currency_symbol(currency()))
        if s is not None:
            tf_sel.props(f'hint="Strategie: {TIMEFRAME_LABELS.get(s.timeframe, s.timeframe)}"')
            if not form["timeframe"]:
                tf_sel.set_value(s.timeframe)
            if not cost_sw.value:
                fee_in.set_value(s.risk.fee_pct)
                slip_in.set_value(s.risk.slippage_pct)
            errs = validate(s)
            info_lbl.set_text(("⚠ " + errs[0]) if errs else f"Vorlauf: {warmup_bars(s)} Kerzen werden zusätzlich "
                                                              "vor dem Startdatum geladen.")
        fee_in.set_enabled(cost_sw.value)
        slip_in.set_enabled(cost_sw.value)
        edit_btn.set_visibility(form["strategy"] is not None)

    def on_strategy(e) -> None:
        form["strategy"] = e.value
        form["timeframe"] = None
        refresh_form_info()
        persist()
        build_optimizer()

    def on_symbol(e) -> None:
        if not e.value:
            return
        form["symbol"] = str(e.value).strip()
        if form["symbol"] in watch:
            src_sel.set_value(watch[form["symbol"]])
        cur_lbl.set_text(currency_symbol(currency()))
        persist()

    def on_src(e) -> None:
        form["broker"] = e.value
        cur_lbl.set_text(currency_symbol(currency()))
        persist()

    def on_tf(e) -> None:
        s = ctx.get("strategy")
        form["timeframe"] = None if (s is not None and e.value == s.timeframe) else e.value
        persist()

    strat_sel.on_value_change(on_strategy)
    sym_sel.on_value_change(on_symbol)
    src_sel.on_value_change(on_src)
    tf_sel.on_value_change(on_tf)
    start_in.on_value_change(lambda e: (form.update(start=e.value), persist()))
    end_in.on_value_change(lambda e: (form.update(end=e.value), persist()))
    cap_in.on_value_change(lambda e: (form.update(capital=e.value), persist()))
    cost_sw.on_value_change(lambda e: (form.update(override_costs=e.value), refresh_form_info(), persist()))

    def open_editor() -> None:
        kind, _, name = (form["strategy"] or "").partition(":")
        ui.navigate.to(f"/strategies?{'name' if kind == 's' else 'preset'}={quote(name)}")

    edit_btn.on_click(open_editor)

    def prepared_strategy() -> Strategy:
        s = load_strategy(form["strategy"])
        tf = tf_sel.value or s.timeframe
        s.timeframe = tf
        if cost_sw.value:
            if fee_in.value is not None:
                s.risk.fee_pct = float(fee_in.value)
            if slip_in.value is not None:
                s.risk.slippage_pct = float(slip_in.value)
        return s

    def check_form() -> tuple[datetime, datetime] | None:
        if not form["strategy"]:
            ui.notify("Bitte eine Strategie wählen.", color="warning")
            return None
        if not form["symbol"]:
            ui.notify("Bitte ein Symbol wählen.", color="warning")
            return None
        try:
            start = _utc(start_in.value)
            end = _utc(end_in.value) + timedelta(days=1) - timedelta(seconds=1)
        except (TypeError, ValueError):
            ui.notify("Bitte gültige Daten für den Zeitraum eingeben.", color="warning")
            return None
        if start >= end:
            ui.notify("Das Startdatum muss vor dem Enddatum liegen.", color="warning")
            return None
        now = datetime.now(timezone.utc)
        end = min(end, now)
        if not cap_in.value or cap_in.value <= 0:
            ui.notify("Bitte ein Startkapital größer 0 eingeben.", color="warning")
            return None
        return start, end

    async def get_data(s: Strategy, start: datetime, end: datetime) -> pd.DataFrame:
        warm = warmup_bars(s)
        key = (form["broker"], form["symbol"], s.timeframe, start, end, warm)
        if ctx["data_key"] == key and ctx["df"] is not None:
            return ctx["df"]
        df, _ = await run.io_bound(load_data, form["broker"], form["symbol"], s.timeframe, start, end, warm)
        if df is None or df.empty:
            raise BrokerError("Für diesen Zeitraum wurden keine Kursdaten gefunden.")
        if pd.DatetimeIndex(df.index).tz is None:
            df.index = pd.DatetimeIndex(df.index).tz_localize("UTC")
        if df.index[-1] < pd.Timestamp(start):
            raise BrokerError("Im gewählten Zeitraum liegen keine Kursdaten vor.")
        ctx["df"], ctx["data_key"] = df, key
        return df

    def set_busy(text: str | None) -> None:
        ctx["busy"] = text is not None
        progress_row.set_visibility(text is not None)
        progress_lbl.set_text(text or "")
        run_btn.set_enabled(text is None)
        if "opt_btn" in ctx:
            ctx["opt_btn"].set_enabled(text is None)

    def friendly_error(exc: Exception) -> str:
        msg = str(exc).strip() or exc.__class__.__name__
        if isinstance(exc, BrokerError):
            return msg
        low = msg.lower()
        if any(x in low for x in ("timed out", "connection", "network", "resolve", "451", "unreachable")):
            return "Keine Verbindung zum Datenanbieter. Bitte Internetverbindung prüfen oder eine andere Datenquelle wählen."
        return msg

    async def run_bt() -> None:
        if ctx["busy"]:
            return
        rng = check_form()
        if rng is None:
            return
        start, end = rng
        try:
            s = prepared_strategy()
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"Strategie konnte nicht geladen werden: {exc}", color="negative")
            return
        errs = validate(s)
        if errs:
            show_error("Die Strategie ist noch nicht vollständig", errs)
            return
        persist()
        tabs.set_value("res")
        set_busy(f"Lade Kursdaten für {form['symbol']} …")
        try:
            df = await get_data(s, start, end)
            n_trade = int((df.index >= pd.Timestamp(start)).sum())
            set_busy(f"Berechne Backtest über {n_trade:,} Kerzen …".replace(",", "."))
            res = await run.io_bound(run_backtest, s, df, float(cap_in.value), form["symbol"], pd.Timestamp(start))
        except Exception as exc:  # noqa: BLE001
            show_error("Backtest fehlgeschlagen", [friendly_error(exc)])
            return
        finally:
            set_busy(None)
        ctx["result"] = res
        render_result(res, df, s, start, end)

    run_btn.on_click(run_bt)

    def show_error(title: str, lines: list[str]) -> None:
        results.clear()
        with results, card("tb-danger-zone gap-2"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("error_outline", size="22px").classes("tb-neg")
                ui.label(title).classes("text-base font-semibold")
            for ln in lines:
                ui.label(ln).classes("text-sm")

    # ------------------------------------------------------------------ results
    def render_result(res, df: pd.DataFrame, s: Strategy, start: datetime, end: datetime) -> None:
        m = res.metrics
        cur = currency()
        csym = currency_symbol(cur)
        cap = float(cap_in.value)
        results.clear()
        with results:
            # verdict
            ret = m.get("total_return_pct") or 0.0
            pnl = (m.get("final_equity") or cap) - cap
            bh = m.get("buy_hold_return_pct")
            pos = ret >= 0
            with ui.element("div").classes(f"tb-verdict w-full {'pos' if pos else 'neg'}"):
                with ui.row().classes("w-full items-center gap-5 no-wrap"):
                    ui.icon("trending_up" if pos else "trending_down", size="44px").classes(
                        "tb-pos" if pos else "tb-neg")
                    with ui.column().classes("gap-1 flex-1 min-w-0"):
                        word = "Gewinn" if pos else "Verlust"
                        ui.label(f"Mit dieser Strategie hättest du {fmt_pct(ret, 1)} "
                                 f"({fmt_money(abs(pnl), csym, 0)}) {word} gemacht.").classes("tb-verdict-title")
                        if bh is not None:
                            diff = ret - bh
                            better = "besser" if diff >= 0 else "schlechter"
                            ui.label(f"Einfach kaufen und halten (Buy & Hold) hätte {fmt_pct(bh, 1)} gebracht – "
                                     f"die Strategie war damit {fmt_de(abs(round(diff, 1)))} Prozentpunkte "
                                     f"{better}.").classes("text-sm")
                        n_bars = len(res.equity)
                        ui.label(f"{form['symbol']} · {TIMEFRAME_LABELS.get(s.timeframe, s.timeframe)} · "
                                 f"{fmt_dt(res.equity.index[0])} – {fmt_dt(res.equity.index[-1])} · "
                                 f"{fmt_num(n_bars, 0)} Kerzen · Startkapital {fmt_money(cap, csym, 0)} · "
                                 f"„{s.name}“").classes("tb-muted text-xs")

            # KPI grid
            with ui.element("div").classes("tb-kpi-grid w-full"):
                for k in MAIN_KPIS:
                    v = m.get(k)
                    with ui.element("div").classes("tb-kpi-tile"):
                        with ui.row().classes("items-center gap-1 no-wrap"):
                            ui.label({"total_return_pct": "Rendite", "buy_hold_return_pct": "Buy & Hold",
                                      "max_drawdown_pct": "Max. Drawdown"}.get(k, METRIC_LABELS[k][0])).classes(
                                "tb-kpi-label")
                            help_icon(KPI_HELP.get(k, ""), "14px")
                        ui.label(fmt_metric(k, v, csym)).classes(f"tb-kpi-value {metric_class(k, v)}")
                        sub = ""
                        if k == "total_return_pct":
                            sub = fmt_money(pnl, csym, 0)
                        elif k == "buy_hold_return_pct" and bh is not None:
                            sub = fmt_money(cap * bh / 100, csym, 0)
                        elif k == "num_trades":
                            sub = f"Ø {fmt_num(m.get('avg_bars_held'), 1)} Kerzen gehalten" if m.get("num_trades") else ""
                        elif k == "win_rate_pct" and m.get("num_trades"):
                            wins = sum(1 for t in res.trades if t.pnl > 0)
                            sub = f"{wins} von {len(res.trades)} Trades"
                        ui.label(sub).classes("tb-muted text-xs")

            # warnings / skipped
            skipped = getattr(res, "skipped", None) or []
            warns = list(res.warnings or [])
            if warns or skipped:
                with card("tb-warn gap-2"):
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("warning_amber", size="20px").style("color:#f59e0b")
                        ui.label("Hinweise zum Ergebnis").classes("font-semibold")
                    for w in warns:
                        ui.label("• " + w).classes("text-sm")
                    if skipped:
                        counts: dict[str, int] = {}
                        for x in skipped:
                            counts[x.get("reason", "?")] = counts.get(x.get("reason", "?"), 0) + 1
                        txt = ", ".join(f"{n}× {r}" for r, n in counts.items())
                        ui.label(f"• {len(skipped)} Kaufsignal{'e' if len(skipped) != 1 else ''} konnte"
                                 f"{'n' if len(skipped) != 1 else ''} nicht ausgeführt werden: {txt}.").classes("text-sm")

            # equity chart
            with card("gap-2"):
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label("Kapitalverlauf").classes("tb-section")
                    with ui.row().classes("items-center gap-4 text-xs tb-muted"):
                        with ui.row().classes("items-center gap-1"):
                            ui.element("span").classes("tb-legend-dot").style("background:#3b82f6")
                            ui.label("Strategie")
                        with ui.row().classes("items-center gap-1"):
                            ui.element("span").classes("tb-legend-dot").style("background:#8b95a5")
                            ui.label("Buy & Hold")
                eq_chart = TVChart(theme=theme).classes("w-full h-[300px]")
                eq_chart.show(None, lines=[
                    {"title": "Buy & Hold", "color": "#8b95a5", "data": series_line(res.buy_hold), "width": 1.5},
                    {"title": "Strategie", "color": "#3b82f6", "data": series_line(res.equity), "area": True},
                ])

            # price chart
            with card("gap-2"):
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label("Kurs, Indikatoren & Trades").classes("tb-section")
                    ui.label("▲ Kauf  ▼ Verkauf (TP = Take-Profit, SL = Stop-Loss, Trail = Trailing-Stop)").classes(
                        "tb-muted text-xs")
                chart = TVChart(theme=theme).classes("w-full h-[560px]")
                try:
                    inds = required_indicators(s)
                    payload = build_payload(df, inds, chart_markers(res.markers))
                except Exception:  # noqa: BLE001
                    payload = build_payload(df, [], chart_markers(res.markers))
                payload = trim_payload(payload, int(pd.Timestamp(res.equity.index[0]).timestamp()))
                # push the data once the chart is mounted and laid out (oscillator panes need its height)
                ui.timer(0.3, lambda: chart.set_payload(payload), once=True)

            # all metrics
            with card("gap-2"):
                ui.label("Alle Kennzahlen").classes("tb-section")
                with ui.element("div").classes("grid w-full gap-x-8 grid-cols-1 md:grid-cols-2 xl:grid-cols-3"):
                    for k, (label, _f) in METRIC_LABELS.items():
                        with ui.element("div").classes("tb-metric-row"):
                            ui.label(label).classes("tb-muted")
                            ui.label(fmt_metric(k, m.get(k), csym)).classes(
                                f"font-medium whitespace-nowrap {metric_class(k, m.get(k))}")
            render_trades(res, csym)

    def render_trades(res, csym: str) -> None:
        with card("gap-2 min-w-0"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label(f"Trades ({len(res.trades)})").classes("tb-section")
                ui.button("CSV exportieren", icon="download", on_click=lambda: export_csv(res)).props(
                    "flat dense no-caps color=primary").set_enabled(bool(res.trades))
            if not res.trades:
                ui.label("Keine Trades im gewählten Zeitraum.").classes("tb-muted text-sm py-4")
                return
            bars = list(getattr(res, "bars_held", []) or [])
            rows = []
            for i, t in enumerate(res.trades):
                held = bars[i] if i < len(bars) else None
                dur = fmt_duration(pd.Timestamp(t.exit_time) - pd.Timestamp(t.entry_time))
                rows.append({
                    "nr": i + 1,
                    "entry": pd.Timestamp(t.entry_time).timestamp(), "entry_s": fmt_dt(t.entry_time),
                    "entry_price": t.entry_price, "entry_price_s": fmt_price(t.entry_price),
                    "exit": pd.Timestamp(t.exit_time).timestamp(), "exit_s": fmt_dt(t.exit_time),
                    "exit_price": t.exit_price, "exit_price_s": fmt_price(t.exit_price),
                    "qty": t.quantity, "qty_s": fmt_num(t.quantity, 6 if t.quantity < 1 else 4),
                    "pnl": t.pnl, "pnl_s": fmt_money(t.pnl, csym),
                    "pnl_pct": t.pnl_pct, "pnl_pct_s": fmt_pct(t.pnl_pct),
                    "reason": EXIT_REASON_LABELS.get(t.exit_reason.value, t.exit_reason.value),
                    "held": held or 0, "held_s": dur,
                })
            cols = [
                {"name": "nr", "label": "#", "field": "nr", "sortable": True, "align": "left"},
                {"name": "entry", "label": "Kauf", "field": "entry", "sortable": True, "align": "left"},
                {"name": "entry_price", "label": "Kaufkurs", "field": "entry_price", "sortable": True},
                {"name": "exit", "label": "Verkauf", "field": "exit", "sortable": True, "align": "left"},
                {"name": "exit_price", "label": "Verkaufskurs", "field": "exit_price", "sortable": True},
                {"name": "qty", "label": "Menge", "field": "qty", "sortable": True},
                {"name": "pnl", "label": "G/V", "field": "pnl", "sortable": True, "cls": True},
                {"name": "pnl_pct", "label": "G/V %", "field": "pnl_pct", "sortable": True, "cls": True},
                {"name": "reason", "label": "Grund", "field": "reason", "sortable": True, "align": "left"},
                {"name": "held", "label": "Haltedauer", "field": "held", "sortable": True},
            ]
            tbl = ui.table(columns=cols, rows=rows, row_key="nr", pagination=12).props(
                "flat dense").classes("w-full tb-table")
            tbl.add_slot("body-cell", CELL_SLOT)

    def export_csv(res) -> None:
        df = res.trades_frame()
        if df.empty:
            return
        df = df.rename(columns={"symbol": "Symbol", "quantity": "Menge", "entry_price": "Kaufkurs",
                                "exit_price": "Verkaufskurs", "entry_time": "Kaufzeit", "exit_time": "Verkaufszeit",
                                "fees": "Gebühren", "exit_reason": "Grund", "strategy": "Strategie", "pnl": "G/V",
                                "pnl_pct": "G/V %", "bars_held": "Haltedauer (Kerzen)"})
        if "Grund" in df:
            df["Grund"] = df["Grund"].map(lambda r: EXIT_REASON_LABELS.get(r, r))
        df = df.drop(columns=[c for c in ("id",) if c in df.columns])
        csv = df.to_csv(index=False, sep=";", decimal=",")
        fname = f"backtest_{form['symbol'].replace('/', '-')}_{date.today().isoformat()}.csv"
        ui.download.content("﻿" + csv, fname, "text/csv")

    # ------------------------------------------------------------------ optimizer
    opt_state: dict = {"rows": {}, "candidates": []}

    def build_optimizer() -> None:
        opt_box.clear()
        s = current_strategy()
        with opt_box:
            with card("tb-warn gap-1"):
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.icon("psychology_alt", size="22px").style("color:#f59e0b")
                    ui.label("Vorsicht vor Überanpassung (Overfitting)").classes("font-semibold")
                ui.label("Die Optimierung sucht die Einstellungen, die in der Vergangenheit am besten funktioniert "
                         "hätten. Je mehr Parameter und Kombinationen, desto größer die Gefahr, nur Zufallsmuster "
                         "zu finden, die in Zukunft nicht wiederkehren. Nutze den Train/Test-Split und vertraue nur "
                         "Ergebnissen, die auch im Testzeitraum (Out-of-Sample) gut sind.").classes("text-sm")
            if s is None:
                with card():
                    ui.label("Bitte oben eine Strategie wählen.").classes("tb-muted")
                return
            cands = param_candidates(s)
            opt_state["candidates"] = cands
            opt_state["rows"] = {}
            with ui.element("div").classes("grid w-full gap-4 grid-cols-1 xl:grid-cols-[minmax(0,1fr)_300px]"):
                with card("gap-2 min-w-0"):
                    ui.label("Parameter variieren").classes("tb-section")
                    ui.label("Wähle die Werte, die durchprobiert werden sollen. Gleiche Indikator-Einstellungen in "
                             "mehreren Bedingungen werden gemeinsam verändert.").classes("tb-muted text-xs")
                    with ui.element("div").classes("w-full overflow-x-auto"):
                        with ui.element("div").classes("min-w-[600px]"):
                            with ui.element("div").classes("tb-param-row head"):
                                for h in ("", "Parameter", "Aktuell", "Von", "Bis", "Schritt", "Werte"):
                                    ui.label(h)
                            last_group = None
                            for c in cands:
                                if c.group != last_group:
                                    last_group = c.group
                                    ui.label({"Indikator": "Indikator-Einstellungen",
                                              "Schwellenwert": "Schwellenwerte",
                                              "Trading": "Trading-Einstellungen"}[c.group]).classes(
                                        "tb-section mt-3 mb-1 px-1")
                                param_row(c)
                with card("gap-3"):
                    ui.label("Optimierung").classes("tb-section")
                    metric_opts = {k: METRIC_LABELS[k][0] for k in RESULT_METRICS if k in METRIC_LABELS
                                   and k not in ("final_equity", "buy_hold_return_pct", "exposure_pct")}
                    ctx["opt_metric"] = ui.select(metric_opts, label="Ziel (beste zuerst)",
                                                  value="total_return_pct").props(
                        "outlined dense options-dense").classes("w-full")
                    with ui.row().classes("items-center gap-1 no-wrap"):
                        ctx["opt_split"] = ui.switch("Train/Test-Split 70/30", value=True).props("dense")
                        help_icon("Die ersten 70 % des Zeitraums dienen zum Suchen der besten Werte (Training, "
                                  "„In-Sample“). Die letzten 30 % prüfen dieselben Werte auf Daten, die bei der "
                                  "Suche nicht verwendet wurden (Test, „Out-of-Sample“). Nur wenn die Werte auch "
                                  "dort gut abschneiden, ist das Ergebnis halbwegs belastbar.")
                    ctx["opt_count"] = ui.label().classes("text-sm")
                    ctx["opt_btn"] = ui.button("Optimierung starten", icon="rocket_launch",
                                               on_click=run_opt).props("unelevated no-caps color=primary").classes(
                        "w-full")
                    ctx["opt_prog"] = ui.linear_progress(value=0, show_value=False).props("rounded size=6px")
                    ctx["opt_prog_lbl"] = ui.label().classes("tb-muted text-xs")
                    ctx["opt_prog"].set_visibility(False)
                    ui.label("Verwendet Symbol, Zeitraum, Zeiteinheit und Startkapital aus dem Formular oben.").classes(
                        "tb-hint")
            ctx["opt_results"] = ui.column().classes("w-full gap-4")
            update_count()

    def param_row(c: ParamCandidate) -> None:
        row = {"c": c, "on": False}
        opt_state["rows"][c.id] = row
        kw = {"format": "%.0f"} if c.type == "int" else {}
        with ui.element("div").classes("tb-param-row off") as el:
            row["el"] = el

            def toggle(e) -> None:
                row["on"] = bool(e.value)
                el.classes(remove="off") if row["on"] else el.classes("off")
                update_count()

            ui.checkbox(value=False, on_change=toggle).props("dense")
            ui.label(c.label).classes("text-sm leading-snug line-clamp-2").tooltip(c.label)
            ui.label("aus" if c.value is None else fmt_de(c.value)).classes("text-sm tb-muted")
            row["lo"] = ui.number(value=c.lo, step=c.step, min=c.min, max=c.max, on_change=lambda: update_count(),
                                  **kw).props("outlined dense").classes("tb-param-in")
            row["hi"] = ui.number(value=c.hi, step=c.step, min=c.min, max=c.max, on_change=lambda: update_count(),
                                  **kw).props("outlined dense").classes("tb-param-in")
            row["step"] = ui.number(value=c.step, min=0, on_change=lambda: update_count(), **kw).props(
                "outlined dense").classes("tb-param-in")
            row["n"] = ui.label().classes("text-sm text-right tb-param-in")

    def selected_grid() -> tuple[dict[str, list], dict[str, list[str]], list[str]]:
        grid, paths, problems = {}, {}, []
        for cid, row in opt_state["rows"].items():
            c: ParamCandidate = row["c"]
            vals = grid_values(row["lo"].value, row["hi"].value, row["step"].value, c.type, MAX_RUNS)
            row["n"].set_text(str(len(vals)) if vals else "–")
            if not row["on"]:
                continue
            if not vals:
                problems.append(c.label)
                continue
            grid[cid] = vals
            paths[cid] = c.paths
        return grid, paths, problems

    def update_count() -> None:
        if "opt_count" not in ctx:
            return
        grid, _p, problems = selected_grid()
        n = count_combinations([len(v) for v in grid.values()]) if grid else 0
        lbl = ctx["opt_count"]
        if problems:
            lbl.set_text(f"Ungültiger Bereich: {problems[0]}")
            lbl.classes(replace="text-sm tb-neg")
        elif not grid:
            lbl.set_text("Noch kein Parameter ausgewählt.")
            lbl.classes(replace="text-sm tb-muted")
        elif n > MAX_RUNS:
            lbl.set_text(f"{fmt_num(n, 0)} Kombinationen – zu viele (max. {MAX_RUNS}). Bereiche verkleinern "
                         "oder Schritt vergrößern.")
            lbl.classes(replace="text-sm tb-neg")
        else:
            lbl.set_text(f"{fmt_num(n, 0)} Kombination{'en' if n != 1 else ''} (max. {MAX_RUNS})")
            lbl.classes(replace="text-sm font-medium")
        ok = bool(grid) and not problems and n <= MAX_RUNS and not ctx["busy"]
        ctx["opt_btn"].set_enabled(ok)

    async def run_opt() -> None:
        if ctx["busy"]:
            return
        grid, paths, problems = selected_grid()
        n = count_combinations([len(v) for v in grid.values()]) if grid else 0
        if problems or not grid or n > MAX_RUNS:
            update_count()
            return
        rng = check_form()
        if rng is None:
            return
        start, end = rng
        try:
            s = prepared_strategy()
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"Strategie konnte nicht geladen werden: {exc}", color="negative")
            return
        metric = ctx["opt_metric"].value
        split = 0.7 if ctx["opt_split"].value else None
        set_busy(f"Lade Kursdaten für {form['symbol']} …")
        prog, prog_lbl = ctx["opt_prog"], ctx["opt_prog_lbl"]
        ctx["opt_progress"] = (0, n)
        try:
            df = await get_data(s, start, end)
            set_busy(f"Optimiere {n} Kombinationen …")
            prog.set_visibility(True)

            def progress(i: int, total: int) -> None:
                ctx["opt_progress"] = (i, total)

            def poll() -> None:
                i, total = ctx["opt_progress"]
                prog.set_value(i / total if total else 0)
                prog_lbl.set_text(f"{i} von {total} Läufen")

            timer = ui.timer(0.3, poll)
            try:
                out = await run.io_bound(run_grid, s, df, grid, paths, metric, float(cap_in.value), MAX_RUNS,
                                         split, pd.Timestamp(start), progress)
            finally:
                timer.cancel()
                poll()
        except Exception as exc:  # noqa: BLE001
            set_busy(None)
            prog.set_visibility(False)
            ui.notify(f"Optimierung fehlgeschlagen: {friendly_error(exc)}", color="negative", multi_line=True)
            return
        set_busy(None)
        update_count()
        ctx["opt_df"] = out
        ctx["opt_meta"] = {"strategy": s, "paths": paths, "metric": metric, "split": split,
                           "labels": {cid: opt_state["rows"][cid]["c"].short or opt_state["rows"][cid]["c"].label
                                      for cid in grid},
                           "full": {cid: opt_state["rows"][cid]["c"].label for cid in grid}}
        render_opt_results()

    def render_opt_results() -> None:
        box = ctx["opt_results"]
        box.clear()
        out: pd.DataFrame = ctx["opt_df"]
        meta = ctx["opt_meta"]
        if out is None:
            return
        split = meta["split"]
        metric = meta["metric"]
        keys = list(meta["paths"])
        top = out.head(50)
        n_err = int(out["error"].notna().sum()) if "error" in out else 0
        with box, card("gap-2 min-w-0"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label(f"Ergebnisse – Top {len(top)} von {len(out)}").classes("tb-section")
                ui.space()
                ui.label(f"sortiert nach {METRIC_LABELS.get(metric, (metric,))[0]}"
                         + (" im Trainingszeitraum" if split else "")).classes("tb-muted text-xs")
            if split:
                ui.label("Spalten „Test“ zeigen dieselben Werte auf den letzten 30 % des Zeitraums – Daten, die bei "
                         "der Suche nicht verwendet wurden. Große Unterschiede zwischen Training und Test deuten auf "
                         "Überanpassung hin.").classes("tb-hint")
            if n_err:
                ui.label(f"{n_err} Kombination{'en' if n_err != 1 else ''} ungültig (z. B. Bereich außerhalb der "
                         "erlaubten Werte) – ganz unten einsortiert.").classes("tb-hint")
            cols = [{"name": "rank", "label": "#", "field": "rank", "align": "left"}]
            for i, k in enumerate(keys):
                lbl = meta["labels"][k]
                short = lbl if len(lbl) <= 28 else lbl[:26] + "…"
                cols.append({"name": f"p{i}", "label": short, "field": f"p{i}", "sortable": True})
            mcols = [("total_return_pct", "Rendite", True), ("max_drawdown_pct", "Max. DD", False),
                     ("num_trades", "Trades", False)]
            if not split:
                mcols += [("win_rate_pct", "Trefferquote", False), ("sharpe", "Sharpe", False)]
            if metric not in [m[0] for m in mcols]:
                mcols.insert(1, (metric, METRIC_LABELS.get(metric, (metric,))[0], False))
            for k, lbl, cls in mcols:
                cols.append({"name": k, "label": (lbl + " (Training)") if (split and k == "total_return_pct") else lbl,
                             "field": k, "sortable": True, "cls": cls})
            if split:
                for k, lbl, cls in [("oos_total_return_pct", "Rendite (Test)", True),
                                    ("oos_max_drawdown_pct", "Max. DD (Test)", False)]:
                    cols.append({"name": k, "label": lbl, "field": k, "sortable": True, "cls": cls})
            cols.append({"name": "actions", "label": "", "field": "rank", "align": "right"})
            rows = []
            for i, (_, r) in enumerate(top.iterrows()):
                row = {"rank": i + 1, "error": r.get("error") if isinstance(r.get("error"), str) else None}
                for j, k in enumerate(keys):
                    v = r.get(k)
                    row[f"p{j}"] = None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)
                    row[f"p{j}_s"] = fmt_de(row[f"p{j}"])
                for k in [c["name"] for c in cols if c["name"] not in ("rank", "actions") and not
                          c["name"].startswith("p")]:
                    v = r.get(k)
                    v = None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)
                    row[k] = v
                    base = k[4:] if k.startswith("oos_") else k
                    row[k + "_s"] = fmt_metric(base, v)
                    if base == "max_drawdown_pct" and v is not None:
                        row[k] = -v
                rows.append(row)
            tbl = ui.table(columns=cols, rows=rows, row_key="rank", pagination=15).props("flat dense").classes(
                "w-full tb-table tb-opt-table")
            tbl.add_slot("body-cell", CELL_SLOT)
            tbl.add_slot("body-cell-actions", r"""
<q-td :props="props" class="text-right whitespace-nowrap">
  <q-btn flat dense round size="sm" color="primary" icon="edit_note"
         :disable="!!props.row.error" @click="() => $parent.$emit('apply_open', props.row.rank)">
    <q-tooltip>Übernehmen: als Kopie im Strategie-Editor öffnen</q-tooltip></q-btn>
  <q-btn flat dense round size="sm" color="primary" icon="save_as"
         :disable="!!props.row.error" @click="() => $parent.$emit('apply_save', props.row.rank)">
    <q-tooltip>Übernehmen: als neue Strategie speichern</q-tooltip></q-btn>
</q-td>""")
            tbl.on("apply_open", lambda e: apply_row(int(e.args), save=False))
            tbl.on("apply_save", lambda e: apply_row(int(e.args), save=True))

    def apply_row(rank: int, save: bool) -> None:
        out: pd.DataFrame = ctx["opt_df"]
        meta = ctx["opt_meta"]
        if out is None or rank < 1 or rank > len(out):
            return
        r = out.iloc[rank - 1]
        values = {}
        for cid in meta["paths"]:
            c = opt_state["rows"].get(cid, {}).get("c")
            v = r.get(cid)
            if v is None or (isinstance(v, float) and math.isnan(v)):
                continue
            values[cid] = int(round(v)) if (c is not None and c.type == "int") else float(v)
        base = load_strategy(form["strategy"])  # original (without per-run timeframe/cost overrides)
        new = apply_values(base, values, meta["paths"])
        stem = base.name[:-len(" (optimiert)")] if base.name.endswith(" (optimiert)") else base.name
        new.name = f"{stem} (optimiert)"
        if save:
            from tradebot.gui.pages.strategies import unique_name
            new.name = unique_name(new.name, store.list_strategies())
            errs = validate(new)
            if errs:
                ui.notify("Kombination ungültig: " + errs[0], color="negative")
                return
            store.save_strategy(new)
            ui.notify(f"Als „{new.name}“ gespeichert.", color="positive")
            opts = strategy_options()
            strat_sel.set_options(opts)
        else:
            app.storage.user[DRAFT_KEY] = new.to_dict()
            ui.navigate.to("/strategies?draft=1")

    refresh_form_info()
    build_optimizer()
