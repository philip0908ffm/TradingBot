"""Broker registry and factory.

``BROKERS`` lists the user-selectable brokers / data providers. ``PaperBroker`` is not in
the registry: it wraps one of these brokers for test (paper) trading.
"""
from __future__ import annotations

from tradebot.brokers.alpaca_broker import AlpacaBroker
from tradebot.brokers.base import (
    AuthenticationError,
    BadSymbolError,
    Broker,
    BrokerError,
    BrokerInfo,
    CredentialField,
    InsufficientFundsError,
    InvalidOrderError,
    NetworkError,
    NotSupportedError,
    RateLimitError,
)
from tradebot.brokers.cache import fetch_history
from tradebot.brokers.ccxt_broker import BinanceBroker, CcxtBroker, KrakenBroker
from tradebot.brokers.paper import PaperAccount, PaperBroker
from tradebot.brokers.yahoo_broker import YahooBroker

BROKERS: dict[str, type[Broker]] = {
    "binance": BinanceBroker,
    "kraken": KrakenBroker,
    "alpaca": AlpacaBroker,
    "yahoo": YahooBroker,
}


def create_broker(key: str, credentials: dict | None = None, options: dict | None = None) -> Broker:
    """Instantiate a registered broker by key."""
    try:
        cls = BROKERS[key]
    except KeyError:
        raise BrokerError(
            f"Unbekannter Broker '{key}'. Verfügbar: {', '.join(BROKERS)}"
        ) from None
    return cls(credentials=credentials, options=options)


def broker_infos() -> list[BrokerInfo]:
    return [cls.info() for cls in BROKERS.values()]


__all__ = [
    "BROKERS",
    "AlpacaBroker",
    "AuthenticationError",
    "BadSymbolError",
    "BinanceBroker",
    "Broker",
    "BrokerError",
    "BrokerInfo",
    "CcxtBroker",
    "CredentialField",
    "InsufficientFundsError",
    "InvalidOrderError",
    "KrakenBroker",
    "NetworkError",
    "NotSupportedError",
    "PaperAccount",
    "PaperBroker",
    "RateLimitError",
    "YahooBroker",
    "broker_infos",
    "create_broker",
    "fetch_history",
]
