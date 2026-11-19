"""Test-Trading (/test) and Live-Trading (/live) pages – one shared builder."""
from __future__ import annotations

import json
import logging

from nicegui import app, run, ui

from tradebot.core.models import TradingMode, WatchItem
from tradebot.gui.components import trade_common as tc
from tradebot.gui.components.tv_chart import TVChart
from tradebot.gui.layout import card, frame
from tradebot.gui.state import state
from tradebot.gui.theme import PRIMARY, fmt_money, fmt_num, fmt_pct, fmt_price, pnl_class

log = logging.getLogger(__name__)

SETTINGS_LIVE_URL = "/settings?tab=live"


@ui.page("/test")
def test_page() -> None:
    with frame("Test-Trading", "Papierhandel mit echten Kursen und virtuellem Geld"):
        build_trading_page(TradingMode.TEST)


@ui.page("/live")
def live_page() -> None:
    with frame("Live-Trading", "Handel mit echtem Geld über deinen Broker"):
        build_trading_page(TradingMode.LIVE)


# ---------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------

def _strategy_options() -> dict[str, str]:
    from tradebot.strategy.presets import PRESETS

    opts: dict[str, str] = {}
    try:
        for name in state().store.list_strategies():
            opts[f"s:{name}"] = name
    except Exception:  # noqa: BLE001
        log.exception("Strategien konnten nicht gelesen werden")
    for p in PRESETS:
        opts[f"p:{p.name}"] = f"Vorlage · {p.name}"
    return opts


def _load_strategy(key: str | None):
    from tradebot.strategy.presets import get_preset

    if not key:
        return None
    kind, _, name = key.partition(":")
    if kind == "s":
        return state().store.load_strategy(name)
    return get_preset(name)


def _p(v: float | None) -> str:
    """Short percentage number: 0,08 / 5 / 2,5."""
    if v is None:
        return "–"
    txt = fmt_num(v, 2)
    return txt.rstrip("0").rstrip(",") if "," in txt else txt


def _risk_summary(strategy) -> str:
    if strategy is None:
        return ""
    r = strategy.risk
    size = (f"{_p(r.position_size_pct)} % je Position" if r.position_size_mode == "pct"
            else f"{fmt_num(r.position_size_fixed)} je Position")
    parts = [f"Zeitrahmen {strategy.timeframe}", size]
    parts.append(f"TP {_p(r.take_profit_pct)} %" if r.take_profit_pct else "kein TP")
    parts.append(f"SL {_p(r.stop_loss_pct)} %" if r.stop_loss_pct else "kein SL")
    if r.trailing_stop_pct:
        parts.append(f"Trailing {_p(r.trailing_stop_pct)} %")
    parts.append(f"max. {r.max_open_positions} Positionen")
    return " · ".join(parts)


def _storage_get(key: str, default=None):
    try:
        return app.storage.general.get(key, default)
    except Exception:  # noqa: BLE001
        return default


def _storage_set(key: str, value) -> None:
    try:
        app.storage.general[key] = value
    except Exception:  # noqa: BLE001
        pass


def _kpi(label: str, value: str = "–", sub: str = ""):
    with ui.card().classes("tb-card gap-1 min-w-0"):
        c = ui.label(label).classes("tb-kpi-label truncate")
        v = ui.label(value).classes("tb-kpi-value truncate")
        s = ui.label(sub).classes("tb-muted text-xs truncate")
    v.cur = lambda cur: c.set_text(f"{label} · {cur}" if cur else label)  # currency next to the label
    return v, s


def _set_cls(label: ui.label, cls: str) -> None:
    label.classes(remove="tb-pos tb-neg")
    if cls:
        label.classes(cls)


# ---------------------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------------------

