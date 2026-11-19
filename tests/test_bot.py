import asyncio
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import tradebot.engine.bot as bot_mod
from tradebot.brokers.base import Broker, BrokerError, BrokerInfo, CredentialField
from tradebot.core.models import (
    AssetClass, ExitReason, Order, OrderStatus, OrderType, Side, TradingMode, WatchItem,
)
from tradebot.core.settings import SettingsStore
from tradebot.engine.bot import BotError, TradingBot
from tradebot.engine.manager import BotManager
from tradebot.strategy.models import RiskSettings, Strategy

T0 = datetime(2026, 1, 5, 10, 0, 30, tzinfo=timezone.utc)  # 10:00 candle is still forming


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


class FakeBroker(Broker):
    def __init__(self, key="fake", trading=True, cred_fields=(), cash=10000.0, quote="USDT"):
        super().__init__()
        self.key, self.trading, self.cred_fields = key, trading, list(cred_fields)
        self.cash, self.quote = cash, quote
        self.holdings: dict[str, float] = {}
        self.prices: dict[str, float] = {}
        self.candles: dict[str, pd.DataFrame] = {}
        self.fail: set[str] = set()
        self.fail_ohlcv: set[str] = set()
        self.orders: list[Order] = []

    def info(self):  # instance-level override of the classmethod
        return BrokerInfo(key=self.key, name=f"Fake-{self.key}", asset_classes=[AssetClass.CRYPTO],
                          supports_trading=self.trading, requires_credentials_for_data=False,
                          credential_fields=[CredentialField(k, k) for k in self.cred_fields],
                          quote_currency=self.quote)

    def fetch_ohlcv(self, symbol, timeframe, start=None, end=None, limit=None):
        if symbol in self.fail or symbol in self.fail_ohlcv:
            raise BrokerError("Netzwerkfehler")
        df = self.candles[symbol]
        return df.iloc[-limit:] if limit else df

    def fetch_price(self, symbol):
        if symbol in self.fail:
            raise BrokerError("Netzwerkfehler")
        return self.prices[symbol]

    def list_symbols(self):
        return list(self.prices)

    def fetch_balance(self):
        return {self.quote: self.cash, **self.holdings}

    def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None):
        p = self.prices[symbol]
        fee = quantity * p * 0.001
        base = symbol.split("/")[0]
        if side == Side.BUY:
            self.cash -= quantity * p + fee
            self.holdings[base] = self.holdings.get(base, 0.0) + quantity
        else:
            self.cash += quantity * p - fee
            self.holdings[base] = self.holdings.get(base, 0.0) - quantity
        o = Order(symbol, side, quantity, price=p, status=OrderStatus.FILLED,
                  filled_quantity=quantity, fee=fee)
        self.orders.append(o)
        return o

    def cancel_order(self, order_id, symbol):
        pass


def make_candles(last_open: datetime, n=10, price=100.0, tf=60):
    idx = pd.DatetimeIndex([last_open - timedelta(seconds=tf * i) for i in reversed(range(n))])
    return pd.DataFrame({"open": price, "high": price, "low": price, "close": price,
                         "volume": 1.0}, index=idx)


class Signals:
    def __init__(self):
        self.entry: set = set()
        self.exit: set = set()
        self.calls = 0

    def __call__(self, strategy, df):
        self.calls += 1
        return pd.DataFrame({"entry": [ts in self.entry for ts in df.index],
                             "exit": [ts in self.exit for ts in df.index]}, index=df.index)


@pytest.fixture
def sig(monkeypatch):
    s = Signals()
    monkeypatch.setattr(bot_mod, "_compute_signals", s)
    monkeypatch.setattr(bot_mod, "_warmup_bars", lambda strategy: 3)
    monkeypatch.setattr(bot_mod, "_candles_to_load", lambda strategy: 8)
    monkeypatch.setattr(bot_mod, "_validate", lambda strategy: [])
    return s


@pytest.fixture
def store(tmp_path):
    return SettingsStore(tmp_path / "home")


def strategy(**risk):
    return Strategy(name="Test", timeframe="1m", risk=RiskSettings(**risk))


