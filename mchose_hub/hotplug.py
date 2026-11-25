"""
hotplug.py — Aviso en tiempo real cuando se conecta o desconecta un dispositivo HID.

Estrategia (la más rápida disponible, sin exigir dependencias extra):
  1. Si hay `pyudev`: se escuchan eventos `add/remove` del subsistema `hidraw` (instantáneo).
  2. Si no: se sondea `enumerate()` cada pocos segundos y se compara el conjunto de rutas.

El monitor sólo AVISA («algo cambió»); quien lo usa vuelve a ejecutar discover().
El callback se invoca desde un hilo secundario: la GUI lo reenvía al hilo principal con GLib.idle_add.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, FrozenSet, List, Optional

log = logging.getLogger(__name__)


class HotplugMonitor:
    def __init__(self, on_change: Callable[[], None],
                 enumerate_fn: Optional[Callable[[], List[dict]]] = None,
                 poll_interval: float = 2.0, force_polling: bool = False):
        self._on_change = on_change
        self._enumerate = enumerate_fn
        self._interval = poll_interval
        self._force_polling = force_polling
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.mode = "idle"        # «udev» o «polling» una vez arrancado (útil para depurar)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="hotplug", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- implementación ------------------------------------------------------
    def _run(self) -> None:
        if not self._force_polling and self._run_udev():
            return
        self._run_polling()

    def _notify(self) -> None:
        try:
            self._on_change()
        except Exception:                       # un fallo del callback no debe matar el monitor
            log.exception("Error en el callback de hot-plug")

    def _run_udev(self) -> bool:
        """Devuelve False si pyudev no está disponible (→ se usa el sondeo)."""
        try:
            import pyudev  # type: ignore
            monitor = pyudev.Monitor.from_netlink(pyudev.Context())
            monitor.filter_by(subsystem="hidraw")
            monitor.start()
        except Exception as exc:
            log.info("pyudev no disponible (%s); uso sondeo", exc)
            return False
        self.mode = "udev"
        log.debug("Hot-plug por eventos udev")
        while not self._stop.is_set():
            dev = monitor.poll(timeout=1.0)     # None si vence el timeout (permite comprobar _stop)
            if dev is None or dev.action not in ("add", "remove"):
                continue
            # Un dispositivo expone varias interfaces: espera un instante y descarta el resto de eventos
            self._stop.wait(0.4)
            while monitor.poll(timeout=0) is not None:
                pass
            self._notify()
        return True

    def _run_polling(self) -> None:
        self.mode = "polling"
        log.debug("Hot-plug por sondeo cada %.1f s", self._interval)
        previous = self._snapshot()
        while not self._stop.wait(self._interval):
            current = self._snapshot()
            if current != previous:
                previous = current
                self._notify()

    def _snapshot(self) -> FrozenSet[bytes]:
        try:
            return frozenset(e["path"] for e in (self._enumerate() if self._enumerate else []))
        except Exception as exc:
            log.debug("enumerate() falló: %s", exc)
            return frozenset()
