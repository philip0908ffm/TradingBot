"""Core domain models shared by every module (brokers, strategy, backtest, engine, GUI).

All timestamps are timezone-aware UTC ``datetime`` objects.
OHLCV data is always a ``pandas.DataFrame`` with a UTC ``DatetimeIndex`` and the
lower-case columns ``open, high, low, close, volume`` (floats), sorted ascending.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]

# Timeframes supported throughout the app (ccxt style notation).
TIMEFRAMES: dict[str, int] = {  # timeframe -> seconds
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
    "1w": 604800,
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class AssetClass(str, Enum):
    CRYPTO = "crypto"
    STOCK = "stock"
    ETF = "etf"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, Enum):
    NEW = "new"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


class TradingMode(str, Enum):
    TEST = "test"  # paper trading: real prices, virtual money, nothing sent to broker
    LIVE = "live"  # real money


class ExitReason(str, Enum):
    SIGNAL = "signal"
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    TRAILING_STOP = "trailing_stop"
    MAX_BARS = "max_bars"
    MANUAL = "manual"
    END_OF_DATA = "end_of_data"


@dataclass
class Order:
    symbol: str
    side: Side
    quantity: float
    order_type: OrderType = OrderType.MARKET
    price: float | None = None  # limit price (request) / avg fill price (result)
    status: OrderStatus = OrderStatus.NEW
    filled_quantity: float = 0.0
    fee: float = 0.0  # in quote currency
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)
    raw: dict | None = None  # broker raw response (not persisted to GUI)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("raw", None)
        d["side"] = self.side.value
        d["order_type"] = self.order_type.value
        d["status"] = self.status.value
        d["created_at"] = self.created_at.isoformat()
        return d


@dataclass
class Position:
    """An open long position (the bot is long-only)."""

    symbol: str
    quantity: float
    entry_price: float
    entry_time: datetime = field(default_factory=utcnow)
    highest_price: float = 0.0  # for trailing stop, updated while open
    bars_held: int = 0
    strategy: str | None = None
    id: str = field(default_factory=new_id)

    def __post_init__(self) -> None:
        if not self.highest_price:
            self.highest_price = self.entry_price

    def market_value(self, price: float) -> float:
        return self.quantity * price

    def unrealized_pnl(self, price: float) -> float:
        return (price - self.entry_price) * self.quantity

    def unrealized_pnl_pct(self, price: float) -> float:
        return (price / self.entry_price - 1.0) * 100.0 if self.entry_price else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["entry_time"] = self.entry_time.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        d = dict(d)
        d["entry_time"] = datetime.fromisoformat(d["entry_time"])
        return cls(**d)


@dataclass
class ClosedTrade:
    symbol: str
    quantity: float
    entry_price: float
    exit_price: float
    entry_time: datetime
    exit_time: datetime
    fees: float  # total fees (entry + exit) in quote currency
    exit_reason: ExitReason
    strategy: str | None = None
    id: str = field(default_factory=new_id)

    @property
    def pnl(self) -> float:
        return (self.exit_price - self.entry_price) * self.quantity - self.fees

    @property
    def pnl_pct(self) -> float:
        cost = self.entry_price * self.quantity
        return self.pnl / cost * 100.0 if cost else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["entry_time"] = self.entry_time.isoformat()
        d["exit_time"] = self.exit_time.isoformat()
        d["exit_reason"] = self.exit_reason.value
        d["pnl"] = self.pnl
        d["pnl_pct"] = self.pnl_pct
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ClosedTrade":
        d = {k: v for k, v in d.items() if k not in ("pnl", "pnl_pct")}
        d["entry_time"] = datetime.fromisoformat(d["entry_time"])
        d["exit_time"] = datetime.fromisoformat(d["exit_time"])
        d["exit_reason"] = ExitReason(d["exit_reason"])
        return cls(**d)


@dataclass
class WatchItem:
    """One instrument the user wants to observe/trade."""

    symbol: str  # broker-native symbol, e.g. "BTC/USDT" (ccxt) or "AAPL"
    broker: str  # key in tradebot.brokers.BROKERS, e.g. "binance", "alpaca", "yahoo"
    asset_class: AssetClass = AssetClass.CRYPTO
    enabled: bool = True

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "broker": self.broker,
                "asset_class": self.asset_class.value, "enabled": self.enabled}

    @classmethod
    def from_dict(cls, d: dict) -> "WatchItem":
        return cls(symbol=d["symbol"], broker=d["broker"],
                   asset_class=AssetClass(d.get("asset_class", "crypto")),
                   enabled=d.get("enabled", True))
