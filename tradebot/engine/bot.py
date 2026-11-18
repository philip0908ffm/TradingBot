"""Live / paper trading engine.

One ``TradingBot`` runs one strategy over a watchlist in one ``TradingMode``:

* TEST: real prices, virtual money (``PaperBroker`` on a shared ``PaperAccount``).
* LIVE: real orders, guarded by several safety switches (master switch, per-order cap,
  daily loss kill switch, max open positions).

Exits and sizing use ``tradebot.strategy.risk`` so live behaviour matches the backtest.
The bot only manages positions it opened itself (recorded in its ``TradeJournal``).
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from tradebot.brokers.base import Broker, BrokerError
from tradebot.core.models import (
    TIMEFRAMES,
    ClosedTrade,
    ExitReason,
    Order,
    OrderStatus,
    Position,
    Side,
    TradingMode,
    WatchItem,
    utcnow,
)
from tradebot.core.settings import AppSettings, SettingsStore
from tradebot.engine.journal import TradeJournal
from tradebot.strategy.models import RiskSettings, Strategy
from tradebot.strategy.risk import check_exit, position_quantity, stop_levels, update_after_bar

log = logging.getLogger(__name__)

EVENT_LEVELS = ("info", "warning", "error", "trade")
MAX_BACKOFF_SECONDS = 600.0

EXIT_REASON_LABELS = {
    ExitReason.SIGNAL: "Ausstiegssignal",
    ExitReason.TAKE_PROFIT: "Take-Profit",
    ExitReason.STOP_LOSS: "Stop-Loss",
    ExitReason.TRAILING_STOP: "Trailing-Stop",
    ExitReason.MAX_BARS: "Max. Haltedauer",
    ExitReason.MANUAL: "Manuell",
    ExitReason.END_OF_DATA: "Datenende",
}


class BotError(Exception):
    """User-facing bot error (German message)."""


# ---- strategy engine hooks (module-level so tests can monkeypatch them) ---------------
def _compute_signals(strategy: Strategy, df: pd.DataFrame) -> pd.DataFrame:
    from tradebot.strategy.engine import compute_signals

    return compute_signals(strategy, df)


def _warmup_bars(strategy: Strategy) -> int:
    from tradebot.strategy.engine import warmup_bars

    return int(warmup_bars(strategy))


def _validate(strategy: Strategy) -> list[str]:
    from tradebot.strategy.engine import validate

    return list(validate(strategy))


def journal_path(store: SettingsStore, mode: TradingMode) -> Path:
    return store.state_dir / f"journal_{mode.value}.json"


def paper_account_path(store: SettingsStore) -> Path:
    return store.state_dir / "paper_account.json"


def quote_of(symbol: str) -> str | None:
    """Quote currency from a ccxt style symbol ("BTC/USDT" -> "USDT"), else None."""
    if "/" in symbol:
        return symbol.split("/", 1)[1].split(":", 1)[0]
    return None


def base_of(symbol: str) -> str:
    return symbol.split("/", 1)[0]


@dataclass
class BotEvent:
    time: datetime
    level: str  # "info" | "warning" | "error" | "trade"
    message: str
    symbol: str | None = None

    def to_dict(self) -> dict:
        return {"time": self.time.isoformat(), "level": self.level,
                "message": self.message, "symbol": self.symbol}


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


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
) -> dict:
    prices = prices if prices is not None else journal.last_prices
    positions = []
    for sym, pos in journal.positions.items():
        price = prices.get(sym)
        cur = price if price else pos.entry_price
        positions.append({
            "id": pos.id,
            "symbol": sym,
            "broker": journal.position_brokers.get(sym),
            "quantity": pos.quantity,
            "entry_price": pos.entry_price,
            "entry_time": pos.entry_time.isoformat(),
            "current_price": price,
            "market_value": pos.market_value(cur),
            "unrealized_pnl": pos.unrealized_pnl(cur),
            "unrealized_pnl_pct": pos.unrealized_pnl_pct(cur),
            "highest_price": pos.highest_price,
            "bars_held": pos.bars_held,
            "strategy": pos.strategy,
            "stop_levels": stop_levels(pos, risk) if risk else None,
        })
    if equity is None and journal.equity_history:
        equity = journal.equity_history[-1][1]
    start = journal.start_equity
    pnl = (equity - start) if (equity is not None and start) else None
    return {
        "running": running,
        "mode": mode.value,
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
        "closed_trades": [t.to_dict() for t in journal.closed_trades],
        "equity_history": [list(x) for x in journal.equity_history],
        "events": [e.to_dict() for e in (events or [])],
        "max_open_positions": max_open_positions,
        "kill_switch": killed or (journal.kill_switch_date == utcnow().date().isoformat()),
        "daily_start_equity": (journal.daily_start or {}).get("equity"),
    }


def journal_status(mode: TradingMode, store: SettingsStore) -> dict:
    """Status of a mode while no bot is running (last persisted state)."""
    journal = TradeJournal(journal_path(store, mode))
    risk = None
    if journal.strategy_name:
        try:
            risk = store.load_strategy(journal.strategy_name).risk
        except (KeyError, ValueError):
            risk = None
    return build_status(journal, mode, running=False, strategy_name=journal.strategy_name, risk=risk)


def reset_paper_state(store: SettingsStore, starting_cash: float) -> None:
    """Reset the shared paper account and the TEST journal."""
    try:
        from tradebot.brokers.paper import PaperAccount

        PaperAccount(paper_account_path(store), float(starting_cash)).reset(float(starting_cash))
    except ImportError:  # pragma: no cover - paper broker module missing
        log.warning("Papierkonto-Modul nicht verfügbar – nur Journal zurückgesetzt")
    journal = TradeJournal(journal_path(store, TradingMode.TEST))
    journal.reset()
    journal.start_equity = float(starting_cash)
    journal.save()


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
        self.poll_seconds = int(poll_seconds or self.settings.poll_seconds or 15)
        self.clock = clock or utcnow
        # If False, the first closed candle seen per symbol only sets the baseline
        # (avoid acting on stale signals right after start).
        self.trade_on_first_candle = trade_on_first_candle
        self.journal = TradeJournal(journal_path(store, self.mode))

        self._factory = broker_factory
        self._uses_default_factory = broker_factory is None
        self._brokers: dict[str, Broker] = {}
        self._paper_account: Any = None
        self._balance_cache: dict[str, dict[str, float]] = {}
        self._prices: dict[str, float] = dict(self.journal.last_prices)
        self._errors: dict[str, tuple[int, datetime]] = {}
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
    def max_open_positions(self) -> int:
        n = int(self.strategy.risk.max_open_positions)
        if self.mode == TradingMode.LIVE:
            n = min(n, int(self.settings.live_max_open_positions))
        return max(n, 0)

    @property
    def events(self) -> list[BotEvent]:
        return list(self._events)

    # ---- events -----------------------------------------------------------------------
    def subscribe(self, callback: Callable[[BotEvent], Any]) -> Callable[[], None]:
        """Register an event callback (sync or async). Returns an unsubscribe function."""
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe

    def _emit(self, level: str, message: str, symbol: str | None = None) -> BotEvent:
        ev = BotEvent(self.clock(), level, message, symbol)
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

    # ---- brokers ----------------------------------------------------------------------
    def _get_paper_account(self):
        if self._paper_account is None:
            from tradebot.brokers.paper import PaperAccount

            self._paper_account = PaperAccount(paper_account_path(self.store),
                                               float(self.settings.paper_starting_cash))
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
                           fee_pct=self.strategy.risk.fee_pct,
                           slippage_pct=self.strategy.risk.slippage_pct)

    def get_broker(self, key: str) -> Broker:
        b = self._brokers.get(key)
        if b is None:
            factory = self._factory or self._default_factory
            b = factory(key)
            self._brokers[key] = b
        return b

    def _quote_for(self, broker_key: str, symbol: str) -> str:
        q = quote_of(symbol)
        if q:
            return q
        try:
            return self.get_broker(broker_key).info().quote_currency
        except Exception:  # noqa: BLE001
            return "USD"

    async def _balance(self, broker_key: str) -> dict[str, float]:
        if broker_key not in self._balance_cache:
            broker = self.get_broker(broker_key)
            self._balance_cache[broker_key] = dict(await asyncio.to_thread(broker.fetch_balance))
        return self._balance_cache[broker_key]

    async def _cash_for(self, broker_key: str, symbol: str) -> float:
        if self._paper_account is not None:
            return float(self._paper_account.cash)
        bal = await self._balance(broker_key)
        return float(bal.get(self._quote_for(broker_key, symbol), 0.0) or 0.0)

    async def _total_cash(self) -> float:
        if self._paper_account is not None:
            return float(self._paper_account.cash)
        pairs: set[tuple[str, str]] = set()
        for w in self.watchlist:
            if w.enabled:
                pairs.add((w.broker, self._quote_for(w.broker, w.symbol)))
        for sym, key in self.journal.position_brokers.items():
            pairs.add((key, self._quote_for(key, sym)))
        total = 0.0
        for key, quote in sorted(pairs):
            try:
                bal = await self._balance(key)
            except Exception as exc:  # noqa: BLE001
                log.warning("Kontostand von %s nicht abrufbar: %s", key, exc)
                continue
            total += float(bal.get(quote, 0.0) or 0.0)
        return total

    async def _compute_equity(self) -> tuple[float, float]:
        cash = await self._total_cash()
        value = sum(p.quantity * (self._prices.get(s) or p.entry_price)
                    for s, p in self.journal.positions.items())
        return cash + value, cash

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

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
        for key in sorted({w.broker for w in items}):
            try:
                broker = self.get_broker(key)
                info = broker.info()
            except BrokerError as exc:
                raise BotError(f"Broker '{key}' konnte nicht initialisiert werden: {exc}") from exc
            except Exception as exc:  # noqa: BLE001
                raise BotError(f"Broker '{key}' konnte nicht initialisiert werden: {exc}") from exc
            if self.mode != TradingMode.LIVE:
                continue
            if not info.supports_trading:
                raise BotError(f"{info.name} liefert nur Kursdaten und unterstützt keinen "
                               "Live-Handel – bitte aus der Watchlist entfernen oder deaktivieren")
            if info.credential_fields and not self.store.has_credentials(key):
                raise BotError(f"Für {info.name} sind keine API-Zugangsdaten hinterlegt")
            try:
                await asyncio.to_thread(broker.fetch_balance)
            except Exception as exc:  # noqa: BLE001
                raise BotError(f"Verbindung zu {info.name} fehlgeschlagen: {exc}") from exc

    async def start(self) -> None:
        if self._running:
            return
        self.settings = self.store.load()
        await self._preflight()
        self._running = True
        self._killed = False
        self.started_at = self.clock()
        self.journal.strategy_name = self.strategy.name
        self._wake = asyncio.Event()
        mode_label = "LIVE (echtes Geld)" if self.mode == TradingMode.LIVE else "TEST (Papierhandel)"
        self._emit("info", f"Bot gestartet: {mode_label}, Strategie '{self.strategy.name}', "
                           f"{sum(w.enabled for w in self.watchlist)} Werte, "
                           f"Intervall {self.poll_seconds}s")
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
                await asyncio.wait_for(asyncio.shield(task), timeout=60)
            except asyncio.TimeoutError:
                task.cancel()
            except Exception:  # noqa: BLE001
                log.exception("Fehler beim Beenden des Bots")
        self.journal.save()
        if was_running:
            self._emit("info", "Bot gestoppt")

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
            self._balance_cache.clear()
            items = [w for w in self.watchlist if w.enabled]
            seen = {w.symbol for w in items}
            # keep managing exits of bot positions whose symbol was removed from the watchlist
            orphans = [WatchItem(s, k) for s, k in self.journal.position_brokers.items() if s not in seen]
            for item, candles in [(w, True) for w in items] + [(w, False) for w in orphans]:
                key = f"{item.broker}:{item.symbol}"
                err = self._errors.get(key)
                if err and now < err[1]:
                    continue
                try:
                    await self._process_symbol(item, now, candles)
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

    async def _after_iteration(self, now: datetime) -> None:
        try:
            equity, cash = await self._compute_equity()
        except Exception as exc:  # noqa: BLE001
            self._emit("warning", f"Kontostand konnte nicht ermittelt werden: {exc}")
            self.journal.last_prices = dict(self._prices)
            self.journal.save()
            return
        self.equity, self.cash = equity, cash
        if self.journal.start_equity is None:
            self.journal.start_equity = equity
        self.journal.record_equity(now, equity)
        daily_start = self.journal.ensure_daily_start(now.date(), equity)
        self.journal.last_prices = dict(self._prices)
        if self.mode == TradingMode.LIVE and daily_start > 0:
            limit = float(self.settings.live_daily_loss_limit_pct)
            loss_pct = (daily_start - equity) / daily_start * 100.0
            if limit > 0 and loss_pct >= limit:
                self._killed = True
                self._running = False
                self.journal.kill_switch_date = now.date().isoformat()
                if self._wake is not None:
                    self._wake.set()
                self._emit("error", f"NOTSCHALTER: Tagesverlust {loss_pct:.2f}% erreicht "
                                    f"(Limit {limit:.2f}%) – Bot wurde gestoppt. "
                                    "Offene Positionen bleiben bestehen.")
        self.journal.save()

    async def _process_symbol(self, item: WatchItem, now: datetime, candles: bool = True) -> None:
        sym = item.symbol
        broker = self.get_broker(item.broker)
        price = float(await asyncio.to_thread(broker.fetch_price, sym))
        if price > 0:
            self._prices[sym] = price
        pos = self.journal.positions.get(sym)
        own = pos is not None and self.journal.position_brokers.get(sym, item.broker) == item.broker
        exited = False
        # (a) protective exits on the live price tick
        if own and price > 0:
            hit = check_exit(pos, self.strategy.risk, price, price, price)
            if hit:
                await self._sell(item.broker, sym, hit[0], price)
                exited = True
        if not candles:
            return
        # (b) new closed candle -> signals
        tf = self.strategy.timeframe
        tf_seconds = TIMEFRAMES[tf]
        limit = _warmup_bars(self.strategy) + 5
        df = await asyncio.to_thread(functools.partial(broker.fetch_ohlcv, sym, tf, limit=limit))
        if df is None or len(df) == 0:
            return
        idx = pd.DatetimeIndex(df.index)
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
            df = df.set_axis(idx)
        closed_mask = (idx + pd.Timedelta(seconds=tf_seconds)) <= pd.Timestamp(now)
        closed = df[closed_mask]
        if len(closed) == 0:
            return
        last_ts = closed.index[-1]
        key = f"{item.broker}:{sym}"
        prev_iso = self.journal.last_candle.get(key)
        prev = pd.Timestamp(prev_iso) if prev_iso else None
        if prev is not None and last_ts <= prev:
            return  # already processed
        self.journal.last_candle[key] = last_ts.isoformat()
        new_bars = closed[closed.index > prev] if prev is not None else closed.iloc[-1:]
        pos = self.journal.positions.get(sym)
        own = pos is not None and self.journal.position_brokers.get(sym, item.broker) == item.broker
        if own:
            update_after_bar(pos, float(new_bars["high"].max()))
            pos.bars_held += max(len(new_bars) - 1, 0)
        if prev is None and not self.trade_on_first_candle:
            return
        signals = await asyncio.to_thread(_compute_signals, self.strategy, closed)
        if signals is None or len(signals) == 0:
            return
        row = signals.iloc[-1]
        entry = bool(row.get("entry", False)) if hasattr(row, "get") else False
        exit_ = bool(row.get("exit", False)) if hasattr(row, "get") else False
        if own and exit_:
            await self._sell(item.broker, sym, ExitReason.SIGNAL, price)
        elif pos is None and entry and not exited:
            await self._buy(item, price, reason="Einstiegssignal")

    # ---- orders -----------------------------------------------------------------------
    def _fee(self, order: Order, qty: float, price: float) -> float:
        if order.fee and order.fee > 0:
            return float(order.fee)
        return qty * price * self.strategy.risk.fee_pct / 100.0

    @staticmethod
    def _check_order(order: Order, action: str, symbol: str) -> None:
        if order.status in (OrderStatus.REJECTED, OrderStatus.CANCELED):
            raise BrokerError(f"{action}-Order für {symbol} wurde abgelehnt ({order.status.value})")

    async def _buy(self, item: WatchItem, price: float, *, reason: str,
                   quote_amount: float | None = None, manual: bool = False) -> tuple[Position | None, str]:
        sym = item.symbol
        if self._killed:
            return None, "Notschalter aktiv – keine neuen Käufe"
        if sym in self.journal.positions:
            return None, f"Für {sym} ist bereits eine Position offen"
        if len(self.journal.positions) >= self.max_open_positions:
            msg = f"{sym}: Kaufsignal ignoriert – max. {self.max_open_positions} offene Positionen erreicht"
            self._emit("info", msg, sym)
            return None, msg
        if self.mode == TradingMode.LIVE and not self.settings.live_enabled:
            return None, "Live-Handel ist in den Einstellungen deaktiviert"
        if price <= 0:
            return None, f"{sym}: kein gültiger Kurs"
        broker = self.get_broker(item.broker)
        risk = self.strategy.risk
        cash = await self._cash_for(item.broker, sym)
        if quote_amount is not None:
            budget = min(float(quote_amount), cash)
            eff = price * (1 + risk.slippage_pct / 100) * (1 + risk.fee_pct / 100)
            qty = max(budget / eff, 0.0)
        else:
            equity, _ = await self._compute_equity()
            qty = position_quantity(equity, cash, price, risk)
        if self.mode == TradingMode.LIVE:
            cap = float(self.settings.live_max_order_value)
            if qty * price > cap:
                qty = cap / price
                self._emit("info", f"{sym}: Ordergröße auf Live-Maximum {cap:.2f} begrenzt", sym)
        qty = float(broker.normalize_quantity(sym, qty))
        min_value = float(broker.min_order_value(sym) or 0.0)
        if qty <= 0 or qty * price < min_value:
            msg = (f"{sym}: Kauf übersprungen – Ordergröße zu klein "
                   f"({qty * price:.2f}, Minimum {min_value:.2f}, verfügbar {cash:.2f})")
            self._emit("info", msg, sym)
            return None, msg
        if self.mode == TradingMode.LIVE and qty * price > float(self.settings.live_max_order_value) * 1.001:
            msg = f"{sym}: Kauf übersprungen – Ordergröße überschreitet das Live-Maximum"
            self._emit("warning", msg, sym)
            return None, msg
        order = await asyncio.to_thread(broker.place_order, sym, Side.BUY, qty)
        self._balance_cache.pop(item.broker, None)
        self._check_order(order, "Kauf", sym)
        filled = float(order.filled_quantity or 0.0) or qty
        if order.status == OrderStatus.NEW and not order.filled_quantity:
            self._emit("warning", f"{sym}: Kauforder angenommen, aber noch nicht ausgeführt", sym)
        fill_price = float(order.price or price)
        fee = self._fee(order, filled, fill_price)
        pos = Position(symbol=sym, quantity=filled, entry_price=fill_price,
                       entry_time=self.clock(), strategy=self.strategy.name)
        self.journal.open_position(pos, item.broker, fee)
        self.journal.save()
        self._emit("trade", f"KAUF {sym}: {filled:.8g} @ {fill_price:.6g} "
                            f"(Wert {filled * fill_price:.2f}, Gebühr {fee:.4f}) – {reason}", sym)
        return pos, "ok"

    async def _sell(self, broker_key: str, sym: str, reason: ExitReason, price: float) -> ClosedTrade:
        pos = self.journal.positions.get(sym)
        if pos is None:
            raise BotError(f"Keine Bot-Position für {sym}")
        broker = self.get_broker(broker_key)
        qty = pos.quantity
        if self.mode == TradingMode.LIVE:
            # fees may have been deducted in the base asset -> never sell more than we hold
            bal = await self._balance(broker_key)
            free = bal.get(base_of(sym))
            if free is not None and 0 < float(free) < qty:
                qty = float(free)
        qty = float(broker.normalize_quantity(sym, qty))
        if qty <= 0:
            raise BrokerError(f"Verkaufsmenge für {sym} ist nach Rundung 0")
        order = await asyncio.to_thread(broker.place_order, sym, Side.SELL, qty)
        self._balance_cache.pop(broker_key, None)
        self._check_order(order, "Verkauf", sym)
        sold = float(order.filled_quantity or 0.0) or qty
        fill_price = float(order.price or price)
        exit_fee = self._fee(order, sold, fill_price)
        entry_fee = self.journal.entry_fees.get(pos.id, 0.0)
        trade = ClosedTrade(symbol=sym, quantity=sold, entry_price=pos.entry_price,
                            exit_price=fill_price, entry_time=pos.entry_time,
                            exit_time=self.clock(), fees=entry_fee + exit_fee,
                            exit_reason=reason, strategy=pos.strategy)
        self.journal.close_position(sym, trade)
        self.journal.save()
        self._emit("trade", f"VERKAUF {sym}: {sold:.8g} @ {fill_price:.6g} – "
                            f"{EXIT_REASON_LABELS.get(reason, reason.value)}, "
                            f"Ergebnis {trade.pnl:+.2f} ({trade.pnl_pct:+.2f}%)", sym)
        return trade

    # ---- manual control ---------------------------------------------------------------
    def _find_item(self, symbol: str) -> WatchItem:
        for w in self.watchlist:
            if w.symbol == symbol:
                return w
        key = self.journal.position_brokers.get(symbol)
        if key:
            return WatchItem(symbol, key)
        raise BotError(f"{symbol} ist nicht in der Watchlist")

    async def close_position(self, symbol: str) -> ClosedTrade:
        async with self._get_lock():
            if symbol not in self.journal.positions:
                raise BotError(f"Keine offene Bot-Position für {symbol}")
            key = self.journal.position_brokers.get(symbol) or self._find_item(symbol).broker
            broker = self.get_broker(key)
            try:
                price = float(await asyncio.to_thread(broker.fetch_price, symbol))
                self._prices[symbol] = price
                return await self._sell(key, symbol, ExitReason.MANUAL, price)
            except BrokerError as exc:
                self._emit("error", f"{symbol}: manueller Verkauf fehlgeschlagen ({exc})", symbol)
                raise BotError(f"Verkauf von {symbol} fehlgeschlagen: {exc}") from exc

    async def close_all(self) -> list[ClosedTrade]:
        trades = []
        for sym in list(self.journal.positions):
            try:
                trades.append(await self.close_position(sym))
            except BotError:
                pass  # already reported as event
        return trades

    async def buy_now(self, symbol: str, quote_amount: float) -> Position:
        if quote_amount is None or quote_amount <= 0:
            raise BotError("Bitte einen Betrag größer 0 angeben")
        async with self._get_lock():
            item = self._find_item(symbol)
            broker = self.get_broker(item.broker)
            if self.mode == TradingMode.LIVE:
                if not broker.info().supports_trading:
                    raise BotError(f"{broker.info().name} unterstützt keinen Handel")
            try:
                price = float(await asyncio.to_thread(broker.fetch_price, symbol))
                self._prices[symbol] = price
                pos, msg = await self._buy(item, price, reason="manueller Kauf",
                                           quote_amount=quote_amount, manual=True)
            except BrokerError as exc:
                self._emit("error", f"{symbol}: manueller Kauf fehlgeschlagen ({exc})", symbol)
                raise BotError(f"Kauf von {symbol} fehlgeschlagen: {exc}") from exc
            if pos is None:
                raise BotError(msg)
            return pos

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

    # ---- status -----------------------------------------------------------------------
    def status(self) -> dict:
        return build_status(
            self.journal, self.mode, running=self._running, strategy_name=self.strategy.name,
            risk=self.strategy.risk, started_at=self.started_at, last_loop_at=self.last_loop_at,
            equity=self.equity, cash=self.cash, prices=self._prices, events=list(self._events),
            max_open_positions=self.max_open_positions, killed=self._killed,
        )
