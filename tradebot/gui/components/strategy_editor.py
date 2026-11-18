"""Visual strategy editor: basics, rule-group builders (entry / exit) and trading settings.

The editor works on a plain dict (``Strategy.to_dict()``) and calls ``on_change`` after every
edit. ``strategy()`` converts it back; ``errors()`` validates it (never raises).
"""
from __future__ import annotations

import copy
import json
import math
from typing import Callable

from nicegui import ui

from tradebot.indicators import REGISTRY
from tradebot.strategy.models import OPERATORS, PRICE_FIELDS, Strategy
from tradebot.strategy.risk import min_true_count
from tradebot.gui.components.strategy_labels import (
    COST_FIELDS,
    LOGIC_OPTIONS,
    ORDER_TYPE_OPTIONS,
    PRICE_FIELD_LABELS,
    RISK_FIELDS,
    TIMEFRAME_OPTIONS,
    RiskField,
    fmt_de,
    indicator_options,
    output_label,
)

CSS = """
.tb-cond { border: 1px solid var(--tb-border); background: var(--tb-surface-2); border-radius: 12px;
           padding: 12px 12px 12px 10px; }
.tb-cond-idx { width: 24px; height: 24px; border-radius: 50%; font-size: .75rem; font-weight: 650;
               display: flex; align-items: center; justify-content: center; flex: none; margin-top: 8px;
               background: color-mix(in srgb, var(--tb-primary) 18%, transparent); color: var(--tb-primary); }
.tb-cond-exit .tb-cond-idx { background: color-mix(in srgb, var(--tb-neg) 18%, transparent); color: var(--tb-neg); }
.tb-opbox { flex: 1 1 290px; min-width: 250px; }
.tb-op-label { font-size: .7rem; font-weight: 600; letter-spacing: .05em; text-transform: uppercase;
               color: var(--tb-muted); }
.tb-kind .q-btn { padding: 0 10px; min-height: 28px; font-size: .78rem; }
.tb-kind { border: 1px solid var(--tb-border); border-radius: 9px; }
.tb-level-chip { cursor: pointer; font-size: .72rem; padding: 1px 9px; border-radius: 999px;
                 border: 1px solid var(--tb-border); color: var(--tb-muted); }
.tb-level-chip:hover { color: var(--tb-text); border-color: var(--tb-primary); }
.tb-risk-group-title { font-size: .95rem; font-weight: 650; }
.tb-field-off .q-field__native, .tb-field-off .q-field__label, .tb-field-off .q-field__suffix { opacity: .5; }
.tb-field-off .q-field__control:before { border-style: solid !important; }
.tb-kind .q-btn { white-space: nowrap; }
.tb-kind .q-btn__content { flex-wrap: nowrap; }
.tb-hint { font-size: .78rem; color: var(--tb-muted); }
.tb-group-head-icon { width: 34px; height: 34px; border-radius: 10px; display: flex; align-items: center;
                      justify-content: center; flex: none; }
.tb-help { color: var(--tb-muted); cursor: help; }
.tb-help:hover { color: var(--tb-primary); }
"""

IND_OPTIONS, IND_CATEGORIES = indicator_options()
_CAT_JS = json.dumps(IND_CATEGORIES, ensure_ascii=False)
IND_OPTION_SLOT = (
    '<q-item v-bind="props.itemProps"><q-item-section>'
    '<q-item-label>{{ props.opt.label.split(" · ")[0] }}</q-item-label></q-item-section>'
    '<q-item-section side><q-item-label caption>{{ ' + _CAT_JS + '[props.opt.value] }}</q-item-label>'
    '</q-item-section></q-item>'
)


def add_editor_css() -> None:
    ui.add_css(CSS)


def help_icon(text: str, size: str = "16px") -> ui.icon:
    ic = ui.icon("info_outline", size=size).classes("tb-help")
    ic.tooltip(text).classes("text-sm max-w-[320px]")
    return ic


