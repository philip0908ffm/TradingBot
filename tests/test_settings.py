import json
import os
import stat

import pytest

from tradebot.core.models import WatchItem
from tradebot.core.settings import AppSettings, SettingsStore, masked, slugify
from tradebot.strategy.models import RiskSettings, Strategy


def test_dirs_created(tmp_path):
    store = SettingsStore(tmp_path / "home")
    for sub in ("strategies", "state", "cache", "logs"):
        assert (store.base_dir / sub).is_dir()
    if os.name == "posix":
        assert stat.S_IMODE(store.base_dir.stat().st_mode) == 0o700


def test_env_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADEBOT_HOME", str(tmp_path / "envhome"))
    store = SettingsStore()
    assert store.base_dir == tmp_path / "envhome"


def test_defaults_and_round_trip(tmp_path):
    store = SettingsStore(tmp_path)
    s = store.load()
    assert [w.symbol for w in s.watchlist] == ["BTC/USDT", "ETH/USDT", "SOL/USDT", "AAPL", "SPY"]
    assert s.live_enabled is False
    assert s.broker_options["alpaca"]["paper"] is True
    s.watchlist.append(WatchItem("XRP/USDT", "binance"))
    s.live_enabled = True
    s.live_max_order_value = 50.0
    s.active_strategy_test = "Meine"
    store.save(s)
    s2 = store.load()
    assert s2.to_dict() == s.to_dict()


def test_tolerant_loading(tmp_path):
    store = SettingsStore(tmp_path)
    store.settings_path.write_text(json.dumps({
        "poll_seconds": "30", "unknown_key": 1, "live_max_open_positions": "abc",
        "watchlist": [{"symbol": "AAPL", "broker": "yahoo", "asset_class": "stock"}, {"bad": 1}],
        "broker_options": {"binance": {"sandbox": True}},
    }))
    s = store.load()
    assert s.poll_seconds == 30
    assert s.live_max_open_positions == 3
    assert [w.symbol for w in s.watchlist] == ["AAPL"]
    assert s.broker_options["binance"] == {"sandbox": True, "public_data_mirror": False}
    store.settings_path.write_text("{ kaputt")
    assert store.load().to_dict() == AppSettings().to_dict()


def test_credentials_encrypted(tmp_path):
    store = SettingsStore(tmp_path)
    assert not store.has_credentials("binance")
    store.set_credentials("binance", {"api_key": "KEY-123456", "api_secret": "SUPERSECRETVALUE"})
    raw = store.credentials_path.read_bytes()
    assert b"SUPERSECRETVALUE" not in raw and b"KEY-123456" not in raw
    assert store.has_credentials("binance")
    assert SettingsStore(tmp_path).get_credentials("binance")["api_secret"] == "SUPERSECRETVALUE"
    if os.name == "posix":
        assert stat.S_IMODE(store.key_path.stat().st_mode) == 0o600
    store.delete_credentials("binance")
    assert not store.has_credentials("binance")
    assert store.get_credentials("binance") == {}


def test_masked():
    assert masked("abcdefghijkl") == "••••ijkl"
    assert masked("") == ""
    assert "abc" not in masked("abc")


def test_strategy_crud(tmp_path):
    store = SettingsStore(tmp_path)
    assert store.list_strategies() == []
    st = Strategy(name="RSI Dip / Größe", timeframe="4h", risk=RiskSettings(take_profit_pct=7))
    path = store.save_strategy(st)
    assert path.parent == store.strategies_dir
    assert "/" not in path.name and slugify(st.name) == path.stem
    assert store.list_strategies() == ["RSI Dip / Größe"]
    loaded = store.load_strategy("RSI Dip / Größe")
    assert loaded.to_dict() == st.to_dict()
    assert store.delete_strategy("RSI Dip / Größe")
    assert store.list_strategies() == []
    with pytest.raises(KeyError):
        store.load_strategy("RSI Dip / Größe")
