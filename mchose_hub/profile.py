"""
profile.py — Perfiles de dispositivo (JSON/YAML) → objetos validados.

Un perfil describe UN modelo (o familia) de periférico:
  * cómo reconocerlo en el bus (VID / PID / usage page / interfaz),
  * cómo «hablarle» (tipo de report, longitud, checksum, tiempos),
  * qué comandos existen (plantilla de bytes + parámetros + formato de respuesta),
  * qué controles debe dibujar la GUI (`features`).

El motor (protocol.py + device.py) NO contiene ningún byte específico de MCHOSE:
todo vive aquí, en datos. Añadir un dispositivo = añadir un archivo .json.

Este módulo no importa `hid` ni `gi`, así que lo usan también el generador de reglas
udev, el validador de la CLI y los tests.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
INT_WIDTHS = {"u8": 1, "u16le": 2, "u16be": 2, "u32le": 4, "u32be": 4}
PARAM_TYPES = set(INT_WIDTHS) | {"rgb", "bool"}
WIDGETS = {"choice", "slider", "color", "switch", "battery", "mapping"}
CHECKSUMS = {"sum8", "sub8", "xor8"}
KINDS = {"mouse", "keyboard", "other"}


class ProfileError(ValueError):
    """Perfil inválido. El mensaje indica dónde está el fallo para que el autor lo corrija."""


# ----------------------------------------------------------------------------
# Pequeños helpers de validación
# ----------------------------------------------------------------------------
def to_int(value: Any, where: str) -> int:
    """Acepta 31, "31" o "0x1F" y devuelve un int (JSON no tiene literales hex)."""
    if isinstance(value, bool):  # bool es subclase de int: lo rechazamos a propósito
        raise ProfileError(f"{where}: se esperaba un número, no un booleano")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip(), 0)
        except ValueError:
            pass
    raise ProfileError(f"{where}: valor numérico inválido: {value!r}")


def _opt_int(d: dict, key: str, where: str) -> Optional[int]:
    return None if d.get(key) is None else to_int(d[key], f"{where}.{key}")


def _obj(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise ProfileError(f"{where}: se esperaba un objeto JSON {{...}}")
    return value


def _require(d: dict, key: str, where: str) -> Any:
    if key not in d:
        raise ProfileError(f"{where}: falta la clave obligatoria '{key}'")
    return d[key]


def _linear(d: dict, where: str) -> Optional[Tuple[int, int, int]]:
    """'linear': {mul, div, add}  →  valor_crudo = valor * mul // div + add."""
    if d.get("linear") is None:
        return None
    lin = _obj(d["linear"], f"{where}.linear")
    mul = to_int(lin.get("mul", 1), f"{where}.linear.mul")
    div = to_int(lin.get("div", 1), f"{where}.linear.div")
    add = to_int(lin.get("add", 0), f"{where}.linear.add")
    if div == 0:
        raise ProfileError(f"{where}.linear.div no puede ser 0")
    return (mul, div, add)


# ----------------------------------------------------------------------------
# Estructuras de datos (lo que consume el motor)
# ----------------------------------------------------------------------------
@dataclass
class Choice:
    label: str                 # texto que ve el usuario
    value: Any                 # valor lógico (p. ej. 1000 para «1000 Hz»)
    wire: Optional[int] = None  # valor crudo a enviar; si falta, se codifica `value`


@dataclass
class ParamSpec:
    name: str
    type: str = "u8"
    default: Any = None
    min: Optional[float] = None
    max: Optional[float] = None
    step: float = 1
    unit: str = ""
    linear: Optional[Tuple[int, int, int]] = None
    choices: List[Choice] = field(default_factory=list)
    rgb_order: str = "rgb"     # orden de los 3 bytes de color: "rgb", "grb", "bgr"…

    @property
    def width(self) -> int:
        """Bytes que ocupa este parámetro en el frame."""
        return 3 if self.type == "rgb" else INT_WIDTHS.get(self.type, 1)


@dataclass
class FieldSpec:
    """Campo de una respuesta: dónde está y cómo se interpreta."""
    name: str
    offset: int
    type: str = "u8"
    mask: Optional[int] = None
    shift: int = 0
    linear: Optional[Tuple[int, int, int]] = None
    as_bool: bool = False
    labels: Dict[str, str] = field(default_factory=dict)


