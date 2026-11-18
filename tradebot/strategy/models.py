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


@dataclass
class RuleGroup:
    logic: str = "all"  # "all" (AND) | "any" (OR)
    conditions: list[Condition] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict | None) -> "RuleGroup":
        if not d:
            return cls()
        return cls(d.get("logic", "all"), [Condition.from_dict(c) for c in d.get("conditions", [])])


@dataclass
class RiskSettings:
    position_size_pct: float = 10.0  # % of current equity per new position
    take_profit_pct: float | None = 5.0
    stop_loss_pct: float | None = 3.0
    trailing_stop_pct: float | None = None  # % below highest price since entry
    max_open_positions: int = 5  # across all symbols run by one bot
    max_holding_bars: int | None = None
    fee_pct: float = 0.1  # per side, used for backtest/paper simulation
    slippage_pct: float = 0.05  # per side, used for backtest/paper simulation

    @classmethod
    def from_dict(cls, d: dict | None) -> "RiskSettings":
        return cls(**(d or {}))


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
