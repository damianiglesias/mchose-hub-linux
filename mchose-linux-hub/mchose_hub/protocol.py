"""
protocol.py — Motor de protocolo (puro, sin E/S).

Convierte  «comando + valores lógicos»  →  frame de bytes listo para enviar, y
«bytes recibidos»  →  diccionario de campos. No sabe nada de USB: así se puede probar
sin hardware y reutilizar con cualquier backend (hidapi, pyusb, un mock…).

Convenciones (las mismas que documenta el README):
  * El frame incluye el Report ID en el byte 0 y mide exactamente `transport.report_length`.
  * `request` en el JSON NO incluye el Report ID: se antepone solo. El resto se rellena con 0x00.
  * Los offsets de checksum y de respuesta cuentan desde el byte 0 del buffer.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple

from .profile import (ChecksumSpec, CommandSpec, FieldSpec, INT_WIDTHS, ParamSpec,
                      TransportSpec)


class ProtocolError(Exception):
    """Error al construir o interpretar un paquete."""


class ParamError(ProtocolError):
    """Un valor proporcionado por el usuario no es válido para el parámetro."""


class ResponseError(ProtocolError):
    """La respuesta recibida es demasiado corta o no tiene el formato esperado."""


# ----------------------------------------------------------------------------
# Codificación de parámetros
# ----------------------------------------------------------------------------
def _pack(spec: ParamSpec, raw: int) -> bytes:
    width = spec.width
    if not 0 <= raw < (1 << (8 * width)):
        raise ParamError(f"'{spec.name}': el valor crudo {raw} no cabe en {spec.type}")
    order = "big" if spec.type.endswith("be") else "little"
    return raw.to_bytes(width, order)


def _to_rgb(value: Any, name: str) -> Tuple[int, int, int]:
    """Acepta '#RRGGBB', 'RRGGBB' o una tupla/lista (r, g, b) de 0–255."""
    try:
        if isinstance(value, str):
            h = value.strip().lstrip("#")
            if len(h) != 6:
                raise ValueError
            rgb = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
        else:
            rgb = tuple(int(x) for x in value)  # type: ignore[assignment]
        if len(rgb) != 3 or not all(0 <= c <= 255 for c in rgb):
            raise ValueError
        return rgb  # type: ignore[return-value]
    except (ValueError, TypeError):
        raise ParamError(f"'{name}': color inválido {value!r} (usa '#RRGGBB')") from None


def encode_param(spec: ParamSpec, value: Any) -> bytes:
    """Valor lógico → bytes del frame, aplicando choices / linear / rango."""
    if value is None:
        value = spec.default
    if value is None:
        raise ParamError(f"falta el parámetro '{spec.name}' (y no tiene 'default')")

    if spec.type == "rgb":
        comp = dict(zip("rgb", _to_rgb(value, spec.name)))
        return bytes(comp[c] for c in spec.rgb_order)

    if spec.choices:  # dominio discreto: sólo valores de la lista
        match = next((c for c in spec.choices if c.value == value), None)
        if match is None:
            raise ParamError(f"'{spec.name}': {value!r} no es una opción válida "
                             f"({[c.value for c in spec.choices]})")
        if match.wire is not None:
            return _pack(spec, match.wire)

    if spec.type == "bool":
        return _pack(spec, 1 if value else 0)

    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ParamError(f"'{spec.name}': se esperaba un número, no {value!r}") from None
    if (spec.min is not None and num < spec.min) or (spec.max is not None and num > spec.max):
        raise ParamError(f"'{spec.name}': {value} fuera de rango [{spec.min}, {spec.max}]")
    raw = int(round(num))
    if spec.linear:
        mul, div, add = spec.linear
        raw = raw * mul // div + add
    return _pack(spec, raw)


# ----------------------------------------------------------------------------
# Construcción del frame
# ----------------------------------------------------------------------------
def apply_checksum(frame: bytearray, cs: ChecksumSpec) -> None:
    """Escribe el checksum en `frame[cs.store_at]`. Para algoritmos nuevos añade un `elif`."""
    data = frame[cs.start:cs.end]
    if cs.algorithm == "sum8":
        value = (cs.init + sum(data)) & 0xFF
    elif cs.algorithm == "sub8":
        value = (cs.init - sum(data)) & 0xFF
    elif cs.algorithm == "xor8":
        value = cs.init & 0xFF
        for b in data:
            value ^= b
    else:  # el validador de perfiles ya lo impide; defensa extra
        raise ProtocolError(f"checksum desconocido: {cs.algorithm}")
    frame[cs.store_at] = value


def build_frame(transport: TransportSpec, command: CommandSpec,
                values: Optional[Mapping[str, Any]] = None) -> bytes:
    """Plantilla del comando + valores → frame completo (Report ID, payload, relleno, checksum)."""
    values = dict(values or {})
    unknown = set(values) - set(command.params)
    if unknown:
        raise ParamError(f"el comando '{command.name}' no tiene los parámetros {sorted(unknown)}")

    body = bytearray()
    for tok in command.tokens:
        if isinstance(tok, int):                      # byte literal
            body.append(tok)
        else:                                         # marcador {param}
            body += encode_param(command.params[tok], values.get(tok))

    # El comando puede sobreescribir report_id y report_length del transporte
    report_id = command.report_id if command.report_id is not None else transport.report_id
    report_length = command.report_length if command.report_length is not None else transport.report_length

    frame = bytearray(report_length)                  # ya rellena con ceros
    frame[0] = report_id
    frame[1:1 + len(body)] = body
    if transport.checksum:
        apply_checksum(frame, transport.checksum)
    if transport.xor_mask:
        # El byte 0 es el Report ID y NO se invierte; los demás sí
        for i in range(1, len(frame)):
            frame[i] ^= transport.xor_mask
    return bytes(frame)


# ----------------------------------------------------------------------------
# Interpretación de la respuesta
# ----------------------------------------------------------------------------
def response_matches(command: CommandSpec, data: bytes, xor_mask: int = 0x00) -> bool:
    """¿Es `data` la respuesta a este comando? (comprueba los bytes 'expect' del perfil)."""
    if command.response is None:
        return False
    if xor_mask:
        data = bytes([data[0]] + [b ^ xor_mask for b in data[1:]])
    return all(len(data) > off and data[off] == val for off, val in command.response.expect.items())


def _decode_field(f: FieldSpec, data: bytes) -> Any:
    width = INT_WIDTHS[f.type]
    if len(data) < f.offset + width:
        raise ResponseError(f"respuesta de {len(data)} bytes: no contiene el campo '{f.name}' "
                            f"(offset {f.offset}, {width} B)")
    raw = int.from_bytes(data[f.offset:f.offset + width], "big" if f.type.endswith("be") else "little")
    if f.mask is not None:
        raw &= f.mask
    raw >>= f.shift
    if f.linear:
        mul, div, add = f.linear
        raw = raw * mul // div + add
    if f.as_bool:
        return raw != 0
    return f.labels.get(str(raw), raw)


def parse_response(command: CommandSpec, data: bytes,
                   xor_mask: int = 0x00) -> Dict[str, Any]:
    """Bytes recibidos → {nombre_campo: valor} según `response.fields` del perfil."""
    if command.response is None:
        return {}
    if xor_mask:
        data = bytes([data[0]] + [b ^ xor_mask for b in data[1:]])
    return {name: _decode_field(f, data) for name, f in command.response.fields.items()}
