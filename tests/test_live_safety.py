"""Regression tests for the live-trading safety review (each reproduced a confirmed bug)."""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pandas as pd
import pytest

import tradebot.engine.bot as bot_mod
from tests.test_bot import (  # noqa: F401 - fixtures sig/store are used by pytest
    CLOSED, FORMING, Clock, FakeBroker, make_candles, run, sig, store, strategy,
)
from tradebot.brokers.base import (
    BrokerError, InsufficientFundsError, InvalidOrderError, NetworkError, order_fee_base,
)
from tradebot.brokers.ccxt_broker import BinanceBroker, KrakenBroker
from tradebot.core.models import (
    ExitReason, Order, OrderStatus, OrderType, Position, Side, TradingMode, WatchItem,
)
from tradebot.core.settings import CredentialsError, SettingsStore
from tradebot.engine.bot import BotError, TradingBot
from tradebot.engine.journal import TradeJournal
from tradebot.engine.manager import BotManager


class LiveFake(FakeBroker):
    """Like ccxt: zero balances omitted; optional delays / lost responses / fee in base."""

    def __init__(self, client_ids=False, **kw):
        super().__init__(cred_fields=("api_key",), **kw)
        self.delay = 0.0
        self.raise_after_fill = False  # order executed, response lost
        self.raise_before_fill = False  # request failed, nothing executed
        self.fee_in_base = False
        self.reject_qty_below = 0.0
        self.client_ids = client_ids
        self.by_cid: dict[str, Order] = {}
        self.lookups = 0

    def fetch_balance(self):
        return {k: v for k, v in {self.quote: self.cash, **self.holdings}.items() if v}

    def _place(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None, cid=None):
        import time
        if quantity < self.reject_qty_below:
            raise InvalidOrderError("below LOT_SIZE")
        if self.raise_before_fill:
            raise NetworkError("connect timeout")
        base = symbol.split("/")[0]
        if side == Side.SELL and self.holdings.get(base, 0.0) < quantity - 1e-12:
            raise InsufficientFundsError("insufficient balance")
        if self.delay:
            time.sleep(self.delay)
        p = self.prices[symbol]
        fee = quantity * 0.001 * p
        if side == Side.BUY:
            self.cash -= quantity * p
            got = quantity * (0.999 if self.fee_in_base else 1.0)
            self.holdings[base] = self.holdings.get(base, 0.0) + got
        else:
            self.cash += quantity * p - fee
            self.holdings[base] = self.holdings.get(base, 0.0) - quantity
        o = Order(symbol, side, quantity, price=p, status=OrderStatus.FILLED, filled_quantity=quantity, fee=fee)
        self.orders.append(o)
        if cid:
            self.by_cid[cid] = o
        if self.raise_after_fill:
            raise NetworkError("read timeout")
        return o

    def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None):
        return self._place(symbol, side, quantity, order_type, price)


class LiveFakeCid(LiveFake):
    """Supports client order ids + lookup."""

    def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None,
                    client_order_id=None):
        return self._place(symbol, side, quantity, order_type, price, client_order_id)

    def fetch_order_by_client_id(self, client_order_id, symbol):
        self.lookups += 1
        return self.by_cid.get(client_order_id)


def live_store(store, **kw):
    s = store.load()
    s.live_enabled = True
    for k, v in kw.items():
        setattr(s, k, v)
    store.save(s)
    store.set_credentials("fake", {"api_key": "x"})


def mk(store, fake, mode=TradingMode.LIVE, **risk):
    fake.prices.setdefault("BTC/USDT", 100.0)
    fake.candles["BTC/USDT"] = make_candles(datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc))
    return TradingBot(mode, strategy(**risk), [WatchItem("BTC/USDT", "fake")], store,
                      broker_factory=lambda k: fake, poll_seconds=1, clock=Clock(), trade_on_first_candle=True)


