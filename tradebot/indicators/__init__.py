"""Technical indicator library (pure pandas/numpy).

Importing this package registers all indicators in :data:`REGISTRY`.
"""
from . import momentum, other, trend, volatility, volume  # noqa: F401  (registration)
from .registry import CATEGORIES, REGISTRY, IndicatorSpec, Param, column_name, compute, get

__all__ = ["REGISTRY", "compute", "get", "column_name", "Param", "IndicatorSpec", "CATEGORIES"]
