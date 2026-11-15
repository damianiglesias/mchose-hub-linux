"""
hidinfo.py — Inspección de dispositivos HID desconocidos y creación de perfiles base.

Sin protocolo no hay comandos, pero el *report descriptor* que cada dispositivo publica
dice qué reports existen en sus interfaces propias (usage page ≥ 0xFF00): ID, tipo
(Input/Output/Feature) y tamaño. Con eso se deduce el `transport` del perfil sin sniffing
(los comandos concretos sí requieren captura o ingeniería inversa del Web Driver).

El descriptor se lee de sysfs (/sys/class/hidraw/hidrawN/device/report_descriptor), que es
legible SIN permisos especiales, a diferencia de abrir /dev/hidrawN.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .profile import user_profile_dir

VENDOR_PAGE = 0xFF00


@dataclass
class ReportInfo:
    usage_page: int      # de la colección de nivel superior que contiene el report
    usage: int
    kind: str            # "Input" | "Output" | "Feature"
    report_id: int       # 0 = sin ID
    bits: int

    @property
    def size(self) -> int:
        return (self.bits + 7) // 8

    @property
    def frame_len(self) -> int:
        """Longitud TOTAL en la convención del perfil (Report ID incluido, también si es 0)."""
        return self.size + 1


def parse_report_descriptor(data: bytes) -> List[ReportInfo]:
    """Resumen de reports de un descriptor HID (items cortos; los largos se saltan)."""
    g = {"page": 0, "size": 0, "count": 0, "rid": 0}      # estado «global»
    stack: List[Dict[str, int]] = []
    local_usage: Optional[int] = None
    local_page: Optional[int] = None
    depth, app = 0, (0, 0)
    totals: Dict[Tuple[int, int, str, int], int] = {}
    i, n = 0, len(data)
    while i < n:
        prefix = data[i]
        i += 1
        if prefix == 0xFE:                                  # item largo: 0xFE, tamaño, tag, datos
            i += 2 + (data[i] if i < n else 0)
            continue
        size = (0, 1, 2, 4)[prefix & 3]
        kind, tag = (prefix >> 2) & 3, prefix >> 4
        val = int.from_bytes(data[i:i + size], "little") if size else 0
        i += size
        if kind == 1:                                       # global
            if tag == 0x0:
                g["page"] = val
            elif tag == 0x7:
                g["size"] = val
            elif tag == 0x8:
                g["rid"] = val
            elif tag == 0x9:
                g["count"] = val
            elif tag == 0xA:
                stack.append(dict(g))
            elif tag == 0xB and stack:
                g = stack.pop()
        elif kind == 2:                                     # local
            if tag == 0x0 and local_usage is None:
                local_usage = val & 0xFFFF
                local_page = (val >> 16) if size == 4 else None
        elif kind == 0:                                     # main
            if tag == 0xA:                                  # Collection
                if depth == 0:
                    app = (local_page if local_page is not None else g["page"], local_usage or 0)
                depth += 1
            elif tag == 0xC:                                # End Collection
                depth = max(0, depth - 1)
            elif tag in (0x8, 0x9, 0xB):
                name = {0x8: "Input", 0x9: "Output", 0xB: "Feature"}[tag]
                key = (app[0], app[1], name, g["rid"])
                totals[key] = totals.get(key, 0) + g["size"] * g["count"]
            local_usage = local_page = None                 # los items locales caducan tras cada main
    return [ReportInfo(*key, bits) for key, bits in totals.items()]


def format_report(r: ReportInfo) -> str:
    rid = f"ID 0x{r.report_id:02X}" if r.report_id else "sin ID"
    return f"{r.kind:<7} {rid:<8} {r.size} B (frame de {r.frame_len} B)  [{r.usage_page:04X}/{r.usage:04X}]"


def node_str(path: Any) -> str:
    return path.decode(errors="replace") if isinstance(path, bytes) else str(path)


def is_vendor_node(entries: List[Dict[str, Any]]) -> bool:
    return any(e.get("usage_page", 0) >= VENDOR_PAGE for e in entries)


def vendor_node_paths(dev: Any) -> List[bytes]:
    return [p for p, ents in dev.nodes().items() if is_vendor_node(ents)]


def device_report(dev: Any, backend: Any) -> str:
    """Informe en texto plano (para la terminal o para pegarlo en un issue/chat)."""
    lines = [f"{dev.display_name} — {dev.vid_pid}" + (f" (serie {dev.serial})" if dev.serial else "")]
    for path, entries in dev.nodes().items():
        usages = " ".join(f"{e.get('usage_page', 0):04X}/{e.get('usage', 0):04X}" for e in entries)
        lines.append(f"  {node_str(path)}  IF{entries[0].get('interface_number', -1)}  {usages}")
        if is_vendor_node(entries):
            desc = backend.report_descriptor(path)
            if desc is None:
                lines.append("    (descriptor no disponible)")
                continue
            reports = [r for r in parse_report_descriptor(desc) if r.usage_page >= VENDOR_PAGE]
            lines += [f"    {format_report(r)}" for r in reports] or ["    (sin reports propios del fabricante)"]
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Perfil base
# ----------------------------------------------------------------------------
def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "dispositivo"


def make_base_profile(dev: Any, backend: Any) -> Dict[str, Any]:
    """Esqueleto de perfil: detecta el dispositivo y deduce el transporte del descriptor.

    NO contiene comandos: esos hay que capturarlos (README §5)."""
    nodes = dev.nodes()
    vendor_paths = vendor_node_paths(dev)
    interface = None
    reports: List[ReportInfo] = []
    if vendor_paths:
        interface = nodes[vendor_paths[0]][0].get("interface_number", None)
        desc = backend.report_descriptor(vendor_paths[0])
        if desc:
            reports = [r for r in parse_report_descriptor(desc) if r.usage_page >= VENDOR_PAGE]

    first = lambda kind: next((r for r in reports if r.kind == kind), None)  # noqa: E731
    out, feat, inp = first("Output"), first("Feature"), first("Input")
    wr = out or feat
    transport: Dict[str, Any] = {
        "write_mode": "output_report" if out else "feature_report",
        "read_mode": "input_report" if inp else ("feature_report" if feat else "none"),
        "report_id": f"0x{(wr.report_id if wr else 0):02X}",
        "report_length": wr.frame_len if wr else 64,
        "response_timeout_ms": 500, "retries": 2, "wake_delay_ms": 300, "inter_command_delay_ms": 20,
    }
    all_usages = {(e.get("usage_page", 0), e.get("usage", 0)) for ents in nodes.values() for e in ents}
    kind = "mouse" if (1, 2) in all_usages else "keyboard" if (1, 6) in all_usages else "other"
    return {
        "_comment": [
            "Perfil BASE generado por MCHOSE Linux Hub. Sólo identifica el dispositivo y propone un transporte",
            "deducido de su report descriptor: confírmalo con una captura. NO tiene comandos ni controles todavía.",
            "Reports propios del fabricante en la interfaz elegida:",
            *[f"  {format_report(r)}" for r in reports],
            "Limitación: 'transport.report_id' es único para escribir y leer; si el descriptor usa IDs distintos, ajústalo.",
            "Siguiente paso: README §4 y §5 (añadir 'commands' y 'features').",
        ],
        "schema_version": 1,
        "device": {"id": _slug(dev.display_name), "name": dev.display_name, "kind": kind,
                   "enabled": True, "verified": False, "author": "", "notes": "Perfil base autogenerado."},
        "match": {"ids": [f"{dev.vendor_id:04X}:{dev.product_id:04X}"], "usage_page": None,
                  "usage": None, "interface_number": interface},
        "transport": transport,
        "commands": {},
        "features": [],
    }


def save_base_profile(dev: Any, backend: Any, directory: Optional[Path] = None) -> Path:
    """Guarda el perfil base en el directorio de perfiles del usuario. Si ya existe (p. ej. el cable
    ya tiene perfil y ahora conectas el dongle) sólo añade el nuevo VID:PID."""
    directory = Path(directory) if directory else user_profile_dir()
    directory.mkdir(parents=True, exist_ok=True)
    data = make_base_profile(dev, backend)
    path = directory / f"{data['device']['id']}.json"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        ids = existing.setdefault("match", {}).setdefault("ids", [])
        pair = data["match"]["ids"][0]
        if pair.upper() not in (str(x).upper() for x in ids):
            ids.append(pair)
        data = existing
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
