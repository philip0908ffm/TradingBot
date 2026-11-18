"""Application settings, encrypted broker credentials and strategy storage.

Everything lives below the data directory (env ``TRADEBOT_HOME`` or ``~/.tradebot``)::

    settings.json        non-secret application settings (AppSettings)
    credentials.enc      Fernet-encrypted JSON {broker_key: {field: value}}
    .key                 Fernet key (chmod 600)
    strategies/*.json    saved strategies
    state/               bot journals, paper account
    cache/               market data cache
    logs/                log files
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from tradebot.core.models import AssetClass, WatchItem
from tradebot.strategy.models import Strategy

log = logging.getLogger(__name__)

SUBDIRS = ("strategies", "state", "cache", "logs")
IS_POSIX = os.name == "posix"


def default_home() -> Path:
    env = os.environ.get("TRADEBOT_HOME")
    return Path(env).expanduser() if env else Path.home() / ".tradebot"


def atomic_write_text(path: Path, text: str, mode: int | None = None) -> None:
    """Write ``text`` to ``path`` atomically (temp file in same dir + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None and IS_POSIX:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_bytes(path: Path, data: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None and IS_POSIX:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def masked(value: str | None, visible: int = 4) -> str:
    """Mask a secret for display: ``"••••abcd"`` (only the last 4 characters visible)."""
    if not value:
        return ""
    value = str(value)
    if len(value) <= visible * 2:
        return "••••"
    return "••••" + value[-visible:]


def slugify(name: str) -> str:
    """Filesystem-safe slug for strategy file names."""
    s = name.strip().lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:80] or "strategie"


def _default_watchlist() -> list[WatchItem]:
    return [
        WatchItem("BTC/USDT", "binance", AssetClass.CRYPTO),
        WatchItem("ETH/USDT", "binance", AssetClass.CRYPTO),
        WatchItem("SOL/USDT", "binance", AssetClass.CRYPTO),
        WatchItem("AAPL", "yahoo", AssetClass.STOCK),
        WatchItem("SPY", "yahoo", AssetClass.ETF),
    ]


SOUND_EVENTS = ("buy", "sell_profit", "sell_loss", "stop_loss", "take_profit", "error")


def _default_sound_events() -> dict[str, bool]:
    return {k: True for k in SOUND_EVENTS}


def _default_broker_options() -> dict[str, dict]:
    return {
        "binance": {"sandbox": False, "public_data_mirror": False},
        "alpaca": {"paper": True},
    }


@dataclass
class AppSettings:
    watchlist: list[WatchItem] = field(default_factory=_default_watchlist)
    broker_options: dict[str, dict] = field(default_factory=_default_broker_options)
    default_broker: str = "binance"
    paper_starting_cash: float = 10000.0
    poll_seconds: int = 15
    live_enabled: bool = False  # master switch for real-money trading
    live_max_order_value: float = 100.0  # hard cap per order (quote currency)
    live_daily_loss_limit_pct: float = 5.0  # kill switch
    live_max_open_positions: int = 3
    active_strategy_test: str | None = None
    active_strategy_live: str | None = None
    theme: str = "dark"
    order_timeout_seconds: int = 30  # wait for limit/pending orders, then cancel the rest
    sound_enabled: bool = True
    sound_volume: float = 0.6  # 0..1
    sound_events: dict[str, bool] = field(default_factory=_default_sound_events)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {}
        for f in fields(self):
            d[f.name] = getattr(self, f.name)
        d["watchlist"] = [w.to_dict() for w in self.watchlist]
        d["broker_options"] = {k: dict(v) for k, v in self.broker_options.items()}
        d["sound_events"] = dict(self.sound_events)
        return d

    @classmethod
    def from_dict(cls, d: dict | None) -> "AppSettings":
        """Tolerant: missing keys get defaults, unknown keys are ignored, bad values skipped."""
        s = cls()
        if not isinstance(d, dict):
            return s
        defaults = cls()
        for f in fields(cls):
            if f.name not in d or f.name in ("watchlist", "broker_options", "sound_events"):
                continue
            val = d[f.name]
            default = getattr(defaults, f.name)
            try:
                if isinstance(default, bool):
                    val = bool(val)
                elif isinstance(default, int):
                    val = int(val)
                elif isinstance(default, float):
                    val = float(val)
                elif f.name.startswith("active_strategy"):
                    val = None if val in (None, "") else str(val)
                elif isinstance(default, str):
                    val = str(val)
            except (TypeError, ValueError):
                log.warning("Ungültiger Einstellungswert für %s ignoriert", f.name)
                continue
            setattr(s, f.name, val)
        if isinstance(d.get("watchlist"), list):
            items = []
            for raw in d["watchlist"]:
                try:
                    items.append(WatchItem.from_dict(raw))
                except (KeyError, TypeError, ValueError):
                    log.warning("Ungültiger Watchlist-Eintrag ignoriert: %r", raw)
            s.watchlist = items
        if isinstance(d.get("broker_options"), dict):
            opts = _default_broker_options()
            for k, v in d["broker_options"].items():
                if isinstance(v, dict):
                    opts.setdefault(k, {}).update(v)
            s.broker_options = opts
        if isinstance(d.get("sound_events"), dict):
            ev = _default_sound_events()
            for k, v in d["sound_events"].items():
                if isinstance(k, str):
                    ev[k] = bool(v)
            s.sound_events = ev
        s.sound_volume = min(max(s.sound_volume, 0.0), 1.0)
        s.order_timeout_seconds = max(s.order_timeout_seconds, 0)
        return s


class SettingsStore:
    """Persistent storage for settings, credentials and strategies."""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        self.base_dir = Path(base_dir).expanduser() if base_dir else default_home()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        if IS_POSIX:
            try:
                os.chmod(self.base_dir, 0o700)
            except OSError:
                pass
        for sub in SUBDIRS:
            (self.base_dir / sub).mkdir(exist_ok=True)

    # ---- paths ------------------------------------------------------------------------
    @property
    def settings_path(self) -> Path:
        return self.base_dir / "settings.json"

    @property
    def credentials_path(self) -> Path:
        return self.base_dir / "credentials.enc"

    @property
    def key_path(self) -> Path:
        return self.base_dir / ".key"

    @property
    def strategies_dir(self) -> Path:
        return self.base_dir / "strategies"

    @property
    def state_dir(self) -> Path:
        return self.base_dir / "state"

    @property
    def cache_dir(self) -> Path:
        return self.base_dir / "cache"

    @property
    def logs_dir(self) -> Path:
        return self.base_dir / "logs"

    # ---- settings ---------------------------------------------------------------------
    def load(self) -> AppSettings:
        p = self.settings_path
        if not p.exists():
            return AppSettings()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Einstellungen konnten nicht gelesen werden (%s) – Standardwerte werden verwendet", exc)
            return AppSettings()
        return AppSettings.from_dict(data)

    def save(self, settings: AppSettings) -> None:
        atomic_write_text(self.settings_path,
                          json.dumps(settings.to_dict(), indent=2, ensure_ascii=False))

    # ---- credentials ------------------------------------------------------------------
    def _fernet(self) -> Fernet:
        kp = self.key_path
        if kp.exists():
            key = kp.read_bytes().strip()
        else:
            key = Fernet.generate_key()
            atomic_write_bytes(kp, key, mode=0o600)
        if IS_POSIX:
            try:
                os.chmod(kp, 0o600)
            except OSError:
                pass
        return Fernet(key)

    def _read_all_credentials(self) -> dict[str, dict]:
        p = self.credentials_path
        if not p.exists():
            return {}
        try:
            raw = self._fernet().decrypt(p.read_bytes())
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except InvalidToken:
            log.error("Zugangsdaten konnten nicht entschlüsselt werden (Schlüssel passt nicht)")
            return {}
        except (OSError, ValueError) as exc:
            log.error("Zugangsdaten konnten nicht gelesen werden: %s", type(exc).__name__)
            return {}

    def _write_all_credentials(self, data: dict[str, dict]) -> None:
        token = self._fernet().encrypt(json.dumps(data).encode("utf-8"))
        atomic_write_bytes(self.credentials_path, token, mode=0o600)

    def get_credentials(self, broker: str) -> dict:
        return dict(self._read_all_credentials().get(broker, {}))

    def set_credentials(self, broker: str, credentials: dict) -> None:
        data = self._read_all_credentials()
        clean = {k: str(v) for k, v in (credentials or {}).items() if v not in (None, "")}
        if clean:
            data[broker] = clean
        else:
            data.pop(broker, None)
        self._write_all_credentials(data)
        log.info("Zugangsdaten für %s gespeichert", broker)

    def delete_credentials(self, broker: str) -> None:
        data = self._read_all_credentials()
        if data.pop(broker, None) is not None:
            self._write_all_credentials(data)
            log.info("Zugangsdaten für %s gelöscht", broker)

    def has_credentials(self, broker: str) -> bool:
        return bool(self._read_all_credentials().get(broker))

    # ---- strategies -------------------------------------------------------------------
    def _strategy_path(self, name: str) -> Path:
        return self.strategies_dir / f"{slugify(name)}.json"

    def _iter_strategy_files(self):
        for p in sorted(self.strategies_dir.glob("*.json")):
            try:
                yield p, json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                log.warning("Strategiedatei %s ist beschädigt und wird ignoriert", p.name)

    def list_strategies(self) -> list[str]:
        names = []
        for p, data in self._iter_strategy_files():
            if isinstance(data, dict):
                names.append(str(data.get("name") or p.stem))
        return sorted(names, key=str.lower)

    def _find_strategy_file(self, name: str) -> Path | None:
        p = self._strategy_path(name)
        if p.exists():
            return p
        for p, data in self._iter_strategy_files():
            if isinstance(data, dict) and data.get("name") == name:
                return p
        return None

    def load_strategy(self, name: str) -> Strategy:
        p = self._find_strategy_file(name)
        if p is None:
            raise KeyError(f"Strategie '{name}' nicht gefunden")
        try:
            return Strategy.from_json(p.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"Strategie '{name}' konnte nicht geladen werden: {exc}") from exc

    def save_strategy(self, strategy: Strategy) -> Path:
        p = self._strategy_path(strategy.name)
        atomic_write_text(p, strategy.to_json())
        return p

    def delete_strategy(self, name: str) -> bool:
        p = self._find_strategy_file(name)
        if p is None:
            return False
        p.unlink()
        return True
