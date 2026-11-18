"""Indicator registry.

Each indicator is a pure function ``func(df: pd.DataFrame, **params) -> pd.DataFrame``.
``df`` has OHLCV columns; the returned frame shares df's index and has exactly the
columns listed in ``outputs`` (float, NaN during warm-up). No look-ahead allowed:
the value at row t may only use data up to and including row t.

Register with the ``@indicator(...)`` decorator. Modules containing indicators must be
imported in ``tradebot/indicators/__init__.py`` so that registration happens.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

CATEGORIES = ["Trend", "Momentum", "Volatility", "Volume", "Other"]


@dataclass
class Param:
    name: str
    default: float | int
    min: float | int
    max: float | int
    step: float | int = 1
    label: str = ""
    type: str = "int"  # "int" | "float"

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class IndicatorSpec:
    key: str  # e.g. "rsi"
    name: str  # e.g. "Relative Strength Index"
    category: str  # one of CATEGORIES
    params: list[Param]
    outputs: list[str]  # e.g. ["macd", "signal", "hist"]
    func: Callable[..., pd.DataFrame]
    overlay: bool = False  # True: plotted on the price chart (MA, Bollinger...), False: own panel
    description: str = ""  # short German explanation for the GUI
    # Optional horizontal reference lines for oscillators (e.g. RSI 30/70)
    levels: list[float] = field(default_factory=list)

    def default_params(self) -> dict:
        return {p.name: p.default for p in self.params}


REGISTRY: dict[str, IndicatorSpec] = {}


def indicator(
    key: str,
    name: str,
    category: str,
    params: list[Param],
    outputs: list[str],
    overlay: bool = False,
    description: str = "",
    levels: list[float] | None = None,
):
    def deco(func):
        if key in REGISTRY:
            raise ValueError(f"Indicator {key!r} registered twice")
        REGISTRY[key] = IndicatorSpec(key, name, category, params, outputs, func, overlay,
                                      description, levels or [])
        return func

    return deco


def get(key: str) -> IndicatorSpec:
    try:
        return REGISTRY[key]
    except KeyError:
        raise KeyError(f"Unknown indicator {key!r}") from None


def compute(key: str, df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """Compute indicator ``key`` with params (missing params filled with defaults)."""
    spec = get(key)
    p = spec.default_params()
    if params:
        p.update({k: v for k, v in params.items() if k in p})
    for prm in spec.params:  # coerce types
        p[prm.name] = int(p[prm.name]) if prm.type == "int" else float(p[prm.name])
    out = spec.func(df, **p)
    return out[spec.outputs]


def column_name(key: str, params: dict | None, output: str) -> str:
    """Stable column name, e.g. 'rsi(14).rsi' or 'macd(12,26,9).signal'."""
    spec = get(key)
    p = spec.default_params()
    if params:
        p.update({k: v for k, v in params.items() if k in p})
    args = ",".join(f"{p[x.name]:g}" for x in spec.params)
    return f"{key}({args}).{output}"
