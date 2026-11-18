"""Shared helpers for the Test-/Live-Trading pages and the Wallet page.

Formatting (times, durations, exit reasons, event kinds), a light-weight table helper,
broker tradability checks and publishing of manual actions into the global event feed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from nicegui import ui

from tradebot.core.models import TradingMode
from tradebot.gui.state import state

CSS = """
.tb-tbl-wrap { width: 100%; overflow-x: auto; }
.tb-tbl { width: 100%; border-collapse: separate; border-spacing: 0; font-size: .86rem; }
.tb-tbl th { text-align: left; font-weight: 600; font-size: .7rem; letter-spacing: .05em; text-transform: uppercase;
             color: var(--tb-muted); padding: 8px 8px; border-bottom: 1px solid var(--tb-border); white-space: nowrap; vertical-align: bottom; }
.tb-tbl th.wrap { white-space: normal; min-width: 90px; }
.tb-tbl td { padding: 9px 8px; border-bottom: 1px solid var(--tb-border); white-space: nowrap; vertical-align: middle; }
.tb-tbl tr:last-child td { border-bottom: none; }
.tb-tbl tbody tr:hover td { background: var(--tb-surface-2); }
.tb-tbl .r { text-align: right; }
.tb-tbl .c { text-align: center; }
.tb-tbl tr.tb-row-warn td { background: color-mix(in srgb, #f59e0b 8%, transparent); }
.tb-badge { display: inline-block; font-size: .66rem; font-weight: 600; letter-spacing: .03em; padding: 1px 7px;
            border-radius: 999px; border: 1px solid var(--tb-border); color: var(--tb-muted); text-transform: uppercase; }
.tb-badge-live { color: #fff; background: var(--tb-neg); border-color: var(--tb-neg); }
.tb-badge-test { color: #fff; background: var(--tb-primary); border-color: var(--tb-primary); }
.tb-badge-pos { color: var(--tb-pos); border-color: color-mix(in srgb, var(--tb-pos) 45%, transparent); }
.tb-badge-neg { color: var(--tb-neg); border-color: color-mix(in srgb, var(--tb-neg) 45%, transparent); }
.tb-badge-warn { color: #f59e0b; border-color: color-mix(in srgb, #f59e0b 45%, transparent); }
.tb-banner { border-radius: 14px; padding: 14px 18px; border: 1px solid var(--tb-border); background: var(--tb-surface); }
.tb-banner-info { border-color: color-mix(in srgb, var(--tb-primary) 40%, transparent);
                  background: color-mix(in srgb, var(--tb-primary) 7%, var(--tb-surface)); }
.tb-banner-warn { border-color: color-mix(in srgb, #f59e0b 45%, transparent);
                  background: color-mix(in srgb, #f59e0b 8%, var(--tb-surface)); }
.tb-banner-error { border-color: color-mix(in srgb, var(--tb-neg) 50%, transparent);
                   background: color-mix(in srgb, var(--tb-neg) 10%, var(--tb-surface)); }
.tb-event { display: flex; gap: 10px; align-items: flex-start; padding: 7px 2px; border-bottom: 1px solid var(--tb-border); }
.tb-event:last-child { border-bottom: none; }
.tb-event .tb-ev-icon { width: 28px; height: 28px; min-width: 28px; border-radius: 8px; display: flex;
                        align-items: center; justify-content: center; }
.tb-scroll { overflow-y: auto; }
.tb-sym-chip.q-chip { margin: 0; border: 1px solid var(--tb-border); background: transparent; }
.tb-sym-chip.q-chip--selected { background: color-mix(in srgb, var(--tb-primary) 18%, transparent);
                                border-color: color-mix(in srgb, var(--tb-primary) 60%, transparent); }
.tb-sym-chip.q-chip--disabled { opacity: .45; }
.tb-empty { padding: 26px 8px; text-align: center; color: var(--tb-muted); font-size: .88rem; }
.tb-kv { display: grid; grid-template-columns: auto 1fr; gap: 6px 18px; font-size: .9rem; }
.tb-kv > .k { color: var(--tb-muted); }
.tb-kv > .v { font-weight: 600; text-align: right; }
"""


def add_css() -> None:
    ui.add_css(CSS)


# ---------------------------------------------------------------------------------------
# labels
# ---------------------------------------------------------------------------------------

REASON_LABELS = {
    "signal": "Ausstiegssignal",
    "take_profit": "Take-Profit",
    "stop_loss": "Stop-Loss",
    "trailing_stop": "Trailing-Stop",
    "max_bars": "Max. Haltedauer",
    "manual": "Manuell",
    "end_of_data": "Datenende",
}

# kind -> (icon, css color)
KIND_STYLE = {
    "buy": ("south_west", "var(--tb-pos)"),
    "sell": ("north_east", "var(--tb-primary)"),
    "manual": ("north_east", "var(--tb-primary)"),
    "take_profit": ("emoji_events", "var(--tb-pos)"),
    "stop_loss": ("shield", "var(--tb-neg)"),
    "trailing_stop": ("trending_down", "var(--tb-neg)"),
    "error": ("error", "var(--tb-neg)"),
    "kill_switch": ("power_settings_new", "var(--tb-neg)"),
    "warning": ("warning", "#f59e0b"),
    "paused": ("pause_circle", "#f59e0b"),
    "external_closed": ("link_off", "#f59e0b"),
    "info": ("info", "var(--tb-muted)"),
}


def kind_style(kind: str | None, level: str | None = None, pnl: float | None = None) -> tuple[str, str]:
    icon, color = KIND_STYLE.get(kind or "", KIND_STYLE.get(level or "", KIND_STYLE["info"]))
    if kind in ("sell", "manual") and pnl is not None:
        color = "var(--tb-pos)" if pnl >= 0 else "var(--tb-neg)"
    return icon, color


def reason_label(reason: str | None) -> str:
    return REASON_LABELS.get(reason or "", reason or "–")


# ---------------------------------------------------------------------------------------
# time formatting (UTC iso strings -> local time)
# ---------------------------------------------------------------------------------------

def parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fmt_time(value: Any, seconds: bool = False, date: bool = True) -> str:
    dt = parse_time(value)
    if dt is None:
        return "–"
    local = dt.astimezone()
    fmt = ("%d.%m.%Y " if date else "") + ("%H:%M:%S" if seconds else "%H:%M")
    return local.strftime(fmt)


def fmt_time_smart(value: Any) -> str:
    """Time only for today, date + time otherwise."""
    dt = parse_time(value)
    if dt is None:
        return "–"
    local = dt.astimezone()
    if local.date() == datetime.now().astimezone().date():
        return local.strftime("%H:%M:%S")
    return local.strftime("%d.%m. %H:%M")


def fmt_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "–"
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d} T {h} h"
    if h:
        return f"{h} h {m} min"
    if m:
        return f"{m} min {s} s"
    return f"{s} s"


def since_seconds(value: Any) -> float | None:
    dt = parse_time(value)
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds()


def fmt_qty(v: float | None) -> str:
    if v is None:
        return "–"
    from tradebot.gui.theme import fmt_num

    a = abs(v)
    digits = 2 if a >= 1000 else 4 if a >= 1 else 6 if a >= 0.001 else 8
    s = fmt_num(v, digits)
    if "," in s:
        s = s.rstrip("0").rstrip(",")
    return s


def quote_currency(symbol: str | None) -> str:
    if symbol and "/" in symbol:
        return symbol.split("/", 1)[1]
    return ""


def common_currency(symbols: Iterable[str]) -> str:
    qs = {quote_currency(s) for s in symbols if s}
    qs.discard("")
    return qs.pop() if len(qs) == 1 else ""


# ---------------------------------------------------------------------------------------
# brokers
# ---------------------------------------------------------------------------------------

def broker_name(key: str | None) -> str:
    if not key:
        return "–"
    try:
        from tradebot.brokers import BROKERS

        return BROKERS[key].info().name
    except Exception:  # noqa: BLE001
        return key


def live_trade_block_reason(broker_key: str) -> str | None:
    """None if LIVE trading is possible with this broker, else a German reason."""
    try:
        from tradebot.brokers import BROKERS

        info = BROKERS[broker_key].info()
    except Exception:  # noqa: BLE001
        return "Unbekannter Broker"
    if not info.supports_trading:
        return f"{info.name} liefert nur Kursdaten (kein Handel möglich)"
    if info.credential_fields and not state().store.has_credentials(broker_key):
        return f"Keine API-Zugangsdaten für {info.name} hinterlegt"
    return None


# ---------------------------------------------------------------------------------------
# feed
# ---------------------------------------------------------------------------------------

def feed_covered(mode: TradingMode) -> bool:
    """True if bot events of ``mode`` already reach the global feed (subscribed bot exists).

    Manual actions are executed on the running bot or on a temporary bot that forwards the
    subscribers of the last bot – in both cases the feed receives the event itself.
    """
    st = state()
    bot = st.manager.get(mode)
    if bot is None:
        return False
    return id(bot) in getattr(st, "_subscribed", set())


def attach_existing(mode: TradingMode) -> None:
    st = state()
    bot = st.manager.get(mode)
    if bot is not None:
        st.attach(mode, bot)


def publish_sell(mode: TradingMode, trade: Any) -> None:
    if feed_covered(mode):
        return
    pnl = float(getattr(trade, "pnl", 0.0) or 0.0)
    pct = float(getattr(trade, "pnl_pct", 0.0) or 0.0)
    sym = getattr(trade, "symbol", "")
    qty = getattr(trade, "quantity", 0.0)
    price = getattr(trade, "exit_price", 0.0)
    from tradebot.gui.theme import fmt_num, fmt_pct, fmt_price

    msg = (f"VERKAUF {sym}: {fmt_qty(qty)} @ {fmt_price(price)} – Manuell, "
           f"Ergebnis {'+' if pnl >= 0 else ''}{fmt_num(pnl)} ({fmt_pct(pct)})")
    state().feed.publish(TradingMode(mode).value, "sell", msg, "trade", sym,
                         {"symbol": sym, "qty": qty, "price": price, "pnl": pnl, "pnl_pct": pct,
                          "reason": "manual"})


def publish_buy(mode: TradingMode, pos: Any) -> None:
    if feed_covered(mode):
        return
    from tradebot.gui.theme import fmt_num, fmt_price

    sym = getattr(pos, "symbol", "")
    qty = getattr(pos, "quantity", 0.0)
    price = getattr(pos, "entry_price", 0.0)
    msg = f"KAUF {sym}: {fmt_qty(qty)} @ {fmt_price(price)} (Wert {fmt_num(qty * price)}) – manueller Kauf"
    state().feed.publish(TradingMode(mode).value, "buy", msg, "trade", sym,
                         {"symbol": sym, "qty": qty, "price": price, "value": qty * price})


# ---------------------------------------------------------------------------------------
# small UI building blocks
# ---------------------------------------------------------------------------------------

def badge(text: str, kind: str = "") -> ui.label:
    return ui.label(text).classes("tb-badge" + (f" tb-badge-{kind}" if kind else ""))


def banner(kind: str, icon: str, title: str, text: str | None = None):
    """Returns the banner row (context manager) so callers can add buttons into it."""
    color = {"info": "text-primary", "warn": "text-warning", "error": "text-negative"}.get(kind, "")
    row = ui.row().classes(f"tb-banner tb-banner-{kind} w-full items-center no-wrap gap-4")
    with row:
        ui.icon(icon, size="26px").classes(color)
        with ui.column().classes("gap-0 flex-1 min-w-0"):
            ui.label(title).classes("font-semibold")
            if text:
                ui.label(text).classes("tb-muted text-sm")
    return row


class Col:
    """Column of a :func:`table`."""

    def __init__(self, label: str, render: Callable[[dict], Any], align: str = "", tooltip: str | None = None):
        self.label = label
        self.render = render  # returns str (text) or builds UI itself and returns None
        self.align = align
        self.tooltip = tooltip


def table(cols: list[Col], rows: list[dict], empty: str = "Keine Einträge",
          row_class: Callable[[dict], str] | None = None) -> None:
    if not rows:
        ui.label(empty).classes("tb-empty w-full")
        return
    with ui.element("div").classes("tb-tbl-wrap"):
        with ui.element("table").classes("tb-tbl"):
            with ui.element("thead"):
                with ui.element("tr"):
                    for c in cols:
                        th = ui.element("th").classes(c.align)
                        with th:
                            ui.label(c.label)
                            if c.tooltip:
                                ui.tooltip(c.tooltip)
            with ui.element("tbody"):
                for r in rows:
                    with ui.element("tr").classes(row_class(r) if row_class else ""):
                        for c in cols:
                            with ui.element("td").classes(c.align):
                                out = c.render(r)
                                if isinstance(out, tuple):
                                    ui.label(out[0]).classes(out[1])
                                elif out is not None:
                                    ui.label(str(out))


def confirm_dialog(title: str, body: Callable[[], None], ok_label: str, ok_color: str = "primary",
                   on_ok: Callable[[], Any] | None = None, danger: bool = False) -> ui.dialog:
    """Generic confirm dialog. ``on_ok`` may be async; the dialog closes after it finished."""
    with ui.dialog() as dlg, ui.card().classes("tb-card min-w-[420px] max-w-[560px] gap-4" +
                                               (" tb-danger-zone" if danger else "")):
        ui.label(title).classes("text-lg font-semibold")
        body()
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Abbrechen", on_click=dlg.close).props("flat color=grey-6")

            async def ok() -> None:
                btn.props("loading")
                try:
                    if on_ok is not None:
                        res = on_ok()
                        if hasattr(res, "__await__"):
                            await res
                finally:
                    btn.props(remove="loading")
                dlg.close()

            btn = ui.button(ok_label, on_click=ok).props(f"unelevated color={ok_color}")
    return dlg
