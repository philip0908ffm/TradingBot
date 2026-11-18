"""Tests for the GUI optimizer helpers (parameter discovery + grid search)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from tradebot.gui.components.optimizer import grid_values, param_candidates, run_grid
from tradebot.strategy.presets import PRESETS


def _df(n: int = 600) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    close = 100 + np.cumsum(np.random.default_rng(3).normal(0, 1, n))
    return pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close,
                         "volume": 1000.0}, index=idx)


def test_grid_values():
    assert grid_values(10, 18, 4, "int") == [10, 14, 18]
    assert grid_values(0.5, 1.0, 0.25) == [0.5, 0.75, 1.0]
    assert grid_values(5, 1, 1) == []


def test_candidates_link_identical_indicator_params():
    macd = next(p for p in PRESETS if p.name == "MACD-Kreuzung")
    cands = param_candidates(macd)
    fast = next(c for c in cands if c.label.startswith("MACD · Schnelle"))
    assert len(fast.paths) == 4  # both sides of entry and exit cross change together
    assert any(c.id == "risk.take_profit_pct" for c in cands)
    for c in cands:
        if c.value is not None:
            assert c.lo <= c.value <= c.hi


def test_run_grid_with_split_and_start():
    s = PRESETS[0]
    cands = {c.id: c for c in param_candidates(s)}
    tp = cands["risk.take_profit_pct"]
    grid = {tp.id: [4.0, 8.0]}
    df = _df()
    out = run_grid(s, df, grid, {tp.id: tp.paths}, split=0.7, start=df.index[100])
    assert len(out) == 2
    assert "oos_total_return_pct" in out.columns
    assert out["error"].isna().all()
