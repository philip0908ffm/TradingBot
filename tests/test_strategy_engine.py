"""Tests for tradebot.strategy.engine and presets."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tradebot.indicators.registry import REGISTRY, IndicatorSpec, Param
from tradebot.strategy import engine
from tradebot.strategy.models import Condition, Operand, RiskSettings, RuleGroup, Strategy

# ---------------------------------------------------------------- private test indicators
# Registered only while this module's tests run (other test modules iterate REGISTRY).
def _test_sma(df, period=3):
    return pd.DataFrame({"_test_sma": df["close"].rolling(period, min_periods=period).mean()},
                        index=df.index)


def _test_two(df, k=1):
    return pd.DataFrame({"a": df["close"] * k, "b": df["open"] * k}, index=df.index)


TEST_SPECS = [
    IndicatorSpec("_test_sma", "Test SMA", "Trend", [Param("period", 3, 1, 500)], ["_test_sma"],
                  _test_sma),
    IndicatorSpec("_test_two", "Test Two Outputs", "Other", [Param("k", 1, 0, 10)], ["a", "b"],
                  _test_two),
]


@pytest.fixture(autouse=True, scope="module")
def _test_indicators():
    added = []
    for spec in TEST_SPECS:
        if spec.key not in REGISTRY:
            REGISTRY[spec.key] = spec
            added.append(spec.key)
    yield
    for key in added:
        REGISTRY.pop(key, None)


def make_df(closes, opens=None) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    opens = np.asarray(opens if opens is not None else closes, dtype=float)
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="1h", tz="UTC")
    return pd.DataFrame({"open": opens, "high": np.maximum(opens, closes) + 0.5,
                         "low": np.minimum(opens, closes) - 0.5, "close": closes,
                         "volume": 1.0}, index=idx)


def random_df(n=400, seed=7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    o = np.r_[c[0], c[:-1]]
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.003,
                         "low": np.minimum(o, c) * 0.997, "close": c, "volume": 10.0}, index=idx)


def price(field="close", offset=0):
    return Operand("price", field=field, offset=offset)


def val(v):
    return Operand("value", value=v)


def ind(key, output=None, offset=0, **params):
    return Operand("indicator", indicator=key, params=params, output=output, offset=offset)


def strat(entry, exit_=None, logic="all", exit_logic="any", **risk) -> Strategy:
    return Strategy(name="T", timeframe="1h", entry=RuleGroup(logic, entry),
                    exit=RuleGroup(exit_logic, exit_ or []), risk=RiskSettings(**risk))


# ---------------------------------------------------------------- comparisons / crosses
def test_simple_comparisons():
    df = make_df([1, 2, 3, 2, 1])
    for op, expected in [(">", [0, 0, 1, 0, 0]), (">=", [0, 1, 1, 1, 0]),
                         ("<", [1, 0, 0, 0, 1]), ("<=", [1, 1, 0, 1, 1])]:
        s = engine.evaluate_condition(Condition(price(), op, val(2)), df)
        assert s.tolist() == [bool(x) for x in expected], op


def test_crosses_above_and_below_semantics():
    # equal at t-1 counts as "not above" -> crossing when it moves strictly above
    df = make_df([1, 2, 3, 3, 2, 1, 2, 2, 3])
    up = engine.evaluate_condition(Condition(price(), "crosses_above", val(2)), df)
    # t=2: 3>2 and 2<=2 -> True ; t=8: 3>2 and prev 2<=2 -> True
    assert up.tolist() == [False, False, True, False, False, False, False, False, True]
    down = engine.evaluate_condition(Condition(price(), "crosses_below", val(2)), df)
    # t=5: 1<2 and prev 2>=2 -> True
    assert down.tolist() == [False, False, False, False, False, True, False, False, False]


def test_first_bar_never_crosses():
    df = make_df([5, 5])
    s = engine.evaluate_condition(Condition(price(), "crosses_above", val(1)), df)
    assert s.tolist() == [False, False]


def test_offset_shifts_operand():
    df = make_df([1, 2, 3, 4])
    s = engine.operand_series(price(offset=1), df)
    assert np.isnan(s.iloc[0]) and s.iloc[1:].tolist() == [1, 2, 3]
    # close > close 1 bar ago: rising every bar, but NaN at bar 0 -> False
    cond = Condition(price(), ">", price(offset=1))
    assert engine.evaluate_condition(cond, df).tolist() == [False, True, True, True]


def test_nan_is_false():
    df = make_df([1, 2, 3, 4, 5])
    s = engine.evaluate_condition(Condition(ind("_test_sma", period=3), "<", val(100)), df)
    assert s.tolist() == [False, False, True, True, True]
    s = engine.evaluate_condition(Condition(ind("_test_sma", period=3), ">=", val(-100)), df)
    assert s.tolist() == [False, False, True, True, True]


def test_logic_all_any_and_empty_groups():
    df = make_df([1, 2, 3, 4])
    st = strat([Condition(price(), ">", val(1)), Condition(price(), "<", val(4))], logic="all")
    assert engine.compute_signals(st, df)["entry"].tolist() == [False, True, True, False]
    st = strat([Condition(price(), "<", val(2)), Condition(price(), ">", val(3))], logic="any")
    sig = engine.compute_signals(st, df)
    assert sig["entry"].tolist() == [True, False, False, True]
    assert sig["exit"].tolist() == [False] * 4  # empty exit group -> never
    st = strat([])
    assert not engine.compute_signals(st, df)["entry"].any()


def test_compute_signals_columns_and_multi_output():
    df = make_df([1, 2, 3, 4, 5], opens=[1, 1, 1, 1, 1])
    st = strat([Condition(ind("_test_two", "a", k=2), ">", ind("_test_two", "b", k=2))],
               [Condition(ind("_test_sma", period=2), ">", val(3))])
    sig = engine.compute_signals(st, df)
    assert "_test_two(2).a" in sig.columns and "_test_two(2).b" in sig.columns
    assert "_test_sma(2)._test_sma" in sig.columns
    assert sig["entry"].tolist() == [False, True, True, True, True]
    assert sig["exit"].tolist() == [False, False, False, True, True]
    assert sig["entry"].dtype == bool
    # original columns kept, df untouched
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    pd.testing.assert_series_equal(sig["close"], df["close"])


def test_default_output_is_first():
    df = make_df([1, 2, 3], opens=[0, 0, 0])
    s = engine.operand_series(ind("_test_two", k=1), df)
    assert s.tolist() == [1, 2, 3]


def test_required_indicators_unique_and_warmup():
    st = strat([Condition(ind("_test_sma", period=10), ">", ind("_test_sma", period=10)),
                Condition(ind("_test_sma", period=5, offset=7), ">", ind("_test_sma"))])
    req = engine.required_indicators(st)
    assert ("_test_sma", {"period": 10}) in req and ("_test_sma", {"period": 3}) in req
    assert len(req) == 3
    assert engine.warmup_bars(st) == 50  # 3*10+7+1 < 50
    st2 = strat([Condition(ind("_test_sma", period=100, offset=4), ">", val(0))])
    assert engine.warmup_bars(st2) == 3 * 100 + 4 + 1


def test_no_lookahead_signals_prefix():
    df = random_df(300)
    st = strat([Condition(ind("_test_sma", period=5), "crosses_above", ind("_test_sma", period=20)),
                Condition(price(), ">", price("open", offset=2))],
               [Condition(ind("_test_sma", period=5), "crosses_below", ind("_test_sma", period=20))])
    full = engine.compute_signals(st, df)
    for k in (30, 101, 250):
        part = engine.compute_signals(st, df.iloc[:k])
        pd.testing.assert_frame_equal(part, full.iloc[:k])


# ---------------------------------------------------------------- validation
def test_validate_ok():
    st = strat([Condition(ind("_test_sma", period=3), ">", val(1))], stop_loss_pct=2)
    assert engine.validate(st) == []


def test_validate_messages():
    st = Strategy(name="X", timeframe="2h", entry=RuleGroup("all", [
        Condition(ind("does_not_exist"), ">", val(1)),
        Condition(ind("_test_two", "zzz"), "~", Operand("value", value=None)),
        Condition(ind("_test_sma", period=9999), ">", price("foo")),
    ]), risk=RiskSettings(position_size_pct=150, stop_loss_pct=-1, take_profit_pct=0,
                          fee_pct=-1))
    errs = "\n".join(engine.validate(st))
    assert "Unbekannter Indikator 'does_not_exist'" in errs
    assert "Unbekannte Ausgabe 'zzz'" in errs
    assert "Unbekannter Operator '~'" in errs
    assert "Wert fehlt" in errs
    assert "Unbekannter Zeitrahmen '2h'" in errs
    assert "Positionsgröße" in errs
    assert "Stop-Loss" in errs
    assert "Take-Profit" in errs
    assert "Gebühr" in errs
    assert "zwischen 1 und 500" in errs
    assert "Unbekanntes Preisfeld 'foo'" in errs


def test_validate_no_entry_and_no_exit():
    st = Strategy(name="X", entry=RuleGroup(), risk=RiskSettings(take_profit_pct=None,
                                                                 stop_loss_pct=None))
    errs = engine.validate(st)
    assert "Keine Einstiegsbedingungen definiert." in errs
    assert any("Kein Ausstieg definiert" in e for e in errs)


# ---------------------------------------------------------------- description
def test_describe_condition():
    c = Condition(ind("_test_sma", period=14), "<", val(30))
    assert engine.describe_condition(c) == "_TEST_SMA(14) kleiner als 30"
    c = Condition(price(), "crosses_above", ind("_test_two", "b", k=2, offset=1))
    assert engine.describe_condition(c) == "Schlusskurs kreuzt nach oben _TEST_TWO(2) b (vor 1 Kerze)"
    c = Condition(price("high", offset=3), ">=", val(1.5))
    assert engine.describe_condition(c) == "Hoch (vor 3 Kerzen) größer gleich 1,5"


def test_describe_strategy():
    st = strat([Condition(price(), ">", val(1))], [Condition(price(), "<", val(1))],
               take_profit_pct=5, stop_loss_pct=2.5)
    text = engine.describe_strategy(st)
    assert "Schlusskurs größer als 1" in text and "Stop-Loss 2,5 %" in text


# ---------------------------------------------------------------- real indicators & presets
def test_real_rsi_sma_strategy():
    import tradebot.indicators  # noqa: F401
    if "rsi" not in REGISTRY or "sma" not in REGISTRY:
        pytest.skip("Indikatoren noch nicht verfügbar")
    df = random_df(500)
    st = strat([Condition(ind("rsi", period=14), "<", val(40)),
                Condition(price(), ">", ind("sma", period=50))],
               [Condition(ind("rsi", period=14), ">", val(60))])
    assert engine.validate(st) == []
    assert engine.describe_condition(st.entry.conditions[0]) == "RSI(14) kleiner als 40"
    full = engine.compute_signals(st, df)
    assert "rsi(14).rsi" in full.columns and "sma(50).sma" in full.columns
    assert not full["entry"].iloc[:49].any()  # sma(50) NaN during warm-up
    part = engine.compute_signals(st, df.iloc[:321])
    pd.testing.assert_frame_equal(part, full.iloc[:321])


def test_presets_validate_and_run():
    import tradebot.indicators  # noqa: F401
    from tradebot.strategy.presets import PRESETS
    assert len(PRESETS) >= 8
    assert len({p.name for p in PRESETS}) == len(PRESETS)
    df = random_df(800)
    for p in PRESETS:
        assert engine.validate(p) == [], (p.name, engine.validate(p))
        sig = engine.compute_signals(p, df)
        assert sig["entry"].dtype == bool
        assert engine.describe_strategy(p)
        # JSON round trip
        assert Strategy.from_json(p.to_json()) == p
