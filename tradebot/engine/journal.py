"""Persistent per-mode bot state (JSON, atomic writes).

Holds only what the BOT itself did: its own open positions (several per symbol possible),
its closed trades, per-symbol rule state (cooldown, loss streak, pause), the last processed
candle per symbol, equity history and the kill-switch baseline.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

from tradebot.core.models import ClosedTrade, Position
from tradebot.core.settings import atomic_write_text
from tradebot.strategy.risk import SymbolState

log = logging.getLogger(__name__)

MAX_EQUITY_POINTS = 10_000
MAX_CLOSED_TRADES = 5_000
EQUITY_MIN_INTERVAL_S = 60.0
JOURNAL_VERSION = 2


class TradeJournal:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.positions: dict[str, Position] = {}  # position id -> bot position
        self.position_brokers: dict[str, str] = {}  # position id -> broker key
        self.entry_fees: dict[str, float] = {}  # position id -> entry fee (quote)
        self.external_closed: set[str] = set()  # position ids no longer held at the broker
        self.dust: set[str] = set()  # position ids too small to sell (below lot/notional)
        self.position_envs: dict[str, str] = {}  # position id -> broker environment
        # client order id -> pending/uncertain order (write-ahead before place_order)
        self.pending_orders: dict[str, dict] = {}
        self.symbol_states: dict[str, SymbolState] = {}  # symbol -> buy-rule state
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
        old_brokers = dict(d.get("position_brokers") or {})
        positions: dict[str, Position] = {}
        brokers: dict[str, str] = {}
        for key, raw in (d.get("positions") or {}).items():
            try:
                pos = Position.from_dict(raw)
            except (KeyError, TypeError, ValueError):
                log.warning("Ungültige Position im Journal ignoriert: %s", key)
                continue
            positions[pos.id] = pos
            # v1 journals were keyed by symbol -> migrate to position id
            broker = old_brokers.get(pos.id) or old_brokers.get(key)
            if broker:
                brokers[pos.id] = broker
        self.positions = positions
        self.position_brokers = brokers
        self.entry_fees = {k: float(v) for k, v in (d.get("entry_fees") or {}).items()}
        self.external_closed = {i for i in d.get("external_closed") or [] if i in positions}
        self.dust = {i for i in d.get("dust") or [] if i in positions}
        self.position_envs = {k: str(v) for k, v in (d.get("position_envs") or {}).items() if k in positions}
        self.pending_orders = {k: dict(v) for k, v in (d.get("pending_orders") or {}).items()
                               if isinstance(v, dict)}
        states = {}
        for sym, raw in (d.get("symbol_states") or {}).items():
            try:
                states[sym] = SymbolState.from_dict(raw)
            except (TypeError, ValueError):
                log.warning("Ungültiger Symbolzustand im Journal ignoriert: %s", sym)
        self.symbol_states = states
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
            "version": JOURNAL_VERSION,
            "positions": {i: p.to_dict() for i, p in self.positions.items()},
            "position_brokers": self.position_brokers,
            "entry_fees": self.entry_fees,
            "external_closed": sorted(self.external_closed),
            "dust": sorted(self.dust),
            "position_envs": self.position_envs,
            "pending_orders": self.pending_orders,
            "symbol_states": {s: st.to_dict() for s, st in self.symbol_states.items()},
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
        self.external_closed.clear()
        self.dust.clear()
        self.position_envs.clear()
        self.pending_orders.clear()
        self.symbol_states.clear()
        self.closed_trades.clear()
        self.last_candle.clear()
        self.equity_history.clear()
        self.daily_start = None
        self.start_equity = None
        self.kill_switch_date = None
        self.last_prices.clear()
        self.save()

    # ---- queries ----------------------------------------------------------------------
    def positions_for(self, symbol: str, broker: str | None = None) -> list[Position]:
        """Open positions of a symbol (optionally of one broker), oldest first."""
        out = [p for i, p in self.positions.items()
               if p.symbol == symbol and (broker is None or self.position_brokers.get(i) == broker)]
        return sorted(out, key=lambda p: p.entry_time)

    def state(self, symbol: str) -> SymbolState:
        st = self.symbol_states.get(symbol)
        if st is None:
            st = self.symbol_states[symbol] = SymbolState()
        return st

    # ---- mutations --------------------------------------------------------------------
    def open_position(self, pos: Position, broker: str, entry_fee: float, env: str | None = None) -> None:
        self.positions[pos.id] = pos
        self.position_brokers[pos.id] = broker
        self.entry_fees[pos.id] = float(entry_fee)
        if env:
            self.position_envs[pos.id] = env

    def remove_position(self, pos_id: str) -> Position | None:
        pos = self.positions.pop(pos_id, None)
        self.position_brokers.pop(pos_id, None)
        self.entry_fees.pop(pos_id, None)
        self.external_closed.discard(pos_id)
        self.dust.discard(pos_id)
        self.position_envs.pop(pos_id, None)
        return pos

    def close_position(self, pos_id: str, trade: ClosedTrade) -> None:
        self.remove_position(pos_id)
        self.add_trade(trade)

    def add_trade(self, trade: ClosedTrade) -> None:
        self.closed_trades.append(trade)
        if len(self.closed_trades) > MAX_CLOSED_TRADES:
            del self.closed_trades[: len(self.closed_trades) - MAX_CLOSED_TRADES]

    def record_equity(self, when: datetime, equity: float) -> None:
        """Append an equity point; at most one point per minute (the newest value wins)."""
        if self.equity_history:
            try:
                last = datetime.fromisoformat(self.equity_history[-1][0])
                if (when - last).total_seconds() < EQUITY_MIN_INTERVAL_S:
                    self.equity_history[-1] = (self.equity_history[-1][0], float(equity))
                    return
            except ValueError:
                pass
        self.equity_history.append((when.isoformat(), float(equity)))
        if len(self.equity_history) > MAX_EQUITY_POINTS:
            del self.equity_history[: len(self.equity_history) - MAX_EQUITY_POINTS]

    def ensure_daily_start(self, today: date, equity: float, pnl_mark: float | None = None) -> float:
        """Return the start equity of ``today`` (UTC), initialising it on a new day.

        ``pnl_mark`` = the bot's own P&L mark (realized + unrealized) at the start of the day;
        the kill switch compares against it.
        """
        key = today.isoformat()
        if not self.daily_start or self.daily_start.get("date") != key:
            self.daily_start = {"date": key, "equity": float(equity)}
        if pnl_mark is not None and self.daily_start.get("pnl_mark") is None:
            self.daily_start["pnl_mark"] = float(pnl_mark)
        return float(self.daily_start["equity"])

    def pending_for_position(self, pos_id: str) -> dict | None:
        return next((o for o in self.pending_orders.values() if o.get("position_id") == pos_id), None)

    def pending_buy_for(self, symbol: str, broker: str) -> dict | None:
        return next((o for o in self.pending_orders.values()
                     if o.get("side") == "buy" and o.get("symbol") == symbol
                     and o.get("broker") == broker), None)
