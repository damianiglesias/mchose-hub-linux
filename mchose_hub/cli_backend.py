"""
cli_backend.py — Backend que delega en el binario 'mchose' de alexfrih/mchose-linux.

La GUI GTK4 sigue siendo la misma. Este módulo reemplaza el motor HID propio
(device.py + protocol.py) por llamadas al CLI verificado en hardware real.

Comandos que usa internamente:
  mchose show          → estado completo (DPI, polling, sensor, LOD, etc.)
  mchose info          → batería, firmware, link
  mchose dpi 1600      → cambia DPI del perfil activo
  mchose dpi --list 400,800,1600,3200,6400,26000  → escribe los 6 perfiles
  mchose stage 1       → cambia el perfil activo (0-5)
  mchose rate 1000     → cambia la frecuencia de sondeo inalámbrica
  mchose lod 1         → distancia de despegue (1 ó 2 mm)
  mchose debounce 3    → tiempo de rebote (ms)
  mchose sleep 5       → minutos de reposo (0 = off)
  mchose motion-sync on/off
  mchose angle-snap on/off
  mchose ripple on/off  (puede no existir en todos los firmwares)
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

MCHOSE_BINARY = "mchose"   # ~/.local/bin/mchose debe estar en $PATH
_CLI_LOCK = threading.Lock()  # serializa TODAS las llamadas al CLI

WEDGED_MSG = "empty identity"   # mensaje que indica canal largo bloqueado


# ---------------------------------------------------------------------------
# Excepciones propias
# ---------------------------------------------------------------------------
class MchoseCliError(Exception):
    pass


class MchoseCliNotFound(MchoseCliError):
    pass


# ---------------------------------------------------------------------------
# Ejecución del CLI
# ---------------------------------------------------------------------------
def _run(*args: str, timeout: int = 10, _retry: bool = True) -> str:
    """Ejecuta el CLI y devuelve stdout.
    Serializa con _CLI_LOCK para evitar conflictos entre hilos.
    Reintenta una vez si el canal está bloqueado ('empty identity').
    """
    with _CLI_LOCK:
        return _run_locked(*args, timeout=timeout, _retry=_retry)


def _run_locked(*args: str, timeout: int = 10, _retry: bool = True) -> str:
    """Implementación interna (ya dentro del lock)."""
    binary = shutil.which(MCHOSE_BINARY)
    if binary is None:
        raise MchoseCliNotFound(
            f"No se encuentra '{MCHOSE_BINARY}' en $PATH.\n"
            f"Instala alexfrih/mchose-linux y asegúrate de que "
            f"~/.local/bin está en $PATH."
        )
    try:
        r = subprocess.run(
            [binary, *args],
            capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise MchoseCliError(f"mchose {' '.join(args)}: timeout") from exc

    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()
        if _retry and WEDGED_MSG in msg:
            log.warning("Canal bloqueado ('empty identity'), reintentando en 1.5 s…")
            time.sleep(1.5)
            return _run_locked(*args, timeout=timeout + 5, _retry=False)
        raise MchoseCliError(msg or f"mchose {' '.join(args)}: código {r.returncode}")
    return r.stdout


# ---------------------------------------------------------------------------
# Modelo de estado
# ---------------------------------------------------------------------------
@dataclass
class MouseState:
    """Estado completo del ratón leído de 'mchose show' + 'mchose info'."""
    active_stage: int = 0                   # índice del perfil DPI activo (0-5)
    stages: List[int] = field(default_factory=lambda: [400, 800, 1600, 3200, 6400, 26000])
    rate_wired: int = 1000                  # Hz
    rate_wireless: int = 1000              # Hz
    debounce: int = 0                       # ms
    sleep: int = 0                          # minutos (0 = off)
    ripple: bool = False
    angle_snap: bool = False
    motion_sync: bool = False
    lod: int = 1                            # mm
    battery: Optional[int] = None          # %
    charging: bool = False
    firmware: str = ""
    link: str = ""                          # "wireless" / "wired" / ""

    @property
    def active_dpi(self) -> int:
        try:
            return self.stages[self.active_stage]
        except IndexError:
            return 0


RATE_VALUES = [125, 500, 1000, 2000, 4000, 8000]


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------
def _parse_rate_wireless(text: str) -> int:
    m = re.search(r'(\d+)\s*Hz\s*\(wireless\)', text)
    if m:
        return int(m.group(1))
    m = re.search(r'(\d+)\s*Hz', text)
    return int(m.group(1)) if m else 1000


def _parse_rate_wired(text: str) -> int:
    m = re.search(r'(\d+)\s*Hz\s*\(wired\)', text)
    return int(m.group(1)) if m else 1000


def _parse_on_off(text: str) -> bool:
    """Parsea on/off sin confundir la palabra ``off`` con ``on``."""
    value = text.lower().replace("_", " ")
    if re.search(r"\b(?:off|disabled|false)\b", value):
        return False
    return bool(re.search(r"\b(?:on|enabled|true)\b", value))


def parse_show(output: str) -> MouseState:
    """Parsea la salida de 'mchose show' en un MouseState."""
    s = MouseState()
    stages: Dict[int, int] = {}

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue

        # Línea de perfil DPI:  "* 2  1600 dpi"  o  "  1   400 dpi"
        m = re.search(r'(?P<active>\*)?\s*(?P<idx>\d+)\s+(?P<dpi>\d+)\s*dpi\b', line, re.I)
        if m:
            active = bool(m.group("active"))
            idx = int(m.group("idx"))
            dpi = int(m.group("dpi"))
            stages[idx] = dpi
            if active:
                s.active_stage = idx
            continue

        if line.startswith("rate"):
            rest = line[4:].strip()
            s.rate_wireless = _parse_rate_wireless(rest)
            s.rate_wired = _parse_rate_wired(rest)
        elif line.startswith("debounce"):
            m2 = re.search(r'(\d+)', line)
            if m2:
                s.debounce = int(m2.group(1))
        elif line.startswith("sleep"):
            m2 = re.search(r'(\d+)', line)
            s.sleep = int(m2.group(1)) if m2 else 0
        elif line.startswith("ripple"):
            s.ripple = _parse_on_off(line)
        elif line.startswith("angle snap"):
            s.angle_snap = _parse_on_off(line)
        elif line.startswith("motion sync"):
            s.motion_sync = _parse_on_off(line)
        elif line.startswith("lod"):
            m2 = re.search(r'(\d+)', line)
            if m2:
                s.lod = int(m2.group(1))

    if stages:
        max_idx = max(stages)
        s.stages = [stages.get(i, 0) for i in range(max_idx + 1)]
        # Asegurar siempre 6 perfiles
        while len(s.stages) < 6:
            s.stages.append(s.stages[-1] if s.stages else 800)

    return s


def parse_info(output: str) -> Tuple[Optional[int], bool, str, str]:
    """Parsea 'mchose info' → (battery%, charging, firmware, link)."""
    battery: Optional[int] = None
    charging = False
    firmware = ""
    link = ""
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("battery"):
            m = re.search(r'(\d+)\s*%', line)
            if m:
                battery = int(m.group(1))
            charging = "charg" in line.lower()
        elif line.startswith("firmware"):
            parts = line.split(None, 1)
            firmware = parts[1].strip() if len(parts) > 1 else ""
        elif line.startswith("link"):
            parts = line.split(None, 1)
            link = parts[1].strip() if len(parts) > 1 else ""
    return battery, charging, firmware, link


# ---------------------------------------------------------------------------
# Backend público
# ---------------------------------------------------------------------------
class MchoseCliBackend:
    """Backend completo: lee estado y escribe settings via el CLI de alexfrih."""

    # -- disponibilidad ------------------------------------------------------
    @staticmethod
    def is_available() -> bool:
        return shutil.which(MCHOSE_BINARY) is not None

    # -- lectura del estado completo -----------------------------------------
    def get_state(self) -> MouseState:
        """Llama a 'mchose show' + 'mchose info' y devuelve un MouseState."""
        show_out = _run("show")
        s = parse_show(show_out)
        try:
            info_out = _run("info")
            battery, charging, firmware, link = parse_info(info_out)
            s.battery = battery
            s.charging = charging
            s.firmware = firmware
            s.link = link
        except MchoseCliError:
            pass   # batería no disponible (p. ej. modo cable sin dongle)
        return s

    # -- escritura -----------------------------------------------------------
    def set_dpi_stage(self, stage_index: int, dpi: int) -> None:
        """Cambia el DPI de un perfil específico (0-5) sin tocar los demás."""
        # Leer el estado actual para no borrar los otros perfiles
        try:
            state = parse_show(_run("show", timeout=15))
        except MchoseCliError:
            state = MouseState()
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
        # ripple puede no existir en todos los firmwares; fallo silencioso
        try:
            _run("ripple", "on" if on else "off", timeout=15)
        except MchoseCliError as exc:
            log.debug("set_ripple: %s", exc)

    def set_angle_snap(self, on: bool) -> None:
        _run("angle-snap", "on" if on else "off", timeout=15)
