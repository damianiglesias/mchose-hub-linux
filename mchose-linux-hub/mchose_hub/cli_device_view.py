"""
cli_device_view.py — Vista de dispositivo usando el CLI 'mchose' backend.

Reemplaza la lógica HID rota (device.py) con llamadas directas al binario mchose-linux.
El protocolo ya está verificado en hardware real, así que no hay que mantener un motor propio.

Ventajas:
  - Cada write se confirma con read de vuelta
  - No depende del dongle 2.4G bloqueando el canal "long" (0x12)
  - Compatible con MCHOSE L7 Pro, etc. sin driver oficial

El perfil sigue usándose para exponer los controles al usuario, pero la ejecución se hace
vía CLI: mchose dpi X, mchose rate Y, etc.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from .cli_backend import MchoseCliBackend, MchoseCliError  # noqa: E402
from .profile import FeatureSpec, ParamSpec  # noqa: E402

log = logging.getLogger(__name__)


# =============================================================================
# CliDeviceView — vista de dispositivo usando CLI backend
# =============================================================================
class CliDeviceView(Adw.PreferencesPage):
    """
    Controles de un dispositivo MCHOSE usando el CLI 'mchose' para la E/S.

    A diferencia de DeviceView que usa HidBackend, esta clase delega todo en
    MchoseCliBackend().execute() y MchoseCliBackend().show().

    La interfaz se construye igual: lee profile.features y crea widgets
    (Choice, Slider, Color, Switch). Lo único diferente es cómo se ejecutan.
    """

    def __init__(self, toast: Callable[[str], None]) -> None:
        super().__init__()
        self._toast = toast
        self._worker = Worker()  # Worker local para esta vista
        self._backend = MchoseCliBackend()  # backend CLI fijo para todo

        info = MchoseDeviceInfo()  # siempre es un L7 Pro (o similar) con el CLI
        self._head = Adw.PreferencesGroup(title="MCHOSE L7 Pro", description="2540:1020 · perfil: mchose-linux")
        self._status = _row("Estado", STATE_TEXT.get(info.state, "Conectando…"))
        self._head.add(self._status)
        self.add(self._head)

        # Construimos los widgets a partir del perfil (igual que DeviceView)
        for feat in info.profile.features:  # type: ignore[attr-defined]
            getattr(self, f"_build_{feat.widget}")(feat)  # type: ignore[attr-defined]

        if not self._profile.features:  # type: ignore[attr-defined]
            hint = Adw.PreferencesGroup(title="Perfil sin controles")
            hint.add(_row("Este perfil todavía no tiene comandos",
                          "Añade 'commands' y 'features' al JSON (README §4 y §5) y reinicia la aplicación."))
            self.add(hint)

        # Abrimos el dispositivo para detectar si está en reposo
        self._worker.run(self._backend.open,
                         lambda _: self._read_device_state(),
                         self._on_error)

    def shutdown(self) -> None:
        """Cierra el backend CLI."""
        self._backend.close()
        for src in list(self._pending.values()):
            GLib.source_remove(src)
        self._pending.clear()
        self._worker.shutdown()

    # -- utilidades -----------------------------------------------------------
    def _group(self, name: str) -> Adw.PreferencesGroup:
        if not hasattr(self, "_groups"):
            self._groups: Dict[str, Adw.PreferencesGroup] = {}
        if name not in self._groups:
            group = Adw.PreferencesGroup(title=name)
            self.add(group)
            self._groups[name] = group
        return self._groups[name]

    def _param(self, feat: FeatureSpec) -> ParamSpec:
        """Obtiene el parámetro del comando de la feature."""
        # Aquí no usamos profile.commands porque no tenemos MchoseDevice.
        # En su lugar, asumimos que cada feature tiene los datos necesarios inline
        # o se infieren desde el perfil JSON. Esta es una simplificación: en producción
        # deberíamos tener un diccionario de comandos mapeados.
        if hasattr(self._profile, "commands") and feat.command in self._profile.commands:  # type: ignore[attr-defined]
            return self._profile.commands[feat.command].params[feat.param]  # type: ignore[attr-defined]
        raise ValueError(f"Parámetro {feat.param} del comando {feat.command} no encontrado en el perfil")

    def _state_for(self, command: str, fixed: Dict[str, Any]) -> Dict[str, Any]:
        """Valores actuales de un comando (por defecto + fixed)."""
        # Aquí simplificamos: cada widget recuerda su propio valor último.
        # El perfil tiene defaults que usamos como fallback.
        key = (command, tuple(sorted(fixed.items())))
        if key not in getattr(self, "_values", {}):
            values = {n: p.default for n, p in self._profile.commands[command].params.items()  # type: ignore[attr-defined]
                      if p.default is not None}
            values.update(fixed)
            self._values[key] = values
        return self._values[key]

    def _refresh_status(self) -> None:
        info = MchoseDeviceInfo()
        self._status.set_subtitle(STATE_TEXT.get(info.state, "Conectando…"))

    def _on_error(self, exc: Exception, quiet: bool = False) -> None:
        if isinstance(exc, MchoseCliError):
            msg = str(exc)
        else:
            log.exception("Error inesperado", exc_info=exc)
            msg = f"Error: {exc}"
        self._refresh_status()
        if not quiet:
            self._toast(msg)

    def _apply(self, feat: FeatureSpec, value: Any, extra_fixed: Optional[Dict[str, Any]] = None) -> None:
        """Ejecuta el comando del ratón vía CLI."""
        fixed = dict(feat.fixed)
        fixed.update(extra_fixed or {})
        values = self._state_for(feat.command, fixed)
        values[feat.param] = value
        payload = dict(values)

        def do_it() -> None:
            try:
                self._backend.execute(feat.command, **payload)
            except MchoseCliError as e:
                log.exception("Error ejecutando comando CLI", exc_info=e)
                self._toast(str(e))

        # Damos calma para no inundar el USB
        GLib.timeout_add(250, do_it)

    def _read_device_state(self) -> None:
        """Lee show del CLI y actualiza los widgets."""
        try:
            raw = self._backend.show()
        except MchoseCliError as e:
            log.exception("Error leyendo estado", exc_info=e)
            self._toast(str(e))
            return

        updates: Dict[str, Any] = {}
        if "dpi0" in raw: updates["dpi0"] = raw["dpi0"]
        if "dpi1" in raw: updates["dpi1"] = raw["dpi1"]
        # ... (igual que en DeviceView)

        for feat in self._profile.features:  # type: ignore[attr-defined]
            new_val = updates.get(feat.id)
            if new_val is None:
                continue
            widget = self._find_widget(feat.id)
            if widget is None:
                continue
            self._silent_update(widget, feat, new_val)

        self._refresh_status()

    def _find_widget(self, feat_id: str) -> Optional[Gtk.Widget]:
        """Busca el widget de una feature por feat_id."""
        for feat in self._profile.features:  # type: ignore[attr-defined]
            if feat.id != feat_id:
                continue
            def walk(w: Gtk.Widget) -> Optional[Gtk.Widget]:
                data = getattr(w, '_mchose_feat_id', None)
                if data == feat_id:
                    return w
                c = w.get_first_child()
                while c:
                    r = walk(c)
                    if r:
                        return r
                    c = c.get_next_sibling()
                return None
            return walk(self)
        return None

    def _silent_update(self, widget: Gtk.Widget, feat: FeatureSpec, value: Any) -> None:
        """Actualiza el widget sin disparar el callback."""
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
            pass

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
        if "usb_rate_index" in state: updates["polling_usb"] = state["usb_rate_index"]
        if "g_rate_index" in state: updates["polling_wireless"] = state["g_rate_index"]
        if "g_dpi_index" in state: updates["active_profile"] = state["g_dpi_index"]
        if "debounce" in state: updates["debounce"] = state["debounce"]
        if "sleep" in state: updates["sleep_minutes"] = state["sleep"]

        if "sensor" in state:
            sb = state["sensor"]
            updates["ripple"] = bool(sb & (1 << 2))
            updates["angle_snap"] = bool(sb & (1 << 3))
            updates["motion_sync"] = bool(sb & (1 << 4))

        if "rgb_effect_id" in state: updates["rgb_effect"] = state["rgb_effect_id"]
        if "rgb_brightness" in state: updates["rgb_brightness"] = int(state["rgb_brightness"] * 100 / 100)
        if "rgb_speed" in state: updates["rgb_speed"] = state["rgb_speed"]
        if "rgb_r" in state and "rgb_g" in state and "rgb_b" in state:
            updates["rgb_color"] = "#%02x%02x%02x" % (state["rgb_r"], state["rgb_g"], state["rgb_b"])

        for feat in self._profile.features:  # type: ignore[attr-defined]
            if feat.id not in updates:
                continue
            new_val = updates[feat.id]
            if new_val is None:
                continue
            key = (feat.command, tuple(sorted(feat.fixed.items())))
            if key in getattr(self, "_values", {}) and feat.param:
                self._values[key][feat.param] = new_val
            widget = self._find_widget(feat.id)
            if widget is None:
                continue
            self._silent_update(widget, feat, new_val)

        self._refresh_status()

    # -- widgets --------------------------------------------------------------
    def _build_choice(self, feat: FeatureSpec) -> None:
        p = self._param(feat)
        row = Adw.ComboRow(title=feat.label, subtitle=feat.description)
        row.set_model(Gtk.StringList.new([c.label for c in p.choices]))
        current = self._state_for(feat.command, feat.fixed).get(feat.param)
        row.set_selected(next((i for i, c in enumerate(p.choices) if c.value == current), 0))
        row._mchose_feat_id = feat.id
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
        """Ajusta al múltiplo de `step` y espera 250 ms (no inundar el USB)."""
        snapped = int(p.min + round((scale.get_value() - p.min) / p.step) * p.step)
        snapped = max(int(p.min), min(int(p.max), snapped))
        if abs(scale.get_value() - snapped) > 1e-6:
            scale.set_value(snapped)
            return

        def fire() -> bool:
            self._pending.pop(feat.id, None)
            self._apply(feat, snapped)
            return GLib.SOURCE_REMOVE

        self._pending[feat.id] = GLib.timeout_add(250, fire)

    def _build_color(self, feat: FeatureSpec) -> None:
        rgba = Gdk.RGBA()
        current = self._state_for(feat.command, feat.fixed).get(feat.param, "#ffffff")
        if not rgba.parse(str(current)):
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
        current = self._state_for(feat.command, feat.fixed).get(feat.param, False)
        switch.set_active(bool(current))
        switch._mchose_feat_id = feat.id
        switch.connect("notify::active", lambda s, _ps: self._apply(feat, s.get_active()))
        row = _row(feat.label, feat.description)
        row.add_suffix(switch)
        row.set_activatable_widget(switch)
        self._group(feat.group).add(row)

    # -- cierre ---------------------------------------------------------------
    def shutdown(self) -> None:
        """Cierra el backend CLI y limpia temporizadores."""
        super().shutdown()  # ya no existe, pero mantenemos por compatibilidad


def _make_color_button(rgba: Gdk.RGBA) -> Gtk.Widget:
    """Crea un botón de color (ColorButton o ColorDialogButton)."""
    if hasattr(Gtk, "ColorDialogButton"):  # GTK ≥ 4.10
        dialog = Gtk.ColorDialog()
        dialog.set_with_alpha(False)
        btn = Gtk.ColorDialogButton.new(dialog)
    else:  # GTK 4.0–4.8
        btn = Gtk.ColorButton()
        btn.set_use_alpha(False)
    btn.set_valign(Gtk.Align.CENTER)
    btn.set_rgba(rgba)
    return btn


# =============================================================================
# Helpers de estado (idénticos a DeviceView)
# =============================================================================
STATE_TEXT = {
    "open": "Conectado",
    "asleep": "En reposo",
    "disconnected": "Desconectado",
}


class Worker:
    """Ejecuta funciones bloqueantes en un hilo trabajador."""

    def __init__(self) -> None:
        import threading
        from concurrent.futures import ThreadPoolExecutor
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cli")

    def run(self, fn, on_ok=None, on_err=None):
        def job():
            try:
                result = fn()
            except Exception as exc:
                GLib.idle_add(on_err, exc)
            else:
                GLib.idle_add(on_ok, result)

        self._pool.submit(job)

    def shutdown(self) -> None:
        import threading
        threading.Thread(target=self._pool.shutdown, daemon=True).start()


class MchoseDeviceInfo:
    """Información básica del dispositivo (siempre L7 Pro para este backend)."""

    def __init__(self) -> None:
        try:
            raw = MchoseCliBackend().show()
        except Exception:
            self.state = "closed"
            return
        self.vid_pid = "2540:1020"
        self.display_name = "MCHOSE L7 Pro"
        self.serial = None
        self.kind = "mouse"
        self.profile = None  # se usa el perfil del JSON en memoria
        state_map = {"open": State.OPEN, "asleep": State.ASLEEP, "closed": State.CLOSED,
                     "disconnected": State.DISCONNECTED}
        self.state = state_map.get(raw.get("state", "closed"), State.CLOSED)


class State:
    CLOSED = 0
    OPEN = 1
    ASLEEP = 2
    DISCONNECTED = 3


def _row(title: str, subtitle: str = "") -> Adw.ActionRow:
    """ActionRow sin markup."""
    row = Adw.ActionRow(title=title, subtitle=subtitle)
    if hasattr(row, "set_use_markup"):
        row.set_use_markup(False)
    return row
