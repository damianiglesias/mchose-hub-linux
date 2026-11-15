"""
device.py — Capa de dispositivo: descubrir, abrir, enviar comandos y recuperarse de fallos.

    discover(perfiles, backend)  →  [DeviceInfo]          (qué hay conectado y con qué perfil)
    MchoseDevice(info, backend)  →  .execute("set_dpi", dpi=1600)

Tres ideas de diseño:
  1. El «backend» es una fachada mínima sobre el módulo `hid` (hidapi). Los dos paquetes
     PyPI que usan `import hid` (hidapi/cython y hid/ctypes) tienen API casi igual y aquí se
     unifican. Los tests y el modo --demo inyectan un backend falso (demo.py).
  2. MchoseDevice es seguro entre hilos (un RLock serializa «escribir → leer respuesta»).
  3. Fallos esperables en periféricos inalámbricos se traducen a excepciones propias:
       DeviceAsleepError        el dispositivo no contesta (reposo / fuera de alcance)
       DeviceDisconnectedError  el nodo hidraw desapareció (cable/dongle desconectado)
       DeviceAccessError        sin permisos sobre /dev/hidrawN (faltan reglas udev)
"""
from __future__ import annotations

import enum
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .profile import Profile
from .udevgen import KNOWN_VIDS
from .protocol import ProtocolError, build_frame, parse_response, response_matches

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Excepciones
# ----------------------------------------------------------------------------
class BackendUnavailable(Exception):
    """No hay biblioteca HID instalada."""


class DeviceError(Exception):
    """Base de los errores de dispositivo."""


class DeviceAccessError(DeviceError):
    """No se puede abrir el dispositivo (normalmente permisos)."""


class DeviceDisconnectedError(DeviceError):
    """El dispositivo ya no está conectado."""


class DeviceAsleepError(DeviceError):
    """El dispositivo está enumerado pero no responde (reposo, fuera de alcance, ocupado)."""


class State(str, enum.Enum):
    CLOSED = "closed"
    READY = "ready"
    ASLEEP = "asleep"
    DISCONNECTED = "disconnected"


# ----------------------------------------------------------------------------
# Backend HID (fachada sobre el módulo `hid`)
# ----------------------------------------------------------------------------
class _Handle:
    """Dispositivo abierto. Normaliza tipos de retorno (list/bytes) y excepciones (→ OSError)."""

    def __init__(self, raw: Any):
        self._raw = raw

    def _call(self, name: str, *args: Any) -> Any:
        try:
            return getattr(self._raw, name)(*args)
        except (OSError, TypeError, ValueError):
            raise
        except Exception as exc:               # p. ej. hid.HIDException (paquete ctypes)
            raise OSError(str(exc)) from exc

    def write(self, data: bytes) -> int:
        try:
            return self._call("write", data)
        except TypeError:                      # cython-hidapi antiguo sólo acepta listas
            return self._call("write", list(data))

    def send_feature_report(self, data: bytes) -> int:
        try:
            return self._call("send_feature_report", data)
        except TypeError:
            return self._call("send_feature_report", list(data))

    def get_feature_report(self, report_id: int, length: int) -> bytes:
        return bytes(self._call("get_feature_report", report_id, length) or b"")

    def read(self, length: int, timeout_ms: int) -> bytes:
        return bytes(self._call("read", length, timeout_ms) or b"")

    def close(self) -> None:
        try:
            self._raw.close()
        except Exception:                      # cerrar nunca debe lanzar
            pass


class HidBackend:
    """Fachada mínima sobre hidapi. Pasa `module=` para inyectar un backend falso."""

    def __init__(self, module: Any = None):
        if module is None:
            try:
                import hid as module  # type: ignore
            except ImportError as exc:
                raise BackendUnavailable(
                    "No se encuentra la biblioteca HID de Python ('hid').\n"
                    "Ejecuta install.sh o instala el paquete: pip install hidapi") from exc
        self._hid = module

    def enumerate(self) -> List[Dict[str, Any]]:
        """Todas las interfaces HID del sistema (una entrada por colección/interfaz)."""
        return list(self._hid.enumerate(0, 0))

    def report_descriptor(self, path: bytes) -> Optional[bytes]:
        """Report descriptor HID del nodo. Se lee de sysfs: NO hacen falta permisos sobre /dev/hidrawN."""
        fake = getattr(self._hid, "report_descriptor", None)   # backend simulado (demo/tests)
        if fake is not None:
            return fake(path)
        p = path.decode(errors="replace") if isinstance(path, bytes) else str(path)
        if p.startswith("/dev/hidraw"):
            try:
                return Path(f"/sys/class/hidraw/{p[5:]}/device/report_descriptor").read_bytes()
            except OSError:
                return None
        return None

    def open(self, path: bytes) -> _Handle:
        try:
            if hasattr(self._hid, "device"):   # cython-hidapi: hid.device().open_path()
                dev = self._hid.device()
                dev.open_path(path)
            else:                              # paquete ctypes: hid.Device(path=...)
                dev = self._hid.Device(path=path)
        except OSError:
            raise
        except Exception as exc:
            raise OSError(str(exc)) from exc
        return _Handle(dev)


