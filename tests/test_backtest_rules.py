"""Backtest tests for the extended trading settings (multi positions, DCA, cooldown,
loss-streak pause, sizing limits, min profit, break-even, trailing activation, limits)."""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from tradebot.backtest.engine import run_backtest
from tradebot.core.models import ExitReason
from tradebot.strategy.models import Condition, Operand, RiskSettings, RuleGroup, Strategy

FEE, SLIP = 0.001, 0.0005


def bars(rows) -> pd.DataFrame:
    """rows: (open, high, low, close) tuples or a single float for a flat bar."""
    rows = [(r, r, r, r) if np.isscalar(r) else r for r in rows]
    a = np.asarray(rows, dtype=float)
    idx = pd.date_range("2024-01-01", periods=len(a), freq="1h", tz="UTC")
    return pd.DataFrame({"open": a[:, 0], "high": a[:, 1], "low": a[:, 2], "close": a[:, 3],
                         "volume": 1.0}, index=idx)


def price(field="close"):
    return Operand("price", field=field)


def val(v):
    return Operand("value", value=v)


ALWAYS = [Condition(price(), ">", val(0))]


def strat(entry=None, exit_=None, entry_logic="all", **risk) -> Strategy:
    base = dict(position_size_pct=10, take_profit_pct=None, stop_loss_pct=50,
                fee_pct=0.1, slippage_pct=0.05)
    base.update(risk)
    return Strategy(name="T", timeframe="1h", entry=RuleGroup(entry_logic, entry or ALWAYS),
                    exit=RuleGroup("any", exit_ or []), risk=RiskSettings(**base))


def entry_bars(res, df) -> list[int]:
    return [int(df.index.get_loc(pd.Timestamp(t.entry_time))) for t in res.trades]


def cost(t) -> float:
    """Total cash spent on the buy incl. entry fee."""
    return t.quantity * t.entry_price * (1 + FEE)


# ---------------------------------------------------------------- multiple positions / DCA
def test_dca_spacing_and_per_symbol_limit():
    df = bars([100, 100, 100, 94, 94, 89, 85, 80, 80])
    res = run_backtest(strat(max_positions_per_symbol=3, add_position_min_drop_pct=5), df)
    assert sorted(entry_bars(res, df)) == [1, 3, 5]
    assert all(t.exit_reason == ExitReason.END_OF_DATA for t in res.trades)
    assert len([m for m in res.markers if m["type"] == "buy"]) == 3


def test_max_open_positions_limits_total():
    df = bars([100, 100, 100, 94, 94, 89, 85, 80, 80])
    res = run_backtest(strat(max_positions_per_symbol=3, add_position_min_drop_pct=5,
                             max_open_positions=2), df)
    assert sorted(entry_bars(res, df)) == [1, 3]


def test_single_position_default():
    df = bars([100] * 8)
    res = run_backtest(strat(), df)
    assert len(res.trades) == 1


# ---------------------------------------------------------------- cooldown
@pytest.mark.parametrize("cooldown,expected", [(0, [1, 2, 3]), (3600, [1, 2, 3]),
                                               (5400, [1, 3, 5]), (7200, [1, 3, 5]),
                                               (7201, [1, 4, 7])])
def test_cooldown_seconds_vs_bar_times(cooldown, expected):
    df = bars([100] * 9)
    res = run_backtest(strat(max_positions_per_symbol=3, add_position_min_drop_pct=0,
                             cooldown_seconds=cooldown), df)
    assert sorted(entry_bars(res, df)) == expected


