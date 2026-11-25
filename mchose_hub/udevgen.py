"""
udevgen.py — Genera las reglas udev que permiten usar el hardware SIN ser root.

Las reglas se derivan de los perfiles (cada perfil aporta su VID), de modo que al añadir un
JSON de un dispositivo nuevo basta con volver a generarlas:

    sudo mchose-hub udev --install

Mecanismo: la etiqueta `uaccess` hace que systemd-logind conceda una ACL sobre /dev/hidrawN
al usuario con la sesión local activa (sin grupos ni MODE=0666). Para sistemas sin logind
existe el modo `--group NAME` (GROUP + MODE=0660).

El archivo debe ordenarse ANTES de 73-seat-late.rules, que es quien aplica la etiqueta
uaccess; de ahí el prefijo «70-».
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Any, Dict, Iterable, List, Optional

from .profile import Profile

RULES_PATH = "/etc/udev/rules.d/70-mchose-hub.rules"

# VID «candidatos» mencionados al diseñar el proyecto. NO están verificados: confirma el tuyo
# con `lsusb` o `mchose-hub list --all`. Los modelos MCHOSE usan chips distintos (Nordic, Beken,
# Realtek…), así que es normal que haya varios VID. Quita de aquí los que no necesites.
KNOWN_VIDS: Dict[int, str] = {
    0x320F: "candidato (sin verificar)",
    0x1915: "candidato (sin verificar; es el VID de Nordic Semiconductor, común en dongles 2.4 GHz)",
}


def detect_mchose_vids(entries: Iterable[Dict[str, Any]]) -> Dict[int, str]:
    """VID de los dispositivos conectados ahora cuyo nombre/fabricante contiene «mchose».
    Así las reglas cubren tu hardware real aunque su VID no figure en ninguna lista."""
    found: Dict[int, str] = {}
    for e in entries:
        text = f"{e.get('manufacturer_string') or ''} {e.get('product_string') or ''}"
        if "mchose" in text.lower() and e.get("vendor_id"):
            found[e["vendor_id"]] = f"detectado: {e.get('product_string') or 'MCHOSE'}"
    return found


def existing_vids(path: str = RULES_PATH) -> Dict[int, str]:
    """VID ya presentes en un archivo de reglas generado antes (para acumularlos: el cable y el dongle
    suelen tener VID distintos y no están conectados a la vez)."""
    vids: Dict[int, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"# VID 0x([0-9A-Fa-f]{4}) — (.*)", line)
                if m:
                    vids[int(m.group(1), 16)] = m.group(2).strip()
    except OSError:
        pass
    return vids


def collect_vids(profiles: Iterable[Profile], extra: Iterable[int] = (), include_known: bool = True,
                 detected: Optional[Dict[int, str]] = None,
                 previous: Optional[Dict[int, str]] = None) -> Dict[int, str]:
    """VID → origen. Precedencia (de menor a mayor): reglas previas < candidatos < detectados < --vid < perfiles."""
    vids: Dict[int, str] = dict(previous or {})
    if include_known:
        vids.update(KNOWN_VIDS)
    vids.update(detected or {})
    for vid in extra:
        vids[vid] = "indicado con --vid"
    for p in profiles:
        if p.enabled:
            for vid in p.vendor_ids:
                if vid:
                    vids[vid] = f"perfil '{p.id}'"
    return dict(sorted(vids.items()))


def render_rules(vids: Dict[int, str], group: Optional[str] = None) -> str:
    """Texto completo del archivo .rules."""
    access = f', GROUP="{group}", MODE="0660"' if group else ""
    lines = [
        "# /etc/udev/rules.d/70-mchose-hub.rules",
        "# Generado por «mchose-hub udev». NO lo edites a mano: se regenera al instalar o al añadir perfiles.",
        "# Da acceso a /dev/hidrawN (y al nodo USB, por si se usa libusb) al usuario con sesión local activa.",
        "# Debe ordenarse antes de 73-seat-late.rules (por eso el prefijo 70-).",
        "",
    ]
    for vid, origin in vids.items():
        lo, up = f"{vid:04x}", f"{vid:04X}"
        lines += [
            f"# VID 0x{up} — {origin}",
            f'SUBSYSTEM=="hidraw", ATTRS{{idVendor}}=="{lo}", TAG+="uaccess"{access}',      # USB: cable / dongle 2.4 GHz
            f'SUBSYSTEM=="hidraw", KERNELS=="0005:{up}:*", TAG+="uaccess"{access}',         # Bluetooth (bus 0005)
            f'SUBSYSTEM=="usb", ENV{{DEVTYPE}}=="usb_device", ATTR{{idVendor}}=="{lo}", TAG+="uaccess"{access}',
            "",
        ]
    return "\n".join(lines)


def write_rules(content: str, path: str = RULES_PATH) -> None:
    """Escritura atómica (archivo temporal + rename) con permisos 0644. Requiere root."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def reload_udev() -> List[str]:
    """Recarga las reglas y las reaplica a lo ya conectado. Devuelve avisos (lista vacía = todo bien)."""
    notes: List[str] = []
    steps = (["udevadm", "control", "--reload-rules"],
             ["udevadm", "trigger", "--action=add", "--subsystem-match=hidraw", "--subsystem-match=usb"])
    for cmd in steps:
        try:
            subprocess.run(cmd, check=True, timeout=30, capture_output=True)
        except FileNotFoundError:
            notes.append("udevadm no está instalado: reconecta los dispositivos o reinicia")
            break
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            notes.append(f"'{' '.join(cmd)}' falló ({exc.__class__.__name__}); reconecta los dispositivos")
    return notes
