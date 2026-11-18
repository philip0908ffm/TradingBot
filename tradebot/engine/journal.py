"""Persistent per-mode bot state (JSON, atomic writes).

Holds only what the BOT itself did: its own open positions, its closed trades,
the last processed candle per symbol, equity history and the kill-switch baseline.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

from tradebot.core.models import ClosedTrade, Position
from tradebot.core.settings import atomic_write_text

log = logging.getLogger(__name__)

MAX_EQUITY_POINTS = 10_000
MAX_CLOSED_TRADES = 5_000
EQUITY_MIN_INTERVAL_S = 60.0


class TradeJournal:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.positions: dict[str, Position] = {}  # symbol -> bot position
        self.position_brokers: dict[str, str] = {}  # symbol -> broker key
        self.entry_fees: dict[str, float] = {}  # position id -> entry fee (quote)
        self.closed_trades: list[ClosedTrade] = []
        self.last_candle: dict[str, str] = {}  # "broker:symbol" -> iso ts of last processed candle
        self.equity_history: list[tuple[str, float]] = []
        self.daily_start: dict | None = None  # {"date": "YYYY-MM-DD", "equity": float}
        self.start_equity: float | None = None
        self.kill_switch_date: str | None = None
        self.last_prices: dict[str, float] = {}
        self.strategy_name: str | None = None
        self.load()

    # ---- persistence ------------------------------------------------------------------
    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Journal %s konnte nicht gelesen werden: %s", self.path.name, exc)
            return
        positions = {}
        for sym, p in (d.get("positions") or {}).items():
            try:
                positions[sym] = Position.from_dict(p)
            except (KeyError, TypeError, ValueError):
                log.warning("Ungültige Position im Journal ignoriert: %s", sym)
        self.positions = positions
        self.position_brokers = dict(d.get("position_brokers") or {})
        self.entry_fees = {k: float(v) for k, v in (d.get("entry_fees") or {}).items()}
        trades = []
        for t in d.get("closed_trades") or []:
            try:
                trades.append(ClosedTrade.from_dict(t))
            except (KeyError, TypeError, ValueError):
                log.warning("Ungültiger Trade im Journal ignoriert")
        self.closed_trades = trades
        self.last_candle = dict(d.get("last_candle") or {})
        self.equity_history = [(str(t), float(e)) for t, e in d.get("equity_history") or []]
        self.daily_start = d.get("daily_start")
        self.start_equity = d.get("start_equity")
        self.kill_switch_date = d.get("kill_switch_date")
        self.last_prices = {k: float(v) for k, v in (d.get("last_prices") or {}).items()}
        self.strategy_name = d.get("strategy_name")

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "positions": {s: p.to_dict() for s, p in self.positions.items()},
            "position_brokers": self.position_brokers,
            "entry_fees": self.entry_fees,
            "closed_trades": [t.to_dict() for t in self.closed_trades],
            "last_candle": self.last_candle,
            "equity_history": [list(x) for x in self.equity_history],
            "daily_start": self.daily_start,
            "start_equity": self.start_equity,
            "kill_switch_date": self.kill_switch_date,
            "last_prices": self.last_prices,
            "strategy_name": self.strategy_name,
        }

    def save(self) -> None:
        atomic_write_text(self.path, json.dumps(self.to_dict(), indent=1, ensure_ascii=False))

    def reset(self) -> None:
        self.positions.clear()
        self.position_brokers.clear()
        self.entry_fees.clear()
        self.closed_trades.clear()
        self.last_candle.clear()
        self.equity_history.clear()
        self.daily_start = None
        self.start_equity = None
        self.kill_switch_date = None
        self.last_prices.clear()
        self.save()

    # ---- mutations --------------------------------------------------------------------
    def open_position(self, pos: Position, broker: str, entry_fee: float) -> None:
        self.positions[pos.symbol] = pos
        self.position_brokers[pos.symbol] = broker
        self.entry_fees[pos.id] = float(entry_fee)

    def close_position(self, symbol: str, trade: ClosedTrade) -> None:
        pos = self.positions.pop(symbol, None)
        self.position_brokers.pop(symbol, None)
        if pos is not None:
            self.entry_fees.pop(pos.id, None)
        self.closed_trades.append(trade)
        if len(self.closed_trades) > MAX_CLOSED_TRADES:
            del self.closed_trades[: len(self.closed_trades) - MAX_CLOSED_TRADES]

    def record_equity(self, when: datetime, equity: float) -> None:
        """Append an equity point; at most one point per minute (the newest replaces)."""
        iso = when.isoformat()
        if self.equity_history:
            try:
                last = datetime.fromisoformat(self.equity_history[-1][0])
                if (when - last).total_seconds() < EQUITY_MIN_INTERVAL_S:
                    # keep the minute's first timestamp, update value
                    self.equity_history[-1] = (self.equity_history[-1][0], float(equity))
                    return
            except ValueError:
                pass
        self.equity_history.append((iso, float(equity)))
        if len(self.equity_history) > MAX_EQUITY_POINTS:
            del self.equity_history[: len(self.equity_history) - MAX_EQUITY_POINTS]

    def ensure_daily_start(self, today: date, equity: float) -> float:
        """Return the start equity of ``today`` (UTC), initialising it on a new day."""
        key = today.isoformat()
        if not self.daily_start or self.daily_start.get("date") != key:
            self.daily_start = {"date": key, "equity": float(equity)}
        return float(self.daily_start["equity"])