@dataclass
class ResponseSpec:
    required: bool = True
    expect: Dict[int, int] = field(default_factory=dict)   # offset → byte esperado
    fields: Dict[str, FieldSpec] = field(default_factory=dict)


@dataclass
class CommandSpec:
    name: str
    tokens: List[Union[int, str]]          # int = byte literal; str = nombre de parámetro
    params: Dict[str, ParamSpec] = field(default_factory=dict)
    response: Optional[ResponseSpec] = None
    then: List[str] = field(default_factory=list)   # comandos a enviar después (p. ej. «commit»)
    description: str = ""
    report_id: Optional[int] = None       # sobreescribe transport.report_id para este comando
    report_length: Optional[int] = None   # sobreescribe transport.report_length para este comando


@dataclass
class ChecksumSpec:
    algorithm: str   # sum8: (init+Σ) & FF · sub8: (init−Σ) & FF · xor8: init ^ bytes…
    init: int
    start: int       # rango [start, end) sobre el frame completo (el byte 0 es el Report ID)
    end: int
    store_at: int    # índice del frame donde se escribe el checksum


@dataclass
class TransportSpec:
    write_mode: str = "feature_report"    # feature_report | output_report
    read_mode: str = "none"               # feature_report | input_report | none
    report_id: int = 0
    report_length: int = 64               # bytes TOTALES del frame, incluido el Report ID
    response_timeout_ms: int = 500
    retries: int = 2
    wake_delay_ms: int = 300              # pausa entre reintentos (despertar dispositivos dormidos)
    inter_command_delay_ms: int = 20
    xor_mask: int = 0x00                 # 0xFF para el protocolo MCHOSE L7/A7 (payload invertido ^ 0xFF, sin checksum)
    checksum: Optional[ChecksumSpec] = None


@dataclass
class Slot:
    label: str
    fixed: Dict[str, Any] = field(default_factory=dict)
    default: Any = None        # opción preseleccionada en la GUI (la GUI no lee el estado real)


@dataclass
class FeatureSpec:
    """Un control de la GUI. Se enlaza con un comando y (casi siempre) con uno de sus parámetros."""
    id: str
    label: str
    widget: str
    command: str
    param: Optional[str] = None
    fixed: Dict[str, Any] = field(default_factory=dict)
    group: str = "Ajustes"
    description: str = ""
    slots: List[Slot] = field(default_factory=list)   # sólo widget «mapping»
    percent: Optional[str] = None                     # sólo widget «battery»
    charging: Optional[str] = None
    poll_seconds: int = 60


@dataclass
class Profile:
    id: str
    name: str
    kind: str
    enabled: bool
    verified: bool
    ids: Tuple[Tuple[int, int], ...]     # pares (VID, PID): cable, dongle y Bluetooth pueden tener VID distintos
    transport: TransportSpec
    commands: Dict[str, CommandSpec]
    features: List[FeatureSpec]
    usage_page: Optional[int] = None
    usage: Optional[int] = None
    interface_number: Optional[int] = None
    author: str = ""
    notes: str = ""
    source: Optional[Path] = None

    @property
    def vendor_ids(self) -> Tuple[int, ...]:
        return tuple(sorted({vid for vid, _ in self.ids}))

    def matches_ids(self, vendor_id: int, product_id: int) -> bool:
        return (vendor_id, product_id) in self.ids


