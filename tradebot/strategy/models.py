"""Strategy definition (pure data, JSON-serialisable).

A strategy is long-only:
  * ``entry`` rule group -> buy when it evaluates True on a closed candle and no position is open
  * ``exit`` rule group (optional) -> sell when True
  * ``risk`` -> position sizing, take-profit, stop-loss, trailing stop, max holding time

Example (JSON):
{
  "name": "RSI Dip im Aufwärtstrend",
  "timeframe": "4h",
  "entry": {"logic": "all", "conditions": [
     {"left": {"kind": "indicator", "indicator": "rsi", "params": {"period": 14}, "output": "rsi"},
      "op": "<", "right": {"kind": "value", "value": 30}},
     {"left": {"kind": "price", "field": "close"}, "op": ">",
      "right": {"kind": "indicator", "indicator": "ema", "params": {"period": 200}, "output": "ema"}}
  ]},
  "exit": {"logic": "any", "conditions": [
     {"left": {"kind": "indicator", "indicator": "macd", "params": {}, "output": "macd"},
      "op": "crosses_below",
      "right": {"kind": "indicator", "indicator": "macd", "params": {}, "output": "signal"}}
  ]},
  "risk": {"position_size_pct": 10, "take_profit_pct": 6, "stop_loss_pct": 3}
}
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

OPERATORS = {
    ">": "größer als",
    "<": "kleiner als",
    ">=": "größer gleich",
    "<=": "kleiner gleich",
    "crosses_above": "kreuzt nach oben",
    "crosses_below": "kreuzt nach unten",
}
PRICE_FIELDS = ["open", "high", "low", "close", "volume"]


@dataclass
class Operand:
    kind: str  # "indicator" | "price" | "value"
    indicator: str | None = None  # registry key when kind == "indicator"
    params: dict = field(default_factory=dict)
    output: str | None = None  # indicator output column; default = first output
    field: str = "close"  # when kind == "price"
    value: float | None = None  # when kind == "value"
    offset: int = 0  # look back N bars (0 = current closed bar)

    @classmethod
    def from_dict(cls, d: dict) -> "Operand":
        return cls(**d)


@dataclass
class Condition:
    left: Operand
    op: str  # key of OPERATORS
    right: Operand

    @classmethod
    def from_dict(cls, d: dict) -> "Condition":
        return cls(Operand.from_dict(d["left"]), d["op"], Operand.from_dict(d["right"]))


LOGIC = {"all": "alle Bedingungen", "any": "mindestens eine Bedingung",
         "min": "Mindestanteil der Bedingungen"}


@dataclass
class RuleGroup:
    logic: str = "all"  # "all" (AND) | "any" (OR) | "min" (at least min_true_pct % of conditions)
    conditions: list[Condition] = field(default_factory=list)
    min_true_pct: float = 50.0  # only used when logic == "min" ("Mindest-Signalstärke")

    @classmethod
    def from_dict(cls, d: dict | None) -> "RuleGroup":
        if not d:
            return cls()
        return cls(d.get("logic", "all"), [Condition.from_dict(c) for c in d.get("conditions", [])],
                   float(d.get("min_true_pct", 50.0)))


@dataclass
class RiskSettings:
    """Trading settings ("Trading-Einstellungen") of a strategy.

    Used identically by backtest, TEST (paper) and LIVE mode via ``tradebot.strategy.risk``.
    Money values are in the quote currency of the instrument (USDT, USD, EUR...).
    """

    # ---- Buying -------------------------------------------------------------------
    position_size_mode: str = "pct"  # "pct" (% of equity) | "fixed" (fixed amount)
    position_size_pct: float = 10.0  # % of current equity per new position
    position_size_fixed: float = 100.0  # amount per new position when mode == "fixed"
    min_order_value: float = 10.0  # skip buys below this amount
    max_order_value: float | None = None  # cap per buy (None = no cap)
    max_open_positions: int = 5  # total across all symbols of one bot
    max_positions_per_symbol: int = 1  # >1 allows adding to a position (DCA)
    add_position_min_drop_pct: float = 3.0  # extra buy only if price is x% below last entry
    cooldown_seconds: int = 0  # wait after a buy before buying the same symbol again
    max_loss_streak: int = 0  # pause a symbol after N losing trades in a row (0 = off)
    loss_streak_pause_hours: float = 24.0  # pause length (0 = until bot restart / manual reset)
    max_exposure_pct: float = 100.0  # max % of equity invested at the same time
    cash_reserve: float = 0.0  # amount that is never used for buying
    klines_to_load: int = 0  # candles loaded per evaluation (0 = auto from indicators)
    buy_order_type: str = "market"  # "market" | "limit"
    buy_price_multiple: float = 1.001  # limit price = price * multiple (>= 1: fills reliably)

    # ---- Selling ------------------------------------------------------------------
    take_profit_pct: float | None = 5.0
    stop_loss_pct: float | None = 3.0
    trailing_stop_pct: float | None = None  # % below highest price since entry
    trailing_activation_pct: float | None = None  # trailing stop active only after +x %
    breakeven_trigger_pct: float | None = None  # move stop to entry price after +x %
    min_profit_pct: float = 0.0  # signal exits only if net profit >= x % (stops always apply)
    min_profit_abs: float = 0.0  # signal exits only if net profit >= amount
    max_holding_bars: int | None = None
    sell_order_type: str = "market"  # "market" | "limit"
    sell_price_multiple: float = 0.999  # limit price = price * multiple (<= 1: fills reliably)

    # ---- Costs (simulation: backtest & TEST mode) ----------------------------------
    fee_pct: float = 0.1  # per side
    slippage_pct: float = 0.05  # per side

    @classmethod
    def from_dict(cls, d: dict | None) -> "RiskSettings":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


@dataclass
class Strategy:
    name: str = "Neue Strategie"
    timeframe: str = "1h"
    entry: RuleGroup = field(default_factory=RuleGroup)
    exit: RuleGroup = field(default_factory=RuleGroup)
    risk: RiskSettings = field(default_factory=RiskSettings)
    description: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "Strategy":
        return cls(
            name=d.get("name", "Neue Strategie"),
            timeframe=d.get("timeframe", "1h"),
            entry=RuleGroup.from_dict(d.get("entry")),
            exit=RuleGroup.from_dict(d.get("exit")),
            risk=RiskSettings.from_dict(d.get("risk")),
            description=d.get("description", ""),
        )

    @classmethod
    def from_json(cls, s: str) -> "Strategy":
        return cls.from_dict(json.loads(s))