# ---------------------------------------------------------------- loss-streak pause
def test_loss_streak_pause_timed_release():
    # flat prices -> every trade loses its fees; max_holding_bars=1 forces an exit each time
    df = bars([100] * 12)
    res = run_backtest(strat(max_holding_bars=1, max_loss_streak=2, loss_streak_pause_hours=3),
                       df)
    # trades: buy 1 / exit 2 (loss 1), buy 4 / exit 5 (loss 2 -> paused until bar 8),
    # signal at bar 6 is blocked at bar 7, buy at bar 8 (pause over), exit 9, buy 11 (EOD)
    assert entry_bars(res, df) == [1, 4, 8, 11]
    assert [t.exit_reason for t in res.trades] == [ExitReason.MAX_BARS] * 3 + [ExitReason.END_OF_DATA]
    assert res.trades[1].exit_time == df.index[5].to_pydatetime()
    assert any("Verlust-Serie" in w for w in res.warnings)


def test_loss_streak_resets_after_win():
    rows = [100, 100, 100, 100, 101, 102, 100, 100, 100, 100, 100, 100]
    df = bars(rows)
    res = run_backtest(strat(max_holding_bars=1, max_loss_streak=2, loss_streak_pause_hours=0),
                       df)
    # trade 1 (1->2) loses, trade 2 (4->5 rising) wins -> streak reset, trades continue
    assert res.trades[1].pnl > 0
    assert len(res.trades) >= 4


def test_loss_streak_indefinite_pause():
    df = bars([100] * 12)
    res = run_backtest(strat(max_holding_bars=1, max_loss_streak=2, loss_streak_pause_hours=0),
                       df)
    assert entry_bars(res, df) == [1, 4]


# ---------------------------------------------------------------- sizing
def test_fixed_position_size():
    df = bars([100] * 4)
    res = run_backtest(strat(position_size_mode="fixed", position_size_fixed=250), df)
    assert cost(res.trades[0]) == pytest.approx(250, rel=1e-12)


def test_max_order_value_caps():
    df = bars([100] * 4)
    res = run_backtest(strat(position_size_pct=50, max_order_value=700), df)
    assert cost(res.trades[0]) == pytest.approx(700, rel=1e-12)


def test_min_order_value_skips_small_buys():
    df = bars([100] * 6)
    res = run_backtest(strat(), df, initial_capital=50)  # 10 % of 50 = 5 < 10
    assert res.trades == []
    assert len(res.skipped) == 5  # signals at bars 0..4 -> fills 1..5 (last signal unused)
    assert {s["code"] for s in res.skipped} == {"min_order_value"}
    assert any("Mindest-Kaufbetrag" in w for w in res.warnings)
    res = run_backtest(strat(min_order_value=0), df, initial_capital=50)
    assert cost(res.trades[0]) == pytest.approx(5, rel=1e-12)


def test_cash_reserve():
    df = bars([100] * 6)
    res = run_backtest(strat(position_size_mode="fixed", position_size_fixed=400,
                             cash_reserve=300, max_positions_per_symbol=5,
                             add_position_min_drop_pct=0), df, initial_capital=1000)
    costs = [cost(t) for t in sorted(res.trades, key=lambda t: t.entry_time)]
    assert costs == [pytest.approx(400), pytest.approx(300)]
    assert res.skipped and res.skipped[0]["code"] == "cash_reserve"


def test_exposure_cap():
    df = bars([100] * 8)
    res = run_backtest(strat(position_size_pct=10, max_exposure_pct=25,
                             max_positions_per_symbol=5, add_position_min_drop_pct=0), df)
    trades = sorted(res.trades, key=lambda t: t.entry_time)
    assert len(trades) == 3
    assert cost(trades[0]) == pytest.approx(1000) and cost(trades[1]) == pytest.approx(1000, abs=1)
    assert cost(trades[2]) < 600
    invested = sum(t.quantity * 100 for t in trades)
    assert invested <= 0.25 * res.equity.iloc[-2] + 1e-6
    assert {s["code"] for s in res.skipped} == {"max_exposure"}


# ---------------------------------------------------------------- min profit
MP_ENTRY = [Condition(price(), "crosses_above", val(105))]
MP_EXIT = [Condition(price(), "<", val(200))]  # always true
MP_ROWS = [
    (100, 101, 99, 100), (100, 101, 99, 100), (100, 106, 99, 106),
    (107, 108, 106, 107),  # 3: entry at 107; exit signal
    (107.5, 108, 107, 108),  # 4: signal exit blocked (net profit < 1 %)
    (110, 111, 109, 110),  # 5: exit at 110 allowed
    (110, 111, 109, 110),
]