# ----------------------------------------------------------------------------
# Parseo por secciones
# ----------------------------------------------------------------------------
def _parse_param(name: str, d: dict, where: str) -> ParamSpec:
    d = _obj(d, where)
    ptype = d.get("type", "u8")
    if ptype not in PARAM_TYPES:
        raise ProfileError(f"{where}.type: '{ptype}' no válido (usa {sorted(PARAM_TYPES)})")
    choices = []
    for i, c in enumerate(d.get("choices", [])):
        c = _obj(c, f"{where}.choices[{i}]")
        choices.append(Choice(
            label=str(_require(c, "label", f"{where}.choices[{i}]")),
            value=_require(c, "value", f"{where}.choices[{i}]"),
            wire=_opt_int(c, "wire", f"{where}.choices[{i}]"),
        ))
    order = str(d.get("rgb_order", "rgb")).lower()
    if sorted(order) != ["b", "g", "r"]:
        raise ProfileError(f"{where}.rgb_order: debe ser una permutación de 'rgb' (p. ej. 'grb')")
    return ParamSpec(
        name=name, type=ptype, default=d.get("default"),
        min=d.get("min"), max=d.get("max"), step=d.get("step", 1),
        unit=str(d.get("unit", "")), linear=_linear(d, where), choices=choices, rgb_order=order,
    )


def _parse_field(name: str, d: dict, where: str) -> FieldSpec:
    d = _obj(d, where)
    ftype = d.get("type", "u8")
    if ftype not in INT_WIDTHS:
        raise ProfileError(f"{where}.type: '{ftype}' no válido (usa {sorted(INT_WIDTHS)})")
    return FieldSpec(
        name=name, offset=to_int(_require(d, "offset", where), f"{where}.offset"), type=ftype,
        mask=_opt_int(d, "mask", where), shift=to_int(d.get("shift", 0), f"{where}.shift"),
        linear=_linear(d, where), as_bool=bool(d.get("bool", False)),
        labels={str(k): str(v) for k, v in d.get("labels", {}).items()},
    )


def _parse_response(d: dict, where: str) -> ResponseSpec:
    d = _obj(d, where)
    expect = {to_int(k, f"{where}.expect"): to_int(v, f"{where}.expect[{k}]")
              for k, v in _obj(d.get("expect", {}), f"{where}.expect").items()}
    fields = {n: _parse_field(n, fd, f"{where}.fields.{n}")
              for n, fd in _obj(d.get("fields", {}), f"{where}.fields").items()}
    return ResponseSpec(required=bool(d.get("required", True)), expect=expect, fields=fields)


_TOKEN = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _parse_template(text: Any, params: Dict[str, ParamSpec], where: str) -> List[Union[int, str]]:
    """'07 {slot} {dpi}' → [0x07, 'slot', 'dpi']  (bytes literales en hex o marcadores {param})."""
    tokens: List[Union[int, str]] = []
    for tok in str(text).split():
        m = _TOKEN.match(tok)
        if m:
            if m.group(1) not in params:
                raise ProfileError(f"{where}: el marcador {tok} no está declarado en 'params'")
            tokens.append(m.group(1))
            continue
        try:
            byte = int(tok, 16)
        except ValueError:
            raise ProfileError(f"{where}: '{tok}' no es un byte hex (p. ej. 0A) ni un marcador {{param}}")
        if not 0 <= byte <= 0xFF:
            raise ProfileError(f"{where}: '{tok}' fuera de rango (00–FF)")
        tokens.append(byte)
    return tokens


def _parse_checksum(d: dict, length: int, where: str) -> ChecksumSpec:
    d = _obj(d, where)
    algo = d.get("algorithm", "sum8")
    if algo not in CHECKSUMS:
        raise ProfileError(f"{where}.algorithm: '{algo}' no válido (usa {sorted(CHECKSUMS)})")
    cs = ChecksumSpec(
        algorithm=algo, init=to_int(d.get("init", 0), f"{where}.init"),
        start=to_int(d.get("start", 0), f"{where}.start"),
        end=to_int(_require(d, "end", where), f"{where}.end"),
        store_at=to_int(_require(d, "store_at", where), f"{where}.store_at"),
    )
    if not (0 <= cs.start < cs.end <= length and 0 <= cs.store_at < length):
        raise ProfileError(f"{where}: start/end/store_at deben caber en report_length={length}")
    return cs


