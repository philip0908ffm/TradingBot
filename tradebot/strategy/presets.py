"""Well-known example strategies (starting points for users, not recommendations)."""
from __future__ import annotations

import copy

from tradebot.strategy.models import Condition, Operand, RiskSettings, RuleGroup, Strategy


def _ind(key: str, output: str | None = None, offset: int = 0, **params) -> Operand:
    return Operand(kind="indicator", indicator=key, params=params, output=output or key,
                   offset=offset)


def _price(field: str = "close", offset: int = 0) -> Operand:
    return Operand(kind="price", field=field, offset=offset)


def _val(v: float) -> Operand:
    return Operand(kind="value", value=v)


def _c(left: Operand, op: str, right: Operand) -> Condition:
    return Condition(left, op, right)


PRESETS: list[Strategy] = [
    Strategy(
        name="RSI-Rücksetzer im Aufwärtstrend",
        timeframe="4h",
        description="Kauft überverkaufte Rücksetzer (RSI < 30), aber nur oberhalb des EMA 200 "
                    "(Aufwärtstrend). Verkauft, wenn der RSI wieder über 55 steigt.",
        entry=RuleGroup("all", [
            _c(_ind("rsi", period=14), "<", _val(30)),
            _c(_price("close"), ">", _ind("ema", period=200)),
        ]),
        exit=RuleGroup("any", [_c(_ind("rsi", period=14), ">", _val(55))]),
        risk=RiskSettings(position_size_pct=20, take_profit_pct=8, stop_loss_pct=4,
                          min_profit_pct=0.5, max_loss_streak=3, loss_streak_pause_hours=24),
    ),
    Strategy(
        name="MACD-Kreuzung",
        timeframe="1d",
        description="Kauft, wenn die MACD-Linie die Signallinie nach oben kreuzt, und verkauft "
                    "bei der Kreuzung nach unten.",
        entry=RuleGroup("all", [
            _c(_ind("macd", "macd", fast=12, slow=26, signal=9), "crosses_above",
               _ind("macd", "signal", fast=12, slow=26, signal=9)),
        ]),
        exit=RuleGroup("any", [
            _c(_ind("macd", "macd", fast=12, slow=26, signal=9), "crosses_below",
               _ind("macd", "signal", fast=12, slow=26, signal=9)),
        ]),
        risk=RiskSettings(position_size_pct=25, take_profit_pct=None, stop_loss_pct=8),
    ),
    Strategy(
        name="Golden Cross (SMA 50/200)",
        timeframe="1d",
        description="Klassisches Trendfolge-Signal: Kauf, wenn der SMA 50 den SMA 200 nach oben "
                    "kreuzt (Golden Cross), Verkauf beim Death Cross.",
        entry=RuleGroup("all", [
            _c(_ind("sma", period=50), "crosses_above", _ind("sma", period=200)),
        ]),
        exit=RuleGroup("any", [
            _c(_ind("sma", period=50), "crosses_below", _ind("sma", period=200)),
        ]),
        risk=RiskSettings(position_size_pct=50, take_profit_pct=None, stop_loss_pct=15),
    ),
    Strategy(
        name="Bollinger-Abpraller mit Nachkauf",
        timeframe="1h",
        description="Kauft, wenn der Kurs vom unteren Bollinger-Band wieder nach oben ins Band "
                    "kreuzt; kauft bis zu zweimal nach, wenn der Kurs mindestens 5 % unter dem "
                    "letzten Kauf liegt (DCA). Verkauft am Mittelband.",
        entry=RuleGroup("all", [
            _c(_price("close"), "crosses_above", _ind("bbands", "lower", period=20, std=2.0)),
        ]),
        exit=RuleGroup("any", [
            _c(_price("close"), ">=", _ind("bbands", "middle", period=20, std=2.0)),
        ]),
        risk=RiskSettings(position_size_pct=10, take_profit_pct=6, stop_loss_pct=12,
                          max_positions_per_symbol=3, add_position_min_drop_pct=5,
                          cooldown_seconds=4 * 3600),
    ),
    Strategy(
        name="Supertrend-Trendfolge",
        timeframe="4h",
        description="Kauft beim Wechsel des Supertrends auf Aufwärtstrend (Richtung +1) und "
                    "verkauft beim Wechsel auf Abwärtstrend.",
        entry=RuleGroup("all", [
            _c(_ind("supertrend", "direction", period=10, multiplier=3.0), "crosses_above",
               _val(0)),
        ]),
        exit=RuleGroup("any", [
            _c(_ind("supertrend", "direction", period=10, multiplier=3.0), "crosses_below",
               _val(0)),
        ]),
        risk=RiskSettings(position_size_pct=25, take_profit_pct=None, stop_loss_pct=10,
                          trailing_stop_pct=6, trailing_activation_pct=5),
    ),
    Strategy(
        name="Stochastik überverkauft",
        timeframe="1h",
        description="Kauft, wenn %K im überverkauften Bereich (< 20) die %D-Linie nach oben "
                    "kreuzt; verkauft im überkauften Bereich (%K > 80).",
        entry=RuleGroup("all", [
            _c(_ind("stoch", "k", period=14, smooth_k=3, smooth_d=3), "crosses_above",
               _ind("stoch", "d", period=14, smooth_k=3, smooth_d=3)),
            _c(_ind("stoch", "k", period=14, smooth_k=3, smooth_d=3), "<", _val(20)),
        ]),
        exit=RuleGroup("any", [
            _c(_ind("stoch", "k", period=14, smooth_k=3, smooth_d=3), ">", _val(80)),
        ]),
        risk=RiskSettings(position_size_pct=15, take_profit_pct=4, stop_loss_pct=2.5),
    ),
    Strategy(
        name="Donchian-Ausbruch (Turtle)",
        timeframe="1d",
        description="Kauft, wenn der Schlusskurs das 20-Tage-Hoch der Vortage übersteigt; "
                    "verkauft, wenn er unter das 10-Tage-Tief der Vortage fällt.",
        entry=RuleGroup("all", [
            _c(_price("close"), ">", _ind("donchian", "upper", offset=1, period=20)),
        ]),
        exit=RuleGroup("any", [
            _c(_price("close"), "<", _ind("donchian", "lower", offset=1, period=10)),
        ]),
        risk=RiskSettings(position_size_pct=25, take_profit_pct=None, stop_loss_pct=None,
                          trailing_stop_pct=12),
    ),
    Strategy(
        name="ADX-Trend mit EMA",
        timeframe="4h",
        description="Kauft bei starkem Trend (ADX > 25) mit Käuferdominanz (+DI > −DI), wenn der "
                    "Kurs über dem EMA 50 liegt; verkauft, wenn der Kurs unter den EMA 50 fällt.",
        entry=RuleGroup("all", [
            _c(_ind("adx", "adx", period=14), ">", _val(25)),
            _c(_ind("adx", "plus_di", period=14), ">", _ind("adx", "minus_di", period=14)),
            _c(_price("close"), ">", _ind("ema", period=50)),
        ]),
        exit=RuleGroup("any", [
            _c(_price("close"), "crosses_below", _ind("ema", period=50)),
        ]),
        risk=RiskSettings(position_size_pct=25, take_profit_pct=None, stop_loss_pct=6,
                          trailing_stop_pct=8, breakeven_trigger_pct=3),
    ),
]


def get_preset(name: str) -> Strategy:
    """Deep copy of the preset with ``name`` (so callers can modify it freely)."""
    for s in PRESETS:
        if s.name == name:
            return copy.deepcopy(s)
    raise KeyError(f"Unbekannte Vorlage {name!r}")