def seed_position(bot, qty=1.0, price=100.0, env=None, entry=datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)):
    pos = Position(symbol="BTC/USDT", quantity=qty, entry_price=price, entry_time=entry, strategy="Test")
    bot.journal.open_position(pos, "fake", 0.1, env=env)
    bot.journal.save()
    return pos


def sells(fake):
    return [o for o in fake.orders if o.side == Side.SELL]


# ---- 1. concurrent manual sells without running bot ------------------------------------
def test_double_manual_sell_without_running_bot_sells_once(store, sig):
    live_store(store)
    fake = LiveFake()
    fake.holdings["BTC"] = 6.0  # 1 bot + 5 user-owned
    fake.delay = 0.2
    pos = seed_position(mk(store, fake))
    mgr = BotManager(store, broker_factory=lambda k: fake)

    async def go():
        return await asyncio.gather(mgr.manual_sell(TradingMode.LIVE, pos.id),
                                    mgr.manual_sell(TradingMode.LIVE, pos.id), return_exceptions=True)
    res = run(go())
    assert len(sells(fake)) == 1
    assert fake.holdings["BTC"] == pytest.approx(5.0)
    assert sum(isinstance(r, BotError) for r in res) == 1
    assert mgr._ensure_bot(TradingMode.LIVE) is mgr._ensure_bot(TradingMode.LIVE)


def test_manual_buys_respect_max_positions_concurrently(store, sig):
    live_store(store, live_max_open_positions=1, live_max_order_value=1000.0)
    fake = LiveFake()
    fake.prices["BTC/USDT"] = 100.0
    fake.delay = 0.1
    mgr = BotManager(store, broker_factory=lambda k: fake)

    async def go():
        return await asyncio.gather(mgr.manual_buy(TradingMode.LIVE, "fake", "BTC/USDT", 50),
                                    mgr.manual_buy(TradingMode.LIVE, "fake", "BTC/USDT", 50),
                                    return_exceptions=True)
    res = run(go())
    assert len([o for o in fake.orders if o.side == Side.BUY]) == 1
    assert sum(isinstance(r, BotError) for r in res) == 1


# ---- 2. manual action while the bot is stopping ---------------------------------------
def test_manual_sell_during_stop_does_not_resurrect_position(store, sig):
    live_store(store)
    fake = LiveFake()
    fake.holdings["BTC"] = 6.0
    b = mk(store, fake)
    pos = seed_position(b)
    mgr = BotManager(store, broker_factory=lambda k: fake)

    async def go():
        b2 = await mgr.start(TradingMode.LIVE, b.strategy, b.watchlist)
        await asyncio.sleep(0.05)
        gate = asyncio.Event()

        async def slow_iteration():  # an iteration still in progress (e.g. waiting on an order)
            async with b2._get_lock():
                await gate.wait()
        b2._task.cancel()
        b2._task = asyncio.create_task(slow_iteration())
        await asyncio.sleep(0)
        stop = asyncio.create_task(mgr.stop(TradingMode.LIVE))
        await asyncio.sleep(0.01)
        sell = asyncio.create_task(mgr.manual_sell(TradingMode.LIVE, pos.id))
        await asyncio.sleep(0.01)
        assert mgr._ensure_bot(TradingMode.LIVE) is b2  # one object per mode
        gate.set()
        await stop
        await sell
    run(go())
    j = TradeJournal(b.journal.path)
    assert pos.id not in j.positions
    assert len(sells(fake)) == 1 and fake.holdings["BTC"] == pytest.approx(5.0)


# ---- 3. master switch / limits re-read every loop ---------------------------------------
def test_live_switch_off_stops_running_bot_without_orders(store, sig):
    live_store(store)
    fake = LiveFake()
    b = mk(store, fake)

    async def go():
        await b.start()
        await asyncio.sleep(0.02)
        s = store.load()
        s.live_enabled = False
        store.save(s)
        sig.entry.add(CLOSED)
        await b.run_once()
        await b.stop()
    run(go())
    assert not any(o.side == Side.BUY for o in fake.orders)
    assert not b.running
    assert any("deaktiviert" in e.message and e.level == "error" for e in b.events)


