"""Engine features: order reconciliation, multiple positions per symbol, loss-streak pause,
minimum profit, wallet / manual actions without running bot, external closes, event kinds."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from tests.test_bot import (  # noqa: F401 - fixtures sig/store are used by pytest
    CLOSED, FORMING, T0, Clock, FakeBroker, make_candles, pos_of, run, sig, store, strategy,
)
from tradebot.brokers.base import BrokerError, NotSupportedError
from tradebot.core.models import (
    ExitReason, Order, OrderStatus, OrderType, Side, TradingMode, WatchItem,
)
from tradebot.engine.bot import BotError, TradingBot
from tradebot.engine.journal import TradeJournal
from tradebot.engine.manager import BotManager


class OrderBroker(FakeBroker):
    """Orders stay pending; fetch_order reports according to ``behaviour``:
    "fill" -> FILLED on first poll, "partial" -> 50 % filled, "never" -> nothing filled,
    "unsupported" -> fetch_order raises NotSupportedError."""

    def __init__(self, behaviour="fill", **kw):
        super().__init__(**kw)
        self.behaviour = behaviour
        self.pending: dict[str, Order] = {}
        self.cancelled: list[str] = []
        self.placed: list[tuple] = []

    def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None):
        self.placed.append((symbol, side, quantity, order_type, price))
        o = Order(symbol, side, quantity, order_type=order_type, price=price,
                  status=OrderStatus.NEW, filled_quantity=0.0)
        self.pending[o.id] = o
        return o

    def _apply(self, o: Order, qty: float):
        p = o.price or self.prices[o.symbol]
        base = o.symbol.split("/")[0]
        sign = 1 if o.side == Side.BUY else -1
        self.cash -= sign * qty * p
        self.holdings[base] = self.holdings.get(base, 0.0) + sign * qty

    def fetch_order(self, order_id, symbol):
        if self.behaviour == "unsupported":
            raise NotSupportedError("nicht unterstützt")
        o = self.pending[order_id]
        if o.status in (OrderStatus.FILLED, OrderStatus.CANCELED):
            return o
        target = {"fill": o.quantity, "partial": o.quantity / 2, "never": 0.0}[self.behaviour]
        if target > o.filled_quantity:
            self._apply(o, target - o.filled_quantity)
            o.filled_quantity = target
        o.status = OrderStatus.FILLED if target >= o.quantity else (
            OrderStatus.PARTIALLY_FILLED if target else OrderStatus.NEW)
        o.fee = o.filled_quantity * (o.price or 0) * 0.001
        return o

    def cancel_order(self, order_id, symbol):
        self.cancelled.append(order_id)
        self.pending[order_id].status = OrderStatus.CANCELED


def make_bot(store, broker, mode=TradingMode.TEST, symbols=("BTC/USDT",), clock=None, **risk):
    for s in symbols:
        broker.prices.setdefault(s, 100.0)
        broker.candles.setdefault(s, make_candles(FORMING))
    clock = clock or Clock()
    bot = TradingBot(mode, strategy(**risk), [WatchItem(s, "fake") for s in symbols], store,
                     broker_factory=lambda k: broker, poll_seconds=1, clock=clock,
                     trade_on_first_candle=True)
    bot.order_poll_seconds = 0.0
    return bot, clock


def set_timeout(store, seconds):
    s = store.load()
    s.order_timeout_seconds = seconds
    store.save(s)


def next_candle(broker, clock, sym="BTC/USDT", minutes=1):
    """Advance one minute: the previously forming candle is now closed."""
    clock.now += timedelta(minutes=minutes)
    last_open = FORMING + timedelta(minutes=round((clock.now - T0).total_seconds() / 60))
    broker.candles[sym] = make_candles(last_open, n=10 + minutes)
    return pd.Timestamp(last_open - timedelta(minutes=1))  # newest closed candle


# ---- order types / reconciliation ------------------------------------------------------
def test_limit_buy_filled_after_poll(store, sig):
    b = OrderBroker("fill")
    bot, _ = make_bot(store, b, buy_order_type="limit", buy_price_multiple=1.002)
    sig.entry.add(pd.Timestamp(CLOSED))
    run(bot.run_once())
    sym, side, qty, otype, price = b.placed[0]
    assert otype == OrderType.LIMIT and price == pytest.approx(100.2)
    pos = pos_of(bot, "BTC/USDT")
    assert pos.quantity == pytest.approx(qty) and pos.entry_price == pytest.approx(100.2)
    assert not b.cancelled


def test_limit_partial_fill_then_cancel(store, sig):
    set_timeout(store, 0)
    b = OrderBroker("partial")
    bot, _ = make_bot(store, b, buy_order_type="limit")
    sig.entry.add(pd.Timestamp(CLOSED))
    run(bot.run_once())
    qty = b.placed[0][2]
    assert b.cancelled
    assert pos_of(bot, "BTC/USDT").quantity == pytest.approx(qty / 2)
    assert any("Rest storniert" in e.message for e in bot.events)


def test_limit_never_filled_no_position(store, sig):
    set_timeout(store, 0)
    b = OrderBroker("never")
    bot, _ = make_bot(store, b, buy_order_type="limit")
    sig.entry.add(pd.Timestamp(CLOSED))
    run(bot.run_once())
    assert b.cancelled and not bot.journal.positions
    assert any("nicht ausgeführt" in e.message for e in bot.events)


def test_limit_sell_unfilled_keeps_position(store, sig):
    b = FakeBroker()
    bot, _ = make_bot(store, b, sell_order_type="limit")
    sig.entry.add(pd.Timestamp(CLOSED))
    run(bot.run_once())
    pos = pos_of(bot, "BTC/USDT")
    ob = OrderBroker("never")
    ob.prices, ob.candles, ob.holdings = b.prices, b.candles, b.holdings
    bot._brokers["fake"] = ob
    set_timeout(store, 0)
    bot.settings = store.load()
    with pytest.raises(BotError):
        run(bot.sell_position(pos.id))
    assert ob.placed[0][3] == OrderType.LIMIT and ob.placed[0][4] == pytest.approx(99.9)
    assert pos.id in bot.journal.positions


def test_market_pending_without_fetch_order_assumed_filled(store, sig):
    b = OrderBroker("unsupported")
    bot, _ = make_bot(store, b)
    sig.entry.add(pd.Timestamp(CLOSED))
    run(bot.run_once())
    assert pos_of(bot, "BTC/USDT").quantity == pytest.approx(b.placed[0][2])
    assert any("angenommen" in e.message for e in bot.events)


def test_market_pending_reconciled_via_fetch_order(store, sig):
    b = OrderBroker("partial")
    set_timeout(store, 0)
    bot, _ = make_bot(store, b)
    sig.entry.add(pd.Timestamp(CLOSED))
    run(bot.run_once())
    assert pos_of(bot, "BTC/USDT").quantity == pytest.approx(b.placed[0][2] / 2)


# ---- multiple positions per symbol / rules -----------------------------------------------
def test_multiple_positions_per_symbol_and_per_position_exits(store, sig):
    b = FakeBroker()
    bot, clock = make_bot(store, b, max_positions_per_symbol=2, add_position_min_drop_pct=3,
                          take_profit_pct=5, stop_loss_pct=20)
    sig.entry.add(pd.Timestamp(CLOSED))
    run(bot.run_once())
    assert len(bot.journal.positions) == 1
    # entry again at 99 -> blocked (needs -3 %)
    b.prices["BTC/USDT"] = 99.0
    sig.entry.add(next_candle(b, clock))
    run(bot.run_once())
    run(bot.run_once())
    assert len(bot.journal.positions) == 1
    blocked = [e for e in bot.events if "Nachkauf erst unter" in e.message]
    assert len(blocked) == 1  # no spam
    b.prices["BTC/USDT"] = 96.0
    sig.entry.add(next_candle(b, clock))
    run(bot.run_once())
    ps = bot.journal.positions_for("BTC/USDT")
    assert [p.entry_price for p in ps] == [100.0, 96.0]
    # TP of the second position only (96 * 1.05 = 100.8)
    b.prices["BTC/USDT"] = 101.0
    run(bot.run_once())
    ps = bot.journal.positions_for("BTC/USDT")
    assert [p.entry_price for p in ps] == [100.0]
    assert bot.journal.closed_trades[-1].exit_reason == ExitReason.TAKE_PROFIT
    assert bot.journal.state("BTC/USDT").last_entry_price == 100.0
    st = bot.status()
    assert {"id", "estimated_net_proceeds", "estimated_net_pnl", "estimated_net_pnl_pct",
            "value", "stop_levels"} <= set(st["positions"][0])


def test_old_journal_format_is_migrated(store):
    path = store.state_dir / "journal_test.json"
    path.write_text(json.dumps({
        "positions": {"BTC/USDT": {"symbol": "BTC/USDT", "quantity": 1.0, "entry_price": 100.0,
                                   "entry_time": T0.isoformat(), "highest_price": 101.0,
                                   "bars_held": 2, "strategy": "Test", "id": "abc123"}},
        "position_brokers": {"BTC/USDT": "fake"},
        "entry_fees": {"abc123": 0.1},
    }))
    j = TradeJournal(path)
    assert list(j.positions) == ["abc123"]
    assert j.position_brokers == {"abc123": "fake"}
    j.save()
    assert json.loads(path.read_text())["version"] == 2


def test_loss_streak_pause_and_resume(store, sig):
    b = FakeBroker()
    bot, clock = make_bot(store, b, max_loss_streak=1, loss_streak_pause_hours=2, stop_loss_pct=3)
    sig.entry.add(pd.Timestamp(CLOSED))
    run(bot.run_once())
    b.prices["BTC/USDT"] = 95.0
    run(bot.run_once())
    assert bot.journal.closed_trades[0].exit_reason == ExitReason.STOP_LOSS
    paused = [e for e in bot.events if e.kind == "paused"]
    assert paused and "pausiert nach 1 Verlusten" in paused[0].message
    st = bot.status()["symbol_states"]["BTC/USDT"]
    assert st["paused"] is True and st["paused_until"]
    sig.entry.add(next_candle(b, clock))
    run(bot.run_once())
    assert not bot.journal.positions
    bot.resume_symbol("BTC/USDT")
    sig.entry.add(next_candle(b, clock))
    run(bot.run_once())
    assert len(bot.journal.positions) == 1


def test_signal_exit_respects_min_profit(store, sig):
    b = FakeBroker()
    bot, clock = make_bot(store, b, min_profit_pct=2, take_profit_pct=None)
    sig.entry.add(pd.Timestamp(CLOSED))
    run(bot.run_once())
    b.prices["BTC/USDT"] = 100.5
    sig.exit.add(next_candle(b, clock))
    run(bot.run_once())
    assert bot.journal.positions
    assert any("Mindestgewinn" in e.message for e in bot.events)
    b.prices["BTC/USDT"] = 103.0
    sig.exit.add(next_candle(b, clock))
    run(bot.run_once())
    assert not bot.journal.positions
    assert bot.journal.closed_trades[0].exit_reason == ExitReason.SIGNAL


def test_fixed_size_and_cash_reserve(store, sig):
    b = FakeBroker(cash=1000.0)
    bot, _ = make_bot(store, b, position_size_mode="fixed", position_size_fixed=200, cash_reserve=900)
    sig.entry.add(pd.Timestamp(CLOSED))
    run(bot.run_once())
    # budget = min(fixed 200, cash 1000 - reserve 900) = 100 (incl. fees)
    pos = pos_of(bot, "BTC/USDT")
    assert pos.quantity * 100 == pytest.approx(100 / (1.0005 * 1.001))


def test_event_kinds_and_data(store, sig):
    b = FakeBroker()
    bot, _ = make_bot(store, b)
    got = []
    bot.subscribe(got.append)
    sig.entry.add(pd.Timestamp(CLOSED))
    run(bot.run_once())
    b.prices["BTC/USDT"] = 106.0
    run(bot.run_once())
    kinds = [e.kind for e in got]
    assert "buy" in kinds and "take_profit" in kinds
    tp = next(e for e in got if e.kind == "take_profit")
    assert tp.data["pnl"] > 0 and tp.data["symbol"] == "BTC/USDT" and tp.data["qty"] > 0
    assert tp.to_dict()["kind"] == "take_profit"


# ---- manager: manual actions & wallet without running bot ---------------------------------
def test_manager_manual_actions_without_bot(store, sig):
    b = FakeBroker()
    b.prices["BTC/USDT"] = 100.0
    mgr = BotManager(store, broker_factory=lambda k: b)
    pos = run(mgr.manual_buy(TradingMode.TEST, "fake", "BTC/USDT", 300))
    assert pos.quantity > 0
    b.prices["BTC/USDT"] = 110.0
    w = run(mgr.wallet(TradingMode.TEST))
    assert w["positions"][0]["current_price"] == 110.0
    assert w["totals"]["value"] == pytest.approx(pos.quantity * 110)
    assert w["cash"] == pytest.approx(b.cash) and "broker_holdings" not in w
    st = mgr.status(TradingMode.TEST)
    assert len(st["positions"]) == 1 and st["running"] is False
    trade = run(mgr.manual_sell(TradingMode.TEST, pos.id, store))
    assert trade.exit_reason == ExitReason.MANUAL and trade.pnl > 0
    assert mgr.status(TradingMode.TEST)["positions"] == []
    with pytest.raises(BotError):
        run(mgr.manual_sell(TradingMode.TEST, "unknown", store))


def test_manager_manual_sell_delegates_to_running_bot(store, sig):
    b = FakeBroker()
    b.prices["BTC/USDT"] = 100.0
    b.candles["BTC/USDT"] = make_candles(FORMING)
    mgr = BotManager(store, broker_factory=lambda k: b)

    async def go():
        bot = await mgr.start(TradingMode.TEST, strategy(), [WatchItem("BTC/USDT", "fake")])
        pos = await mgr.manual_buy(TradingMode.TEST, "fake", "BTC/USDT", 100)
        assert pos.id in bot.journal.positions
        await mgr.manual_sell(TradingMode.TEST, pos.id)
        assert not bot.journal.positions
        await mgr.stop_all()
    run(go())


def test_live_manual_requires_switch_and_credentials(store, sig):
    b = FakeBroker(cred_fields=["api_key"])
    b.prices["BTC/USDT"] = 100.0
    mgr = BotManager(store, broker_factory=lambda k: b)
    with pytest.raises(BotError, match="deaktiviert"):
        run(mgr.manual_buy(TradingMode.LIVE, "fake", "BTC/USDT", 50))
    s = store.load()
    s.live_enabled = True
    store.save(s)
    with pytest.raises(BotError, match="Zugangsdaten"):
        run(mgr.manual_buy(TradingMode.LIVE, "fake", "BTC/USDT", 50))
    store.set_credentials("fake", {"api_key": "k"})
    pos = run(mgr.manual_buy(TradingMode.LIVE, "fake", "BTC/USDT", 500))
    assert pos.quantity * 100 <= 100.0  # live hard cap


def test_live_wallet_broker_holdings(store, sig):
    s = store.load()
    s.live_enabled = True
    s.broker_options["broken"] = {}
    store.save(s)
    store.set_credentials("fake", {"api_key": "k"})
    store.set_credentials("broken", {"api_key": "k"})
    b = FakeBroker()
    b.prices["BTC/USDT"] = 100.0
    b.holdings = {"ETH": 2.0}

    class Broken(FakeBroker):
        def fetch_balance(self):
            raise BrokerError("Zugriff verweigert")

    brokers = {"fake": b, "broken": Broken()}
    mgr = BotManager(store, broker_factory=lambda k: brokers[k])
    pos = run(mgr.manual_buy(TradingMode.LIVE, "fake", "BTC/USDT", 50))
    w = run(mgr.wallet(TradingMode.LIVE))
    hold = {h["asset"]: h for h in w["broker_holdings"]["fake"]["holdings"]}
    assert "BTC" not in hold  # fully managed by the bot
    assert hold["ETH"]["quantity"] == 2.0
    assert "USDT" in hold
    assert w["broker_holdings"]["broken"]["error"] == "Zugriff verweigert"
    assert w["positions"][0]["id"] == pos.id


def test_live_external_close_and_forget(store, sig):
    s = store.load()
    s.live_enabled = True
    store.save(s)
    b = FakeBroker()
    bot, clock = make_bot(store, b, mode=TradingMode.LIVE)
    pos = run(bot.buy_now("BTC/USDT", 50))
    b.holdings["BTC"] = 0.0  # user sold it on the exchange
    b.prices["BTC/USDT"] = 90.0  # stop loss triggers
    run(bot.run_once())
    assert pos.id in bot.journal.external_closed
    assert any(e.kind == "external_closed" for e in bot.events)
    n_orders = len(b.orders)
    run(bot.run_once())  # no retry spam / no further orders
    assert len(b.orders) == n_orders
    assert bot.status()["positions"][0]["external_closed"] is True
    with pytest.raises(BotError):
        run(bot.sell_position(pos.id))
    mgr = BotManager(store, broker_factory=lambda k: b)
    run(mgr.forget_position(TradingMode.LIVE, pos.id))
    assert not TradeJournal(store.state_dir / "journal_live.json").positions


def test_manager_resume_symbol_offline(store):
    j = TradeJournal(store.state_dir / "journal_test.json")
    st = j.state("BTC/USDT")
    st.paused_indefinitely = True
    j.save()
    BotManager(store, broker_factory=lambda k: FakeBroker()).resume_symbol(TradingMode.TEST, "BTC/USDT")
    assert TradeJournal(store.state_dir / "journal_test.json").state("BTC/USDT").paused_indefinitely is False
