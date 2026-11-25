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
    rgb: Optional[Dict[str, Any]] = None   # RGB state if supported

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
        elif line.startswith("rgb"):
            # Parse RGB data from show output if available
            r = g = b = effect = speed = brightness = None
            if "rgb:" in line.lower():
                match = re.search(r'rgb:\s*(\d+),\s*(\d+),\s*(\d+)', line)
                if match:
                    r, g, b = int(match.group(1)), int(match.group(2)), int(match.group(3))
            match = re.search(r'effect\s*=\s*(\d+)', line)
            if match: effect = int(match.group(1))
            match = re.search(r'speed\s*=\s*(\d+)', line)
            if match: speed = int(match.group(1))
            match = re.search(r'brightness\s*=\s*(\d+)', line)
            if match: brightness = int(match.group(1))
            s.rgb = {
                "r": r, "g": g, "b": b,
                "effect_id": effect,
                "speed": speed,
                "brightness": brightness
            } if any(x is not None for x in [r, g, b]) else None
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

    def __enter__(self):
        """Context manager opcional para compatibilidad con código que llama backend.open()."""
        return self

    def __exit__(self, *args):
        pass  # No hay recursos que cerrar en el modo CLI

    def open(self):
        """Context manager opcional para compatibilidad con código que llama backend.open()."""
        # El CLI no requiere mantener un canal abierto; cada comando es independiente.
        return self

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

    def get_rgb(self) -> Optional[Dict[str, Any]]:
        """Devuelve el estado RGB si está soportado, o None si no hay comando."""
        try:
            # Intentar leer RGB con el comando rgb show
            show = _run("rgb", timeout=15)
            # El CLI devuelve algo como "rgb: 128,64,32, effect=0, speed=10, brightness=10"
            # o simplemente la información RGB disponible.
            # Buscamos patrones típicos en la salida
            r = g = b = None
            effect = speed = brightness = None
            if "rgb:" in show.lower():
                # Extraemos los valores RGB (R,G,B, ...)
                match = re.search(r"rgb:\s*(\d+),\s*(\d+),\s*(\d+)", show)
                if match:
                    r, g, b = int(match.group(1)), int(match.group(2)), int(match.group(3))
                # Extraemos el effect ID si existe
                match = re.search(r"effect\s*=\s*(\d+)", show)
                if match:
                    effect = int(match.group(1))
                # Extraemos la velocidad si existe
                match = re.search(r"speed\s*=\s*(\d+)", show)
                if match:
                    speed = int(match.group(1))
                # Extraemos el brillo si existe
                match = re.search(r"brightness\s*=\s*(\d+)", show)
                if match:
                    brightness = int(match.group(1))
            # Si no hay salida explícita, devolvemos None
            return {
                "r": r, "g": g, "b": b,
                "effect_id": effect,
                "speed": speed,
                "brightness": brightness
            } if any(x is not None for x in [r, g, b]) else None
        except MchoseCliError:
            # Si el comando no existe o falla
            return None


    # RGB commands (if supported by the device)
    def set_rgb_effect(self, effect_id: int) -> None:
        """Establece el efecto RGB por ID."""
        try:
            _run("rgb", "--effect", str(effect_id), timeout=15)
        except MchoseCliError:
            # El comando podría no existir en todos los dispositivos
            pass

    def set_rgb_speed(self, speed: int) -> None:
        """Establece la velocidad del efecto RGB (0-255)."""
        try:
            _run("rgb", "--speed", str(speed), timeout=15)
        except MchoseCliError:
            pass

    def set_rgb_brightness(self, brightness: int) -> None:
        """Establece el brillo RGB (0-255)."""
        try:
            _run("rgb", "--brightness", str(brightness), timeout=15)
        except MchoseCliError:
            pass

    def set_rgb_color(self, r: int, g: int, b: int) -> None:
        """Establece el color RGB (0-255 cada canal)."""
        try:
            _run("rgb", "--r", str(r), "--g", str(g), "--b", str(b), timeout=15)
        except MchoseCliError:
            pass


def save_base_profile(dev: Any, backend: Any, directory: Optional[Path] = None) -> Path:
    """Guarda el perfil base en el directorio de perfiles del usuario. Si ya existe (p. ej. el cable
    ya tiene perfil y ahora conectas el dongle) sólo añade el nuevo VID:PID."""
    from hidinfo import make_base_profile, user_profile_dir  # noqa: E402

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