def test_lowered_live_limits_apply_without_restart(store, sig):
    live_store(store, live_max_order_value=1000.0)
    fake = LiveFake()
    b = mk(store, fake, position_size_pct=50)
    s = store.load()
    s.live_max_order_value = 20.0
    store.save(s)
    sig.entry.add(CLOSED)
    run(b.run_once())
    buy = next(o for o in fake.orders if o.side == Side.BUY)
    assert buy.quantity * 100 <= 20.0 + 1e-9


# ---- 4. fee charged in base currency ----------------------------------------------------
def test_fee_in_base_never_sells_user_holdings(store, sig):
    live_store(store)
    fake = LiveFake()
    fake.fee_in_base = True
    fake.holdings["BTC"] = 5.0  # user-owned
    b = mk(store, fake)
    sig.entry.add(CLOSED)
    run(b.run_once())
    p = next(iter(b.journal.positions.values()))
    received = fake.holdings["BTC"] - 5.0
    assert p.quantity == pytest.approx(received)
    run(b.sell_position(p.id))
    assert fake.holdings["BTC"] == pytest.approx(5.0)


def test_fee_base_from_order_info_reduces_position(store, sig):
    class FeeBase(FakeBroker):
        def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None):
            o = super().place_order(symbol, side, quantity, order_type, price)
            if o.side == Side.BUY:
                o.raw = {"fee_base": o.quantity * 0.001}
            return o
    fake = FeeBase()
    b = mk(store, fake, mode=TradingMode.TEST)
    sig.entry.add(CLOSED)
    run(b.run_once())
    buy = fake.orders[0]
    p = next(iter(b.journal.positions.values()))
    assert p.quantity == pytest.approx(buy.quantity * 0.999)


# ---- 5. lost order responses -------------------------------------------------------------
def test_sell_response_lost_is_not_sold_twice(store, sig):
    live_store(store)
    fake = LiveFake()
    fake.holdings["BTC"] = 6.0
    b = mk(store, fake, stop_loss_pct=5.0)
    pos = seed_position(b)
    fake.prices["BTC/USDT"] = 90.0
    fake.raise_after_fill = True
    run(b.run_once())
    fake.raise_after_fill = False
    b.clock.now += timedelta(minutes=5)
    run(b.run_once())
    assert len(sells(fake)) == 1 and fake.holdings["BTC"] == pytest.approx(5.0)
    assert pos.id not in b.journal.positions
    assert b.journal.closed_trades[-1].exit_reason == ExitReason.STOP_LOSS
    assert not b.journal.pending_orders


def test_buy_response_lost_still_records_position(store, sig):
    live_store(store)
    fake = LiveFake()
    b = mk(store, fake)
    sig.entry.add(CLOSED)
    fake.raise_after_fill = True
    run(b.run_once())
    assert len(b.journal.positions) == 1
    p = next(iter(b.journal.positions.values()))
    assert p.quantity == pytest.approx(fake.holdings["BTC"])
    assert not b.journal.pending_orders


def test_lost_response_found_via_client_order_id(store, sig):
    live_store(store)
    fake = LiveFakeCid()
    b = mk(store, fake, mode=TradingMode.TEST)  # TEST: no balance heuristic -> lookup needed
    sig.entry.add(CLOSED)
    fake.raise_after_fill = True
    run(b.run_once())
    assert fake.lookups == 1 and len(b.journal.positions) == 1
    assert list(fake.by_cid)[0].startswith("tb")


def test_failed_request_with_lookup_none_places_nothing(store, sig):
    live_store(store)
    fake = LiveFakeCid()
    b = mk(store, fake)
    sig.entry.add(CLOSED)
    fake.raise_before_fill = True
    run(b.run_once())
    assert not b.journal.positions and not b.journal.pending_orders
    assert any("Netzwerk" in e.message or "timeout" in e.message for e in b.events)


