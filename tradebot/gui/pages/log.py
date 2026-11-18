"""Protokoll: Bot-Ereignisse (Feed) mit Filtern und das Logfile der Anwendung."""
from __future__ import annotations

import html
import re
from collections import deque
from pathlib import Path

from nicegui import run, ui

from tradebot.gui.components.widgets import KIND_LABELS, MODE_LABELS, add_widget_css, kind_label, local_time
from tradebot.gui.layout import card, frame
from tradebot.gui.state import state
from tradebot.gui.theme import fmt_money

MAX_LINES = 500
LEVELS = {"all": "Alle Stufen", "info": "Info und höher", "warning": "Warnungen und Fehler", "error": "Nur Fehler"}
_LEVEL_RANK = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}
_LINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ (\w+)")

LOG_CSS = """
.tb-logview { font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
              font-size: 12px; line-height: 1.55; white-space: pre-wrap; word-break: break-word;
              background: var(--tb-surface-2); border: 1px solid var(--tb-border); border-radius: 10px;
              padding: 12px 14px; overflow: auto; }
.tb-logview .l-ERROR, .tb-logview .l-CRITICAL { color: var(--tb-neg); }
.tb-logview .l-WARNING { color: #f59e0b; }
.tb-logview .l-DEBUG { color: var(--tb-muted); }
.tb-logview .ts { color: var(--tb-muted); }
.tb-feed .q-table tbody td { padding-top: 6px; padding-bottom: 6px; }
.tb-feed .q-table__container { border-radius: 12px; }
"""


def tail_lines(path: Path, n: int = MAX_LINES) -> list[str]:
    """Last ``n`` lines of a text file (blocking)."""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        return [ln.rstrip("\n") for ln in deque(fh, maxlen=n)]


def _line_level(line: str, current: str) -> str:
    m = _LINE_RE.match(line)
    return m.group(1) if m else current  # continuation lines (tracebacks) keep the previous level


def render_log_html(lines: list[str], min_level: str = "all", query: str = "") -> tuple[str, int]:
    rank = {"all": -1, "info": 1, "warning": 2, "error": 3}[min_level]
    q = query.strip().lower()
    out, level, shown = [], "INFO", 0
    for ln in lines:
        level = _line_level(ln, level)
        if _LEVEL_RANK.get(level, 1) < rank or (q and q not in ln.lower()):
            continue
        esc = html.escape(ln)
        m = _LINE_RE.match(ln)
        if m:
            ts_end = ln.find(" ", 11)
            esc = f'<span class="ts">{html.escape(ln[:ts_end])}</span>{html.escape(ln[ts_end:])}'
        out.append(f'<div class="l-{level}">{esc}</div>')
        shown += 1
    return "".join(out), shown


COLUMNS = [
    {"name": "time", "label": "Zeit", "field": "time", "align": "left", "sortable": True,
     "headerStyle": "width:150px"},
    {"name": "mode", "label": "Modus", "field": "mode", "align": "left", "sortable": True,
     "headerStyle": "width:90px"},
    {"name": "kind", "label": "Art", "field": "kind_label", "align": "left", "sortable": True,
     "headerStyle": "width:130px"},
    {"name": "symbol", "label": "Symbol", "field": "symbol", "align": "left", "sortable": True,
     "headerStyle": "width:120px"},
    {"name": "message", "label": "Meldung", "field": "message", "align": "left",
     "style": "white-space:normal"},
    {"name": "pnl", "label": "Ergebnis", "field": "pnl_text", "align": "right", "headerStyle": "width:110px"},
]


