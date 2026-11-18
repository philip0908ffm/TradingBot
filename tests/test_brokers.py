"""Offline tests for the broker layer (ccxt / alpaca / yfinance are mocked)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import ccxt
import numpy as np
import pandas as pd
import pytest

from tradebot.brokers import (
    BROKERS,
    AlpacaBroker,
    AuthenticationError,
    BadSymbolError,
    BinanceBroker,
    BrokerError,
    InsufficientFundsError,
    InvalidOrderError,
    KrakenBroker,
    NetworkError,
    NotSupportedError,
    PaperAccount,
    PaperBroker,
    RateLimitError,
    YahooBroker,
    broker_infos,
    create_broker,
)
from tradebot.brokers import cache as cache_mod
from tradebot.brokers import yahoo_broker
from tradebot.brokers.base import Broker, BrokerInfo
from tradebot.brokers.ccxt_broker import translate_ccxt_error
from tradebot.core.models import OHLCV_COLUMNS, AssetClass, OrderStatus, OrderType, Side

H = 3600_000  # ms per hour
T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------- helpers / fakes
class FakeExchange:
    """Minimal stand-in for a ccxt exchange producing hourly candles from T0 on."""

    timeframes = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h",
                  "4h": "4h", "1d": "1d", "1w": "1w"}

    def __init__(self, first=T0, last=T0 + timedelta(days=30), overlap=0):
        self.first_ms = int(first.timestamp() * 1000)
        self.last_ms = int(last.timestamp() * 1000)
        self.overlap = overlap  # return `overlap` already-seen candles at the page start
        self.calls = []
        self.markets = {
            "BTC/USDT": {"spot": True, "active": True, "limits": {"cost": {"min": 5.0}}},
            "ETH/USDT": {"spot": True, "active": True, "limits": {"cost": {"min": None}}},
            "OLD/USDT": {"spot": True, "active": False, "limits": {}},
            "BTC/USDT:USDT": {"spot": False, "active": True, "limits": {}},
        }

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.calls.append((since, limit))
        if since is None:
            since = self.last_ms - (limit - 1) * H
        start = max(self.first_ms, since - self.overlap * H)
        start = self.first_ms + ((start - self.first_ms + H - 1) // H) * H
        rows = []
        ts = start
        while ts <= self.last_ms and len(rows) < limit:
            i = (ts - self.first_ms) // H
            rows.append([ts, 100 + i, 101 + i, 99 + i, 100.5 + i, 10.0])
            ts += H
        return rows

    def load_markets(self):
        return self.markets

    def market(self, symbol):
        if symbol not in self.markets:
            raise ccxt.BadSymbol(f"binance does not have market symbol {symbol}")
        return self.markets[symbol]

    def amount_to_precision(self, symbol, amount):
        return str(np.floor(amount * 1e5) / 1e5)

    def price_to_precision(self, symbol, price):
        return str(round(price, 2))


def make_ccxt(cls=BinanceBroker, creds=True, **kw):
    b = cls(credentials={"api_key": "k", "api_secret": "s"} if creds else None)
    b._exchange = FakeExchange(**kw)
    return b


# ---------------------------------------------------------------- registry
def test_registry_and_factory():
    assert set(BROKERS) == {"binance", "kraken", "alpaca", "yahoo"}
    assert "paper" not in BROKERS
    infos = broker_infos()
    assert all(isinstance(i, BrokerInfo) for i in infos)
    assert {i.key for i in infos} == set(BROKERS)
    b = create_broker("binance", options={"sandbox": True})
    assert isinstance(b, BinanceBroker) and b.options["sandbox"] is True
    with pytest.raises(BrokerError, match="Unbekannter Broker"):
        create_broker("nope")
    assert YahooBroker.info().supports_trading is False
    assert AlpacaBroker.info().requires_credentials_for_data is True


# ---------------------------------------------------------------- ccxt
def test_ccxt_pagination_dedupe_and_range():
    b = make_ccxt(overlap=3)
    b.page_limit = 100
    start, end = T0 + timedelta(hours=5), T0 + timedelta(days=20)
    df = b.fetch_ohlcv("BTC/USDT", "1h", start=start, end=end)
    assert list(df.columns) == OHLCV_COLUMNS
    assert str(df.index.tz) == "UTC"
    assert df.index[0] == start and df.index[-1] == end
    assert len(df) == (end - start) // timedelta(hours=1) + 1
    assert not df.index.duplicated().any()
    assert (df.index.to_series().diff().dropna() == timedelta(hours=1)).all()
    assert len(b.exchange.calls) > 4  # paginated
    assert df.dtypes.eq(float).all()


def test_ccxt_limit_only_and_start_limit():
    b = make_ccxt()
    df = b.fetch_ohlcv("BTC/USDT", "1h", limit=50)
    assert len(df) == 50 and b.exchange.calls == [(None, 50)]
    df2 = b.fetch_ohlcv("BTC/USDT", "1h", start=T0, limit=10)
    assert len(df2) == 10 and df2.index[0] == T0


def test_ccxt_stops_when_exchange_returns_nothing():
    b = make_ccxt(last=T0 + timedelta(hours=10))
    df = b.fetch_ohlcv("BTC/USDT", "1h", start=T0, end=T0 + timedelta(days=5))
    assert len(df) == 11


def test_ccxt_unknown_timeframe():
    b = make_ccxt()
    with pytest.raises(BrokerError, match="Zeitrahmen"):
        b.fetch_ohlcv("BTC/USDT", "2h")


def test_ccxt_price_symbols_limits():
    b = make_ccxt()
    b.exchange.fetch_ticker = lambda s: {"last": None, "close": None, "bid": 10, "ask": 12}
    assert b.fetch_price("BTC/USDT") == 11
    assert b.list_symbols() == ["BTC/USDT", "ETH/USDT"]
    assert b.min_order_value("BTC/USDT") == 5.0
    assert b.min_order_value("ETH/USDT") == 0.0
    assert b.normalize_quantity("BTC/USDT", 0.123456789) == pytest.approx(0.12345)
    with pytest.raises(BadSymbolError):
        b.min_order_value("XXX/USDT")


def test_ccxt_balance_drops_zero_and_requires_keys():
    b = make_ccxt()
    b.exchange.fetch_balance = lambda: {"free": {"USDT": 100.0, "BTC": 0.0, "ETH": None, "BNB": 0.5}}
    assert b.fetch_balance() == {"USDT": 100.0, "BNB": 0.5}
    with pytest.raises(AuthenticationError, match="Keine API-Zugangsdaten"):
        make_ccxt(creds=False).fetch_balance()


def test_ccxt_order_mapping_with_fee_conversion():
    b = make_ccxt()
    b.exchange.create_order = MagicMock(return_value={
        "id": "123", "status": "closed", "amount": 0.01, "filled": 0.01,
        "cost": 500.0, "average": None, "price": None, "timestamp": 1_700_000_000_000,
        "fees": [
            {"currency": "USDT", "cost": 0.5},
            {"currency": "BTC", "cost": 0.00001},
            {"currency": "BNB", "cost": 0.002},
        ],
    })
    b.exchange.fetch_ticker = lambda s: {"last": 300.0}  # BNB/USDT
    o = b.place_order("BTC/USDT", Side.BUY, 0.0100004)
    args = b.exchange.create_order.call_args.args
    assert args == ("BTC/USDT", "market", "buy", 0.01, None)
    assert o.id == "123" and o.status == OrderStatus.FILLED
    assert o.price == pytest.approx(50_000.0)
    assert o.filled_quantity == 0.01
    assert o.fee == pytest.approx(0.5 + 0.00001 * 50_000 + 0.002 * 300)
    assert o.created_at == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)


def test_ccxt_limit_order_and_partial():
    b = make_ccxt()
    b.exchange.create_order = MagicMock(return_value={
        "id": "9", "status": "open", "amount": 1.0, "filled": 0.4, "average": 99.0, "fee": None})
    o = b.place_order("ETH/USDT", Side.SELL, 1.0, OrderType.LIMIT, price=99.123)
    assert b.exchange.create_order.call_args.args == ("ETH/USDT", "limit", "sell", 1.0, 99.12)
    assert o.status == OrderStatus.PARTIALLY_FILLED and o.price == 99.0 and o.fee == 0
    with pytest.raises(InvalidOrderError):
        b.place_order("ETH/USDT", Side.SELL, 1.0, OrderType.LIMIT)
    with pytest.raises(InvalidOrderError):
        b.place_order("ETH/USDT", Side.SELL, 0.000001)


def test_ccxt_fetches_order_when_only_id_returned():
    b = make_ccxt()
    b.exchange.create_order = MagicMock(return_value={"id": "5", "status": None, "filled": None})
    b.exchange.fetch_order = MagicMock(return_value={
        "id": "5", "status": "closed", "amount": 1, "filled": 1, "average": 10.0})
    o = b.place_order("ETH/USDT", Side.BUY, 1)
    assert o.status == OrderStatus.FILLED and o.price == 10.0


@pytest.mark.parametrize("exc,expected", [
    (ccxt.AuthenticationError("bad key"), AuthenticationError),
    (ccxt.PermissionDenied("no"), AuthenticationError),
    (ccxt.InsufficientFunds("poor"), InsufficientFundsError),
    (ccxt.InvalidOrder("min notional"), InvalidOrderError),
    (ccxt.BadSymbol("xx"), BadSymbolError),
    (ccxt.RateLimitExceeded("slow down"), RateLimitError),
    (ccxt.RequestTimeout("timeout"), NetworkError),
    (ccxt.ExchangeNotAvailable("451 restricted location"), NetworkError),
    (ccxt.NotSupported("nope"), NotSupportedError),
    (ccxt.ExchangeError("boom"), BrokerError),
])
def test_ccxt_error_mapping(exc, expected):
    err = translate_ccxt_error(exc, "Binance")
    assert type(err) is expected
    assert str(err).startswith("Binance:")


def test_ccxt_call_translates_errors():
    b = make_ccxt()
    b.exchange.cancel_order = MagicMock(side_effect=ccxt.OrderNotFound("unknown order"))
    with pytest.raises(BrokerError, match="Order nicht gefunden"):
        b.cancel_order("1", "BTC/USDT")
    b.exchange.fetch_ticker = MagicMock(side_effect=ccxt.ExchangeNotAvailable("451"))
    with pytest.raises(NetworkError, match="451"):
        b.fetch_price("BTC/USDT")


def test_binance_options_mirror_and_sandbox():
    b = BinanceBroker(options={"public_data_mirror": True})
    assert b.exchange.urls["api"]["public"].startswith("https://data-api.binance.vision")
    assert "api.binance.com" in b.exchange.urls["api"]["private"]
    s = BinanceBroker(options={"sandbox": True})
    assert "testnet.binance.vision" in s.exchange.urls["api"]["public"]
    k = KrakenBroker()
    assert k.page_limit == 720 and k.info().quote_currency == "EUR"


# ---------------------------------------------------------------- alpaca
def _bar(ts, px):
    return SimpleNamespace(timestamp=ts, open=px, high=px + 1, low=px - 1, close=px + 0.5, volume=100)


def make_alpaca(**opts):
    b = AlpacaBroker(credentials={"api_key": "k", "api_secret": "s"}, options={"fill_timeout": 0, **opts})
    b._data_client = MagicMock()
    b._trading_client = MagicMock()
    return b


def test_alpaca_requires_credentials():
    with pytest.raises(AuthenticationError):
        AlpacaBroker().fetch_price("AAPL")
    assert AlpacaBroker().paper is True
    assert AlpacaBroker(options={"paper": False}).paper is False


def test_alpaca_bars_and_timeframe():
    b = make_alpaca()
    bars = [_bar(T0 + timedelta(days=i), 100 + i) for i in range(10)]
    b.data_client.get_stock_bars.return_value = SimpleNamespace(data={"AAPL": bars})
    df = b.fetch_ohlcv("AAPL", "1d", start=T0, end=T0 + timedelta(days=9))
    req = b.data_client.get_stock_bars.call_args.args[0]
    assert req.timeframe.amount_value == 1 and req.timeframe.unit_value.value == "Day"
    assert req.feed.value == "iex"
    assert len(df) == 10 and list(df.columns) == OHLCV_COLUMNS and str(df.index.tz) == "UTC"
    df2 = b.fetch_ohlcv("AAPL", "4h", limit=3)
    req2 = b.data_client.get_stock_bars.call_args.args[0]
    assert req2.timeframe.amount_value == 4 and len(df2) == 3
    b.data_client.get_stock_bars.return_value = SimpleNamespace(data={})
    assert b.fetch_ohlcv("AAPL", "1h", limit=5).empty


def test_alpaca_price_balance():
    b = make_alpaca(feed="sip")
    b.data_client.get_stock_latest_trade.return_value = {"AAPL": SimpleNamespace(price=190.5)}
    assert b.fetch_price("AAPL") == 190.5
    assert b.data_client.get_stock_latest_trade.call_args.args[0].feed.value == "sip"
    b.trading_client.get_account.return_value = SimpleNamespace(cash="1000.5")
    b.trading_client.get_all_positions.return_value = [
        SimpleNamespace(symbol="AAPL", qty="2.5", qty_available="2.5"),
        SimpleNamespace(symbol="SPY", qty="0", qty_available="0"),
    ]
    assert b.fetch_balance() == {"USD": 1000.5, "AAPL": 2.5}


def test_alpaca_orders():
    b = make_alpaca()
    b.trading_client.submit_order.return_value = SimpleNamespace(
        id="uuid-1", status=SimpleNamespace(value="filled"), filled_qty="0.5",
        filled_avg_price="200.0", qty="0.5", limit_price=None,
        submitted_at=T0)
    o = b.place_order("AAPL", Side.BUY, 0.5)
    req = b.trading_client.submit_order.call_args.args[0]
    assert req.qty == 0.5 and req.time_in_force.value == "day" and req.side.value == "buy"
    assert o.id == "uuid-1" and o.status == OrderStatus.FILLED and o.price == 200.0
    assert o.filled_quantity == 0.5 and o.fee == 0.0 and o.created_at == T0

    b.trading_client.submit_order.return_value = SimpleNamespace(
        id="uuid-2", status="accepted", filled_qty="0", filled_avg_price=None, qty="3",
        limit_price="150.0", submitted_at=None)
    o2 = b.place_order("AAPL", Side.SELL, 3, OrderType.LIMIT, price=150.004)
    req2 = b.trading_client.submit_order.call_args.args[0]
    assert req2.limit_price == 150.0
    assert o2.status == OrderStatus.NEW and o2.price == 150.0
    b.cancel_order("uuid-2", "AAPL")
    b.trading_client.cancel_order_by_id.assert_called_once_with("uuid-2")


def test_alpaca_error_mapping():
    from alpaca.common.exceptions import APIError

    def api_error(status, msg):
        http = SimpleNamespace(response=SimpleNamespace(status_code=status), request=None)
        return APIError('{"code": 1, "message": "%s"}' % msg, http)

    b = make_alpaca()
    b.trading_client.submit_order.side_effect = api_error(403, "insufficient buying power")
    with pytest.raises(InsufficientFundsError):
        b.place_order("AAPL", Side.BUY, 1)
    b.trading_client.get_account.side_effect = api_error(401, "unauthorized.")
    with pytest.raises(AuthenticationError):
        b.fetch_balance()
    b.trading_client.cancel_order_by_id.side_effect = api_error(429, "rate limit")
    with pytest.raises(RateLimitError):
        b.cancel_order("x", "AAPL")


# ---------------------------------------------------------------- yahoo
def _yf_frame(index, tz="America/New_York"):
    idx = pd.DatetimeIndex(index)
    idx = idx.tz_localize(tz) if idx.tz is None else idx
    n = len(idx)
    return pd.DataFrame({"Open": np.arange(n) + 1.0, "High": np.arange(n) + 2.0,
                         "Low": np.arange(n) + 0.5, "Close": np.arange(n) + 1.5,
                         "Volume": np.full(n, 1000)}, index=idx)


class FakeTicker:
    frames: list = []
    calls: list = []

    def __init__(self, symbol):
        self.symbol = symbol

    def history(self, **kw):
        FakeTicker.calls.append(kw)
        return FakeTicker.frames.pop(0) if FakeTicker.frames else pd.DataFrame()

    @property
    def fast_info(self):
        return {"last_price": 123.4}


@pytest.fixture
def fake_yf(monkeypatch):
    FakeTicker.frames, FakeTicker.calls = [], []
    monkeypatch.setattr(yahoo_broker.yf, "Ticker", FakeTicker)
    return FakeTicker


def test_yahoo_daily_normalization(fake_yf):
    fake_yf.frames = [_yf_frame(pd.date_range("2024-01-02", periods=5, freq="D"))]
    df = YahooBroker().fetch_ohlcv("AAPL", "1d", start=datetime(2024, 1, 1), end=datetime(2024, 1, 10))
    assert list(df.columns) == OHLCV_COLUMNS
    assert str(df.index.tz) == "UTC"
    assert df.index[0] == pd.Timestamp("2024-01-02", tz="UTC")  # local midnight -> UTC date
    assert fake_yf.calls[0]["interval"] == "1d"


def test_yahoo_4h_resample_and_1w_interval(fake_yf):
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    idx = pd.date_range(now - timedelta(hours=23), periods=24, freq="h", tz="UTC")
    fake_yf.frames = [_yf_frame(idx)]
    df = YahooBroker().fetch_ohlcv("BTC-USD", "4h", start=now - timedelta(days=1), end=now)
    assert fake_yf.calls[0]["interval"] == "1h"
    assert (df.index.hour % 4 == 0).all()
    assert df["volume"].sum() == pytest.approx(24 * 1000)
    fake_yf.frames = [_yf_frame(pd.date_range("2024-01-01", periods=3, freq="7D"))]
    YahooBroker().fetch_ohlcv("SPY", "1w", start=datetime(2024, 1, 1), end=datetime(2024, 1, 20))
    assert fake_yf.calls[-1]["interval"] == "1wk"


def test_yahoo_interval_limits(fake_yf):
    now = datetime.now(timezone.utc)
    with pytest.raises(BrokerError, match="nur für die letzten"):
        YahooBroker().fetch_ohlcv("AAPL", "5m", start=now - timedelta(days=200),
                                  end=now - timedelta(days=100))
    # start clamped into the allowed window
    YahooBroker().fetch_ohlcv("AAPL", "1h", start=now - timedelta(days=1000), end=now)
    assert fake_yf.calls[-1]["start"] >= now - timedelta(days=730)
    # 1m requests are chunked in 7-day windows
    fake_yf.calls.clear()
    YahooBroker().fetch_ohlcv("AAPL", "1m", start=now - timedelta(days=20), end=now)
    assert len(fake_yf.calls) == 3


def test_yahoo_empty_and_trading(fake_yf):
    with pytest.raises(BadSymbolError):
        YahooBroker().fetch_ohlcv("NOPE", "1d", limit=10)
    y = YahooBroker()
    assert y.fetch_price("AAPL") == 123.4
    assert "SAP.DE" in y.list_symbols() and "BTC-USD" in y.list_symbols()
    assert len(y.list_symbols()) >= 55
    for call in (y.fetch_balance, lambda: y.place_order("AAPL", Side.BUY, 1),
                 lambda: y.cancel_order("1", "AAPL")):
        with pytest.raises(NotSupportedError, match="reiner Datenanbieter"):
            call()


def test_yahoo_rate_limit_mapping(monkeypatch):
    from yfinance.exceptions import YFRateLimitError

    class Boom:
        def __init__(self, s):
            pass

        def history(self, **kw):
            raise YFRateLimitError()

    monkeypatch.setattr(yahoo_broker.yf, "Ticker", Boom)
    with pytest.raises(RateLimitError):
        YahooBroker().fetch_ohlcv("AAPL", "1d", limit=5)


# ---------------------------------------------------------------- paper
class StubData(Broker):
    def __init__(self, price=100.0, min_value=0.0):
        super().__init__()
        self.price = price
        self.min_value = min_value
        self.ohlcv_calls = []

    @classmethod
    def info(cls):
        return BrokerInfo(key="stub", name="Stub", asset_classes=[AssetClass.CRYPTO],
                          supports_trading=True, requires_credentials_for_data=False)

    def fetch_ohlcv(self, symbol, timeframe, start=None, end=None, limit=None):
        self.ohlcv_calls.append((start, end))
        idx = pd.date_range(start, end, freq=pd.Timedelta(timeframe.replace("m", "min")), tz="UTC")
        return pd.DataFrame({c: np.ones(len(idx)) for c in OHLCV_COLUMNS}, index=idx)

    def fetch_price(self, symbol):
        return self.price

    def list_symbols(self):
        return ["BTC/USDT"]

    def fetch_balance(self):
        raise AssertionError("paper must never touch the real account")

    def place_order(self, *a, **k):
        raise AssertionError("paper must never send orders")

    def cancel_order(self, *a, **k):
        raise AssertionError("paper must never cancel real orders")

    def min_order_value(self, symbol):
        return self.min_value


def test_paper_market_buy_sell_fees_slippage():
    acc = PaperAccount(10_000)
    pb = PaperBroker(StubData(100.0), acc, fee_pct=0.1, slippage_pct=0.05)
    o = pb.place_order("BTC/USDT", Side.BUY, 10)
    assert o.status == OrderStatus.FILLED and o.price == pytest.approx(100.05)
    assert o.fee == pytest.approx(10 * 100.05 * 0.001)
    assert acc.cash == pytest.approx(10_000 - 1000.5 - 1.0005)
    assert pb.fetch_balance() == {"USD": pytest.approx(acc.cash), "BTC/USDT": 10}
    s = pb.place_order("BTC/USDT", Side.SELL, 10)
    assert s.price == pytest.approx(99.95)
    assert acc.cash == pytest.approx(10_000 - 1000.5 - 1.0005 + 999.5 - 0.9995)
    assert "BTC/USDT" not in acc.holdings
    assert len(acc.orders) == 2
    assert acc.equity({}) == pytest.approx(acc.cash)


def test_paper_insufficient_and_limits():
    acc = PaperAccount(1000)
    pb = PaperBroker(StubData(100.0, min_value=10.0), acc)
    with pytest.raises(InsufficientFundsError, match="Nicht genügend Guthaben"):
        pb.place_order("BTC/USDT", Side.BUY, 10)
    with pytest.raises(InsufficientFundsError, match="Nicht genügend Bestand"):
        pb.place_order("BTC/USDT", Side.SELL, 1)
    with pytest.raises(InvalidOrderError, match="Mindestbetrag"):
        pb.place_order("BTC/USDT", Side.BUY, 0.01)
    assert acc.cash == 1000 and not acc.orders
    # marketable limit: capped at limit price
    o = pb.place_order("BTC/USDT", Side.BUY, 1, OrderType.LIMIT, price=100.01)
    assert o.price == pytest.approx(100.01)
    with pytest.raises(NotSupportedError):
        pb.place_order("BTC/USDT", Side.BUY, 1, OrderType.LIMIT, price=90)
    with pytest.raises(NotSupportedError):
        pb.cancel_order("x", "BTC/USDT")
    assert acc.equity({"BTC/USDT": 110}) == pytest.approx(acc.cash + 110)


def test_paper_account_persistence_and_reset(tmp_path):
    path = tmp_path / "paper" / "account.json"
    acc = PaperAccount(5000, path=path)
    pb = PaperBroker(StubData(50.0), acc, fee_pct=0, slippage_pct=0)
    pb.place_order("ETH/USDT", Side.BUY, 4)
    assert path.exists()
    again = PaperAccount(1, path=path)
    assert again.cash == pytest.approx(4800) and again.holdings == {"ETH/USDT": 4}
    assert again.orders[0]["symbol"] == "ETH/USDT" and again.orders[0]["status"] == "filled"
    assert again.starting_cash == 5000
    again.reset(2000)
    assert PaperAccount(path=path).to_dict() == {
        "starting_cash": 2000, "cash": 2000, "holdings": {}, "orders": []}


def test_paper_shared_account_threadsafe():
    import threading
    acc = PaperAccount(100_000)
    brokers = [PaperBroker(StubData(10.0), acc, fee_pct=0, slippage_pct=0) for _ in range(4)]

    def work(b):
        for _ in range(50):
            b.place_order("X", Side.BUY, 1)

    threads = [threading.Thread(target=work, args=(b,)) for b in brokers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert acc.holdings["X"] == 200 and acc.cash == pytest.approx(100_000 - 2000)


# ---------------------------------------------------------------- cache
def test_cache_fetches_only_edges(tmp_path):
    data = StubData()
    s, e = T0 + timedelta(days=10), T0 + timedelta(days=12)
    df = cache_mod.fetch_history(data, "BTC/USDT", "1h", s, e, tmp_path)
    assert df.index[0] == s and df.index[-1] == e and len(data.ohlcv_calls) == 1
    assert (tmp_path / "stub" / "BTC_USDT_1h.pkl").exists()

    data.ohlcv_calls.clear()
    df = cache_mod.fetch_history(data, "BTC/USDT", "1h", s + timedelta(hours=3), e, tmp_path)
    assert data.ohlcv_calls == [] and len(df) == 46

    df = cache_mod.fetch_history(data, "BTC/USDT", "1h", T0 + timedelta(days=9), T0 + timedelta(days=13), tmp_path)
    assert data.ohlcv_calls == [(T0 + timedelta(days=9), s), (e, T0 + timedelta(days=13))]
    assert len(df) == 4 * 24 + 1 and not df.index.duplicated().any()

    # disjoint request replaces cache with that range only
    data.ohlcv_calls.clear()
    far = T0 + timedelta(days=100)
    cache_mod.fetch_history(data, "BTC/USDT", "1h", far, far + timedelta(days=1), tmp_path)
    assert data.ohlcv_calls == [(far, far + timedelta(days=1))]


def test_cache_refetches_forming_tail(tmp_path):
    data = StubData()
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    cache_mod.fetch_history(data, "BTC/USDT", "1h", now - timedelta(days=1), now, tmp_path)
    data.ohlcv_calls.clear()
    cache_mod.fetch_history(data, "BTC/USDT", "1h", now - timedelta(days=1), now, tmp_path)
    assert len(data.ohlcv_calls) == 1  # only the tail after the last complete candle
    assert data.ohlcv_calls[0][0] >= now - timedelta(hours=1)


def test_cache_disabled_and_paper_key(tmp_path):
    data = StubData()
    df = cache_mod.fetch_history(data, "A", "1h", T0, T0 + timedelta(hours=2), None)
    assert len(df) == 3
    pb = PaperBroker(data, PaperAccount())
    assert cache_mod.cache_key(pb) == "stub"
    assert cache_mod.cache_key(BinanceBroker(options={"sandbox": True})) == "binance_sandbox"


# ---------------------------------------------------------------- network (opt-in)
@pytest.mark.network
def test_live_binance_mirror():
    b = BinanceBroker(options={"public_data_mirror": True})
    now = datetime.now(timezone.utc)
    df = b.fetch_ohlcv("BTC/USDT", "1h", start=now - timedelta(days=60), end=now)
    assert len(df) > 1400 and not df.index.duplicated().any()
    assert b.fetch_price("BTC/USDT") > 0
    assert len(b.list_symbols()) > 100


@pytest.mark.network
def test_live_yahoo():
    df = YahooBroker().fetch_ohlcv("AAPL", "1d", limit=30)
    assert 20 <= len(df) <= 30