# ----------------------------------------------------------------------------
# Descubrimiento
# ----------------------------------------------------------------------------
@dataclass
class DeviceInfo:
    """Un dispositivo concreto conectado, ya emparejado con su perfil."""
    profile: Profile
    path: bytes
    vendor_id: int
    product_id: int
    serial: str = ""
    product: str = ""
    usage_page: int = 0
    usage: int = 0
    interface_number: int = -1

    @property
    def path_str(self) -> str:
        return self.path.decode(errors="replace") if isinstance(self.path, bytes) else str(self.path)

    @property
    def vid_pid(self) -> str:
        return f"{self.vendor_id:04X}:{self.product_id:04X}"

    @property
    def display_name(self) -> str:
        return self.product or self.profile.name

    @property
    def key(self) -> str:
        """Identificador estable mientras el dispositivo siga conectado."""
        return f"{self.vid_pid}@{self.path_str}"


def _interface_ok(profile: Profile, e: Dict[str, Any]) -> bool:
    """¿Es ésta la interfaz de configuración? Evita tocar la interfaz de escritura del teclado."""
    if profile.usage_page is None and profile.interface_number is None and profile.usage is None:
        return e.get("usage_page", 0) >= 0xFF00          # por defecto: sólo «vendor defined»
    if profile.usage_page is not None and e.get("usage_page") != profile.usage_page:
        return False
    if profile.usage is not None and e.get("usage") != profile.usage:
        return False
    if profile.interface_number is not None and e.get("interface_number") != profile.interface_number:
        return False
    return True


def entry_matches(profile: Profile, e: Dict[str, Any]) -> bool:
    return profile.matches_ids(e.get("vendor_id", 0), e.get("product_id", 0)) and _interface_ok(profile, e)


def discover(profiles: List[Profile], backend: HidBackend) -> List[DeviceInfo]:
    """Empareja las interfaces HID conectadas con los perfiles cargados."""
    entries = backend.enumerate()
    found: Dict[bytes, DeviceInfo] = {}
    for prof in profiles:
        for e in entries:
            if not entry_matches(prof, e) or e["path"] in found:
                continue
            found[e["path"]] = DeviceInfo(
                profile=prof, path=e["path"], vendor_id=e["vendor_id"], product_id=e["product_id"],
                serial=e.get("serial_number") or "", product=e.get("product_string") or "",
                usage_page=e.get("usage_page", 0), usage=e.get("usage", 0),
                interface_number=e.get("interface_number", -1))
    return sorted(found.values(), key=lambda d: d.path_str)


@dataclass
class UnknownDevice:
    """Dispositivo detectado SIN perfil utilizable. Se muestra igualmente para poder diagnosticarlo."""
    vendor_id: int
    product_id: int
    product: str
    serial: str
    reason: str
    interfaces: List[Dict[str, Any]]      # entradas crudas de enumerate()

    @property
    def vid_pid(self) -> str:
        return f"{self.vendor_id:04X}:{self.product_id:04X}"

    @property
    def display_name(self) -> str:
        return self.product or f"Dispositivo HID {self.vid_pid}"

    @property
    def key(self) -> str:
        return f"unk:{self.vid_pid}:{self.serial}"

    def nodes(self) -> Dict[bytes, List[Dict[str, Any]]]:
        """Interfaces agrupadas por nodo (/dev/hidrawN): un nodo puede tener varias colecciones/usages."""
        out: Dict[bytes, List[Dict[str, Any]]] = {}
        for e in sorted(self.interfaces, key=lambda e: (e.get("interface_number", -1), e["path"])):
            out.setdefault(e["path"], []).append(e)
        return out


