"""German labels, help texts and small helpers shared by the strategy editor and backtest pages."""
from __future__ import annotations

from dataclasses import dataclass

from tradebot.core.models import TIMEFRAMES
from tradebot.indicators import CATEGORIES, REGISTRY

TIMEFRAME_LABELS = {
    "1m": "1 Minute", "5m": "5 Minuten", "15m": "15 Minuten", "30m": "30 Minuten",
    "1h": "1 Stunde", "4h": "4 Stunden", "1d": "1 Tag", "1w": "1 Woche",
}
TIMEFRAME_OPTIONS = {k: TIMEFRAME_LABELS.get(k, k) for k in TIMEFRAMES}

CATEGORY_LABELS = {"Trend": "Trend", "Momentum": "Momentum", "Volatility": "Volatilität",
                   "Volume": "Volumen", "Other": "Sonstige"}

PRICE_FIELD_LABELS = {"close": "Schlusskurs", "open": "Eröffnungskurs", "high": "Hoch", "low": "Tief",
                      "volume": "Volumen"}

OUTPUT_LABELS = {
    "macd": "MACD-Linie", "signal": "Signallinie", "hist": "Histogramm", "upper": "Oberes Band",
    "middle": "Mittellinie", "lower": "Unteres Band", "percent_b": "%B (Lage im Band)",
    "bandwidth": "Bandbreite", "k": "%K", "d": "%D", "adx": "ADX", "plus_di": "+DI",
    "minus_di": "−DI", "up": "Aroon Up", "down": "Aroon Down", "osc": "Oszillator", "plus": "VI+",
    "minus": "VI−", "tenkan": "Tenkan-sen", "kijun": "Kijun-sen", "span_a": "Senkou Span A",
    "span_b": "Senkou Span B", "supertrend": "Supertrend-Linie", "direction": "Richtung (+1 / −1)",
    "tsi": "TSI", "trix": "TRIX", "ppo": "PPO-Linie",
}

EXIT_REASON_LABELS = {
    "signal": "Verkaufssignal", "take_profit": "Take-Profit", "stop_loss": "Stop-Loss",
    "trailing_stop": "Trailing-Stop", "max_bars": "Max. Haltedauer", "manual": "Manuell",
    "end_of_data": "Ende des Zeitraums",
}

LOGIC_OPTIONS = {
    "all": "alle Bedingungen (UND)",
    "any": "mindestens eine Bedingung (ODER)",
    "min": "Mindestanteil der Bedingungen",
}


def output_label(key: str, output: str) -> str:
    return OUTPUT_LABELS.get(output, output.upper() if output == key else output)


def indicator_options() -> tuple[dict[str, str], list[str]]:
    """Indicator select options sorted by category, plus category caption per option index."""
    order = {c: i for i, c in enumerate(CATEGORIES)}
    specs = sorted(REGISTRY.values(), key=lambda s: (order.get(s.category, 99), s.name.lower()))
    options = {s.key: f"{s.key.upper()} – {s.name}" for s in specs}
    return options, [CATEGORY_LABELS.get(s.category, s.category) for s in specs]


# ------------------------------------------------------------------------------------------
# trading settings ("Trading-Einstellungen")
# ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RiskField:
    key: str
    label: str
    unit: str
    help: str
    type: str = "float"  # float | int
    optional: bool = False  # None-able -> switch
    on_default: float | None = None  # value when switched on
    step: float = 1
    min: float | None = 0
    max: float | None = None
    short: str = ""  # short label for optimizer lists