def test_min_profit_blocks_signal_exit():
    df = bars(MP_ROWS)
    res = run_backtest(strat(MP_ENTRY, MP_EXIT, stop_loss_pct=None, min_profit_pct=1), df)
    t = res.trades[0]
    assert t.exit_reason == ExitReason.SIGNAL
    assert t.exit_time == df.index[5].to_pydatetime()
    assert t.exit_price == pytest.approx(110 * (1 - SLIP))
    # without min profit: exit at bar 4
    res = run_backtest(strat(MP_ENTRY, MP_EXIT, stop_loss_pct=None), df)
    assert res.trades[0].exit_time == df.index[4].to_pydatetime()
    # absolute minimum profit (qty ~ 9.33 -> ~ +27 at 110, ~ +2.3 at 107.5)
    res = run_backtest(strat(MP_ENTRY, MP_EXIT, stop_loss_pct=None, min_profit_abs=10), df)
    assert res.trades[0].exit_time == df.index[5].to_pydatetime()


def test_min_profit_does_not_block_stop_loss():
    rows = MP_ROWS[:4] + [(107.5, 108, 100, 101), (101, 102, 100, 101)]
    df = bars(rows)
    res = run_backtest(strat(MP_ENTRY, MP_EXIT, stop_loss_pct=5, min_profit_pct=50), df)
    t = res.trades[0]
    assert t.exit_reason == ExitReason.STOP_LOSS
    assert t.exit_time == df.index[4].to_pydatetime()


def test_signal_exit_sells_only_allowed_positions():
    entry = [Condition(price(), "<", val(101))]
    exit_ = [Condition(price(), ">", val(103))]
    df = bars([100, 100, (100, 100, 94, 94), (94, 104, 94, 104), 104, 104])
    res = run_backtest(strat(entry, exit_, max_positions_per_symbol=3,
                             add_position_min_drop_pct=5, min_profit_pct=5), df)
    by_entry = {int(df.index.get_loc(pd.Timestamp(t.entry_time))): t for t in res.trades}
    assert set(by_entry) == {1, 3}
    assert by_entry[3].exit_reason == ExitReason.SIGNAL  # ~ +10 %
    assert by_entry[3].exit_time == df.index[4].to_pydatetime()
    assert by_entry[1].exit_reason == ExitReason.END_OF_DATA  # only ~ +3.6 % -> kept


# ---------------------------------------------------------------- break-even / trailing
BE_ENTRY = [Condition(price(), "crosses_above", val(105))]
BE_BASE = [(100, 101, 99, 100), (100, 101, 99, 100), (100, 106, 99, 106)]


def test_breakeven_stop():
    df = bars(BE_BASE + [(107, 110, 106, 109), (109, 109.5, 106, 107), (107, 108, 106, 107)])
    res = run_backtest(strat(BE_ENTRY, stop_loss_pct=5, breakeven_trigger_pct=2), df)
    t = res.trades[0]
    entry = 107 * (1 + SLIP)
    assert t.exit_reason == ExitReason.STOP_LOSS
    assert t.exit_time == df.index[4].to_pydatetime()
    assert t.exit_price == pytest.approx(entry * (1 - SLIP))
    # without break-even no exit
    res = run_backtest(strat(BE_ENTRY, stop_loss_pct=5), df)
    assert res.trades[0].exit_reason == ExitReason.END_OF_DATA


def test_breakeven_not_triggered_below_threshold():
    df = bars(BE_BASE + [(107, 108, 106, 107.5), (107.5, 108, 106, 107), (107, 108, 106, 107)])
    res = run_backtest(strat(BE_ENTRY, stop_loss_pct=5, breakeven_trigger_pct=2), df)
    assert res.trades[0].exit_reason == ExitReason.END_OF_DATA