def discover_unprofiled(profiles: List[Profile], backend: HidBackend,
                        claimed: Optional[List[DeviceInfo]] = None,
                        show_all: bool = False) -> List[UnknownDevice]:
    """Dispositivos presentes que NINGÚN perfil reclama.

    Por defecto sólo los que «parecen MCHOSE» (nombre, VID de un perfil o VID candidato). Con
    show_all=True, todos los HID del sistema. `claimed` = lo que discover() ya emparejó."""
    taken = {(i.vendor_id, i.product_id) for i in (claimed or [])}
    interesting = {vid for p in profiles for vid in p.vendor_ids if vid} | set(KNOWN_VIDS)
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for e in backend.enumerate():
        groups.setdefault((e["vendor_id"], e["product_id"], e.get("serial_number") or ""), []).append(e)

    found: List[UnknownDevice] = []
    for (vid, pid, serial), entries in sorted(groups.items()):
        if (vid, pid) in taken:
            continue
        text = " ".join(f"{e.get('manufacturer_string') or ''} {e.get('product_string') or ''}"
                        for e in entries).lower()
        by_ids = [p for p in profiles if p.matches_ids(vid, pid)]
        if not (show_all or "mchose" in text or vid in interesting or by_ids):
            continue
        reason = ("sin perfil: todavía no se puede configurar" if not by_ids else
                  f"el perfil '{by_ids[0].id}' coincide en VID:PID pero ninguna interfaz cumple "
                  f"usage_page/interface_number")
        product = next((e.get("product_string") for e in entries if e.get("product_string")), "") or ""
        found.append(UnknownDevice(vid, pid, product, serial, reason, entries))
    return found


