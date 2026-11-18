"""Wallet page: bot positions with current prices, manual sell/buy, LIVE broker holdings."""
from __future__ import annotations

import logging

from nicegui import background_tasks, run, ui

from tradebot.core.models import TradingMode
from tradebot.gui.components import trade_common as tc
from tradebot.gui.layout import card, frame
from tradebot.gui.state import state
from tradebot.gui.theme import fmt_money, fmt_num, fmt_pct, fmt_price, pnl_class

log = logging.getLogger(__name__)

REFRESH_SECONDS = 10.0


@ui.page("/wallet")
def page(tab: str = "test") -> None:
    with frame("Wallet", "Bot-Positionen mit aktuellen Kursen – manuell verkaufen oder kaufen"):
        tc.add_css()
        ui.add_css("""
        .tb-wallet-tabs .q-tab { text-transform: none; font-weight: 600; min-height: 42px; padding: 0 18px; }
        .tb-wallet-tabs .q-tab--active.tb-tab-live { color: var(--tb-neg); }
        .tb-wallet-tabs .q-tab--active.tb-tab-live .q-tab__indicator { background: var(--tb-neg); }
        .tb-wallet-panels { background: transparent !important; }
        .tb-wallet-panels .q-tab-panel { padding: 0; }
        """)
        initial = "live" if tab == "live" else "test"
        with ui.tabs(value=initial).classes("tb-wallet-tabs").props("align=left inline-label no-caps") as tabs:
            ui.tab("test", label="TEST · Papierhandel", icon="science")
            ui.tab("live", label="LIVE · Echtgeld", icon="bolt").classes("tb-tab-live")
        panels = {}
        with ui.tab_panels(tabs, value=initial).classes("w-full tb-wallet-panels").props("keep-alive animated=false"):
            with ui.tab_panel("test"):
                panels["test"] = WalletPanel(TradingMode.TEST, tabs)
            with ui.tab_panel("live"):
                panels["live"] = WalletPanel(TradingMode.LIVE, tabs)

        def on_tab(e) -> None:
            ui.navigate.history.replace(f"/wallet?tab={e.value}")
            p = panels.get(e.value)
            if p is not None:
                background_tasks.create(p.refresh())

        tabs.on_value_change(on_tab)


