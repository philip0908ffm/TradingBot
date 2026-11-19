"""Process-wide GUI state: settings store, bot manager and the event feed.

The GUI is a single-user desktop app, so process-wide state is intended here.
Bot events are collected into ``feed`` with increasing sequence numbers; every open page
polls ``feed.since(seq)`` (see layout.py) to show notifications and play sounds.
"""
from __future__ import annotations

import itertools
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from tradebot.core.models import TradingMode
from tradebot.core.settings import AppSettings, SettingsStore
from tradebot.engine.manager import get_manager


@dataclass
class FeedItem:
    seq: int
    mode: str  # "test" | "live" | "app"
    kind: str  # buy, sell, stop_loss, take_profit, trailing_stop, error, kill_switch, info, ...
    level: str  # info | warning | error | trade
    message: str
    symbol: str | None = None
    data: dict = field(default_factory=dict)
    time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class EventFeed:
    def __init__(self, maxlen: int = 1000) -> None:
        self._items: deque[FeedItem] = deque(maxlen=maxlen)
        self._seq = itertools.count(1)
        self._lock = threading.Lock()

    def publish(self, mode: str, kind: str, message: str, level: str = "info",
                symbol: str | None = None, data: dict | None = None) -> FeedItem:
        with self._lock:
            item = FeedItem(next(self._seq), mode, kind, level, message, symbol, data or {})
            self._items.append(item)
            return item

    def publish_bot_event(self, mode: TradingMode, event) -> None:
        self.publish(mode.value, getattr(event, "kind", None) or event.level, event.message,
                     event.level, getattr(event, "symbol", None), getattr(event, "data", None) or {})

    def last_seq(self) -> int:
        with self._lock:
            return self._items[-1].seq if self._items else 0

    def since(self, seq: int) -> list[FeedItem]:
        with self._lock:
            return [i for i in self._items if i.seq > seq]

    def recent(self, n: int = 200) -> list[FeedItem]:
        with self._lock:
            return list(self._items)[-n:]


class GuiState:
    def __init__(self) -> None:
        self.store = SettingsStore()
        self.manager = get_manager(self.store)
        self.feed = EventFeed()
        self._settings: AppSettings | None = None
        self._subscribed: set[int] = set()

    # settings are cached; call save_settings() to persist + refresh cache
    @property
    def settings(self) -> AppSettings:
        if self._settings is None:
            self._settings = self.store.load()
        return self._settings

    def save_settings(self, settings: AppSettings | None = None) -> None:
        self._settings = settings or self.settings
        self.store.save(self._settings)

    def reload_settings(self) -> AppSettings:
        self._settings = None
        return self.settings

    async def start_bot(self, mode: TradingMode, strategy, watchlist):
        """Start a bot via the manager and route its events into the feed."""
        bot = await self.manager.start(mode, strategy, watchlist)
        self.attach(mode, bot)
        return bot

    def attach(self, mode: TradingMode, bot) -> None:
        if bot is None or id(bot) in self._subscribed:
            return
        self._subscribed.add(id(bot))
        # BotManager.start() copies the subscribers of the previous bot – don't subscribe twice
        if any(getattr(cb, "_tb_feed", False) for cb in getattr(bot, "_subscribers", [])):
            return
        cb = lambda ev, m=mode: self.feed.publish_bot_event(m, ev)  # noqa: E731
        cb._tb_feed = True
        bot.subscribe(cb)


_state: GuiState | None = None


def state() -> GuiState:
    global _state
    if _state is None:
        _state = GuiState()
    return _state