def setup(store, mode=TradingMode.TEST, symbols=("BTC/USDT",), brokers=None, clock=None, **risk):
    fake = FakeBroker()
    for s in symbols:
        fake.prices[s] = 100.0
        fake.candles[s] = make_candles(datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc))
    brokers = brokers or {"fake": fake}
    clock = clock or Clock()
    wl = [WatchItem(s, "fake") for s in symbols]
    bot = TradingBot(mode, strategy(**risk), wl, store, broker_factory=lambda k: brokers[k],
                     poll_seconds=1, clock=clock, trade_on_first_candle=True)
    return bot, fake, clock


CLOSED = datetime(2026, 1, 5, 9, 59, tzinfo=timezone.utc)  # newest closed 1m candle at T0
FORMING = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)


def pos_of(bot, symbol):
    ps = bot.journal.positions_for(symbol)
    return ps[0] if ps else None


def run(coro):
    return asyncio.run(coro)


def test_entry_buys_and_sizing(store, sig):
    bot, fake, _ = setup(store, position_size_pct=10)
    sig.entry.add(pd.Timestamp(CLOSED))
    run(bot.run_once())
    pos = pos_of(bot, "BTC/USDT")
    assert pos.entry_price == 100.0
    assert 9.9 < pos.quantity < 10.0  # ~10% of 10000 incl. fee/slippage buffer
    assert fake.orders[0].side == Side.BUY
    st = bot.status()
    assert st["positions"][0]["stop_levels"]["take_profit"] == pytest.approx(105.0)
    assert st["equity"] == pytest.approx(fake.cash + pos.quantity * 100.0)
    assert any(e["level"] == "trade" for e in st["events"])


def test_forming_candle_ignored_and_processed_once(store, sig):
    bot, fake, clock = setup(store)
    sig.entry.add(pd.Timestamp(FORMING))
    run(bot.run_once())
    assert not bot.journal.positions and sig.calls == 1
    run(bot.run_once())  # same closed candle -> not processed again
    assert sig.calls == 1
    clock.now = T0 + timedelta(seconds=60)  # 10:00 candle now closed (10:01 forming, not in data)
    run(bot.run_once())
    assert sig.calls == 2
    assert pos_of(bot, "BTC/USDT") is not None


def test_first_candle_is_baseline_by_default(store, sig):
    bot, fake, clock = setup(store)
    bot.trade_on_first_candle = False
    sig.entry.add(pd.Timestamp(CLOSED))
    run(bot.run_once())
    assert sig.calls == 0 and not bot.journal.positions


@pytest.mark.parametrize("price,reason", [(106.0, ExitReason.TAKE_PROFIT), (96.0, ExitReason.STOP_LOSS)])
def test_tp_sl_on_price_tick(store, sig, price, reason):
    bot, fake, _ = setup(store, take_profit_pct=5, stop_loss_pct=3)
    sig.entry.add(pd.Timestamp(CLOSED))
    run(bot.run_once())
    fake.prices["BTC/USDT"] = 101.0
    run(bot.run_once())
    assert bot.journal.positions
    fake.prices["BTC/USDT"] = price
    run(bot.run_once())
    assert not bot.journal.positions
    t = bot.journal.closed_trades[0]
    assert t.exit_reason == reason and t.exit_price == price
    assert t.fees == pytest.approx(t.quantity * 100 * 0.001 + t.quantity * price * 0.001)
    assert fake.holdings["BTC"] == pytest.approx(0.0)


def test_trailing_stop_uses_candle_high(store, sig):
    bot, fake, clock = setup(store, take_profit_pct=None, stop_loss_pct=None, trailing_stop_pct=2)
    sig.entry.add(pd.Timestamp(CLOSED))
    run(bot.run_once())
    df = make_candles(datetime(2026, 1, 5, 10, 1, tzinfo=timezone.utc), n=11)
    df.loc[pd.Timestamp(FORMING), "high"] = 110.0
    fake.candles["BTC/USDT"] = df
    clock.now = T0 + timedelta(seconds=60)
    run(bot.run_once())
    pos = pos_of(bot, "BTC/USDT")
    assert pos.highest_price == 110.0 and pos.bars_held == 1
    fake.prices["BTC/USDT"] = 107.5  # below 110 * 0.98
    run(bot.run_once())
    assert bot.journal.closed_trades[0].exit_reason == ExitReason.TRAILING_STOP


