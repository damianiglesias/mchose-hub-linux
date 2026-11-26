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
import json
from pathlib import Path
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
        self._refresh_timer: int = 0
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

        self._rate_row = Adw.ComboRow(title="Polling inalámbrico")
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

        g_presets = Adw.PreferencesGroup(title="Presets")
        self._preset_name = Adw.ComboRow(title="Preset")
        self._preset_name.set_model(Gtk.StringList.new(["Gaming", "Trabajo", "FPS"]))
        g_presets.add(self._preset_name)
        save = Gtk.Button(label="Guardar", valign=Gtk.Align.CENTER)
        load = Gtk.Button(label="Cargar", valign=Gtk.Align.CENTER)
        save.connect("clicked", self._save_preset)
        load.connect("clicked", self._load_preset)
        preset_row = _row("Configuración completa", "~/.config/mchose-hub/presets.json")
        preset_row.add_suffix(save)
        preset_row.add_suffix(load)
        g_presets.add(preset_row)
        reset = Gtk.Button(label="Restaurar valores predeterminados", valign=Gtk.Align.CENTER)
        reset.connect("clicked", self._reset_defaults)
        g_presets.add(reset)
        self.add(g_presets)

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
        # El enlace USB puede cambiar sin que cambie la ventana: refrescar el
        # estado también cubre el cambio cable ↔ dongle de forma inmediata.
        self._refresh_timer = GLib.timeout_add_seconds(3, self._poll_state)

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

    def _poll_state(self) -> bool:
        _worker(self._backend.get_state, self._apply_state,
                lambda _exc: None)
        return GLib.SOURCE_CONTINUE

    def _apply_battery_only(self, state: MouseState) -> None:
        if state.battery is not None:
            suffix = "  · cargando" if state.charging else ("  · batería baja" if state.battery <= 20 else "")
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
        link = state.link.lower()
        if "cable" in link or "wired" in link or "usb" in link:
            parts.append("Cable USB (conectado)")
        elif "wireless" in link or "2.4" in link or "dongle" in link:
            parts.append("2.4 GHz (conectado)")
        elif state.link:
            parts.append(state.link)
        self._head.set_description("  ·  ".join(parts) if parts else "")
        self._status_row.set_subtitle("Conectado")
        is_wired = any(token in link for token in ("cable", "wired", "usb"))
        active_rate = state.rate_wired if is_wired else state.rate_wireless
        self._rate_row.set_title("Polling por cable" if is_wired else "Polling inalámbrico")
        active_idx = next((i for i, r in enumerate(RATE_OPTIONS) if r == active_rate),
                          RATE_OPTIONS.index(1000))
        self._rate_row.set_selected(active_idx)

        # --- Batería ---
        if state.battery is not None:
            suffix = "  · cargando" if state.charging else ("  · batería baja" if state.battery <= 20 else "")
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
            self._status_row.set_subtitle("Aplicando…")
            _worker(
                lambda: self._backend.set_dpi_stage(stage, dpi),
                lambda _: self._success("DPI aplicado"),
                self._on_error
            )
            return GLib.SOURCE_REMOVE

        self._pending[key] = GLib.timeout_add(400, fire)

    def _on_stage_changed(self, row: Adw.ComboRow, _ps: Any) -> None:
        if self._building:
            return
        stage = row.get_selected()
        self._status_row.set_subtitle("Aplicando…")
        # Marcar visualmente el perfil activo inmediatamente
        for i, drow in enumerate(self._dpi_rows):
            drow.set_title(f"{'▶ ' if i == stage else ''}Perfil {i + 1} — DPI")
        _worker(
            lambda: self._backend.set_active_stage(stage),
            lambda _: self._success("Perfil aplicado"),
            self._on_error
        )

    def _on_rate_changed(self, row: Adw.ComboRow, _ps: Any) -> None:
        if self._building:
            return
        hz = RATE_OPTIONS[row.get_selected()]
        self._status_row.set_subtitle("Aplicando…")
        _worker(lambda: self._backend.set_rate(hz), lambda _: self._success("Polling aplicado"), self._on_error)

    def _on_lod_changed(self, row: Adw.ComboRow, _ps: Any) -> None:
        if self._building:
            return
        mm = row.get_selected() + 1   # 0 → 1 mm, 1 → 2 mm
        self._status_row.set_subtitle("Aplicando…")
        _worker(lambda: self._backend.set_lod(mm), lambda _: self._success("LOD aplicado"), self._on_error)

    def _on_debounce_changed(self, scale: Gtk.Scale) -> None:
        if self._building:
            return
        ms = int(scale.get_value())
        old = self._pending.pop("debounce", None)
        if old:
            GLib.source_remove(old)

        def fire() -> bool:
            self._pending.pop("debounce", None)
            self._status_row.set_subtitle("Aplicando…")
            _worker(
                lambda: self._backend.set_debounce(ms),
                lambda _: self._success("Debounce aplicado"),
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
            self._status_row.set_subtitle("Aplicando…")
            _worker(
                lambda: self._backend.set_sleep(mins),
                lambda _: self._success("Reposo aplicado"),
                self._on_error
            )
            return GLib.SOURCE_REMOVE

        self._pending["sleep"] = GLib.timeout_add(400, fire)

    def _on_switch_changed(self, sw: Gtk.Switch, _ps: Any, title: str) -> None:
        if self._building:
            return
        val = sw.get_active()
        self._status_row.set_subtitle("Aplicando…")
        tl = title.lower()
        if "ripple" in tl:
            _worker(lambda: self._backend.set_ripple(val), lambda _: self._success("Ripple aplicado"), self._on_error)
        elif "angle" in tl:
            _worker(lambda: self._backend.set_angle_snap(val), lambda _: self._success("Angle snap aplicado"), self._on_error)
        elif "motion" in tl:
            _worker(lambda: self._backend.set_motion_sync(val), lambda _: self._success("Motion sync aplicado"), self._on_error)

    def _success(self, message: str) -> None:
        self._status_row.set_subtitle("Conectado")
        self._toast(message)

    @staticmethod
    def _preset_path() -> Path:
        return Path.home() / ".config" / "mchose-hub" / "presets.json"

    def _save_preset(self, _button: Gtk.Button) -> None:
        if not self._state:
            self._toast("Todavía no hay estado leído")
            return
        path = self._preset_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
            names = ["Gaming", "Trabajo", "FPS"]
            data[names[self._preset_name.get_selected()]] = {"stages": self._state.stages,
                                "active_stage": self._state.active_stage,
                                "rate_wireless": self._state.rate_wireless,
                                "lod": self._state.lod, "debounce": self._state.debounce,
                                "sleep": self._state.sleep, "ripple": self._state.ripple,
                                "angle_snap": self._state.angle_snap,
                                "motion_sync": self._state.motion_sync}
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            self._toast(f"Preset guardado: {names[self._preset_name.get_selected()]}")
        except (OSError, ValueError) as exc:
            self._toast(f"No se pudo guardar el preset: {exc}")

    def _load_preset(self, _button: Gtk.Button) -> None:
        try:
            names = ["Gaming", "Trabajo", "FPS"]
            data = json.loads(self._preset_path().read_text(encoding="utf-8"))[names[self._preset_name.get_selected()]]
            jobs = []
            for i, dpi in enumerate(data.get("stages", [])):
                jobs.append(lambda i=i, dpi=dpi: self._backend.set_dpi_stage(i, int(dpi)))
            if "active_stage" in data:
                jobs.append(lambda: self._backend.set_active_stage(int(data["active_stage"])))
            if "rate_wireless" in data:
                jobs.append(lambda: self._backend.set_rate(int(data["rate_wireless"])))
            jobs.extend([lambda: self._backend.set_lod(int(data.get("lod", 1))),
                         lambda: self._backend.set_debounce(int(data.get("debounce", 0))),
                         lambda: self._backend.set_sleep(int(data.get("sleep", 0))),
                         lambda: self._backend.set_motion_sync(bool(data.get("motion_sync", False)))])
            def apply_all() -> None:
                for job in jobs:
                    job()
            _worker(apply_all, lambda _: (self._toast("Preset cargado"), self._refresh())[1], self._on_error)
        except (OSError, ValueError, KeyError) as exc:
            self._toast(f"No se pudo cargar el preset: {exc}")

    def _reset_defaults(self, _button: Gtk.Button) -> None:
        defaults = MouseState()
        self._apply_state(defaults)
        _worker(lambda: [self._backend.set_dpi_stage(i, dpi) for i, dpi in enumerate(defaults.stages)],
                lambda _: self._success("Valores predeterminados restaurados"), self._on_error)

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
        if self._refresh_timer:
            GLib.source_remove(self._refresh_timer)
            self._refresh_timer = 0
