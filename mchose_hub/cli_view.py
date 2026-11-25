"""
cli_view.py — Vista GTK4 / libadwaita para el MCHOSE A7 V2 Pro.

Usa exclusivamente el CLI 'mchose' de alexfrih/mchose-linux como backend.
No abre ningún nodo hidraw directamente: cada acción es una llamada al binario.

Flujo:
  1. Al crear la vista → lanza get_state() en un hilo trabajador.
  2. apply_state() actualiza todos los widgets en el hilo GTK.
  3. Cada cambio de widget → antirrebote de 400 ms → llama al método de escritura.
  4. Polling de batería cada 60 s.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List, Optional

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from .cli_backend import MchoseCliBackend, MchoseCliError, MchoseCliNotFound, MouseState

log = logging.getLogger(__name__)

RATE_OPTIONS = [125, 500, 1000, 2000, 4000, 8000]
NUM_STAGES = 6


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _row(title: str, subtitle: str = "") -> Adw.ActionRow:
    row = Adw.ActionRow(title=title, subtitle=subtitle)
    if hasattr(row, "set_use_markup"):
        row.set_use_markup(False)
    return row


def _worker(fn: Callable, on_ok: Optional[Callable] = None,
            on_err: Optional[Callable] = None) -> None:
    """Ejecuta fn en un hilo daemon; entrega el resultado al hilo GTK."""
    def run() -> None:
        try:
            result = fn()
        except Exception as exc:
            if on_err:
                GLib.idle_add(on_err, exc)
        else:
            if on_ok:
                GLib.idle_add(on_ok, result)
    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------------------
# Vista principal
# ---------------------------------------------------------------------------
class CliDeviceView(Adw.PreferencesPage):
    """Vista completa del ratón usando el CLI de alexfrih como backend.

    Se instancia con un único argumento: la función toast de MainWindow.
    El backend CLI se crea internamente.
    """

    def __init__(self, toast: Callable[[str], None]) -> None:
        super().__init__()
        self._toast = toast
        self._backend = MchoseCliBackend()
        self._state: Optional[MouseState] = None
        self._pending: Dict[str, int] = {}   # temporizadores de antirrebote
        self._battery_timer: int = 0
        # Flag para suprimir callbacks de escritura mientras se actualiza la UI
        self._building = True

        # ----------------------------------------------------------------
        # Cabecera: nombre del dispositivo, estado y batería
        # ----------------------------------------------------------------
        self._head = Adw.PreferencesGroup(
            title="MCHOSE A7 V2 Pro",
            description="Conectando…"
        )
        self._status_row = _row("Estado", "Conectando…")
        self._battery_row = _row("Batería", "—")
        self._bar = Gtk.LevelBar(min_value=0, max_value=100, value=0)
        self._bar.set_size_request(120, -1)
        self._bar.set_valign(Gtk.Align.CENTER)
        self._battery_row.add_suffix(self._bar)
        self._head.add(self._status_row)
        self._head.add(self._battery_row)
        self.add(self._head)

        # ----------------------------------------------------------------
        # Perfil activo
        # ----------------------------------------------------------------
        g_active = Adw.PreferencesGroup(title="Perfil activo")
        self._stage_row = Adw.ComboRow(title="Perfil DPI activo")
        self._stage_row.set_model(
            Gtk.StringList.new([f"Perfil {i + 1}" for i in range(NUM_STAGES)])
        )
        g_active.add(self._stage_row)
        self.add(g_active)

        # ----------------------------------------------------------------
        # DPI — 6 sliders
        # ----------------------------------------------------------------
        self._g_dpi = Adw.PreferencesGroup(title="DPI por perfil")
        self._dpi_scales: List[Gtk.Scale] = []
        self._dpi_rows: List[Adw.ActionRow] = []
        for i in range(NUM_STAGES):
            row = _row(f"Perfil {i + 1} — DPI", "DPI")
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 100, 26000, 100)
            scale.set_digits(0)
            scale.set_draw_value(True)
            scale.set_value_pos(Gtk.PositionType.RIGHT)
            scale.set_hexpand(True)
            scale.set_valign(Gtk.Align.CENTER)
            scale.set_size_request(260, -1)
            scale.set_value(800.0)
            scale._stage_index = i  # type: ignore[attr-defined]
            self._dpi_scales.append(scale)
            self._dpi_rows.append(row)
            row.add_suffix(scale)
            self._g_dpi.add(row)
        self.add(self._g_dpi)

        # ----------------------------------------------------------------
        # Sensor
        # ----------------------------------------------------------------
        g_sensor = Adw.PreferencesGroup(title="Sensor")

        self._rate_row = Adw.ComboRow(title="Frecuencia de sondeo (inalámbrico)")
        self._rate_row.set_model(
            Gtk.StringList.new([f"{r} Hz" for r in RATE_OPTIONS])
        )
        g_sensor.add(self._rate_row)

        self._lod_row = Adw.ComboRow(title="Distancia de despegue (LOD)")
        self._lod_row.set_model(Gtk.StringList.new(["1 mm", "2 mm"]))
        g_sensor.add(self._lod_row)

        self._debounce_scale = self._make_slider(
            g_sensor, "Tiempo de rebote", "ms", 0, 20, 1
        )
        self.add(g_sensor)

        # ----------------------------------------------------------------
        # Opciones del sensor
        # ----------------------------------------------------------------
        g_opts = Adw.PreferencesGroup(title="Opciones del sensor")
        self._ripple_sw = self._make_switch(g_opts, "Ripple control")
        self._angle_sw = self._make_switch(g_opts, "Corrección de ángulo (angle snap)")
        self._motion_sw = self._make_switch(g_opts, "Motion sync")
        self.add(g_opts)

        # ----------------------------------------------------------------
        # Reposo
        # ----------------------------------------------------------------
        g_sleep = Adw.PreferencesGroup(title="Reposo")
        self._sleep_scale = self._make_slider(
            g_sleep, "Minutos hasta el reposo", "min  (0 = desactivado)", 0, 30, 1
        )
        self.add(g_sleep)

        # ----------------------------------------------------------------
        # Conectar señales DESPUÉS de construir todos los widgets
        # ----------------------------------------------------------------
        self._building = False
        self._stage_row.connect("notify::selected", self._on_stage_changed)
        self._rate_row.connect("notify::selected", self._on_rate_changed)
        self._lod_row.connect("notify::selected", self._on_lod_changed)
        for scale in self._dpi_scales:
            scale.connect("value-changed", self._on_dpi_changed)
        self._debounce_scale.connect("value-changed", self._on_debounce_changed)
        self._sleep_scale.connect("value-changed", self._on_sleep_changed)

        # ----------------------------------------------------------------
        # Lectura inicial del estado real
        # ----------------------------------------------------------------
        _worker(self._backend.get_state, self._apply_state, self._on_error)
        self._battery_timer = GLib.timeout_add_seconds(60, self._poll_battery)

    # ----------------------------------------------------------------
    # Helpers de construcción
    # ----------------------------------------------------------------
    def _make_slider(self, group: Adw.PreferencesGroup, title: str, unit: str,
                     mn: float, mx: float, step: float) -> Gtk.Scale:
        row = _row(title, unit)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, mn, mx, step)
        scale.set_digits(0)
        scale.set_draw_value(True)
        scale.set_value_pos(Gtk.PositionType.RIGHT)
        scale.set_hexpand(True)
        scale.set_valign(Gtk.Align.CENTER)
        scale.set_size_request(200, -1)
        row.add_suffix(scale)
        group.add(row)
        return scale

    def _make_switch(self, group: Adw.PreferencesGroup, title: str) -> Gtk.Switch:
        sw = Gtk.Switch(valign=Gtk.Align.CENTER)
        row = _row(title)
        row.add_suffix(sw)
        row.set_activatable_widget(sw)
        group.add(row)
        sw.connect("notify::active", self._on_switch_changed, title)
        return sw

    # ----------------------------------------------------------------
    # Lectura / actualización del estado
    # ----------------------------------------------------------------
    def _refresh(self) -> None:
        """Releer el estado completo del ratón (en un hilo trabajador)."""
        _worker(self._backend.get_state, self._apply_state, self._on_error)

    def _poll_battery(self) -> bool:
        _worker(
            self._backend.get_state,
            self._apply_battery_only,
            lambda exc: None   # fallo silencioso en el sondeo periódico
        )
        return GLib.SOURCE_CONTINUE

    def _apply_battery_only(self, state: MouseState) -> None:
        if state.battery is not None:
            suffix = "  · cargando" if state.charging else ""
            self._battery_row.set_subtitle(f"{state.battery} %{suffix}")
            self._bar.set_value(float(state.battery))

    def _apply_state(self, state: MouseState) -> None:
        """Actualiza TODOS los widgets con el estado leído del ratón (hilo GTK)."""
        self._state = state
        self._building = True   # inhibir callbacks de escritura

        # --- Cabecera ---
        parts: List[str] = []
        if state.firmware:
            parts.append(f"firmware {state.firmware}")
        if state.link:
            parts.append(state.link)
        self._head.set_description("  ·  ".join(parts) if parts else "")
        self._status_row.set_subtitle("Conectado")

        # --- Batería ---
        if state.battery is not None:
            suffix = "  · cargando" if state.charging else ""
            self._battery_row.set_subtitle(f"{state.battery} %{suffix}")
            self._bar.set_value(float(state.battery))

        # --- Perfil activo ---
        self._stage_row.set_selected(state.active_stage)

        # --- DPI stages ---
        for i, scale in enumerate(self._dpi_scales):
            dpi = state.stages[i] if i < len(state.stages) else 400
            scale.set_value(float(dpi))
            active = (i == state.active_stage)
            self._dpi_rows[i].set_title(
                f"{'▶ ' if active else ''}Perfil {i + 1} — DPI"
            )

        # --- Polling inalámbrico ---
        idx = next(
            (i for i, r in enumerate(RATE_OPTIONS) if r == state.rate_wireless),
            RATE_OPTIONS.index(1000)   # 1000 Hz por defecto
        )
        self._rate_row.set_selected(idx)

        # --- LOD (1mm → índice 0, 2mm → índice 1) ---
        self._lod_row.set_selected(0 if state.lod <= 1 else 1)

        # --- Debounce / sleep ---
        self._debounce_scale.set_value(float(state.debounce))
        self._sleep_scale.set_value(float(state.sleep))

        # --- Switches ---
        self._ripple_sw.set_active(state.ripple)
        self._angle_sw.set_active(state.angle_snap)
        self._motion_sw.set_active(state.motion_sync)

        self._building = False

    # ----------------------------------------------------------------
    # Callbacks de escritura
    # ----------------------------------------------------------------
    def _on_dpi_changed(self, scale: Gtk.Scale) -> None:
        if self._building:
            return
        stage = scale._stage_index  # type: ignore[attr-defined]
        dpi = int(round(scale.get_value() / 100)) * 100
        key = f"dpi{stage}"
        old = self._pending.pop(key, None)
        if old:
            GLib.source_remove(old)

        def fire() -> bool:
            self._pending.pop(key, None)
            _worker(
                lambda: self._backend.set_dpi_stage(stage, dpi),
                lambda _: None,
                self._on_error
            )
            return GLib.SOURCE_REMOVE

        self._pending[key] = GLib.timeout_add(400, fire)

    def _on_stage_changed(self, row: Adw.ComboRow, _ps: Any) -> None:
        if self._building:
            return
        stage = row.get_selected()
        # Marcar visualmente el perfil activo inmediatamente
        for i, drow in enumerate(self._dpi_rows):
            drow.set_title(f"{'▶ ' if i == stage else ''}Perfil {i + 1} — DPI")
        _worker(
            lambda: self._backend.set_active_stage(stage),
            lambda _: None,
            self._on_error
        )

    def _on_rate_changed(self, row: Adw.ComboRow, _ps: Any) -> None:
        if self._building:
            return
        hz = RATE_OPTIONS[row.get_selected()]
        _worker(lambda: self._backend.set_rate(hz), lambda _: None, self._on_error)

    def _on_lod_changed(self, row: Adw.ComboRow, _ps: Any) -> None:
        if self._building:
            return
        mm = row.get_selected() + 1   # 0 → 1 mm, 1 → 2 mm
        _worker(lambda: self._backend.set_lod(mm), lambda _: None, self._on_error)

    def _on_debounce_changed(self, scale: Gtk.Scale) -> None:
        if self._building:
            return
        ms = int(scale.get_value())
        old = self._pending.pop("debounce", None)
        if old:
            GLib.source_remove(old)

        def fire() -> bool:
            self._pending.pop("debounce", None)
            _worker(
                lambda: self._backend.set_debounce(ms),
                lambda _: None,
                self._on_error
            )
            return GLib.SOURCE_REMOVE

        self._pending["debounce"] = GLib.timeout_add(400, fire)

    def _on_sleep_changed(self, scale: Gtk.Scale) -> None:
        if self._building:
            return
        mins = int(scale.get_value())
        old = self._pending.pop("sleep", None)
        if old:
            GLib.source_remove(old)

        def fire() -> bool:
            self._pending.pop("sleep", None)
            _worker(
                lambda: self._backend.set_sleep(mins),
                lambda _: None,
                self._on_error
            )
            return GLib.SOURCE_REMOVE

        self._pending["sleep"] = GLib.timeout_add(400, fire)

    def _on_switch_changed(self, sw: Gtk.Switch, _ps: Any, title: str) -> None:
        if self._building:
            return
        val = sw.get_active()
        tl = title.lower()
        if "ripple" in tl:
            _worker(lambda: self._backend.set_ripple(val), lambda _: None, self._on_error)
        elif "angle" in tl:
            _worker(lambda: self._backend.set_angle_snap(val), lambda _: None, self._on_error)
        elif "motion" in tl:
            _worker(lambda: self._backend.set_motion_sync(val), lambda _: None, self._on_error)

    # ----------------------------------------------------------------
    # Manejo de errores
    # ----------------------------------------------------------------
    def _on_error(self, exc: Exception) -> None:
        if isinstance(exc, MchoseCliNotFound):
            self._status_row.set_subtitle("CLI no encontrado — instala mchose-linux")
            self._toast(str(exc)[:160])
        elif isinstance(exc, MchoseCliError):
            self._status_row.set_subtitle("Error")
            self._toast(f"Error: {exc}")
        else:
            log.exception("Error inesperado en CliDeviceView", exc_info=exc)
            self._toast(f"Error inesperado: {exc}")

    # ----------------------------------------------------------------
    # Cierre limpio
    # ----------------------------------------------------------------
    def shutdown(self) -> None:
        """Cancela temporizadores pendientes. Llamado desde MainWindow al cerrar."""
        for src in list(self._pending.values()):
            GLib.source_remove(src)
        self._pending.clear()
        if self._battery_timer:
            GLib.source_remove(self._battery_timer)
            self._battery_timer = 0
