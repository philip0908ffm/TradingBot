"""Visual design tokens, global CSS and formatting helpers for the GUI."""
from __future__ import annotations

import math

from nicegui import ui

PRIMARY = "#3b82f6"
POSITIVE = "#16c784"
NEGATIVE = "#ea3943"
WARNING = "#f59e0b"

CSS = """
:root {
  --tb-bg: #0b0e13; --tb-surface: #121620; --tb-surface-2: #171c27; --tb-border: #222936;
  --tb-text: #e6e9ef; --tb-muted: #8b95a5; --tb-pos: #16c784; --tb-neg: #ea3943; --tb-primary: #3b82f6;
}
body.body--light {
  --tb-bg: #f4f6f9; --tb-surface: #ffffff; --tb-surface-2: #f8fafc; --tb-border: #e3e7ee;
  --tb-text: #111827; --tb-muted: #6b7280;
}
body { background: var(--tb-bg); color: var(--tb-text);
       font-family: Inter, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
       font-feature-settings: "tnum" 1, "cv11" 1; }
.q-header { background: var(--tb-surface) !important; color: var(--tb-text) !important;
            border-bottom: 1px solid var(--tb-border); }
.q-drawer { background: var(--tb-surface) !important; border-right: 1px solid var(--tb-border) !important; }
.tb-card { background: var(--tb-surface); border: 1px solid var(--tb-border); border-radius: 14px;
           box-shadow: none; padding: 18px; }
.tb-card.q-card { box-shadow: none; }
.tb-muted { color: var(--tb-muted); }
.tb-pos { color: var(--tb-pos); } .tb-neg { color: var(--tb-neg); }
.tb-title { font-size: 1.45rem; font-weight: 650; letter-spacing: -0.01em; }
.tb-section { font-size: 0.78rem; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--tb-muted); }
.tb-kpi-label { font-size: .78rem; color: var(--tb-muted); }
.tb-kpi-value { font-size: 1.45rem; font-weight: 650; letter-spacing: -0.01em; }
.tb-nav .q-item { border-radius: 10px; margin: 2px 10px; min-height: 40px; color: var(--tb-muted); }
.tb-nav .q-item.tb-active { background: color-mix(in srgb, var(--tb-primary) 16%, transparent); color: var(--tb-text); }
.tb-nav .q-item.tb-active .q-icon { color: var(--tb-primary); }
.tb-chip { border-radius: 999px; padding: 2px 10px; font-size: .75rem; font-weight: 600; border: 1px solid var(--tb-border); }
.tb-live-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
@keyframes tbpulse { 0% { box-shadow: 0 0 0 0 rgba(234,57,67,.6);} 70% { box-shadow: 0 0 0 8px rgba(234,57,67,0);} 100% { box-shadow: 0 0 0 0 rgba(234,57,67,0);} }
.tb-pulse { animation: tbpulse 1.6s infinite; }
.q-table__card { background: var(--tb-surface) !important; box-shadow: none !important; }
.q-table { color: var(--tb-text); }
.q-table thead th { color: var(--tb-muted); font-weight: 600; font-size: .75rem; text-transform: uppercase; letter-spacing: .04em; }
.q-field--outlined .q-field__control { border-radius: 10px; }
.q-btn { border-radius: 10px; text-transform: none; font-weight: 600; letter-spacing: 0; }
.tb-danger-zone { border: 1px solid color-mix(in srgb, var(--tb-neg) 45%, transparent);
                  background: color-mix(in srgb, var(--tb-neg) 7%, var(--tb-surface)); }
"""


def apply_theme(dark: bool = True) -> None:
    ui.colors(primary=PRIMARY, positive=POSITIVE, negative=NEGATIVE, warning=WARNING, dark="#0b0e13")
    ui.add_css(CSS)
    ui.dark_mode(dark)


# ---------------------------------------------------------------------------------------
# formatting (German locale)
# ---------------------------------------------------------------------------------------

def _de(s: str) -> str:
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_num(v: float | None, digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "–"
    return _de(f"{v:,.{digits}f}")


def fmt_money(v: float | None, currency: str = "", digits: int = 2) -> str:
    s = fmt_num(v, digits)
    return f"{s} {currency}".strip() if s != "–" else s


def fmt_price(v: float | None) -> str:
    if v is None:
        return "–"
    a = abs(v)
    digits = 2 if a >= 100 else 4 if a >= 1 else 6 if a >= 0.01 else 8
    return fmt_num(v, digits)


def fmt_pct(v: float | None, digits: int = 2, sign: bool = True) -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "–"
    return _de(f"{v:+,.{digits}f}" if sign else f"{v:,.{digits}f}") + " %"


def pnl_class(v: float | None) -> str:
    if v is None or v == 0:
        return ""
    return "tb-pos" if v > 0 else "tb-neg"
