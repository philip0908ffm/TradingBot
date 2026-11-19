"""Strategien page: list of saved strategies + presets, visual editor with live validation."""
from __future__ import annotations

import copy
import json
from urllib.parse import quote

from nicegui import app, ui

from tradebot.core.settings import slugify
from tradebot.gui.components.strategy_editor import StrategyEditor, add_editor_css
from tradebot.gui.components.strategy_labels import TIMEFRAME_LABELS
from tradebot.gui.layout import card, frame
from tradebot.gui.state import state
from tradebot.strategy.engine import describe_strategy, validate, warmup_bars
from tradebot.strategy.models import Strategy
from tradebot.strategy.presets import PRESETS

DRAFT_KEY = "strategy_draft"

PAGE_CSS = """
.tb-slist .q-item { border-radius: 10px; min-height: 44px; padding: 6px 10px; }
.tb-slist .q-item.tb-sel { background: color-mix(in srgb, var(--tb-primary) 14%, transparent); }
.tb-slist .q-item.tb-sel .q-item__label:first-child { color: var(--tb-primary); }
.tb-desc-line { font-size: .86rem; line-height: 1.45; }
.tb-desc-bullet { font-size: .84rem; line-height: 1.4; padding-left: 14px; position: relative; }
.tb-desc-bullet:before { content: ""; position: absolute; left: 2px; top: .58em; width: 5px; height: 5px;
                         border-radius: 50%; background: var(--tb-primary); }
.tb-status-ok { color: var(--tb-pos); } .tb-status-err { color: var(--tb-neg); }
.tb-unsaved { background: color-mix(in srgb, #f59e0b 18%, transparent); color: #f59e0b; border-radius: 999px;
              padding: 2px 10px; font-size: .72rem; font-weight: 600; }
.tb-sticky-bar { position: sticky; top: 62px; z-index: 5; }
.tb-err-item { font-size: .82rem; line-height: 1.35; }
"""


def _summary(s: Strategy) -> str:
    n_in, n_out = len(s.entry.conditions), len(s.exit.conditions)
    return (f"{TIMEFRAME_LABELS.get(s.timeframe, s.timeframe)} · {n_in} Kauf-, "
            f"{n_out} Verkaufsregel{'n' if n_out != 1 else ''}")


def unique_name(base: str, existing: list[str]) -> str:
    names = {n.lower() for n in existing}
    slugs = {slugify(n) for n in existing}
    if base.lower() not in names and slugify(base) not in slugs:
        return base
    i = 2
    while True:
        cand = f"{base} ({i})"
        if cand.lower() not in names and slugify(cand) not in slugs:
            return cand
        i += 1