def test_uncertain_sell_blocks_resell_until_reconciled(store, sig):
    live_store(store)
    fake = LiveFakeCid()
    fake.holdings["BTC"] = 6.0
    b = mk(store, fake, stop_loss_pct=5.0)
    pos = seed_position(b)
    fake.prices["BTC/USDT"] = 90.0
    fake.raise_after_fill = True
    real_lookup = fake.fetch_order_by_client_id
    fake.fetch_order_by_client_id = MagicMock(side_effect=BrokerError("Börse überlastet"))
    fake.delay = 0
    # balance heuristic must not hide this case: user sells the same amount concurrently
    fake.holdings["BTC"] = 6.0
    orig_balance = fake.fetch_balance
    calls = {"n": 0}

    def balance():
        calls["n"] += 1
        bal = orig_balance()
        if calls["n"] >= 2:  # after the order: pretend no change could be seen
            bal["BTC"] = 6.0
        return bal
    fake.fetch_balance = balance
    run(b.run_once())
    assert pos.id in b.journal.positions
    entry = next(iter(b.journal.pending_orders.values()))
    assert entry["uncertain"] is True and entry["position_id"] == pos.id
    assert any(e.kind == "order_uncertain" for e in b.events)
    assert b.status()["positions"][0]["uncertain"] is True
    fake.raise_after_fill = False
    b.clock.now += timedelta(minutes=5)
    run(b.run_once())  # lookup still failing -> no second sell
    assert len(sells(fake)) == 1
    fake.fetch_order_by_client_id = real_lookup
    fake.fetch_balance = orig_balance
    b.clock.now += timedelta(minutes=5)
    run(b.run_once())  # reconciled: the sell did happen
    assert len(sells(fake)) == 1
    assert pos.id not in b.journal.positions and not b.journal.pending_orders


# ---- 6. zero balances omitted by ccxt -----------------------------------------------------
def test_missing_zero_balance_is_detected_as_external_close(store, sig):
    live_store(store)
    fake = LiveFake()
    b = mk(store, fake, stop_loss_pct=5.0, max_positions_per_symbol=3)
    p1 = seed_position(b, qty=1.0)
    p2 = seed_position(b, qty=1.0)
    fake.holdings = {}
    fake.prices["BTC/USDT"] = 90.0
    run(b.run_once())
    assert b.journal.external_closed == {p1.id, p2.id}
    assert not sells(fake)


# ---- 7. unsellable dust / backoff must not block stop-losses -----------------------------
def test_unsellable_dust_does_not_block_other_stoplosses(store, sig):
    live_store(store)
    fake = LiveFake()
    fake.reject_qty_below = 0.01
    b = mk(store, fake, stop_loss_pct=5.0, max_positions_per_symbol=3)
    dust = seed_position(b, qty=0.001, entry=datetime(2026, 1, 5, 8, 0, tzinfo=timezone.utc))
    big = seed_position(b, qty=2.0)
    fake.holdings["BTC"] = 2.001
    fake.prices["BTC/USDT"] = 90.0
    run(b.run_once())
    assert big.id not in b.journal.positions and len(sells(fake)) == 1
    assert dust.id in b.journal.dust
    assert sum(e.kind == "dust" for e in b.events) == 1
    run(b.run_once())
    assert sum(e.kind == "dust" for e in b.events) == 1  # event once
    run(BotManager(store, broker_factory=lambda k: fake).forget_position(TradingMode.LIVE, dust.id))
    assert not TradeJournal(b.journal.path).positions


def test_price_tick_exits_ignore_candle_backoff(store, sig):
    live_store(store)
    fake = LiveFake()
    fake.holdings["BTC"] = 1.0
    b = mk(store, fake, stop_loss_pct=5.0)
    pos = seed_position(b)
    fake.fail_ohlcv.add("BTC/USDT")
    run(b.run_once())
    run(b.run_once())  # candles in backoff now
    assert "fake:BTC/USDT" in b._errors
    fake.prices["BTC/USDT"] = 90.0
    run(b.run_once())
    assert pos.id not in b.journal.positions