def _num(v, typ: str = "float"):
    """Normalise a ui.number value (None stays None)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    if typ == "int":
        return int(round(f))
    return int(f) if f == int(f) and abs(f) < 1e12 and typ == "auto" else f


def default_operand(kind: str, partner: dict | None = None, side: str = "left") -> dict:
    if kind == "indicator":
        key = "rsi" if side == "left" else "ema"
        spec = REGISTRY[key]
        return {"kind": "indicator", "indicator": key, "params": spec.default_params(), "output": spec.outputs[0],
                "field": "close", "value": None, "offset": 0}
    if kind == "price":
        return {"kind": "price", "indicator": None, "params": {}, "output": None, "field": "close",
                "value": None, "offset": 0}
    value = 0.0
    if partner and partner.get("kind") == "indicator" and partner.get("indicator") in REGISTRY:
        levels = REGISTRY[partner["indicator"]].levels
        if levels:
            value = float(levels[0])
    return {"kind": "value", "indicator": None, "params": {}, "output": None, "field": "close",
            "value": value, "offset": 0}


def default_condition(group: str) -> dict:
    left = default_operand("indicator", side="left")
    right = default_operand("value", partner=left)
    right["value"] = 30.0 if group == "entry" else 70.0
    return {"left": left, "op": "<" if group == "entry" else ">", "right": right}


class StrategyEditor:
    """Builds the editor UI for one strategy (as dict) and reports every change."""

    def __init__(self, strategy: Strategy, on_change: Callable[[], None]) -> None:
        self.d = copy.deepcopy(strategy.to_dict())
        self.on_change = on_change
        self._group_refreshers: dict[str, Callable[[], None]] = {}
        self._min_hints: dict[str, Callable[[], None]] = {}

    # ---- model --------------------------------------------------------------------------
    def snapshot(self) -> str:
        return json.dumps(self.d, sort_keys=True, ensure_ascii=False, default=str)

    def strategy(self) -> Strategy:
        return Strategy.from_dict(copy.deepcopy(self.d))

    def changed(self) -> None:
        self.on_change()

    # ---- basics -------------------------------------------------------------------------
    def build_basics(self) -> None:
        d = self.d

        def set_name(e) -> None:
            d["name"] = (e.value or "").strip()
            self.changed()

        def set_desc(e) -> None:
            d["description"] = e.value or ""
            self.changed()

        def set_tf(e) -> None:
            d["timeframe"] = e.value
            self.changed()

        with ui.row().classes("w-full gap-4 items-start"):
            ui.input("Name", value=d.get("name", ""), on_change=set_name).props("outlined dense").classes(
                "flex-1 min-w-[220px]")
            sel = ui.select(TIMEFRAME_OPTIONS, label="Zeiteinheit (Kerze)", value=d.get("timeframe", "1h"),
                            on_change=set_tf).props("outlined dense options-dense").classes("w-48")
            sel.tooltip("Auf welcher Kerzengröße die Regeln ausgewertet werden. Signale entstehen immer "
                        "beim Schließen einer Kerze.")
        ui.textarea("Beschreibung (optional)", value=d.get("description", ""), on_change=set_desc).props(
            "outlined dense autogrow").classes("w-full")

    # ---- rule groups --------------------------------------------------------------------
    def build_group(self, key: str) -> None:
        g = self.d.setdefault(key, {"logic": "all", "conditions": [], "min_true_pct": 50.0})
        g.setdefault("min_true_pct", 50.0)
        entry = key == "entry"
        title = "Kaufregeln" if entry else "Verkaufsregeln"
        sub = ("Wann soll gekauft werden? (wenn keine Position offen ist)" if entry else
               "Wann soll eine offene Position verkauft werden? (zusätzlich zu Stop-Loss/Take-Profit)")
        color = "var(--tb-pos)" if entry else "var(--tb-neg)"

        with ui.row().classes("w-full items-center gap-3 no-wrap"):
            with ui.element("div").classes("tb-group-head-icon").style(
                    f"background: color-mix(in srgb, {color} 15%, transparent); color: {color}"):
                ui.icon("login" if entry else "logout", size="20px")
            with ui.column().classes("gap-0 flex-1 min-w-0"):
                ui.label(title).classes("text-base font-semibold")
                ui.label(sub).classes("tb-muted text-xs")

        with ui.row().classes("w-full items-center gap-3"):
            ui.label("Auslösen, wenn").classes("text-sm")

            def set_logic(e) -> None:
                g["logic"] = e.value
                min_row.set_visibility(e.value == "min")
                update_hint()
                self.changed()

            ui.select(LOGIC_OPTIONS, value=g.get("logic", "all"), on_change=set_logic).props(
                "outlined dense options-dense").classes("w-80")
            ui.label("erfüllt sind.").classes("text-sm")
            with ui.row().classes("items-center gap-2") as min_row:
                def set_pct(e) -> None:
                    v = _num(e.value)
                    g["min_true_pct"] = v
                    update_hint()
                    self.changed()

                ui.number("Mindest-Signalstärke", value=g.get("min_true_pct", 50.0), min=1, max=100, step=5,
                          on_change=set_pct).props('outlined dense suffix="%"').classes("w-48")
                hint = ui.label().classes("tb-hint")
                help_icon("Es müssen mindestens so viel Prozent der Bedingungen gleichzeitig erfüllt sein "
                          "(aufgerundet, mindestens eine). Beispiel: 60 % von 5 Bedingungen = 3.")
            min_row.set_visibility(g.get("logic") == "min")

        def update_hint() -> None:
            n = len(g["conditions"])
            pct = g.get("min_true_pct")
            if not n or pct is None or not (0 < pct <= 100):
                hint.set_text("")
                return
            k = min_true_count(n, pct)
            hint.set_text(f"= mindestens {k} von {n} Bedingung{'en' if n != 1 else ''}")

        self._min_hints[key] = update_hint
        update_hint()

        cond_box = ui.column().classes("w-full gap-3")

        def rebuild() -> None:
            cond_box.clear()
            with cond_box:
                if not g["conditions"]:
                    with ui.row().classes("w-full items-center gap-2 tb-cond justify-center py-5"):
                        ui.icon("rule", size="20px").classes("tb-muted")
                        ui.label("Noch keine Bedingung." + (
                            "" if entry else " Ohne Verkaufsregeln wird nur über Take-Profit, Stop-Loss, "
                                             "Trailing-Stop oder Haltedauer verkauft.")).classes("tb-muted text-sm")
                for i, cond in enumerate(g["conditions"]):
                    self._condition(key, i, cond, rebuild)
            update_hint()

        self._group_refreshers[key] = rebuild
        rebuild()

        def add() -> None:
            g["conditions"].append(default_condition(key))
            rebuild()
            self.changed()

        ui.button("Bedingung hinzufügen", icon="add", on_click=add).props("flat dense no-caps color=primary")

    def _condition(self, group: str, idx: int, cond: dict, rebuild_group: Callable[[], None]) -> None:
        g = self.d[group]
        box = ui.element("div").classes(f"tb-cond w-full {'tb-cond-exit' if group == 'exit' else ''}")

        def render() -> None:
            box.clear()
            with box, ui.row().classes("w-full items-start gap-3 no-wrap"):
                ui.label(str(idx + 1)).classes("tb-cond-idx")
                with ui.column().classes("flex-1 gap-2 min-w-0"):
                    with ui.row().classes("w-full items-start gap-2"):
                        self._operand(cond, "left", render)

                    with ui.row().classes("w-full items-start gap-2"):
                        def set_op(e) -> None:
                            cond["op"] = e.value
                            self.changed()

                        ui.select(OPERATORS, value=cond.get("op", ">"), on_change=set_op).props(
                            "outlined dense options-dense").classes("w-[196px] tb-op-select")
                        self._operand(cond, "right", render)

                def remove() -> None:
                    g["conditions"].pop(idx)
                    rebuild_group()
                    self.changed()

                ui.button(icon="delete_outline", on_click=remove).props(
                    "flat round dense color=grey-6").classes("mt-1").tooltip("Bedingung löschen")

        render()

    def _operand(self, cond: dict, side: str, rerender: Callable[[], None]) -> None:
        """One operand as an inline, wrapping row: kind toggle + its inputs."""
        op = cond[side]
        partner = cond["left" if side == "right" else "right"]

        def set_kind(e) -> None:
            if e.value == op.get("kind"):
                return
            cond[side] = default_operand(e.value, partner if side == "right" else None, side)
            rerender()
            self.changed()

        ui.toggle({"indicator": "Indikator", "price": "Kurs", "value": "Wert"}, value=op.get("kind"),
                  on_change=set_kind).props("dense no-caps unelevated toggle-color=primary").classes(
            "tb-kind self-center")

        kind = op.get("kind")
        if kind == "indicator":
            self._indicator_operand(cond, side, rerender)
        elif kind == "price":
            def set_field(e) -> None:
                op["field"] = e.value
                self.changed()

            ui.select({f: PRICE_FIELD_LABELS[f] for f in PRICE_FIELDS}, value=op.get("field", "close"),
                      on_change=set_field).props("outlined dense options-dense").classes("w-[180px]")
            self._offset_input(op)
        else:
            def set_value(e) -> None:
                op["value"] = _num(e.value)
                self.changed()

            num = ui.number("Wert", value=op.get("value"), on_change=set_value).props(
                "outlined dense").classes("w-[130px]")
            levels = []
            if partner.get("kind") == "indicator" and partner.get("indicator") in REGISTRY:
                levels = list(REGISTRY[partner["indicator"]].levels)
            if levels:
                with ui.row().classes("items-center gap-1 self-center"):
                    ui.label("Typisch:").classes("tb-hint mr-1")
                    for lv in levels:
                        ui.label(fmt_de(lv)).classes("tb-level-chip").on(
                            "click", lambda _e, v=lv: num.set_value(float(v))).tooltip("Wert übernehmen")

    def _offset_input(self, op: dict) -> None:
        def set_offset(e) -> None:
            v = _num(e.value, "int")
            op["offset"] = v if v is not None else 0
            self.changed()

        n = ui.number("vor N Kerzen", value=op.get("offset", 0) or 0, min=0, max=500, step=1, format="%.0f",
                      on_change=set_offset).props("outlined dense").classes("w-[96px]")
        n.tooltip("0 = aktuelle (zuletzt geschlossene) Kerze, 1 = die Kerze davor usw.")

    def _indicator_operand(self, cond: dict, side: str, rerender: Callable[[], None]) -> None:
        op = cond[side]
        key = op.get("indicator")
        spec = REGISTRY.get(key) if key else None

        def set_indicator(e) -> None:
            if not e.value or e.value == op.get("indicator"):
                return
            s = REGISTRY[e.value]
            op["indicator"] = e.value
            op["params"] = s.default_params()
            op["output"] = s.outputs[0]
            rerender()
            self.changed()

        with ui.row().classes("items-center gap-1 no-wrap"):
            sel = ui.select(IND_OPTIONS, value=key if key in IND_OPTIONS else None, with_input=True,
                            on_change=set_indicator).props(
                'outlined dense options-dense placeholder="Indikator suchen …" popup-content-class="tb-ind-menu"'
            ).classes("w-[250px]")
            sel.add_slot("option", IND_OPTION_SLOT)
            if spec is not None:
                tip = spec.name + (" – " + spec.description if spec.description else "")
                if spec.levels:
                    tip += f" Typische Marken: {', '.join(fmt_de(x) for x in spec.levels)}."
                help_icon(tip, "18px")
        if spec is None:
            return
        params = op.setdefault("params", {})
        for p in spec.params:
            if p.name not in params:
                params[p.name] = p.default

            def set_param(e, p=p) -> None:
                params[p.name] = _num(e.value, p.type)
                self.changed()

            kw = {"format": "%.0f"} if p.type == "int" else {}
            ui.number(p.label or p.name, value=params.get(p.name), min=p.min, max=p.max, step=p.step,
                      on_change=set_param, **kw).props("outlined dense").classes(
                "w-[92px]" if p.type == "int" else "w-[104px]").tooltip(
                f"{p.label or p.name}: {fmt_de(p.min)} – {fmt_de(p.max)} (Standard {fmt_de(p.default)})")
        if len(spec.outputs) > 1:
            def set_output(e) -> None:
                op["output"] = e.value
                self.changed()

            ui.select({o: output_label(spec.key, o) for o in spec.outputs}, label="Linie",
                      value=op.get("output") or spec.outputs[0], on_change=set_output).props(
                "outlined dense options-dense").classes("w-[136px]")
        self._offset_input(op)

    def refresh_hints(self) -> None:
        for fn in self._min_hints.values():
            fn()

    # ---- trading settings ---------------------------------------------------------------
    def build_risk(self) -> None:
        r = self.d.setdefault("risk", {})
        with ui.row().classes("w-full items-center gap-3 no-wrap"):
            with ui.element("div").classes("tb-group-head-icon").style(
                    "background: color-mix(in srgb, var(--tb-primary) 15%, transparent); color: var(--tb-primary)"):
                ui.icon("tune", size="20px")
            with ui.column().classes("gap-0"):
                ui.label("Trading-Einstellungen").classes("text-base font-semibold")
                ui.label("Positionsgröße, Schutz-Stops und Kosten – gelten identisch für Backtest, "
                         "Test- und Live-Trading.").classes("tb-muted text-xs")

        with ui.tabs().props("dense no-caps align=left inline-label").classes("w-full") as tabs:
            t_buy = ui.tab("buy", label="Kaufen", icon="shopping_cart")
            t_sell = ui.tab("sell", label="Verkaufen", icon="sell")
            t_cost = ui.tab("cost", label="Kosten", icon="receipt")
        ui.separator().classes("-mt-3")
        with ui.tab_panels(tabs, value=t_buy).props("animated=false").classes("w-full bg-transparent"):
            with ui.tab_panel(t_buy).classes("p-0 pt-2"):
                self._buy_panel(r)
            with ui.tab_panel(t_sell).classes("p-0 pt-2"):
                self._sell_panel(r)
            with ui.tab_panel(t_cost).classes("p-0 pt-2"):
                with ui.element("div").classes("grid w-full gap-4 grid-cols-1 md:grid-cols-2 xl:grid-cols-3"):
                    for f in COST_FIELDS:
                        self._risk_input(r, f)
                ui.label("Kosten werden nur simuliert (Backtest, Test-Trading). Im Live-Trading berechnet "
                         "die Börse die tatsächlichen Gebühren.").classes("tb-hint mt-3")

    def _grid(self):
        return ui.element("div").classes("grid w-full gap-x-4 gap-y-4 grid-cols-1 md:grid-cols-2 xl:grid-cols-3")

    def _sub(self, text: str) -> None:
        ui.label(text).classes("tb-section mt-4 mb-1")

    def _buy_panel(self, r: dict) -> None:
        self._sub("Positionsgröße")
        with self._grid():
            with ui.column().classes("gap-1"):
                def set_mode(e) -> None:
                    r["position_size_mode"] = e.value
                    pct_box.set_visibility(e.value != "fixed")
                    fixed_box.set_visibility(e.value == "fixed")
                    self.changed()

                ui.toggle({"pct": "% vom Kapital", "fixed": "Fester Betrag"}, value=r.get("position_size_mode", "pct"),
                          on_change=set_mode).props("no-caps unelevated toggle-color=primary").classes("tb-kind")
                ui.label("Wie viel pro Kauf eingesetzt wird").classes("tb-hint")
            with ui.element("div") as pct_box:
                self._risk_input(r, RISK_FIELDS["position_size_pct"])
            with ui.element("div") as fixed_box:
                self._risk_input(r, RISK_FIELDS["position_size_fixed"])
            pct_box.set_visibility(r.get("position_size_mode", "pct") != "fixed")
            fixed_box.set_visibility(r.get("position_size_mode", "pct") == "fixed")
            for k in ("min_order_value", "max_order_value", "max_exposure_pct", "cash_reserve"):
                self._risk_input(r, RISK_FIELDS[k])

        self._sub("Positionen & Nachkäufe")
        with self._grid():
            for k in ("max_open_positions", "max_positions_per_symbol", "add_position_min_drop_pct"):
                self._risk_input(r, RISK_FIELDS[k])

        self._sub("Pausen")
        with self._grid():
            for k in ("cooldown_seconds", "max_loss_streak", "loss_streak_pause_hours"):
                self._risk_input(r, RISK_FIELDS[k])

        self._sub("Order & Daten")
        with self._grid():
            self._order_type(r, "buy")
            self._risk_input(r, RISK_FIELDS["klines_to_load"])

    def _sell_panel(self, r: dict) -> None:
        self._sub("Schutz-Stops & Gewinnziel")
        with self._grid():
            for k in ("take_profit_pct", "stop_loss_pct", "trailing_stop_pct", "trailing_activation_pct",
                      "breakeven_trigger_pct", "max_holding_bars"):
                self._risk_input(r, RISK_FIELDS[k])
        self._sub("Signal-Verkäufe")
        with self._grid():
            for k in ("min_profit_pct", "min_profit_abs"):
                self._risk_input(r, RISK_FIELDS[k])
        self._sub("Order")
        with self._grid():
            self._order_type(r, "sell")

    def _order_type(self, r: dict, side: str) -> None:
        key = f"{side}_order_type"
        f = RISK_FIELDS[f"{side}_price_multiple"]
        with ui.column().classes("gap-1"):
            with ui.row().classes("items-center gap-2"):
                ui.label("Ordertyp " + ("Kauf" if side == "buy" else "Verkauf")).classes("text-sm")
                help_icon("Market: sofort zum aktuellen Kurs (Slippage möglich). Limit: Order mit Preisgrenze "
                          "= Kurs × Faktor – schützt vor schlechten Ausführungskursen.")

            def set_type(e) -> None:
                r[key] = e.value
                mult_box.set_visibility(e.value == "limit")
                self.changed()

            ui.toggle(ORDER_TYPE_OPTIONS, value=r.get(key, "market"), on_change=set_type).props(
                "no-caps unelevated toggle-color=primary").classes("tb-kind")
        with ui.element("div") as mult_box:
            self._risk_input(r, f)
        mult_box.set_visibility(r.get(key, "market") == "limit")

    def _risk_input(self, r: dict, f: RiskField) -> None:
        val = r.get(f.key)
        kw = {"format": "%.0f"} if f.type == "int" else {}
        if f.key.endswith("price_multiple"):
            kw = {"format": "%.4f"}
        state = {"on": (val is not None) if f.optional else True,
                 "last": val if val is not None else f.on_default}

        def on_value(e) -> None:
            if not state["on"]:
                return
            v = _num(e.value, f.type)
            r[f.key] = v
            if v is not None:
                state["last"] = v
            self.changed()

        num = ui.number(f.label, value=val if state["on"] else None, min=f.min, max=f.max, step=f.step,
                        on_change=on_value, **kw).props(f'outlined dense stack-label suffix="{f.unit}"').classes("w-full")
        with num.add_slot("append"):
            help_icon(f.help)
        if not f.optional:
            return

        def apply_state() -> None:
            if state["on"]:
                num.props(remove="readonly placeholder")
                num.classes(remove="tb-field-off")
            else:
                num.props('readonly placeholder="aus"')
                num.classes("tb-field-off")

        def toggle(e) -> None:
            state["on"] = bool(e.value)
            if state["on"]:
                v = state["last"] if state["last"] is not None else f.on_default
                r[f.key] = v
                apply_state()
                num.set_value(v)
            else:
                r[f.key] = None
                num.set_value(None)
                apply_state()
            self.changed()

        with num.add_slot("prepend"):
            ui.switch(value=state["on"], on_change=toggle).props("dense size=xs").tooltip(
                "Ein-/ausschalten")
        apply_state()
