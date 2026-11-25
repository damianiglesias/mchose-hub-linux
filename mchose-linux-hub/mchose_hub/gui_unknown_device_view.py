"""UnknownDeviceView simplificado para backend CLI."""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Any, Callable, List, Tuple

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib

log = logging.getLogger(__name__)


class UnknownDeviceView(Adw.PreferencesPage):
    """Versión simplificada para backend CLI: solo muestra info básica y acciones."""

    def __init__(self, dev_info: dict, backend: Any, toast: Callable[[str], None],
                 on_profile_saved: Callable[[], None]) -> None:
        super().__init__()
        self._dev = dev_info or {}
        self._backend = backend
        self._worker = Worker()
        self._dynamic: List[Tuple[Adw.PreferencesGroup, Any]] = []

        # Información básica del dispositivo
        vendor_id = self._dev.get("vendor_id", 0)
        product_id = self._dev.get("product_id", 0)
        device_name = self._dev.get("device_name", f"VID:{vendor_id:04X} PID:{product_id:04X}")

        head = Adw.PreferencesGroup(title=device_name, description="Dispositivo detectado")
        head.add(Adw.ActionRow(title="Estado", subtitle="Listo para configurar"))
        self.add(head)

        actions = Adw.PreferencesGroup(title="Acciones")
        self.add(actions)

        create_btn = Gtk.Button(label="Crear perfil base", valign=Gtk.Align.CENTER)
        create_btn.add_css_class("suggested-action")
        create_btn.connect("clicked", self._create_profile)
        actions.add(create_btn)

    def _create_profile(self, btn: Gtk.Button) -> None:
        try:
            path = save_base_profile_simple(self._dev, self._backend)
            if path:
                self._toast(f"Perfil base guardado en {path}")
                GLib.idle_add(self._on_saved)
            else:
                self._toast("No se pudo crear el perfil")
        except Exception as e:
            log.exception("Error al crear perfil")
            self._toast(f"Error: {e}")

    def _add(self, group: Adw.PreferencesGroup, row: Any) -> None:
        group.add(row)
        self._dynamic.append((group, row))

    def shutdown(self) -> None:
        self._worker.shutdown()


def save_base_profile_simple(dev_info: dict, backend: Any) -> str | None:
    """Crea un perfil base simplificado con las info del dispositivo."""
    from mchose_hub.config import PROFILES_DIR
    from mchose_hub.protocol import VENDOR_PAGE
    
    vendor_id = dev_info.get("vendor_id", 0)
    product_id = dev_info.get("product_id", 0)
    
    profile_path = PROFILES_DIR / f"{vendor_id:04X}_{product_id:04X}.json"
    
    # Perfil básico con el tipo de transporte detectado (si es posible)
    transport_type = dev_info.get("transport_type", "hid")  # hid, hidpp20, usbif
    
    profile = {
        "vendor_id": hex(vendor_id),
        "product_id": hex(product_id),
        "features": [],  # Se añadirán con backend.execute()
        "transport": transport_type,
        "name": f"Base VID:{vendor_id:04X} PID:{product_id:04X}",
    }
    
    try:
        import json
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        with open(profile_path, "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=2)
        log.info(f"Perfil base creado: {profile_path}")
        return str(profile_path)
    except Exception as e:
        log.exception("No se pudo guardar perfil base")
        return None
