"""
cli_view.py — Vista GTK4 que usa el binario 'mchose' de alexfrih como backend.

Reemplaza DeviceView para el A7 V2 Pro (y cualquier ratón compatible con ese CLI).
La GUI lee el estado real con 'mchose show' + 'mchose info' y escribe con
'mchose dpi', 'mchose rate', etc. — el mismo protocolo verificado en hardware.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List, Optional

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk

from .cli_backend import MchoseCliBackend, MchoseCliError, MchoseCliNotFound, MouseState

log = logging.getLogger(__name__)

RATE_OPTIONS = [125, 500, 1000, 2000, 4000, 8000]


def _row(title: str, subtitle: str = "") -> Adw.ActionRow:
    row = Adw.ActionRow(title=title, subtitle=subtitle)
    if hasattr(row, "set_use_markup"):
        row.set_use_markup(False)
    return row


def _worker(fn: Callable, on_ok: Optional[Callable] = None,
            on_err: Optional[Callable] = None) -> None:
    def run():
        try:
            result = fn()
        except Exception as exc:
            if on_err:
                GLib.idle_add(on_err, exc)
        else:
            if on_ok:
                GLib.idle_add(on_ok, result)
    threading.Thread(target=run, daemon=True).start()


class CliDeviceView(Adw.PreferencesPage):
    """Vista completa del ratón usando el CLI de alexfrih como backend."""

    def __init__(self, toast: Callable[[str], None]) -> None:
        super().__init__()
        self._toast = toast
        self._backend = MchoseCliBackend()
        self._state: Optional[MouseState] = None
        self._pending: Dict[str, int] = {}
        self._battery_timer = 0
        self._building = True  # suprimir callbacks durante la construcción

        # --- Cabecera ---
        self._head = Adw.PreferencesGroup(title="MCHOSE A7 V2 Pro", description="")
        self._status_row = _row("Estado", "Conectando…")
        self._battery_row = _row("Batería", "—")
        self._bar = Gtk.LevelBar(min_value=0, max_value=100, value=0)
        self._bar.set_size_request(120, -1); self._bar.set_valign(Gtk.Align.CENTER)
        self._battery_row.add_suffix(self._bar)
        self._head.add(self._status_row); self._head.add(self._battery_row)
        self.add(self._head)

        # --- Perfil activo ---
        g_active = Adw.PreferencesGroup(title="Perfil activo")
        self._stage_row = Adw.ComboRow(title="Perfil DPI activo")
        self._stage_row.set_model(Gtk.StringList.new(
            [f"Perfil {i+1}" for i in range(6)]))
        g_active.add(self._stage_row)
        self.add(g_active)

        # --- DPI (6 sliders) ---
        self._g_dpi = Adw.PreferencesGroup(title="DPI")
        self._dpi_scales: List[Gtk.Scale] = []
        self._dpi_rows: List[Adw.ActionRow] = []
        for i in range(6):
            row = _row(f"Perfil {i+1} — DPI", "DPI")
            self._dpi_rows.append(row)
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 100, 26000, 100)
            scale.set_digits(0); scale.set_draw_value(True)
            scale.set_value_pos(Gtk.PositionType.RIGHT)
            scale.set_hexpand(True); scale.set_valign(Gtk.Align.CENTER)
            scale.set_size_request(260, -1)
            scale._stage_index = i
            scale.set_value(800.0)  # valor inicial hasta que llegue el estado real
            self._dpi_scales.append(scale)
            row.add_suffix(scale)
            self._g_dpi.add(row)
        self.add(self._g_dpi)
        # _dpi_rows ya se llena en el loop

        # --- Sensor ---
        g_sensor = Adw.PreferencesGroup(title="Sensor")
        self._rate_row = Adw.ComboRow(title="Frecuencia de sondeo (inalámbrico)")
        self._rate_row.set_model(Gtk.StringList.new(
            [f"{r} Hz" for r in RATE_OPTIONS]))
        g_sensor.add(self._rate_row)

        self._lod_row = Adw.ComboRow(title="Distancia de despegue (LOD)")
        self._lod_row.set_model(Gtk.StringList.new(["1 mm", "2 mm"]))
        g_sensor.add(self._lod_row)

        self._debounce_scale = self._make_slider(g_sensor, "Tiempo de rebote", "ms", 0, 20, 1)
        self.add(g_sensor)

        # --- Opciones del sensor ---
        g_opts = Adw.PreferencesGroup(title="Opciones del sensor")
        self._ripple_sw    = self._make_switch(g_opts, "Ripple control")
        self._angle_sw     = self._make_switch(g_opts, "Corrección de ángulo (angle snap)")
        self._motion_sw    = self._make_switch(g_opts, "Motion sync")
        self.add(g_opts)

        # --- Reposo ---
        g_sleep = Adw.PreferencesGroup(title="Reposo")
        self._sleep_scale = self._make_slider(g_sleep, "Minutos hasta el reposo",
                                              "min (0 = desactivado)", 0, 30, 1)
        self.add(g_sleep)

        # --- Presets ---
        g_presets = Adw.PreferencesGroup(title="Presets")
        # Fila: nombre + botón guardar
        self._preset_entry = Gtk.Entry(placeholder_text="Nombre del preset…",
                                       hexpand=True, valign=Gtk.Align.CENTER)
        save_btn = Gtk.Button(label="Guardar", valign=Gtk.Align.CENTER)
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", self._on_save_preset)
        new_row = _row("Guardar estado actual")
        new_row.add_suffix(self._preset_entry)
        new_row.add_suffix(save_btn)
        g_presets.add(new_row)
        # Lista de presets guardados (se rellena en _refresh_presets)
        self._presets_group = g_presets
        self._preset_rows: Dict[str, Adw.ActionRow] = {}
        self.add(g_presets)
        self._refresh_presets()

        # --- Conectar señales DESPUÉS de construir ---
        self._building = False
        self._stage_row.connect("notify::selected", self._on_stage_changed)
        self._rate_row.connect("notify::selected", self._on_rate_changed)
        self._lod_row.connect("notify::selected", self._on_lod_changed)
        for scale in self._dpi_scales:
            scale.connect("value-changed", self._on_dpi_changed)
        self._debounce_scale.connect("value-changed", self._on_debounce_changed)
        self._sleep_scale.connect("value-changed", self._on_sleep_changed)

        # Leer estado inicial
        self._refresh()
        self._battery_timer = GLib.timeout_add_seconds(60, self._poll_battery)

    # -- helpers de construcción de widgets ------------------------------------
    def _make_slider(self, group: Adw.PreferencesGroup, title: str, unit: str,
                     mn: float, mx: float, step: float) -> Gtk.Scale:
        row = _row(title, unit)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, mn, mx, step)
        scale.set_digits(0); scale.set_draw_value(True)
        scale.set_value_pos(Gtk.PositionType.RIGHT)
        scale.set_hexpand(True); scale.set_valign(Gtk.Align.CENTER)
        scale.set_size_request(200, -1)
        row.add_suffix(scale); group.add(row)
        return scale

    def _make_switch(self, group: Adw.PreferencesGroup, title: str) -> Gtk.Switch:
        sw = Gtk.Switch(valign=Gtk.Align.CENTER)
        row = _row(title); row.add_suffix(sw); row.set_activatable_widget(sw)
        group.add(row)
        sw.connect("notify::active", self._on_switch_changed, title)
        return sw

    # -- lectura del estado ---------------------------------------------------
    def _refresh(self) -> None:
        _worker(self._backend.get_state, self._apply_state, self._on_error)

    def _poll_battery(self) -> bool:
        _worker(self._backend.get_state, self._apply_battery_only,
                lambda exc: None)
        return GLib.SOURCE_CONTINUE

    def _apply_battery_only(self, state: MouseState) -> None:
        if state.battery is not None:
            self._battery_row.set_subtitle(
                f"{state.battery} %{'  · cargando' if state.charging else ''}")
            self._bar.set_value(float(state.battery))

    def _apply_state(self, state: MouseState) -> None:
        self._state = state
        self._building = True  # bloquear callbacks durante la actualización

        # Batería
        if state.battery is not None:
            self._battery_row.set_subtitle(
                f"{state.battery} %{'  · cargando' if state.charging else ''}")
            self._bar.set_value(float(state.battery))

        # Descripción con firmware/link
        parts = []
        if state.firmware: parts.append(f"firmware {state.firmware}")
        if state.link:     parts.append(state.link)
        self._head.set_description("  ·  ".join(parts))
        self._status_row.set_subtitle("Conectado")

        # Perfil activo
        self._stage_row.set_selected(state.active_stage)

        # DPI stages
        for i, scale in enumerate(self._dpi_scales):
            dpi = state.stages[i] if i < len(state.stages) else 400
            scale.set_value(float(dpi))
            if i < len(self._dpi_rows):
                active = (i == state.active_stage)
                self._dpi_rows[i].set_title(f"{'▶ ' if active else ''}Perfil {i+1} — DPI")

        # Polling inalámbrico
        idx = next((i for i, r in enumerate(RATE_OPTIONS) if r == state.rate_wireless), 2)
        self._rate_row.set_selected(idx)

        # LOD (1mm=0, 2mm=1)
        self._lod_row.set_selected(0 if state.lod <= 1 else 1)

        # Debounce / sleep
        self._debounce_scale.set_value(float(state.debounce))
        self._sleep_scale.set_value(float(state.sleep))

        # Switches
        self._ripple_sw.set_active(state.ripple)
        self._angle_sw.set_active(state.angle_snap)
        self._motion_sw.set_active(state.motion_sync)

        self._building = False

    # -- callbacks de escritura ------------------------------------------------
    def _on_dpi_changed(self, scale: Gtk.Scale) -> None:
        if self._building: return
        stage = scale._stage_index
        dpi = int(round(scale.get_value() / 100) * 100)
        old = self._pending.pop(f"dpi{stage}", None)
        if old: GLib.source_remove(old)
        def fire():
            self._pending.pop(f"dpi{stage}", None)
            _worker(lambda: self._backend.set_dpi_stage(stage, dpi),
                    lambda _: None, self._on_error)
            return GLib.SOURCE_REMOVE
        self._pending[f"dpi{stage}"] = GLib.timeout_add(400, fire)

    def _on_stage_changed(self, row: Adw.ComboRow, _ps: Any) -> None:
        if self._building: return
        stage = row.get_selected()
        # Actualizar marcador visual inmediatamente
        for i, drow in enumerate(self._dpi_rows):
            drow.set_title(f"{'▶ ' if i == stage else ''}Perfil {i+1} — DPI")
        _worker(lambda: self._backend.set_active_stage(stage),
                lambda _: None, self._on_error)

    def _on_rate_changed(self, row: Adw.ComboRow, _ps: Any) -> None:
        if self._building: return
        hz = RATE_OPTIONS[row.get_selected()]
        _worker(lambda: self._backend.set_rate(hz), lambda _: None, self._on_error)

    def _on_lod_changed(self, row: Adw.ComboRow, _ps: Any) -> None:
        if self._building: return
        mm = row.get_selected() + 1  # 0→1mm, 1→2mm
        _worker(lambda: self._backend.set_lod(mm), lambda _: None, self._on_error)

    def _on_debounce_changed(self, scale: Gtk.Scale) -> None:
        if self._building: return
        ms = int(scale.get_value())
        old = self._pending.pop("debounce", None)
        if old: GLib.source_remove(old)
        def fire():
            self._pending.pop("debounce", None)
            _worker(lambda: self._backend.set_debounce(ms), lambda _: None, self._on_error)
            return GLib.SOURCE_REMOVE
        self._pending["debounce"] = GLib.timeout_add(400, fire)

    def _on_sleep_changed(self, scale: Gtk.Scale) -> None:
        if self._building: return
        mins = int(scale.get_value())
        old = self._pending.pop("sleep", None)
        if old: GLib.source_remove(old)
        def fire():
            self._pending.pop("sleep", None)
            _worker(lambda: self._backend.set_sleep(mins), lambda _: None, self._on_error)
            return GLib.SOURCE_REMOVE
        self._pending["sleep"] = GLib.timeout_add(400, fire)

    def _on_switch_changed(self, sw: Gtk.Switch, _ps: Any, title: str) -> None:
        if self._building: return
        val = sw.get_active()
        if "ripple" in title.lower():
            _worker(lambda: self._backend.set_ripple(val), lambda _: None, self._on_error)
        elif "angle" in title.lower():
            _worker(lambda: self._backend.set_angle_snap(val), lambda _: None, self._on_error)
        elif "motion" in title.lower():
            _worker(lambda: self._backend.set_motion_sync(val), lambda _: None, self._on_error)

    def _on_error(self, exc: Exception) -> None:
        if isinstance(exc, MchoseCliNotFound):
            self._status_row.set_subtitle("CLI no encontrado — instala mchose-linux")
            self._toast(str(exc)[:120])
        elif isinstance(exc, MchoseCliError):
            self._toast(f"Error: {exc}")
        else:
            log.exception("Error inesperado", exc_info=exc)
            self._toast(f"Error: {exc}")

    # -- presets ----------------------------------------------------------
    def _refresh_presets(self) -> None:
        # Eliminar filas anteriores
        for row in self._preset_rows.values():
            self._presets_group.remove(row)
        self._preset_rows.clear()
        for name in self._backend.list_presets():
            row = _row(name)
            load_btn = Gtk.Button(label="Cargar", valign=Gtk.Align.CENTER)
            del_btn  = Gtk.Button(label="Borrar",  valign=Gtk.Align.CENTER)
            del_btn.add_css_class("destructive-action")
            load_btn.connect("clicked", self._on_load_preset, name)
            del_btn.connect("clicked",  self._on_delete_preset, name)
            row.add_suffix(load_btn)
            row.add_suffix(del_btn)
            self._presets_group.add(row)
            self._preset_rows[name] = row

    def _on_save_preset(self, _btn) -> None:
        name = self._preset_entry.get_text().strip()
        if not name:
            self._toast("Escribe un nombre para el preset")
            return
        def do():
            self._backend.save_preset(name)
        def ok(_):
            self._preset_entry.set_text("")
            self._refresh_presets()
            self._toast(f"Preset '{name}' guardado")
        _worker(do, ok, self._on_error)

    def _on_load_preset(self, _btn, name: str) -> None:
        _worker(lambda: self._backend.load_preset(name),
                lambda _: (self._refresh(), self._toast(f"Preset '{name}' aplicado")),
                self._on_error)

    def _on_delete_preset(self, _btn, name: str) -> None:
        self._backend.delete_preset(name)
        self._refresh_presets()
        self._toast(f"Preset '{name}' eliminado")

    def shutdown(self) -> None:
        for src in list(self._pending.values()):
            GLib.source_remove(src)
        self._pending.clear()
        if self._battery_timer:
            GLib.source_remove(self._battery_timer)
