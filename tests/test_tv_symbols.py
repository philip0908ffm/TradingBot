import pytest

from tradebot.gui.components.tv_symbols import to_tv_interval, to_tv_symbol


@pytest.mark.parametrize("symbol,broker,expected", [
    ("BTC/USDT", "binance", "BINANCE:BTCUSDT"),
    ("eth/usdt", "binance", "BINANCE:ETHUSDT"),
    ("BTC/EUR", "kraken", "KRAKEN:BTCEUR"),
    ("XBT/EUR", "kraken", "KRAKEN:BTCEUR"),
    ("BTC/USD", "alpaca", "COINBASE:BTCUSD"),
    ("AAPL", "yahoo", "NASDAQ:AAPL"),
    ("AAPL", "alpaca", "NASDAQ:AAPL"),
    ("JPM", "yahoo", "NYSE:JPM"),
    ("SPY", "yahoo", "AMEX:SPY"),
    ("XYZQ", "yahoo", "XYZQ"),
    ("SAP.DE", "yahoo", "XETR:SAP"),
    ("VWCE.DE", "yahoo", "XETR:VWCE"),
    ("IWDA.AS", "yahoo", "EURONEXT:IWDA"),
    ("MC.PA", "yahoo", "EURONEXT:MC"),
    ("VOD.L", "yahoo", "LSE:VOD"),
    ("BTC-USD", "yahoo", "COINBASE:BTCUSD"),
    ("ETH-EUR", "yahoo", "COINBASE:ETHEUR"),
    ("BRK-B", "yahoo", "NYSE:BRK.B"),
    ("NASDAQ:MSFT", "yahoo", "NASDAQ:MSFT"),
    ("", "yahoo", ""),
])
def test_to_tv_symbol(symbol, broker, expected):
    assert to_tv_symbol(symbol, broker) == expected


def test_interval():
    assert to_tv_interval("1h") == "60"
    assert to_tv_interval("1d") == "D"
    assert to_tv_interval("4h") == "240"
    assert to_tv_interval("??") == "60"
