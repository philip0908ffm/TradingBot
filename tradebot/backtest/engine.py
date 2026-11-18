"""Event-driven single-symbol backtester.

All buy/sell rules come from :mod:`tradebot.strategy.risk` (the same helpers the live/paper
engine uses), with the bar timestamps as "now".

Execution model
---------------
* Long-only. Several positions per symbol are possible (``max_positions_per_symbol`` > 1,
  "DCA"); every buy is its own :class:`Position` and becomes its own ``ClosedTrade``.
  ``can_open`` limits new positions (open_positions_total = open positions of this
  backtest, i.e. effectively ``min(max_open_positions, max_positions_per_symbol)``), the
  re-buy spacing ``add_position_min_drop_pct`` (checked against the fill bar's open),
  ``cooldown_seconds`` and the loss-streak pause (``register_close`` after every closed
  trade with ``pnl = trade.pnl``; pause end compared with bar timestamps; an indefinite
  pause lasts until the end of the backtest).
* Signals (``entry``/``exit``) are evaluated on the CLOSE of bar t, orders are filled at the
  OPEN of bar t+1; "now" for the rules is the timestamp of bar t+1 (= the moment the
  order is placed, i.e. the close time of bar t). Signals of the last bar are not executed.
* Sizing: ``order_budget(equity, cash, invested_value, risk)`` with equity / invested value
  marked at the signal bar's close, then ``quantity_for_budget(budget, next_open, risk)``.
  A budget of 0 means no buy; such skipped buys are listed in ``BacktestResult.skipped``
  (``{"time", "code", "reason"}``) with code ``min_order_value``, ``cash_reserve``, ``cash``
  or ``max_exposure`` (the binding limit).
* Fill prices: market buy = ``open * (1 + slippage)``; market sell = ``price * (1 - slippage)``.
  Limit orders are assumed to fill (the multiples are validated to be marketable) and only
  cap the slippage: limit buy = ``min(open * (1 + slippage), open * buy_price_multiple)``,
  limit sell = ``max(price * (1 - slippage), price * sell_price_multiple)``. The sell
  formula applies to every exit (signal, stops, take-profit, max-bars, end of data).
* Fees: ``fee_pct`` of the traded notional per side; entry + exit fee of a position go into
  ``ClosedTrade.fees``.
* Protective exits: ``check_exit`` per position on every bar while it is open, starting
  with its entry bar (entry at the open, then that bar's high/low may already hit SL/TP –
  conservative). Gaps fill at the open, stop beats take-profit in the same bar. Break-even
  and trailing activation work automatically via ``stop_levels``. Positions still open
  after the bar get ``update_after_bar``.
* An exit signal on bar t sells, at the open of t+1, every position for which
  ``signal_exit_allowed(pos, open, risk, entry_fee)`` is True (minimum profit); the others
  stay open. Protective exits ignore the minimum profit.
* Conflicts: if the exit signal fires while positions are open, an entry signal on the
  same bar is ignored. If any position was closed during bar t (protective exit or signal
  exit at its open), an entry signal on the close of bar t is ignored (no re-entry on the
  bar of an exit).
* Positions still open after the last bar are sold at the last close (slippage + fee,
  reason ``END_OF_DATA``). Equity is marked to market at every close (cash + Σ quantity ×
  close); its last value is the cash after that liquidation.
* Buy & hold benchmark: same capital, buys at the open of the first tradable bar (one bar
  after all used indicators have values) incl. slippage and fee, liquidated at the last
  close incl. slippage and fee.
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
    # buys that were signalled and allowed but had a budget of 0: {"time", "code", "reason"}
    skipped: list[dict] = field(default_factory=list)

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


def buy_fill_price(open_: float, r) -> float:
    fill = open_ * (1 + r.slippage_pct / 100)
    if r.buy_order_type == "limit":
        fill = min(fill, open_ * r.buy_price_multiple)
    return fill


def sell_fill_price(price: float, r) -> float:
    fill = price * (1 - r.slippage_pct / 100)
    if r.sell_order_type == "limit":
        fill = max(fill, price * r.sell_price_multiple)
    return fill


def _skip_reason(equity: float, cash: float, invested: float, r) -> tuple[str, str]:
    """Which rule made ``order_budget`` return 0 (code, German text)."""
    if r.position_size_mode == "fixed":
        base = r.position_size_fixed
    else:
        base = equity * r.position_size_pct / 100.0
    if r.max_order_value:
        base = min(base, r.max_order_value)
    free = cash - r.cash_reserve
    room = equity * r.max_exposure_pct / 100.0 - invested
    limit = min(base, free, room)
    if limit == room and room < base:
        return "max_exposure", "Maximaler investierter Anteil erreicht"
    if limit == free and free < base:
        if r.cash_reserve > 0:
            return "cash_reserve", "Nicht genug freies Kapital (Cash-Reserve)"
        return "cash", "Nicht genug freies Kapital"
    return "min_order_value", f"Kaufbetrag unter dem Mindest-Kaufbetrag ({r.min_order_value:g})"


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
    times = [_to_dt(t) for t in index]

    opens = signals["open"].to_numpy(dtype=float)
    highs = signals["high"].to_numpy(dtype=float)
    lows = signals["low"].to_numpy(dtype=float)
    closes = signals["close"].to_numpy(dtype=float)
    entry_sig = signals["entry"].to_numpy(dtype=bool)
    exit_sig = signals["exit"].to_numpy(dtype=bool)

    cash = initial_capital
    positions: list[Position] = []
    entry_fees: dict[str, float] = {}  # position id -> entry fee
    state = risk_mod.SymbolState()
    pending_entry = pending_exit = False
    signal_equity = signal_invested = 0.0
    trades: list[ClosedTrade] = []
    bars_held: list[int] = []
    markers: list[dict] = []
    skipped: list[dict] = []
    pauses = 0
    equity = np.empty(n, dtype=float)
    in_market = np.zeros(n, dtype=bool)

    def close_position(pos: Position, i: int, raw_price: float, reason: ExitReason,
                       held: int, register: bool = True) -> None:
        nonlocal cash, pauses
        fill = sell_fill_price(raw_price, r)
        exit_fee = pos.quantity * fill * fee
        cash += pos.quantity * fill - exit_fee
        trade = ClosedTrade(
            symbol=symbol, quantity=pos.quantity, entry_price=pos.entry_price, exit_price=fill,
            entry_time=pos.entry_time, exit_time=times[i],
            fees=entry_fees.pop(pos.id, 0.0) + exit_fee, exit_reason=reason,
            strategy=strategy.name,
        )
        trades.append(trade)
        bars_held.append(held)
        markers.append(_marker(index[i], "sell", fill, reason.value))
        positions.remove(pos)
        if register:
            remaining = positions[-1].entry_price if positions else None
            if risk_mod.register_close(state, trade.pnl, times[i], r, remaining):
                pauses += 1

    for i in range(n):
        o, h, lo, c = opens[i], highs[i], lows[i], closes[i]
        now = times[i]
        exited = False

        # 1) orders from the previous bar's close, filled at this bar's open
        if pending_exit:
            for pos in list(positions):
                if risk_mod.signal_exit_allowed(pos, o, r, entry_fees.get(pos.id)):
                    close_position(pos, i, o, ExitReason.SIGNAL, pos.bars_held)
                    exited = True
        elif pending_entry:
            ok, _why = risk_mod.can_open(state, len(positions), len(positions), o, now, r)
            if ok:
                budget = risk_mod.order_budget(signal_equity, cash, signal_invested, r)
                qty = risk_mod.quantity_for_budget(budget, o, r)
                if qty > 0:
                    fill = buy_fill_price(o, r)
                    efee = qty * fill * fee
                    cash -= qty * fill + efee
                    if cash < 0 and cash > -1e-9 * initial_capital:  # float rounding
                        cash = 0.0
                    pos = Position(symbol=symbol, quantity=qty, entry_price=fill,
                                   entry_time=now, strategy=strategy.name)
                    positions.append(pos)
                    entry_fees[pos.id] = efee
                    risk_mod.register_buy(state, fill, now)
                    markers.append(_marker(index[i], "buy", fill, "signal"))
                else:
                    code, reason = _skip_reason(signal_equity, cash, signal_invested, r)
                    skipped.append({"time": now, "code": code, "reason": reason})
        pending_entry = pending_exit = False

        # 2) protective exits within this bar (incl. the entry bar), 3) bar bookkeeping
        if positions:
            in_market[i] = True
        for pos in list(positions):
            hit = risk_mod.check_exit(pos, r, o, h, lo)
            if hit is not None:
                reason, price = hit
                close_position(pos, i, price, reason, pos.bars_held + 1)
                exited = True
            else:
                risk_mod.update_after_bar(pos, h)

        # 4) mark to market at the close
        invested = sum(p.quantity * c for p in positions)
        equity[i] = cash + invested

        # 5) signals at this close -> orders for the next bar's open
        if i < n - 1:
            if positions and exit_sig[i]:
                pending_exit = True
            elif entry_sig[i] and not exited:
                pending_entry = True
                signal_equity, signal_invested = equity[i], invested

    for pos in list(positions):
        close_position(pos, n - 1, closes[-1], ExitReason.END_OF_DATA, pos.bars_held,
                       register=False)
    if trades and trades[-1].exit_reason == ExitReason.END_OF_DATA:
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
        bh[-1] = bh_qty * closes[-1] * (1 - slip) * (1 - fee)
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
    n_min = sum(1 for x in skipped if x["code"] == "min_order_value")
    if n_min and (n_min >= 3 or not trades):
        warnings.append(
            f"{n_min} Käufe wurden übersprungen, weil der Kaufbetrag unter dem Mindest-Kaufbetrag "
            f"({r.min_order_value:g}) lag – Startkapital oder Positionsgröße erhöhen."
        )
    if pauses:
        warnings.append(f"Das Symbol wurde {pauses}× wegen einer Verlust-Serie pausiert.")
    if fee == 0 and slip == 0:
        warnings.append("Gebühren und Slippage sind 0 – das Ergebnis ist vermutlich zu optimistisch.")

    return BacktestResult(
        trades=trades, equity=equity_s, buy_hold=buy_hold, signals=signals, metrics=metrics,
        markers=markers, strategy=strategy, symbol=symbol, warnings=warnings,
        bars_held=bars_held, in_market=in_market_s, skipped=skipped,
    )
