"""Tests for tradebot.backtest (engine, metrics, optimize)."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from tradebot.backtest.engine import run_backtest
from tradebot.backtest.metrics import METRIC_LABELS, compute_metrics, drawdown_stats
from tradebot.backtest.optimize import apply_params, optimize, set_path
from tradebot.core.models import ClosedTrade, ExitReason
from tradebot.indicators.registry import REGISTRY, IndicatorSpec, Param
from tradebot.strategy.models import Condition, Operand, RiskSettings, RuleGroup, Strategy

FEE, SLIP = 0.001, 0.0005


# ---------------------------------------------------------------- private test indicator
def _bt_sma(df, period=3):
    return pd.DataFrame({"v": df["close"].rolling(period, min_periods=period).mean()},
                        index=df.index)


@pytest.fixture(autouse=True, scope="module")
def _test_indicators():
    added = False
    if "_bt_sma" not in REGISTRY:
        REGISTRY["_bt_sma"] = IndicatorSpec("_bt_sma", "BT SMA", "Trend",
                                            [Param("period", 3, 1, 500)], ["v"], _bt_sma)
        added = True
    yield
    if added:
        REGISTRY.pop("_bt_sma", None)


# ---------------------------------------------------------------- helpers
def bars(rows) -> pd.DataFrame:
    """rows: list of (open, high, low, close)."""
    a = np.asarray(rows, dtype=float)
    idx = pd.date_range("2024-01-01", periods=len(a), freq="1h", tz="UTC")
    return pd.DataFrame({"open": a[:, 0], "high": a[:, 1], "low": a[:, 2], "close": a[:, 3],
                         "volume": 1.0}, index=idx)


def random_df(n=600, seed=3, freq="1h") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    o = np.r_[c[0], c[:-1]] * (1 + rng.normal(0, 0.002, n))
    idx = pd.date_range("2024-01-01", periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.004,
                         "low": np.minimum(o, c) * 0.996, "close": c, "volume": 5.0}, index=idx)


def price(field="close"):
    return Operand("price", field=field)


def val(v):
    return Operand("value", value=v)


def strat(entry, exit_=None, **risk) -> Strategy:
    base = dict(position_size_pct=50, take_profit_pct=None, stop_loss_pct=None,
                fee_pct=0.1, slippage_pct=0.05)
    base.update(risk)
    return Strategy(name="T", timeframe="1h", entry=RuleGroup("all", entry),
                    exit=RuleGroup("any", exit_ or []), risk=RiskSettings(**base))


ENTRY_CROSS = [Condition(price(), "crosses_above", val(105))]
EXIT_CROSS = [Condition(price(), "crosses_below", val(100))]

BASE_ROWS = [
    (100, 101, 99, 100),
    (100, 101, 99, 100),
    (100, 106, 99, 106),  # 2: entry signal at close
    (107, 108, 106, 107),  # 3: entry fill at open 107
]


def qty_for(open_price, budget=5000.0):
    return budget / (open_price * (1 + SLIP) * (1 + FEE))


# ---------------------------------------------------------------- execution model
def test_signal_entry_exit_next_bar_open_exact_fees():
    df = bars(BASE_ROWS + [
        (107, 108, 98, 99),  # 4: exit signal
        (98, 99, 97, 98),  # 5: exit fill at open 98
        (98, 99, 97, 98),
    ])
    res = run_backtest(strat(ENTRY_CROSS, EXIT_CROSS), df, 10_000)
    assert len(res.trades) == 1
    t = res.trades[0]
    entry = 107 * (1 + SLIP)
    exit_ = 98 * (1 - SLIP)
    q = qty_for(107)
    assert t.entry_price == pytest.approx(entry, rel=1e-12)
    assert t.exit_price == pytest.approx(exit_, rel=1e-12)
    assert t.quantity == pytest.approx(q, rel=1e-12)
    assert t.entry_time == df.index[3] and t.exit_time == df.index[5]
    assert t.exit_reason == ExitReason.SIGNAL
    assert t.fees == pytest.approx(q * entry * FEE + q * exit_ * FEE, rel=1e-12)
    final = 10_000 - q * entry * (1 + FEE) + q * exit_ * (1 - FEE)
    assert res.equity.iloc[-1] == pytest.approx(final, rel=1e-12)
    assert t.pnl == pytest.approx(final - 10_000, rel=1e-9)
    # mark to market
    cash_after_entry = 10_000 - q * entry * (1 + FEE)
    assert res.equity.iloc[2] == pytest.approx(10_000)
    assert res.equity.iloc[3] == pytest.approx(cash_after_entry + q * 107)
    assert res.equity.iloc[4] == pytest.approx(cash_after_entry + q * 99)
    assert res.bars_held == [2]
    assert res.metrics["exposure_pct"] == pytest.approx(2 / 7 * 100)
    assert res.metrics["total_fees"] == pytest.approx(t.fees)
    assert [m["type"] for m in res.markers] == ["buy", "sell"]
    assert res.markers[0]["time"] == int(df.index[3].timestamp())
    assert res.markers[1]["reason"] == "signal"
    assert res.markers[1]["price"] == pytest.approx(exit_)


def test_buy_and_hold_benchmark():
    df = bars(BASE_ROWS + [(107, 108, 98, 99), (98, 99, 97, 98), (98, 99, 97, 98)])
    res = run_backtest(strat(ENTRY_CROSS, EXIT_CROSS), df, 10_000)
    q = 10_000 / (100 * (1 + SLIP) * (1 + FEE))  # buys at bar 1 open (first tradable bar)
    assert res.buy_hold.iloc[0] == 10_000
    assert res.buy_hold.iloc[2] == pytest.approx(q * 106)
    assert res.buy_hold.iloc[-1] == pytest.approx(q * 98 * (1 - SLIP) * (1 - FEE))
    assert res.metrics["buy_hold_return_pct"] == pytest.approx(
        (q * 98 * (1 - SLIP) * (1 - FEE) / 10_000 - 1) * 100)


def test_buy_and_hold_waits_for_indicator_warmup():
    df = random_df(50)
    st = strat([Condition(Operand("indicator", indicator="_bt_sma", params={"period": 10}),
                          ">", val(0))], stop_loss_pct=5)
    res = run_backtest(st, df, 1000)
    # sma(10) first valid at bar 9 -> first fill possible at bar 10
    assert (res.buy_hold.iloc[:10] == 1000).all()
    q = 1000 / (df["open"].iloc[10] * (1 + SLIP) * (1 + FEE))
    assert res.buy_hold.iloc[10] == pytest.approx(q * df["close"].iloc[10])
    assert res.trades[0].entry_time == df.index[10]


def test_stop_loss_intrabar():
    df = bars(BASE_ROWS + [(107, 108, 98, 99), (99, 100, 98, 99)])
    res = run_backtest(strat(ENTRY_CROSS, stop_loss_pct=5), df)
    t = res.trades[0]
    level = 107 * (1 + SLIP) * 0.95
    assert t.exit_reason == ExitReason.STOP_LOSS
    assert t.exit_time == df.index[4]
    assert t.exit_price == pytest.approx(level * (1 - SLIP), rel=1e-12)
    assert res.bars_held == [2]


def test_stop_loss_gap_fills_at_open():
    df = bars(BASE_ROWS + [(95, 96, 94, 95), (95, 96, 94, 95)])
    res = run_backtest(strat(ENTRY_CROSS, stop_loss_pct=5), df)
    t = res.trades[0]
    assert t.exit_reason == ExitReason.STOP_LOSS
    assert t.exit_price == pytest.approx(95 * (1 - SLIP), rel=1e-12)


def test_stop_wins_tie_with_take_profit():
    df = bars(BASE_ROWS + [(107, 115, 98, 108), (108, 109, 107, 108)])
    res = run_backtest(strat(ENTRY_CROSS, stop_loss_pct=5, take_profit_pct=2), df)
    assert res.trades[0].exit_reason == ExitReason.STOP_LOSS


def test_take_profit_and_gap_up():
    df = bars(BASE_ROWS + [(107, 110, 106, 109), (109, 110, 108, 109)])
    res = run_backtest(strat(ENTRY_CROSS, stop_loss_pct=5, take_profit_pct=2), df)
    t = res.trades[0]
    tp = 107 * (1 + SLIP) * 1.02
    assert t.exit_reason == ExitReason.TAKE_PROFIT
    assert t.exit_price == pytest.approx(tp * (1 - SLIP), rel=1e-12)
    df = bars(BASE_ROWS + [(112, 113, 111, 112), (112, 113, 111, 112)])
    t = run_backtest(strat(ENTRY_CROSS, take_profit_pct=2), df).trades[0]
    assert t.exit_price == pytest.approx(112 * (1 - SLIP), rel=1e-12)


def test_stop_on_entry_bar():
    rows = BASE_ROWS[:3] + [(107, 108, 100, 104), (104, 105, 103, 104)]
    df = bars(rows)
    res = run_backtest(strat(ENTRY_CROSS, stop_loss_pct=5), df)
    t = res.trades[0]
    assert t.entry_time == t.exit_time == df.index[3]
    assert t.exit_reason == ExitReason.STOP_LOSS
    assert res.bars_held == [1]


def test_trailing_stop():
    df = bars(BASE_ROWS + [
        (108, 120, 107, 119),  # highest -> 120 after this bar
        (118, 118, 113, 114),  # trailing level 114 hit
        (114, 115, 113, 114),
    ])
    res = run_backtest(strat(ENTRY_CROSS, trailing_stop_pct=5), df)
    t = res.trades[0]
    assert t.exit_reason == ExitReason.TRAILING_STOP
    assert t.exit_time == df.index[5]
    assert t.exit_price == pytest.approx(120 * 0.95 * (1 - SLIP), rel=1e-12)


def test_max_holding_bars():
    df = bars(BASE_ROWS + [(107, 108, 106, 107), (101, 102, 100, 101), (101, 102, 100, 101)])
    res = run_backtest(strat(ENTRY_CROSS, max_holding_bars=2), df)
    t = res.trades[0]
    assert t.exit_reason == ExitReason.MAX_BARS
    assert t.exit_time == df.index[5]
    assert t.exit_price == pytest.approx(101 * (1 - SLIP))


def test_end_of_data():
    df = bars(BASE_ROWS + [(107, 108, 106, 108)])
    res = run_backtest(strat(ENTRY_CROSS, EXIT_CROSS), df)
    t = res.trades[0]
    assert t.exit_reason == ExitReason.END_OF_DATA
    assert t.exit_time == df.index[-1]
    assert t.exit_price == pytest.approx(108 * (1 - SLIP))
    q = qty_for(107)
    final = 10_000 - q * t.entry_price * (1 + FEE) + q * t.exit_price * (1 - FEE)
    assert res.equity.iloc[-1] == pytest.approx(final)
    assert res.markers[-1]["reason"] == "end_of_data"


def test_signal_on_last_bar_not_executed():
    df = bars(BASE_ROWS[:3])  # entry signal on the last bar
    res = run_backtest(strat(ENTRY_CROSS, EXIT_CROSS), df)
    assert res.trades == [] and res.markers == []
    assert (res.equity == 10_000).all()


def test_no_reentry_on_exit_bar():
    above = [Condition(price(), ">", val(105))]
    df = bars(BASE_ROWS + [
        (107, 108, 100, 106),  # 4: stop hit, close still > 105 -> entry ignored
        (106, 107, 105.5, 106),  # 5: no fill here; signal again at close
        (106, 107, 105.5, 106),  # 6: re-entry fill
        (106, 107, 105.5, 106),
    ])
    res = run_backtest(strat(above, stop_loss_pct=5), df)
    assert len(res.trades) == 2
    assert res.trades[0].exit_time == df.index[4]
    assert res.trades[1].entry_time == df.index[6]
    assert res.trades[1].exit_reason == ExitReason.END_OF_DATA


def test_exit_and_entry_signals_one_position_at_a_time():
    # entry always true: never more than one open position, no immediate double buys
    df = random_df(200)
    st = strat([Condition(price(), ">", val(0))], [Condition(price(), "<", price("open"))])
    res = run_backtest(st, df)
    for a, b in zip(res.trades, res.trades[1:]):
        assert b.entry_time > a.exit_time
    buys = [m for m in res.markers if m["type"] == "buy"]
    assert len(buys) == len(res.trades)


def test_invalid_strategy_raises():
    with pytest.raises(ValueError, match="Strategie ungültig"):
        run_backtest(Strategy(name="X", entry=RuleGroup()), random_df(10))


def test_no_trades_metrics_clean():
    df = random_df(300)
    res = run_backtest(strat([Condition(price(), ">", val(1e9))], EXIT_CROSS), df)
    m = res.metrics
    assert set(m) == set(METRIC_LABELS)
    assert m["num_trades"] == 0 and m["final_equity"] == 10_000
    assert m["total_return_pct"] == 0 and m["max_drawdown_pct"] == 0
    assert m["sharpe"] is None and m["profit_factor"] is None and m["win_rate_pct"] is None
    assert m["exposure_pct"] == 0
    for v in m.values():
        assert v is None or math.isfinite(v)
    assert any("kein einziger Trade" in w for w in res.warnings)


def test_warnings_few_trades_and_short_data():
    df = bars(BASE_ROWS + [(107, 108, 98, 99), (98, 99, 97, 98)])
    st = strat([Condition(Operand("indicator", indicator="_bt_sma", params={"period": 1}),
                          "crosses_above", val(105))], EXIT_CROSS)
    res = run_backtest(st, df)
    text = " ".join(res.warnings)
    assert "statistisch kaum aussagekräftig" in text
    assert "Aufwärmphase" in text


def test_no_lookahead_backtest_prefix():
    df = random_df(500)
    sma = lambda p: Operand("indicator", indicator="_bt_sma", params={"period": p})  # noqa: E731
    st = strat([Condition(sma(5), "crosses_above", sma(20))],
               [Condition(sma(5), "crosses_below", sma(20))], stop_loss_pct=2,
               take_profit_pct=3, trailing_stop_pct=2.5)
    full = run_backtest(st, df)
    assert len(full.trades) > 5
    for k in (120, 333):
        part = run_backtest(st, df.iloc[:k])
        # equity identical up to the bar before the forced END_OF_DATA liquidation
        np.testing.assert_allclose(part.equity.iloc[:-1], full.equity.iloc[:k - 1], rtol=1e-12)
        closed_before = [t for t in full.trades if t.exit_time < df.index[k - 1]]
        assert [(t.entry_time, t.exit_time, t.exit_price) for t in closed_before] == \
               [(t.entry_time, t.exit_time, t.exit_price) for t in part.trades[:len(closed_before)]]


def test_real_indicators_backtest():
    import tradebot.indicators  # noqa: F401
    if "rsi" not in REGISTRY or "sma" not in REGISTRY:
        pytest.skip("Indikatoren noch nicht verfügbar")
    df = random_df(1500, freq="4h")
    st = strat([Condition(Operand("indicator", indicator="rsi", params={"period": 14}), "<", val(35)),
                Condition(price(), ">", Operand("indicator", indicator="sma",
                                                params={"period": 100}))],
               [Condition(Operand("indicator", indicator="rsi", params={"period": 14}), ">",
                          val(60))], stop_loss_pct=4)
    res = run_backtest(st, df)
    assert "rsi(14).rsi" in res.signals.columns
    for v in res.metrics.values():
        assert v is None or math.isfinite(v)


def test_presets_backtest():
    import tradebot.indicators  # noqa: F401
    from tradebot.strategy.presets import PRESETS
    df = random_df(1200, freq="4h")
    for p in PRESETS:
        res = run_backtest(p, df, symbol="TEST")
        assert len(res.equity) == len(df)
        assert all(t.symbol == "TEST" for t in res.trades)


# ---------------------------------------------------------------- metrics
def test_metrics_known_equity_curve():
    idx = pd.date_range("2024-01-01", periods=6, freq="1D", tz="UTC")
    eq = pd.Series([100, 120, 90, 95, 130, 110], index=idx, dtype=float)
    dd, dur = drawdown_stats(eq)
    assert dd == pytest.approx(25.0)
    assert dur == 2
    m = compute_metrics(eq, [], eq, "1d", 100)
    assert m["max_drawdown_pct"] == pytest.approx(25.0)
    assert m["total_return_pct"] == pytest.approx(10.0)
    assert m["buy_hold_return_pct"] == pytest.approx(10.0)
    years = 5 / 365.25
    assert m["cagr_pct"] == pytest.approx((1.1 ** (1 / years) - 1) * 100)
    rets = eq.pct_change().dropna()
    bpy = 5 / years  # 365.25 bars per year
    assert m["sharpe"] == pytest.approx(rets.mean() / rets.std(ddof=1) * math.sqrt(bpy))
    assert m["calmar"] == pytest.approx(m["cagr_pct"] / 25.0)


def test_metrics_trades():
    t0 = pd.Timestamp("2024-01-01", tz="UTC").to_pydatetime()
    mk = lambda e, x, f=0.0: ClosedTrade("X", 1.0, e, x, t0, t0, f, ExitReason.SIGNAL)  # noqa: E731
    trades = [mk(100, 110), mk(100, 95), mk(100, 120, 1.0)]
    idx = pd.date_range("2024-01-01", periods=4, freq="1h", tz="UTC")
    eq = pd.Series([100.0, 100, 100, 100], index=idx)
    m = compute_metrics(eq, trades, None, "1h", 100, bars_held=[1, 2, 3])
    assert m["num_trades"] == 3
    assert m["win_rate_pct"] == pytest.approx(200 / 3)
    assert m["profit_factor"] == pytest.approx((10 + 19) / 5)
    assert m["avg_trade_pct"] == pytest.approx((10 - 5 + 19) / 3)
    assert m["avg_win_pct"] == pytest.approx(14.5)
    assert m["avg_loss_pct"] == pytest.approx(-5)
    assert m["best_trade_pct"] == pytest.approx(19) and m["worst_trade_pct"] == pytest.approx(-5)
    assert m["avg_bars_held"] == pytest.approx(2)
    assert m["total_fees"] == pytest.approx(1)
    assert m["expectancy"] == pytest.approx(8)
    assert m["sharpe"] is None and m["volatility_ann_pct"] == 0  # flat curve
    # only winners -> profit factor undefined, not inf
    m = compute_metrics(eq, [mk(100, 110)], None, "1h", 100)
    assert m["profit_factor"] is None and m["avg_loss_pct"] is None


def test_metric_labels_complete():
    for key, (label, fmt) in METRIC_LABELS.items():
        assert label and fmt in {"pct", "money", "num", "int"}


# ---------------------------------------------------------------- optimize
def test_set_path_and_apply_params():
    st = strat(ENTRY_CROSS, EXIT_CROSS)
    s2 = apply_params(st, {"entry.conditions.0.right.value": 104, "risk.take_profit_pct": 7,
                           "entry.conditions.0.left.params.period": 9})
    assert s2.entry.conditions[0].right.value == 104
    assert s2.risk.take_profit_pct == 7
    assert s2.entry.conditions[0].left.params == {"period": 9}
    assert st.entry.conditions[0].right.value == 105  # original untouched
    with pytest.raises(KeyError):
        set_path(st.to_dict(), "entry.conditions.5.right.value", 1)


def test_optimize_grid_sorted():
    df = bars(BASE_ROWS + [(107, 112, 106, 111), (111, 113, 110, 112), (112, 113, 99, 99),
                           (99, 100, 98, 99)])
    grid = {"entry.conditions.0.right.value": [105, 110, 500], "risk.take_profit_pct": [None, 3]}
    out = optimize(strat(ENTRY_CROSS, EXIT_CROSS), df, grid)
    assert len(out) == 6
    assert list(out.columns[:2]) == list(grid)
    vals = out["total_return_pct"].tolist()
    assert vals == sorted(vals, reverse=True)
    # each row equals a direct backtest of that combination
    for _, row in out.iterrows():
        params = {k: (None if pd.isna(row[k]) else row[k]) for k in grid}
        direct = run_backtest(apply_params(strat(ENTRY_CROSS, EXIT_CROSS), params), df)
        assert row["total_return_pct"] == pytest.approx(direct.metrics["total_return_pct"])
    no_trade = out[out["entry.conditions.0.right.value"] == 500]
    assert (no_trade["num_trades"] == 0).all()
    # lower-is-better metric sorts ascending
    out_dd = optimize(strat(ENTRY_CROSS, EXIT_CROSS), df, grid, metric="max_drawdown_pct")
    dd = out_dd["max_drawdown_pct"].tolist()
    assert dd == sorted(dd)


def test_optimize_limits_and_errors():
    df = random_df(100)
    with pytest.raises(ValueError, match="Zu viele Kombinationen"):
        optimize(strat(ENTRY_CROSS, EXIT_CROSS), df, {"risk.fee_pct": list(range(10))},
                 max_runs=5)
    out = optimize(strat(ENTRY_CROSS, EXIT_CROSS), df, {"risk.position_size_pct": [50, 500]})
    assert out["error"].isna().iloc[0]
    assert "Positionsgröße" in out["error"].iloc[1]


def test_optimize_split_oos():
    df = random_df(600)
    sma = lambda p: Operand("indicator", indicator="_bt_sma", params={"period": p})  # noqa: E731
    st = strat([Condition(sma(5), "crosses_above", sma(20))],
               [Condition(sma(5), "crosses_below", sma(20))])
    out = optimize(st, df, {"entry.conditions.0.left.params.period": [3, 5, 8]}, split=0.7)
    assert len(out) == 3
    assert "oos_total_return_pct" in out.columns and "oos_num_trades" in out.columns
    best = out.iloc[0]
    s_best = apply_params(st, {"entry.conditions.0.left.params.period":
                               int(best["entry.conditions.0.left.params.period"])})
    ins = run_backtest(s_best, df.iloc[:420])
    oos = run_backtest(s_best, df, start=420)
    assert best["total_return_pct"] == pytest.approx(ins.metrics["total_return_pct"])
    assert best["oos_total_return_pct"] == pytest.approx(oos.metrics["total_return_pct"])
    assert oos.equity.index[0] == df.index[420]
    assert all(t.entry_time >= df.index[421] for t in oos.trades)