# ---- 8. testnet positions vs. real account ---------------------------------------------
def test_positions_of_other_environment_are_never_traded(store, sig):
    live_store(store)
    s = store.load()
    s.broker_options["fake"] = {"sandbox": True}
    store.save(s)
    fake = LiveFake()
    fake.holdings["BTC"] = 1.0
    b = mk(store, fake, stop_loss_pct=5.0)
    pos = run(b.buy_now("BTC/USDT", 50))
    assert b.journal.position_envs[pos.id] == "sandbox"
    s.broker_options["fake"] = {"sandbox": False}  # user switches to the real account
    store.save(s)
    with pytest.raises(BotError, match="Testnet"):
        run(b.start())
    with pytest.raises(BotError, match="Testnet"):
        run(b.sell_position(pos.id))
    fake.prices["BTC/USDT"] = 50.0
    n = len(fake.orders)
    run(b.run_once())
    assert len(fake.orders) == n and pos.id in b.journal.positions
    assert b.status()["positions"][0]["env_mismatch"] is True


# ---- 9. failed cancel after limit timeout ----------------------------------------------
class StuckLimit(LiveFake):
    def __init__(self):
        super().__init__()
        self.open: dict[str, Order] = {}
        self.cancel_ok = False

    def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None):
        o = Order(symbol, side, quantity, order_type=order_type, price=price, status=OrderStatus.NEW)
        self.open[o.id] = o
        self.orders.append(o)
        return o

    def fetch_order(self, order_id, symbol):
        return self.open[order_id]

    def cancel_order(self, order_id, symbol):
        if not self.cancel_ok:
            raise NetworkError("cancel timeout")
        self.open[order_id].status = OrderStatus.CANCELED


def test_failed_cancel_keeps_order_pending_and_blocks_new_orders(store, sig):
    live_store(store, order_timeout_seconds=0)
    fake = StuckLimit()
    b = mk(store, fake, buy_order_type="limit")
    b.order_poll_seconds = 0
    sig.entry.add(CLOSED)
    run(b.run_once())
    assert len(fake.orders) == 1 and not b.journal.positions
    entry = next(iter(b.journal.pending_orders.values()))
    assert entry["open"] is True and entry["order_id"] == fake.orders[0].id
    # next candle with entry signal: no second buy while the first is pending
    b.clock.now += timedelta(minutes=1)
    fake.candles["BTC/USDT"] = make_candles(FORMING + timedelta(minutes=1), n=11)
    sig.entry.add(pd.Timestamp(FORMING))
    run(b.run_once())
    assert len(fake.orders) == 1
    # the order fills later -> reconciled into a position
    o = fake.orders[0]
    o.status, o.filled_quantity, o.price = OrderStatus.FILLED, o.quantity, 100.0
    fake.holdings["BTC"] = o.quantity
    run(b.run_once())
    assert not b.journal.pending_orders
    assert next(iter(b.journal.positions.values())).quantity == pytest.approx(o.quantity)


# ---- 10. kill switch on unreliable data ---------------------------------------------------
def test_kill_switch_skipped_when_balance_fetch_fails(store, sig):
    live_store(store, live_daily_loss_limit_pct=5.0)
    fake = LiveFake()
    fake.holdings["BTC"] = 10.0
    b = mk(store, fake, stop_loss_pct=None, take_profit_pct=None)
    seed_position(b, qty=10.0)
    run(b.run_once())  # baseline
    assert b.journal.daily_start["pnl_mark"] == pytest.approx(0.0, abs=1)
    orig = fake.fetch_balance
    fake.fetch_balance = MagicMock(side_effect=NetworkError("timeout"))
    fake.prices["BTC/USDT"] = 99.0
    run(b.run_once())
    assert not b._killed
    fake.fetch_balance = orig
    fake.prices["BTC/USDT"] = 40.0  # -600 on 10000 -> 6 %
    run(b.run_once())
    assert b._killed


