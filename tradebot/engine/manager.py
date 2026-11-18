"""Holds at most one running bot per TradingMode (TEST and LIVE may run side by side).

Manual wallet actions (sell, buy, forget, resume, wallet view) also work while no bot is
running for a mode: a temporary, never-started ``TradingBot`` is built on the persisted
journal with the same execution broker (paper or live) the bot would use.
"""
from __future__ import annotations

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
        old = self._bots.get(mode)
        if old is not None and old.running:
            raise BotError("Für diesen Modus läuft bereits ein Bot – bitte zuerst stoppen")
        bot = TradingBot(mode, strategy, watchlist, self.store,
                         broker_factory=broker_factory or self.broker_factory, poll_seconds=poll_seconds)
        if old is not None:
            # keep event subscribers of the previous bot (e.g. GUI log views)
            for cb in old._subscribers:
                bot.subscribe(cb)
        await bot.start()  # raises BotError on failed safety checks
        self._bots[mode] = bot
        return bot

    async def stop(self, mode: TradingMode) -> None:
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
        if bot is not None and bot.running:
            return bot.status()
        # not running: always read the journal from disk (manual actions may have changed it)
        return journal_status(mode, self.store, events=bot.events if bot else None)

    def reset_paper(self, starting_cash: float) -> None:
        if self.is_running(TradingMode.TEST):
            raise BotError("Bitte den Test-Bot vor dem Zurücksetzen stoppen")
        reset_paper_state(self.store, starting_cash)

    # ---- manual / wallet actions (work with or without running bot) --------------------
    def _bot_for(self, mode: TradingMode) -> TradingBot:
        """The running bot of ``mode`` or a temporary (never started) one on its journal."""
        mode = TradingMode(mode)
        bot = self.get(mode)
        if bot is not None and bot.running:
            return bot
        settings = self.store.load()
        strategy = None
        name = TradeJournal(journal_path(self.store, mode)).strategy_name or (
            settings.active_strategy_live if mode == TradingMode.LIVE else settings.active_strategy_test)
        if name:
            try:
                strategy = self.store.load_strategy(name)
            except (KeyError, ValueError):
                strategy = None
        tmp = TradingBot(mode, strategy or Strategy(), settings.watchlist, self.store,
                         broker_factory=self.broker_factory)
        if bot is not None:  # forward events of manual actions to the GUI subscribers
            for cb in bot._subscribers:
                tmp.subscribe(cb)
        return tmp

    async def manual_sell(self, mode: TradingMode, position_id: str,
                          store: SettingsStore | None = None) -> ClosedTrade:
        """Sell one bot position (reason MANUAL), even if no bot is running for ``mode``."""
        return await self._bot_for(mode).sell_position(position_id)

    async def manual_buy(self, mode: TradingMode, broker_key: str, symbol: str,
                         quote_amount: float) -> Position:
        """Manual buy tracked as bot position, even if no bot is running for ``mode``."""
        return await self._bot_for(mode).buy_manual(broker_key, symbol, quote_amount)

    async def close_symbol(self, mode: TradingMode, symbol: str) -> list[ClosedTrade]:
        return await self._bot_for(mode).close_position(symbol)

    async def forget_position(self, mode: TradingMode, position_id: str) -> None:
        """Remove an (externally closed) position from the journal after user confirmation."""
        await self._bot_for(mode).forget_position(position_id)

    def resume_symbol(self, mode: TradingMode, symbol: str) -> None:
        self._bot_for(mode).resume_symbol(symbol)

    async def wallet(self, mode: TradingMode) -> dict:
        """Wallet view with current prices (+ LIVE broker holdings not managed by the bot)."""
        return await self._bot_for(mode).wallet()


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
