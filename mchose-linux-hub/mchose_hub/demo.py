"""
demo.py — Un «módulo hid» simulado.

Sirve para dos cosas:
  * `mchose-hub --demo`: probar la interfaz sin hardware (simula un ratón que sigue la
    plantilla profiles/mchose_mouse_template.json: batería al 87 %).
  * Tests automáticos: se puede forzar que el dispositivo «duerma» o se «desenchufe».

Imita la API de cython-hidapi (`hid.enumerate()`, `hid.device().open_path()`…).
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

DEMO_ENTRY: Dict[str, Any] = {
    "path": b"demo:0001", "vendor_id": 0, "product_id": 0, "serial_number": "DEMO0001",
    "product_string": "MCHOSE A5 (demo)", "manufacturer_string": "MCHOSE",
    "usage_page": 0xFF02, "usage": 1, "interface_number": 1,
}


def _unprofiled_a7() -> List[Dict[str, Any]]:
    """Réplica de la estructura de interfaces de un MCHOSE A7 V2 Pro real conectado por cable
    (VID 3837:4018): puntero, teclado/consumer y una interfaz propia con dos colecciones del fabricante."""
    base = {"vendor_id": 0x3837, "product_id": 0x4018, "serial_number": "",
            "manufacturer_string": "", "product_string": "MCHOSE A7 V2 Pro"}
    rows = [(b"demo:u0", 0, 0x0001, 0x0002), (b"demo:u1", 1, 0x0001, 0x0006), (b"demo:u1", 1, 0x000C, 0x0001),
            (b"demo:u2", 2, 0xFF0B, 0x0104), (b"demo:u2", 2, 0xFF01, 0x0001)]
    return [dict(base, path=p, interface_number=i, usage_page=up, usage=u) for p, i, up, u in rows]


# Descriptor de ejemplo de la interfaz propia: usage page FF01, report ID 9, 63 bytes en Input/Output/Feature.
DEMO_VENDOR_DESCRIPTOR = bytes.fromhex(
    "0601FF 0901 A101 8509 1500 26FF00 7508 953F 0901 8102 0901 9102 0901 B102 C0".replace(" ", ""))


def demo_responder(frame: bytes) -> bytes:
    """Respuesta de la plantilla de ratón: comando 0x04 = batería (87 %, sin cargar)."""
    resp = bytearray(len(frame))
    resp[0] = frame[0]
    if len(frame) > 7 and frame[1] == 0x04:
        resp[1], resp[6], resp[7] = 0x04, 87, 0x00
    else:
        resp[1:] = frame[1:]
    return bytes(resp)


class FakeHandle:
    def __init__(self, hid: "FakeHid"):
        self._hid = hid
        self._last = b""

    def open_path(self, path: bytes) -> None:
        if self._hid.mode == "unplugged":
            raise OSError("open failed")

    def _io(self) -> None:
        if self._hid.mode == "unplugged":
            raise OSError("read error")        # lo que haría hidapi con un nodo hidraw que ya no existe
        if self._hid.io_failures > 0:
            self._hid.io_failures -= 1
            raise OSError("transient I/O error")

    def write(self, data: bytes) -> int:
        self._io()
        self._last = bytes(data)
        self._hid.sent.append(self._last)
        return len(data)

    send_feature_report = write

    def _answer(self) -> List[int]:
        return [] if self._hid.mode == "asleep" else list(self._hid.responder(self._last))

    def get_feature_report(self, report_id: int, length: int) -> List[int]:
        self._io()
        return self._answer()

    def read(self, length: int, timeout_ms: int = 0) -> List[int]:
        self._io()
        data = self._answer()
        if not data:
            time.sleep(min(timeout_ms, 50) / 1000.0)
        return data

    def close(self) -> None:
        pass


class FakeHid:
    """mode: 'ok' | 'asleep' (no responde) | 'unplugged' (sin dispositivo y con errores de E/S)."""

    def __init__(self, entries: Optional[List[Dict[str, Any]]] = None,
                 responder: Optional[Callable[[bytes], bytes]] = None):
        self.entries = entries if entries is not None else [dict(DEMO_ENTRY)] + _unprofiled_a7()
        self.responder = responder or demo_responder
        self.mode = "ok"
        self.io_failures = 0                   # nº de operaciones de E/S que fallarán (simula un reset USB)
        self.sent: List[bytes] = []            # todo lo escrito, para que los tests lo inspeccionen

    def enumerate(self, vid: int = 0, pid: int = 0) -> List[Dict[str, Any]]:
        if self.mode == "unplugged":
            return []
        return [dict(e) for e in self.entries if vid in (0, e["vendor_id"]) and pid in (0, e["product_id"])]

    def report_descriptor(self, path: bytes) -> Optional[bytes]:
        return DEMO_VENDOR_DESCRIPTOR if path == b"demo:u2" else None

    def device(self) -> FakeHandle:
        return FakeHandle(self)
