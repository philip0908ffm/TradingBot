"""Trading rules shared by the backtester AND the live/paper engine.

Keeping every buy/sell rule in this one module guarantees that backtest results and live
behaviour match. Both callers pass "now" explicitly (bar time in the backtest, wall clock
live), so time-based rules (cooldown, loss-streak pause) behave identically.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from tradebot.core.models import ExitReason, Position
from tradebot.strategy.models import RiskSettings

# --------------------------------------------------------------------------------------
# Exits
# --------------------------------------------------------------------------------------


def stop_levels(pos: Position, risk: RiskSettings) -> dict[str, float | None]:
    """Current absolute price levels for stop-loss, trailing stop and take-profit.

    * break-even: once the highest price since entry reached +breakeven_trigger_pct,
      the stop-loss is raised to the entry price.
    * trailing stop: only active once the highest price reached +trailing_activation_pct.
    """
    e = pos.entry_price
    sl = e * (1 - risk.stop_loss_pct / 100) if risk.stop_loss_pct else None
    if risk.breakeven_trigger_pct is not None and pos.highest_price >= e * (1 + risk.breakeven_trigger_pct / 100):
        sl = max(sl or 0.0, e)
    ts = None
    if risk.trailing_stop_pct:
        act = risk.trailing_activation_pct
        if not act or pos.highest_price >= e * (1 + act / 100):
            ts = pos.highest_price * (1 - risk.trailing_stop_pct / 100)
    tp = e * (1 + risk.take_profit_pct / 100) if risk.take_profit_pct else None
    return {"stop_loss": sl, "trailing_stop": ts, "take_profit": tp}


def check_exit(
    pos: Position,
    risk: RiskSettings,
    open_: float,
    high: float,
    low: float,
) -> tuple[ExitReason, float] | None:
    """Check whether a protective exit triggers within a bar (or a live price tick,
    then pass open_=high=low=price).

    Returns (reason, trigger_price) or None. Conservative assumptions:
      * if the bar gaps through a level, fill at the open
      * if stop and take-profit are both touched in the same bar, the stop wins
    Does NOT update ``pos.highest_price`` – call ``update_after_bar`` afterwards.
    """
    lv = stop_levels(pos, risk)
    stops = [(ExitReason.STOP_LOSS, lv["stop_loss"]), (ExitReason.TRAILING_STOP, lv["trailing_stop"])]
    stops = [(r, p) for r, p in stops if p is not None]
    if stops:
        reason, level = max(stops, key=lambda x: x[1])  # tightest stop
        if low <= level:
            return reason, min(open_, level)
    tp = lv["take_profit"]
    if tp is not None and high >= tp:
        return ExitReason.TAKE_PROFIT, max(open_, tp)
    if risk.max_holding_bars and pos.bars_held >= risk.max_holding_bars:
        return ExitReason.MAX_BARS, open_
    return None


def update_after_bar(pos: Position, high: float, count_bar: bool = True) -> None:
    pos.highest_price = max(pos.highest_price, high)
    if count_bar:
        pos.bars_held += 1


def estimated_net_proceeds(pos: Position, price: float, risk: RiskSettings) -> float:
    """What selling the position at ``price`` would yield after slippage and fee."""
    return pos.quantity * price * (1 - risk.slippage_pct / 100) * (1 - risk.fee_pct / 100)


def estimated_net_pnl(pos: Position, price: float, risk: RiskSettings, entry_fee: float | None = None) -> float:
    """Net profit of selling now: proceeds - cost basis - entry fee (estimated if unknown)."""
    cost = pos.quantity * pos.entry_price
    if entry_fee is None:
        entry_fee = cost * risk.fee_pct / 100
    return estimated_net_proceeds(pos, price, risk) - cost - entry_fee


def signal_exit_allowed(pos: Position, price: float, risk: RiskSettings, entry_fee: float | None = None) -> bool:
    """Signal-based exits respect the minimum profit; protective exits never call this."""
    if not risk.min_profit_abs and not risk.min_profit_pct:
        return True
    pnl = estimated_net_pnl(pos, price, risk, entry_fee)
    cost = pos.quantity * pos.entry_price
    pct = pnl / cost * 100 if cost else 0.0
    return pnl >= risk.min_profit_abs and pct >= risk.min_profit_pct


# --------------------------------------------------------------------------------------
# Entries
# --------------------------------------------------------------------------------------


@dataclass
class SymbolState:
    """Per-symbol bookkeeping needed by the buy rules (persisted by the live engine)."""

    last_buy_time: datetime | None = None
    last_entry_price: float | None = None  # entry price of the most recent open position
    loss_streak: int = 0
    paused_until: datetime | None = None
    paused_indefinitely: bool = False  # pause with loss_streak_pause_hours == 0

    def to_dict(self) -> dict:
        return {
            "last_buy_time": self.last_buy_time.isoformat() if self.last_buy_time else None,
            "last_entry_price": self.last_entry_price,
            "loss_streak": self.loss_streak,
            "paused_until": self.paused_until.isoformat() if self.paused_until else None,
            "paused_indefinitely": self.paused_indefinitely,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "SymbolState":
        d = d or {}
        ts = lambda v: datetime.fromisoformat(v) if v else None  # noqa: E731
        return cls(ts(d.get("last_buy_time")), d.get("last_entry_price"), int(d.get("loss_streak", 0)),
                   ts(d.get("paused_until")), bool(d.get("paused_indefinitely", False)))


def can_open(
    state: SymbolState,
    open_positions_symbol: int,
    open_positions_total: int,
    price: float,
    now: datetime,
    risk: RiskSettings,
    max_open_positions: int | None = None,
) -> tuple[bool, str]:
    """Whether a NEW position (first or additional) may be opened for a symbol.

    Returns (allowed, German reason if not allowed). ``max_open_positions`` overrides
    ``risk.max_open_positions`` (e.g. a stricter global LIVE limit).
    """
    limit_total = risk.max_open_positions if max_open_positions is None else max_open_positions
    if open_positions_total >= limit_total:
        return False, f"Maximale Anzahl offener Positionen ({limit_total}) erreicht"
    if open_positions_symbol >= max(1, risk.max_positions_per_symbol):
        return False, f"Maximale Positionen für dieses Symbol ({risk.max_positions_per_symbol}) erreicht"
    if state.paused_indefinitely:
        return False, "Symbol pausiert (Verlust-Serie) – Freigabe manuell oder durch Neustart des Bots"
    if state.paused_until and now < state.paused_until:
        return False, f"Symbol pausiert (Verlust-Serie) bis {state.paused_until:%d.%m.%Y %H:%M} UTC"
    if risk.cooldown_seconds and state.last_buy_time and now < state.last_buy_time + timedelta(seconds=risk.cooldown_seconds):
        return False, "Cooldown nach letztem Kauf aktiv"
    if open_positions_symbol > 0 and state.last_entry_price:
        threshold = state.last_entry_price * (1 - risk.add_position_min_drop_pct / 100)
        if price > threshold:
            return False, f"Nachkauf erst unter {threshold:.6g} (−{risk.add_position_min_drop_pct:g} %)"
    return True, ""


def order_budget(equity: float, cash: float, invested_value: float, risk: RiskSettings,
                 hard_cap: float | None = None) -> float:
    """Amount (quote currency, incl. fees) to spend on a new position; 0 = don't buy.

    Applies: sizing mode, max order value, global hard cap, cash reserve, max exposure,
    minimum order value.
    """
    if risk.position_size_mode == "fixed":
        budget = risk.position_size_fixed
    else:
        budget = equity * risk.position_size_pct / 100.0
    if risk.max_order_value:
        budget = min(budget, risk.max_order_value)
    if hard_cap:
        budget = min(budget, hard_cap)
    budget = min(budget, cash - risk.cash_reserve)
    budget = min(budget, equity * risk.max_exposure_pct / 100.0 - invested_value)
    if budget <= 0 or budget < risk.min_order_value:
        return 0.0
    return budget


def quantity_for_budget(budget: float, price: float, risk: RiskSettings) -> float:
    """Quantity purchasable with ``budget`` incl. slippage and fee."""
    eff_price = price * (1 + risk.slippage_pct / 100) * (1 + risk.fee_pct / 100)
    return max(budget / eff_price, 0.0) if eff_price > 0 and budget > 0 else 0.0


def position_quantity(equity: float, cash: float, price: float, risk: RiskSettings,
                      invested_value: float = 0.0, hard_cap: float | None = None) -> float:
    """Quantity to buy for a new position (fees included); 0 if the rules forbid buying."""
    return quantity_for_budget(order_budget(equity, cash, invested_value, risk, hard_cap), price, risk)


def register_buy(state: SymbolState, price: float, now: datetime) -> None:
    state.last_buy_time = now
    state.last_entry_price = price


def register_close(state: SymbolState, pnl: float, now: datetime, risk: RiskSettings,
                   remaining_entry_price: float | None = None) -> bool:
    """Update loss streak after a closed trade. Returns True if the symbol got paused."""
    state.last_entry_price = remaining_entry_price
    if pnl < 0:
        state.loss_streak += 1
    else:
        state.loss_streak = 0
    if risk.max_loss_streak and state.loss_streak >= risk.max_loss_streak:
        state.loss_streak = 0
        if risk.loss_streak_pause_hours and risk.loss_streak_pause_hours > 0:
            state.paused_until = now + timedelta(hours=risk.loss_streak_pause_hours)
        else:
            state.paused_indefinitely = True
        return True
    return False


def resume_symbol(state: SymbolState) -> None:
    """Manual release of a paused symbol."""
    state.paused_until = None
    state.paused_indefinitely = False
    state.loss_streak = 0


# --------------------------------------------------------------------------------------
# Order prices & validation
# --------------------------------------------------------------------------------------


def limit_price(side: str, price: float, risk: RiskSettings) -> float | None:
    """Limit price for an order, or None for a market order."""
    if side == "buy":
        return price * risk.buy_price_multiple if risk.buy_order_type == "limit" else None
    return price * risk.sell_price_multiple if risk.sell_order_type == "limit" else None


def validate_risk(risk: RiskSettings) -> list[str]:
    """German validation messages for the trading settings."""
    err: list[str] = []
    if risk.position_size_mode not in ("pct", "fixed"):
        err.append("Positionsgröße: Modus muss 'pct' oder 'fixed' sein")
    if risk.position_size_mode == "pct" and not 0 < risk.position_size_pct <= 100:
        err.append("Positionsgröße muss zwischen 0 und 100 % liegen")
    if risk.position_size_mode == "fixed" and risk.position_size_fixed <= 0:
        err.append("Fester Kaufbetrag muss größer als 0 sein")
    if risk.min_order_value < 0:
        err.append("Mindest-Kaufbetrag darf nicht negativ sein")
    if risk.max_order_value is not None and risk.max_order_value < risk.min_order_value:
        err.append("Maximaler Kaufbetrag ist kleiner als der Mindest-Kaufbetrag")
    if risk.max_open_positions < 1:
        err.append("Max. offene Positionen muss mindestens 1 sein")
    if risk.max_positions_per_symbol < 1:
        err.append("Max. Positionen pro Symbol muss mindestens 1 sein")
    if risk.add_position_min_drop_pct < 0:
        err.append("Nachkauf-Abstand darf nicht negativ sein")
    if risk.cooldown_seconds < 0 or risk.max_loss_streak < 0 or risk.loss_streak_pause_hours < 0:
        err.append("Cooldown, Verlust-Serie und Pausendauer dürfen nicht negativ sein")
    if not 0 < risk.max_exposure_pct <= 100:
        err.append("Max. investierter Anteil muss zwischen 0 und 100 % liegen")
    if risk.cash_reserve < 0:
        err.append("Cash-Reserve darf nicht negativ sein")
    if risk.klines_to_load < 0:
        err.append("Anzahl Kerzen darf nicht negativ sein")
    for name, v in (("Take-Profit", risk.take_profit_pct), ("Stop-Loss", risk.stop_loss_pct),
                    ("Trailing-Stop", risk.trailing_stop_pct), ("Trailing-Aktivierung", risk.trailing_activation_pct),
                    ("Break-Even", risk.breakeven_trigger_pct)):
        if v is not None and v < 0:
            err.append(f"{name} darf nicht negativ sein")
    if risk.stop_loss_pct is not None and risk.stop_loss_pct >= 100:
        err.append("Stop-Loss muss unter 100 % liegen")
    if risk.trailing_stop_pct is not None and risk.trailing_stop_pct >= 100:
        err.append("Trailing-Stop muss unter 100 % liegen")
    if risk.buy_order_type not in ("market", "limit") or risk.sell_order_type not in ("market", "limit"):
        err.append("Ordertyp muss 'market' oder 'limit' sein")
    if risk.buy_order_type == "limit" and not 1.0 <= risk.buy_price_multiple <= 1.05:
        err.append("Kauf-Preisfaktor muss zwischen 1,0 und 1,05 liegen (sonst wird die Order evtl. nie ausgeführt)")
    if risk.sell_order_type == "limit" and not 0.95 <= risk.sell_price_multiple <= 1.0:
        err.append("Verkaufs-Preisfaktor muss zwischen 0,95 und 1,0 liegen (sonst wird die Order evtl. nie ausgeführt)")
    if risk.min_profit_pct < 0 or risk.min_profit_abs < 0:
        err.append("Mindestgewinn darf nicht negativ sein")
    if risk.trailing_activation_pct and not risk.trailing_stop_pct:
        err.append("Trailing-Aktivierung ohne Trailing-Stop hat keine Wirkung – bitte Trailing-Stop setzen")
    if risk.fee_pct < 0 or risk.slippage_pct < 0:
        err.append("Gebühr und Slippage dürfen nicht negativ sein")
    if risk.max_holding_bars is not None and risk.max_holding_bars < 1:
        err.append("Max. Haltedauer muss mindestens 1 Kerze sein")
    return err


def min_true_count(n_conditions: int, min_true_pct: float) -> int:
    """Number of conditions that must be true for logic == 'min' (at least 1)."""
    return max(1, math.ceil(n_conditions * min_true_pct / 100 - 1e-9))