def _parse_transport(d: dict, where: str) -> TransportSpec:
    d = _obj(d, where)
    t = TransportSpec(
        write_mode=d.get("write_mode", "feature_report"),
        read_mode=d.get("read_mode", "none"),
        report_id=to_int(d.get("report_id", 0), f"{where}.report_id"),
        report_length=to_int(d.get("report_length", 64), f"{where}.report_length"),
        response_timeout_ms=to_int(d.get("response_timeout_ms", 500), f"{where}.response_timeout_ms"),
        retries=to_int(d.get("retries", 2), f"{where}.retries"),
        wake_delay_ms=to_int(d.get("wake_delay_ms", 300), f"{where}.wake_delay_ms"),
        inter_command_delay_ms=to_int(d.get("inter_command_delay_ms", 20), f"{where}.inter_command_delay_ms"),
        xor_mask=to_int(d.get("xor_mask", 0), f"{where}.xor_mask"),
    )
    if t.write_mode not in ("feature_report", "output_report"):
        raise ProfileError(f"{where}.write_mode: usa feature_report u output_report")
    if t.read_mode not in ("feature_report", "input_report", "none"):
        raise ProfileError(f"{where}.read_mode: usa feature_report, input_report o none")
    if not 0 <= t.report_id <= 0xFF:
        raise ProfileError(f"{where}.report_id fuera de rango (0–255)")
    if not 2 <= t.report_length <= 4096:
        raise ProfileError(f"{where}.report_length debe estar entre 2 y 4096")
    if d.get("checksum"):
        t.checksum = _parse_checksum(d["checksum"], t.report_length, f"{where}.checksum")
    return t


def _parse_command(name: str, d: dict, transport: TransportSpec, where: str) -> CommandSpec:
    d = _obj(d, where)
    params = {pn: _parse_param(pn, pd, f"{where}.params.{pn}")
              for pn, pd in _obj(d.get("params", {}), f"{where}.params").items()}
    tokens = _parse_template(_require(d, "request", where), params, f"{where}.request")
    body = sum(1 if isinstance(t, int) else params[t].width for t in tokens)
    if 1 + body > transport.report_length:
        raise ProfileError(f"{where}.request: ocupa {1 + body} bytes y report_length es {transport.report_length}")
    cs = transport.checksum
    if cs and cs.store_at < 1 + body:
        raise ProfileError(f"{where}.request: pisaría el byte de checksum (índice {cs.store_at})")
    response = _parse_response(d["response"], f"{where}.response") if d.get("response") else None
    if response and transport.read_mode == "none":
        raise ProfileError(f"{where}: define 'response' pero transport.read_mode es 'none'")
    rid = _opt_int(d, "report_id", where)
    rlen = _opt_int(d, "report_length", where)
    return CommandSpec(name=name, tokens=tokens, params=params, response=response,
                       then=[str(x) for x in d.get("then", [])], description=str(d.get("description", "")),
                       report_id=rid, report_length=rlen)


def _infer_widget(p: ParamSpec) -> str:
    if p.choices:
        return "choice"
    return {"rgb": "color", "bool": "switch"}.get(p.type, "slider")