# ----------------------------------------------------------------------------
# Dispositivo
# ----------------------------------------------------------------------------
class MchoseDevice:
    """Un periférico abierto. Todas las operaciones públicas son seguras entre hilos."""

    REOPEN_ATTEMPTS = 3

    def __init__(self, info: DeviceInfo, backend: HidBackend):
        self.info = info
        self.profile = info.profile
        self._backend = backend
        self._handle: Optional[_Handle] = None
        self._lock = threading.RLock()
        self.state = State.CLOSED

    # -- ciclo de vida -------------------------------------------------------
    def open(self) -> None:
        """Abre el nodo hidraw (idempotente). Traduce los fallos a excepciones con pista útil."""
        with self._lock:
            if self._handle is not None:
                return
            self._check_access()
            try:
                self._handle = self._backend.open(self.info.path)
            except OSError as exc:
                if not self._is_present():
                    self.state = State.DISCONNECTED
                    raise DeviceDisconnectedError(f"{self.info.display_name} ya no está conectado") from exc
                raise DeviceAccessError(
                    f"No se pudo abrir {self.info.path_str}: {exc}. ¿Están instaladas las reglas udev "
                    f"(install.sh)? Si acabas de instalarlas, reconecta el dispositivo.") from exc
            self.state = State.READY
            self._init_device()

    def _init_device(self) -> None:
        """Inicialización post-apertura: desbloquea el canal largo (CMD 0x58).
        Se llama desde open() con el lock ya tomado. Falla silenciosamente."""
        if 'set_active_profile' not in self.profile.commands:
            return
        try:
            import time as _time
            cmd = self.profile.commands['set_active_profile']
            frame = build_frame(self.profile.transport, cmd, {'profile': 0})
            self._write(frame)
            _time.sleep(0.15)
        except Exception:
            pass  # El canal se desbloqueará en el primer intento de lectura

    def close(self) -> None:
        with self._lock:
            self._drop_handle()
            self.state = State.CLOSED

    def __enter__(self) -> "MchoseDevice":
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _drop_handle(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def _check_access(self) -> None:
        """Diagnóstico previo (sólo backend hidraw, donde la ruta es /dev/hidrawN)."""
        path = self.info.path_str
        if not path.startswith("/dev/"):
            return
        if not os.path.exists(path):
            raise DeviceDisconnectedError(f"{path} ya no existe: dispositivo desconectado")
        if not os.access(path, os.R_OK | os.W_OK):
            raise DeviceAccessError(
                f"Sin permisos sobre {path}. Instala las reglas udev (install.sh) y reconecta el dispositivo.")

    # -- API pública ---------------------------------------------------------
    def execute(self, command: str, **values: Any) -> Optional[Dict[str, Any]]:
        """Envía un comando del perfil. Devuelve los campos de la respuesta (o None si no hay).

        Ejemplo: dev.execute("set_dpi", slot=0, dpi=1600)  ·  dev.execute("get_battery")
        """
        cmd = self.profile.commands.get(command)
        if cmd is None:
            raise ProtocolError(f"el perfil '{self.profile.id}' no define el comando '{command}'")
        frame = build_frame(self.profile.transport, cmd, values)   # valida parámetros antes de tocar USB
        with self._lock:
            result = self._transact(cmd, frame)
            for follow_up in cmd.then:                              # p. ej. un «commit/apply»
                self.execute(follow_up)
        return result

    # -- E/S con recuperación ------------------------------------------------
    def _transact(self, cmd: Any, frame: bytes) -> Optional[Dict[str, Any]]:
        t = self.profile.transport
        attempts_left = t.retries + 1
        reopens_left = 2
        while attempts_left > 0:
            attempts_left -= 1
            try:
                self.open()
                self._write(frame)
                time.sleep(t.inter_command_delay_ms / 1000.0)
                if cmd.response is None:
                    self.state = State.READY
                    return None
                data = self._read_response(cmd, t.xor_mask)
                if data is not None:
                    self.state = State.READY
                    return parse_response(cmd, data, t.xor_mask)
                # Escritura OK pero silencio: lo típico de un ratón/teclado inalámbrico dormido.
                log.debug("%s: sin respuesta a '%s' (quedan %d intentos)",
                          self.info.vid_pid, cmd.name, attempts_left)
                time.sleep(t.wake_delay_ms / 1000.0)               # darle tiempo a despertar
            except OSError as exc:
                log.warning("Error de E/S con %s: %s", self.info.vid_pid, exc)
                self._drop_handle()
                if reopens_left > 0 and self._reopen():
                    reopens_left -= 1
                    attempts_left += 1          # reabrir con éxito no gasta un intento normal
                    continue
                if not self._is_present():
                    self.state = State.DISCONNECTED
                    raise DeviceDisconnectedError(
                        f"{self.info.display_name} se ha desconectado") from exc
                break                            # sigue enumerado pero falla: lo tratamos como reposo

        if cmd.response is not None and not cmd.response.required:
            return {}
        self.state = State.ASLEEP
        raise DeviceAsleepError(
            f"{self.info.display_name} no responde (¿en reposo o fuera de alcance?). "
            f"Muévelo o pulsa un botón y reintenta.")

    def _write(self, frame: bytes) -> None:
        assert self._handle is not None
        if self.profile.transport.write_mode == "feature_report":
            n = self._handle.send_feature_report(frame)
        else:
            n = self._handle.write(frame)
        if isinstance(n, int) and n < 0:
            raise OSError("la escritura HID devolvió un error")

    def _read_response(self, cmd: Any, xor_mask: int = 0x00) -> Optional[bytes]:
        """Espera una respuesta que cumpla `expect`; descarta paquetes ajenos. None = timeout."""
        assert self._handle is not None
        t = self.profile.transport
        # Usar report_id y length del comando si lo sobreescribe
        report_id = cmd.report_id if cmd.report_id is not None else t.report_id
        report_length = cmd.report_length if cmd.report_length is not None else t.report_length
        deadline = time.monotonic() + t.response_timeout_ms / 1000.0
        while True:
            if t.read_mode == "feature_report":
                data = self._handle.get_feature_report(report_id, report_length)
            else:  # input_report
                remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
                data = self._handle.read(report_length, remaining_ms)
            if data and response_matches(cmd, data, xor_mask):
                return data
            if time.monotonic() >= deadline:
                return None
            if t.read_mode == "feature_report":
                time.sleep(0.01)                 # get_feature_report no bloquea: sondeo suave

    # -- reconexión ------------------------------------------------------------
    def _find_current_entry(self) -> Optional[Dict[str, Any]]:
        """Busca el mismo dispositivo en la enumeración actual (la ruta puede haber cambiado)."""
        for e in self._backend.enumerate():
            if not entry_matches(self.profile, e):
                continue
            if self.info.serial and e.get("serial_number") and e["serial_number"] != self.info.serial:
                continue
            if e.get("vendor_id") == self.info.vendor_id and e.get("product_id") == self.info.product_id:
                return e
        return None

    def _is_present(self) -> bool:
        try:
            return self._find_current_entry() is not None
        except Exception:
            return False

    def _reopen(self) -> bool:
        """Tras un fallo de E/S (p. ej. el USB se reinició al despertar), intenta volver a abrir."""
        for _ in range(self.REOPEN_ATTEMPTS):
            time.sleep(0.25)
            try:
                entry = self._find_current_entry()
                if entry is None:
                    continue
                self.info.path = entry["path"]
                self._handle = self._backend.open(self.info.path)
                self.state = State.READY
                log.info("%s: reabierto en %s", self.info.vid_pid, self.info.path_str)
                return True
            except (OSError, DeviceError):
                continue
        return False