def test_trailing_activation():
    df = bars(BE_BASE + [
        (107, 110, 106, 109),  # 3: entry, highest 110 (< +5 %)
        (109, 110, 106, 107),  # 4: inactive trailing (would be 106.7)
        (108, 115, 112, 114),  # 5: highest 115 -> activated after this bar
        (114, 114, 111, 112),  # 6: level 111.55 hit
        (112, 113, 111, 112),
    ])
    res = run_backtest(strat(BE_ENTRY, stop_loss_pct=None, trailing_stop_pct=3,
                             trailing_activation_pct=5), df)
    t = res.trades[0]
    assert t.exit_reason == ExitReason.TRAILING_STOP
    assert t.exit_time == df.index[6].to_pydatetime()
    assert t.exit_price == pytest.approx(115 * 0.97 * (1 - SLIP))
    res = run_backtest(strat(BE_ENTRY, stop_loss_pct=None, trailing_stop_pct=3), df)
    assert res.trades[0].exit_time == df.index[4].to_pydatetime()


# ---------------------------------------------------------------- limit orders
def test_limit_prices_cap_slippage():
    df = bars([100, 100, 100, 100])
    r = dict(max_holding_bars=1, buy_order_type="limit", buy_price_multiple=1.0002,
             sell_order_type="limit", sell_price_multiple=0.9998)
    t = run_backtest(strat(**r), df).trades[0]
    assert t.entry_price == pytest.approx(100.02)
    assert t.exit_price == pytest.approx(99.98)
    assert cost(t) < 1000  # quantity sized on the (worse) market basis
    # limits worse than slippage -> market fill
    r.update(buy_price_multiple=1.001, sell_price_multiple=0.999)
    t = run_backtest(strat(**r), df).trades[0]
    assert t.entry_price == pytest.approx(100 * (1 + SLIP))
    assert t.exit_price == pytest.approx(100 * (1 - SLIP))


# ---------------------------------------------------------------- logic "min"
def test_entry_logic_min():
    conds = [Condition(price(), ">", val(1)), Condition(price(), ">", val(2)),
             Condition(price(), ">", val(3)), Condition(price(), ">", val(4))]
    df = bars([1.5, 2.5, 3.5, 4.5, 4.5])
    st = strat(conds, entry_logic="min", max_holding_bars=1)
    st.entry.min_true_pct = 50  # 2 of 4
    from tradebot.strategy.engine import compute_signals
    assert compute_signals(st, df)["entry"].tolist() == [False, True, True, True, True]
    st.entry.min_true_pct = 75  # 3 of 4
    assert compute_signals(st, df)["entry"].tolist() == [False, False, True, True, True]
    res = run_backtest(st, df)
    assert entry_bars(res, df) == [3]


def test_cooldown_uses_bar_timestamps_with_gaps():
    # irregular index (gap between bar 2 and 3): the cooldown is measured in real time
    df = bars([100] * 5)
    t0 = df.index[0]
    df.index = pd.DatetimeIndex([t0 + timedelta(hours=h) for h in (0, 1, 2, 5, 6)])
    res = run_backtest(strat(max_positions_per_symbol=3, add_position_min_drop_pct=0,
                             cooldown_seconds=7200), df)
    assert sorted(entry_bars(res, df)) == [1, 3]


def test_optimize_new_risk_paths():
    from tradebot.backtest.optimize import optimize
    df = bars([100, 100, 100, 94, 94, 89, 85, 80, 80])
    out = optimize(strat(), df, {"risk.max_positions_per_symbol": [1, 3],
                                 "risk.add_position_min_drop_pct": [5, 50],
                                 "entry.min_true_pct": [50]})
    assert len(out) == 4 and out["error"].isna().all()
    trades = {(r["risk.max_positions_per_symbol"], r["risk.add_position_min_drop_pct"]):
              r["num_trades"] for _, r in out.iterrows()}
    assert trades == {(1, 5): 1, (1, 50): 1, (3, 5): 3, (3, 50): 1}