def _parse_feature(d: dict, commands: Dict[str, CommandSpec], where: str) -> FeatureSpec:
    d = _obj(d, where)
    fid = str(_require(d, "id", where))
    where = f"{where}({fid})"
    cmd_name = str(_require(d, "command", where))
    if cmd_name not in commands:
        raise ProfileError(f"{where}.command: el comando '{cmd_name}' no existe")
    cmd = commands[cmd_name]
    param_name = d.get("param")
    if param_name is not None and param_name not in cmd.params:
        raise ProfileError(f"{where}.param: '{param_name}' no es un parámetro de '{cmd_name}'")
    fixed = _obj(d.get("fixed", {}), f"{where}.fixed")
    slots = [Slot(label=str(_require(_obj(s, f"{where}.slots[{i}]"), "label", f"{where}.slots[{i}]")),
                  fixed=dict(s.get("fixed", {})), default=s.get("default"))
             for i, s in enumerate(d.get("slots", []))]
    for extra in [fixed] + [s.fixed for s in slots]:
        for k in extra:
            if k not in cmd.params:
                raise ProfileError(f"{where}.fixed: '{k}' no es un parámetro de '{cmd_name}'")

    widget = d.get("widget") or (_infer_widget(cmd.params[param_name]) if param_name else None)
    if widget not in WIDGETS:
        raise ProfileError(f"{where}.widget: indica uno de {sorted(WIDGETS)} (o define 'param' para inferirlo)")
    feat = FeatureSpec(id=fid, label=str(d.get("label", fid)), widget=widget, command=cmd_name,
                       param=param_name, fixed=dict(fixed), group=str(d.get("group", "Ajustes")),
                       description=str(d.get("description", "")), slots=slots,
                       percent=d.get("percent"), charging=d.get("charging"),
                       poll_seconds=max(5, to_int(d.get("poll_seconds", 60), f"{where}.poll_seconds")))

    if widget == "battery":
        resp = cmd.response
        if not resp or feat.percent not in resp.fields:
            raise ProfileError(f"{where}.percent: debe nombrar un campo de la respuesta de '{cmd_name}'")
        if feat.charging is not None and feat.charging not in resp.fields:
            raise ProfileError(f"{where}.charging: no es un campo de la respuesta de '{cmd_name}'")
        return feat

    if param_name is None:
        raise ProfileError(f"{where}: el widget '{widget}' necesita 'param'")
    p = cmd.params[param_name]
    if widget == "slider" and (p.min is None or p.max is None):
        raise ProfileError(f"{where}: un slider necesita min y max en el parámetro '{param_name}'")
    if widget in ("choice", "mapping") and not p.choices:
        raise ProfileError(f"{where}: el parámetro '{param_name}' necesita 'choices' para el widget '{widget}'")
    if widget == "mapping" and not slots:
        raise ProfileError(f"{where}: el widget 'mapping' necesita 'slots'")
    # Todo parámetro sin valor por defecto debe estar controlado por esta feature o fijado.
    for pn, ps in cmd.params.items():
        covered = (pn == param_name or pn in feat.fixed
                   or (bool(slots) and all(pn in s.fixed for s in slots)))
        if ps.default is None and not covered:
            raise ProfileError(f"{where}: el parámetro '{pn}' de '{cmd_name}' no tiene 'default' ni 'fixed'")
    return feat


def _parse_ids(match: dict, where: str) -> Tuple[Tuple[int, int], ...]:
    """Admite `"ids": ["3837:4018", "5253:1020"]` (VID:PID en hexadecimal, como en lsusb) y/o la forma
    clásica `vendor_id` + `product_ids`. Un mismo ratón puede tener VID distintos por cable y por dongle."""
    ids: List[Tuple[int, int]] = []
    for item in match.get("ids", []):
        vid_s, sep, pid_s = str(item).partition(":")
        try:
            if not sep:
                raise ValueError
            ids.append((int(vid_s, 16), int(pid_s, 16)))
        except ValueError:
            raise ProfileError(f"{where}.ids: '{item}' debe ser 'VID:PID' en hexadecimal (p. ej. '3837:4018')") from None
    if "vendor_id" in match:
        vid = to_int(match["vendor_id"], f"{where}.vendor_id")
        for pid in _require(match, "product_ids", where):
            ids.append((vid, to_int(pid, f"{where}.product_ids")))
    if not ids:
        raise ProfileError(f"{where}: define 'ids' o bien 'vendor_id' + 'product_ids'")
    if not all(0 <= x <= 0xFFFF for pair in ids for x in pair):
        raise ProfileError(f"{where}: VID/PID fuera de rango (0–0xFFFF)")
    return tuple(dict.fromkeys(ids))          # sin duplicados, conservando el orden


