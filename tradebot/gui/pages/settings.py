"""Einstellungen: Broker & API, Watchlist, Allgemein, LIVE-Sicherheit, Töne, Daten."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from nicegui import run, ui

from tradebot.brokers import broker_infos, create_broker
from tradebot.brokers.base import BrokerInfo
from tradebot.brokers.cache import clear_cache
from tradebot.core.models import AssetClass, TradingMode, WatchItem
from tradebot.core.settings import AppSettings, masked
from tradebot.gui.components.market import broker_names, friendly_error, invalidate_broker
from tradebot.gui.components.widgets import add_widget_css, badge, empty_state, pill
from tradebot.gui.layout import card, frame, play_sound
from tradebot.gui.state import state
from tradebot.gui.theme import fmt_money

log = logging.getLogger(__name__)

ASSET_LABELS = {"crypto": "Krypto", "stock": "Aktie", "etf": "ETF"}
SOUND_LABELS = {
    "buy": ("Kauf", "Eine Position wurde eröffnet"),
    "sell_profit": ("Verkauf mit Gewinn", "Position mit Gewinn geschlossen"),
    "sell_loss": ("Verkauf mit Verlust", "Position mit Verlust geschlossen"),
    "take_profit": ("Take-Profit", "Gewinnziel erreicht"),
    "stop_loss": ("Stop-Loss", "Stop-Loss oder Trailing-Stop ausgelöst"),
    "error": ("Fehler", "Fehler, Warnungen und Notschalter"),
}
TABS = [("broker", "Broker & API", "key"), ("watchlist", "Watchlist", "list"), ("general", "Allgemein", "tune"),
        ("live", "LIVE-Sicherheit", "gpp_maybe"), ("sounds", "Töne", "volume_up"), ("data", "Daten", "storage")]

_symbol_cache: dict[str, list[str]] = {}

SETTINGS_CSS = """
.tb-settings-tabs .q-tab { text-transform: none; font-weight: 600; min-height: 44px; padding: 0 14px; }
.tb-settings-tabs .q-tab__label { font-size: .86rem; }
.tb-settings-panels, .tb-settings-panels .q-tab-panel { background: transparent !important; padding: 0 !important; }
.tb-row { display:grid; grid-template-columns: minmax(0,1.6fr) minmax(0,1fr) minmax(0,1fr) 90px 56px;
          align-items:center; gap:12px; padding:10px 4px; border-top:1px solid var(--tb-border); }
.tb-row.tb-head { border-top:none; font-size:.72rem; font-weight:600; letter-spacing:.05em; text-transform:uppercase;
                  color: var(--tb-muted); padding-top:0; }
.tb-setting { display:flex; align-items:flex-start; justify-content:space-between; gap:24px; padding:14px 0;
              border-top:1px solid var(--tb-border); }