def build_trading_page(mode: TradingMode) -> None:
    st = state()
    mgr = st.manager
    live = mode == TradingMode.LIVE
    tc.add_css()
    tc.attach_existing(mode)  # events of an already running bot reach the feed / toasts
    settings = st.settings
    dark = getattr(settings, "theme", "dark") != "light"

    # ---- intro banner -----------------------------------------------------------------
    if live:
        with tc.banner("error", "bolt", "Echtgeld-Modus",
                       "Der Bot sendet echte Orders an deinen Broker. Gewinne und Verluste sind real. "
                       f"Schutz: max. {fmt_num(settings.live_max_order_value)} je Order, Notschalter bei "
                       f"{fmt_num(settings.live_daily_loss_limit_pct, 1)} % Tagesverlust, "
                       f"max. {settings.live_max_open_positions} offene Positionen."):
            ui.button("Sicherheit einstellen", icon="tune",
                      on_click=lambda: ui.navigate.to(SETTINGS_LIVE_URL)).props("flat color=negative")
        if not settings.live_enabled:
            with tc.banner("warn", "lock", "Live-Handel ist deaktiviert",
                           "Aktiviere zuerst den Hauptschalter „Live-Handel erlauben“ in den Einstellungen "
                           "(Tab LIVE-Sicherheit). Bis dahin kann kein Live-Bot gestartet werden."):
                ui.button("Zu den Einstellungen", icon="arrow_forward",
                          on_click=lambda: ui.navigate.to(SETTINGS_LIVE_URL)).props("unelevated color=warning")
    else:
        tc.banner("info", "science", "Test-Modus: echte Kurse, virtuelles Geld",
                  "Der Bot handelt mit aktuellen Marktpreisen auf einem simulierten Konto (inkl. Gebühren "
                  "und Slippage). Es wird nichts an den Broker gesendet – ideal, um Strategien gefahrlos "
                  "auszuprobieren.")

    # kill switch banner (LIVE only, shown when triggered)
    kill_box = ui.column().classes("w-full gap-0")
    kill_box.set_visibility(False)
    if live:
        with kill_box:
            with tc.banner("error", "power_settings_new", "Notschalter ausgelöst",
                           "Der Tagesverlust hat das eingestellte Limit erreicht. Der Bot wurde gestoppt, "
                           "offene Positionen bleiben bestehen. Neue Käufe sind erst morgen (UTC) möglich – "
                           "oder nach bewusstem Zurücksetzen."):
                ui.button("Notschalter zurücksetzen", icon="restart_alt",
                          on_click=lambda: _confirm_kill_reset()).props("outline color=negative")

    # ---- control card -----------------------------------------------------------------
    ctl = {"running": None}
    opts = _strategy_options()
    saved_active = settings.active_strategy_live if live else settings.active_strategy_test
    start_key = _storage_get(f"tb_strategy_{mode.value}")
    if start_key not in opts:
        start_key = next((k for k in (f"s:{saved_active}", f"p:{saved_active}") if k in opts), None)
    if start_key is None and opts:
        start_key = next(iter(opts))

    enabled_items = [w for w in settings.watchlist if w.enabled]
    blocked = {f"{w.broker}:{w.symbol}": tc.live_trade_block_reason(w.broker) for w in enabled_items} if live else {}
    stored_sel = _storage_get(f"tb_symbols_{mode.value}")
    all_keys = [f"{w.broker}:{w.symbol}" for w in enabled_items]
    selectable = [k for k in all_keys if not blocked.get(k)]
    selected: set[str] = set(k for k in (stored_sel if isinstance(stored_sel, list) else selectable)
                             if k in selectable)

    with card("gap-4" + (" tb-danger-zone" if live else "")):
        with ui.row().classes("w-full items-center gap-3"):
            ui.label("Bot-Steuerung").classes("tb-section")
            status_pill = ui.label("").classes("tb-badge")
            ui.space()
            start_btn = ui.button("Live-Bot starten" if live else "Test-Bot starten", icon="play_arrow",
                                  on_click=lambda: on_start()).props(
                f"unelevated color={'negative' if live else 'positive'}")
            stop_btn = ui.button("Bot stoppen", icon="stop", on_click=lambda: on_stop()).props(
                "unelevated color=grey-8")

        with ui.row().classes("w-full gap-6 items-start no-wrap max-md:flex-wrap"):
            with ui.column().classes("gap-2 w-[380px] max-md:w-full shrink-0"):
                strat_select = ui.select(opts, value=start_key, label="Strategie",
                                         on_change=lambda e: on_strategy(e.value)).props(
                    "outlined dense options-dense").classes("w-full")
                strat_info = ui.label("").classes("tb-muted text-xs leading-relaxed")
                strat_desc = ui.label("").classes("tb-muted text-xs leading-relaxed")
                ui.link("Strategien verwalten", "/strategies").classes("text-xs text-primary")
            with ui.column().classes("gap-2 flex-1 min-w-0"):
                with ui.row().classes("items-center gap-3 w-full"):
                    ui.label("Zu handelnde Werte").classes("text-sm font-medium")
                    sel_count = ui.label("").classes("tb-muted text-xs")
                    ui.space()
                    all_btn = ui.button("Alle", on_click=lambda: select_all(True)).props("flat dense size=sm")
                    none_btn = ui.button("Keine", on_click=lambda: select_all(False)).props(
                        "flat dense size=sm color=grey-6")
                chips: dict[str, ui.chip] = {}
                with ui.row().classes("gap-2 w-full"):
                    if not enabled_items:
                        with ui.row().classes("items-center gap-2"):
                            ui.label("Die Watchlist enthält keine aktiven Werte.").classes("tb-muted text-sm")
                            ui.link("Watchlist bearbeiten", "/settings").classes("text-sm text-primary")
                    for w in enabled_items:
                        k = f"{w.broker}:{w.symbol}"
                        reason = blocked.get(k)
                        chip = ui.chip(w.symbol, selectable=True, selected=k in selected, color=None,
                                       icon="block" if reason else None,
                                       on_selection_change=lambda e, k=k: on_chip(k, e.value)).classes(
                            "tb-sym-chip")
                        with chip:
                            ui.tooltip(reason or f"{tc.broker_name(w.broker)} · {w.asset_class.value}")
                        if reason:
                            chip.disable()
                        chips[k] = chip
                if live and any(blocked.values()):
                    ui.label("Ausgegraute Werte können nicht live gehandelt werden (Grund im Tooltip). "
                             "Zugangsdaten hinterlegst du unter Einstellungen → Broker.").classes(
                        "tb-muted text-xs")
                elif enabled_items:
                    ui.label("Die Auswahl gilt ab dem nächsten Start. Bestehende Bot-Positionen werden "
                             "unabhängig davon weiter überwacht.").classes("tb-muted text-xs")

        error_box = ui.row().classes("w-full")
        error_box.set_visibility(False)

    def show_error(msg: str | None) -> None:
        error_box.clear()
        if not msg:
            error_box.set_visibility(False)
            return
        with error_box:
            with tc.banner("error", "error_outline", "Bot konnte nicht gestartet werden" if msg else "", msg):
                ui.button(icon="close", on_click=lambda: show_error(None)).props("flat round dense color=grey-6")
        error_box.set_visibility(True)

    def update_sel_count() -> None:
        sel_count.set_text(f"{len(selected)} von {len(all_keys)} ausgewählt")

    def on_chip(k: str, value: bool) -> None:
        if value:
            selected.add(k)
        else:
            selected.discard(k)
        _storage_set(f"tb_symbols_{mode.value}", sorted(selected))
        update_sel_count()

    def select_all(value: bool) -> None:
        for k, chip in chips.items():
            if k in selectable:
                chip.set_selected(value) if hasattr(chip, "set_selected") else setattr(chip, "selected", value)
                (selected.add if value else selected.discard)(k)
        _storage_set(f"tb_symbols_{mode.value}", sorted(selected))
        update_sel_count()

    def on_strategy(key: str | None) -> None:
        _storage_set(f"tb_strategy_{mode.value}", key)
        try:
            s = _load_strategy(key)
        except Exception as exc:  # noqa: BLE001
            strat_info.set_text(f"Strategie konnte nicht geladen werden: {exc}")
            strat_desc.set_text("")
            return
        strat_info.set_text(_risk_summary(s))
        strat_desc.set_text((s.description or "") if s else "")
        strat_desc.set_visibility(bool(s and s.description))

    on_strategy(start_key)
    update_sel_count()

    def set_controls(running: bool) -> None:
        if ctl["running"] == running:
            return
        ctl["running"] = running
        start_btn.set_visibility(not running)
        stop_btn.set_visibility(running)
        status_pill.set_text("Läuft" if running else "Gestoppt")
        status_pill.classes(remove="tb-badge-pos tb-badge-neg tb-badge-live tb-badge-test")
        if running:
            status_pill.classes("tb-badge-live" if live else "tb-badge-pos")
        for el in (strat_select, all_btn, none_btn):
            el.set_enabled(not running)
        for k, chip in chips.items():
            chip.set_enabled(not running and k in selectable)
        if live and not settings.live_enabled:
            start_btn.disable()
            start_btn.tooltip("Live-Handel ist in den Einstellungen deaktiviert")

    # ---- start / stop -----------------------------------------------------------------
    def chosen_watchlist() -> list[WatchItem]:
        return [WatchItem(w.symbol, w.broker, w.asset_class, True) for w in enabled_items
                if f"{w.broker}:{w.symbol}" in selected]

    async def do_start(strategy, items) -> None:
        start_btn.props("loading")
        show_error(None)
        try:
            await st.start_bot(mode, strategy, items)
        except Exception as exc:  # noqa: BLE001  (BotError + unexpected)
            msg = str(exc) or exc.__class__.__name__
            show_error(msg)
            ui.notify(f"Start fehlgeschlagen: {msg}", color="negative", position="bottom-right")
            return
        finally:
            start_btn.props(remove="loading")
        s = st.settings
        if live:
            s.active_strategy_live = strategy.name
        else:
            s.active_strategy_test = strategy.name
        try:
            st.save_settings(s)
        except Exception:  # noqa: BLE001
            log.exception("Einstellungen konnten nicht gespeichert werden")
        await refresh(force=True)

    async def on_start() -> None:
        try:
            strategy = _load_strategy(strat_select.value)
        except Exception as exc:  # noqa: BLE001
            show_error(f"Strategie konnte nicht geladen werden: {exc}")
            return
        if strategy is None:
            show_error("Bitte zuerst eine Strategie auswählen.")
            return
        items = chosen_watchlist()
        if not items:
            show_error("Bitte mindestens einen Wert zum Handeln auswählen.")
            return
        if live:
            if not st.settings.live_enabled:
                show_error("Live-Handel ist in den Einstellungen deaktiviert (Hauptschalter "
                           "„Live-Handel erlauben“ unter Einstellungen → LIVE-Sicherheit).")
                return
            _confirm_live_start(strategy, items)
        else:
            await do_start(strategy, items)

    def _confirm_live_start(strategy, items) -> None:
        s = st.settings

        with ui.dialog() as dlg, ui.card().classes("tb-card tb-danger-zone w-[520px] max-w-full gap-4"):
            with ui.row().classes("items-center gap-3 no-wrap"):
                ui.icon("bolt", size="28px").classes("text-negative")
                with ui.column().classes("gap-0"):
                    ui.label("Live-Bot mit echtem Geld starten?").classes("text-lg font-semibold")
                    ui.label("Bitte prüfe die Angaben sorgfältig.").classes("tb-muted text-sm")
            with ui.element("div").classes("tb-kv w-full"):
                for k, v in (
                    ("Strategie", strategy.name),
                    ("Zeitrahmen", strategy.timeframe),
                    ("Werte", ", ".join(f"{w.symbol} ({tc.broker_name(w.broker)})" for w in items)),
                    ("Max. Orderwert", fmt_num(s.live_max_order_value)),
                    ("Tagesverlust-Limit", fmt_num(s.live_daily_loss_limit_pct, 1) + " % (Notschalter)"),
                    ("Max. offene Positionen", str(s.live_max_open_positions)),
                ):
                    ui.label(k).classes("k")
                    ui.label(v).classes("v whitespace-normal break-words")
            ui.label("Der Bot platziert selbstständig echte Kauf- und Verkaufsorders. Verluste sind möglich. "
                     "Kein Anlagerat.").classes("text-sm text-negative")
            confirm_in = ui.input("Zur Bestätigung LIVE eingeben").props("outlined dense autofocus").classes(
                "w-full")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Abbrechen", on_click=dlg.close).props("flat color=grey-6")

                async def go() -> None:
                    if (confirm_in.value or "").strip() != "LIVE":
                        return
                    dlg.close()
                    await do_start(strategy, items)

                go_btn = ui.button("Echtgeld-Bot starten", icon="bolt", on_click=go).props("unelevated color=negative")
                go_btn.disable()
            confirm_in.on_value_change(lambda e: go_btn.set_enabled((e.value or "").strip() == "LIVE"))
            confirm_in.on("keydown.enter", go)
        dlg.open()

    async def on_stop() -> None:
        stop_btn.props("loading")
        try:
            await mgr.stop(mode)
            ui.notify("Bot gestoppt", position="bottom-right")
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"Stoppen fehlgeschlagen: {exc}", color="negative", position="bottom-right")
        finally:
            stop_btn.props(remove="loading")
        await refresh(force=True)

    def _confirm_kill_reset() -> None:
        def body() -> None:
            ui.label("Damit sind heute wieder Live-Käufe möglich, obwohl das Tagesverlust-Limit erreicht "
                     "wurde. Nur zurücksetzen, wenn du die Ursache verstanden hast.").classes("text-sm")

        async def ok() -> None:
            try:
                bot = mgr.get(mode)
                if bot is None or not bot.running:
                    bot = mgr._bot_for(mode) if hasattr(mgr, "_bot_for") else bot
                if bot is None or not hasattr(bot, "reset_kill_switch"):
                    raise RuntimeError("Zurücksetzen wird nicht unterstützt")
                bot.reset_kill_switch()
                ui.notify("Notschalter zurückgesetzt", color="warning", position="bottom-right")
            except Exception as exc:  # noqa: BLE001
                ui.notify(f"Zurücksetzen fehlgeschlagen: {exc}", color="negative", position="bottom-right")
            await refresh(force=True)

        tc.confirm_dialog("Notschalter zurücksetzen?", body, "Zurücksetzen", "negative", ok, danger=True).open()

    # ---- KPIs -------------------------------------------------------------------------
    with ui.element("div").classes("grid gap-4 w-full grid-cols-2 md:grid-cols-3 xl:grid-cols-6"):
        k_equity = _kpi("Kapital")
        k_cash = _kpi("Freies Guthaben")
        k_pnl = _kpi("G/V gesamt")
        k_real = _kpi("Realisierter G/V")
        k_pos = _kpi("Offene Positionen")
        k_run = _kpi("Läuft seit")

    # ---- chart + events ---------------------------------------------------------------
    with ui.element("div").classes("grid gap-5 w-full grid-cols-1 lg:grid-cols-3"):
        with card("gap-3 lg:col-span-2"):
            with ui.row().classes("w-full items-center"):
                ui.label("Kapitalverlauf").classes("tb-section")
                ui.space()
                chart_hint = ui.label("").classes("tb-muted text-xs")
            chart_wrap = ui.element("div").classes("w-full relative")
            with chart_wrap:
                chart = TVChart("dark" if dark else "light").classes("w-full h-[300px]")
                chart_empty = ui.label("Noch keine Kapitaldaten – sie entstehen, sobald der Bot läuft.").classes(
                    "tb-muted text-sm absolute inset-0 flex items-center justify-center z-10 pointer-events-none")
        with card("gap-2"):
            with ui.row().classes("w-full items-center"):
                ui.label("Ereignisse").classes("tb-section")
                ui.space()
                ui.link("Protokoll", "/log").classes("text-xs text-primary")
            events_box = ui.column().classes("w-full gap-0 tb-scroll h-[300px]")

    # ---- open positions ---------------------------------------------------------------
    with card("gap-2"):
        with ui.row().classes("w-full items-center"):
            ui.label("Offene Positionen").classes("tb-section")
            pos_count = ui.label("").classes("tb-muted text-xs")
            ui.space()
            ui.button("Zur Wallet", icon="account_balance_wallet",
                      on_click=lambda: ui.navigate.to(f"/wallet?tab={mode.value}")).props("flat dense")
        pos_box = ui.column().classes("w-full gap-0")

    # ---- symbol states ----------------------------------------------------------------
    states_card = card("gap-2")
    with states_card:
        with ui.row().classes("w-full items-center"):
            ui.label("Symbol-Status").classes("tb-section")
            ui.label("Pausen nach Verlustserien und Abkühlphasen").classes("tb-muted text-xs")
        states_box = ui.column().classes("w-full gap-0")
    states_card.set_visibility(False)

    # ---- closed trades ----------------------------------------------------------------
    with card("gap-2"):
        with ui.row().classes("w-full items-center"):
            ui.label("Abgeschlossene Trades").classes("tb-section")
            trades_count = ui.label("").classes("tb-muted text-xs")
        trades_box = ui.column().classes("w-full gap-0")

    # ---- TEST: reset account ----------------------------------------------------------
    if not live:
        with card("gap-3"):
            with ui.row().classes("w-full items-center gap-4 no-wrap max-md:flex-wrap"):
                with ui.column().classes("gap-0 flex-1 min-w-0"):
                    ui.label("Test-Konto zurücksetzen").classes("font-semibold")
                    ui.label("Setzt das virtuelle Guthaben zurück und löscht Test-Positionen, Trades und "
                             "Kapitalverlauf. Nur bei gestopptem Test-Bot möglich.").classes("tb-muted text-sm")
                reset_btn = ui.button("Zurücksetzen …", icon="restart_alt",
                                      on_click=lambda: open_reset()).props("outline color=warning")

        def open_reset() -> None:
            if mgr.is_running(mode):
                ui.notify("Bitte den Test-Bot vor dem Zurücksetzen stoppen", color="warning",
                          position="bottom-right")
                return
            holder: dict = {}

            def body() -> None:
                ui.label("Alle Test-Positionen, abgeschlossenen Trades und der Kapitalverlauf werden "
                         "gelöscht. Das kann nicht rückgängig gemacht werden.").classes("text-sm")
                holder["n"] = ui.number("Startkapital", value=float(st.settings.paper_starting_cash or 10000),
                                        min=1, step=1000, format="%.2f").props("outlined dense").classes("w-full")

            async def ok() -> None:
                cash = holder["n"].value
                if not cash or cash <= 0:
                    ui.notify("Bitte ein Startkapital größer 0 angeben", color="warning", position="bottom-right")
                    return
                try:
                    await run.io_bound(mgr.reset_paper, float(cash))
                    acct["t"] = 0.0
                    s = st.settings
                    s.paper_starting_cash = float(cash)
                    st.save_settings(s)
                    ui.notify(f"Test-Konto auf {fmt_num(cash)} zurückgesetzt", color="positive",
                              position="bottom-right")
                except Exception as exc:  # noqa: BLE001
                    ui.notify(f"Zurücksetzen fehlgeschlagen: {exc}", color="negative", position="bottom-right")
                await refresh(force=True)

            tc.confirm_dialog("Test-Konto zurücksetzen?", body, "Zurücksetzen", "warning", ok).open()

    # ---- refresh ----------------------------------------------------------------------
    sigs: dict[str, str] = {}

    def changed(name: str, data) -> bool:
        sig = json.dumps(data, sort_keys=True, default=str)
        if sigs.get(name) == sig:
            return False
        sigs[name] = sig
        return True

    def render_positions(positions: list[dict], cur: str) -> None:
        pos_box.clear()
        with pos_box:
            def sym(r):
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.label(r["symbol"]).classes("font-semibold")
                    if r.get("external_closed"):
                        tc.badge("extern geschlossen", "warn")

            def pnl(r):
                v = r.get("estimated_net_pnl")
                with ui.column().classes("gap-0 items-end"):
                    ui.label(fmt_num(v)).classes(pnl_class(v) + " font-semibold")
                    ui.label(fmt_pct(r.get("estimated_net_pnl_pct"))).classes(pnl_class(v) + " text-xs")

            def stops(r):
                lv = r.get("stop_levels") or {}
                sl = lv.get("stop_loss")
                ts = lv.get("trailing_stop")
                if ts and (sl is None or ts > sl):
                    sl_txt = f"{fmt_price(ts)} (T)"
                else:
                    sl_txt = fmt_price(sl)
                return f"{sl_txt} / {fmt_price(lv.get('take_profit'))}"

            tc.table([
                tc.Col("Symbol", sym),
                tc.Col("Menge", lambda r: tc.fmt_qty(r.get("quantity")), "r"),
                tc.Col("Kaufkurs", lambda r: fmt_price(r.get("entry_price")), "r"),
                tc.Col("Aktuell", lambda r: fmt_price(r.get("current_price")), "r"),
                tc.Col("Wert", lambda r: fmt_num(r.get("value")), "r"),
                tc.Col("G/V netto", pnl, "r", "Geschätzt nach Gebühren und Slippage"),
                tc.Col("Stop / Ziel", stops, "r", "Stop-Loss (T = Trailing-Stop) / Take-Profit"),
                tc.Col("Seit", lambda r: tc.fmt_time_smart(r.get("entry_time")), "r"),
            ], positions, empty="Keine offenen Positionen")

    def render_trades(trades: list[dict]) -> None:
        trades_box.clear()
        rows = list(reversed(trades))[:100]
        with trades_box:
            def pnl_abs(r):
                return fmt_num(r.get("pnl")), pnl_class(r.get("pnl")) + " font-semibold"

            def pnl_pct(r):
                return fmt_pct(r.get("pnl_pct")), pnl_class(r.get("pnl"))

            def dur(r):
                a, b = tc.parse_time(r.get("entry_time")), tc.parse_time(r.get("exit_time"))
                return tc.fmt_duration((b - a).total_seconds()) if a and b else "–"

            def reason(r):
                rs = r.get("exit_reason")
                kind = {"take_profit": "pos", "stop_loss": "neg", "trailing_stop": "neg"}.get(rs, "")
                tc.badge(tc.reason_label(rs), kind)

            tc.table([
                tc.Col("Symbol", lambda r: (r.get("symbol"), "font-semibold")),
                tc.Col("Verkauft", lambda r: tc.fmt_time_smart(r.get("exit_time"))),
                tc.Col("Menge", lambda r: tc.fmt_qty(r.get("quantity")), "r"),
                tc.Col("Kauf", lambda r: fmt_price(r.get("entry_price")), "r"),
                tc.Col("Verkauf", lambda r: fmt_price(r.get("exit_price")), "r"),
                tc.Col("Gebühren", lambda r: fmt_num(r.get("fees")), "r"),
                tc.Col("Haltedauer", dur, "r"),
                tc.Col("Grund", reason),
                tc.Col("G/V", pnl_abs, "r"),
                tc.Col("G/V %", pnl_pct, "r"),
            ], rows, empty="Noch keine abgeschlossenen Trades")
            if len(trades) > len(rows):
                ui.label(f"Die neuesten {len(rows)} von {len(trades)} Trades werden angezeigt.").classes(
                    "tb-muted text-xs pt-2")

    def render_events(events: list[dict]) -> None:
        events_box.clear()
        with events_box:
            if not events:
                ui.label("Noch keine Ereignisse. Starte den Bot, um Käufe, Verkäufe und Hinweise hier "
                         "live zu sehen.").classes("tb-empty")
                return
            # collapse repeated identical hints (e.g. "Kaufsignal nicht ausgeführt …") into one row
            collapsed: list[tuple[dict, int]] = []
            for ev in reversed(events):
                if collapsed and collapsed[-1][0].get("message") == ev.get("message") \
                        and ev.get("kind") in (None, "info", "warning"):
                    collapsed[-1] = (collapsed[-1][0], collapsed[-1][1] + 1)
                else:
                    collapsed.append((ev, 1))
            for ev, count in collapsed[:80]:
                if count > 1:
                    ev = {**ev, "message": f"{ev.get('message', '')}  (×{count})"}
                data = ev.get("data") or {}
                icon, color = tc.kind_style(ev.get("kind"), ev.get("level"), data.get("pnl"))
                with ui.element("div").classes("tb-event w-full"):
                    with ui.element("div").classes("tb-ev-icon").style(
                            f"background: color-mix(in srgb, {color} 16%, transparent); color: {color}"):
                        ui.icon(icon, size="17px")
                    with ui.column().classes("gap-0 min-w-0 flex-1"):
                        ui.label(ev.get("message", "")).classes("text-[13px] leading-snug break-words")
                        ui.label(tc.fmt_time_smart(ev.get("time"))).classes("tb-muted text-[11px]")

    def render_states(states: dict) -> bool:
        rows = []
        for sym, s in sorted((states or {}).items()):
            if s.get("paused") or s.get("cooldown_until") or (s.get("loss_streak") or 0) > 0:
                rows.append({"symbol": sym, **s})
        states_box.clear()
        if not rows:
            return False
        with states_box:
            def status_cell(r):
                if r.get("paused"):
                    if r.get("paused_indefinitely") or not r.get("paused_until"):
                        tc.badge("Pausiert bis Freigabe", "neg")
                    else:
                        tc.badge(f"Pausiert bis {tc.fmt_time_smart(r['paused_until'])}", "warn")
                elif r.get("cooldown_until"):
                    tc.badge(f"Abkühlphase bis {tc.fmt_time_smart(r['cooldown_until'])}")
                else:
                    tc.badge("Aktiv", "pos")

            def action(r):
                if r.get("paused"):
                    ui.button("Freigeben", icon="play_arrow",
                              on_click=lambda _, s=r["symbol"]: resume(s)).props("flat dense size=sm color=primary")

            tc.table([
                tc.Col("Symbol", lambda r: (r["symbol"], "font-semibold")),
                tc.Col("Status", status_cell),
                tc.Col("Verluste in Folge", lambda r: str(r.get("loss_streak") or 0), "r"),
                tc.Col("Letzter Kauf", lambda r: tc.fmt_time_smart(r.get("last_buy_time")), "r"),
                tc.Col("", action, "r"),
            ], rows)
        return True

    async def resume(symbol: str) -> None:
        try:
            await run.io_bound(mgr.resume_symbol, mode, symbol)
            ui.notify(f"{symbol} freigegeben – Käufe wieder erlaubt", color="positive", position="bottom-right")
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"Freigabe fehlgeschlagen: {exc}", color="negative", position="bottom-right")
        await refresh(force=True)

    busy = {"v": False}
    acct = {"t": 0.0, "cash": None, "equity": None}

    async def paper_account() -> None:
        """TEST without running bot: virtual cash/equity from the wallet view (max. every 30 s)."""
        import time

        if live or time.monotonic() - acct["t"] < 30:
            return
        acct["t"] = time.monotonic()
        try:
            w = await mgr.wallet(mode)
            acct["cash"], acct["equity"] = w.get("cash"), w.get("equity")
        except Exception:  # noqa: BLE001
            log.debug("Wallet für Test-Konto nicht abrufbar", exc_info=True)

    async def refresh(force: bool = False) -> None:
        if busy["v"] and not force:
            return
        busy["v"] = True
        try:
            running = mgr.is_running(mode)
            if running:
                status = mgr.status(mode)
            else:
                status = await run.io_bound(mgr.status, mode)
        except Exception as exc:  # noqa: BLE001
            log.exception("Status konnte nicht geladen werden")
            k_run[1].set_text(f"Status nicht verfügbar: {exc}")
            return
        finally:
            busy["v"] = False
        if force:
            sigs.clear()
        running = bool(status.get("running"))
        set_controls(running)
        if live:
            kill_box.set_visibility(bool(status.get("kill_switch")))

        positions = status.get("positions") or []
        cur = tc.common_currency([p.get("symbol") for p in positions] +
                                 [k.split(":", 1)[1] for k in selected])

        # KPIs
        for k in (k_equity, k_cash, k_pnl, k_real):
            k[0].cur(cur)
        eq, cash = status.get("equity"), status.get("cash")
        if not running and not live and cash is None:
            await paper_account()
            cash = acct["cash"]
            if acct["equity"] is not None:
                eq = acct["equity"]
        k_equity[0].set_text(fmt_num(eq))
        k_equity[1].set_text(f"Start {fmt_money(status.get('start_equity'), cur)}" if status.get("start_equity")
                             else "")
        k_cash[0].set_text(fmt_num(cash))
        invested = sum(p.get("value") or 0 for p in positions if not p.get("external_closed"))
        k_cash[1].set_text(f"Investiert {fmt_money(invested, cur)}" if positions else
                           ("Beim Start ermittelt" if cash is None else ("Virtuell" if not live else "")))
        pnl, pnl_pct = status.get("pnl"), status.get("pnl_pct")
        k_pnl[0].set_text(("+" if (pnl or 0) > 0 else "") + fmt_num(pnl))
        _set_cls(k_pnl[0], pnl_class(pnl))
        k_pnl[1].set_text(fmt_pct(pnl_pct) + " seit Start" if pnl_pct is not None else "")
        real = status.get("realized_pnl")
        k_real[0].set_text(("+" if (real or 0) > 0 else "") + fmt_num(real))
        _set_cls(k_real[0], pnl_class(real))
        n_trades = len(status.get("closed_trades") or [])
        wins = sum(1 for t in status.get("closed_trades") or [] if (t.get("pnl") or 0) > 0)
        k_real[1].set_text(f"{n_trades} Trades · {wins} Gewinner" if n_trades else "Noch keine Trades")
        k_pos[0].set_text(str(len(positions)))
        mx = status.get("max_open_positions")
        k_pos[1].set_text(f"max. {mx}" if mx else "")
        if running:
            k_run[0].set_text(tc.fmt_duration(tc.since_seconds(status.get("started_at"))))
            last = status.get("last_loop_at")
            k_run[1].set_text(f"Aktualisiert {tc.fmt_time(last, seconds=True, date=False)}"
                              if last else "Erster Durchlauf läuft …")
        else:
            k_run[0].set_text("Gestoppt")
            k_run[1].set_text(f"Strategie: {status.get('strategy')}" if status.get("strategy") else "")

        # chart
        hist = status.get("equity_history") or []
        if changed("chart", [hist[-1] if hist else None, len(hist), dark]):
            pts, last_t = [], None
            for t, v in hist:
                dt = tc.parse_time(t)
                if dt is None:
                    continue
                ts = int(dt.timestamp())
                if last_t is not None and ts <= last_t:
                    if pts:
                        pts[-1]["value"] = float(v)
                    continue
                pts.append({"time": ts, "value": float(v)})
                last_t = ts
            chart_empty.set_visibility(len(pts) < 1)
            chart.show(lines=[{"title": "Kapital", "color": PRIMARY, "data": pts, "area": True}], volume=False)
            chart_hint.set_text(f"{len(pts)} Punkte · Start {fmt_money(status.get('start_equity'), cur)}"
                                if pts else "")

        if changed("events", status.get("events")):
            render_events(status.get("events") or [])
        pos_view = [{k: v for k, v in p.items() if k not in ("value",)} for p in positions]
        if changed("positions", pos_view):
            render_positions(positions, cur)
        pos_count.set_text(f"{len(positions)} offen · Wert {fmt_money(invested, cur)}" if positions else "")
        trades = status.get("closed_trades") or []
        if changed("trades", [len(trades), trades[-1]["id"] if trades else None]):
            render_trades(trades)
        trades_count.set_text(f"{len(trades)} gesamt" if trades else "")
        if changed("states", status.get("symbol_states")):
            states_card.set_visibility(render_states(status.get("symbol_states") or {}))
        if not live:
            reset_btn.set_enabled(not running)

    ui.timer(2.0, refresh)
    ui.timer(0.05, lambda: refresh(force=True), once=True)
