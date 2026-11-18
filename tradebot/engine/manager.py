"""Holds exactly one bot object per TradingMode (TEST and LIVE may run side by side).

The bot object of a mode is kept for the whole process lifetime, started or not. Every
manual action (sell, buy, forget, resume, wallet view) and the trading loop go through that
same object – and therefore through its journal and its lock – so two actions can never
work on diverging copies of the journal. A per-mode manager lock additionally serialises
start / stop / reconfiguration with manual actions.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from tradebot.brokers.base import Broker
from tradebot.core.models import ClosedTrade, Position, TradingMode, WatchItem
from tradebot.core.settings import SettingsStore
from tradebot.engine.bot import (
    BotError,
    TradingBot,
    journal_path,
    journal_status,
    reset_paper_state,
)
from tradebot.engine.journal import TradeJournal
from tradebot.strategy.models import Strategy

log = logging.getLogger(__name__)


class BotManager:
    def __init__(self, store: SettingsStore, broker_factory: Callable[[str], Broker] | None = None) -> None:
        self.store = store
        self.broker_factory = broker_factory  # None = real brokers (tests inject fakes)
        self._bots: dict[TradingMode, TradingBot] = {}
        self._locks: dict[TradingMode, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}

    # ---- helpers ----------------------------------------------------------------------
    def _mode_lock(self, mode: TradingMode) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        cur = self._locks.get(mode)
        if cur is None or cur[0] is not loop:
            cur = (loop, asyncio.Lock())
            self._locks[mode] = cur
        return cur[1]

    def _default_strategy(self, mode: TradingMode) -> Strategy:
        settings = self.store.load()
        name = TradeJournal(journal_path(self.store, mode)).strategy_name or (
            settings.active_strategy_live if mode == TradingMode.LIVE else settings.active_strategy_test)
        if name:
            try:
                return self.store.load_strategy(name)
            except (KeyError, ValueError):
                pass
        return Strategy()

    def _ensure_bot(self, mode: TradingMode) -> TradingBot:
        """The one bot object of ``mode`` (created stopped on first use)."""
        mode = TradingMode(mode)
        bot = self._bots.get(mode)
        if bot is None:
            bot = TradingBot(mode, self._default_strategy(mode), self.store.load().watchlist,
                             self.store, broker_factory=self.broker_factory)
            self._bots[mode] = bot
        return bot

    # ---- lifecycle --------------------------------------------------------------------
    def get(self, mode: TradingMode) -> TradingBot | None:
        return self._bots.get(TradingMode(mode))

    def is_running(self, mode: TradingMode) -> bool:
        bot = self.get(mode)
        return bool(bot and bot.running)

    async def start(
        self,
        mode: TradingMode,
        strategy: Strategy,
        watchlist: list[WatchItem],
        broker_factory: Callable[[str], Broker] | None = None,
        poll_seconds: int | None = None,
    ) -> TradingBot:
        mode = TradingMode(mode)
        async with self._mode_lock(mode):
            bot = self._bots.get(mode)
            if bot is not None and bot.running:
                raise BotError("Für diesen Modus läuft bereits ein Bot – bitte zuerst stoppen")
            factory = broker_factory or self.broker_factory
            if bot is None:
                bot = TradingBot(mode, strategy, watchlist, self.store,
                                 broker_factory=factory, poll_seconds=poll_seconds)
                self._bots[mode] = bot
            else:
                await bot.configure(strategy, watchlist, factory, poll_seconds)
            await bot.start()  # raises BotError on failed safety checks
            return bot

    async def stop(self, mode: TradingMode) -> None:
        mode = TradingMode(mode)
        async with self._mode_lock(mode):
            bot = self.get(mode)
            if bot is not None:
                await bot.stop()

    async def stop_all(self) -> None:
        for mode in list(self._bots):
            try:
                await self.stop(mode)
            except Exception:  # noqa: BLE001
                log.exception("Fehler beim Stoppen des Bots (%s)", mode.value)

    def status(self, mode: TradingMode) -> dict:
        mode = TradingMode(mode)
        bot = self.get(mode)
        if bot is not None:
            return bot.status()
        return journal_status(mode, self.store)

    def reset_paper(self, starting_cash: float) -> None:
        if self.is_running(TradingMode.TEST):
            raise BotError("Bitte den Test-Bot vor dem Zurücksetzen stoppen")
        reset_paper_state(self.store, starting_cash)
        bot = self.get(TradingMode.TEST)
        if bot is not None:  # reload the reset state into the one bot object
            bot.journal = TradeJournal(journal_path(self.store, TradingMode.TEST))
            bot._prices.clear()
            bot._paper_account = None
            bot._brokers.clear()
            bot.equity = bot.cash = None

    # ---- manual / wallet actions (work with or without running bot) --------------------
    async def manual_sell(self, mode: TradingMode, position_id: str,
                          store: SettingsStore | None = None) -> ClosedTrade:
        """Sell one bot position (reason MANUAL), even if no bot is running for ``mode``."""
        async with self._mode_lock(TradingMode(mode)):
            return await self._ensure_bot(mode).sell_position(position_id)

    async def manual_buy(self, mode: TradingMode, broker_key: str, symbol: str,
                         quote_amount: float) -> Position:
        """Manual buy tracked as bot position, even if no bot is running for ``mode``."""
        async with self._mode_lock(TradingMode(mode)):
            return await self._ensure_bot(mode).buy_manual(broker_key, symbol, quote_amount)

    async def close_symbol(self, mode: TradingMode, symbol: str) -> list[ClosedTrade]:
        async with self._mode_lock(TradingMode(mode)):
            return await self._ensure_bot(mode).close_position(symbol)

    async def forget_position(self, mode: TradingMode, position_id: str) -> None:
        """Remove an (externally closed / dust) position from the journal after confirmation."""
        async with self._mode_lock(TradingMode(mode)):
            await self._ensure_bot(mode).forget_position(position_id)

    async def clear_pending_order(self, mode: TradingMode, client_order_id: str) -> None:
        """Drop an uncertain order entry after the user checked it at the broker."""
        async with self._mode_lock(TradingMode(mode)):
            await self._ensure_bot(mode).clear_pending_order(client_order_id)

    def reset_kill_switch(self, mode: TradingMode = TradingMode.LIVE) -> None:
        """Explicit user action: allow LIVE buys again on the day the kill switch fired."""
        self._ensure_bot(mode).reset_kill_switch()

    # backwards compatible name (GUI); returns the ONE persistent bot object of the mode
    def _bot_for(self, mode: TradingMode) -> TradingBot:
        return self._ensure_bot(mode)

    def resume_symbol(self, mode: TradingMode, symbol: str) -> None:
        self._ensure_bot(mode).resume_symbol(symbol)

    async def wallet(self, mode: TradingMode) -> dict:
        """Wallet view with current prices (+ LIVE broker holdings not managed by the bot)."""
        async with self._mode_lock(TradingMode(mode)):
            bot = self._ensure_bot(mode)
        async with bot._get_lock():
            return await bot.wallet()


_manager: BotManager | None = None


def get_manager(store: SettingsStore | None = None) -> BotManager:
    """Process-wide BotManager (created on first call)."""
    global _manager
    if _manager is None:
        _manager = BotManager(store or SettingsStore())
    return _manager


def reset_manager() -> None:
    """For tests only."""
    global _manager
    _manager = None