@ui.page("/strategies")
def page(name: str | None = None, preset: str | None = None, draft: int = 0) -> None:
    st = state()
    store = st.store
    add_editor_css()
    ui.add_css(PAGE_CSS)

    ctx: dict = {"editor": None, "orig": None, "saved_snapshot": None, "source": "new", "pending": False,
                 "errors": []}

    with frame("Strategien", "Regeln für Kauf und Verkauf zusammenstellen, prüfen und testen"):
        with ui.row().classes("w-full items-start gap-5 no-wrap max-lg:flex-wrap"):
            # ------------------------------------------------------------------ left column
            with ui.column().classes("w-[290px] max-lg:w-full gap-4 shrink-0"):
                with card("p-3 gap-2"):
                    with ui.row().classes("w-full items-center justify-between px-1"):
                        ui.label("Meine Strategien").classes("tb-section")
                        with ui.row().classes("gap-0"):
                            ui.button(icon="add", on_click=lambda: guarded(new_strategy)).props(
                                "flat round dense color=primary").tooltip("Neue Strategie")
                            imp = ui.button(icon="upload_file", on_click=lambda: import_dialog.open()).props(
                                "flat round dense color=grey-6")
                            imp.tooltip("JSON importieren")
                    list_box = ui.column().classes("w-full gap-0 tb-slist")
                with card("p-3 gap-1"):
                    with ui.row().classes("w-full items-center justify-between px-1"):
                        ui.label("Vorlagen").classes("tb-section")
                        ui.icon("auto_awesome", size="16px").classes("tb-muted").tooltip(
                            "Bekannte Beispielstrategien als Ausgangspunkt – keine Empfehlung.")
                    ui.label("Laden erzeugt eine bearbeitbare Kopie.").classes("tb-muted text-xs px-1 pb-1")
                    preset_box = ui.column().classes("w-full gap-0 tb-slist")

            # ------------------------------------------------------------------ editor column
            editor_col = ui.column().classes("flex-1 min-w-0 gap-4")

    # ------------------------------------------------------------------ dialogs
    with ui.dialog() as import_dialog, ui.card().classes("tb-card w-[460px] gap-3"):
        ui.label("Strategie importieren").classes("text-lg font-semibold")
        ui.label("Wähle eine zuvor exportierte Strategie-Datei (.json). Sie wird als ungespeicherter "
                 "Entwurf geöffnet.").classes("tb-muted text-sm")

        async def on_upload(e) -> None:
            try:
                text = await e.file.text()
                data = json.loads(text)
                if not isinstance(data, dict):
                    raise ValueError("Die Datei enthält kein Strategie-Objekt.")
                s = Strategy.from_dict(data)
            except Exception as exc:  # noqa: BLE001
                ui.notify(f"Import fehlgeschlagen: {exc}", color="negative")
                upload.reset()
                return
            upload.reset()
            import_dialog.close()
            open_strategy(s, orig=None, source="import")
            ui.notify(f"„{s.name}“ importiert – bitte prüfen und speichern.", color="positive")

        upload = ui.upload(on_upload=on_upload, auto_upload=True, max_file_size=1_000_000).props(
            'accept=".json,application/json" flat bordered label="JSON-Datei auswählen"').classes("w-full")
        with ui.row().classes("w-full justify-end"):
            ui.button("Abbrechen", on_click=import_dialog.close).props("flat")

    async def confirm(title: str, text: str, ok: str = "OK", danger: bool = False) -> bool:
        with ui.dialog() as dlg, ui.card().classes("tb-card w-[440px] gap-3"):
            ui.label(title).classes("text-lg font-semibold")
            ui.label(text).classes("tb-muted text-sm")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Abbrechen", on_click=lambda: dlg.submit(False)).props("flat")
                ui.button(ok, on_click=lambda: dlg.submit(True)).props(
                    f"unelevated color={'negative' if danger else 'primary'}")
        res = await dlg
        dlg.delete()
        return bool(res)

    # ------------------------------------------------------------------ helpers
    def is_dirty() -> bool:
        ed: StrategyEditor | None = ctx["editor"]
        return ed is not None and ed.snapshot() != ctx["saved_snapshot"]

    async def guarded(fn, *args) -> None:
        if is_dirty() and not await confirm("Ungespeicherte Änderungen",
                                            "Die aktuelle Strategie hat ungespeicherte Änderungen. Verwerfen?",
                                            "Verwerfen", danger=True):
            return
        fn(*args)

    def render_lists() -> None:
        list_box.clear()
        names = store.list_strategies()
        with list_box:
            if not names:
                ui.label("Noch keine gespeicherten Strategien. Lade eine Vorlage oder lege eine neue an.").classes(
                    "tb-muted text-sm px-1 py-2")
            for n in names:
                try:
                    sub = _summary(store.load_strategy(n))
                except Exception:  # noqa: BLE001
                    sub = "Datei fehlerhaft"
                active = ctx["orig"] == n
                with ui.item(on_click=lambda n=n: guarded(load_saved, n)).classes("tb-sel" if active else ""):
                    with ui.item_section():
                        ui.item_label(n).classes("text-sm font-medium")
                        ui.item_label(sub).props("caption")
        preset_box.clear()
        with preset_box:
            for p in PRESETS:
                with ui.item(on_click=lambda p=p: guarded(load_preset, p)):
                    with ui.item_section():
                        ui.item_label(p.name).classes("text-sm")
                        ui.item_label(_summary(p)).props("caption")
                    with ui.item_section().props("side"):
                        ui.icon("content_copy", size="16px").classes("tb-muted")

    def load_saved(n: str) -> None:
        try:
            s = store.load_strategy(n)
        except Exception as exc:  # noqa: BLE001
            ui.notify(str(exc), color="negative")
            return
        open_strategy(s, orig=n, source="saved")

    def load_preset(p: Strategy) -> None:
        s = copy.deepcopy(p)
        s.name = unique_name(p.name, store.list_strategies())
        open_strategy(s, orig=None, source="preset")
        ui.notify(f"Vorlage „{p.name}“ geladen – Änderungen werden erst beim Speichern übernommen.")

    def new_strategy() -> None:
        s = Strategy(name=unique_name("Neue Strategie", store.list_strategies()))
        open_strategy(s, orig=None, source="new")

    def duplicate() -> None:
        ed: StrategyEditor = ctx["editor"]
        s = Strategy.from_dict(copy.deepcopy(ed.d))
        s.name = unique_name(f"{(s.name or 'Strategie')} (Kopie)", store.list_strategies())
        open_strategy(s, orig=None, source="copy")
        ui.notify("Kopie erstellt – noch nicht gespeichert.")

    async def delete() -> None:
        orig = ctx["orig"]
        if orig is None:
            if await confirm("Entwurf verwerfen?", "Dieser Entwurf wurde noch nicht gespeichert.", "Verwerfen",
                             danger=True):
                first_or_new()
            return
        if not await confirm("Strategie löschen?", f"„{orig}“ wird endgültig gelöscht. Laufende Bots, die "
                             "diese Strategie verwenden, behalten ihre geladene Kopie.", "Löschen", danger=True):
            return
        store.delete_strategy(orig)
        ui.notify(f"„{orig}“ gelöscht.")
        first_or_new()

    def first_or_new() -> None:
        names = store.list_strategies()
        if names:
            load_saved(names[0])
        else:
            new_strategy()

    def export() -> None:
        ed: StrategyEditor = ctx["editor"]
        data = json.dumps(ed.d, indent=2, ensure_ascii=False)
        ui.download.content(data, f"{slugify(ed.d.get('name') or 'strategie')}.json", "application/json")

    async def save() -> None:
        ed: StrategyEditor = ctx["editor"]
        try:
            s = ed.strategy()
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"Speichern nicht möglich: {exc}", color="negative")
            return
        errs = validate(s)
        if errs:
            ui.notify("Bitte zuerst die markierten Fehler beheben.", color="negative")
            return
        orig = ctx["orig"]
        existing = store.list_strategies()
        clash = [n for n in existing if (n.lower() == s.name.lower() or slugify(n) == slugify(s.name))
                 and n != orig]
        if clash and not await confirm("Name bereits vergeben",
                                       f"Es gibt schon eine Strategie „{clash[0]}“. Überschreiben?",
                                       "Überschreiben", danger=True):
            return
        try:
            for n in clash:
                if slugify(n) != slugify(s.name):
                    store.delete_strategy(n)
            store.save_strategy(s)
            if orig and orig != s.name and slugify(orig) != slugify(s.name):
                store.delete_strategy(orig)
        except OSError as exc:
            ui.notify(f"Speichern fehlgeschlagen: {exc}", color="negative")
            return
        ctx["orig"] = s.name
        ctx["source"] = "saved"
        ctx["saved_snapshot"] = ed.snapshot()
        render_lists()
        update_status()
        ui.notify(f"„{s.name}“ gespeichert.", color="positive")
        try:
            ui.navigate.history.replace(f"/strategies?name={quote(s.name)}")
        except Exception:  # noqa: BLE001
            pass

    async def to_backtest() -> None:
        if is_dirty():
            if not await confirm("Erst speichern?", "Der Backtest verwendet die gespeicherte Version. "
                                 "Jetzt speichern und Backtest öffnen?", "Speichern & weiter"):
                return
            await save()
            if is_dirty():
                return
        ui.navigate.to(f"/backtest?strategy={quote(ctx['orig'])}")

    # ------------------------------------------------------------------ editor
    status = {}

    def on_change() -> None:
        ctx["pending"] = True
        update_dirty()

    def update_dirty() -> None:
        if "unsaved" in status:
            status["unsaved"].set_visibility(is_dirty() or ctx["orig"] is None)
            status["unsaved"].set_text("Ungespeichert" if ctx["orig"] is not None else "Nicht gespeichert")

    def open_strategy(s: Strategy, orig: str | None, source: str) -> None:
        ctx["orig"], ctx["source"] = orig, source
        ed = StrategyEditor(s, on_change)
        ctx["editor"] = ed
        # unsaved drafts compare against an impossible snapshot
        ctx["saved_snapshot"] = ed.snapshot() if orig is not None else None
        build_editor(ed)
        render_lists()
        update_status()

    def build_editor(ed: StrategyEditor) -> None:
        editor_col.clear()
        status.clear()
        with editor_col:
            # sticky toolbar
            with card("p-3 tb-sticky-bar"):
                with ui.row().classes("w-full items-center gap-x-3 gap-y-2 flex-wrap"):
                    with ui.column().classes("gap-0 min-w-[240px] flex-1"):
                        with ui.row().classes("w-full items-center gap-2 no-wrap"):
                            status["title"] = ui.label().classes("text-lg font-semibold truncate min-w-0")
                            status["unsaved"] = ui.label().classes("tb-unsaved whitespace-nowrap")
                        with ui.row().classes("items-center gap-1"):
                            status["icon"] = ui.icon("check_circle", size="16px")
                            status["state"] = ui.label().classes("text-xs")
                            status["state_btn"] = ui.button("Details", on_click=lambda: err_dialog.open()).props(
                                "flat dense no-caps size=sm color=negative")
                    with ui.row().classes("items-center gap-1"):
                        ui.button(icon="content_copy", on_click=duplicate).props(
                            "flat round dense color=grey-6").tooltip("Duplizieren")
                        ui.button(icon="download", on_click=export).props(
                            "flat round dense color=grey-6").tooltip("JSON exportieren")
                        ui.button(icon="delete_outline", on_click=delete).props(
                            "flat round dense color=grey-6").tooltip("Löschen")
                        ui.separator().props("vertical").classes("mx-1")
                        status["bt"] = ui.button("Backtest starten", icon="play_arrow", on_click=to_backtest).props(
                            "outline no-caps color=primary")
                        status["save"] = ui.button("Speichern", icon="save", on_click=save).props(
                            "unelevated no-caps color=primary")

            with ui.element("div").classes("grid w-full gap-4 items-start grid-cols-1 xl:grid-cols-[minmax(0,1fr)_380px]"):
                with card("gap-3"):
                    ui.label("Grunddaten").classes("tb-section")
                    ed.build_basics()
                with card("gap-2"):
                    with ui.row().classes("w-full items-center justify-between"):
                        ui.label("So handelt die Strategie").classes("tb-section")
                        ui.icon("record_voice_over", size="16px").classes("tb-muted")
                    status["desc"] = ui.column().classes("w-full gap-1")
            with card("gap-3"):
                ed.build_group("entry")
            with card("gap-3"):
                ed.build_group("exit")
            with card("gap-3"):
                ed.build_risk()

    with ui.dialog() as err_dialog, ui.card().classes("tb-card w-[560px] gap-3"):
        ui.label("Was noch fehlt").classes("text-lg font-semibold")
        err_box = ui.column().classes("w-full gap-2")
        with ui.row().classes("w-full justify-end"):
            ui.button("Schließen", on_click=err_dialog.close).props("flat")

    def update_status() -> None:
        ed: StrategyEditor | None = ctx["editor"]
        if ed is None or "title" not in status:
            return
        ctx["pending"] = False
        try:
            s = ed.strategy()
            errs = validate(s)
        except Exception as exc:  # noqa: BLE001
            s, errs = None, [f"Ungültige Eingabe: {exc}"]
        ctx["errors"] = errs
        status["title"].set_text(ed.d.get("name") or "Ohne Namen")
        update_dirty()
        ok = not errs
        status["icon"].props(f"name={'check_circle' if ok else 'error'}")
        status["icon"].classes(replace="tb-status-ok" if ok else "tb-status-err")
        status["state"].set_text("Strategie ist gültig" if ok else
                                 f"{len(errs)} Problem{'e' if len(errs) != 1 else ''}: {errs[0]}")
        status["state"].classes(replace="text-xs tb-muted truncate max-w-[520px]" if ok else
                                "text-xs tb-status-err truncate max-w-[520px]")
        status["state_btn"].set_visibility(not ok)
        status["save"].set_enabled(ok)
        status["bt"].set_enabled(ok)
        ed.refresh_hints()

        err_box.clear()
        with err_box:
            for e in errs:
                with ui.row().classes("items-start gap-2 no-wrap"):
                    ui.icon("error_outline", size="18px").classes("tb-status-err mt-[1px]")
                    ui.label(e).classes("tb-err-item")

        box = status["desc"]
        box.clear()
        with box:
            if s is None:
                ui.label("Die Beschreibung erscheint, sobald alle Eingaben Zahlen sind.").classes("tb-muted text-sm")
                return
            try:
                text = describe_strategy(s)
            except Exception:  # noqa: BLE001
                ui.label("Die Beschreibung erscheint, sobald alle Eingaben gültig sind.").classes("tb-muted text-sm")
                return
            lines = text.split("\n")
            for ln in lines[1:]:
                if ln.startswith("  • "):
                    ui.label(ln[4:]).classes("tb-desc-bullet")
                elif ln.startswith("Trading-Einstellungen: "):
                    ui.label("Trading-Einstellungen").classes("tb-desc-line font-semibold mt-2")
                    ui.label(ln[len("Trading-Einstellungen: "):]).classes("tb-desc-line tb-muted")
                elif ln.startswith(("Kaufen", "Verkaufen")):
                    ui.label(ln).classes("tb-desc-line font-semibold mt-1")
                else:
                    ui.label(ln).classes("tb-desc-line tb-muted")
            if not errs:
                try:
                    wu = warmup_bars(s)
                    ui.label(f"Benötigt ca. {wu} Kerzen Vorlauf für die Indikatoren.").classes(
                        "tb-hint mt-2")
                except Exception:  # noqa: BLE001
                    pass

    def tick() -> None:
        if ctx["pending"]:
            update_status()

    ui.timer(0.4, tick)

    # ------------------------------------------------------------------ initial selection
    initial: Strategy | None = None
    if draft:
        raw = app.storage.user.pop(DRAFT_KEY, None)
        if isinstance(raw, dict):
            try:
                initial = Strategy.from_dict(raw)
            except Exception:  # noqa: BLE001
                initial = None
    if initial is not None:
        initial.name = unique_name(initial.name, store.list_strategies())
        open_strategy(initial, orig=None, source="draft")
    elif preset:
        match = next((p for p in PRESETS if p.name == preset), None)
        load_preset(match) if match else first_or_new()
    elif name and name in store.list_strategies():
        load_saved(name)
    else:
        first_or_new()