def test_exit_signal(store, sig):
    bot, fake, clock = setup(store)
    sig.entry.add(pd.Timestamp(CLOSED))
    run(bot.run_once())
    sig.exit.add(pd.Timestamp(FORMING))
    fake.candles["BTC/USDT"] = make_candles(datetime(2026, 1, 5, 10, 1, tzinfo=timezone.utc), n=11)
    clock.now = T0 + timedelta(seconds=60)
    run(bot.run_once())
    assert bot.journal.closed_trades[0].exit_reason == ExitReason.SIGNAL


def test_max_open_positions(store, sig):
    bot, fake, _ = setup(store, symbols=("BTC/USDT", "ETH/USDT", "SOL/USDT"), max_open_positions=2)
    sig.entry.add(pd.Timestamp(CLOSED))
    run(bot.run_once())
    assert len(bot.journal.positions) == 2
    assert any("(2) erreicht" in e.message for e in bot.events)


def test_one_symbol_error_does_not_stop_others(store, sig):
    bot, fake, clock = setup(store, symbols=("BTC/USDT", "ETH/USDT"))
    fake.fail.add("BTC/USDT")
    sig.entry.add(pd.Timestamp(CLOSED))
    run(bot.run_once())
    assert [p.symbol for p in bot.journal.positions.values()] == ["ETH/USDT"]
    assert any(e.level == "error" and e.symbol == "BTC/USDT" for e in bot.events)
    # price failures are retried every loop (stop-losses must never wait for a backoff)
    assert "fake:BTC/USDT" not in bot._errors and bot._tick_errors["BTC/USDT"] == 1
    # candle/signal failures use backoff: second error doubles the wait
    fake.fail = set()
    fake.fail_ohlcv.add("BTC/USDT")
    clock.now += timedelta(seconds=60)
    run(bot.run_once())
    assert bot._errors["fake:BTC/USDT"][0] == 1
    clock.now += timedelta(seconds=1)
    run(bot.run_once())
    assert bot._errors["fake:BTC/USDT"][0] == 2
    clock.now += timedelta(seconds=1)  # still within backoff -> skipped
    run(bot.run_once())
    assert bot._errors["fake:BTC/USDT"][0] == 2
    fake.fail_ohlcv.clear()
    clock.now += timedelta(seconds=5)
    run(bot.run_once())
    assert "fake:BTC/USDT" not in bot._errors


def test_journal_persistence_and_restore(store, sig):
    bot, fake, clock = setup(store)
    sig.entry.add(pd.Timestamp(CLOSED))
    run(bot.run_once())
    qty = pos_of(bot, "BTC/USDT").quantity
    # "restart": new bot, same store and broker
    bot2 = TradingBot(TradingMode.TEST, strategy(), bot.watchlist, store,
                      broker_factory=lambda k: fake, clock=clock, trade_on_first_candle=True)
    assert pos_of(bot2, "BTC/USDT").quantity == qty
    calls = sig.calls
    run(bot2.run_once())  # same candle -> must not be processed again
    assert sig.calls == calls and len(fake.orders) == 1
    assert bot2.status()["start_equity"] == pytest.approx(10000.0, rel=1e-3)
    run(bot2.close_position("BTC/USDT"))
    bot3 = TradingBot(TradingMode.TEST, strategy(), bot.watchlist, store,
                      broker_factory=lambda k: fake, clock=clock)
    assert not bot3.journal.positions
    assert bot3.journal.closed_trades[0].exit_reason == ExitReason.MANUAL
    assert len(bot3.journal.equity_history) >= 1
    # offline status via manager
    st = BotManager(store).status(TradingMode.TEST)
    assert st["running"] is False and len(st["closed_trades"]) == 1


def test_manual_buy_now(store, sig):
    bot, fake, _ = setup(store)
    pos = run(bot.buy_now("BTC/USDT", 500.0))
    assert pos.quantity * 100 == pytest.approx(500 / (1.0005 * 1.001), rel=1e-6)
    run(bot.buy_now("BTC/USDT", 500.0))  # manual add-on buy is allowed
    assert len(bot.journal.positions) == 2
    with pytest.raises(BotError):
        run(bot.buy_now("DOGE/USDT", 10.0))  # not in watchlist
    trades = run(bot.close_all())
    assert len(trades) == 2 and not bot.journal.positions


