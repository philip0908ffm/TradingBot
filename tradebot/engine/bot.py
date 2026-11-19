"""Live / paper trading engine.

One ``TradingBot`` runs one strategy over a watchlist in one ``TradingMode``:

* TEST: real prices, virtual money (``PaperBroker`` on a shared ``PaperAccount``).
* LIVE: real orders, guarded by several safety switches (master switch, per-order cap,
  daily loss kill switch, max open positions) that are re-read from the settings in every
  loop iteration and before every manual action.

All buy/sell rules come from ``tradebot.strategy.risk`` so live behaviour matches the
backtest. The bot only manages positions it opened itself (recorded in its ``TradeJournal``);
several positions per symbol are possible (``risk.max_positions_per_symbol``).

Order safety:
* every order gets a unique client order id and is written to the journal BEFORE it is sent
  (``pending_orders``); a lost response is looked up by that id (or, in LIVE, detected via
  the balance change) instead of assuming nothing happened;
* orders whose state cannot be determined are kept "uncertain" and block further orders for
  that position/symbol until reconciled in a later loop or cleared by the user;
* base-currency fees are subtracted from the bought quantity (never sell user coins);
* positions below the exchange minimum become "dust" and no longer block exits.

A bot object that was never started can still be used for manual actions
(``sell_position``, ``buy_manual``, ``wallet``) – the ``BotManager`` keeps exactly one bot
object per mode and routes every action through its lock.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from tradebot.brokers.base import (
    AuthenticationError,
    BadSymbolError,
    Broker,
    BrokerError,
    InsufficientFundsError,
    InvalidOrderError,
    NotSupportedError,
    order_fee_base,
    supports_client_order_id,
)
from tradebot.core.models import (
    TIMEFRAMES,
    ClosedTrade,
    ExitReason,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Side,
    TradingMode,
    WatchItem,
    utcnow,
)
from tradebot.core.settings import AppSettings, SettingsStore
from tradebot.engine.journal import TradeJournal
from tradebot.strategy import risk as R
from tradebot.strategy.models import RiskSettings, Strategy

log = logging.getLogger(__name__)

EVENT_LEVELS = ("info", "warning", "error", "trade")
EVENT_KINDS = ("buy", "sell", "stop_loss", "take_profit", "trailing_stop", "error",
               "kill_switch", "info", "warning", "paused", "external_closed", "dust",
               "order_uncertain")
MAX_BACKOFF_SECONDS = 600.0
MAX_POSITION_BACKOFF_SECONDS = 120.0
FULL_FILL_TOLERANCE = 0.999  # sold >= 99.9 % of what we hold -> position fully closed
DUST_RATIO = 1e-3  # free balance below 0.1 % of the position -> treated as "not held anymore"
CANCEL_RETRIES = 3
# errors after which an order was definitely NOT accepted by the broker
DEFINITIVE_ORDER_ERRORS = (InsufficientFundsError, InvalidOrderError, AuthenticationError,
                           BadSymbolError, NotSupportedError)
FINAL_STATUSES = (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED)

EXIT_REASON_LABELS = {
    ExitReason.SIGNAL: "Ausstiegssignal",
    ExitReason.TAKE_PROFIT: "Take-Profit",
    ExitReason.STOP_LOSS: "Stop-Loss",
    ExitReason.TRAILING_STOP: "Trailing-Stop",
    ExitReason.MAX_BARS: "Max. Haltedauer",
    ExitReason.MANUAL: "Manuell",
    ExitReason.END_OF_DATA: "Datenende",
}
EXIT_REASON_KINDS = {
    ExitReason.TAKE_PROFIT: "take_profit",
    ExitReason.STOP_LOSS: "stop_loss",
    ExitReason.TRAILING_STOP: "trailing_stop",
}


class BotError(Exception):
    """User-facing bot error (German message)."""


class OrderUncertainError(BrokerError):
    """The broker may or may not have executed an order (event already emitted)."""


# ---- strategy engine hooks (module-level so tests can monkeypatch them) ---------------
def _compute_signals(strategy: Strategy, df: pd.DataFrame) -> pd.DataFrame:
    from tradebot.strategy.engine import compute_signals

    return compute_signals(strategy, df)


def _warmup_bars(strategy: Strategy) -> int:
    from tradebot.strategy.engine import warmup_bars

    return int(warmup_bars(strategy))


def _candles_to_load(strategy: Strategy) -> int:
    from tradebot.strategy import engine

    fn = getattr(engine, "candles_to_load", None)
    if fn is not None:
        return int(fn(strategy))
    if strategy.risk.klines_to_load:
        return int(strategy.risk.klines_to_load)
    return _warmup_bars(strategy) + 5


def _validate(strategy: Strategy) -> list[str]:
    from tradebot.strategy.engine import validate

    problems = list(validate(strategy))
    for msg in R.validate_risk(strategy.risk):
        if msg not in problems:
            problems.append(msg)
    return problems


def journal_path(store: SettingsStore, mode: TradingMode) -> Path:
    return store.state_dir / f"journal_{TradingMode(mode).value}.json"


def paper_account_path(store: SettingsStore) -> Path:
    return store.state_dir / "paper_account.json"


def quote_of(symbol: str) -> str | None:
    """Quote currency from a ccxt style symbol ("BTC/USDT" -> "USDT"), else None."""
    if "/" in symbol:
        return symbol.split("/", 1)[1].split(":", 1)[0]
    return None


def base_of(symbol: str) -> str:
    return symbol.split("/", 1)[0]


def held_quantity(balance: dict, symbol: str) -> float:
    """Free quantity of the asset traded in ``symbol``.

    Brokers key holdings differently (ccxt: base asset "BTC"; Alpaca: "AAPL"/"BTCUSD";
    paper: the symbol). A missing entry means 0 – ccxt omits zero balances.
    """
    for key in (base_of(symbol), symbol, symbol.replace("/", "")):
        if key in balance:
            try:
                return max(float(balance[key] or 0.0), 0.0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def broker_env(options: dict | None) -> str:
    """Environment of a broker configuration: "sandbox" (testnet), "paper" or "live"."""
    o = options or {}
    if o.get("sandbox") or o.get("testnet"):
        return "sandbox"
    if o.get("paper"):
        return "paper"
    return "live"


ENV_LABELS = {"sandbox": "Testnet", "paper": "Broker-Papierkonto", "live": "Echtgeld-Konto",
              "test": "Papierhandel"}


@dataclass
class BotEvent:
    time: datetime
    level: str  # "info" | "warning" | "error" | "trade"
    message: str
    symbol: str | None = None
    kind: str = "info"  # see EVENT_KINDS – lets the GUI pick a sound
    data: dict = field(default_factory=dict)  # symbol, qty, price, pnl, pnl_pct, reason ...

    def to_dict(self) -> dict:
        return {"time": self.time.isoformat(), "level": self.level, "message": self.message,
                "symbol": self.symbol, "kind": self.kind, "data": dict(self.data)}


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def position_info(journal: TradeJournal, pos: Position, price: float | None, risk: RiskSettings,
                  current_env: Callable[[str], str] | None = None) -> dict:
    """GUI/wallet view of one bot position."""
    cur = price if price else pos.entry_price
    entry_fee = journal.entry_fees.get(pos.id)
    cost = pos.quantity * pos.entry_price
    net_pnl = R.estimated_net_pnl(pos, cur, risk, entry_fee)
    broker = journal.position_brokers.get(pos.id)
    env = journal.position_envs.get(pos.id)
    pending = journal.pending_for_position(pos.id)
    mismatch = bool(env and broker and current_env and current_env(broker) != env)
    return {
        "id": pos.id,
        "symbol": pos.symbol,
        "broker": broker,
        "env": env,
        "env_mismatch": mismatch,
        "quantity": pos.quantity,
        "entry_price": pos.entry_price,
        "entry_time": pos.entry_time.isoformat(),
        "entry_fee": entry_fee,
        "cost": cost,
        "current_price": price,
        "value": pos.market_value(cur),
        "market_value": pos.market_value(cur),
        "unrealized_pnl": pos.unrealized_pnl(cur),
        "unrealized_pnl_pct": pos.unrealized_pnl_pct(cur),
        "estimated_net_proceeds": R.estimated_net_proceeds(pos, cur, risk),
        "estimated_net_pnl": net_pnl,
        "estimated_net_pnl_pct": net_pnl / cost * 100.0 if cost else 0.0,
        "highest_price": pos.highest_price,
        "bars_held": pos.bars_held,
        "strategy": pos.strategy,
        "stop_levels": R.stop_levels(pos, risk),
        "external_closed": pos.id in journal.external_closed,
        "dust": pos.id in journal.dust,
        "pending_order": pending is not None,
        "uncertain": bool(pending and pending.get("uncertain")),
        "pending_cid": (pending or {}).get("cid") or (pending or {}).get("client_order_id"),
    }


def symbol_state_info(journal: TradeJournal, risk: RiskSettings, now: datetime) -> dict[str, dict]:
    out = {}
    for sym, st in journal.symbol_states.items():
        cooldown_until = None
        if risk.cooldown_seconds and st.last_buy_time:
            cu = st.last_buy_time + timedelta(seconds=risk.cooldown_seconds)
            cooldown_until = cu if cu > now else None
        paused = st.paused_indefinitely or bool(st.paused_until and st.paused_until > now)
        out[sym] = {
            "paused": paused,
            "paused_until": _iso(st.paused_until) if st.paused_until and st.paused_until > now else None,
            "paused_indefinitely": st.paused_indefinitely,
            "loss_streak": st.loss_streak,
            "cooldown_until": _iso(cooldown_until),
            "last_buy_time": _iso(st.last_buy_time),
            "last_entry_price": st.last_entry_price,
        }
    return out


def build_status(
    journal: TradeJournal,
    mode: TradingMode,
    *,
    running: bool,
    strategy_name: str | None,
    risk: RiskSettings | None,
    started_at: datetime | None = None,
    last_loop_at: datetime | None = None,
    equity: float | None = None,
    cash: float | None = None,
    prices: dict[str, float] | None = None,
    events: list[BotEvent] | None = None,
    max_open_positions: int | None = None,
    killed: bool = False,
    now: datetime | None = None,
    current_env: Callable[[str], str] | None = None,
) -> dict:
    risk = risk or RiskSettings()
    now = now or utcnow()
    prices = prices if prices is not None else journal.last_prices
    positions = [position_info(journal, p, prices.get(p.symbol), risk, current_env)
                 for p in sorted(journal.positions.values(), key=lambda p: p.entry_time)]
    if equity is None and journal.equity_history:
        equity = journal.equity_history[-1][1]
    start = journal.start_equity
    pnl = (equity - start) if (equity is not None and start) else None
    return {
        "running": running,
        "mode": TradingMode(mode).value,
        "strategy": strategy_name or journal.strategy_name,
        "started_at": _iso(started_at),
        "last_loop_at": _iso(last_loop_at),
        "equity": equity,
        "cash": cash,
        "start_equity": start,
        "pnl": pnl,
        "pnl_pct": (pnl / start * 100.0) if (pnl is not None and start) else None,
        "realized_pnl": sum(t.pnl for t in journal.closed_trades),
        "positions": positions,
        "symbol_states": symbol_state_info(journal, risk, now),
        "pending_orders": [dict(o) for o in journal.pending_orders.values()],
        "closed_trades": [t.to_dict() for t in journal.closed_trades],
        "equity_history": [list(x) for x in journal.equity_history],
        "events": [e.to_dict() for e in (events or [])],
        "max_open_positions": max_open_positions,
        "kill_switch": killed or (journal.kill_switch_date == now.date().isoformat()),
        "daily_start_equity": (journal.daily_start or {}).get("equity"),
    }


def journal_status(mode: TradingMode, store: SettingsStore, events: list[BotEvent] | None = None) -> dict:
    """Status of a mode without bot object (last persisted state)."""
    journal = TradeJournal(journal_path(store, mode))
    risk = None
    if journal.strategy_name:
        try:
            risk = store.load_strategy(journal.strategy_name).risk
        except (KeyError, ValueError):
            risk = None
    return build_status(journal, mode, running=False, strategy_name=journal.strategy_name,
                        risk=risk, events=events)


def reset_paper_state(store: SettingsStore, starting_cash: float) -> None:
    """Reset the shared paper account and the TEST journal."""
    try:
        from tradebot.brokers.paper import PaperAccount

        PaperAccount(starting_cash=float(starting_cash),
                     path=paper_account_path(store)).reset(float(starting_cash))
    except ImportError:  # pragma: no cover - paper broker module missing
        log.warning("Papierkonto-Modul nicht verfügbar – nur Journal zurückgesetzt")
    journal = TradeJournal(journal_path(store, TradingMode.TEST))
    journal.reset()
    journal.start_equity = float(starting_cash)
    journal.save()


def _filled_qty(order: Order) -> float:
    if order.filled_quantity and order.filled_quantity > 0:
        return float(order.filled_quantity)
    if order.status == OrderStatus.FILLED:
        return float(order.quantity)
    return 0.0


class TradingBot:
    def __init__(
        self,
        mode: TradingMode,
        strategy: Strategy,
        watchlist: list[WatchItem],
        store: SettingsStore,
        broker_factory: Callable[[str], Broker] | None = None,
        poll_seconds: int | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        trade_on_first_candle: bool = False,
    ) -> None:
        self.mode = TradingMode(mode)
        self.strategy = strategy
        self.watchlist = list(watchlist)
        self.store = store
        self.settings: AppSettings = store.load()
        self._poll_override = poll_seconds
        self.poll_seconds = int(poll_seconds or self.settings.poll_seconds or 15)
        self.clock = clock or utcnow
        # If False, the first closed candle seen per symbol only sets the baseline
        # (avoid acting on stale signals right after start).
        self.trade_on_first_candle = trade_on_first_candle
        self.order_poll_seconds = 1.0  # fetch_order polling interval for pending orders
        self.journal = TradeJournal(journal_path(store, self.mode))

        self._factory = broker_factory
        self._uses_default_factory = broker_factory is None
        self._brokers: dict[str, Broker] = {}
        self._paper_account: Any = None
        self._balance_cache: dict[str, dict[str, float]] = {}
        self._prices: dict[str, float] = dict(self.journal.last_prices)
        self._errors: dict[str, tuple[int, datetime]] = {}  # candle/signal part per symbol
        self._tick_errors: dict[str, int] = {}  # price-tick failures per symbol (no backoff)
        self._pos_errors: dict[str, tuple[int, datetime]] = {}  # failed exits per position
        self._blocked_seen: dict[str, tuple[str, str]] = {}  # symbol -> (reason, candle)
        self._notified: set[str] = set()  # one-time warnings
        self._unreliable = False  # a balance/price fetch failed in this iteration
        self._events: deque[BotEvent] = deque(maxlen=500)
        self._subscribers: list[Callable[[BotEvent], Any]] = []
        self._task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._killed = False
        self.started_at: datetime | None = None
        self.last_loop_at: datetime | None = None
        self.equity: float | None = None
        self.cash: float | None = None

    # ---- properties -------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._running

    @property
    def risk(self) -> RiskSettings:
        return self.strategy.risk

    @property
    def max_open_positions(self) -> int:
        n = int(self.risk.max_open_positions)
        if self.mode == TradingMode.LIVE:
            n = min(n, int(self.settings.live_max_open_positions))
        return max(n, 0)

    @property
    def events(self) -> list[BotEvent]:
        return list(self._events)

    async def configure(self, strategy: Strategy, watchlist: list[WatchItem],
                        broker_factory: Callable[[str], Broker] | None = None,
                        poll_seconds: int | None = None) -> None:
        """Change strategy/watchlist of a stopped bot (keeps journal, lock and subscribers)."""
        async with self._get_lock():
            if self._running:
                raise BotError("Der Bot läuft – Strategie kann nur im gestoppten Zustand geändert werden")
            self.strategy = strategy
            self.watchlist = list(watchlist)
            if broker_factory is not self._factory:
                self._factory = broker_factory
                self._uses_default_factory = broker_factory is None
                self._brokers.clear()
                self._paper_account = None
            self._poll_override = poll_seconds
            self._reload_settings()

    def _reload_settings(self) -> None:
        try:
            self.settings = self.store.load()
        except Exception:  # noqa: BLE001 - keep the last known settings
            log.exception("Einstellungen konnten nicht neu geladen werden")
        self.poll_seconds = int(self._poll_override or self.settings.poll_seconds or 15)

    # ---- events -----------------------------------------------------------------------
    def subscribe(self, callback: Callable[[BotEvent], Any]) -> Callable[[], None]:
        """Register an event callback (sync or async). Returns an unsubscribe function."""
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe

    def _emit(self, level: str, message: str, symbol: str | None = None,
              kind: str | None = None, data: dict | None = None) -> BotEvent:
        if kind is None:
            kind = {"error": "error", "warning": "warning"}.get(level, "info")
        payload = dict(data or {})
        if symbol and "symbol" not in payload:
            payload["symbol"] = symbol
        ev = BotEvent(self.clock(), level, message, symbol, kind, payload)
        self._events.append(ev)
        prefix = f"[{self.mode.value.upper()}] "
        pylevel = {"warning": logging.WARNING, "error": logging.ERROR}.get(level, logging.INFO)
        log.log(pylevel, prefix + message)
        for cb in list(self._subscribers):
            try:
                res = cb(ev)
                if inspect.isawaitable(res):
                    asyncio.ensure_future(res)
            except Exception:  # noqa: BLE001 - never let GUI callbacks break the bot
                log.exception("Fehler in Ereignis-Callback")
        return ev

    def _emit_once(self, key: str, level: str, message: str, symbol: str | None = None,
                   kind: str | None = None, data: dict | None = None) -> None:
        if key not in self._notified:
            self._notified.add(key)
            self._emit(level, message, symbol, kind, data)

    # ---- brokers ----------------------------------------------------------------------
    def _get_paper_account(self):
        if self._paper_account is None:
            from tradebot.brokers.paper import PaperAccount

            self._paper_account = PaperAccount(starting_cash=float(self.settings.paper_starting_cash),
                                               path=paper_account_path(self.store))
        return self._paper_account

    def _default_factory(self, key: str) -> Broker:
        from tradebot.brokers import create_broker

        creds = self.store.get_credentials(key) or None
        options = self.settings.broker_options.get(key)
        if self.mode == TradingMode.LIVE:
            return create_broker(key, creds, options)
        from tradebot.brokers.paper import PaperBroker

        data = create_broker(key, creds, options)
        return PaperBroker(data_broker=data, account=self._get_paper_account(),
                           fee_pct=self.risk.fee_pct, slippage_pct=self.risk.slippage_pct)

    def get_broker(self, key: str) -> Broker:
        b = self._brokers.get(key)
        if b is None:
            factory = self._factory or self._default_factory
            b = factory(key)
            self._brokers[key] = b
        return b

    def env_for(self, broker_key: str) -> str:
        """Current environment of a broker key ("test" for paper trading)."""
        if self.mode == TradingMode.TEST:
            return "test"
        return broker_env(self.settings.broker_options.get(broker_key))

    def _env_ok(self, pos_id: str) -> bool:
        env = self.journal.position_envs.get(pos_id)
        key = self.journal.position_brokers.get(pos_id)
        return not env or not key or env == self.env_for(key)

    def _counted(self, pos_id: str) -> bool:
        """Position counts for equity / limits (really held, sellable environment)."""
        return (pos_id not in self.journal.external_closed and pos_id not in self.journal.dust
                and self._env_ok(pos_id))

    def _quote_for(self, broker_key: str, symbol: str) -> str:
        q = quote_of(symbol)
        if q:
            return q
        try:
            return self.get_broker(broker_key).info().quote_currency
        except Exception:  # noqa: BLE001
            return "USD"

    async def _balance(self, broker_key: str, fresh: bool = False) -> dict[str, float]:
        if fresh or broker_key not in self._balance_cache:
            broker = self.get_broker(broker_key)
            self._balance_cache[broker_key] = dict(await asyncio.to_thread(broker.fetch_balance))
        return self._balance_cache[broker_key]

    def _paper(self):
        """The shared PaperAccount when the default TEST-mode factory is used, else None."""
        if self.mode == TradingMode.TEST and self._uses_default_factory:
            return self._get_paper_account()
        return None

    async def _cash_for(self, broker_key: str, symbol: str) -> float:
        acc = self._paper()
        if acc is not None:
            return float(acc.cash)
        bal = await self._balance(broker_key)
        return float(bal.get(self._quote_for(broker_key, symbol), 0.0) or 0.0)

    async def _total_cash(self) -> float:
        acc = self._paper()
        if acc is not None:
            return float(acc.cash)
        pairs: set[tuple[str, str]] = set()
        for w in self.watchlist:
            if w.enabled:
                pairs.add((w.broker, self._quote_for(w.broker, w.symbol)))
        for pid, key in self.journal.position_brokers.items():
            pos = self.journal.positions.get(pid)
            if pos is not None:
                pairs.add((key, self._quote_for(key, pos.symbol)))
        total = 0.0
        counted: set[tuple[int, str]] = set()  # same broker object + currency only once
        for key, quote in sorted(pairs):
            try:
                broker = self.get_broker(key)
                if not broker.info().supports_trading or (id(broker), quote) in counted:
                    continue
                counted.add((id(broker), quote))
                bal = await self._balance(key)
            except Exception as exc:  # noqa: BLE001
                self._unreliable = True
                log.warning("Kontostand von %s nicht abrufbar: %s", key, exc)
                continue
            total += float(bal.get(quote, 0.0) or 0.0)
        return total

    def _active_positions(self) -> list[Position]:
        return [p for i, p in self.journal.positions.items() if self._counted(i)]

    def _invested_value(self) -> float:
        return sum(p.quantity * (self._prices.get(p.symbol) or p.entry_price)
                   for p in self._active_positions())

    def _pnl_mark(self) -> float:
        """Bot's own P&L: realized (all closed trades) + unrealized of its open positions."""
        realized = sum(t.pnl for t in self.journal.closed_trades)
        unrealized = sum(p.unrealized_pnl(self._prices.get(p.symbol) or p.entry_price)
                         for p in self._active_positions())
        return realized + unrealized

    async def _compute_equity(self) -> tuple[float, float]:
        cash = await self._total_cash()
        return cash + self._invested_value(), cash

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    def _require_manual_trading(self, broker_key: str, buying: bool) -> Broker:
        """Safety checks for manual actions (also without running bot); reloads settings."""
        self._reload_settings()
        try:
            broker = self.get_broker(broker_key)
            info = broker.info()
        except Exception as exc:  # noqa: BLE001
            raise BotError(f"Broker '{broker_key}' konnte nicht initialisiert werden: {exc}") from exc
        if self.mode == TradingMode.LIVE:
            if not self.settings.live_enabled:
                raise BotError("Live-Handel ist in den Einstellungen deaktiviert")
            if not info.supports_trading:
                raise BotError(f"{info.name} unterstützt keinen Handel")
            if info.credential_fields and not self.store.has_credentials(broker_key):
                err = self.store.credentials_error()
                raise BotError(err or f"Für {info.name} sind keine API-Zugangsdaten hinterlegt")
            if buying and self.journal.kill_switch_date == self.clock().date().isoformat():
                raise BotError("Der Verlust-Notschalter wurde heute ausgelöst – keine Käufe möglich")
        return broker

    def _env_mismatch_message(self, pos: Position) -> str:
        key = self.journal.position_brokers.get(pos.id, "?")
        env = self.journal.position_envs.get(pos.id, "?")
        return (f"{pos.symbol}: Position {pos.id} wurde im {ENV_LABELS.get(env, env)} von {key} "
                f"eröffnet, eingestellt ist aber das {ENV_LABELS.get(self.env_for(key), self.env_for(key))} – "
                "sie wird nicht gehandelt. Bitte Broker-Einstellung zurückstellen oder "
                "'Position vergessen' verwenden.")

    # ---- lifecycle --------------------------------------------------------------------
    async def _preflight(self) -> None:
        try:
            problems = _validate(self.strategy)
        except ImportError as exc:
            raise BotError("Strategie-Engine ist nicht verfügbar") from exc
        if problems:
            raise BotError("Strategie ist ungültig: " + "; ".join(problems))
        if self.strategy.timeframe not in TIMEFRAMES:
            raise BotError(f"Unbekannter Zeitrahmen: {self.strategy.timeframe}")
        items = [w for w in self.watchlist if w.enabled]
        if not items:
            raise BotError("Die Watchlist enthält keine aktiven Werte")
        if self.mode == TradingMode.LIVE:
            if not self.settings.live_enabled:
                raise BotError("Live-Handel ist in den Einstellungen deaktiviert "
                               "(Hauptschalter 'Live-Handel erlauben')")
            if self.journal.kill_switch_date == self.clock().date().isoformat():
                raise BotError("Der Verlust-Notschalter wurde heute ausgelöst – "
                               "Live-Handel ist erst morgen (UTC) wieder möglich")
            wrong = [p for i, p in self.journal.positions.items() if not self._env_ok(i)]
            if wrong:
                raise BotError(self._env_mismatch_message(wrong[0]))
        for key in sorted({w.broker for w in items}):
            try:
                broker = self.get_broker(key)
                info = broker.info()
            except Exception as exc:  # noqa: BLE001
                raise BotError(f"Broker '{key}' konnte nicht initialisiert werden: {exc}") from exc
            if self.mode != TradingMode.LIVE:
                continue
            if not info.supports_trading:
                raise BotError(f"{info.name} liefert nur Kursdaten und unterstützt keinen "
                               "Live-Handel – bitte aus der Watchlist entfernen oder deaktivieren")
            if info.credential_fields and not self.store.has_credentials(key):
                err = self.store.credentials_error()
                raise BotError(err or f"Für {info.name} sind keine API-Zugangsdaten hinterlegt")
            try:
                await asyncio.to_thread(broker.fetch_balance)
            except Exception as exc:  # noqa: BLE001
                raise BotError(f"Verbindung zu {info.name} fehlgeschlagen: {exc}") from exc

    async def start(self) -> None:
        if self._running:
            return
        self._reload_settings()
        await self._preflight()
        self._running = True
        self._killed = False
        self._notified.clear()
        self.started_at = self.clock()
        self.journal.strategy_name = self.strategy.name
        # "loss_streak_pause_hours == 0" means: paused until bot restart or manual release
        released = [s for s, st in self.journal.symbol_states.items() if st.paused_indefinitely]
        for s in released:
            self.journal.symbol_states[s].paused_indefinitely = False
        self.journal.save()
        self._wake = asyncio.Event()
        mode_label = "LIVE (echtes Geld)" if self.mode == TradingMode.LIVE else "TEST (Papierhandel)"
        self._emit("info", f"Bot gestartet: {mode_label}, Strategie '{self.strategy.name}', "
                           f"{sum(w.enabled for w in self.watchlist)} Werte, "
                           f"Intervall {self.poll_seconds}s")
        if released:
            self._emit("info", "Pausierte Symbole durch Neustart freigegeben: " + ", ".join(released))
        self._task = asyncio.create_task(self._loop(), name=f"tradebot-{self.mode.value}")

    async def stop(self) -> None:
        if not self._running and self._task is None:
            return
        was_running = self._running
        self._running = False
        if self._wake is not None:
            self._wake.set()
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            try:
                # graceful: let a running iteration finish (never abort mid-order)
                await asyncio.wait_for(asyncio.shield(task), timeout=120)
            except asyncio.TimeoutError:
                task.cancel()
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                log.exception("Fehler beim Beenden des Bots")
        # save under the lock: never interleaves with a manual action on this same journal
        async with self._get_lock():
            self.journal.save()
        if was_running:
            self._emit("info", "Bot gestoppt")

    def _halt(self) -> None:
        self._running = False
        if self._wake is not None:
            self._wake.set()

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001
                log.exception("Fehler im Bot-Durchlauf")
                self._emit("error", f"Fehler im Durchlauf: {exc}")
            if not self._running:
                break
            assert self._wake is not None
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass
        self._running = False

    # ---- main iteration ---------------------------------------------------------------
    async def run_once(self) -> None:
        """Exactly one loop iteration over all symbols (also usable from tests)."""
        async with self._get_lock():
            now = self.clock()
            self._reload_settings()  # master switch & limits apply immediately
            self._balance_cache.clear()
            self._unreliable = False
            if self.mode == TradingMode.LIVE and not self.settings.live_enabled:
                self._emit("error", "Live-Handel wurde in den Einstellungen deaktiviert – "
                                    "Bot gestoppt, es werden keine Orders mehr gesendet.")
                self._halt()
                self.journal.save()
                return
            await self._reconcile_pending(now)
            items = [w for w in self.watchlist if w.enabled]
            seen = {(w.broker, w.symbol) for w in items}
            # keep managing exits of bot positions whose symbol was removed from the watchlist
            orphans = {}
            for pid, pos in self.journal.positions.items():
                key = self.journal.position_brokers.get(pid)
                if key and (key, pos.symbol) not in seen:
                    orphans[(key, pos.symbol)] = WatchItem(pos.symbol, key)
            work = [(w, True) for w in items] + [(w, False) for w in orphans.values()]
            for item, candles in work:
                # (a) price tick + protective exits: ALWAYS, never subject to backoff
                price = await self._tick(item, now)
                if not candles or price is None or not self._running_or_manual():
                    continue
                # (b) candles / signals: with per-symbol error backoff
                key = f"{item.broker}:{item.symbol}"
                err = self._errors.get(key)
                if err and now < err[1]:
                    continue
                try:
                    await self._process_candles(item, price, now)
                    if err:
                        self._emit("info", f"{item.symbol}: wieder erreichbar", item.symbol)
                    self._errors.pop(key, None)
                except Exception as exc:  # noqa: BLE001
                    count = (err[0] if err else 0) + 1
                    delay = min(self.poll_seconds * 2 ** (count - 1), MAX_BACKOFF_SECONDS)
                    self._errors[key] = (count, now + timedelta(seconds=delay))
                    if count <= 3 or count % 10 == 0:
                        self._emit("error", f"{item.symbol}: Fehler ({exc}) – "
                                            f"nächster Versuch in {int(delay)}s", item.symbol)
                    log.debug("Symbolfehler %s", key, exc_info=True)
            await self._after_iteration(now)
            self.last_loop_at = now

    def _running_or_manual(self) -> bool:
        # run_once may be called directly (tests) on a non-started bot; a kill switch or
        # master-switch stop during the iteration must prevent further buys.
        return not self._killed

    async def _tick(self, item: WatchItem, now: datetime) -> float | None:
        sym = item.symbol
        try:
            broker = self.get_broker(item.broker)
            price = float(await asyncio.to_thread(broker.fetch_price, sym))
        except Exception as exc:  # noqa: BLE001
            if any(self._counted(p.id) for p in self.journal.positions_for(sym, item.broker)):
                self._unreliable = True
            n = self._tick_errors.get(sym, 0) + 1
            self._tick_errors[sym] = n
            if n <= 3 or n % 20 == 0:
                self._emit("error", f"{sym}: Kurs nicht abrufbar ({exc})", sym)
            return None
        if self._tick_errors.pop(sym, None):
            self._emit("info", f"{sym}: Kurs wieder verfügbar", sym)
        if price <= 0:
            return None
        self._prices[sym] = price
        for pos in self._exit_candidates(sym, item.broker, now):
            hit = R.check_exit(pos, self.risk, price, price, price)
            if hit:
                await self._guarded_sell(pos, hit[0], price, now)
        return price

    def _exit_candidates(self, symbol: str, broker: str, now: datetime) -> list[Position]:
        out = []
        for pos in self.journal.positions_for(symbol, broker):
            if pos.id in self.journal.external_closed or pos.id in self.journal.dust:
                continue
            if not self._env_ok(pos.id):
                self._emit_once(f"env:{pos.id}", "error", self._env_mismatch_message(pos), symbol)
                continue
            if self.journal.pending_for_position(pos.id) is not None:
                continue  # never send a second order while one is pending/uncertain
            err = self._pos_errors.get(pos.id)
            if err and now < err[1]:
                continue
            out.append(pos)
        return out

    async def _guarded_sell(self, pos: Position, reason: ExitReason, price: float, now: datetime) -> bool:
        """Sell one position; failures never affect other positions. True if sold."""
        try:
            trade = await self._sell(pos.id, reason, price)
            self._pos_errors.pop(pos.id, None)
            return trade is not None
        except Exception as exc:  # noqa: BLE001
            count = (self._pos_errors.get(pos.id, (0, now))[0]) + 1
            delay = min(self.poll_seconds * 2 ** (count - 1), MAX_POSITION_BACKOFF_SECONDS)
            self._pos_errors[pos.id] = (count, now + timedelta(seconds=delay))
            if count <= 3 or count % 10 == 0:
                self._emit("error", f"{pos.symbol}: Verkauf ({EXIT_REASON_LABELS.get(reason, reason.value)}) "
                                    f"fehlgeschlagen ({exc}) – neuer Versuch in {int(delay)}s",
                           pos.symbol, data={"position_id": pos.id})
            log.debug("Verkaufsfehler %s", pos.id, exc_info=True)
            return False

    async def _after_iteration(self, now: datetime) -> None:
        try:
            equity, cash = await self._compute_equity()
        except Exception as exc:  # noqa: BLE001
            self._emit("warning", f"Kontostand konnte nicht ermittelt werden: {exc}")
            self.journal.last_prices = dict(self._prices)
            self.journal.save()
            return
        self.equity, self.cash = equity, cash
        mark = self._pnl_mark()
        if self.journal.start_equity is None and not self._unreliable:
            self.journal.start_equity = equity
        if not self._unreliable:
            self.journal.record_equity(now, equity)
        self.journal.last_prices = dict(self._prices)
        if self._unreliable:
            # incomplete data: no new daily baseline and no kill-switch decision
            self.journal.save()
            return
        daily_start = self.journal.ensure_daily_start(now.date(), equity, mark)
        if self.mode == TradingMode.LIVE and daily_start > 0:
            limit = float(self.settings.live_daily_loss_limit_pct)
            loss = float(self.journal.daily_start.get("pnl_mark", mark)) - mark
            loss_pct = loss / daily_start * 100.0
            if limit > 0 and loss_pct >= limit:
                self._killed = True
                self._halt()
                self.journal.kill_switch_date = now.date().isoformat()
                self._emit("error", f"NOTSCHALTER: Tagesverlust des Bots {loss:.2f} "
                                    f"({loss_pct:.2f}% vom Tagesstartwert, Limit {limit:.2f}%) – "
                                    "Bot wurde gestoppt. Offene Positionen bleiben bestehen.",
                           kind="kill_switch", data={"loss": loss, "loss_pct": loss_pct, "equity": equity})
        self.journal.save()

    async def _process_candles(self, item: WatchItem, price: float, now: datetime) -> None:
        sym = item.symbol
        broker = self.get_broker(item.broker)
        tf = self.strategy.timeframe
        tf_seconds = TIMEFRAMES[tf]
        limit = max(int(_candles_to_load(self.strategy)), 2)
        df = await asyncio.to_thread(functools.partial(broker.fetch_ohlcv, sym, tf, limit=limit))
        if df is None or len(df) == 0:
            return
        idx = pd.DatetimeIndex(df.index)
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
            df = df.set_axis(idx)
        closed = df[(idx + pd.Timedelta(seconds=tf_seconds)) <= pd.Timestamp(now)]
        if len(closed) == 0:
            return
        last_ts = closed.index[-1]
        ckey = f"{item.broker}:{sym}"
        prev_iso = self.journal.last_candle.get(ckey)
        prev = pd.Timestamp(prev_iso) if prev_iso else None
        if prev is not None and last_ts <= prev:
            return  # already processed
        self.journal.last_candle[ckey] = last_ts.isoformat()
        new_bars = closed[closed.index > prev] if prev is not None else closed.iloc[-1:]
        for pos in self.journal.positions_for(sym, item.broker):
            if self._counted(pos.id):
                R.update_after_bar(pos, float(new_bars["high"].max()))
                pos.bars_held += max(len(new_bars) - 1, 0)
        if prev is None and not self.trade_on_first_candle:
            return
        signals = await asyncio.to_thread(_compute_signals, self.strategy, closed)
        if signals is None or len(signals) == 0:
            return
        row = signals.iloc[-1]
        entry = bool(row.get("entry", False))
        exit_ = bool(row.get("exit", False))
        exited = False
        if exit_:
            for pos in self._exit_candidates(sym, item.broker, now):
                if R.signal_exit_allowed(pos, price, self.risk, self.journal.entry_fees.get(pos.id)):
                    exited |= await self._guarded_sell(pos, ExitReason.SIGNAL, price, now)
                else:
                    self._emit("info", f"{sym}: Ausstiegssignal ignoriert – Mindestgewinn "
                                       "nicht erreicht", sym)
        if entry and not exited and self._running_or_manual():
            await self._signal_buy(item, price, now, last_ts.isoformat())

    def _block(self, sym: str, reason: str, candle: str) -> None:
        if self._blocked_seen.get(sym) != (reason, candle):
            self._blocked_seen[sym] = (reason, candle)
            self._emit("info", f"{sym}: Kaufsignal nicht ausgeführt – {reason}", sym)

    async def _signal_buy(self, item: WatchItem, price: float, now: datetime, candle: str) -> None:
        sym = item.symbol
        if self._killed:
            return
        if self.journal.pending_buy_for(sym, item.broker) is not None:
            self._block(sym, "eine frühere Kauforder ist noch offen bzw. ungeklärt", candle)
            return
        active = self._active_positions()
        n_sym = sum(1 for p in active if p.symbol == sym)
        allowed, reason = R.can_open(self.journal.state(sym), n_sym, len(active),
                                     price, now, self.risk, max_open_positions=self.max_open_positions)
        if not allowed:
            self._block(sym, reason, candle)
            return
        cash = await self._cash_for(item.broker, sym)
        equity, _ = await self._compute_equity()
        hard_cap = float(self.settings.live_max_order_value) if self.mode == TradingMode.LIVE else None
        budget = R.order_budget(equity, cash, self._invested_value(), self.risk, hard_cap=hard_cap)
        if budget <= 0:
            self._block(sym, f"kein ausreichendes Budget (verfügbar {cash:.2f}, "
                             f"Mindestbetrag {self.risk.min_order_value:.2f})", candle)
            return
        await self._buy(item, price, budget, reason="Einstiegssignal")

    # ---- orders: placement & reconciliation -------------------------------------------
    def _fee_total(self, order: Order, qty: float, price: float) -> float:
        if order.fee and order.fee > 0:
            return float(order.fee)
        return qty * price * self.risk.fee_pct / 100.0

    async def _cancel(self, broker: Broker, order_id: str, sym: str) -> bool:
        try:
            await asyncio.to_thread(broker.cancel_order, order_id, sym)
            return True
        except BrokerError as exc:
            log.warning("Order %s konnte nicht storniert werden: %s", order_id, exc)
            return False

    async def _fetch_order(self, broker: Broker, entry: dict) -> Order | None:
        """Current state of a journaled order; None if unknown right now.

        Raises ``LookupError`` if the broker definitely has no such order.
        """
        sym, oid, cid = entry["symbol"], entry.get("order_id"), entry["client_order_id"]
        try:
            if oid:
                fetch = getattr(broker, "fetch_order", None)
                if fetch is None:
                    return None
                return await asyncio.to_thread(fetch, oid, sym)
            if not entry.get("cid_sent"):
                return None
            lookup = getattr(broker, "fetch_order_by_client_id", None)
            if lookup is None:
                return None
            found = await asyncio.to_thread(lookup, cid, sym)
            if found is None:
                raise LookupError(cid)
            return found
        except NotSupportedError:
            return None
        except BrokerError as exc:
            log.warning("Orderstatus %s nicht abrufbar: %s", oid or cid, exc)
            return None

    async def _place(self, key: str, sym: str, side: Side, qty: float, ref_price: float, *,
                     position_id: str | None = None, reason: str = "", held_qty: float | None = None,
                     base_before: float | None = None) -> tuple[str, Order]:
        """Write-ahead journal entry, place the order, recover lost responses, wait for fill."""
        broker = self.get_broker(key)
        cid = f"tb{self.mode.value[0]}{uuid.uuid4().hex[:22]}"
        lp = R.limit_price(side.value, ref_price, self.risk)
        with_cid = supports_client_order_id(broker)
        entry = {
            "client_order_id": cid, "order_id": None, "broker": key, "symbol": sym,
            "side": side.value, "quantity": qty, "order_type": "limit" if lp else "market",
            "limit_price": lp, "position_id": position_id, "reason": reason,
            "ref_price": ref_price, "created": self.clock().isoformat(), "applied": 0.0,
            "fee_applied": 0.0, "fee_base_applied": 0.0, "held_qty": held_qty,
            "uncertain": False, "open": False, "cid_sent": with_cid, "env": self.env_for(key),
        }
        self.journal.pending_orders[cid] = entry
        self.journal.save()
        otype = OrderType.LIMIT if lp else OrderType.MARKET
        kwargs = {"client_order_id": cid} if with_cid else {}
        try:
            order = await asyncio.to_thread(
                functools.partial(broker.place_order, sym, side, qty, otype, lp, **kwargs))
        except DEFINITIVE_ORDER_ERRORS:
            self.journal.pending_orders.pop(cid, None)
            self.journal.save()
            raise
        except Exception as exc:  # noqa: BLE001 - response lost / unknown outcome
            order = await self._recover_lost_order(entry, exc, base_before)
        self._balance_cache.clear()
        entry["order_id"] = order.id
        self.journal.save()
        order = await self._await_fill(broker, entry, order)
        return cid, order

    async def _recover_lost_order(self, entry: dict, exc: Exception, base_before: float | None) -> Order:
        """The order call failed in a way that does not prove nothing happened."""
        key, sym, cid = entry["broker"], entry["symbol"], entry["client_order_id"]
        side, qty = Side(entry["side"]), float(entry["quantity"])
        broker = self.get_broker(key)
        try:
            found = await self._fetch_order(broker, entry)
        except LookupError:
            # broker definitely has no such order -> it was never placed
            self.journal.pending_orders.pop(cid, None)
            self.journal.save()
            raise exc
        if found is not None:
            self._emit("warning", f"{sym}: Antwort der Börse verloren – Order über die "
                                  "Client-Order-ID gefunden", sym)
            return found
        if self.mode == TradingMode.LIVE and base_before is not None:
            try:
                after = held_quantity(await self._balance(key, fresh=True), sym)
            except Exception:  # noqa: BLE001
                after = None
            if after is not None:
                change = (after - base_before) if side == Side.BUY else (base_before - after)
                if change >= qty * 0.5:
                    filled = qty if side == Side.BUY else min(change, qty)
                    self._emit("warning", f"{sym}: Antwort der Börse verloren – Ausführung anhand "
                                          "des Kontostands erkannt", sym)
                    return Order(sym, side, qty, price=float(entry["ref_price"]),
                                 status=OrderStatus.FILLED, filled_quantity=filled, fee=0.0,
                                 raw={"reconstructed": True})
        entry["uncertain"] = True
        entry["error"] = str(exc)
        self.journal.save()
        what = ("Die Position wird nicht automatisch erneut verkauft" if side == Side.SELL
                else "Keine neuen Käufe für dieses Symbol")
        self._emit("error", f"{sym}: Orderstatus unklar nach Fehler ({exc}). {what}, bis die Order "
                            "geklärt ist – bitte im Konto der Börse prüfen.", sym,
                   kind="order_uncertain", data={"client_order_id": cid, "side": side.value, "qty": qty})
        raise OrderUncertainError(f"Orderstatus für {sym} unklar: {exc}")

    async def _await_fill(self, broker: Broker, entry: dict, order: Order) -> Order:
        """Poll a non-final order until filled or timeout, then cancel the rest.

        If the remainder cannot be cancelled, the journal entry stays ``open`` and is
        reconciled in later loops (no new order for that position/symbol meanwhile).
        """
        sym = entry["symbol"]
        if order.status in FINAL_STATUSES:
            return order
        is_limit = entry["order_type"] == "limit"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(self.settings.order_timeout_seconds)
        fetch = getattr(broker, "fetch_order", None)
        supported = fetch is not None
        while supported:
            try:
                current = await asyncio.to_thread(fetch, order.id, sym)
            except NotSupportedError:
                supported = False
                break
            except BrokerError as exc:
                log.warning("Orderstatus %s nicht abrufbar: %s", order.id, exc)
                current = None
            if current is not None:
                if not current.quantity:
                    current.quantity = order.quantity
                order = current
                if order.status in FINAL_STATUSES:
                    return order
            if loop.time() >= deadline:
                break
            await asyncio.sleep(self.order_poll_seconds)
        if not supported and not is_limit and not order.filled_quantity:
            # broker can't report order state: a market order is assumed executed
            order.status = OrderStatus.FILLED
            order.filled_quantity = order.quantity
            self._emit("warning", f"{sym}: Orderstatus nicht abrufbar – Market-Order wird "
                                  "als ausgeführt angenommen", sym)
            return order
        # timeout: cancel the remainder (with retries), then read the final fill
        cancelled = False
        for attempt in range(CANCEL_RETRIES + 1):
            if await self._cancel(broker, order.id, sym):
                cancelled = True
                break
            if supported:
                try:
                    current = await asyncio.to_thread(fetch, order.id, sym)
                    if current is not None:
                        order = current
                        if order.status in FINAL_STATUSES:
                            return order
                except BrokerError:
                    pass
            await asyncio.sleep(self.order_poll_seconds)
        if cancelled and supported:
            try:
                order = await asyncio.to_thread(fetch, order.id, sym)
            except BrokerError:
                pass
        self._balance_cache.clear()
        if not cancelled:
            entry["open"] = True
            self.journal.save()
            self._emit("error", f"{sym}: Order {order.id} konnte nicht storniert werden – sie wird "
                                "weiter überwacht; bis dahin keine neue Order für diese Position.", sym,
                       kind="order_uncertain", data={"order_id": order.id})
            return order
        if order.status not in FINAL_STATUSES:
            order.status = OrderStatus.CANCELED
        self._emit("warning", f"{sym}: Order nach {self.settings.order_timeout_seconds}s nicht "
                              f"vollständig ausgeführt – Rest storniert "
                              f"(ausgeführt {_filled_qty(order):.8g} von {float(entry['quantity']):.8g})", sym)
        return order

    async def _reconcile_pending(self, now: datetime) -> None:
        """Retry lookups of pending / uncertain orders from earlier iterations."""
        for cid, entry in list(self.journal.pending_orders.items()):
            sym = entry.get("symbol", "?")
            try:
                broker = self.get_broker(entry["broker"])
            except Exception:  # noqa: BLE001
                continue
            try:
                order = await self._fetch_order(broker, entry)
            except LookupError:
                self.journal.pending_orders.pop(cid, None)
                self.journal.save()
                self._emit("info", f"{sym}: ungeklärte Order wurde nicht ausgeführt (bei der Börse "
                                   "nicht vorhanden) – Handel wieder freigegeben", sym)
                continue
            if order is None:
                continue  # still unknown – keep blocking
            entry["order_id"] = order.id
            entry["uncertain"] = False
            if order.status in FINAL_STATUSES:
                entry["open"] = False
                self._emit("info", f"{sym}: offene/ungeklärte Order {order.id} geklärt "
                                   f"({order.status.value}, ausgeführt {_filled_qty(order):.8g})", sym)
                await self._apply_order(cid, order, float(entry.get("ref_price") or 0.0))
                continue
            entry["open"] = True
            await self._apply_order(cid, order, float(entry.get("ref_price") or 0.0))
            created = datetime.fromisoformat(entry["created"])
            if (now - created).total_seconds() > float(self.settings.order_timeout_seconds):
                if await self._cancel(broker, order.id, sym):
                    try:
                        order = await asyncio.to_thread(broker.fetch_order, order.id, sym)
                    except BrokerError:
                        order.status = OrderStatus.CANCELED
                    if order.status not in FINAL_STATUSES:
                        order.status = OrderStatus.CANCELED
                    if cid in self.journal.pending_orders:
                        self.journal.pending_orders[cid]["open"] = False
                        await self._apply_order(cid, order, float(entry.get("ref_price") or 0.0))

    async def _apply_order(self, cid: str, order: Order, ref_price: float,
                           base_before: float | None = None) -> Position | ClosedTrade | None:
        """Book the not-yet-booked part of an order's fill; drop the entry when final."""
        entry = self.journal.pending_orders.get(cid)
        if entry is None:
            return None
        final = not entry.get("open")
        sym, key = entry["symbol"], entry["broker"]
        side = Side(entry["side"])
        filled_total = _filled_qty(order)
        delta = filled_total - float(entry.get("applied") or 0.0)
        price = float(order.price or ref_price or entry.get("ref_price") or 0.0)
        fee_total = self._fee_total(order, filled_total, price)
        fee_delta = max(fee_total - float(entry.get("fee_applied") or 0.0), 0.0)
        fee_base_total = order_fee_base(order)
        fee_base_delta = max(fee_base_total - float(entry.get("fee_base_applied") or 0.0), 0.0)
        result: Position | ClosedTrade | None = None
        if delta > 1e-15:
            if side == Side.BUY:
                result = await self._book_buy(entry, delta, fee_delta, fee_base_delta, price, base_before)
            else:
                result = self._book_sell(entry, delta, fee_delta, price, final)
            entry["applied"] = filled_total
            entry["fee_applied"] = fee_total
            entry["fee_base_applied"] = fee_base_total
        if final:
            self.journal.pending_orders.pop(cid, None)
            if filled_total <= 0:
                if order.status == OrderStatus.REJECTED:
                    self._emit("error", f"{sym}: Order wurde von der Börse abgelehnt", sym)
                else:
                    what = "keine Position eröffnet" if side == Side.BUY else "Position bleibt offen"
                    self._emit("warning", f"{sym}: {'Kauf' if side == Side.BUY else 'Verkauf'}sorder "
                                          f"nicht ausgeführt – {what}", sym)
            elif side == Side.SELL and result is None:
                # final piece booked earlier as partial; close remainder bookkeeping
                pass
        self.journal.save()
        return result

    async def _book_buy(self, entry: dict, delta: float, fee: float, fee_base: float, price: float,
                        base_before: float | None) -> Position:
        sym, key = entry["symbol"], entry["broker"]
        received = max(delta - fee_base, 0.0)
        if self.mode == TradingMode.LIVE and base_before is not None and not entry.get("applied"):
            # what actually arrived (fees withheld in the base asset are not ours to sell)
            try:
                after = held_quantity(await self._balance(key, fresh=True), sym)
                increase = after - base_before
                if 0 < increase < received * 0.99999:
                    log.info("%s: received %.10g instead of %.10g (fee in base asset)", sym, increase, received)
                    received = increase
            except Exception:  # noqa: BLE001
                log.warning("%s: Kontostand nach Kauf nicht abrufbar", sym)
        now = self.clock()
        pos = self.journal.positions.get(entry.get("position_id") or "")
        if pos is not None:
            total = pos.quantity + received
            if total > 0:
                pos.entry_price = (pos.quantity * pos.entry_price + received * price) / total
            pos.quantity = total
            self.journal.entry_fees[pos.id] = self.journal.entry_fees.get(pos.id, 0.0) + fee
        else:
            pos = Position(symbol=sym, quantity=received, entry_price=price, entry_time=now,
                           strategy=self.strategy.name)
            self.journal.open_position(pos, key, fee, env=entry.get("env") or self.env_for(key))
            entry["position_id"] = pos.id
            R.register_buy(self.journal.state(sym), price, now)
        self.journal.save()
        self._emit("trade", f"KAUF {sym}: {received:.8g} @ {price:.6g} "
                            f"(Wert {received * price:.2f}, Gebühr {fee:.4f}) – {entry.get('reason') or ''}",
                   sym, kind="buy", data={"qty": received, "price": price, "fee": fee,
                                          "value": received * price, "position_id": pos.id})
        return pos

    def _book_sell(self, entry: dict, delta: float, fee: float, price: float, final: bool) -> ClosedTrade | None:
        pid = entry.get("position_id")
        pos = self.journal.positions.get(pid or "")
        if pos is None:
            log.warning("Verkauf für unbekannte Position %s gebucht", pid)
            return None
        sym = pos.symbol
        reason = ExitReason(entry.get("reason") or ExitReason.MANUAL.value)
        held = float(entry.get("held_qty") or pos.quantity)
        sold_total = float(entry.get("applied") or 0.0) + delta
        remaining = held - sold_total  # what the bot still really holds of this position
        full = remaining <= held * (1 - FULL_FILL_TOLERANCE)
        entry_fee_total = self.journal.entry_fees.get(pos.id, 0.0)
        entry_fee = entry_fee_total if full else entry_fee_total * min(delta / pos.quantity, 1.0)
        now = self.clock()
        trade = ClosedTrade(symbol=sym, quantity=delta, entry_price=pos.entry_price,
                            exit_price=price, entry_time=pos.entry_time, exit_time=now,
                            fees=entry_fee + fee, exit_reason=reason, strategy=pos.strategy)
        paused = False
        if full:
            self.journal.close_position(pos.id, trade)
            remaining_pos = [p for p in self.journal.positions_for(sym) if self._counted(p.id)]
            paused = R.register_close(self.journal.state(sym), trade.pnl, now, self.risk,
                                      remaining_entry_price=remaining_pos[-1].entry_price if remaining_pos else None)
        else:
            pos.quantity = max(remaining, 0.0)
            self.journal.entry_fees[pos.id] = entry_fee_total - entry_fee
            self.journal.add_trade(trade)
        label = EXIT_REASON_LABELS.get(reason, reason.value)
        part = "" if full else f" (Teilverkauf, Rest {pos.quantity:.8g})"
        self._emit("trade", f"VERKAUF {sym}: {delta:.8g} @ {price:.6g} – {label}, "
                            f"Ergebnis {trade.pnl:+.2f} ({trade.pnl_pct:+.2f}%){part}", sym,
                   kind=EXIT_REASON_KINDS.get(reason, "sell"),
                   data={"qty": delta, "price": price, "pnl": trade.pnl, "pnl_pct": trade.pnl_pct,
                         "reason": reason.value, "position_id": pos.id, "partial": not full})
        if paused:
            st = self.journal.state(sym)
            until = (f"bis {st.paused_until:%d.%m.%Y %H:%M} UTC" if st.paused_until and not st.paused_indefinitely
                     else "bis zur manuellen Freigabe oder zum Neustart")
            self._emit("warning", f"Symbol {sym} pausiert nach {self.risk.max_loss_streak} Verlusten "
                                  f"in Folge – keine neuen Käufe {until}", sym, kind="paused",
                       data={"paused_until": _iso(st.paused_until),
                             "paused_indefinitely": st.paused_indefinitely})
        return trade

    # ---- buy / sell -------------------------------------------------------------------
    async def _buy(self, item: WatchItem, price: float, budget: float, *, reason: str) -> Position | None:
        """Buy for ``budget`` (quote, incl. fees). Returns the new position or None (skipped)."""
        sym = item.symbol
        if self.mode == TradingMode.LIVE and not self.settings.live_enabled:
            self._emit("error", f"{sym}: Kauf verhindert – Live-Handel ist deaktiviert", sym)
            return None
        broker = self.get_broker(item.broker)
        qty = float(broker.normalize_quantity(sym, R.quantity_for_budget(budget, price, self.risk)))
        min_value = float(broker.min_order_value(sym) or 0.0)
        if qty <= 0 or qty * price < min_value:
            self._emit("info", f"{sym}: Kauf übersprungen – Ordergröße zu klein "
                               f"({qty * price:.2f}, Minimum {min_value:.2f})", sym)
            return None
        if self.mode == TradingMode.LIVE and qty * price > float(self.settings.live_max_order_value) * 1.001:
            self._emit("warning", f"{sym}: Kauf übersprungen – Ordergröße überschreitet das Live-Maximum", sym)
            return None
        base_before = None
        if self.mode == TradingMode.LIVE:
            base_before = held_quantity(await self._balance(item.broker, fresh=True), sym)
        try:
            cid, order = await self._place(item.broker, sym, Side.BUY, qty, price, reason=reason,
                                           base_before=base_before)
        except OrderUncertainError:
            return None
        result = await self._apply_order(cid, order, price, base_before=base_before)
        return result if isinstance(result, Position) else None

    async def _mark_external(self, pos: Position, free: float | None) -> None:
        self.journal.external_closed.add(pos.id)
        self.journal.save()
        self._emit("error", f"{pos.symbol}: Position {pos.id} ist beim Broker nicht mehr vorhanden "
                            f"(Bestand {free or 0:.8g}) – vermutlich außerhalb des Bots verkauft. "
                            "Bitte im Wallet prüfen und 'Position vergessen' bestätigen.",
                   pos.symbol, kind="external_closed",
                   data={"position_id": pos.id, "qty": pos.quantity, "free": free})

    async def _mark_dust(self, pos: Position, why: str) -> None:
        self.journal.dust.add(pos.id)
        self.journal.save()
        self._emit("warning", f"{pos.symbol}: Restposten {pos.quantity:.8g} ({why}) kann nicht verkauft "
                              "werden – er wird nicht mehr gehandelt. Im Wallet mit 'Position vergessen' "
                              "entfernen.", pos.symbol, kind="dust",
                   data={"position_id": pos.id, "qty": pos.quantity})

    async def _sell(self, pos_id: str, reason: ExitReason, price: float) -> ClosedTrade | None:
        """Sell one bot position. Returns the ClosedTrade, or None if nothing was booked
        (not filled, order uncertain, externally closed or dust)."""
        pos = self.journal.positions.get(pos_id)
        if pos is None:
            raise BotError(f"Keine Bot-Position mit ID {pos_id}")
        sym = pos.symbol
        key = self.journal.position_brokers.get(pos_id)
        if key is None:
            raise BotError(f"Broker der Position {pos_id} unbekannt")
        if not self._env_ok(pos_id):
            raise BotError(self._env_mismatch_message(pos))
        if self.journal.pending_for_position(pos_id) is not None:
            raise BotError(f"{sym}: Für diese Position ist bereits eine Order offen bzw. ungeklärt")
        if self.mode == TradingMode.LIVE and not self.settings.live_enabled:
            raise BotError("Live-Handel ist in den Einstellungen deaktiviert")
        broker = self.get_broker(key)
        qty = pos.quantity
        held = qty
        base_before = None
        if self.mode == TradingMode.LIVE:
            free = held_quantity(await self._balance(key, fresh=True), sym)
            if free <= qty * DUST_RATIO:
                await self._mark_external(pos, free)
                return None
            if free < qty:  # never sell more than is really there (fees in base asset ...)
                held = qty = free
            base_before = free
        qty_n = float(broker.normalize_quantity(sym, qty))
        min_value = float(broker.min_order_value(sym) or 0.0)
        if qty_n <= 0 or qty_n * price < min_value:
            await self._mark_dust(pos, "unter Mindestmenge/-betrag der Börse")
            return None
        try:
            cid, order = await self._place(key, sym, Side.SELL, qty_n, price, position_id=pos_id,
                                           reason=reason.value, held_qty=held, base_before=base_before)
        except OrderUncertainError:
            return None
        except InvalidOrderError:
            dust_limit = max(min_value, float(self.risk.min_order_value or 0.0), 1.0)
            if pos.quantity * price < dust_limit:
                await self._mark_dust(pos, "von der Börse als ungültige Ordergröße abgelehnt")
                return None
            raise
        except InsufficientFundsError:
            if self.mode == TradingMode.LIVE:
                try:
                    free = held_quantity(await self._balance(key, fresh=True), sym)
                except Exception:  # noqa: BLE001
                    free = None
                if free is not None and free <= pos.quantity * DUST_RATIO:
                    await self._mark_external(pos, free)
                    return None
            raise
        result = await self._apply_order(cid, order, price)
        return result if isinstance(result, ClosedTrade) else None

    # ---- manual control ---------------------------------------------------------------
    def _find_item(self, symbol: str) -> WatchItem:
        for w in self.watchlist:
            if w.symbol == symbol:
                return w
        for p in self.journal.positions_for(symbol):
            key = self.journal.position_brokers.get(p.id)
            if key:
                return WatchItem(symbol, key)
        raise BotError(f"{symbol} ist nicht in der Watchlist")

    async def sell_position(self, position_id: str) -> ClosedTrade:
        """Manually sell one bot position (reason MANUAL)."""
        async with self._get_lock():
            pos = self.journal.positions.get(position_id)
            if pos is None:
                raise BotError(f"Keine offene Bot-Position mit ID {position_id}")
            if position_id in self.journal.external_closed:
                raise BotError(f"Position {position_id} ist beim Broker nicht mehr vorhanden – "
                               "bitte 'Position vergessen' verwenden")
            if position_id in self.journal.dust:
                raise BotError(f"Position {position_id} ist ein nicht verkaufbarer Restposten – "
                               "bitte 'Position vergessen' verwenden")
            key = self.journal.position_brokers.get(position_id) or self._find_item(pos.symbol).broker
            broker = self._require_manual_trading(key, buying=False)
            try:
                price = float(await asyncio.to_thread(broker.fetch_price, pos.symbol))
                self._prices[pos.symbol] = price
                trade = await self._sell(position_id, ExitReason.MANUAL, price)
            except BrokerError as exc:
                self._emit("error", f"{pos.symbol}: manueller Verkauf fehlgeschlagen ({exc})", pos.symbol)
                raise BotError(f"Verkauf von {pos.symbol} fehlgeschlagen: {exc}") from exc
            if trade is None:
                raise BotError(f"Verkauf von {pos.symbol} wurde nicht ausgeführt")
            return trade

    async def close_position(self, symbol: str) -> list[ClosedTrade]:
        """Sell all bot positions of ``symbol``."""
        ids = [p.id for p in self.journal.positions_for(symbol)
               if p.id not in self.journal.external_closed and p.id not in self.journal.dust]
        if not ids:
            raise BotError(f"Keine offene Bot-Position für {symbol}")
        trades, errors = [], []
        for pid in ids:
            try:
                trades.append(await self.sell_position(pid))
            except BotError as exc:
                errors.append(str(exc))
        if errors and not trades:
            raise BotError("; ".join(errors))
        return trades

    async def close_all(self) -> list[ClosedTrade]:
        trades = []
        for pid in list(self.journal.positions):
            if pid in self.journal.external_closed or pid in self.journal.dust:
                continue
            try:
                trades.append(await self.sell_position(pid))
            except BotError:
                pass  # already reported as event
        return trades

    async def buy_manual(self, broker_key: str, symbol: str, quote_amount: float) -> Position:
        """Manual buy of ``quote_amount`` (quote currency incl. fees), tracked like a bot position.

        Ignores cooldown/pause/re-entry rules (explicit user decision) but respects the
        total position limit, cash, the cash reserve and – in LIVE – the per-order hard cap.
        """
        if quote_amount is None or quote_amount <= 0:
            raise BotError("Bitte einen Betrag größer 0 angeben")
        async with self._get_lock():
            broker = self._require_manual_trading(broker_key, buying=True)
            if len(self._active_positions()) >= self.max_open_positions:
                raise BotError(f"Maximale Anzahl offener Positionen ({self.max_open_positions}) erreicht")
            if self.journal.pending_buy_for(symbol, broker_key) is not None:
                raise BotError(f"{symbol}: Eine frühere Kauforder ist noch offen bzw. ungeklärt")
            try:
                price = float(await asyncio.to_thread(broker.fetch_price, symbol))
                if price <= 0:
                    raise BotError(f"{symbol}: kein gültiger Kurs")
                self._prices[symbol] = price
                cash = await self._cash_for(broker_key, symbol)
                budget = min(float(quote_amount), cash - self.risk.cash_reserve)
                if self.mode == TradingMode.LIVE:
                    budget = min(budget, float(self.settings.live_max_order_value))
                if budget <= 0:
                    raise BotError(f"Nicht genügend Guthaben (verfügbar {cash:.2f})")
                pos = await self._buy(WatchItem(symbol, broker_key), price, budget, reason="manueller Kauf")
            except BrokerError as exc:
                self._emit("error", f"{symbol}: manueller Kauf fehlgeschlagen ({exc})", symbol)
                raise BotError(f"Kauf von {symbol} fehlgeschlagen: {exc}") from exc
            if pos is None:
                last = self._events[-1].message if self._events else ""
                raise BotError(last or f"Kauf von {symbol} wurde nicht ausgeführt")
            return pos

    async def buy_now(self, symbol: str, quote_amount: float) -> Position:
        """Manual buy of a watchlist symbol."""
        return await self.buy_manual(self._find_item(symbol).broker, symbol, quote_amount)

    async def forget_position(self, position_id: str) -> None:
        """Remove a position (and its pending orders) from the journal after user confirmation."""
        async with self._get_lock():
            pos = self.journal.remove_position(position_id)
            if pos is None:
                raise BotError(f"Keine Bot-Position mit ID {position_id}")
            for cid, o in list(self.journal.pending_orders.items()):
                if o.get("position_id") == position_id:
                    self.journal.pending_orders.pop(cid, None)
            remaining = self.journal.positions_for(pos.symbol)
            self.journal.state(pos.symbol).last_entry_price = remaining[-1].entry_price if remaining else None
            self.journal.save()
            self._emit("warning", f"{pos.symbol}: Position {position_id} aus dem Journal entfernt "
                                  "(manuell bestätigt)", pos.symbol)

    async def clear_pending_order(self, client_order_id: str) -> None:
        """Drop an uncertain/open order entry after the user checked it at the broker."""
        async with self._get_lock():
            entry = self.journal.pending_orders.pop(client_order_id, None)
            if entry is None:
                raise BotError(f"Keine offene Order {client_order_id}")
            self.journal.save()
            self._emit("warning", f"{entry.get('symbol')}: ungeklärte Order {client_order_id} "
                                  "manuell verworfen", entry.get("symbol"))

    def resume_symbol(self, symbol: str) -> None:
        """Manual release of a paused symbol."""
        R.resume_symbol(self.journal.state(symbol))
        self.journal.save()
        self._emit("info", f"{symbol}: Pause aufgehoben – Käufe wieder erlaubt", symbol)

    def reset_kill_switch(self) -> None:
        """Allow LIVE trading again on the day the kill switch fired (explicit user action)."""
        self.journal.kill_switch_date = None
        self.journal.daily_start = None
        self._killed = False
        self.journal.save()
        self._emit("warning", "Notschalter manuell zurückgesetzt")

    def reset_paper(self, starting_cash: float) -> None:
        if self.mode != TradingMode.TEST:
            raise BotError("Nur das Testkonto kann zurückgesetzt werden")
        if self._running:
            raise BotError("Bitte den Test-Bot vor dem Zurücksetzen stoppen")
        if self._uses_default_factory:
            self._get_paper_account().reset(float(starting_cash))
        self.journal.reset()
        self.journal.start_equity = float(starting_cash)
        self.journal.save()
        self._prices.clear()
        self.equity = self.cash = None
        self._emit("info", f"Testkonto zurückgesetzt auf {float(starting_cash):.2f}")

    # ---- wallet -----------------------------------------------------------------------
    async def wallet(self) -> dict:
        """Positions with CURRENT prices, cash, equity, totals (+ LIVE broker holdings)."""
        self._reload_settings()
        errors: list[str] = []
        pairs = {(self.journal.position_brokers.get(pid), p.symbol) for pid, p in self.journal.positions.items()}
        for key, sym in sorted(pairs, key=lambda x: (x[0] or "", x[1])):
            if not key:
                continue
            try:
                broker = self.get_broker(key)
                self._prices[sym] = float(await asyncio.to_thread(broker.fetch_price, sym))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{sym}: Kurs nicht abrufbar ({exc})")
        self._balance_cache.clear()
        try:
            equity, cash = await self._compute_equity()
        except Exception as exc:  # noqa: BLE001
            equity = cash = None
            errors.append(f"Kontostand nicht abrufbar ({exc})")
        now = self.clock()
        positions = [position_info(self.journal, p, self._prices.get(p.symbol), self.risk, self.env_for)
                     for p in sorted(self.journal.positions.values(), key=lambda p: p.entry_time)]
        active = [p for p in positions if not (p["external_closed"] or p["dust"] or p["env_mismatch"])]
        totals = {
            "positions": len(active),
            "cost": sum(p["cost"] for p in active),
            "value": sum(p["value"] for p in active),
            "unrealized_pnl": sum(p["unrealized_pnl"] for p in active),
            "estimated_net_proceeds": sum(p["estimated_net_proceeds"] for p in active),
            "estimated_net_pnl": sum(p["estimated_net_pnl"] for p in active),
            "realized_pnl": sum(t.pnl for t in self.journal.closed_trades),
        }
        result = {
            "mode": self.mode.value,
            "running": self._running,
            "time": now.isoformat(),
            "cash": cash,
            "equity": equity,
            "start_equity": self.journal.start_equity,
            "positions": positions,
            "pending_orders": [dict(o) for o in self.journal.pending_orders.values()],
            "totals": totals,
            "symbol_states": symbol_state_info(self.journal, self.risk, now),
            "errors": errors,
        }
        if self.mode == TradingMode.LIVE:
            result["broker_holdings"] = await self._broker_holdings()
        return result

    async def _broker_holdings(self) -> dict[str, dict]:
        """Per broker with credentials: balances NOT managed by the bot (read-only info)."""
        keys = set(self.settings.broker_options) | {w.broker for w in self.watchlist}
        keys |= set(self.journal.position_brokers.values())
        out: dict[str, dict] = {}
        for key in sorted(keys):
            if not self.store.has_credentials(key):
                continue
            try:
                bal = await self._balance(key, fresh=True)
            except Exception as exc:  # noqa: BLE001
                out[key] = {"holdings": [], "error": str(exc)}
                continue
            managed: dict[str, float] = {}
            for pid, p in self.journal.positions.items():
                if self.journal.position_brokers.get(pid) == key and self._counted(pid):
                    asset = next((k for k in (base_of(p.symbol), p.symbol, p.symbol.replace("/", ""))
                                  if k in bal), base_of(p.symbol))
                    managed[asset] = managed.get(asset, 0.0) + p.quantity
            holdings = []
            for asset, amount in sorted(bal.items()):
                try:
                    amount = float(amount or 0.0)
                except (TypeError, ValueError):
                    continue
                external = amount - managed.get(asset, 0.0)
                if external > 1e-12:
                    holdings.append({"asset": asset, "total": amount,
                                     "bot_quantity": managed.get(asset, 0.0), "quantity": external})
            out[key] = {"holdings": holdings, "error": None}
        return out

    # ---- status -----------------------------------------------------------------------
    def status(self) -> dict:
        return build_status(
            self.journal, self.mode, running=self._running, strategy_name=self.strategy.name,
            risk=self.risk, started_at=self.started_at, last_loop_at=self.last_loop_at,
            equity=self.equity, cash=self.cash, prices=self._prices, events=list(self._events),
            max_open_positions=self.max_open_positions, killed=self._killed, now=self.clock(),
            current_env=self.env_for,
        )
