"""Small reusable presentation helpers (badges, sparklines, event labels)."""
from __future__ import annotations

from datetime import datetime, timezone

from nicegui import ui

from tradebot.gui.theme import NEGATIVE, POSITIVE, PRIMARY

WIDGET_CSS = """
.tb-badge { display:inline-flex; align-items:center; gap:4px; border-radius:6px; padding:1px 7px;
            font-size:.7rem; font-weight:600; letter-spacing:.02em; line-height:1.4;
            background: var(--tb-surface-2); border:1px solid var(--tb-border); color: var(--tb-muted); }
.tb-pill { display:inline-flex; align-items:center; gap:5px; border-radius:999px; padding:2px 10px;
           font-size:.75rem; font-weight:600; line-height:1.5; }
.tb-pill-pos { color: var(--tb-pos); background: color-mix(in srgb, var(--tb-pos) 14%, transparent); }
.tb-pill-neg { color: var(--tb-neg); background: color-mix(in srgb, var(--tb-neg) 14%, transparent); }
.tb-pill-neutral { color: var(--tb-muted); background: color-mix(in srgb, var(--tb-muted) 14%, transparent); }
.tb-pill-primary { color: var(--tb-primary); background: color-mix(in srgb, var(--tb-primary) 14%, transparent); }
.tb-pill-warn { color: #f59e0b; background: color-mix(in srgb, #f59e0b 16%, transparent); }
.tb-mono { font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.tb-hover:hover { border-color: color-mix(in srgb, var(--tb-primary) 45%, var(--tb-border)); }
.tb-divider { border-top: 1px solid var(--tb-border); }
.tb-icon-box { width:38px; height:38px; border-radius:10px; display:flex; align-items:center; justify-content:center; }
.tb-skel { background: var(--tb-surface-2) !important; }
.tb-field-hint { font-size:.75rem; color: var(--tb-muted); line-height:1.35; }
.tb-table .q-table__top, .tb-table .q-table__bottom { color: var(--tb-muted); }
.tb-table tbody td { font-size: .82rem; }
.tb-table.q-table--dark, .tb-table .q-table--dark { background: var(--tb-surface) !important; }
.tb-table th, .tb-table td { border-color: var(--tb-border) !important; }
"""

KIND_LABELS = {
    "buy": "Kauf", "sell": "Verkauf", "signal": "Verkauf", "take_profit": "Take-Profit",
    "stop_loss": "Stop-Loss", "trailing_stop": "Trailing-Stop", "max_bars": "Zeitausstieg",
    "manual": "Manuell", "error": "Fehler", "warning": "Warnung", "kill_switch": "Notschalter",
    "info": "Info", "trade": "Trade", "start": "Start", "stop": "Stopp",
}
KIND_ICONS = {
    "buy": ("north_east", POSITIVE), "sell": ("south_east", NEGATIVE), "signal": ("south_east", NEGATIVE),
    "take_profit": ("flag", POSITIVE), "stop_loss": ("shield", NEGATIVE),
    "trailing_stop": ("trending_down", NEGATIVE), "max_bars": ("timer", "#8b95a5"),
    "manual": ("pan_tool", PRIMARY), "error": ("error", NEGATIVE), "warning": ("warning", "#f59e0b"),
    "kill_switch": ("power_settings_new", NEGATIVE), "info": ("info", "#8b95a5"),
}
MODE_LABELS = {"test": "TEST", "live": "LIVE", "app": "APP"}


def add_widget_css() -> None:
    ui.add_css(WIDGET_CSS)


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind.replace("_", " ").capitalize())


def kind_icon(kind: str, level: str = "info") -> tuple[str, str]:
    if kind in KIND_ICONS:
        return KIND_ICONS[kind]
    return KIND_ICONS.get(level, KIND_ICONS["info"])


def badge(text: str, classes: str = "") -> ui.label:
    return ui.label(text).classes(f"tb-badge {classes}")


def pill(text: str, tone: str = "neutral") -> ui.label:
    return ui.label(text).classes(f"tb-pill tb-pill-{tone}")


def mode_badge(mode: str) -> ui.label:
    tone = {"live": "neg", "test": "pos"}.get(mode, "neutral")
    return pill(MODE_LABELS.get(mode, mode.upper()), tone).classes("text-[10px] px-2 py-0")


def local_time(dt: datetime, fmt: str = "%d.%m. %H:%M:%S") -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime(fmt)


def sparkline_svg(values: list[float], color: str | None = None, width: int = 240, height: int = 48) -> str:
    """Inline SVG sparkline with a soft area fill (scales to its container width)."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return f'<svg width="100%" height="{height}"></svg>'
    color = color or (POSITIVE if vals[-1] >= vals[0] else NEGATIVE)
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    pad = 3
    step = width / (len(vals) - 1)
    pts = [(i * step, pad + (hi - v) / span * (height - 2 * pad)) for i, v in enumerate(vals)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"0,{height} " + line + f" {width},{height}"
    gid = f"g{abs(hash((tuple(vals[-5:]), color))) % 10**8}"
    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" width="100%" height="{height}" '
        f'style="display:block">'
        f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="{color}" stop-opacity="0.28"/>'
        f'<stop offset="100%" stop-color="{color}" stop-opacity="0"/></linearGradient></defs>'
        f'<polygon points="{area}" fill="url(#{gid})"/>'
        f'<polyline points="{line}" fill="none" stroke="{color}" stroke-width="1.6" '
        f'stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/></svg>'
    )


def empty_state(icon: str, title: str, text: str = "") -> None:
    with ui.column().classes("w-full items-center justify-center gap-1 py-8"):
        ui.icon(icon, size="34px").classes("tb-muted opacity-70")
        ui.label(title).classes("text-sm font-medium")
        if text:
            ui.label(text).classes("tb-muted text-xs text-center max-w-[420px]")


def is_dark() -> bool:
    from tradebot.gui.state import state

    return getattr(state().settings, "theme", "dark") != "light"