def test_bot_ignores_foreign_holdings(store, sig):
    bot, fake, _ = setup(store)
    fake.holdings["BTC"] = 5.0  # user's own coins
    run(bot.run_once())
    with pytest.raises(BotError):
        run(bot.close_position("BTC/USDT"))
    assert not fake.orders


# ---- LIVE safety -----------------------------------------------------------------------
def enable_live(store, **kw):
    s = store.load()
    s.live_enabled = True
    for k, v in kw.items():
        setattr(s, k, v)
    store.save(s)


def test_live_refused_when_master_switch_off(store, sig):
    bot, fake, _ = setup(store, mode=TradingMode.LIVE)
    with pytest.raises(BotError, match="deaktiviert"):
        run(bot.start())
    assert not bot.running


def test_live_refused_without_credentials(store, sig):
    enable_live(store)
    fake = FakeBroker(cred_fields=["api_key"])
    fake.prices["BTC/USDT"] = 100.0
    bot = TradingBot(TradingMode.LIVE, strategy(), [WatchItem("BTC/USDT", "fake")], store,
                     broker_factory=lambda k: fake)
    with pytest.raises(BotError, match="Zugangsdaten"):
        run(bot.start())
    store.set_credentials("fake", {"api_key": "x"})

    async def go():
        await bot.start()
        assert bot.running
        await bot.stop()
    run(go())
    assert not bot.running


def test_live_refused_for_data_only_broker(store, sig):
    enable_live(store)
    yahoo = FakeBroker(key="yahoo", trading=False)
    bot = TradingBot(TradingMode.LIVE, strategy(), [WatchItem("AAPL", "yahoo")], store,
                     broker_factory=lambda k: yahoo)
    with pytest.raises(BotError, match="Kursdaten"):
        run(bot.start())
    # TEST mode allows data-only brokers
    yahoo.prices["AAPL"] = 100.0
    test_bot = TradingBot(TradingMode.TEST, strategy(), [WatchItem("AAPL", "yahoo")], store,
                          broker_factory=lambda k: yahoo)

    async def go():
        await test_bot.start()
        await test_bot.stop()
    run(go())


def test_live_refused_for_invalid_strategy(store, sig, monkeypatch):
    enable_live(store)
    monkeypatch.setattr(bot_mod, "_validate", lambda s: ["Keine Einstiegsbedingung"])
    bot, fake, _ = setup(store, mode=TradingMode.LIVE)
    with pytest.raises(BotError, match="ungültig"):
        run(bot.start())


def test_live_order_cap_and_position_limit(store, sig):
    enable_live(store, live_max_order_value=100.0, live_max_open_positions=1)
    bot, fake, _ = setup(store, mode=TradingMode.LIVE, symbols=("BTC/USDT", "ETH/USDT"),
                         position_size_pct=50, max_open_positions=5)
    assert bot.max_open_positions == 1
    sig.entry.add(pd.Timestamp(CLOSED))
    run(bot.run_once())
    assert len(fake.orders) == 1
    o = fake.orders[0]
    assert 99.0 < o.quantity * 100.0 <= 100.0


def test_live_sell_limited_to_free_balance(store, sig):
    enable_live(store)
    bot, fake, _ = setup(store, mode=TradingMode.LIVE)
    run(bot.buy_now("BTC/USDT", 50))
    fake.holdings["BTC"] *= 0.999  # fee taken in base asset
    run(bot.close_position("BTC/USDT"))
    assert fake.orders[-1].quantity == pytest.approx(bot.journal.closed_trades[0].quantity)
    assert fake.holdings["BTC"] == pytest.approx(0.0)


def test_kill_switch(store, sig):
    enable_live(store, live_daily_loss_limit_pct=5.0, live_max_order_value=100000.0)
    bot, fake, clock = setup(store, mode=TradingMode.LIVE, position_size_pct=50, stop_loss_pct=None)
    sig.entry.add(pd.Timestamp(CLOSED))

    async def go():
        await bot.start()
        await asyncio.sleep(0.05)  # first iteration: buys ~50 % and sets the daily baseline
        assert bot.running and bot.journal.positions
        fake.cash -= 600  # money moved out by the user: NOT a bot loss -> no kill switch
        clock.now += timedelta(seconds=1)
        bot._wake.set()
        await asyncio.sleep(0.05)
        assert bot.running
        fake.prices["BTC/USDT"] = 85.0  # bot position -15 % of ~5000 = -750 (~7.5 %)
        clock.now += timedelta(seconds=1)
        bot._wake.set()
        for _ in range(100):
            if not bot.running:
                break
            await asyncio.sleep(0.01)
        assert not bot.running
        await bot.stop()
    run(go())
    assert any("NOTSCHALTER" in e.message for e in bot.events)
    assert any(e.kind == "kill_switch" for e in bot.events)
    assert bot.status()["kill_switch"] is True
    bot2, _, _ = setup(store, mode=TradingMode.LIVE, clock=clock)
    with pytest.raises(BotError, match="Notschalter"):
        run(bot2.start())