.tb-setting:first-of-type { border-top:none; }
"""


def _save(settings: AppSettings, msg: str = "Einstellungen gespeichert") -> bool:
    try:
        state().save_settings(settings)
    except OSError as exc:
        ui.notify(f"Speichern fehlgeschlagen: {exc}", type="negative")
        return False
    ui.notify(msg, type="positive", position="bottom-right", icon="check")
    return True


def _confirm(title: str, text: str, ok_label: str, on_ok, danger: bool = True) -> None:
    with ui.dialog() as dlg, ui.card().classes("tb-card w-[460px] max-w-[94vw] gap-3"):
        ui.label(title).classes("text-lg font-semibold")
        ui.label(text).classes("tb-muted text-sm")
        with ui.row().classes("w-full justify-end gap-2 pt-2"):
            ui.button("Abbrechen", on_click=dlg.close).props("flat no-caps color=grey-6")

            async def ok() -> None:
                dlg.close()
                res = on_ok()
                if hasattr(res, "__await__"):
                    await res

            ui.button(ok_label, on_click=ok).props(
                f"unelevated no-caps color={'negative' if danger else 'primary'}")
    dlg.open()


def _setting_row(title: str, hint: str = "", hint_classes: str = ""):
    """Two-column row: label + hint left, control right (use as context manager for the control)."""
    row = ui.element("div").classes("tb-setting w-full")
    with row:
        with ui.column().classes("gap-0 flex-1 min-w-0"):
            ui.label(title).classes("text-sm font-medium")
            if hint:
                ui.label(hint).classes(f"tb-field-hint {hint_classes}")
        ctrl = ui.row().classes("items-center gap-2 shrink-0 justify-end")
    return ctrl


def _section_header(title: str, text: str = "", icon: str | None = None) -> None:
    with ui.row().classes("items-center gap-3 no-wrap"):
        if icon:
            with ui.element("div").classes("tb-icon-box").style(
                    "background: color-mix(in srgb, var(--tb-primary) 14%, transparent)"):
                ui.icon(icon, size="20px").classes("text-primary")
        with ui.column().classes("gap-0"):
            ui.label(title).classes("text-base font-semibold")
            if text:
                ui.label(text).classes("tb-muted text-xs")


# =========================================================================================
# Broker & API
# =========================================================================================

def _broker_card(info: BrokerInfo) -> None:
    st = state()
    key = info.key
    saved = st.store.get_credentials(key)
    opts = dict(st.settings.broker_options.get(key) or {})
    inputs: dict[str, ui.input] = {}

    with card("gap-3"):
        with ui.row().classes("w-full items-start justify-between no-wrap"):
            with ui.column().classes("gap-1 min-w-0"):
                ui.label(info.name).classes("text-base font-semibold")
                with ui.row().classes("gap-1"):
                    for ac in info.asset_classes:
                        badge(ASSET_LABELS.get(ac.value, ac.value))
                    badge("Handel" if info.supports_trading else "Nur Kursdaten")
                    if info.has_sandbox:
                        badge("Paper-Konto" if key == "alpaca" else "Testnet")
                    badge(f"Währung {info.quote_currency}")
            status = ui.row().classes("shrink-0")

        def render_status() -> None:
            status.clear()
            with status:
                if not info.credential_fields:
                    pill("Keine Zugangsdaten nötig", "primary")
                elif st.store.has_credentials(key):
                    pill("Zugangsdaten gespeichert", "pos")
                elif info.requires_credentials_for_data:
                    pill("Zugangsdaten fehlen", "warn")
                else:
                    pill("Ohne Zugangsdaten (nur Kurse)", "neutral")

        render_status()
        ui.label(info.description).classes("tb-muted text-sm")

        if info.credential_fields:
            with ui.element("div").classes("grid grid-cols-1 md:grid-cols-2 gap-3 w-full"):
                for f in info.credential_fields:
                    cur = saved.get(f.key)
                    inp = ui.input(f.label, password=True, password_toggle_button=True,
                                   placeholder=f"gespeichert: {masked(cur)}" if cur else "nicht gesetzt").props(
                        "outlined dense autocomplete=off spellcheck=false")
                    if cur:
                        inp.props("stack-label")
                    inputs[f.key] = inp
            ui.label("Gespeicherte Werte werden nur maskiert angezeigt. Leere Felder behalten den "
                     "gespeicherten Wert.").classes("tb-field-hint")

        switches: dict[str, ui.switch] = {}
        if key == "binance":
            switches["sandbox"] = ui.switch("Testnet verwenden (testnet.binance.vision)",
                                            value=bool(opts.get("sandbox"))).props("dense")
            ui.label("Testnet-Konten haben eigene API-Keys und Spielgeld. Ideal, um Live-Abläufe ohne "
                     "Risiko zu prüfen.").classes("tb-field-hint -mt-1 ml-10")
            switches["public_data_mirror"] = ui.switch(
                "Öffentlichen Daten-Mirror nutzen", value=bool(opts.get("public_data_mirror"))).props("dense")
            ui.label("Nur aktivieren, wenn api.binance.com in deiner Region gesperrt ist (Fehler 451). Kursdaten "
                     "kommen dann von data-api.binance.vision; Handel ist damit nicht möglich.").classes(
                "tb-field-hint -mt-1 ml-10")
        elif key == "alpaca":
            switches["paper"] = ui.switch("Paper-Konto verwenden (paper-api.alpaca.markets)",
                                          value=bool(opts.get("paper", True))).props("dense")
            ui.label("Für echtes Geld deaktivieren und die Live-API-Keys eintragen.").classes(
                "tb-field-hint -mt-1 ml-10")

        result = ui.row().classes("w-full items-start gap-2 no-wrap rounded-lg px-3 py-2")
        result.visible = False

        def show_result(ok: bool, text: str) -> None:
            result.clear()
            result.visible = True
            tone = "pos" if ok else "neg"
            result.style(f"background: color-mix(in srgb, var(--tb-{tone}) 10%, transparent)")
            with result:
                ui.icon("check_circle" if ok else "error", size="18px").classes(f"tb-{tone} mt-[1px]")
                ui.label(text).classes("text-sm")

        def collect() -> tuple[dict, dict]:
            creds = dict(st.store.get_credentials(key))
            for k, inp in inputs.items():
                v = (inp.value or "").strip()
                if v:
                    creds[k] = v
            new_opts = dict(st.settings.broker_options.get(key) or {})
            for k, sw in switches.items():
                new_opts[k] = bool(sw.value)
            return creds, new_opts

        def save() -> None:
            creds, new_opts = collect()
            missing = [f.label for f in info.credential_fields if creds and not creds.get(f.key)]
            if missing:
                ui.notify(f"Bitte auch {', '.join(missing)} eintragen", type="warning")
                return
            try:
                if info.credential_fields and any((inp.value or "").strip() for inp in inputs.values()):
                    st.store.set_credentials(key, creds)
                s = st.settings
                s.broker_options[key] = new_opts
                st.save_settings(s)
            except Exception as exc:  # noqa: BLE001
                ui.notify(f"Speichern fehlgeschlagen: {exc}", type="negative")
                return
            invalidate_broker(key)
            for f in info.credential_fields:
                cur = creds.get(f.key)
                inputs[f.key].set_value("")
                inputs[f.key].props(f'placeholder="{"gespeichert: " + masked(cur) if cur else "nicht gesetzt"}"')
            render_status()
            delete_btn.visible = st.store.has_credentials(key)
            ui.notify(f"{info.name}: Einstellungen gespeichert", type="positive", position="bottom-right")

        async def test() -> None:
            creds, new_opts = collect()
            test_btn.props("loading")
            try:
                msg = await run.io_bound(lambda: create_broker(key, creds or None, new_opts).test_connection())
                show_result(True, str(msg))
            except Exception as exc:  # noqa: BLE001
                log.info("Verbindungstest %s fehlgeschlagen: %s", key, exc)
                show_result(False, friendly_error(exc))
            finally:
                test_btn.props(remove="loading")

        def delete() -> None:
            st.store.delete_credentials(key)
            invalidate_broker(key)
            for f in info.credential_fields:
                inputs[f.key].set_value("")
                inputs[f.key].props('placeholder="nicht gesetzt"')
            render_status()
            delete_btn.visible = False
            result.visible = False
            ui.notify(f"{info.name}: Zugangsdaten gelöscht", type="positive", position="bottom-right")

        ui.space()
        with ui.row().classes("w-full items-center gap-2 tb-divider pt-3"):
            if info.credential_fields or switches:
                ui.button("Speichern", icon="save", on_click=save).props("unelevated no-caps color=primary")
            test_btn = ui.button("Verbindung testen", icon="wifi_tethering", on_click=test).props(
                "outline no-caps color=primary")
            ui.space()
            delete_btn = ui.button("Löschen", icon="delete", on_click=lambda: _confirm(
                "Zugangsdaten löschen?", f"Die gespeicherten Zugangsdaten für {info.name} werden entfernt. "
                "Laufende Bots, die diesen Broker nutzen, verlieren beim nächsten Start den Zugriff.",
                "Löschen", delete)).props("flat no-caps color=negative")
            delete_btn.visible = bool(saved)


def broker_tab() -> None:
    st = state()
    with card("gap-2").style("border-color: color-mix(in srgb, var(--tb-primary) 35%, var(--tb-border))"):
        with ui.row().classes("items-start gap-3 no-wrap"):
            ui.icon("shield", size="22px").classes("text-primary mt-[2px]")
            with ui.column().classes("gap-1"):
                ui.label("Sicherheitshinweise").classes("text-sm font-semibold")
                for line in (
                    f"API-Keys werden verschlüsselt lokal gespeichert ({st.store.credentials_path}) – "
                    "der Schlüssel liegt daneben in „.key“. Diese Dateien niemals weitergeben.",
                    "Beim Broker niemals Auszahlungsrechte (Withdraw) für den API-Key vergeben – nur Lesen und "
                    "Spot-Handel.",
                    "Wenn möglich eine IP-Whitelist für den API-Key einrichten und für den Anfang Testnet bzw. "
                    "Paper-Konto nutzen.",
                ):
                    with ui.row().classes("items-start gap-2 no-wrap"):
                        ui.icon("check", size="16px").classes("tb-muted mt-[2px]")
                        ui.label(line).classes("tb-muted text-sm")
    with ui.element("div").classes("grid grid-cols-1 xl:grid-cols-2 gap-5 w-full"):
        for info in broker_infos():
            _broker_card(info)


# =========================================================================================
# Watchlist
# =========================================================================================

def _list_symbols(key: str) -> list[str]:
    if key not in _symbol_cache:
        from tradebot.gui.components.market import broker_for

        _symbol_cache[key] = list(broker_for(key).list_symbols())
    return _symbol_cache[key]


def _guess_asset_class(broker: str, symbol: str) -> str:
    if broker in ("binance", "kraken") or "/" in symbol:
        return "crypto"
    s = symbol.upper()
    if s.endswith(("-USD", "-EUR", "-USDT")):
        return "crypto"
    if s in {"SPY", "QQQ", "VOO", "VTI", "IWM", "DIA", "GLD", "TLT"} or s.split(".")[0] in {
            "VWCE", "EUNL", "IWDA", "EXS1", "SXR8", "EQQQ", "XDWD"}:
        return "etf"
    return "stock"


def watchlist_tab() -> None:
    st = state()
    names = broker_names()
    items = [WatchItem.from_dict(w.to_dict()) for w in st.settings.watchlist]
    dirty = {"v": False}

    with card("gap-3"):
        with ui.row().classes("w-full items-center justify-between"):
            _section_header("Watchlist", "Diese Werte beobachtet und handelt der Bot. Inaktive Werte werden "
                                         "übersprungen.", "list")
            ui.button("Symbol hinzufügen", icon="add", on_click=lambda: open_add()).props(
                "unelevated no-caps color=primary")
        if st.manager.is_running(TradingMode.TEST) or st.manager.is_running(TradingMode.LIVE):
            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.icon("info", size="16px").classes("text-warning")
                ui.label("Ein Bot läuft gerade – Änderungen gelten ab dem nächsten Start des Bots.").classes(
                    "text-xs tb-muted")
        table = ui.column().classes("w-full gap-0")
        with ui.row().classes("w-full items-center gap-2 tb-divider pt-3"):
            save_btn = ui.button("Speichern", icon="save").props("unelevated no-caps color=primary")
            reset_btn = ui.button("Verwerfen", icon="undo").props("flat no-caps color=grey-6")
            dirty_lbl = ui.label("").classes("text-xs text-warning")

    def mark(d: bool) -> None:
        dirty["v"] = d
        save_btn.props(remove="disable") if d else save_btn.props("disable")
        reset_btn.visible = d
        dirty_lbl.set_text("Ungespeicherte Änderungen" if d else "")

    def render() -> None:
        table.clear()
        with table:
            if not items:
                empty_state("playlist_add", "Noch keine Symbole", "Füge mit „Symbol hinzufügen“ Werte hinzu.")
                return
            with ui.element("div").classes("tb-row tb-head w-full"):
                for h in ("Symbol", "Broker", "Anlageklasse", "Aktiv", ""):
                    ui.label(h)
            for idx, it in enumerate(items):
                with ui.element("div").classes("tb-row w-full"):
                    ui.label(it.symbol).classes("text-sm font-semibold truncate")
                    ui.label(names.get(it.broker, it.broker)).classes("text-sm truncate")
                    ui.label(ASSET_LABELS.get(it.asset_class.value, it.asset_class.value)).classes(
                        "text-sm tb-muted")
                    ui.switch(value=it.enabled, on_change=lambda e, i=idx: toggle(i, e.value)).props("dense")
                    ui.button(icon="delete_outline", on_click=lambda i=idx: remove(i)).props(
                        "flat round dense color=grey-6").tooltip("Entfernen")

    def toggle(i: int, v: bool) -> None:
        items[i].enabled = bool(v)
        mark(True)

    def remove(i: int) -> None:
        items.pop(i)
        mark(True)
        render()

    def save() -> None:
        s = st.settings
        s.watchlist = [WatchItem.from_dict(w.to_dict()) for w in items]
        if _save(s, "Watchlist gespeichert"):
            mark(False)

    def reset() -> None:
        items[:] = [WatchItem.from_dict(w.to_dict()) for w in st.settings.watchlist]
        mark(False)
        render()

    save_btn.on_click(save)
    reset_btn.on_click(reset)

    # ---- add dialog -----------------------------------------------------------------------
    with ui.dialog() as dlg, ui.card().classes("tb-card w-[520px] max-w-[94vw] gap-3"):
        ui.label("Symbol hinzufügen").classes("text-lg font-semibold")
        brk = ui.select(names, label="Broker / Datenquelle", value=st.settings.default_broker
                        if st.settings.default_broker in names else "binance").props("outlined dense").classes("w-full")
        sym = ui.select([], label="Symbol", with_input=True, new_value_mode="add-unique").props(
            "outlined dense use-input hide-selected fill-input input-debounce=150 options-dense").classes("w-full")
        hint = ui.label("").classes("tb-field-hint")
        ac = ui.select(dict(ASSET_LABELS), label="Anlageklasse", value="crypto").props(
            "outlined dense").classes("w-full")
        with ui.row().classes("w-full justify-end gap-2 pt-2"):
            ui.button("Abbrechen", on_click=dlg.close).props("flat no-caps color=grey-6")
            ui.button("Hinzufügen", icon="add", on_click=lambda: add()).props("unelevated no-caps color=primary")

    async def load_symbols() -> None:
        key = brk.value
        examples = {"binance": "z. B. BTC/USDT", "kraken": "z. B. BTC/EUR", "alpaca": "z. B. AAPL",
                    "yahoo": "z. B. AAPL, SAP.DE, VWCE.DE, BTC-USD"}
        hint.set_text(f"Symbole werden geladen … ({examples.get(key, '')})")
        try:
            syms = await run.io_bound(_list_symbols, key)
            sym.set_options(syms)
            hint.set_text(f"{len(syms)} Symbole verfügbar – tippen zum Filtern; eigene Eingabe mit Enter "
                          f"übernehmen ({examples.get(key, '')}).")
        except Exception as exc:  # noqa: BLE001
            sym.set_options([])
            hint.set_text(f"Symbolliste nicht verfügbar ({friendly_error(exc)}). Symbol direkt eingeben und mit "
                          f"Enter übernehmen ({examples.get(key, '')}).")

    def on_symbol(_=None) -> None:
        if sym.value:
            ac.set_value(_guess_asset_class(brk.value, sym.value))

    def on_broker(_=None) -> None:
        sym.set_value(None)
        ac.set_value("crypto" if brk.value in ("binance", "kraken") else "stock")
        ui.timer(0, load_symbols, once=True)

    brk.on_value_change(on_broker)
    sym.on_value_change(on_symbol)

    def open_add() -> None:
        sym.set_value(None)
        on_broker()
        dlg.open()

    def add() -> None:
        s = (sym.value or "").strip().upper()
        if not s:
            ui.notify("Bitte ein Symbol auswählen oder eingeben", type="warning")
            return
        if brk.value in ("binance", "kraken") and "/" not in s:
            ui.notify("Krypto-Börsen erwarten ein Paar wie BTC/USDT", type="warning")
            return
        if any(w.symbol == s and w.broker == brk.value for w in items):
            ui.notify(f"{s} ({names.get(brk.value)}) ist bereits in der Watchlist", type="warning")
            return
        items.append(WatchItem(s, brk.value, AssetClass(ac.value or "crypto"), True))
        mark(True)
        render()
        dlg.close()
        ui.notify(f"{s} hinzugefügt – zum Übernehmen „Speichern“ klicken", type="info", position="bottom-right")

    render()
    mark(False)


# =========================================================================================
# Allgemein
# =========================================================================================

def general_tab() -> None:
    st = state()
    s = st.settings
    names = broker_names()
    with ui.element("div").classes("grid grid-cols-1 xl:grid-cols-2 gap-5 w-full items-start"):
        with card("gap-1"):
            _section_header("Bot & Darstellung", "Grundeinstellungen für beide Modi", "tune")
            with _setting_row("Abfrageintervall", "Wie oft der Bot Kurse prüft (Sekunden, 5–3600)."):
                poll = ui.number(value=s.poll_seconds, min=5, max=3600, step=1, format="%d", suffix="s").props(
                    "outlined dense").classes("w-32")
            with _setting_row("Order-Timeout", "Wartezeit auf Limit-/offene Orders, danach wird der Rest "
                                               "storniert (Sekunden, 0–600)."):
                timeout = ui.number(value=s.order_timeout_seconds, min=0, max=600, step=1, format="%d",
                                    suffix="s").props("outlined dense").classes("w-32")
            with _setting_row("Standard-Broker", "Vorauswahl für neue Watchlist-Einträge und Charts."):
                default_broker = ui.select(names, value=s.default_broker if s.default_broker in names
                                           else "binance").props("outlined dense").classes("w-44")
            with _setting_row("Farbschema", "Dunkles oder helles Erscheinungsbild."):
                theme = ui.select({"dark": "Dunkel", "light": "Hell"}, value=s.theme if s.theme in (
                    "dark", "light") else "dark").props("outlined dense").classes("w-44")

            def save() -> None:
                vals = {"poll": poll.value, "timeout": timeout.value}
                if vals["poll"] is None or not 5 <= vals["poll"] <= 3600:
                    ui.notify("Abfrageintervall muss zwischen 5 und 3600 Sekunden liegen", type="warning")
                    return
                if vals["timeout"] is None or not 0 <= vals["timeout"] <= 600:
                    ui.notify("Order-Timeout muss zwischen 0 und 600 Sekunden liegen", type="warning")
                    return
                cur = st.settings
                theme_changed = cur.theme != theme.value
                cur.poll_seconds = int(vals["poll"])
                cur.order_timeout_seconds = int(vals["timeout"])
                cur.default_broker = default_broker.value or "binance"
                cur.theme = theme.value or "dark"
                if _save(cur) and theme_changed:
                    ui.navigate.reload()

            with ui.row().classes("w-full tb-divider pt-3"):
                ui.button("Speichern", icon="save", on_click=save).props("unelevated no-caps color=primary")

        with card("gap-1"):
            _section_header("Test-Konto (Papierhandel)", "Virtuelles Startkapital für den TEST-Modus", "science")
            with _setting_row("Startkapital", "Gilt beim nächsten Zurücksetzen des Test-Kontos."):
                cash = ui.number(value=s.paper_starting_cash, min=10, max=100_000_000, step=100,
                                 format="%.2f").props("outlined dense").classes("w-44")

            def save_cash() -> None:
                if cash.value is None or cash.value < 10:
                    ui.notify("Startkapital muss mindestens 10 betragen", type="warning")
                    return
                cur = st.settings
                cur.paper_starting_cash = float(cash.value)
                _save(cur, "Startkapital gespeichert")

            def do_reset() -> None:
                if st.manager.is_running(TradingMode.TEST):
                    ui.notify("Der Test-Bot läuft – bitte zuerst im Test-Trading stoppen.", type="warning")
                    return
                amount = float(cash.value or st.settings.paper_starting_cash)
                try:
                    st.manager.reset_paper(amount)
                except Exception as exc:  # noqa: BLE001
                    ui.notify(f"Zurücksetzen nicht möglich: {exc}", type="negative")
                    return
                cur = st.settings
                cur.paper_starting_cash = amount
                st.save_settings(cur)
                st.feed.publish("test", "info", f"Test-Konto auf {fmt_money(amount)} zurückgesetzt")
                ui.notify(f"Test-Konto auf {fmt_money(amount)} zurückgesetzt", type="positive")

            def ask_reset() -> None:
                if st.manager.is_running(TradingMode.TEST):
                    ui.notify("Der Test-Bot läuft – bitte zuerst im Test-Trading stoppen.", type="warning")
                    return
                _confirm("Test-Konto zurücksetzen?",
                         f"Alle virtuellen Positionen, Trades und der Kontoverlauf des TEST-Modus werden gelöscht. "
                         f"Das Konto startet mit {fmt_money(cash.value or 0)} neu. Das LIVE-Konto ist nicht "
                         "betroffen.", "Zurücksetzen", do_reset)

            with ui.row().classes("w-full tb-divider pt-3 gap-2"):
                ui.button("Speichern", icon="save", on_click=save_cash).props("unelevated no-caps color=primary")
                ui.space()
                ui.button("Test-Konto zurücksetzen", icon="restart_alt", on_click=ask_reset).props(
                    "outline no-caps color=negative")


# =========================================================================================
# LIVE-Sicherheit
# =========================================================================================

def _reset_kill_switch() -> None:
    st = state()
    bot = st.manager.get(TradingMode.LIVE)
    if bot is not None and bot.running and hasattr(bot, "reset_kill_switch"):
        bot.reset_kill_switch()
        return
    from tradebot.engine.bot import journal_path
    from tradebot.engine.journal import TradeJournal

    j = TradeJournal(journal_path(st.store, TradingMode.LIVE))
    j.kill_switch_date = None
    j.daily_start = None
    j.save()


def live_tab() -> None:
    st = state()
    s = st.settings
    with card("gap-1 tb-danger-zone"):
        with ui.row().classes("w-full items-start justify-between no-wrap gap-4"):
            with ui.row().classes("items-center gap-3 no-wrap"):
                with ui.element("div").classes("tb-icon-box").style(
                        "background: color-mix(in srgb, var(--tb-neg) 16%, transparent)"):
                    ui.icon("bolt", size="22px").classes("tb-neg")
                with ui.column().classes("gap-0"):
                    ui.label("LIVE-Handel mit echtem Geld").classes("text-base font-semibold")
                    ui.label("Der Hauptschalter muss aktiv sein, bevor ein LIVE-Bot gestartet werden kann.").classes(
                        "tb-muted text-xs")
            state_pill = ui.row().classes("shrink-0")

        def render_pill() -> None:
            state_pill.clear()
            with state_pill:
                if st.settings.live_enabled:
                    pill("LIVE freigegeben", "neg")
                else:
                    pill("LIVE gesperrt", "neutral")

        render_pill()
        with _setting_row("Hauptschalter LIVE-Handel",
                          "Erlaubt dem Bot, echte Orders über deinen Broker zu senden. Zum Aktivieren ist eine "
                          "ausdrückliche Bestätigung nötig."):
            master = ui.switch(value=s.live_enabled).props("color=negative")

        guard = {"skip": False}

        def on_master(e) -> None:
            if guard["skip"]:
                guard["skip"] = False
                return
            if e.value:
                guard["skip"] = True
                master.set_value(False)  # only after explicit confirmation
                open_confirm()
            else:
                cur = st.settings
                cur.live_enabled = False
                _save(cur, "LIVE-Handel gesperrt")
                render_pill()

        master.on_value_change(on_master)

        with ui.dialog() as dlg, ui.card().classes("tb-card w-[520px] max-w-[94vw] gap-3"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("warning", size="24px").classes("tb-neg")
                ui.label("LIVE-Handel aktivieren").classes("text-lg font-semibold")
            ui.label("Im LIVE-Modus handelt der Bot mit echtem Geld auf deinem Broker-Konto. Verluste sind "
                     "möglich und können nicht rückgängig gemacht werden. Teste deine Strategie vorher im "
                     "Backtest und im Test-Trading.").classes("text-sm")
            ack = ui.checkbox("Ich habe verstanden, dass ich echtes Geld verlieren kann und allein für die "
                              "Handelsentscheidungen des Bots verantwortlich bin.").classes("text-sm")
            word = ui.input("Zur Bestätigung LIVE eingeben").props("outlined dense autocomplete=off")
            with ui.row().classes("w-full justify-end gap-2 pt-2"):
                ui.button("Abbrechen", on_click=dlg.close).props("flat no-caps color=grey-6")
                ok_btn = ui.button("LIVE-Handel aktivieren", icon="bolt").props(
                    "unelevated no-caps color=negative disable")

        def validate(_=None) -> None:
            ok = bool(ack.value) and (word.value or "").strip() == "LIVE"
            ok_btn.props(remove="disable") if ok else ok_btn.props("disable")

        ack.on_value_change(validate)
        word.on_value_change(validate)

        def open_confirm() -> None:
            ack.set_value(False)
            word.set_value("")
            validate()
            dlg.open()

        def confirm() -> None:
            if not (ack.value and (word.value or "").strip() == "LIVE"):
                return
            cur = st.settings
            cur.live_enabled = True
            if _save(cur, "LIVE-Handel freigegeben"):
                guard["skip"] = True
                master.set_value(True)
                st.feed.publish("app", "info", "LIVE-Hauptschalter aktiviert")
                render_pill()
            dlg.close()

        ok_btn.on_click(confirm)

    with ui.element("div").classes("grid grid-cols-1 xl:grid-cols-2 gap-5 w-full items-start"):
        with card("gap-1"):
            _section_header("Harte Limits", "Gelten zusätzlich zu den Risiko-Regeln der Strategie", "rule")
            with _setting_row("Maximaler Orderwert", "Obergrenze je LIVE-Order in der Quote-Währung "
                                                     "(z. B. USDT/EUR/USD)."):
                max_order = ui.number(value=s.live_max_order_value, min=1, max=10_000_000, step=10,
                                      format="%.2f").props("outlined dense").classes("w-40")
            with _setting_row("Tagesverlust-Limit (Notschalter)", "Fällt der Kontowert an einem Tag um mehr als "
                                                                  "diesen Prozentsatz, stoppt der LIVE-Bot alle "
                                                                  "Käufe."):
                loss = ui.number(value=s.live_daily_loss_limit_pct, min=0.1, max=100, step=0.5, format="%.1f",
                                 suffix="%").props("outlined dense").classes("w-40")
            with _setting_row("Maximal offene Positionen", "Höchstzahl gleichzeitiger LIVE-Positionen."):
                max_pos = ui.number(value=s.live_max_open_positions, min=1, max=100, step=1, format="%d").props(
                    "outlined dense").classes("w-40")

            def save_limits() -> None:
                if max_order.value is None or max_order.value <= 0:
                    ui.notify("Maximaler Orderwert muss größer als 0 sein", type="warning")
                    return
                if loss.value is None or not 0 < loss.value <= 100:
                    ui.notify("Tagesverlust-Limit muss zwischen 0 und 100 % liegen", type="warning")
                    return
                if max_pos.value is None or max_pos.value < 1:
                    ui.notify("Mindestens 1 offene Position erlauben", type="warning")
                    return
                cur = st.settings
                cur.live_max_order_value = float(max_order.value)
                cur.live_daily_loss_limit_pct = float(loss.value)
                cur.live_max_open_positions = int(max_pos.value)
                _save(cur, "LIVE-Limits gespeichert")

            with ui.row().classes("w-full tb-divider pt-3"):
                ui.button("Speichern", icon="save", on_click=save_limits).props("unelevated no-caps color=primary")

        with card("gap-2"):
            _section_header("Notschalter", "Status des Tagesverlust-Notschalters im LIVE-Modus",
                            "power_settings_new")
            ks_box = ui.column().classes("w-full gap-2")

            async def render_ks() -> None:
                try:
                    status = await run.io_bound(st.manager.status, TradingMode.LIVE)
                except Exception as exc:  # noqa: BLE001
                    status = {"_error": friendly_error(exc)}
                ks_box.clear()
                with ks_box:
                    if status.get("_error"):
                        ui.label(f"Status nicht verfügbar: {status['_error']}").classes("tb-muted text-sm")
                        return
                    active = bool(status.get("kill_switch"))
                    with ui.row().classes("items-center gap-2"):
                        pill("Ausgelöst" if active else "Nicht ausgelöst", "neg" if active else "pos")
                        ui.label("LIVE-Bot läuft" if status.get("running") else "LIVE-Bot gestoppt").classes(
                            "tb-muted text-xs")
                    if active:
                        ui.label("Der Notschalter blockiert heute weitere LIVE-Käufe. Setze ihn nur zurück, wenn du "
                                 "die Ursache verstanden hast.").classes("text-sm")
                        ui.button("Notschalter zurücksetzen", icon="restart_alt", on_click=lambda: _confirm(
                            "Notschalter zurücksetzen?", "Der LIVE-Bot darf danach heute wieder kaufen. Das "
                            "Tagesverlust-Limit wird ab dem aktuellen Kontowert neu berechnet.", "Zurücksetzen",
                            do_reset_ks)).props("outline no-caps color=negative")
                    else:
                        ui.label("Wird das Tagesverlust-Limit erreicht, stoppt der Bot alle Käufe bis zum nächsten "
                                 "Tag oder bis zum manuellen Zurücksetzen.").classes("tb-muted text-sm")

            async def do_reset_ks() -> None:
                try:
                    await run.io_bound(_reset_kill_switch)
                    st.feed.publish("live", "info", "Notschalter manuell zurückgesetzt")
                    ui.notify("Notschalter zurückgesetzt", type="positive")
                except Exception as exc:  # noqa: BLE001
                    ui.notify(f"Zurücksetzen fehlgeschlagen: {exc}", type="negative")
                await render_ks()

            ui.timer(0.05, render_ks, once=True)


# =========================================================================================
# Töne
# =========================================================================================

def sounds_tab() -> None:
    st = state()
    s = st.settings
    events = dict(getattr(s, "sound_events", {}) or {})
    with card("gap-1 max-w-[860px]"):
        _section_header("Töne", "Akustische Signale bei Bot-Ereignissen (in diesem Fenster)", "volume_up")
        with _setting_row("Töne aktiviert", "Hauptschalter für alle Töne (auch über das Lautsprecher-Symbol "
                                            "oben rechts)."):
            enabled = ui.switch(value=bool(getattr(s, "sound_enabled", True)))
        with _setting_row("Lautstärke", ""):
            vol = ui.slider(min=0, max=100, step=5, value=round(float(getattr(s, "sound_volume", 0.6)) * 100)).props(
                "label").classes("w-56")
            vol_lbl = ui.label("").classes("text-sm w-12 text-right")
            vol_lbl.bind_text_from(vol, "value", backward=lambda v: f"{int(v or 0)} %")
        toggles: dict[str, ui.switch] = {}
        ui.label("Ereignisse").classes("tb-section mt-4 mb-1")
        for key, (label, hint) in SOUND_LABELS.items():
            with _setting_row(label, hint):
                def preview(k=key) -> None:
                    vol_now = float(vol.value or 0) / 100
                    ui.run_javascript(f"window.tbSound && window.tbSound({k!r}, {vol_now})")

                ui.button(icon="play_arrow", on_click=preview).props("flat round dense color=primary").tooltip(
                    "Probe hören")
                toggles[key] = ui.switch(value=bool(events.get(key, True)))

        def save() -> None:
            cur = st.settings
            cur.sound_enabled = bool(enabled.value)
            cur.sound_volume = min(max(float(vol.value or 0) / 100, 0.0), 1.0)
            ev = dict(getattr(cur, "sound_events", {}) or {})
            ev.update({k: bool(t.value) for k, t in toggles.items()})
            cur.sound_events = ev
            if _save(cur, "Ton-Einstellungen gespeichert") and cur.sound_enabled:
                play_sound("info", force=True)

        with ui.row().classes("w-full tb-divider pt-3"):
            ui.button("Speichern", icon="save", on_click=save).props("unelevated no-caps color=primary")


# =========================================================================================
# Daten
# =========================================================================================

def _dir_size(path: Path) -> tuple[int, int]:
    total = n = 0
    if path.exists():
        for f in path.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                    n += 1
                except OSError:
                    pass
    return total, n


def _fmt_bytes(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024 or unit == "GB":
            return (f"{b:.0f} {unit}" if unit == "B" else f"{b:.1f} {unit}").replace(".", ",")
        b /= 1024
    return f"{b} B"


def data_tab() -> None:
    st = state()
    store = st.store
    with ui.element("div").classes("grid grid-cols-1 xl:grid-cols-2 gap-5 w-full items-start"):
        with card("gap-1"):
            _section_header("Speicherorte", "Alle Daten liegen lokal auf diesem Rechner", "folder")
            for title, path in (("Datenverzeichnis", store.base_dir), ("Strategien", store.strategies_dir),
                                ("Bot-Zustand & Journale", store.state_dir), ("Protokolle", store.logs_dir),
                                ("Kursdaten-Cache", store.cache_dir)):
                with _setting_row(title, str(path), "tb-mono break-all"):
                    async def copy(p=path) -> None:
                        await ui.clipboard.write(str(p))
                        ui.notify("Pfad kopiert", position="bottom-right")

                    ui.button(icon="content_copy", on_click=copy).props("flat round dense color=grey-6").tooltip(
                        "Pfad kopieren")
            ui.label("Tipp: Mit der Umgebungsvariable TRADEBOT_HOME lässt sich ein anderes Datenverzeichnis "
                     "festlegen.").classes("tb-field-hint pt-2")

        with ui.column().classes("gap-5 w-full"):
            with card("gap-2"):
                _section_header("Kursdaten-Cache", "Heruntergeladene Kerzen für Charts und Backtests", "cached")
                size_lbl = ui.label("…").classes("text-sm")

                async def refresh_size() -> None:
                    total, n = await run.io_bound(_dir_size, store.cache_dir)
                    size_lbl.set_text(f"{_fmt_bytes(total)} in {n} Dateien")

                async def do_clear() -> None:
                    n = await run.io_bound(clear_cache, store.cache_dir)
                    invalidate_broker()
                    ui.notify(f"Cache geleert ({n} Dateien entfernt)", type="positive")
                    await refresh_size()

                ui.label("Der Cache beschleunigt Charts und Backtests. Leeren ist gefahrlos – Daten werden bei "
                         "Bedarf neu geladen.").classes("tb-muted text-sm")
                with ui.row().classes("w-full tb-divider pt-3"):
                    ui.button("Cache leeren", icon="delete_sweep", on_click=lambda: _confirm(
                        "Cache leeren?", "Alle zwischengespeicherten Kursdaten werden gelöscht und bei Bedarf neu "
                        "heruntergeladen.", "Cache leeren", do_clear)).props("outline no-caps color=negative")
                ui.timer(0.05, refresh_size, once=True)

            with card("gap-2"):
                _section_header("Export & Import", "Einstellungen sichern oder übertragen (ohne Zugangsdaten)",
                                "import_export")

                def export() -> None:
                    data = st.settings.to_dict()
                    data["_export"] = {"app": "TradingBot", "time": datetime.now().isoformat(timespec="seconds"),
                                       "note": "Ohne API-Zugangsdaten"}
                    ui.download.content(json.dumps(data, indent=2, ensure_ascii=False),
                                        f"tradebot-einstellungen-{datetime.now():%Y%m%d}.json",
                                        "application/json")

                async def on_upload(e) -> None:
                    try:
                        raw = json.loads(await e.file.text())
                        if not isinstance(raw, dict) or "watchlist" not in raw:
                            raise ValueError("Keine TradeBot-Einstellungsdatei")
                        new = AppSettings.from_dict(raw)
                    except (ValueError, UnicodeDecodeError) as exc:
                        ui.notify(f"Import fehlgeschlagen: {exc}", type="negative")
                        upload.reset()
                        return
                    new.live_enabled = False  # never enable real-money trading through a file
                    _save(new, f"Einstellungen importiert ({len(new.watchlist)} Watchlist-Einträge). "
                               "LIVE-Hauptschalter ist aus Sicherheitsgründen deaktiviert.")
                    invalidate_broker()
                    upload.reset()
                    ui.timer(1.5, ui.navigate.reload, once=True)

                ui.label("Der Export enthält Watchlist, Broker-Optionen, Limits und Töne – keine API-Keys. Beim "
                         "Import wird der LIVE-Hauptschalter immer ausgeschaltet.").classes("tb-muted text-sm")
                with ui.row().classes("w-full items-center gap-2 tb-divider pt-3"):
                    ui.button("Exportieren", icon="download", on_click=export).props(
                        "unelevated no-caps color=primary")
                    upload = ui.upload(auto_upload=True, max_files=1, max_file_size=2_000_000,
                                       on_upload=on_upload).props("accept=.json").classes("hidden")
                    ui.button("Importieren …", icon="upload", on_click=lambda: upload.run_method("pickFiles")).props(
                        "outline no-caps color=primary")


# =========================================================================================

@ui.page("/settings")
def page(tab: str | None = None) -> None:
    add_widget_css()
    ui.add_css(SETTINGS_CSS)
    keys = [t[0] for t in TABS]
    with frame("Einstellungen", "Broker-Zugänge, Watchlist, Sicherheit und Darstellung"):
        with ui.tabs(value=tab if tab in keys else "broker").props(
                "align=left inline-label no-caps active-color=primary indicator-color=primary dense").classes(
                "tb-settings-tabs w-full").style("border-bottom:1px solid var(--tb-border)") as tabs:
            for key, label, icon in TABS:
                t = ui.tab(key, label=label, icon=icon)
                if key == "live":
                    t.props("active-color=negative")
        with ui.tab_panels(tabs, value=tab if tab in keys else "broker").props("keep-alive").classes(
                "tb-settings-panels w-full"):
            for key, fn in (("broker", broker_tab), ("watchlist", watchlist_tab), ("general", general_tab),
                            ("live", live_tab), ("sounds", sounds_tab), ("data", data_tab)):
                with ui.tab_panel(key).classes("gap-5 p-0"):
                    fn()
