"""Event-driven single-symbol backtester.

Execution model
---------------
* Long-only, at most one position at a time (``risk.max_open_positions`` is irrelevant for a
  single-symbol backtest).
* Signals (``entry``/``exit`` from :func:`tradebot.strategy.engine.compute_signals`) are
  evaluated on the CLOSE of bar t. The resulting market order is filled at the OPEN of bar
  t+1. Signals of the last bar are never executed.
* Fill prices include slippage: buys fill at ``open * (1 + slippage_pct/100)``, sells at
  ``price * (1 - slippage_pct/100)``. Slippage is applied to EVERY exit (signal, stop,
  take-profit, max-bars, end of data) – conservative.
* Fees: ``fee_pct`` of the traded notional (fill price × quantity) per side; entry + exit
  fee are accumulated into ``ClosedTrade.fees``.
* Position size: :func:`tradebot.strategy.risk.position_quantity` with equity = cash at the
  time of the fill (we are flat), so the buy incl. slippage and fee never exceeds cash.
* Protective exits use :func:`tradebot.strategy.risk.check_exit` (the same function the
  live engine uses) with the bar's open/high/low. They are checked on every bar while in a
  position, starting with the entry bar itself (entry at the open, then that bar's high/low
  may already hit SL/TP – conservative). Gaps through a level fill at the open; if stop and
  take-profit are touched in the same bar, the stop wins. After a bar has been processed
  and the position is still open, :func:`tradebot.strategy.risk.update_after_bar` updates the
  highest price (trailing stop) and the holding-bar counter.
* An exit signal on bar t closes the position at the open of t+1. If a position was closed
  during bar t (for any reason), an entry signal on the close of bar t is ignored
  (no re-entry on the bar of an exit).
* A position still open after the last bar is closed at the last close (with slippage and
  fee) with reason ``END_OF_DATA``.
* Equity is marked to market at every bar close (cash + quantity × close); the last value
  is the realised cash after the END_OF_DATA liquidation.
* Buy & hold benchmark: same capital, buys at the open of the first tradable bar (the first
  bar at which a strategy entry could have been filled, i.e. one bar after all used
  indicators are available) incl. slippage and fee, marked to market at each close and
  liquidated at the last close incl. slippage and fee.
* No look-ahead: indicators/signals only use data up to the signal bar; fills only use the
  following bar's open (and, for protective exits, the current bar's high/low).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from tradebot.backtest.metrics import compute_metrics
from tradebot.core.models import ClosedTrade, ExitReason, Position
from tradebot.strategy import risk as risk_mod
from tradebot.strategy.engine import compute_signals, indicator_columns, validate, warmup_bars
from tradebot.strategy.models import Strategy

MIN_TRADES_MEANINGFUL = 20


@dataclass
class BacktestResult:
    trades: list[ClosedTrade]
    equity: pd.Series
    buy_hold: pd.Series
    signals: pd.DataFrame
    metrics: dict
    markers: list[dict]
    strategy: Strategy
    symbol: str = ""
    warnings: list[str] = field(default_factory=list)
    # per trade: number of bars the position was held (entry bar counts), same order as trades
    bars_held: list[int] = field(default_factory=list)
    # per bar: True if a position was held during (part of) the bar
    in_market: pd.Series | None = None

    def trades_frame(self) -> pd.DataFrame:
        rows = []
        for t, bars in zip(self.trades, self.bars_held):
            d = t.to_dict()
            d["bars_held"] = bars
            rows.append(d)
        return pd.DataFrame(rows)


def _to_dt(ts):
    return ts.to_pydatetime() if isinstance(ts, pd.Timestamp) else ts


def _marker(ts, kind: str, price: float, reason: str) -> dict:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return {
        "time": int(ts.timestamp()),  # unix seconds (UTC), chart friendly
        "datetime": ts.isoformat(),
        "type": kind,
        "price": float(price),
        "reason": reason,
    }


def _resolve_start(df: pd.DataFrame, start) -> int:
    if start is None:
        return 0
    if isinstance(start, (int, np.integer)):
        return max(int(start), 0)
    return int(df.index.searchsorted(pd.Timestamp(start)))


def run_backtest(
    strategy: Strategy,
    df: pd.DataFrame,
    initial_capital: float = 10_000.0,
    symbol: str = "",
    start=None,
) -> BacktestResult:
    """Backtest ``strategy`` on OHLCV ``df`` (see module docstring for the execution model).

    ``start`` (optional, bar position or timestamp): signals/indicators are computed on the
    full ``df`` (so indicators are warmed up), but trading, equity and buy & hold only begin
    at that bar. Used for out-of-sample evaluation.
    """
    errors = validate(strategy)
    if errors:
        raise ValueError("Strategie ungültig:\n" + "\n".join(errors))
    if df is None or len(df) == 0:
        raise ValueError("Keine Kursdaten für den Backtest vorhanden.")
    df = df.sort_index()
    r = strategy.risk
    fee = float(r.fee_pct) / 100.0
    slip = float(r.slippage_pct) / 100.0
    initial_capital = float(initial_capital)

    full_signals = compute_signals(strategy, df)
    s0 = _resolve_start(df, start)
    signals = full_signals.iloc[s0:]
    n = len(signals)
    index = signals.index

    opens = signals["open"].to_numpy(dtype=float)
    highs = signals["high"].to_numpy(dtype=float)
    lows = signals["low"].to_numpy(dtype=float)
    closes = signals["close"].to_numpy(dtype=float)
    entry_sig = signals["entry"].to_numpy(dtype=bool)
    exit_sig = signals["exit"].to_numpy(dtype=bool)

    cash = initial_capital
    pos: Position | None = None
    entry_fee = 0.0
    pending_entry = pending_exit = False
    trades: list[ClosedTrade] = []
    bars_held: list[int] = []
    markers: list[dict] = []
    equity = np.empty(n, dtype=float)
    in_market = np.zeros(n, dtype=bool)

    def close_position(i: int, raw_price: float, reason: ExitReason, held: int) -> None:
        nonlocal cash, pos, entry_fee
        fill = raw_price * (1 - slip)
        exit_fee = pos.quantity * fill * fee
        cash += pos.quantity * fill - exit_fee
        trades.append(ClosedTrade(
            symbol=symbol, quantity=pos.quantity, entry_price=pos.entry_price, exit_price=fill,
            entry_time=pos.entry_time, exit_time=_to_dt(index[i]), fees=entry_fee + exit_fee,
            exit_reason=reason, strategy=strategy.name,
        ))
        bars_held.append(held)
        markers.append(_marker(index[i], "sell", fill, reason.value))
        pos = None
        entry_fee = 0.0

    for i in range(n):
        o, h, lo, c = opens[i], highs[i], lows[i], closes[i]
        exited = False

        # 1) orders from the previous bar's close, filled at this bar's open
        if pending_exit and pos is not None:
            close_position(i, o, ExitReason.SIGNAL, pos.bars_held)
            exited = True
        elif pending_entry and pos is None:
            qty = risk_mod.position_quantity(cash, cash, o, r)
            if qty > 0:
                fill = o * (1 + slip)
                entry_fee = qty * fill * fee
                cash -= qty * fill + entry_fee
                if cash < 0 and cash > -1e-9 * initial_capital:  # float rounding
                    cash = 0.0
                pos = Position(symbol=symbol, quantity=qty, entry_price=fill,
                               entry_time=_to_dt(index[i]), strategy=strategy.name)
                markers.append(_marker(index[i], "buy", fill, "signal"))
        pending_entry = pending_exit = False

        # 2) protective exits within this bar (incl. the entry bar)
        if pos is not None:
            in_market[i] = True
            hit = risk_mod.check_exit(pos, r, o, h, lo)
            if hit is not None:
                reason, price = hit
                close_position(i, price, reason, pos.bars_held + 1)
                exited = True
            else:
                risk_mod.update_after_bar(pos, h)

        # 3) mark to market at the close
        equity[i] = cash + (pos.quantity * c if pos is not None else 0.0)

        # 4) signals at this close -> orders for the next bar's open
        if i < n - 1:
            if pos is not None:
                pending_exit = bool(exit_sig[i])
            elif not exited:
                pending_entry = bool(entry_sig[i])

    if pos is not None:
        close_position(n - 1, closes[-1], ExitReason.END_OF_DATA, pos.bars_held)
        equity[-1] = cash

    equity_s = pd.Series(equity, index=index, name="equity")
    in_market_s = pd.Series(in_market, index=index, name="in_market")

    # ---- buy & hold
    ind_cols = indicator_columns(strategy)
    if ind_cols:
        valid = signals[ind_cols].notna().all(axis=1).to_numpy()
        first_valid = int(np.argmax(valid)) if valid.any() else n
    else:
        first_valid = 0
    b = first_valid + 1
    bh = np.full(n, initial_capital, dtype=float)
    if b < n:
        bh_qty = initial_capital / (opens[b] * (1 + slip) * (1 + fee))
        bh[b:] = bh_qty * closes[b:]
        last_fill = closes[-1] * (1 - slip)
        bh[-1] = bh_qty * last_fill * (1 - fee)
    buy_hold = pd.Series(bh, index=index, name="buy_hold")

    metrics = compute_metrics(equity_s, trades, buy_hold, strategy.timeframe, initial_capital,
                              exposure=in_market_s, bars_held=bars_held)

    # ---- warnings
    warnings: list[str] = []
    wu = warmup_bars(strategy)
    if len(df) < wu:
        warnings.append(
            f"Die Datenreihe ({len(df)} Kerzen) ist kürzer als die empfohlene Aufwärmphase der "
            f"Indikatoren ({wu} Kerzen). Ergebnisse sind wenig verlässlich."
        )
    if b >= n:
        warnings.append("Die Indikatoren haben in diesem Zeitraum nie gültige Werte geliefert – "
                        "es konnte nicht gehandelt werden.")
    if not trades:
        warnings.append("Es wurde kein einziger Trade ausgeführt.")
    elif len(trades) < MIN_TRADES_MEANINGFUL:
        warnings.append(
            f"Nur {len(trades)} Trade{'s' if len(trades) != 1 else ''} – das Ergebnis ist "
            f"statistisch kaum aussagekräftig (empfohlen: mindestens {MIN_TRADES_MEANINGFUL})."
        )
    if fee == 0 and slip == 0:
        warnings.append("Gebühren und Slippage sind 0 – das Ergebnis ist vermutlich zu optimistisch.")

    return BacktestResult(
        trades=trades, equity=equity_s, buy_hold=buy_hold, signals=signals, metrics=metrics,
        markers=markers, strategy=strategy, symbol=symbol, warnings=warnings,
        bars_held=bars_held, in_market=in_market_s,
    )
