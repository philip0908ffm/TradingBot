"""Exit/risk logic shared by the backtester AND the live/paper engine.

Keeping this in one place guarantees that backtest results and live behaviour match.
"""
from __future__ import annotations

from tradebot.core.models import ExitReason, Position
from tradebot.strategy.models import RiskSettings


def stop_levels(pos: Position, risk: RiskSettings) -> dict[str, float | None]:
    """Current absolute price levels for stop-loss, trailing stop and take-profit."""
    sl = pos.entry_price * (1 - risk.stop_loss_pct / 100) if risk.stop_loss_pct else None
    ts = pos.highest_price * (1 - risk.trailing_stop_pct / 100) if risk.trailing_stop_pct else None
    tp = pos.entry_price * (1 + risk.take_profit_pct / 100) if risk.take_profit_pct else None
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

    Returns (reason, fill_price) or None. Conservative assumptions:
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


def position_quantity(equity: float, cash: float, price: float, risk: RiskSettings) -> float:
    """Quantity to buy for a new position (fees included), limited by available cash."""
    budget = min(equity * risk.position_size_pct / 100.0, cash)
    eff_price = price * (1 + risk.slippage_pct / 100) * (1 + risk.fee_pct / 100)
    return max(budget / eff_price, 0.0) if eff_price > 0 else 0.0
