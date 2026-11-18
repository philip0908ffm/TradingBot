"""Strategy evaluation engine.

Turns a declarative :class:`~tradebot.strategy.models.Strategy` into boolean entry/exit
signal series on an OHLCV frame. Used by the backtester AND the live/paper engine, so
both see exactly the same signals.

Semantics
---------
* All conditions are evaluated per bar on *closed* candles (value at row t uses data up to
  and including row t only – no look-ahead).
* ``crosses_above``: ``left[t] > right[t] and left[t-1] <= right[t-1]``
  (``crosses_below`` analogous).
* ``offset`` shifts an operand N bars into the past.
* NaN anywhere (indicator warm-up, offset, first bar of a cross) evaluates to ``False``.
* Empty rule group: never True (neither entry nor exit).
* ``logic`` "all" = AND, "any" = OR.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from tradebot.core.models import OHLCV_COLUMNS, TIMEFRAMES
from tradebot.indicators import registry  # package import registers all indicators
from tradebot.strategy.models import (
    OPERATORS,
    PRICE_FIELDS,
    Condition,
    Operand,
    RuleGroup,
    Strategy,
)

PRICE_FIELD_LABELS = {
    "open": "Eröffnungskurs",
    "high": "Hoch",
    "low": "Tief",
    "close": "Schlusskurs",
    "volume": "Volumen",
}

LOGIC_LABELS = {"all": "alle", "any": "mindestens eine"}

MIN_WARMUP = 50


# --------------------------------------------------------------------------- helpers
def _normalized_params(key: str, params: dict | None) -> dict:
    """Registry defaults overridden by ``params`` (unknown names dropped, types coerced)."""
    spec = registry.get(key)
    p = spec.default_params()
    if params:
        p.update({k: v for k, v in params.items() if k in p})
    for prm in spec.params:
        p[prm.name] = int(p[prm.name]) if prm.type == "int" else float(p[prm.name])
    return p


def _output(op: Operand) -> str:
    spec = registry.get(op.indicator)
    return op.output or spec.outputs[0]


def _cache_key(key: str, params: dict | None) -> tuple:
    return (key, tuple(sorted(_normalized_params(key, params).items())))


def _iter_operands(strategy: Strategy):
    for group in (strategy.entry, strategy.exit):
        for cond in group.conditions:
            yield cond.left
            yield cond.right


def _fmt_num(v) -> str:
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return str(v)


# --------------------------------------------------------------------------- analysis
def required_indicators(strategy: Strategy) -> list[tuple[str, dict]]:
    """Unique (indicator key, normalized params) pairs used by the strategy."""
    seen: set[tuple] = set()
    result: list[tuple[str, dict]] = []
    for op in _iter_operands(strategy):
        if op.kind != "indicator" or op.indicator not in registry.REGISTRY:
            continue
        ck = _cache_key(op.indicator, op.params)
        if ck in seen:
            continue
        seen.add(ck)
        result.append((op.indicator, _normalized_params(op.indicator, op.params)))
    return result


def warmup_bars(strategy: Strategy) -> int:
    """Heuristic number of history bars needed before signals are reliable:
    3 x the largest integer parameter of any used indicator + the largest offset (min 50)."""
    largest = 0
    for key, params in required_indicators(strategy):
        spec = registry.get(key)
        for prm in spec.params:
            if prm.type == "int":
                largest = max(largest, int(params[prm.name]))
    max_offset = max((max(int(op.offset or 0), 0) for op in _iter_operands(strategy)), default=0)
    return max(MIN_WARMUP, 3 * largest + max_offset + 1)


# --------------------------------------------------------------------------- evaluation
def _indicator_frame(key: str, params: dict | None, df: pd.DataFrame, cache: dict) -> pd.DataFrame:
    ck = _cache_key(key, params)
    if ck not in cache:
        cols = [c for c in OHLCV_COLUMNS if c in df.columns]
        cache[ck] = registry.compute(key, df[cols], params)
    return cache[ck]


def operand_series(op: Operand, df: pd.DataFrame, cache: dict | None = None) -> pd.Series:
    """Float series for one operand (offset applied)."""
    if cache is None:
        cache = {}
    if op.kind == "value":
        if op.value is None:
            raise ValueError("Wert fehlt")
        s = pd.Series(float(op.value), index=df.index, dtype=float)
    elif op.kind == "price":
        if op.field not in PRICE_FIELDS:
            raise ValueError(f"Unbekanntes Preisfeld {op.field!r}")
        s = df[op.field].astype(float)
    elif op.kind == "indicator":
        frame = _indicator_frame(op.indicator, op.params, df, cache)
        s = frame[_output(op)].astype(float)
    else:
        raise ValueError(f"Unbekannter Operanden-Typ {op.kind!r}")
    if op.offset:
        s = s.shift(int(op.offset))
    return s


def evaluate_condition(cond: Condition, df: pd.DataFrame, cache: dict | None = None) -> pd.Series:
    """Boolean series (NaN -> False) for one condition."""
    if cache is None:
        cache = {}
    left = operand_series(cond.left, df, cache).to_numpy(dtype=float)
    right = operand_series(cond.right, df, cache).to_numpy(dtype=float)
    with np.errstate(invalid="ignore"):
        if cond.op == ">":
            res = left > right
        elif cond.op == "<":
            res = left < right
        elif cond.op == ">=":
            res = left >= right
        elif cond.op == "<=":
            res = left <= right
        elif cond.op in ("crosses_above", "crosses_below"):
            pl = np.concatenate([[np.nan], left[:-1]])
            pr = np.concatenate([[np.nan], right[:-1]])
            if cond.op == "crosses_above":
                res = (left > right) & (pl <= pr)
            else:
                res = (left < right) & (pl >= pr)
        else:
            raise ValueError(f"Unbekannter Operator {cond.op!r}")
    # comparisons with NaN are already False in numpy; be explicit anyway
    valid = ~(np.isnan(left) | np.isnan(right))
    return pd.Series(res & valid, index=df.index, dtype=bool)


def evaluate_group(group: RuleGroup, df: pd.DataFrame, cache: dict | None = None) -> pd.Series:
    if cache is None:
        cache = {}
    if not group.conditions:
        return pd.Series(False, index=df.index, dtype=bool)
    parts = [evaluate_condition(c, df, cache).to_numpy() for c in group.conditions]
    if group.logic == "any":
        res = np.logical_or.reduce(parts)
    else:
        res = np.logical_and.reduce(parts)
    return pd.Series(res, index=df.index, dtype=bool)


def compute_signals(strategy: Strategy, df: pd.DataFrame) -> pd.DataFrame:
    """Copy of ``df`` + one column per used indicator output (see ``registry.column_name``,
    without offset) + bool columns ``entry`` and ``exit``."""
    cache: dict = {}
    out = df.copy()
    for op in _iter_operands(strategy):
        if op.kind != "indicator":
            continue
        frame = _indicator_frame(op.indicator, op.params, df, cache)
        output = _output(op)
        col = registry.column_name(op.indicator, op.params, output)
        if col not in out.columns:
            out[col] = frame[output].astype(float).to_numpy()
    out["entry"] = evaluate_group(strategy.entry, df, cache).to_numpy()
    out["exit"] = evaluate_group(strategy.exit, df, cache).to_numpy()
    return out


def indicator_columns(strategy: Strategy) -> list[str]:
    """Names of the indicator columns ``compute_signals`` adds (in order, unique)."""
    cols: list[str] = []
    for op in _iter_operands(strategy):
        if op.kind == "indicator" and op.indicator in registry.REGISTRY:
            col = registry.column_name(op.indicator, op.params, _output(op))
            if col not in cols:
                cols.append(col)
    return cols


# --------------------------------------------------------------------------- validation
def _validate_operand(op: Operand, where: str) -> list[str]:
    errors: list[str] = []
    if op.kind == "indicator":
        if not op.indicator:
            return [f"{where}: Kein Indikator ausgewählt."]
        if op.indicator not in registry.REGISTRY:
            return [f"{where}: Unbekannter Indikator '{op.indicator}'."]
        spec = registry.get(op.indicator)
        if op.output is not None and op.output not in spec.outputs:
            errors.append(
                f"{where}: Unbekannte Ausgabe '{op.output}' für {spec.name} "
                f"(möglich: {', '.join(spec.outputs)})."
            )
        known = {p.name: p for p in spec.params}
        for name, value in (op.params or {}).items():
            prm = known.get(name)
            if prm is None:
                errors.append(f"{where}: Unbekannter Parameter '{name}' für {spec.name}.")
                continue
            try:
                v = float(value)
            except (TypeError, ValueError):
                errors.append(f"{where}: Parameter '{name}' von {spec.name} ist keine Zahl.")
                continue
            if not math.isfinite(v) or v < prm.min or v > prm.max:
                errors.append(
                    f"{where}: Parameter '{name}' von {spec.name} muss zwischen "
                    f"{_fmt_num(prm.min)} und {_fmt_num(prm.max)} liegen (ist {_fmt_num(value)})."
                )
    elif op.kind == "price":
        if op.field not in PRICE_FIELDS:
            errors.append(f"{where}: Unbekanntes Preisfeld '{op.field}'.")
    elif op.kind == "value":
        if op.value is None:
            errors.append(f"{where}: Wert fehlt.")
        else:
            try:
                if not math.isfinite(float(op.value)):
                    errors.append(f"{where}: Wert ist keine gültige Zahl.")
            except (TypeError, ValueError):
                errors.append(f"{where}: Wert ist keine Zahl.")
    else:
        errors.append(f"{where}: Unbekannter Operanden-Typ '{op.kind}'.")
    try:
        if int(op.offset or 0) < 0 or int(op.offset or 0) != float(op.offset or 0):
            errors.append(f"{where}: Versatz (Offset) muss eine ganze Zahl ≥ 0 sein.")
    except (TypeError, ValueError):
        errors.append(f"{where}: Versatz (Offset) muss eine ganze Zahl ≥ 0 sein.")
    return errors


def _validate_group(group: RuleGroup, label: str) -> list[str]:
    errors: list[str] = []
    if group.logic not in ("all", "any"):
        errors.append(f"{label}: Unbekannte Verknüpfung '{group.logic}' (erlaubt: all, any).")
    for i, cond in enumerate(group.conditions, start=1):
        where = f"{label}, Bedingung {i}"
        if cond.op not in OPERATORS:
            errors.append(f"{where}: Unbekannter Operator '{cond.op}'.")
        errors += _validate_operand(cond.left, f"{where} (links)")
        errors += _validate_operand(cond.right, f"{where} (rechts)")
        if cond.left.kind == "value" and cond.right.kind == "value":
            errors.append(f"{where}: Zwei feste Werte zu vergleichen ist sinnlos.")
    return errors


def validate(strategy: Strategy) -> list[str]:
    """German error messages; empty list = strategy is valid."""
    errors: list[str] = []
    if not (strategy.name or "").strip():
        errors.append("Die Strategie braucht einen Namen.")
    if strategy.timeframe not in TIMEFRAMES:
        errors.append(
            f"Unbekannter Zeitrahmen '{strategy.timeframe}' (erlaubt: {', '.join(TIMEFRAMES)})."
        )
    if not strategy.entry.conditions:
        errors.append("Keine Einstiegsbedingungen definiert.")
    errors += _validate_group(strategy.entry, "Einstieg")
    errors += _validate_group(strategy.exit, "Ausstieg")

    r = strategy.risk

    def num(v) -> float | None:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) else None

    ps = num(r.position_size_pct)
    if ps is None or ps <= 0 or ps > 100:
        errors.append("Positionsgröße muss zwischen 0 und 100 % liegen.")
    if r.take_profit_pct is not None:
        v = num(r.take_profit_pct)
        if v is None or v <= 0:
            errors.append("Take-Profit muss größer als 0 % sein (oder leer lassen).")
    if r.stop_loss_pct is not None:
        v = num(r.stop_loss_pct)
        if v is None or v <= 0 or v >= 100:
            errors.append("Stop-Loss muss zwischen 0 und 100 % liegen (oder leer lassen).")
    if r.trailing_stop_pct is not None:
        v = num(r.trailing_stop_pct)
        if v is None or v <= 0 or v >= 100:
            errors.append("Trailing-Stop muss zwischen 0 und 100 % liegen (oder leer lassen).")
    if r.max_holding_bars is not None:
        v = num(r.max_holding_bars)
        if v is None or v < 1 or v != int(v):
            errors.append("Maximale Haltedauer muss eine ganze Zahl ≥ 1 sein (oder leer lassen).")
    v = num(r.max_open_positions)
    if v is None or v < 1 or v != int(v):
        errors.append("Maximale Anzahl offener Positionen muss eine ganze Zahl ≥ 1 sein.")
    v = num(r.fee_pct)
    if v is None or v < 0 or v >= 100:
        errors.append("Gebühr muss zwischen 0 und 100 % liegen.")
    v = num(r.slippage_pct)
    if v is None or v < 0 or v >= 100:
        errors.append("Slippage muss zwischen 0 und 100 % liegen.")
    if (not strategy.exit.conditions and not r.take_profit_pct and not r.stop_loss_pct
            and not r.trailing_stop_pct and not r.max_holding_bars):
        errors.append(
            "Kein Ausstieg definiert: Bitte Ausstiegsbedingungen oder Take-Profit/Stop-Loss/"
            "Trailing-Stop/Haltedauer festlegen."
        )
    return errors


# --------------------------------------------------------------------------- description
def describe_operand(op: Operand) -> str:
    if op.kind == "value":
        text = _fmt_num(op.value) if op.value is not None else "?"
    elif op.kind == "price":
        text = PRICE_FIELD_LABELS.get(op.field, op.field)
    elif op.kind == "indicator":
        if op.indicator in registry.REGISTRY:
            spec = registry.get(op.indicator)
            p = _normalized_params(op.indicator, op.params)
            args = ",".join(_fmt_num(p[x.name]) for x in spec.params)
            text = op.indicator.upper() + (f"({args})" if args else "")
            out = op.output or spec.outputs[0]
            if len(spec.outputs) > 1:
                text += f" {out}"
        else:
            text = f"{op.indicator}?"
    else:
        text = "?"
    if op.offset:
        n = int(op.offset)
        text += f" (vor {n} Kerze{'n' if n != 1 else ''})"
    return text


def describe_condition(cond: Condition) -> str:
    op = OPERATORS.get(cond.op, cond.op)
    return f"{describe_operand(cond.left)} {op} {describe_operand(cond.right)}"


def describe_group(group: RuleGroup) -> list[str]:
    return [describe_condition(c) for c in group.conditions]


def describe_strategy(strategy: Strategy) -> str:
    lines = [f"Strategie „{strategy.name}“ (Zeitrahmen {strategy.timeframe})"]
    if strategy.description:
        lines.append(strategy.description)

    def group_text(group: RuleGroup, title: str) -> None:
        if not group.conditions:
            lines.append(f"{title}: keine Bedingungen")
            return
        if len(group.conditions) == 1:
            lines.append(f"{title}, wenn:")
        else:
            lines.append(f"{title}, wenn {LOGIC_LABELS.get(group.logic, group.logic)} "
                         f"der folgenden Bedingungen erfüllt sind:")
        lines.extend(f"  • {d}" for d in describe_group(group))

    group_text(strategy.entry, "Kaufen")
    group_text(strategy.exit, "Verkaufen")

    r = strategy.risk
    risk = [f"Positionsgröße {_fmt_num(r.position_size_pct)} % des Kapitals"]
    if r.take_profit_pct:
        risk.append(f"Take-Profit {_fmt_num(r.take_profit_pct)} %")
    if r.stop_loss_pct:
        risk.append(f"Stop-Loss {_fmt_num(r.stop_loss_pct)} %")
    if r.trailing_stop_pct:
        risk.append(f"Trailing-Stop {_fmt_num(r.trailing_stop_pct)} %")
    if r.max_holding_bars:
        risk.append(f"max. Haltedauer {int(r.max_holding_bars)} Kerzen")
    risk.append(f"Gebühr {_fmt_num(r.fee_pct)} % / Slippage {_fmt_num(r.slippage_pct)} % je Seite")
    lines.append("Risiko: " + ", ".join(risk))
    return "\n".join(lines)