def parse_profile(data: Any, source: Optional[Path] = None) -> Profile:
    """Valida un dict (ya cargado de JSON/YAML) y construye un `Profile`."""
    where = source.name if source else "perfil"
    root = _obj(data, where)
    if root.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise ProfileError(f"{where}: schema_version no soportada (esperada {SCHEMA_VERSION})")

    dev = _obj(_require(root, "device", where), f"{where}.device")
    match = _obj(_require(root, "match", where), f"{where}.match")
    kind = dev.get("kind", "other")
    if kind not in KINDS:
        raise ProfileError(f"{where}.device.kind: usa {sorted(KINDS)}")

    ids = _parse_ids(match, f"{where}.match")
    enabled = bool(dev.get("enabled", True))
    if enabled and any(vid == 0 for vid, _ in ids):
        raise ProfileError(f"{where}: enabled=true pero hay un VID 0x0000 (¿plantilla sin rellenar?)")

    transport = _parse_transport(_require(root, "transport", where), f"{where}.transport")
    commands = {n: _parse_command(n, c, transport, f"{where}.commands.{n}")
                for n, c in _obj(_require(root, "commands", where), f"{where}.commands").items()}
    for c in commands.values():
        for nxt in c.then:
            if nxt not in commands:
                raise ProfileError(f"{where}.commands.{c.name}.then: el comando '{nxt}' no existe")
            if commands[nxt].then:   # evita bucles infinitos: un comando de seguimiento no encadena otro
                raise ProfileError(f"{where}.commands.{c.name}.then: '{nxt}' no puede tener a su vez 'then'")
    features = [_parse_feature(f, commands, f"{where}.features[{i}]")
                for i, f in enumerate(root.get("features", []))]

    return Profile(
        id=str(_require(dev, "id", f"{where}.device")), name=str(dev.get("name", dev.get("id"))),
        kind=kind, enabled=enabled, verified=bool(dev.get("verified", False)),
        ids=ids, transport=transport, commands=commands, features=features,
        usage_page=_opt_int(match, "usage_page", f"{where}.match"),
        usage=_opt_int(match, "usage", f"{where}.match"),
        interface_number=_opt_int(match, "interface_number", f"{where}.match"),
        author=str(dev.get("author", "")), notes=str(dev.get("notes", "")), source=source,
    )


# ----------------------------------------------------------------------------
# Carga desde disco
# ----------------------------------------------------------------------------
def load_profile(path: Union[str, Path]) -> Profile:
    """Lee y valida UN archivo de perfil (.json, o .yaml/.yml si hay PyYAML)."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    try:
        if path.suffix.lower() in (".yaml", ".yml"):
            try:
                import yaml  # type: ignore
            except ImportError as exc:
                raise ProfileError("para perfiles YAML instala PyYAML (pip install pyyaml)") from exc
            data = yaml.safe_load(text)
        else:
            data = json.loads(text)
    except ProfileError:
        raise
    except Exception as exc:  # JSON/YAML mal formado
        raise ProfileError(f"no se pudo leer {path.name}: {exc}") from exc
    return parse_profile(data, source=path)


def _user_config_dirs() -> List[Path]:
    """Perfiles del usuario: el de este proceso y, bajo sudo/pkexec, el del usuario real (no /root)."""
    dirs = [user_profile_dir()]
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and os.geteuid() == 0:
        try:
            import pwd
            dirs.append(Path(pwd.getpwnam(sudo_user).pw_dir) / ".config" / "mchose-hub" / "profiles")
        except (KeyError, ImportError):
            pass
    return dirs


def user_profile_dir() -> Path:
    """Dónde guarda la GUI los perfiles creados por el usuario."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "mchose-hub" / "profiles"


def default_profile_dirs() -> List[Path]:
    """Orden de búsqueda; los últimos pisan a los primeros si repiten `device.id`."""
    dirs = [Path(__file__).resolve().parent.parent / "profiles",   # perfiles que vienen con la app
            Path("/etc/mchose-hub/profiles")]                      # perfiles de todo el sistema
    dirs += _user_config_dirs()                                    # perfiles del usuario
    dirs += [Path(p) for p in os.environ.get("MCHOSE_PROFILES_DIR", "").split(os.pathsep) if p]
    return dirs


def load_profiles(dirs: Optional[List[Path]] = None,
                  include_disabled: bool = False) -> Tuple[List[Profile], List[str]]:
    """Carga todos los perfiles. Devuelve (perfiles, errores): un perfil roto no tumba a los demás."""
    found: Dict[str, Profile] = {}
    errors: List[str] = []
    for d in (dirs if dirs is not None else default_profile_dirs()):
        d = Path(d)
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if p.suffix.lower() not in (".json", ".yaml", ".yml"):
                continue
            try:
                prof = load_profile(p)
            except (ProfileError, OSError) as exc:
                errors.append(f"{p}: {exc}")
                log.warning("Perfil ignorado %s: %s", p, exc)
                continue
            found[prof.id] = prof
    return [p for p in found.values() if include_disabled or p.enabled], errors