@ui.page("/log")
def page() -> None:
    add_widget_css()
    ui.add_css(LOG_CSS)
    st = state()
    log_path = st.store.logs_dir / "tradebot.log"

    with frame("Protokoll", "Bot-Ereignisse und technisches Logfile"):
        with ui.tabs(value="events").props(
                "align=left inline-label no-caps active-color=primary indicator-color=primary dense").classes(
                "w-full").style("border-bottom:1px solid var(--tb-border)") as tabs:
            ui.tab("events", label="Ereignisse", icon="notifications")
            ui.tab("file", label="Logdatei", icon="description")
        with ui.tab_panels(tabs, value="events").props("keep-alive").classes("w-full").style(
                "background:transparent"):
            # ---- events -----------------------------------------------------------------
            with ui.tab_panel("events").classes("p-0 gap-4"):
                with card("gap-3"):
                    with ui.row().classes("w-full items-center gap-3"):
                        mode_sel = ui.select({"all": "Alle Modi", "test": "TEST", "live": "LIVE", "app": "App"},
                                             value="all", label="Modus").props("outlined dense").classes("w-36")
                        kind_sel = ui.select({"all": "Alle Arten"}, value="all", label="Art").props(
                            "outlined dense").classes("w-44")
                        search = ui.input(placeholder="Suchen in Meldung, Symbol …").props(
                            "outlined dense clearable").classes("w-64")
                        with search.add_slot("prepend"):
                            ui.icon("search", size="18px")
                        ui.space()
                        count_lbl = ui.label("").classes("tb-muted text-xs")
                        auto_sw = ui.switch("Auto-Aktualisierung", value=True).props("dense")
                    table = ui.table(columns=COLUMNS, rows=[], row_key="seq",
                                     pagination={"rowsPerPage": 50}).props(
                        'flat dense no-data-label="Noch keine Ereignisse – sobald ein Bot läuft, erscheinen '
                        'hier Käufe, Verkäufe und Meldungen." rows-per-page-label="Zeilen pro Seite" '
                        'no-results-label="Keine Treffer für diesen Filter"').classes("w-full tb-feed tb-table")
                    table.add_slot("body-cell-mode", r"""
                        <q-td :props="props">
                          <span :class="'tb-pill text-[10px] px-2 py-0 ' + (props.row.mode === 'live' ? 'tb-pill-neg'
                                : props.row.mode === 'test' ? 'tb-pill-pos' : 'tb-pill-neutral')">
                            {{ props.row.mode_label }}</span>
                        </q-td>""")
                    table.add_slot("body-cell-kind", r"""
                        <q-td :props="props">
                          <span :class="props.row.level === 'error' ? 'tb-neg font-semibold'
                                : props.row.level === 'warning' ? 'text-warning font-semibold' : 'font-medium'">
                            {{ props.row.kind_label }}</span>
                        </q-td>""")
                    table.add_slot("body-cell-time", r"""
                        <q-td :props="props"><span class="tb-mono tb-muted text-xs">{{ props.row.time }}</span></q-td>
                        """)
                    table.add_slot("body-cell-pnl", r"""
                        <q-td :props="props">
                          <span :class="props.row.pnl > 0 ? 'tb-pos font-semibold' : props.row.pnl < 0
                                ? 'tb-neg font-semibold' : 'tb-muted'">{{ props.row.pnl_text }}</span>
                        </q-td>""")

            # ---- log file ---------------------------------------------------------------
            with ui.tab_panel("file").classes("p-0 gap-4"):
                with card("gap-3"):
                    with ui.row().classes("w-full items-center gap-3"):
                        lvl_sel = ui.select(LEVELS, value="all", label="Stufe").props("outlined dense").classes(
                            "w-52")
                        file_search = ui.input(placeholder="Suchen …").props("outlined dense clearable").classes(
                            "w-64")
                        refresh_btn = ui.button("Aktualisieren", icon="refresh").props(
                            "unelevated no-caps color=primary")
                        ui.space()
                        file_info = ui.label("").classes("tb-muted text-xs")
                        file_auto = ui.switch("Auto", value=False).props("dense")
                    ui.label(f"{log_path} · letzte {MAX_LINES} Zeilen").classes("tb-muted text-xs tb-mono")
                    view = ui.html("").classes("tb-logview w-full").style(
                        "height: calc(100vh - 380px); min-height: 320px")

    # ---- events behaviour -------------------------------------------------------------------
    shown = {"seq": None, "filter": None}

    def rows() -> list[dict]:
        out = []
        for it in reversed(st.feed.recent(1000)):  # newest first
            pnl = (it.data or {}).get("pnl")
            out.append({
                "seq": it.seq, "time": local_time(it.time), "mode": it.mode,
                "mode_label": MODE_LABELS.get(it.mode, it.mode.upper()), "kind": it.kind,
                "kind_label": kind_label(it.kind), "level": it.level, "symbol": it.symbol or "",
                "message": it.message, "pnl": pnl, "pnl_text": fmt_money(pnl) if pnl is not None else "",
            })
        return out

    def refresh_events(force: bool = False) -> None:
        last = st.feed.last_seq()
        flt = (mode_sel.value, kind_sel.value, (search.value or "").strip().lower())
        if not force and last == shown["seq"] and flt == shown["filter"]:
            return
        shown.update(seq=last, filter=flt)
        all_rows = rows()
        kinds = sorted({r["kind"] for r in all_rows} | {"buy", "sell", "stop_loss", "take_profit", "error"},
                       key=lambda k: kind_label(k))
        opts = {"all": "Alle Arten", **{k: KIND_LABELS.get(k, kind_label(k)) for k in kinds}}
        if opts != kind_sel.options:
            kind_sel.set_options(opts, value=kind_sel.value if kind_sel.value in opts else "all")
        m, k, q = flt
        filtered = [r for r in all_rows
                    if (m == "all" or r["mode"] == m)
                    and (k == "all" or r["kind"] == k)
                    and (not q or q in f"{r['message']} {r['symbol']} {r['kind_label']}".lower())]
        table.rows = filtered
        table.update()
        count_lbl.set_text(f"{len(filtered)} von {len(all_rows)} Ereignissen")

    mode_sel.on_value_change(lambda _: refresh_events())
    kind_sel.on_value_change(lambda _: refresh_events())
    search.on_value_change(lambda _: refresh_events())
    ev_timer = ui.timer(2.0, refresh_events)
    auto_sw.on_value_change(lambda e: setattr(ev_timer, "active", bool(e.value)))
    refresh_events(force=True)

    # ---- log file behaviour ---------------------------------------------------------------
    cache: dict = {"lines": []}

    async def load_file(scroll: bool = True) -> None:
        refresh_btn.props("loading")
        try:
            cache["lines"] = await run.io_bound(tail_lines, log_path, MAX_LINES)
        except OSError as exc:
            view.set_content(f'<div class="l-ERROR">Logdatei konnte nicht gelesen werden: {html.escape(str(exc))}</div>')
            return
        finally:
            refresh_btn.props(remove="loading")
        render_file(scroll)

    def render_file(scroll: bool = True) -> None:
        lines = cache["lines"]
        if not lines:
            view.set_content('<div class="ts">Die Logdatei ist noch leer.</div>')
            file_info.set_text("")
            return
        content, n = render_log_html(lines, lvl_sel.value or "all", file_search.value or "")
        view.set_content(content or '<div class="ts">Keine Zeilen für diesen Filter.</div>')
        file_info.set_text(f"{n} von {len(lines)} Zeilen")
        if scroll:
            ui.run_javascript(f"const e=getHtmlElement({view.id}); if(e) e.scrollTop=e.scrollHeight;")

    refresh_btn.on_click(lambda: load_file())
    lvl_sel.on_value_change(lambda _: render_file())
    file_search.on_value_change(lambda _: render_file(scroll=False))
    file_timer = ui.timer(5.0, lambda: load_file(), active=False)
    file_auto.on_value_change(lambda e: setattr(file_timer, "active", bool(e.value)))
    ui.timer(0.05, load_file, once=True)