# ---- 11. partial sell after quantity reduced to free balance / credentials -----------------
def test_partial_sell_keeps_actual_remaining_holdings(store, sig):
    class HalfFill(LiveFake):
        def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None):
            o = super().place_order(symbol, side, quantity / 2, order_type, price)
            o.quantity = quantity
            o.status = OrderStatus.CANCELED  # remainder cancelled, final
            return o
    live_store(store)
    fake = HalfFill()
    fake.holdings["BTC"] = 0.8  # fees withheld: only 0.8 of 1.0 really there
    b = mk(store, fake)
    pos = seed_position(b, qty=1.0)
    run(b.sell_position(pos.id))
    assert b.journal.positions[pos.id].quantity == pytest.approx(0.4)
    assert fake.holdings["BTC"] == pytest.approx(0.4)


def test_missing_key_never_overwrites_credentials(tmp_path):
    s = SettingsStore(tmp_path)
    s.set_credentials("binance", {"api_key": "a", "api_secret": "b"})
    before = s.credentials_path.read_bytes()
    os.remove(s.key_path)
    with pytest.raises(CredentialsError, match="Schlüssel"):
        s.set_credentials("kraken", {"api_key": "c"})
    assert s.credentials_path.read_bytes() == before
    assert not s.key_path.exists()
    assert s.has_credentials("binance") is False and "Schlüssel" in s.credentials_error()


# ---- broker side: client order ids / base fees in ccxt -----------------------------------
def _ccxt(cls):
    b = cls(credentials={"api_key": "k", "api_secret": "s"})
    b._exchange = MagicMock()
    b.normalize_quantity = lambda s, q: q
    return b


def test_ccxt_client_order_id_and_fee_base():
    b = _ccxt(BinanceBroker)
    b.exchange.create_order.return_value = {
        "id": "1", "status": "closed", "amount": 1.0, "filled": 1.0, "average": 100.0,
        "fees": [{"cost": 0.001, "currency": "BTC"}]}
    o = b.place_order("BTC/USDT", Side.BUY, 1.0, client_order_id="tbl123")
    assert b.exchange.create_order.call_args.args[-1] == {"newClientOrderId": "tbl123"}
    assert order_fee_base(o) == pytest.approx(0.001) and o.fee == pytest.approx(0.1)
    b.exchange.fetch_order.return_value = {"id": "1", "status": "closed", "amount": 1.0,
                                           "filled": 1.0, "average": 100.0, "side": "buy"}
    found = b.fetch_order_by_client_id("tbl123", "BTC/USDT")
    assert found.id == "1"
    assert b.exchange.fetch_order.call_args.args[2] == {"origClientOrderId": "tbl123"}


def test_kraken_client_order_id_via_userref():
    b = _ccxt(KrakenBroker)
    b.exchange.create_order.return_value = {"id": "K1", "status": "closed", "amount": 1.0,
                                            "filled": 1.0, "average": 100.0}
    b.place_order("BTC/EUR", Side.BUY, 1.0, client_order_id="tbl999")
    ref = b.exchange.create_order.call_args.args[-1]["userref"]
    assert isinstance(ref, int) and 0 <= ref < 2 ** 31
    b.exchange.has = {"fetchOpenOrders": True, "fetchClosedOrders": True}
    b.exchange.fetch_open_orders.return_value = []
    b.exchange.fetch_closed_orders.return_value = [
        {"id": "K1", "status": "closed", "amount": 1.0, "filled": 1.0, "average": 100.0,
         "side": "buy", "info": {"userref": ref}}]
    assert b.fetch_order_by_client_id("tbl999", "BTC/EUR").id == "K1"
    assert b.fetch_order_by_client_id("other", "BTC/EUR") is None
