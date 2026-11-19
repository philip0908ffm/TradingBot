"""Parameter discovery and grid search for the backtest optimizer tab (GUI independent).

``param_candidates`` lists the numbers of a strategy that can be varied. Identical indicator
parameters that occur in several conditions (e.g. the MACD settings on both sides of a cross)
are linked into one candidate with several paths, so they always change together.

``run_grid`` mirrors :func:`tradebot.backtest.optimize.optimize` (same paths, same result
columns, in-sample columns plain, out-of-sample columns prefixed ``oos_``) but additionally
supports linked paths, a trading start (warm-up candles before the chosen period) and a progress
callback.
"""
from __future__ import annotations

import copy
import itertools
import math
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from tradebot.backtest.engine import run_backtest
from tradebot.backtest.optimize import LOWER_IS_BETTER, RESULT_METRICS, set_path
from tradebot.indicators import REGISTRY
from tradebot.strategy.models import OPERATORS, Strategy
from tradebot.gui.components.strategy_labels import PRICE_FIELD_LABELS, RISK_FIELDS, fmt_de, output_label

GROUP_LABELS = {"entry": "Kauf", "exit": "Verkauf"}
OPT_RISK_KEYS = ["take_profit_pct", "stop_loss_pct", "trailing_stop_pct", "trailing_activation_pct",
                 "breakeven_trigger_pct", "max_holding_bars", "position_size_pct", "min_profit_pct"]


@dataclass
class ParamCandidate:
    id: str  # first path, stable key
    paths: list[str]
    label: str
    group: str  # "Indikator" | "Schwellenwert" | "Trading"
    value: float | None
    type: str  # "int" | "float"
    lo: float
    hi: float
    step: float
    min: float | None = None
    max: float | None = None
    extra: dict = field(default_factory=dict)
    short: str = ""  # compact label for result tables


def _nice_step(span: float, n: int = 4, integer: bool = False) -> float:
    if span <= 0:
        return 1 if integer else 0.1
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            step = m * mag
            break
    if integer:
        step = max(1, round(step))
    return float(step)


def _range_around(v: float, integer: bool, lo_lim: float | None, hi_lim: float | None,
                  factor_lo: float = 0.5, factor_hi: float = 1.5) -> tuple[float, float, float]:
    if v == 0:
        lo, hi = -1.0, 1.0
    else:
        a, b = v * factor_lo, v * factor_hi
        lo, hi = min(a, b), max(a, b)
    if lo_lim is not None:
        lo = max(lo, lo_lim)
    if hi_lim is not None:
        hi = min(hi, hi_lim)
    step = max(1.0, float(round((hi - lo) / 4))) if integer else _nice_step(hi - lo, 6)
    # keep the current value on the grid
    lo = v - math.floor((v - lo) / step + 1e-9) * step
    hi = v + math.floor((hi - v) / step + 1e-9) * step
    if lo_lim is not None and lo < lo_lim:
        lo += math.ceil((lo_lim - lo) / step - 1e-9) * step
    if hi_lim is not None and hi > hi_lim:
        hi -= math.ceil((hi - hi_lim) / step - 1e-9) * step
    lo, hi = round(lo, 10), round(hi, 10)
    return float(lo), float(hi), float(step)


def _ind_text(op: dict) -> str:
    spec = REGISTRY.get(op.get("indicator") or "")
    if spec is None:
        return "?"
    p = {**spec.default_params(), **(op.get("params") or {})}
    args = ",".join(fmt_de(p[x.name]) for x in spec.params)
    t = spec.key.upper() + (f"({args})" if args else "")
    if len(spec.outputs) > 1:
        t += " " + output_label(spec.key, op.get("output") or spec.outputs[0])
    return t


def _operand_text(op: dict) -> str:
    k = op.get("kind")
    if k == "indicator":
        return _ind_text(op)
    if k == "price":
        return PRICE_FIELD_LABELS.get(op.get("field"), op.get("field") or "?")
    return fmt_de(op.get("value"))


