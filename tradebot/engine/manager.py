"""Holds at most one running bot per TradingMode (TEST and LIVE may run side by side)."""
from __future__ import annotations

import logging
from typing import Callable

from tradebot.brokers.base import Broker
from tradebot.core.models import TradingMode, WatchItem
from tradebot.core.settings import SettingsStore
from tradebot.engine.bot import BotError, TradingBot, journal_status, reset_paper_state
from tradebot.strategy.models import Strategy

log = logging.getLogger(__name__)


class BotManager:
    def __init__(self, store: SettingsStore) -> None:
        self.store = store
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
                         broker_factory=broker_factory, poll_seconds=poll_seconds)
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
        bot = self.get(mode)
        if bot is not None:
            return bot.status()
        return journal_status(TradingMode(mode), self.store)

    def reset_paper(self, starting_cash: float) -> None:
        if self.is_running(TradingMode.TEST):
            raise BotError("Bitte den Test-Bot vor dem Zurücksetzen stoppen")
        reset_paper_state(self.store, starting_cash)
        self._bots.pop(TradingMode.TEST, None)


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
