"""
cli_backend.py — Backend que delega en el binario 'mchose' de alexfrih/mchose-linux.

La GUI GTK4 sigue siendo la misma. Este módulo reemplaza el motor HID propio
(device.py + protocol.py) por llamadas al CLI verificado en hardware real.

Ventajas:
  - El protocolo ya está verificado en hardware idéntico (L7 Pro, 5253:1020)
  - No hay que mantener el motor HID propio para este modelo
  - Cada write se lee de vuelta y se confirma (el CLI lo hace internamente)

El CLI lee el estado con 'mchose show' y escribe con 'mchose dpi', 'mchose rate', etc.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

MCHOSE_BINARY = "mchose"   # debe estar en $PATH tras 'install.sh' del repo de alexfrih
_CLI_LOCK = threading.Lock()  # serializa todas las llamadas al CLI


class MchoseCliError(Exception):
    pass


class MchoseCliNotFound(MchoseCliError):
    pass


WEDGED_MSG = "empty identity"   # mensaje que indica canal largo bloqueado


def _run(*args: str, timeout: int = 10, _retry: bool = True) -> str:
    """Ejecuta el CLI y devuelve stdout. Lanza MchoseCliError si falla.
    Si el canal está bloqueado ('empty identity'), reintenta una vez tras 1 s.
    Todas las llamadas se serializan con _CLI_LOCK para evitar conflictos."""
    with _CLI_LOCK:
        return _run_locked(*args, timeout=timeout, _retry=_retry)


def _run_locked(*args: str, timeout: int = 10, _retry: bool = True) -> str:
    """Implementación interna (ya dentro del lock)."""
    binary = shutil.which(MCHOSE_BINARY)
    if binary is None:
        raise MchoseCliNotFound(
            f"No se encuentra '{MCHOSE_BINARY}' en $PATH.\n"
            f"Instala alexfrih/mchose-linux: cd ~/Downloads/mchose-linux && ./install.sh"
        )
    try:
        r = subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise MchoseCliError(f"mchose {' '.join(args)}: timeout") from exc
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()
        # Canal largo bloqueado: esperar y reintentar una vez
        if _retry and WEDGED_MSG in msg:
            import time
            log.warning("Canal bloqueado, reintentando en 1.5 s…")
            time.sleep(1.5)
            return _run_locked(*args, timeout=timeout + 5, _retry=False)
        raise MchoseCliError(msg)
    return r.stdout


@dataclass
class MouseState:
    """Estado completo del ratón leído de 'mchose show'."""
    profile: int = 0
    active_stage: int = 1          # índice del perfil DPI activo (0-5)
    stages: List[int] = field(default_factory=lambda: [400,800,1600,3200,6400,26000])
    rate_wired: int = 1000         # Hz
    rate_wireless: int = 1000      # Hz
    debounce: int = 0              # ms
    sleep: int = 0                 # minutos (0 = off)
    ripple: bool = False
    angle_snap: bool = False
    motion_sync: bool = False
    lod: int = 1                   # mm
    battery: Optional[int] = None  # %
    charging: bool = False
    firmware: str = ""
    link: str = ""

    @property
    def active_dpi(self) -> int:
        try:
            return self.stages[self.active_stage]
        except IndexError:
            return 0


RATE_VALUES = [125, 500, 1000, 2000, 4000, 8000]


def _parse_rate(text: str) -> int:
    """'8000 Hz (wired), 1000 Hz (wireless)' → 1000 (el inalámbrico activo)."""
    # Buscar wireless primero
    m = re.search(r'(\d+)\s*Hz\s*\(wireless\)', text)
    if m:
        return int(m.group(1))
    m = re.search(r'(\d+)\s*Hz', text)
    return int(m.group(1)) if m else 1000


def _parse_rate_wired(text: str) -> int:
    m = re.search(r'(\d+)\s*Hz\s*\(wired\)', text)
    return int(m.group(1)) if m else 1000


def parse_show(output: str) -> MouseState:
    """Parsea la salida de 'mchose show' en un MouseState."""
    s = MouseState()
    stages: Dict[int, int] = {}
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("profile"):
            m = re.search(r'(\d+)', line)
            if m: s.profile = int(m.group(1))
        elif re.match(r'\*?\s*\d+\s+\d+ dpi', line):
            active = line.startswith('*')
            m = re.match(r'\*?\s*(\d+)\s+(\d+)', line)
            if m:
                idx, dpi = int(m.group(1)), int(m.group(2))
                stages[idx] = dpi
                if active:
                    s.active_stage = idx
        elif line.startswith("rate"):
            rest = line[4:].strip()
            s.rate_wireless = _parse_rate(rest)
            s.rate_wired    = _parse_rate_wired(rest)
        elif line.startswith("debounce"):
            m = re.search(r'(\d+)', line)
            if m: s.debounce = int(m.group(1))
        elif line.startswith("sleep"):
            m = re.search(r'(\d+)', line)
            s.sleep = int(m.group(1)) if m else 0
        elif line.startswith("ripple"):
            s.ripple = "on" in line
        elif line.startswith("angle snap"):
            s.angle_snap = "on" in line
        elif line.startswith("motion sync"):
            s.motion_sync = "on" in line
        elif line.startswith("lod"):
            m = re.search(r'(\d+)', line)
            if m: s.lod = int(m.group(1))
    if stages:
        s.stages = [stages.get(i, 0) for i in range(max(stages) + 1)]
    return s


def parse_info(output: str) -> Tuple[Optional[int], bool, str, str]:
    """Parsea 'mchose info' → (battery%, charging, firmware, link)."""
    battery, charging, firmware, link = None, False, "", ""
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("battery"):
            m = re.search(r'(\d+)\s*%', line)
            if m: battery = int(m.group(1))
            charging = "charg" in line.lower()
        elif line.startswith("firmware"):
            firmware = line.split(None, 1)[1].strip() if " " in line else ""
        elif line.startswith("link"):
            link = line.split(None, 1)[1].strip() if " " in line else ""
    return battery, charging, firmware, link


class MchoseCliBackend:
    """Backend completo: lee estado y escribe settings via el CLI de alexfrih."""

    def is_available(self) -> bool:
        return shutil.which(MCHOSE_BINARY) is not None

    def get_state(self) -> MouseState:
        show = _run("show")
        s = parse_show(show)
        try:
            info = _run("info")
            battery, charging, firmware, link = parse_info(info)
            s.battery  = battery
            s.charging = charging
            s.firmware = firmware
            s.link     = link
        except MchoseCliError:
            pass
        return s

    def set_dpi_stage(self, stage_index: int, dpi: int) -> None:
        """Cambia el DPI de un perfil específico (0-5)."""
        # Leer todos los stages actuales, cambiar el que corresponde
        state = parse_show(_run("show", timeout=15))
        stages = list(state.stages)
        while len(stages) < 6:
            stages.append(stages[-1] if stages else 800)
        stages[stage_index] = dpi
        _run("dpi", "--list", ",".join(str(d) for d in stages), timeout=15)

    def set_active_stage(self, stage_index: int) -> None:
        _run("stage", str(stage_index), timeout=15)

    def set_rate(self, hz: int) -> None:
        _run("rate", str(hz), timeout=15)

    def set_lod(self, mm: int) -> None:
        _run("lod", str(mm), timeout=15)

    def set_debounce(self, ms: int) -> None:
        _run("debounce", str(ms), timeout=15)

    def set_sleep(self, minutes: int) -> None:
        _run("sleep", str(minutes), timeout=15)

    def set_motion_sync(self, on: bool) -> None:
        _run("motion-sync", "on" if on else "off", timeout=15)

    def set_ripple(self, on: bool) -> None:
        _run("ripple", "on" if on else "off", timeout=15)  # puede que no exista en el CLI

    def set_angle_snap(self, on: bool) -> None:
        _run("angle-snap", "on" if on else "off", timeout=15)