BUY_FIELDS = [
    RiskField("position_size_pct", "Positionsgröße", "%",
              "Anteil des aktuellen Kapitals (Cash + Wert offener Positionen), der pro neuem Kauf "
              "eingesetzt wird.", step=1, min=0.1, max=100),
    RiskField("position_size_fixed", "Kaufbetrag je Position", "Betrag",
              "Fester Betrag in der Kurswährung (z. B. USDT, USD, EUR), der pro neuem Kauf eingesetzt wird.",
              step=10, min=0),
    RiskField("min_order_value", "Mindest-Kaufbetrag", "Betrag",
              "Käufe unter diesem Betrag werden übersprungen – viele Börsen haben eine Mindest-Ordergröße.",
              step=1),
    RiskField("max_order_value", "Max. Kaufbetrag", "Betrag",
              "Obergrenze pro Kauf, egal wie groß das Kapital ist. Aus = keine Begrenzung.",
              optional=True, on_default=1000, step=10),
    RiskField("max_open_positions", "Max. offene Positionen", "Pos.",
              "Höchstzahl gleichzeitig offener Positionen über alle Symbole dieses Bots.", "int", min=1),
    RiskField("max_positions_per_symbol", "Max. Positionen je Symbol", "Pos.",
              "Mehr als 1 erlaubt Nachkäufe in dasselbe Symbol (Durchschnittspreis senken, „DCA“). "
              "Jeder Nachkauf ist eine eigene Position.", "int", min=1),
    RiskField("add_position_min_drop_pct", "Nachkauf ab Kursrückgang", "%",
              "Ein Nachkauf erfolgt nur, wenn der Kurs mindestens so viel Prozent unter dem letzten "
              "Kaufkurs liegt. Nur wirksam bei mehr als einer Position je Symbol.", step=0.5),
    RiskField("cooldown_seconds", "Wartezeit nach Kauf", "Sek.",
              "Nach einem Kauf wird dasselbe Symbol so lange nicht erneut gekauft. 0 = keine Wartezeit. "
              "Beispiel: 3600 = 1 Stunde, 86400 = 1 Tag.", "int", step=60),
    RiskField("max_loss_streak", "Pause nach Verlust-Serie", "Trades",
              "Pausiert ein Symbol nach so vielen Verlust-Trades in Folge. 0 = aus.", "int"),
    RiskField("loss_streak_pause_hours", "Pausendauer", "Std.",
              "Dauer der Pause nach einer Verlust-Serie. 0 = bis zum Neustart des Bots bzw. zur "
              "manuellen Freigabe.", step=1),
    RiskField("max_exposure_pct", "Max. investierter Anteil", "%",
              "Höchstens dieser Anteil des Kapitals darf gleichzeitig investiert sein; der Rest bleibt "
              "als Cash.", step=5, min=1, max=100),
    RiskField("cash_reserve", "Cash-Reserve", "Betrag",
              "Dieser Betrag wird nie für Käufe verwendet.", step=10),
    RiskField("klines_to_load", "Kerzen laden", "Kerzen",
              "Wie viele Kerzen pro Auswertung (Live/Test) geladen werden. 0 = automatisch passend zu "
              "den Indikatoren (empfohlen).", "int", step=50),
    RiskField("buy_price_multiple", "Limit-Preisfaktor (Kauf)", "×",
              "Limit-Preis = aktueller Kurs × Faktor. Werte ≥ 1 (z. B. 1,001) sorgen dafür, dass die "
              "Order zuverlässig ausgeführt wird. Erlaubt: 1,0 – 1,05.", step=0.001, min=1, max=1.05),
]