def param_candidates(strategy: Strategy) -> list[ParamCandidate]:
    d = strategy.to_dict()
    out: list[ParamCandidate] = []
    linked: dict[tuple, ParamCandidate] = {}

    for g in ("entry", "exit"):
        for i, cond in enumerate(d[g]["conditions"]):
            where = f"{GROUP_LABELS[g]} {i + 1}"
            cond_text = (f"{_operand_text(cond['left'])} {OPERATORS.get(cond['op'], cond['op'])} "
                         f"{_operand_text(cond['right'])}")
            for side in ("left", "right"):
                op = cond[side]
                base = f"{g}.conditions.{i}.{side}"
                if op.get("kind") == "indicator" and op.get("indicator") in REGISTRY:
                    spec = REGISTRY[op["indicator"]]
                    params = {**spec.default_params(), **(op.get("params") or {})}
                    key_all = (spec.key, tuple(sorted(params.items())))
                    for p in spec.params:
                        if p.type == "int" and p.max > 1000:
                            continue  # e.g. periods_per_year
                        path = f"{base}.params.{p.name}"
                        lk = key_all + (p.name,)
                        if lk in linked:
                            c = linked[lk]
                            c.paths.append(path)
                            if where not in c.extra["where"]:
                                c.extra["where"].append(where)
                            c.label = f"{spec.key.upper()} · {p.label or p.name} ({', '.join(c.extra['where'])})"
                            continue
                        v = float(params[p.name])
                        integer = p.type == "int"
                        lo, hi, step = _range_around(v, integer, p.min, p.max)
                        c = ParamCandidate(path, [path], f"{spec.key.upper()} · {p.label or p.name} ({where})",
                                           "Indikator", v, p.type, lo, hi, step, p.min, p.max,
                                           extra={"where": [where]},
                                           short=f"{spec.key.upper()} {p.label or p.name}")
                        linked[lk] = c
                        out.append(c)
                elif op.get("kind") == "value" and op.get("value") is not None:
                    v = float(op["value"])
                    path = f"{base}.value"
                    lo, hi, step = _range_around(v, False, None, None, 0.8, 1.2)
                    if float(v).is_integer() and step >= 1:
                        lo, hi = math.floor(lo), math.ceil(hi)
                    out.append(ParamCandidate(path, [path], f"Schwellenwert {where}: {cond_text}", "Schwellenwert",
                                              v, "float", lo, hi, step, short=f"Schwelle {where}"))
    r = d["risk"]
    for k in OPT_RISK_KEYS:
        f = RISK_FIELDS[k]
        v = r.get(k)
        active = v is not None
        base_v = float(v if active else (f.on_default or 1))
        integer = f.type == "int"
        lo, hi, step = _range_around(base_v, integer, f.min if f.min is not None else 0, f.max, 0.5, 2.0)
        lbl = f.label + ("" if active else " (derzeit aus)")
        out.append(ParamCandidate(f"risk.{k}", [f"risk.{k}"], lbl, "Trading", v, f.type, lo, hi, step,
                                  f.min, f.max, short=f.short or f.label))
    order = {"Indikator": 0, "Schwellenwert": 1, "Trading": 2}
    return sorted(out, key=lambda c: order[c.group])


def grid_values(lo: float, hi: float, step: float, typ: str = "float", max_values: int = 10_000) -> list:
    """Inclusive range lo..hi with step (rounded to avoid float noise)."""
    if lo is None or hi is None or step is None or step <= 0 or hi < lo:
        return []
    n = int(math.floor((hi - lo) / step + 1e-9)) + 1
    if n > max_values:
        n = max_values + 1  # caller sees "too many"
    vals = []
    for i in range(n):
        v = lo + i * step
        vals.append(int(round(v)) if typ == "int" else round(v, 10))
    if typ == "int":
        vals = sorted(set(vals))
    return vals


def count_combinations(sizes: list[int]) -> int:
    total = 1
    for s in sizes:
        total *= s
    return total


def apply_values(strategy: Strategy, values: dict[str, object], paths: dict[str, list[str]]) -> Strategy:
    d = copy.deepcopy(strategy.to_dict())
    for cid, v in values.items():
        for p in paths.get(cid, [cid]):
            set_path(d, p, v)
    return Strategy.from_dict(d)


def run_grid(strategy: Strategy, df: pd.DataFrame, grid: dict[str, list], paths: dict[str, list[str]],
             metric: str = "total_return_pct", initial_capital: float = 10_000, max_runs: int = 500,
             split: float | None = None, start=None,
             progress: Callable[[int, int], None] | None = None) -> pd.DataFrame:
    keys = list(grid)
    values = [list(grid[k]) for k in keys]
    total = count_combinations([len(v) for v in values])
    if total == 0:
        raise ValueError("Keine Kombinationen – bitte Bereiche prüfen.")
    if total > max_runs:
        raise ValueError(f"Zu viele Kombinationen ({total}); erlaubt sind höchstens {max_runs}.")
    metrics = list(RESULT_METRICS)
    if metric not in metrics:
        metrics.append(metric)
    df = df.sort_index()
    s0 = 0
    if start is not None:
        s0 = int(df.index.searchsorted(pd.Timestamp(start)))
    cut = s0 + int((len(df) - s0) * split) if split else None

    rows = []
    for n, combo in enumerate(itertools.product(*values), start=1):
        params = dict(zip(keys, combo))
        row: dict = dict(params)
        row["error"] = None
        try:
            strat = apply_values(strategy, params, paths)
            if cut is not None:
                res = run_backtest(strat, df.iloc[:cut], initial_capital, start=s0)
                oos = run_backtest(strat, df, initial_capital, start=cut)
                for k in metrics:
                    row[f"oos_{k}"] = oos.metrics.get(k)
            else:
                res = run_backtest(strat, df, initial_capital, start=s0)
            for k in metrics:
                row[k] = res.metrics.get(k)
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            row["error"] = str(exc).splitlines()[-1] if str(exc) else repr(exc)
        rows.append(row)
        if progress:
            progress(n, total)

    out = pd.DataFrame(rows)
    cols = metrics + ([f"oos_{k}" for k in metrics] if cut is not None else [])
    for k in cols:
        if k not in out.columns:
            out[k] = np.nan
        out[k] = pd.to_numeric(out[k], errors="coerce")
    out = out.sort_values(metric, ascending=metric in LOWER_IS_BETTER, na_position="last", kind="stable")
    return out.reset_index(drop=True)
