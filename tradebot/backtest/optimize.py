"""Brute-force grid search over strategy parameters.

Grid keys are dotted paths into ``Strategy.to_dict()``; list elements are addressed by
their index, e.g. ``"entry.conditions.0.right.value"``, ``"risk.take_profit_pct"`` or
``"entry.conditions.0.left.params.period"``.

With ``split`` (e.g. 0.7) every combination is backtested on the first 70 % of the data
(in-sample, plain metric columns, used for ranking) and on the remaining 30 %
(out-of-sample, columns prefixed ``oos_``). The out-of-sample run computes indicators on
the full history (so they are warmed up) but only trades in the out-of-sample period.
"""
from __future__ import annotations

import copy
import itertools

import pandas as pd

from tradebot.backtest.engine import run_backtest
from tradebot.strategy.models import Strategy

RESULT_METRICS = [
    "total_return_pct", "cagr_pct", "max_drawdown_pct", "sharpe", "sortino", "calmar",
    "num_trades", "win_rate_pct", "profit_factor", "avg_trade_pct", "exposure_pct",
    "final_equity", "buy_hold_return_pct",
]
# metrics where smaller is better
LOWER_IS_BETTER = {"max_drawdown_pct", "max_drawdown_duration_bars", "total_fees"}


def set_path(d: dict, path: str, value) -> None:
    """Set ``value`` at dotted ``path`` inside nested dicts/lists (in place)."""
    parts = path.split(".")
    node = d
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if isinstance(node, list):
            try:
                idx = int(part)
                if last:
                    node[idx] = value
                    return
                node = node[idx]
            except (ValueError, IndexError):
                raise KeyError(f"Ungültiger Pfad '{path}': Element '{part}' existiert nicht.") from None
        elif isinstance(node, dict):
            if last:
                node[part] = value
                return
            if part not in node or node[part] is None:
                # allow creating e.g. "params" dicts on the fly
                node[part] = {}
            node = node[part]
        else:
            raise KeyError(f"Ungültiger Pfad '{path}': '{part}' liegt nicht in einem Objekt.")


def get_path(d, path: str):
    node = d
    for part in path.split("."):
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def apply_params(strategy: Strategy, params: dict) -> Strategy:
    d = copy.deepcopy(strategy.to_dict())
    for path, value in params.items():
        set_path(d, path, value)
    return Strategy.from_dict(d)


def optimize(
    strategy: Strategy,
    df: pd.DataFrame,
    grid: dict[str, list],
    metric: str = "total_return_pct",
    initial_capital: float = 10_000,
    max_runs: int = 500,
    split: float | None = None,
) -> pd.DataFrame:
    """One row per parameter combination (params + key metrics), best first by ``metric``.

    Combinations that produce an invalid strategy get an ``error`` text and NaN metrics
    (sorted last). Raises ``ValueError`` if there are more than ``max_runs`` combinations.
    """
    keys = list(grid)
    values = [list(grid[k]) for k in keys]
    total = 1
    for v in values:
        total *= len(v)
    if total == 0:
        return pd.DataFrame(columns=keys + RESULT_METRICS + ["error"])
    if total > max_runs:
        raise ValueError(
            f"Zu viele Kombinationen ({total}); erlaubt sind höchstens {max_runs}. "
            "Bitte das Raster verkleinern."
        )
    if split is not None and not (0 < split < 1):
        raise ValueError("Der Aufteilungsanteil muss zwischen 0 und 1 liegen (z. B. 0,7).")
    metrics = list(RESULT_METRICS)
    if metric not in metrics:
        metrics.append(metric)

    df = df.sort_index()
    cut = int(len(df) * split) if split is not None else None

    rows = []
    for combo in itertools.product(*values):
        params = dict(zip(keys, combo))
        row: dict = dict(params)
        row["error"] = None
        try:
            strat = apply_params(strategy, params)
            if cut is not None:
                res = run_backtest(strat, df.iloc[:cut], initial_capital)
                oos = run_backtest(strat, df, initial_capital, start=cut)
                for k in metrics:
                    row[f"oos_{k}"] = oos.metrics.get(k)
            else:
                res = run_backtest(strat, df, initial_capital)
            for k in metrics:
                row[k] = res.metrics.get(k)
        except (ValueError, KeyError, TypeError) as exc:
            row["error"] = str(exc)
        rows.append(row)

    out = pd.DataFrame(rows)
    for k in metrics + [f"oos_{k}" for k in metrics] if cut is not None else metrics:
        if k not in out.columns:
            out[k] = None
        out[k] = pd.to_numeric(out[k], errors="coerce")
    ascending = metric in LOWER_IS_BETTER
    out = out.sort_values(metric, ascending=ascending, na_position="last", kind="stable")
    return out.reset_index(drop=True)
