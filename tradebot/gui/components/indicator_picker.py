"""Categorised indicator picker dialog + label helpers (used by the Charts page)."""
from __future__ import annotations

from typing import Callable

from nicegui import ui

from tradebot.indicators import CATEGORIES, REGISTRY, get

CATEGORY_LABELS = {"Trend": "Trend", "Momentum": "Momentum", "Volatility": "Volatilität",
                   "Volume": "Volumen", "Other": "Sonstige"}

GERMAN_NAMES = {
    "rsi": "Relative Stärke (RSI)", "stochrsi": "Stochastischer RSI", "stoch": "Stochastik-Oszillator",
    "willr": "Williams %R", "cci": "Commodity Channel Index", "roc": "Änderungsrate (ROC)",
    "mom": "Momentum", "ao": "Awesome Oscillator", "uo": "Ultimate Oscillator",
    "tsi": "True Strength Index", "cmo": "Chande-Momentum-Oszillator", "mfi": "Geldfluss-Index (MFI)",
    "change": "Kursänderung %", "highest": "Höchstes Hoch", "lowest": "Tiefstes Tief", "zscore": "Z-Score",
    "ma_dist": "Abstand zum gleitenden Durchschnitt %", "sma": "Einfacher gleitender Durchschnitt (SMA)",
    "ema": "Exponentieller gleitender Durchschnitt (EMA)", "wma": "Gewichteter gleitender Durchschnitt (WMA)",
    "dema": "Doppelter EMA (DEMA)", "tema": "Dreifacher EMA (TEMA)", "hma": "Hull Moving Average (HMA)",
    "kama": "Kaufman Adaptive MA (KAMA)", "vwma": "Volumengewichteter Durchschnitt (VWMA)",
    "zlema": "Zero-Lag EMA", "macd": "MACD", "ppo": "Prozentualer Preis-Oszillator (PPO)",
    "adx": "Trendstärke (ADX)", "aroon": "Aroon", "vortex": "Vortex-Indikator",
    "ichimoku": "Ichimoku Kinko Hyo", "psar": "Parabolic SAR", "supertrend": "Supertrend", "trix": "TRIX",
    "linreg_slope": "Steigung der linearen Regression", "bbands": "Bollinger-Bänder",
    "keltner": "Keltner-Kanäle", "donchian": "Donchian-Kanäle", "atr": "Average True Range (ATR)",
    "natr": "Normalisierte ATR", "stddev": "Standardabweichung", "hv": "Historische Volatilität",
    "obv": "On-Balance-Volumen (OBV)", "cmf": "Chaikin Money Flow", "ad": "Akkumulation/Distribution",
    "vwap": "Gleitender VWAP", "volume_ratio": "Volumen-Verhältnis", "efi": "Force Index",
    "eom": "Ease of Movement",
}


def german_name(key: str) -> str:
    return GERMAN_NAMES.get(key) or get(key).name


def indicator_label(key: str, params: dict | None) -> str:
    """Short label such as ``EMA(50)`` or ``MACD(12, 26, 9)``."""
    spec = get(key)
    p = {**spec.default_params(), **(params or {})}
    args = ", ".join(f"{p[x.name]:g}" for x in spec.params)
    return f"{key.upper()}({args})" if args else key.upper()


def sanitize(indicators: list) -> list[dict]:
    """Drop unknown/broken entries from a stored indicator list."""
    out = []
    for it in indicators or []:
        try:
            key = it["key"]
            spec = get(key)
            params = {k: v for k, v in (it.get("params") or {}).items() if k in spec.default_params()}
            out.append({"key": key, "params": params})
        except (KeyError, TypeError, AttributeError):
            continue
    return out