def test_manager_one_bot_per_mode(store, sig):
    fake = FakeBroker()
    fake.prices["BTC/USDT"] = 100.0
    fake.candles["BTC/USDT"] = make_candles(FORMING)
    mgr = BotManager(store)

    async def go():
        await mgr.start(TradingMode.TEST, strategy(), [WatchItem("BTC/USDT", "fake")],
                        broker_factory=lambda k: fake)
        with pytest.raises(BotError):
            await mgr.start(TradingMode.TEST, strategy(), [WatchItem("BTC/USDT", "fake")],
                            broker_factory=lambda k: fake)
        assert mgr.status(TradingMode.TEST)["running"] is True
        await mgr.stop_all()
        assert mgr.status(TradingMode.TEST)["running"] is False
    run(go())


def test_reset_paper_refused_while_running(store, sig):
    bot, fake, _ = setup(store)

    async def go():
        await bot.start()
        with pytest.raises(BotError):
            bot.reset_paper(5000)
        await bot.stop()
    run(go())
    bot.reset_paper(5000)
    assert bot.journal.start_equity == 5000 and not bot.journal.positions


# ---- integration with the real paper broker / strategy engine ---------------------------
def test_default_factory_paper_mode(store, sig, monkeypatch):
    import tradebot.brokers as brokers_pkg

    data = FakeBroker(key="binance")
    data.prices["BTC/USDT"] = 100.0
    data.candles["BTC/USDT"] = make_candles(FORMING)
    created = []

    def fake_create(key, credentials=None, options=None):
        created.append((key, credentials, options))
        return data

    monkeypatch.setattr(brokers_pkg, "create_broker", fake_create)
    clock = Clock()
    bot = TradingBot(TradingMode.TEST, strategy(position_size_pct=20), [WatchItem("BTC/USDT", "binance")],
                     store, clock=clock, trade_on_first_candle=True)
    sig.entry.add(pd.Timestamp(CLOSED))
    run(bot.run_once())
    assert created and created[0][0] == "binance"
    assert not data.orders  # nothing sent to the real (data) broker
    acc = bot._paper_account
    assert acc.cash < 10000 and acc.holdings
    assert (store.state_dir / "paper_account.json").exists()
    data.prices["BTC/USDT"] = 110.0
    run(bot.run_once())
    assert bot.journal.closed_trades[0].exit_reason == ExitReason.TAKE_PROFIT
    assert acc.cash > 10000
    bot.reset_paper(2000)
    assert acc.cash == 2000 and not bot.journal.closed_trades


def test_real_engine_smoke(store, monkeypatch):
    import numpy as np

    from tradebot.strategy.presets import PRESETS

    st = PRESETS[0]
    fake = FakeBroker()
    n = 400
    tf = bot_mod.TIMEFRAMES[st.timeframe]
    last_open = datetime(2026, 1, 5, tzinfo=timezone.utc)
    idx = pd.DatetimeIndex([last_open - timedelta(seconds=tf * i) for i in reversed(range(n))])
    close = 100 * np.exp(np.cumsum(np.random.default_rng(1).normal(0, 0.01, n)))
    fake.candles["BTC/USDT"] = pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                                             "close": close, "volume": 1.0}, index=idx)
    fake.prices["BTC/USDT"] = float(close[-1])
    clock = Clock(last_open + timedelta(seconds=tf + 1))
    bot = TradingBot(TradingMode.TEST, st, [WatchItem("BTC/USDT", "fake")], store,
                     broker_factory=lambda k: fake, clock=clock, trade_on_first_candle=True)
    run(bot.run_once())
    assert not any(e.level == "error" for e in bot.events), [e.message for e in bot.events]