class WalletPanel:
    def __init__(self, mode: TradingMode, tabs: ui.tabs) -> None:
        self.mode = mode
        self.live = mode == TradingMode.LIVE
        self.tabs = tabs
        self.data: dict | None = None
        self.error: str | None = None
        self.loading = False
        self.mgr = state().manager
        tc.attach_existing(mode)
        self._build()
        ui.timer(REFRESH_SECONDS, self._tick)
        if tabs.value == mode.value:
            ui.timer(0.05, self.refresh, once=True)

    # ---- layout -----------------------------------------------------------------------
    def _build(self) -> None:
        with ui.column().classes("w-full gap-5 pt-4"):
            if self.live:
                with tc.banner("error", "bolt", "Echtgeld",
                               "Diese Positionen liegen auf deinem echten Broker-Konto. Verkäufe und Käufe "
                               "werden sofort als echte Orders ausgeführt."):
                    pass
            # toolbar
            with ui.row().classes("w-full items-center gap-2"):
                self.updated = ui.label("").classes("tb-muted text-xs")
                self.running_lbl = ui.label("").classes("tb-badge")
                ui.space()
                ui.button("Manuell kaufen", icon="add_shopping_cart", on_click=self.open_buy).props(
                    "flat color=primary")
                self.sell_all_btn = ui.button("Alle verkaufen", icon="sell", on_click=self.open_sell_all).props(
                    "flat color=negative")
                self.refresh_btn = ui.button("Aktualisieren", icon="refresh", on_click=self.refresh).props(
                    "outline color=grey-6")
            self.body = ui.column().classes("w-full gap-5")
            self.dialog_host = ui.element("div")  # dialogs live outside the re-rendered body
            self._skeleton()

    def _skeleton(self) -> None:
        self.body.clear()
        with self.body:
            with ui.element("div").classes("grid gap-4 w-full grid-cols-2 md:grid-cols-3 xl:grid-cols-5"):
                for _ in range(5):
                    with ui.card().classes("tb-card gap-2"):
                        ui.skeleton(type="text").classes("w-1/2")
                        ui.skeleton(type="rect", height="28px").classes("w-3/4")
            with card("gap-3"):
                ui.skeleton(type="text").classes("w-40")
                for _ in range(3):
                    ui.skeleton(type="rect", height="34px").classes("w-full")

    # ---- data -------------------------------------------------------------------------
    async def _tick(self) -> None:
        if self.tabs.value == self.mode.value:
            await self.refresh()

    async def refresh(self) -> None:
        if self.loading:
            return
        self.loading = True
        self.refresh_btn.props("loading")
        try:
            self.data = await self.mgr.wallet(self.mode)
            self.error = None
        except Exception as exc:  # noqa: BLE001
            log.warning("Wallet (%s) nicht abrufbar: %s", self.mode.value, exc)
            self.error = str(exc) or exc.__class__.__name__
        finally:
            self.loading = False
            self.refresh_btn.props(remove="loading")
        self.render()

    # ---- render -----------------------------------------------------------------------
    def render(self) -> None:
        d = self.data
        self.body.clear()
        with self.body:
            if self.error and d is None:
                with tc.banner("error", "cloud_off", "Wallet konnte nicht geladen werden", self.error):
                    ui.button("Erneut versuchen", icon="refresh", on_click=self.refresh).props("flat color=negative")
                return
            if self.error:
                tc.banner("warn", "sync_problem", "Aktualisierung fehlgeschlagen – letzte Daten werden angezeigt",
                          self.error)
            if d is None:
                return
            self.updated.set_text(f"Stand {tc.fmt_time(d.get('time'), seconds=True, date=False)} · "
                                  f"aktualisiert alle {int(REFRESH_SECONDS)} s")
            running = d.get("running")
            self.running_lbl.set_text(("Bot läuft" if running else "Bot gestoppt"))
            self.running_lbl.classes(remove="tb-badge-pos tb-badge-live")
            if running:
                self.running_lbl.classes("tb-badge-live" if self.live else "tb-badge-pos")

            positions = d.get("positions") or []
            totals = d.get("totals") or {}
            cur = tc.common_currency(p.get("symbol") for p in positions)
            active = [p for p in positions if not p.get("external_closed")]
            self.sell_all_btn.set_enabled(bool(active))
            no_access = self.live and not self._live_access()
            if no_access:
                with tc.banner("info", "key_off", "Kein Broker-Konto verbunden",
                               "Für den Echtgeld-Kontostand werden ein handelsfähiger Broker in der Watchlist und "
                               "hinterlegte API-Zugangsdaten benötigt."):
                    ui.button("Zugangsdaten hinterlegen", icon="key",
                              on_click=lambda: ui.navigate.to("/settings")).props("flat color=primary")
                d = dict(d, cash=None, equity=None)

            # KPIs
            cost = totals.get("cost") or 0.0
            upnl = totals.get("unrealized_pnl")
            with ui.element("div").classes("grid gap-4 w-full grid-cols-2 md:grid-cols-3 xl:grid-cols-5"):
                self._kpi("Gesamtwert", fmt_num(d.get("equity")),
                          f"Start {fmt_money(d.get('start_equity'), cur)}" if d.get("start_equity") else "", cur=cur)
                self._kpi("Freies Guthaben", fmt_num(d.get("cash")),
                          "Beim Broker" if self.live else "Virtuell", cur=cur)
                self._kpi("Investiert", fmt_num(totals.get("value")),
                          f"{totals.get('positions', 0)} Pos. · Einstand {fmt_num(cost)}", cur=cur)
                self._kpi("Unrealisierter G/V", _signed(upnl),
                          (fmt_pct(upnl / cost * 100) + " · netto " + _signed(totals.get("estimated_net_pnl")))
                          if cost else "", pnl_class(upnl), cur=cur)
                self._kpi("Realisierter G/V", _signed(totals.get("realized_pnl")),
                          "aus Bot-Trades", pnl_class(totals.get("realized_pnl")), cur=cur)

            errors = d.get("errors") or []
            if errors:
                with tc.banner("warn", "warning", "Einige Daten sind nicht aktuell"):
                    pass
                with ui.column().classes("gap-0 -mt-3 pl-14"):
                    for e in errors[:8]:
                        ui.label(e).classes("tb-muted text-xs")

            external = [p for p in positions if p.get("external_closed")]
            if external:
                tc.banner("warn", "link_off", f"{len(external)} Position(en) beim Broker nicht mehr vorhanden",
                          "Vermutlich außerhalb des Bots verkauft. Prüfe dein Broker-Konto und entferne die "
                          "Position anschließend aus dem Journal.")

            with card("gap-2"):
                with ui.row().classes("w-full items-center"):
                    ui.label("Bot-Positionen").classes("tb-section")
                    if positions:
                        ui.label(f"{len(positions)} offen").classes("tb-muted text-xs")
                    ui.space()
                    ui.label("Nur Positionen, die der Bot (oder du hier) eröffnet hat.").classes("tb-muted text-xs")
                self._positions_table(positions, cur)

            if self.live:
                self._holdings(d.get("broker_holdings") or {})

    def _live_access(self) -> bool:
        settings = state().settings
        keys = {w.broker for w in settings.watchlist if w.enabled}
        keys |= {p.get("broker") for p in (self.data or {}).get("positions") or [] if p.get("broker")}
        return any(tc.live_trade_block_reason(k) is None for k in keys)

    def _kpi(self, label: str, value: str, sub: str = "", cls: str = "", cur: str = "") -> None:
        with ui.card().classes("tb-card gap-1 min-w-0"):
            ui.label(f"{label} · {cur}" if cur else label).classes("tb-kpi-label truncate")
            ui.label(value).classes(f"tb-kpi-value truncate {cls}")
            ui.label(sub).classes("tb-muted text-xs truncate")

    def _positions_table(self, positions: list[dict], cur: str) -> None:
        def sym(r):
            with ui.column().classes("gap-0"):
                ui.label(r["symbol"]).classes("font-semibold")
                with ui.row().classes("items-center gap-1 no-wrap"):
                    tc.badge(tc.broker_name(r.get("broker")))
                    if r.get("external_closed"):
                        tc.badge("extern geschlossen", "warn")

        def entry(r):
            with ui.column().classes("gap-0 items-end"):
                ui.label(fmt_price(r.get("entry_price")))
                ui.label(tc.fmt_time(r.get("entry_time"))).classes("tb-muted text-xs")

        def pnl(r):
            v = r.get("unrealized_pnl")
            with ui.column().classes("gap-0 items-end"):
                ui.label(_signed(v)).classes(pnl_class(v) + " font-semibold")
                ui.label(fmt_pct(r.get("unrealized_pnl_pct"))).classes(pnl_class(v) + " text-xs")

        def levels(r):
            lv = r.get("stop_levels") or {}
            with ui.column().classes("gap-0 items-end text-xs"):
                sl, ts, tp = lv.get("stop_loss"), lv.get("trailing_stop"), lv.get("take_profit")
                ui.label(f"SL {fmt_price(sl)}").classes("tb-neg" if sl else "tb-muted")
                if ts:
                    ui.label(f"Trail {fmt_price(ts)}").classes("tb-neg")
                ui.label(f"TP {fmt_price(tp)}").classes("tb-pos" if tp else "tb-muted")

        def proceeds(r):
            v = r.get("estimated_net_pnl")
            with ui.column().classes("gap-0 items-end"):
                ui.label(fmt_num(r.get("estimated_net_proceeds"))).classes("font-semibold")
                ui.label(f"netto {_signed(v)} ({fmt_pct(r.get('estimated_net_pnl_pct'))})").classes(
                    pnl_class(v) + " text-xs")

        def cur_price(r):
            if r.get("current_price") is None:
                with ui.row().classes("items-center gap-1 no-wrap"):
                    ui.icon("help_outline", size="14px").classes("text-warning")
                    ui.label(fmt_price(r.get("entry_price"))).classes("tb-muted")
                    ui.tooltip("Kein aktueller Kurs verfügbar – Kaufkurs angezeigt")
                return None
            return fmt_price(r.get("current_price"))

        def action(r):
            if r.get("external_closed"):
                ui.button("Aus Journal entfernen", icon="delete_outline",
                          on_click=lambda _, p=r: self.open_forget(p)).props("flat dense size=sm color=warning")
            else:
                ui.button("Verkaufen", icon="sell", on_click=lambda _, p=r: self.open_sell(p)).props(
                    "unelevated dense size=sm color=negative" if self.live else "unelevated dense size=sm color=primary"
                ).classes("px-3")

        tc.table([
            tc.Col("Symbol", sym),
            tc.Col("Menge", lambda r: tc.fmt_qty(r.get("quantity")), "r"),
            tc.Col("Kaufkurs / -zeit", entry, "r"),
            tc.Col("Aktuell", cur_price, "r", "Aktueller Marktkurs"),
            tc.Col("Wert", lambda r: fmt_num(r.get("value")), "r"),
            tc.Col("G/V", pnl, "r", "Brutto, ohne Gebühren"),
            tc.Col("Stop / Ziel", levels, "r", "Aktuelle Stop-Loss-, Trailing-Stop- und Take-Profit-Kurse"),
            tc.Col("Verkauf ergäbe (netto)", proceeds, "r wrap",
                   "Geschätzter Erlös nach Gebühren und Slippage – und das daraus folgende Netto-Ergebnis"),
            tc.Col("", action, "r"),
        ], positions, empty="Keine offenen Bot-Positionen.",
            row_class=lambda r: "tb-row-warn" if r.get("external_closed") else "")

    def _holdings(self, holdings: dict) -> None:
        with card("gap-2"):
            with ui.row().classes("w-full items-center"):
                ui.label("Weitere Bestände beim Broker").classes("tb-section")
                ui.space()
                ui.label("Nur zur Information – der Bot fasst diese Bestände nicht an.").classes("tb-muted text-xs")
            if not holdings:
                ui.label("Keine Broker mit hinterlegten API-Zugangsdaten. Zugangsdaten kannst du unter "
                         "Einstellungen → Broker hinterlegen.").classes("tb-empty w-full")
                return
            for key, info in holdings.items():
                with ui.column().classes("w-full gap-1 pt-2"):
                    with ui.row().classes("items-center gap-2"):
                        ui.label(tc.broker_name(key)).classes("font-semibold")
                        if info.get("error"):
                            tc.badge("Fehler", "neg")
                    if info.get("error"):
                        ui.label(f"Kontostand nicht abrufbar: {info['error']}").classes("text-sm text-negative")
                        continue
                    tc.table([
                        tc.Col("Asset", lambda r: (r.get("asset"), "font-semibold")),
                        tc.Col("Gesamt beim Broker", lambda r: tc.fmt_qty(r.get("total")), "r"),
                        tc.Col("Davon Bot", lambda r: tc.fmt_qty(r.get("bot_quantity")), "r"),
                        tc.Col("Nicht vom Bot verwaltet", lambda r: (tc.fmt_qty(r.get("quantity")), "font-semibold"),
                               "r"),
                    ], info.get("holdings") or [], empty="Keine weiteren Bestände")

    # ---- actions ----------------------------------------------------------------------
    def _live_warning(self) -> None:
        if self.live:
            with ui.row().classes("items-center gap-2 no-wrap tb-banner tb-banner-error w-full"):
                ui.icon("warning", size="20px").classes("text-negative")
                ui.label("Dies ist eine echte Order mit echtem Geld.").classes("text-sm font-semibold text-negative")

    def open_sell(self, p: dict) -> None:
        cur = tc.quote_currency(p.get("symbol"))

        def body() -> None:
            with ui.element("div").classes("tb-kv w-full"):
                net = p.get("estimated_net_pnl")
                for k, v, cls in (
                    ("Symbol", p.get("symbol"), ""),
                    ("Broker", tc.broker_name(p.get("broker")), ""),
                    ("Menge", tc.fmt_qty(p.get("quantity")), ""),
                    ("Kaufkurs", fmt_price(p.get("entry_price")), ""),
                    ("Aktueller Kurs", fmt_price(p.get("current_price")), ""),
                    ("Geschätzter Erlös (netto)", fmt_money(p.get("estimated_net_proceeds"), cur), ""),
                    ("Geschätzter G/V (netto)",
                     f"{_signed(net, cur)} ({fmt_pct(p.get('estimated_net_pnl_pct'))})", pnl_class(net)),
                ):
                    ui.label(k).classes("k")
                    ui.label(v).classes(f"v {cls}")
            ui.label("Verkauf als Market-Order bzw. gemäß Strategie-Ordertyp; der tatsächliche Kurs kann "
                     "abweichen.").classes("tb-muted text-xs")
            self._live_warning()

        async def ok() -> None:
            await self._sell(p)

        with self.dialog_host:
            self.dialog_host.clear()
            dlg = tc.confirm_dialog(f"{p.get('symbol')} verkaufen?", body,
                                    "Echt verkaufen" if self.live else "Verkaufen",
                                    "negative" if self.live else "primary", ok, danger=self.live)
        dlg.open()

    async def _sell(self, p: dict) -> bool:
        try:
            trade = await self.mgr.manual_sell(self.mode, p["id"])
        except Exception as exc:  # noqa: BLE001
            ui.notify(_err(f"Verkauf von {p.get('symbol')} fehlgeschlagen", exc), color="negative",
                      position="bottom-right", multi_line=True)
            await self.refresh()
            return False
        tc.publish_sell(self.mode, trade)
        await self.refresh()
        return True

    def open_sell_all(self) -> None:
        positions = [p for p in (self.data or {}).get("positions") or [] if not p.get("external_closed")]
        if not positions:
            ui.notify("Keine Positionen zum Verkaufen", position="bottom-right")
            return
        cur = tc.common_currency(p.get("symbol") for p in positions)

        def body() -> None:
            total = sum(p.get("estimated_net_proceeds") or 0 for p in positions)
            net = sum(p.get("estimated_net_pnl") or 0 for p in positions)
            ui.label(f"{len(positions)} Positionen werden einzeln verkauft:").classes("text-sm")
            with ui.column().classes("gap-0 w-full max-h-[200px] overflow-y-auto"):
                for p in positions:
                    with ui.row().classes("w-full justify-between text-sm"):
                        ui.label(f"{p['symbol']} · {tc.fmt_qty(p.get('quantity'))}")
                        v = p.get("estimated_net_pnl")
                        ui.label(_signed(v)).classes(pnl_class(v))
            with ui.element("div").classes("tb-kv w-full"):
                ui.label("Geschätzter Erlös (netto)").classes("k")
                ui.label(fmt_money(total, cur)).classes("v")
                ui.label("Geschätzter G/V (netto)").classes("k")
                ui.label(_signed(net, cur)).classes(f"v {pnl_class(net)}")
            self._live_warning()

        async def ok() -> None:
            done, failed = 0, []
            for p in positions:
                try:
                    trade = await self.mgr.manual_sell(self.mode, p["id"])
                    tc.publish_sell(self.mode, trade)
                    done += 1
                except Exception as exc:  # noqa: BLE001
                    failed.append(f"{p.get('symbol')}: {exc}")
            if failed:
                ui.notify(f"{done} verkauft, {len(failed)} fehlgeschlagen – " + "; ".join(failed[:3]),
                          color="negative", position="bottom-right", multi_line=True, timeout=10000)
            await self.refresh()

        with self.dialog_host:
            self.dialog_host.clear()
            dlg = tc.confirm_dialog("Alle Positionen verkaufen?", body,
                                    "Alle echt verkaufen" if self.live else "Alle verkaufen", "negative", ok,
                                    danger=self.live)
        dlg.open()

    def open_forget(self, p: dict) -> None:
        def body() -> None:
            ui.label(f"Die Position {p.get('symbol')} ({tc.fmt_qty(p.get('quantity'))}) ist beim Broker nicht "
                     "mehr vorhanden. Entferne sie nur, wenn du sicher bist, dass sie außerhalb des Bots "
                     "verkauft wurde. Es wird keine Order gesendet.").classes("text-sm")

        async def ok() -> None:
            try:
                await self.mgr.forget_position(self.mode, p["id"])
                ui.notify(f"{p.get('symbol')}: Position aus dem Journal entfernt", color="warning",
                          position="bottom-right")
            except Exception as exc:  # noqa: BLE001
                ui.notify(f"Entfernen fehlgeschlagen: {exc}", color="negative", position="bottom-right")
            await self.refresh()

        with self.dialog_host:
            self.dialog_host.clear()
            dlg = tc.confirm_dialog("Position aus dem Journal entfernen?", body, "Entfernen", "warning", ok)
        dlg.open()

    def open_buy(self) -> None:
        st = state()
        settings = st.settings
        items = [w for w in settings.watchlist if w.enabled]
        options: dict[str, str] = {}
        reasons: dict[str, str] = {}
        for w in items:
            k = f"{w.broker}|{w.symbol}"
            reason = tc.live_trade_block_reason(w.broker) if self.live else None
            if reason:
                reasons[w.symbol] = reason
                continue
            options[k] = f"{w.symbol} · {tc.broker_name(w.broker)}"
        brokers: dict = {}
        info = {"price": None}

        self.dialog_host.clear()
        with self.dialog_host, ui.dialog() as dlg, ui.card().classes("tb-card w-[480px] max-w-full gap-4" +
                                                   (" tb-danger-zone" if self.live else "")):
            ui.label("Manuell kaufen" + (" (Echtgeld)" if self.live else "")).classes("text-lg font-semibold")
            ui.label("Der Kauf wird als Bot-Position geführt und anschließend von Stop-Loss/Take-Profit der "
                     "aktiven Strategie überwacht.").classes("tb-muted text-sm")
            if self.live and not settings.live_enabled:
                tc.banner("warn", "lock", "Live-Handel ist deaktiviert",
                          "Aktiviere ihn unter Einstellungen → LIVE-Sicherheit.")
            if not options:
                ui.label("Keine handelbaren Werte in der Watchlist." +
                         (" Für Live-Käufe werden Broker mit Handelsfunktion und API-Zugangsdaten benötigt."
                          if self.live else "")).classes("text-sm text-warning")
            sym_sel = ui.select(options, label="Wert", value=next(iter(options), None)).props(
                "outlined dense options-dense").classes("w-full")
            amount = ui.number("Betrag (Quote-Währung, inkl. Gebühren)", value=100.0 if not self.live else
                               min(50.0, float(settings.live_max_order_value)), min=0, step=10,
                               format="%.2f").props("outlined dense").classes("w-full")
            if self.live:
                ui.label(f"Maximaler Orderwert laut LIVE-Sicherheit: {fmt_num(settings.live_max_order_value)}").classes(
                    "tb-muted text-xs -mt-2")
            with ui.element("div").classes("tb-kv w-full"):
                ui.label("Aktueller Kurs").classes("k")
                price_lbl = ui.label("–").classes("v")
                ui.label("Geschätzte Menge").classes("k")
                qty_lbl = ui.label("–").classes("v")
            if reasons:
                ui.label("Nicht verfügbar: " + "; ".join(f"{s} ({r})" for s, r in list(reasons.items())[:4])).classes(
                    "tb-muted text-xs")
            self._live_warning()
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Abbrechen", on_click=dlg.close).props("flat color=grey-6")
                buy_btn = ui.button("Echt kaufen" if self.live else "Kaufen", icon="shopping_cart").props(
                    f"unelevated color={'negative' if self.live else 'positive'}")

        def update_qty() -> None:
            p, a = info["price"], amount.value
            if p and a:
                cur = tc.quote_currency((sym_sel.value or "|").split("|", 1)[1])
                # rough estimate: default fee + slippage of 0.15 %
                qty_lbl.set_text(f"ca. {tc.fmt_qty(a * (1 - 0.0015) / p)}" + (f"  (für {fmt_num(a)} {cur})"
                                                                              if cur else ""))
            else:
                qty_lbl.set_text("–")

        async def load_price() -> None:
            info["price"] = None
            price_lbl.set_text("wird geladen …")
            update_qty()
            if not sym_sel.value:
                price_lbl.set_text("–")
                return
            key, sym = sym_sel.value.split("|", 1)
            try:
                broker = brokers.get(key)
                if broker is None:
                    from tradebot.brokers import create_broker

                    creds = st.store.get_credentials(key) or None
                    broker = create_broker(key, creds, settings.broker_options.get(key))
                    brokers[key] = broker
                price = float(await run.io_bound(broker.fetch_price, sym))
                if sym_sel.value != f"{key}|{sym}":
                    return
                info["price"] = price
                price_lbl.set_text(fmt_price(price) + (f" {tc.quote_currency(sym)}" if tc.quote_currency(sym) else ""))
            except Exception as exc:  # noqa: BLE001
                price_lbl.set_text("nicht verfügbar")
                price_lbl.tooltip(str(exc))
            update_qty()

        async def do_buy() -> None:
            if not sym_sel.value:
                ui.notify("Bitte einen Wert auswählen", color="warning", position="bottom-right")
                return
            if not amount.value or amount.value <= 0:
                ui.notify("Bitte einen Betrag größer 0 angeben", color="warning", position="bottom-right")
                return
            key, sym = sym_sel.value.split("|", 1)
            buy_btn.props("loading")
            try:
                pos = await self.mgr.manual_buy(self.mode, key, sym, float(amount.value))
            except Exception as exc:  # noqa: BLE001
                ui.notify(_err(f"Kauf von {sym} fehlgeschlagen", exc), color="negative", position="bottom-right",
                          multi_line=True)
                return
            finally:
                buy_btn.props(remove="loading")
            tc.publish_buy(self.mode, pos)
            dlg.close()
            await self.refresh()

        sym_sel.on_value_change(lambda _: load_price())
        amount.on_value_change(lambda _: update_qty())
        buy_btn.on_click(do_buy)
        buy_btn.set_enabled(bool(options) and (not self.live or settings.live_enabled))
        dlg.open()
        background_tasks.create(load_price())


def _err(prefix: str, exc: Exception) -> str:
    msg = str(exc) or exc.__class__.__name__
    return msg if "fehlgeschlagen" in msg else f"{prefix}: {msg}"


def _signed(v: float | None, cur: str = "") -> str:
    if v is None:
        return "–"
    return ("+" if v > 0 else "") + fmt_money(v, cur)