class IndicatorPicker:
    """Dialog: search + category filter, list of indicators, parameter form, "Hinzufügen"."""

    def __init__(self, on_add: Callable[[str, dict], None]) -> None:
        self.on_add = on_add
        self.category = "all"
        self.selected: str | None = None
        self.inputs: dict[str, ui.number] = {}
        with ui.dialog() as self.dialog, \
                ui.card().classes("tb-card w-[900px] max-w-[96vw] gap-3 p-0").style("padding:0"):
            with ui.row().classes("w-full items-center justify-between px-5 pt-4"):
                with ui.column().classes("gap-0"):
                    ui.label("Indikator hinzufügen").classes("text-lg font-semibold")
                    ui.label("Indikator auswählen, Parameter anpassen und zum Chart hinzufügen.").classes(
                        "tb-muted text-xs")
                ui.button(icon="close", on_click=self.dialog.close).props("flat round dense color=grey-6")
            with ui.row().classes("w-full items-center gap-3 px-5 no-wrap"):
                self.search = ui.input(placeholder="Suchen, z. B. RSI, Bollinger, Trend …").props(
                    "outlined dense clearable autofocus").classes("flex-1")
                self.search.on_value_change(lambda _: self.render_list.refresh())
                opts = {"all": "Alle", **{c: CATEGORY_LABELS.get(c, c) for c in CATEGORIES}}
                ui.toggle(opts, value="all", on_change=self._set_category).props(
                    "dense no-caps unelevated rounded toggle-color=primary padding='4px 10px'")
            with ui.row().classes("w-full gap-0 no-wrap border-t").style(
                    "border-color: var(--tb-border); height: 440px"):
                with ui.scroll_area().classes("w-1/2 h-full").style("border-right:1px solid var(--tb-border)"):
                    self.render_list()
                with ui.column().classes("w-1/2 h-full p-5 gap-3"):
                    self.render_detail()

    def open(self) -> None:
        self.selected = None
        self.search.set_value("")
        self.render_list.refresh()
        self.render_detail.refresh()
        self.dialog.open()

    def _set_category(self, e) -> None:
        self.category = e.value
        self.render_list.refresh()

    def _matches(self, key: str) -> bool:
        spec = REGISTRY[key]
        if self.category != "all" and spec.category != self.category:
            return False
        q = (self.search.value or "").strip().lower()
        if not q:
            return True
        hay = " ".join([key, spec.name, german_name(key), spec.description, spec.category,
                        CATEGORY_LABELS.get(spec.category, "")]).lower()
        return all(part in hay for part in q.split())

    def _select(self, key: str) -> None:
        self.selected = key
        self.render_list.refresh()
        self.render_detail.refresh()

    @ui.refreshable_method
    def render_list(self) -> None:
        keys = [k for k in REGISTRY if self._matches(k)]
        order = {c: i for i, c in enumerate(CATEGORIES)}
        keys.sort(key=lambda k: (order.get(REGISTRY[k].category, 99), german_name(k).lower()))
        with ui.column().classes("w-full gap-0 py-1"):
            if not keys:
                ui.label("Kein Indikator gefunden.").classes("tb-muted text-sm p-5")
            for k in keys:
                spec = REGISTRY[k]
                active = k == self.selected
                with ui.element("div").classes(
                        "w-full px-5 py-2 cursor-pointer tb-pick" + (" tb-pick-active" if active else "")
                ).on("click", lambda _, k=k: self._select(k)):
                    with ui.row().classes("w-full items-center justify-between no-wrap gap-2"):
                        ui.label(german_name(k)).classes("text-sm font-medium truncate")
                        ui.label(k.upper()).classes("tb-badge shrink-0")
                    ui.label(spec.description).classes("tb-muted text-xs line-clamp-2")

    @ui.refreshable_method
    def render_detail(self) -> None:
        self.inputs = {}
        if not self.selected:
            with ui.column().classes("w-full h-full items-center justify-center gap-2"):
                ui.icon("query_stats", size="40px").classes("tb-muted opacity-60")
                ui.label("Wähle links einen Indikator aus.").classes("tb-muted text-sm")
            return
        spec = get(self.selected)
        ui.label(german_name(spec.key)).classes("text-base font-semibold")
        with ui.row().classes("gap-1"):
            ui.label(CATEGORY_LABELS.get(spec.category, spec.category)).classes("tb-badge")
            ui.label("im Kurs-Chart" if spec.overlay else "eigenes Fenster").classes("tb-badge")
            if spec.name != german_name(spec.key):
                ui.label(spec.name).classes("tb-badge")
        ui.label(spec.description).classes("tb-muted text-sm")
        if spec.params:
            ui.label("Parameter").classes("tb-section mt-2")
            with ui.element("div").classes("grid grid-cols-2 gap-3 w-full"):
                for p in spec.params:
                    num = ui.number(p.label or p.name, value=p.default, min=p.min, max=p.max, step=p.step,
                                    format="%d" if p.type == "int" else None).props("outlined dense")
                    num.tooltip(f"{p.min:g} – {p.max:g}")
                    self.inputs[p.name] = num
        else:
            ui.label("Dieser Indikator hat keine Parameter.").classes("tb-muted text-sm mt-2")
        ui.space()
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Abbrechen", on_click=self.dialog.close).props("flat no-caps color=grey-6")
            ui.button("Hinzufügen", icon="add", on_click=self._add).props("unelevated no-caps color=primary")

    def _add(self) -> None:
        if not self.selected:
            return
        spec = get(self.selected)
        params = {}
        for p in spec.params:
            v = self.inputs[p.name].value
            if v is None:
                ui.notify(f"Bitte einen Wert für „{p.label or p.name}“ eingeben", type="warning")
                return
            v = min(max(float(v), float(p.min)), float(p.max))
            params[p.name] = int(round(v)) if p.type == "int" else float(v)
        self.on_add(spec.key, params)
        self.dialog.close()


PICKER_CSS = """
.tb-pick { border-left: 3px solid transparent; }
.tb-pick:hover { background: var(--tb-surface-2); }
.tb-pick-active { background: color-mix(in srgb, var(--tb-primary) 12%, transparent) !important;
                  border-left-color: var(--tb-primary); }
"""
