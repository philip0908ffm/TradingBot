"""Map broker-native symbols / timeframes to TradingView notation.

Examples::

    to_tv_symbol("BTC/USDT", "binance")  -> "BINANCE:BTCUSDT"
    to_tv_symbol("BTC/EUR", "kraken")    -> "KRAKEN:BTCEUR"
    to_tv_symbol("AAPL", "yahoo")        -> "NASDAQ:AAPL"
    to_tv_symbol("SAP.DE", "yahoo")      -> "XETR:SAP"
    to_tv_symbol("BTC-USD", "yahoo")     -> "COINBASE:BTCUSD"
"""
from __future__ import annotations

# Yahoo suffix -> TradingView exchange prefix
SUFFIX_EXCHANGES: dict[str, str] = {
    ".DE": "XETR",
    ".F": "FWB",
    ".AS": "EURONEXT",
    ".PA": "EURONEXT",
    ".BR": "EURONEXT",
    ".LS": "EURONEXT",
    ".L": "LSE",
    ".SW": "SIX",
    ".MI": "MIL",
    ".MC": "BME",
    ".VI": "VIE",
    ".ST": "OMXSTO",
    ".CO": "OMXCOP",
    ".HE": "OMXHEX",
    ".OL": "OSL",
    ".TO": "TSX",
    ".HK": "HKEX",
    ".T": "TSE",
    ".AX": "ASX",
}

NASDAQ = {
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "TSLA", "AVGO", "NFLX", "AMD", "INTC",
    "PEP", "COST", "ADBE", "CSCO", "QCOM", "TXN", "AMAT", "PYPL", "SBUX", "MU", "PLTR", "QQQ", "TLT",
}
NYSE = {"BRK.B", "JPM", "V", "MA", "UNH", "XOM", "LLY", "JNJ", "PG", "HD", "KO", "DIS", "WMT", "BAC",
        "CVX", "MRK", "ORCL", "CRM", "NKE", "IBM", "GE"}
AMEX = {"SPY", "VOO", "VTI", "IWM", "DIA", "GLD", "SLV", "XLK", "XLF", "XLE"}

CRYPTO_EXCHANGES = {"binance": "BINANCE", "kraken": "KRAKEN", "coinbase": "COINBASE", "bitstamp": "BITSTAMP"}

TV_INTERVALS: dict[str, str] = {
    "1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "4h": "240", "1d": "D", "1w": "W",
}


def to_tv_interval(timeframe: str) -> str:
    return TV_INTERVALS.get(timeframe, "60")


def to_tv_symbol(symbol: str, broker: str | None = None) -> str:
    """Best-effort TradingView symbol for a broker-native symbol."""
    sym = (symbol or "").strip()
    if not sym:
        return ""
    if ":" in sym:  # already TradingView notation
        return sym.upper()
    broker = (broker or "").lower()
    up = sym.upper()

    # ccxt style pair "BASE/QUOTE"
    if "/" in up:
        base, quote = up.split("/", 1)
        quote = quote.split(":", 1)[0]
        if broker == "kraken" and base == "XBT":
            base = "BTC"
        exchange = CRYPTO_EXCHANGES.get(broker, "BINANCE" if quote in ("USDT", "USDC", "BUSD", "FDUSD")
                                        else "COINBASE")
        return f"{exchange}:{base}{quote}"

    # Yahoo crypto "BTC-USD"
    if "-" in up and broker != "alpaca":
        base, quote = up.rsplit("-", 1)
        if quote in ("USD", "EUR", "GBP", "USDT", "USDC", "BTC", "ETH"):
            return f"COINBASE:{base}{quote}"
        up = f"{base}.{quote}"  # share classes, e.g. BRK-B -> BRK.B

    # Yahoo exchange suffix "SAP.DE"
    if "." in up:
        idx = up.rfind(".")
        suffix = up[idx:]
        if suffix in SUFFIX_EXCHANGES:
            return f"{SUFFIX_EXCHANGES[suffix]}:{up[:idx]}"
        if suffix == ".B" and up in NYSE:  # BRK.B
            return f"NYSE:{up}"
        return up

    # US stocks / ETFs (Yahoo, Alpaca)
    if up in NASDAQ:
        return f"NASDAQ:{up}"
    if up in NYSE:
        return f"NYSE:{up}"
    if up in AMEX:
        return f"AMEX:{up}"
    return up
