"""
gui.py — Interfaz GTK4 + libadwaita.

La GUI NO conoce ningún modelo concreto: construye sus controles leyendo `profile.features`
(choice → desplegable, slider → deslizador, color → selector de color, switch, battery, mapping).
Un perfil nuevo ⇒ una pantalla nueva sin tocar este archivo.

Reglas de oro:
  * Toda la E/S HID se hace en un hilo trabajador (clase Worker): jamás se bloquea la interfaz.
  * Los resultados vuelven al hilo de GTK con GLib.idle_add.
  * Cuando un comando lleva varios parámetros (p. ej. RGB = efecto+color+brillo+velocidad) la vista
    recuerda el valor de cada uno y reenvía el conjunto completo, para no «resetear» los demás.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, __version__  # noqa: E402
from .device import MchoseDevice, MchoseCliDevice, discover, discover_unprofiled  # noqa: E402
from .cli_backend import MchoseCliBackend, is_cli_available  # noqa: E402
from .cli_backend_methods import CliBackendMethodsMixin  # noqa: E402

# Tipo abstracto para compatibilidad con código existente que usa HidBackend:
class _GenericBackend:
    """Tipo abstracto para compatibilidad con código existente que usa HidBackend."""
    def execute(self, cmd: str) -> Optional[Dict[str, Any]]: ...
    def close(self) -> None: ...


class ConfigurableBackend(CliBackendMethodsMixin):
    """Combina _GenericBackend con métodos CLI para perfiles configurables.
    
    Este backend usa el binario 'mchose' de alexfrih/mchose-linux para interactuar
    con dispositivos MCHOSE, proporcionando acceso a todos los comandos del CLI:
    dpi, rate, rgb, battery, etc.
    
    Los perfiles JSON guardan la configuración por tipo de dispositivo y las 
    características dinámicas (choice/slider/color) se aplican mediante estos 
    comandos del backend CLI.
    """

    def __init__(self, dev: Optional[_GenericBackend] = None):
        """Dev puede ser un HidBackend para obtener info del descriptor (solo lectura)."""
        self._hid = dev or _GenericBackend()  # Empty generic backend si no hay HID
        self._cli = MchoseCliBackend()

    def execute(self, cmd: str) -> Dict[str, Any]:
        """Ejecuta un comando CLI y devuelve el resultado como dict."""
        try:
            result = self._cli.execute(cmd)
            return {k: v for k, v in result.items() if v is not None}  # Solo valores no None
        except Exception as e:
            log.error(f"Error ejecutando comando CLI '{cmd}': {e}")
            raise

    def close(self) -> None:
        """No hay recursos que liberar con el backend CLI."""
        self._cli.close()

    def _cli_execute(self, cmd: str) -> Any:
        """Delegación al backend CLI para ejecución nativa."""
        return self._cli.execute(cmd)

from typing import Optional, Dict, Any  # noqa: E402
from .hidinfo import (device_report, format_report, is_vendor_node, node_str,  # noqa: E402
                      parse_report_descriptor, save_base_profile, VENDOR_PAGE)
from .hotplug import HotplugMonitor  # noqa: E402
from .profile import FeatureSpec, ParamSpec, load_profiles  # noqa: E402
from .protocol import ProtocolError  # noqa: E402

log = logging.getLogger(__name__)

STATE_TEXT = {
    _DeviceStatus.CLOSING: "Conectando…",
    _DeviceStatus.READY: "Conectado",
    _DeviceStatus.ASLEEP: "En reposo: muévelo o pulsa un botón",
    _DeviceStatus.DISCONNECTED: "Desconectado",
}
KIND_ICON = {"mouse": "input-mouse-symbolic", "keyboard": "input-keyboard-symbolic"}


def _row(title: str, subtitle: str = "") -> Adw.ActionRow:
    """ActionRow sin markup (los textos vienen de JSON de terceros)."""
    row = Adw.ActionRow(title=title, subtitle=subtitle)
    if hasattr(row, "set_use_markup"):          # libadwaita ≥ 1.1
        row.set_use_markup(False)
    return row


def _make_color_button(rgba: Gdk.RGBA) -> Gtk.Widget:
    if hasattr(Gtk, "ColorDialogButton"):       # GTK ≥ 4.10
        dialog = Gtk.ColorDialog()
        dialog.set_with_alpha(False)
        btn = Gtk.ColorDialogButton.new(dialog)
    else:                                       # GTK 4.0–4.8 (ColorButton está deprecado pero funciona)
        btn = Gtk.ColorButton()
        btn.set_use_alpha(False)
    btn.set_valign(Gtk.Align.CENTER)
    btn.set_rgba(rgba)
    return btn


class Worker:
    """Ejecuta funciones bloqueantes en UN hilo (en orden) y entrega el resultado en el hilo de GTK."""

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hid")

    def run(self, fn: Callable[[], Any], on_ok: Optional[Callable[[Any], None]] = None,
            on_err: Optional[Callable[[Exception], None]] = None) -> None:
        def job() -> None:
            try:
                result = fn()
            except Exception as exc:            # se reenvía a la GUI; nunca muere el hilo
                GLib.idle_add(self._deliver, on_err, exc)
            else:
                GLib.idle_add(self._deliver, on_ok, result)
        try:
            self._pool.submit(job)
        except RuntimeError:                    # el pool ya se cerró (ventana cerrándose)
            pass

    @staticmethod
    def _deliver(callback: Optional[Callable[[Any], None]], arg: Any) -> bool:
        if callback:
            callback(arg)
        return GLib.SOURCE_REMOVE

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


# ============================================================================
# Página de un dispositivo
# ============================================================================
class CliDeviceView(Adw.PreferencesPage):
    """Controles de UN dispositivo usando el backend CLI (mchose-linux).

    Inyectado desde MainWindow para evitar múltiples instancias del backend.
    """

    def __init__(self, toast: Callable[[str], None], backend: MchoseCliBackend, profile: Profile) -> None:
        super().__init__()
        self._backend = backend  # Backend CLI verificado en hardware
        self._toast = toast
        self._profile = profile  # El perfil del dispositivo (lecto desde el JSON de la app)
        self._worker = Worker()
        self._values: Dict[Tuple[str, Tuple], Dict[str, Any]] = {}   # estado por (comando, fixed)
        self._pending: Dict[str, int] = {}       # temporizadores de «antirrebote» de la UI
        self._battery: Optional[Tuple[FeatureSpec, Adw.ActionRow, Gtk.LevelBar]] = None
        self._battery_busy = False
        self._battery_timer = 0
        self._groups: Dict[str, Adw.PreferencesGroup] = {}

        # Active profile management
        self._active_profile_idx: Optional[int] = None
        self._profile_rows: List[Tuple[FeatureSpec, int, Adw.ActionRow]] = []  # (feat, idx, row)
        
        # El backend CLI no requiere mantener un canal abierto; cada comando es independiente.
        self._worker.run(self._read_device_state,
                         self._apply_device_state,
                         lambda exc: self._on_error(exc, quiet=True),
                         self._on_error)

    # -- utilidades ----------------------------------------------------------
    def _group(self, name: str) -> Adw.PreferencesGroup:
        if name not in self._groups:
            group = Adw.PreferencesGroup(title=name)
            self.add(group)
            self._groups[name] = group
        return self._groups[name]

    def _param(self, feat: FeatureSpec) -> ParamSpec:
        return self._profile.commands[feat.command].params[feat.param]

    def _state_for(self, command: str, fixed: Dict[str, Any]) -> Dict[str, Any]:
        """Valores actuales de un comando (por defecto + fixed). Se comparte entre features."""
        key = (command, tuple(sorted(fixed.items())))
        if key not in self._values:
            values = {n: p.default for n, p in self._profile.commands[command].params.items()
                      if p.default is not None}
            values.update(fixed)
            self._values[key] = values
        return self._values[key]

    def _parse_rgb_hex(self, hex_color: str) -> Tuple[int, int, int]:
        """Convierte un color hexadecimal '#RRGGBB' a (r, g, b)."""
        hex_color = hex_color.lstrip('#')
        try:
            return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
        except (ValueError, IndexError):
            # Fallback a blanco si el formato es inválido
            return 255, 255, 255

    def _refresh_status(self) -> None:
        self._status.set_subtitle(STATE_TEXT[self.device.state])

    def _on_error(self, exc: Exception, quiet: bool = False) -> None:
        # El nuevo backend CLI no usa estos errores específicos
        msg = str(exc) if not quiet else ""
        self.toast(msg)
        self._refresh_status()
        if not quiet:
            self._toast(msg)

    def _apply(self, feat: FeatureSpec, value: Any, extra_fixed: Optional[Dict[str, Any]] = None) -> None:
        """Guarda el valor del parámetro y envía el comando COMPLETO en el hilo trabajador."""
        fixed = dict(feat.fixed)
        fixed.update(extra_fixed or {})
        values = self._state_for(feat.command, fixed)
        values[feat.param] = value
        payload = dict(values)                   # copia: el hilo no comparte el dict mutable
        
        # Mapeo de comandos a métodos específicos del backend CLI
        def apply_command():
            try:
                if feat.command == "set_dpi":
                    # Necesitamos stage_index (extra_fixed) y dpi (value)
                    stage = extra_fixed.get("stage", 0) if extra_fixed else 0
                    self._backend.set_dpi_stage(stage, value)
                elif feat.command == "set_rate":
                    # set_rate usa un índice global (0-5), no se pasa el stage aquí
                    self._backend.set_rate(value)
                elif feat.command == "set_lod":
                    self._backend.set_lod(value)
                elif feat.command == "set_debounce":
                    self._backend.set_debounce(value)
                elif feat.command == "set_sleep":
                    # set_sleep espera minutos enteros
                    self._backend.set_sleep(int(value))
                elif feat.command == "set_motion_sync":
                    on_str = "on" if value else "off"
                    self._backend.set_motion_sync(on_str == "on")
                elif feat.command == "set_ripple":
                    on_str = "on" if value else "off"
                    self._backend.set_ripple(on_str == "on")
                elif feat.command == "set_angle_snap":
                    on_str = "on" if value else "off"
                    self._backend.set_angle_snap(on_str == "on")
                elif feat.command == "switch_active_profile":
                    # switch_active_profile cambia el stage activo, no modifica valores del perfil
                    stage = extra_fixed.get("stage", 0) if extra_fixed else 0
                    self._backend.set_active_stage(stage)
                elif feat.command == "set_rgb_effect":
                    self._backend.set_rgb_effect(value)
                elif feat.command == "set_rgb_speed":
                    self._backend.set_rgb_speed(value)
                elif feat.command == "set_rgb_brightness":
                    self._backend.set_rgb_brightness(value)
                elif feat.command == "set_rgb_color":
                    # RGB color necesita R, G, B por separado
                    r = extra_fixed.get("r", value) if extra_fixed and "r" in extra_fixed else None
                    g = extra_fixed.get("g", None) if extra_fixed and "g" in extra_fixed else None
                    b = extra_fixed.get("b", None) if extra_fixed and "b" in extra_fixed else None
                    # Para set_rgb_color, value es el color hex o un canal RGB individual
                    # Necesitamos manejar ambos casos: color completo o cambiar un solo canal
                    if isinstance(value, str):
                        # Es un color hexadecimal o nombre
                        self._backend.set_rgb_color(*self._parse_rgb_hex(value))
                    else:
                        # Cambiar un canal individual (r/g/b)
                        r_val = extra_fixed.get("r", value)
                        g_val = extra_fixed.get("g", None)
                        b_val = extra_fixed.get("b", None)
                        self._backend.set_rgb_color(r_val, g_val, b_val)
                else:
                    # Para comandos desconocidos o no soportados, saltar
                    return False
                return True
            except Exception as e:
                log.exception("Error aplicando comando CLI")
                raise
        
        self._worker.run(apply_command, 
                         lambda: GLib.idle_add(self._refresh_status), 
                         self._on_error)

    # -- lectura del estado real del ratón -------------------------------------
    def _read_device_state(self) -> Optional[Dict[str, Any]]:
        """Lee el estado del ratón desde el backend CLI y devuelve un dict con todo.
        Se llama al iniciar la vista para refrescar los controles."""
        result: Dict[str, Any] = {}
        
        # Usamos get_state() del backend CLI que lee 'mchose show' (incluye stages)
        # Luego extraemos battery/firmware/link si están disponibles en parse_info
        try:
            state = self._backend.get_state()
            
            # Convertir MouseState a dict para la UI
            result.update({
                "dpi": state.stages[self._active_stage],
                "rate": state.rate,
                "lod": state.lod,
                "debounce": state.debounce,
                "sleep_minutes": state.sleep_minutes,
                "motion_sync": state.motion_sync,
                "ripple": state.ripple,
                "angle_snap": state.angle_snap,
            })
            
            # Si parse_info falló, usamos valores por defecto o None
            if state.battery is not None:
                result["battery"] = state.battery
            if state.charging is not None:
                result["charging"] = state.charging
            if state.firmware is not None:
                result["firmware"] = state.firmware
            if state.link is not None:
                result["link"] = state.link
                
        except Exception as exc:
            self._on_error(exc, quiet=True)
            return None
        
        # Leer RGB si el perfil lo soporta (get_rgb es independiente del canal HID)
        if "get_rgb" in self._profile.commands:
            try:
                rgb = self._backend.get_rgb()  # Este comando ya está en MchoseCliBackend
                result["rgb"] = rgb
            except Exception as exc:
                self._on_error(exc, quiet=True)
        
        return result

    def _apply_device_state(self, state: Optional[Dict[str, Any]]) -> None:
        """Aplica el estado leído del ratón a los controles de la GUI (hilo GTK)."""
        if not state:
            self._refresh_status()
            return
        updates: Dict[str, Any] = {}
        if "dpi0" in state: updates["dpi0"] = state["dpi0"]
        if "dpi1" in state: updates["dpi1"] = state["dpi1"]
        if "dpi2" in state: updates["dpi2"] = state["dpi2"]
        if "dpi3" in state: updates["dpi3"] = state["dpi3"]
        if "dpi4" in state: updates["dpi4"] = state["dpi4"]
        if "dpi5" in state: updates["dpi5"] = state["dpi5"]
        # Índices de polling: coinciden directamente con el value de las choices (0-5)
        if "usb_rate_index" in state:
            updates["polling_usb"] = state["usb_rate_index"]
        if "g_rate_index" in state:
            updates["polling_wireless"] = state["g_rate_index"]
        # Perfil activo
        if "g_dpi_index" in state:
            updates["active_profile"] = state["g_dpi_index"]
        if "debounce" in state: updates["debounce"] = state["debounce"]
        if "sleep"    in state: updates["sleep_minutes"] = state["sleep"]
        # Sensor bits: bit2=ripple, bit3=angle_snap, bit4=motion_sync
        if "sensor" in state:
            sb = state["sensor"]
            updates["ripple"]      = bool(sb & (1 << 2))
            updates["angle_snap"]  = bool(sb & (1 << 3))
            updates["motion_sync"] = bool(sb & (1 << 4))
        # RGB
        if "rgb_effect_id" in state: updates["rgb_effect"] = state["rgb_effect_id"]
        if "rgb_brightness" in state: updates["rgb_brightness"] = int(state["rgb_brightness"] * 100 / 100)
        if "rgb_speed" in state: updates["rgb_speed"] = state["rgb_speed"]
        if "rgb_r" in state and "rgb_g" in state and "rgb_b" in state:
            updates["rgb_color"] = "#%02x%02x%02x" % (state["rgb_r"], state["rgb_g"], state["rgb_b"])

        # Actualizar cada widget por feature id
        # Primero actualizar los valores en _state_for para todos los comandos
        # que compartan parámetros (p. ej. set_polling tiene usb_rate y free_rate)
        param_to_val: Dict[str, Any] = {
            "dpi0": updates.get("dpi0"), "dpi1": updates.get("dpi1"),
            "dpi2": updates.get("dpi2"), "dpi3": updates.get("dpi3"),
            "dpi4": updates.get("dpi4"), "dpi5": updates.get("dpi5"),
            "usb_rate": updates.get("polling_usb"),
            "free_rate": updates.get("polling_wireless"),
        }
        for key, vals in self._values.items():
            for pname, pval in param_to_val.items():
                if pname in vals and pval is not None:
                    vals[pname] = pval

        # Update active profile row first (before other widgets to avoid race)
        if "active_profile" in updates:
            active_idx = updates["active_profile"]
            for feat, idx, row in self._profile_rows:
                if feat.id == "active_profile":
                    current = row.get_selected()
                    p = self._param(feat)
                    model = row.get_model()
                    if isinstance(model, Gtk.StringList):
                        new_label = model.get(current)
                        # Update label to show profile index
                        new_label = new_label.replace("Profile ", f"Profile {idx} - ")
                        model.set(current, new_label)
                    else:  # Gtk.ListStore or empty model
                        labels = [model.get_value(i).unpack()[0] for i in range(model.get_n_items())]
                        idx_in_list = next((i for i, c in enumerate(labels) if c == labels[idx]), 0)
                        model.set(idx, Gtk.TreeModelIter())
                    row.set_selected(active_idx)

        for feat in self._profile.features:
            if feat.id not in updates:
                continue
            new_val = updates[feat.id]
            if new_val is None:
                continue
            # Guardar en el estado interno del motor
            key = (feat.command, tuple(sorted(feat.fixed.items())))
            if key in self._values and feat.param:
                self._values[key][feat.param] = new_val
            # Actualizar el widget sin disparar el callback de escritura
            widget = self._find_widget(feat.id)
            if widget is None:
                continue
            self._silent_update(widget, feat, new_val)
        self._refresh_status()

    def _find_widget(self, feat_id: str) -> Optional[Gtk.Widget]:
        """Devuelve el widget de control de una feature (Scale, ComboRow, Switch, ColorButton)."""
        for feat in self._profile.features:
            if feat.id != feat_id:
                continue
            # Recorremos el árbol buscando por feat_id almacenado como dato
            def walk(w: Gtk.Widget) -> Optional[Gtk.Widget]:
                data = getattr(w, '_mchose_feat_id', None)
                if data == feat_id:
                    return w
                c = w.get_first_child()
                while c:
                    r = walk(c)
                    if r: return r
                    c = c.get_next_sibling()
                return None
            return walk(self)
        return None

    def _silent_update(self, widget: Gtk.Widget, feat: FeatureSpec, value: Any) -> None:
        """Actualiza el widget sin disparar el callback de envío al ratón."""
        try:
            if isinstance(widget, Gtk.Scale):
                widget.handler_block_matched(0, GLib.SignalMatchType.TYPE, 0, None, None, None, None)
                widget.set_value(float(value))
                widget.handler_unblock_matched(0, GLib.SignalMatchType.TYPE, 0, None, None, None, None)
            elif isinstance(widget, Adw.ComboRow):
                p = self._param(feat)
                idx = next((i for i, c in enumerate(p.choices) if c.value == value), None)
                if idx is not None:
                    widget.handler_block_matched(0, GLib.SignalMatchType.TYPE, 0, None, None, None, None)
                    widget.set_selected(idx)
                    widget.handler_unblock_matched(0, GLib.SignalMatchType.TYPE, 0, None, None, None, None)
            elif isinstance(widget, Gtk.Switch):
                widget.handler_block_matched(0, GLib.SignalMatchType.TYPE, 0, None, None, None, None)
                widget.set_active(bool(value))
                widget.handler_unblock_matched(0, GLib.SignalMatchType.TYPE, 0, None, None, None, None)
        except Exception:
            pass   # Si falla el bloqueo de señal, al menos actualiza el valor visual


    # -- widgets -------------------------------------------------------------
    def _build_choice(self, feat: FeatureSpec) -> None:
        p = self._param(feat)
        row = Adw.ComboRow(title=feat.label, subtitle=feat.description)
        row.set_model(Gtk.StringList.new([c.label for c in p.choices]))
        current = self._state_for(feat.command, feat.fixed).get(feat.param)
        row.set_selected(next((i for i, c in enumerate(p.choices) if c.value == current), 0))
        row._mchose_feat_id = feat.id
        # Se conecta DESPUÉS de fijar la selección inicial para no enviar nada al construir la fila.
        row.connect("notify::selected", lambda r, _ps: self._apply(feat, p.choices[r.get_selected()].value))
        self._group(feat.group).add(row)

    def _build_slider(self, feat: FeatureSpec) -> None:
        p = self._param(feat)
        row = _row(feat.label, p.unit or feat.description)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, p.min, p.max, p.step)
        scale.set_digits(0)
        scale.set_draw_value(True)
        scale.set_value_pos(Gtk.PositionType.RIGHT)
        scale.set_hexpand(True)
        scale.set_valign(Gtk.Align.CENTER)
        scale.set_size_request(260, -1)
        scale.set_value(float(self._state_for(feat.command, feat.fixed).get(feat.param, p.min)))
        scale._mchose_feat_id = feat.id
        scale.connect("value-changed", self._on_slider, feat, p)
        row.add_suffix(scale)
        self._group(feat.group).add(row)

    def _on_slider(self, scale: Gtk.Scale, feat: FeatureSpec, p: ParamSpec) -> None:
        # Ajusta al múltiplo de `step` y espera 250 ms de calma antes de enviar (no inundar el USB).
        snapped = int(p.min + round((scale.get_value() - p.min) / p.step) * p.step)
        snapped = max(int(p.min), min(int(p.max), snapped))
        if abs(scale.get_value() - snapped) > 1e-6:
            scale.set_value(snapped)             # vuelve a disparar la señal con el valor ya ajustado
            return
        old = self._pending.pop(feat.id, None)
        if old:
            GLib.source_remove(old)

        def fire() -> bool:
            self._pending.pop(feat.id, None)
            self._apply(feat, snapped)
            return GLib.SOURCE_REMOVE
        self._pending[feat.id] = GLib.timeout_add(250, fire)

    def _build_color(self, feat: FeatureSpec) -> None:
        rgba = Gdk.RGBA()
        if not rgba.parse(str(self._state_for(feat.command, feat.fixed).get(feat.param, "#ffffff"))):
            rgba.parse("#ffffff")
        button = _make_color_button(rgba)
        button.connect("notify::rgba", self._on_color, feat)
        row = _row(feat.label, feat.description)
        row.add_suffix(button)
        self._group(feat.group).add(row)

    def _on_color(self, button: Gtk.Widget, _pspec: Any, feat: FeatureSpec) -> None:
        c = button.get_rgba()
        self._apply(feat, "#%02x%02x%02x" % (round(c.red * 255), round(c.green * 255), round(c.blue * 255)))

    def _build_switch(self, feat: FeatureSpec) -> None:
        switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        switch.set_active(bool(self._state_for(feat.command, feat.fixed).get(feat.param, False)))
        switch._mchose_feat_id = feat.id
        switch.connect("notify::active", lambda s, _ps: self._apply(feat, s.get_active()))
        row = _row(feat.label, feat.description)
        row.add_suffix(switch)
        row.set_activatable_widget(switch)
        self._group(feat.group).add(row)

    def _build_mapping(self, feat: FeatureSpec) -> None:
        """Una fila desplegable por botón/tecla (cada `slot` aporta sus propios `fixed`)."""
        p = self._param(feat)
        labels = Gtk.StringList.new([c.label for c in p.choices])
        for slot in feat.slots:
            row = Adw.ComboRow(title=slot.label)
            row.set_model(labels)
            wanted = slot.default if slot.default is not None else p.default
            row.set_selected(next((i for i, c in enumerate(p.choices) if c.value == wanted), 0))
            row.connect("notify::selected",
                        lambda r, _ps, s=slot: self._apply(feat, p.choices[r.get_selected()].value, s.fixed))
            self._group(feat.group).add(row)

    def _build_battery(self, feat: FeatureSpec) -> None:
        row = _row(feat.label, "—")
        bar = Gtk.LevelBar(min_value=0, max_value=100, value=0)
        bar.set_size_request(120, -1)
        bar.set_valign(Gtk.Align.CENTER)
        row.add_suffix(bar)
        self._head.add(row)
        self._battery = (feat, row, bar)
        self._refresh_battery()
        self._battery_timer = GLib.timeout_add_seconds(feat.poll_seconds, self._poll_battery)

    def _poll_battery(self) -> bool:
        self._refresh_battery()
        return GLib.SOURCE_CONTINUE              # también reintenta si el dispositivo estaba dormido

    def _refresh_battery(self) -> None:
        if self._battery is None or self._battery_busy:
            return
        feat, row, bar = self._battery
        self._battery_busy = True

        def done(result: Optional[Dict[str, Any]]) -> None:
            self._battery_busy = False
            pct = (result or {}).get(feat.percent)
            if pct is not None:
                charging = (result or {}).get(feat.charging) if feat.charging else None
                extra = (" · cargando" if charging is True
                         else f" · carga: {charging}" if isinstance(charging, str) else "")
                row.set_subtitle(f"{pct} %{extra}")
                bar.set_value(float(pct))
            self._refresh_status()

        def failed(exc: Exception) -> None:
            self._battery_busy = False
            row.set_subtitle("No disponible")
            self._on_error(exc, quiet=True)      # sin avisos emergentes en el sondeo periódico

        self._worker.run(lambda: self._backend.execute(feat.command), done, failed)

    # -- cierre --------------------------------------------------------------
    def shutdown(self) -> None:
        for src in list(self._pending.values()) + ([self._battery_timer] if self._battery_timer else []):
            GLib.source_remove(src)
        self._pending.clear()
        self._battery_timer = 0
        self._worker.shutdown()
        # close() espera al candado si hay E/S en curso: se hace fuera del hilo de GTK
        threading.Thread(target=self._backend.close, daemon=True).start()


# ============================================================================
# Dispositivo detectado SIN perfil
# ============================================================================
def _access(path: bytes) -> Optional[bool]:
    """¿Puede este usuario abrir el nodo? None si no aplica (backend que no usa /dev/hidrawN)."""
    p = node_str(path)
    return os.access(p, os.R_OK | os.W_OK) if p.startswith("/dev/") else None


class UnknownDeviceView(Adw.PreferencesPage):
    """Siempre se muestra lo detectado: interfaces, permisos, reports del fabricante y acciones."""

    def __init__(self, dev: Any, backend: _GenericBackend | MchoseCliBackend, toast: Callable[[str], None],
                 on_profile_saved: Callable[[], None]) -> None:
        super().__init__()
        self._dev = dev
        self._backend = backend
        self._worker = Worker()
        self._dynamic: List[Tuple[Adw.PreferencesGroup, Gtk.Widget]] = []   # filas que se regeneran

        head = Adw.PreferencesGroup(title=dev.display_name,
                                    description=dev.vid_pid + (f" · serie {dev.serial}" if dev.serial else ""))
        head.add(_row("Estado", f"Detectado, {dev.reason}"))
        self.add(head)
        self._perm = Adw.PreferencesGroup(title="Permisos")
        self._nodes = Adw.PreferencesGroup(title="Interfaces HID")
        self._reports = Adw.PreferencesGroup(title="Reports del fabricante (según el descriptor)")
        actions = Adw.PreferencesGroup(title="Acciones")
        for g in (self._perm, self._nodes, self._reports, actions):
            self.add(g)

        create = Gtk.Button(label="Crear", valign=Gtk.Align.CENTER)
        create.add_css_class("suggested-action")
        create.connect("clicked", self._save_base_profile_cli)
        row = _row("Crear perfil base", "Guarda un perfil con este VID:PID; después se le añaden los comandos del fabricante.")
        row.add_suffix(create)
        actions.add(row)
        copy = Gtk.Button(label="Copiar", valign=Gtk.Align.CENTER)
        copy.connect("clicked", self._copy_report)
        row = _row("Copiar informe", "Interfaces y reports al portapapeles, para pegarlos al pedir ayuda.")
        row.add_suffix(copy)
        actions.add(row)
        self._fill()

    # -- contenido dinámico (se regenera tras dar permisos) -----------------------
    def _add(self, group: Adw.PreferencesGroup, row: Gtk.Widget) -> None:
        group.add(row)
        self._dynamic.append((group, row))

    def _fill(self) -> None:
        for group, row in self._dynamic:
            group.remove(row)
        self._dynamic.clear()
        denied = False
        vendor_paths = []
        for path, entries in self._dev.nodes().items():
            usages = " ".join(f"{e.get('usage_page', 0):04X}/{e.get('usage', 0):04X}" for e in entries)
            vendor = is_vendor_node(entries)
            ok = _access(path)
            parts = [f"IF{entries[0].get('interface_number', -1)}", usages,
                     "propia del fabricante" if vendor else "",
                     {True: "acceso: sí", False: "acceso: NO", None: ""}[ok]]
            self._add(self._nodes, _row(node_str(path), " · ".join(p for p in parts if p)))
            if vendor:
                vendor_paths.append(path)
                denied = denied or ok is False

        if denied:
            grant = Gtk.Button(label="Dar permisos", valign=Gtk.Align.CENTER)
            grant.add_css_class("suggested-action")
            grant.connect("clicked", self._grant_access)
            row = _row("Faltan permisos sobre la interfaz del fabricante",
                       f"Instala las reglas udev para {self._dev.vendor_id:04X}. En terminal: "
                       f"sudo mchose-hub udev --install")
            row.add_suffix(grant)
            self._add(self._perm, row)
        else:
            self._add(self._perm, _row("Acceso a la interfaz del fabricante", "Sin problemas detectados"))

        if not vendor_paths:
            self._add(self._reports, _row("Sin interfaz propia del fabricante",
                                          "No hay usage page ≥ FF00: no hay nada que configurar por HID."))
        for path in vendor_paths:
            desc = self._backend.report_descriptor(path)
            reports = [r for r in parse_report_descriptor(desc) if r.usage_page >= VENDOR_PAGE] if desc else []
            if not reports:
                self._add(self._reports, _row(node_str(path), "Descriptor no disponible o sin reports"))
            for r in reports:
                rid = f"ID 0x{r.report_id:02X}" if r.report_id else "sin ID"
                self._add(self._reports, _row(f"{r.kind} · {rid}",
                                              f"{r.size} B (frame de {r.frame_len} B) · {r.usage_page:04X}/{r.usage:04X}"))

    # -- acciones -----------------------------------------------------------------
    def _grant_access(self, _btn: Gtk.Button) -> None:
        launcher, pkexec = shutil.which("mchose-hub"), shutil.which("pkexec")
        vid = f"0x{self._dev.vendor_id:04X}"
        if not (launcher and pkexec):
            self._toast(f"Ejecuta en una terminal: sudo mchose-hub udev --install --vid {vid}")
            return
        self._worker.run(
            lambda: subprocess.run([pkexec, launcher, "udev", "--install", "--vid", vid],
                                   capture_output=True, text=True, timeout=180),
            self._after_grant, lambda exc: self._toast(f"No se pudo pedir permisos: {exc}"))

    def _after_grant(self, proc: "subprocess.CompletedProcess[str]") -> None:
        if proc.returncode == 0:
            self._toast("Reglas instaladas. Si sigue sin acceso, reconecta el dispositivo.")
            GLib.timeout_add(800, lambda: (self._fill(), GLib.SOURCE_REMOVE)[1])
        else:
            self._toast("No se instalaron los permisos (¿cancelado?). "
                        + (proc.stderr or "").strip()[:100])

    def _create_profile(self, _btn: Gtk.Button) -> None:
        """Crea un perfil base para el dispositivo usando save_base_profile() o _save_base_profile_cli()."""
        self._save_base_profile_cli(_btn)  # Reutiliza la misma lógica

    def _copy_report(self, _btn: Gtk.Button) -> None:
        """Obtiene y copia al portapapeles el informe del dispositivo usando el backend CLI (informe de estado actual + interfaces)."""
        self.get_clipboard().set_text(device_report(self._dev, self._backend))
        self._toast("Informe copiado al portapapeles")

    def _save_base_profile_cli(self, _btn: Gtk.Button) -> None:
        """Guarda el perfil base usando cli_backend.save_base_profile()."""
        try:
            path = cli_backend.save_base_profile(self._dev, self._backend)
        except OSError as exc:
            self._toast(f"No se pudo guardar el perfil: {exc}")
            return
        self._toast(f"Perfil base guardado en {path}")
        self._on_saved()  # recarga perfiles y refresca la lista
        try:
            path = cli_backend.save_base_profile(self._dev, self._backend)
        except OSError as exc:
            self._toast(f"No se pudo guardar el perfil: {exc}")
            return
        self._toast(f"Perfil base guardado en {path}")
        self._on_saved()  # recarga perfiles y refresca la lista

    def shutdown(self) -> None:
        self._worker.shutdown()


# ============================================================================
# Ventana principal
# ============================================================================
class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, backend: MchoseCliBackend, profiles: list,
                 errors: List[str], demo: bool) -> None:
        super().__init__(application=app, title="MCHOSE Linux Hub" + (" (demo)" if demo else ""),
                         default_width=940, default_height=660)
        self._backend = backend  # Backend CLI verificado en hardware
        self._profiles = profiles
        self._demo = demo
        self._show_all = False
        self._worker = Worker()
        self._views: Dict[str, Gtk.Widget] = {}
        self._rows: Dict[str, Adw.ActionRow] = {}
        self._row_keys: Dict[Adw.ActionRow, str] = {}

        self._toasts = Adw.ToastOverlay()
        header = Adw.HeaderBar()
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Buscar dispositivos")
        refresh.connect("clicked", lambda _b: self.refresh_devices())
        header.pack_start(refresh)
        show_all = Gtk.ToggleButton(label="Todos los HID",
                                    tooltip_text="Mostrar también los dispositivos que no son MCHOSE")
        show_all.connect("toggled", self._on_show_all)
        header.pack_end(show_all)

        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self._list.add_css_class("navigation-sidebar")
        self._list.connect("row-selected", self._on_row_selected)
        sidebar = Gtk.ScrolledWindow(child=self._list, hscrollbar_policy=Gtk.PolicyType.NEVER, width_request=250)

        self._stack = Gtk.Stack(hexpand=True, vexpand=True)
        self._stack.add_named(
            Adw.StatusPage(icon_name="input-mouse-symbolic", title="Ningún dispositivo MCHOSE",
                           description="Conecta un ratón o teclado MCHOSE: aparecerá aunque aún no tenga perfil.\n"
                                       "La lista se actualiza sola al enchufar o desenchufar."), "empty")

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True)
        body.append(sidebar)
        body.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        body.append(self._stack)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(header)
        root.append(body)
        self._toasts.set_child(root)
        self.set_content(self._toasts)
        self.connect("close-request", self._on_close)

        if errors:
            self.toast(f"{len(errors)} perfil(es) con errores; ejecuta «mchose-hub validate»")

        # Hot-plug: el monitor avisa desde otro hilo → se reenvía al hilo de GTK.
        self._monitor = HotplugMonitor(lambda: GLib.idle_add(self.refresh_devices),
                                       enumerate_fn=backend.enumerate, force_polling=demo)
        self._monitor.start()
        self.refresh_devices()

    def toast(self, message: str) -> None:
        self._toasts.add_toast(Adw.Toast.new(message))

    # -- sincronización de la lista con lo conectado ----------------------------
    def _on_show_all(self, button: Gtk.ToggleButton) -> None:
        self._show_all = button.get_active()
        self.refresh_devices()

    def reload_profiles(self) -> None:
        """Vuelve a leer los perfiles del disco (p. ej. tras crear uno desde la GUI) y refresca."""
        self._profiles, errors = load_profiles(include_disabled=self._demo)
        if errors:
            self.toast(f"{len(errors)} perfil(es) con errores; ejecuta «mchose-hub validate»")
        self.refresh_devices()

    def refresh_devices(self) -> None:
        profiles, show_all = self._profiles, self._show_all

        def scan() -> Tuple[List[DeviceInfo], List[UnknownDevice]]:
            infos = discover(profiles, self._backend)
            return infos, discover_unprofiled(profiles, self._backend, infos, show_all)
        self._worker.run(scan, self._apply_devices,
                         lambda exc: self.toast(f"Error al buscar dispositivos: {exc}"))

    def _apply_devices(self, result: Tuple[List[DeviceInfo], List[UnknownDevice]]) -> None:
        infos, unknowns = result
        present: Dict[str, Any] = {i.key: i for i in infos}
        present.update({u.key: u for u in unknowns})
        for key in [k for k in self._views if k not in present]:
            self._remove_device(key)
        for key, item in present.items():
            if key not in self._views:
                self._add_device(item)
        self._sync_visible_page()

    def _add_device(self, item: Any) -> None:
        if isinstance(item, UnknownDevice):      # detectado pero sin perfil: se muestra igualmente
            view: Gtk.Widget = UnknownDeviceView(item, self._backend, self.toast, self.reload_profiles)
            row = _row(item.display_name, f"{item.vid_pid} · sin perfil")
            row.add_prefix(Gtk.Image.new_from_icon_name("dialog-question-symbolic"))
        elif item.profile.kind == "mouse" and MchoseCliBackend.is_available():
            # Usar el CLI verificado en hardware como backend si está instalado
            view = CliDeviceView(self.toast, self._backend, item.profile)  # pasar backend y profile desde MainWindow
            row = _row(item.display_name, f"{item.vid_pid} · mchose-linux")
            row.add_prefix(Gtk.Image.new_from_icon_name("input-mouse-symbolic"))
        else:
            # Usar el backend CLI (mchose-linux) como estándar por ahora
            view = CliDeviceView(self.toast, self._backend, item.profile)  # pasar backend y profile desde MainWindow
            row = _row(item.display_name, f"{item.vid_pid} · mchose-linux")
            row.add_prefix(Gtk.Image.new_from_icon_name(KIND_ICON.get(item.profile.kind, "input-gaming-symbolic")))
        self._stack.add_named(view, item.key)
        self._list.append(row)
        self._views[item.key], self._rows[item.key], self._row_keys[row] = view, row, item.key
        if self._list.get_selected_row() is None:
            self._list.select_row(row)

    def _remove_device(self, key: str) -> None:
        view, row = self._views.pop(key), self._rows.pop(key)
        self._row_keys.pop(row, None)
        view.shutdown()
        self._stack.remove(view)
        self._list.remove(row)
        if not key.startswith("unk:"):
            self.toast("Dispositivo desconectado")

    def _sync_visible_page(self) -> None:
        if not self._views:
            self._stack.set_visible_child_name("empty")
            return
        if self._list.get_selected_row() is None:
            self._list.select_row(next(iter(self._rows.values())))

    def _on_row_selected(self, _listbox: Gtk.ListBox, row: Optional[Adw.ActionRow]) -> None:
        key = self._row_keys.get(row) if row is not None else None
        self._stack.set_visible_child_name(key if key in self._views else "empty")

    def _on_close(self, _win: Gtk.Window) -> bool:
        self._monitor.stop()
        self._worker.shutdown()
        for view in self._views.values():
            view.shutdown()
        return False                             # False = permitir el cierre


class ErrorWindow(Adw.ApplicationWindow):
    """Se muestra si falta la biblioteca HID: un error visible vale más que uno en la terminal."""

    def __init__(self, app: Adw.Application, message: str) -> None:
        super().__init__(application=app, title="MCHOSE Linux Hub", default_width=560, default_height=360)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(Adw.HeaderBar())
        box.append(Adw.StatusPage(icon_name="dialog-error-symbolic", title="Falta una dependencia",
                                  description=message, vexpand=True))
        self.set_content(box)


class HubApp(Adw.Application):
    def __init__(self, demo: bool = False) -> None:
        flags = getattr(Gio.ApplicationFlags, "DEFAULT_FLAGS", Gio.ApplicationFlags.FLAGS_NONE)
        super().__init__(application_id=APP_ID, flags=flags)
        self._demo = demo

    def do_activate(self) -> None:
        win = self.props.active_window
        if win is None:
            try:
                profiles, errors = load_profiles(include_disabled=self._demo)
                backend = MchoseCliBackend()  # Backend CLI verificado en hardware
                win = MainWindow(self, backend, profiles, errors, self._demo)
            except BackendUnavailable as exc:
                win = ErrorWindow(self, str(exc))
            else:
                win.present()


def run(demo: bool = False) -> int:
    Gtk.Window.set_default_icon_name("input-mouse")
    return HubApp(demo=demo).run([sys.argv[0]])   # no se pasan los argumentos propios a GTK