SELL_FIELDS = [
    RiskField("take_profit_pct", "Take-Profit", "%",
              "Gewinnmitnahme: verkauft automatisch, sobald der Kurs so viel Prozent über dem Kaufkurs liegt.",
              optional=True, on_default=5, step=0.5, short="Take-Profit"),
    RiskField("stop_loss_pct", "Stop-Loss", "%",
              "Verlustbegrenzung: verkauft automatisch, sobald der Kurs so viel Prozent unter den Kaufkurs fällt.",
              optional=True, on_default=3, step=0.5, max=99.9, short="Stop-Loss"),
    RiskField("trailing_stop_pct", "Trailing-Stop", "%",
              "Nachgezogener Stop: verkauft, wenn der Kurs so viel Prozent unter das höchste Hoch seit "
              "dem Kauf fällt. Sichert Gewinne in Trends.", optional=True, on_default=3, step=0.5,
              max=99.9, short="Trailing-Stop"),
    RiskField("trailing_activation_pct", "Trailing aktiv ab Gewinn", "%",
              "Der Trailing-Stop greift erst, nachdem die Position mindestens so viel Prozent im Plus "
              "war. Aus = sofort aktiv.", optional=True, on_default=2, step=0.5, short="Trailing-Aktivierung"),
    RiskField("breakeven_trigger_pct", "Break-Even ab Gewinn", "%",
              "Sobald die Position so viel Prozent im Plus war, wird der Stop-Loss auf den Kaufkurs "
              "angehoben – aus einem Gewinner wird kein Verlierer mehr (abgesehen von Gebühren und "
              "Kurslücken).", optional=True, on_default=2, step=0.5, short="Break-Even"),
    RiskField("min_profit_pct", "Mindestgewinn für Signal-Verkauf", "%",
              "Verkaufssignale werden nur ausgeführt, wenn der Nettogewinn (nach Gebühren) mindestens so "
              "hoch ist. Stop-Loss, Take-Profit und Trailing-Stop gelten immer.", step=0.1),
    RiskField("min_profit_abs", "Mindestgewinn (Betrag)", "Betrag",
              "Wie oben, aber als absoluter Betrag je Position. 0 = aus.", step=1),
    RiskField("max_holding_bars", "Max. Haltedauer", "Kerzen",
              "Verkauft spätestens nach so vielen Kerzen, unabhängig von den Verkaufsregeln.",
              "int", optional=True, on_default=48, min=1, short="Max. Haltedauer"),
    RiskField("sell_price_multiple", "Limit-Preisfaktor (Verkauf)", "×",
              "Limit-Preis = aktueller Kurs × Faktor. Werte ≤ 1 (z. B. 0,999) sorgen dafür, dass die "
              "Order zuverlässig ausgeführt wird. Erlaubt: 0,95 – 1,0.", step=0.001, min=0.95, max=1),
]

COST_FIELDS = [
    RiskField("fee_pct", "Gebühr je Seite", "%",
              "Handelsgebühr je Kauf und je Verkauf in Prozent des Orderwerts (Simulation: Backtest und "
              "Test-Trading). Binance z. B. 0,1 %, Kraken 0,25 %.", step=0.01, max=99),
    RiskField("slippage_pct", "Slippage je Seite", "%",
              "Angenommene Abweichung zwischen erwartetem und tatsächlichem Ausführungskurs je Order "
              "(Simulation). Bei liquiden Märkten 0,02–0,1 %.", step=0.01, max=99),
]

RISK_FIELDS = {f.key: f for f in BUY_FIELDS + SELL_FIELDS + COST_FIELDS}

ORDER_TYPE_OPTIONS = {"market": "Market", "limit": "Limit"}


def fmt_de(v, digits: int | None = None) -> str:
    """Compact German number (decimal comma), e.g. 0.5 -> '0,5'."""
    if v is None:
        return "–"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    s = f"{f:.{digits}f}" if digits is not None else f"{f:g}"
    return s.replace(".", ",")


def quote_currency(symbol: str, broker: str = "") -> str:
    """Best-effort quote currency of a symbol (for display only)."""
    s = (symbol or "").upper()
    if "/" in s:
        return s.split("/")[-1].split(":")[0]
    if "-" in s and broker == "yahoo":
        return s.split("-")[-1]
    if s.endswith((".DE", ".F", ".PA", ".AS", ".MI", ".MC", ".VI", ".BR")):
        return "EUR"
    if s.endswith(".L"):
        return "GBP"
    if s.endswith(".SW"):
        return "CHF"
    if broker == "kraken":
        return "EUR"
    return "USD"


CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£"}


def currency_symbol(code: str) -> str:
    return CURRENCY_SYMBOLS.get(code, code)
