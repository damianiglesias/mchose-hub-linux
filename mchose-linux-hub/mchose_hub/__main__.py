"""
__main__.py — Punto de entrada.   `mchose-hub`  abre la GUI; los subcomandos sirven para diagnosticar
y para que la comunidad cree perfiles nuevos:

    mchose-hub                      abre la interfaz gráfica
    mchose-hub list [--all]         lista interfaces HID (VID/PID/usage page) y qué perfil las reclama
    mchose-hub validate [archivos]  comprueba que tus JSON son válidos
    mchose-hub send ID CMD k=v …    envía un comando de un perfil (sin GUI) y muestra la respuesta
    mchose-hub inspect [--all]      informe de interfaces y reports HID (para crear perfiles)
    mchose-hub udev [--install]     genera/instala las reglas udev (perfiles + MCHOSE conectados)
    mchose-hub --demo               GUI con un ratón simulado (sin hardware)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .profile import ProfileError, default_profile_dirs, load_profile, load_profiles


def _backend(demo: bool):
    from .device import HidBackend
    if demo:
        from .demo import FakeHid
        return HidBackend(FakeHid())
    return HidBackend()


def cmd_list(args: argparse.Namespace) -> int:
    from .device import BackendUnavailable, entry_matches
    from .udevgen import KNOWN_VIDS
    try:
        backend = _backend(args.demo)
    except BackendUnavailable as exc:
        print(exc, file=sys.stderr)
        return 2
    profiles, errors = load_profiles(include_disabled=args.demo)
    for err in errors:
        print(f"aviso: {err}", file=sys.stderr)
    known = {vid for p in profiles for vid in p.vendor_ids} | set(KNOWN_VIDS)

    print(f"{'VID:PID':<10}{'USAGE':<12}{'IF':<4}{'PRODUCTO':<32}{'RUTA':<18}PERFIL")
    shown = 0
    for e in sorted(backend.enumerate(), key=lambda e: (e["vendor_id"], e["product_id"], e.get("interface_number", -1))):
        text = f"{e.get('manufacturer_string') or ''} {e.get('product_string') or ''}".lower()
        if not (args.all or e["vendor_id"] in known or "mchose" in text):
            continue
        match = next((p.id for p in profiles if entry_matches(p, e)), "")
        path = e["path"].decode(errors="replace") if isinstance(e["path"], bytes) else str(e["path"])
        usage = f"{e.get('usage_page', 0):04X}/{e.get('usage', 0):04X}"
        product = (e.get("product_string") or "")[:30]
        print(f"{e['vendor_id']:04X}:{e['product_id']:04X}  {usage:<12}{e.get('interface_number', -1):<4}"
              f"{product:<32}{path:<18}{match or '—'}")
        shown += 1
    if not shown:
        print("(ninguna interfaz MCHOSE conocida; prueba con --all y busca tu dispositivo por nombre)")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    paths: List[Path] = [Path(f) for f in args.files]
    if not paths:
        for d in default_profile_dirs():
            if d.is_dir():
                paths += sorted(p for p in d.iterdir() if p.suffix.lower() in (".json", ".yaml", ".yml"))
    bad = 0
    for p in paths:
        try:
            prof = load_profile(p)
        except (ProfileError, OSError) as exc:
            bad += 1
            print(f"✗ {p}: {exc}")
        else:
            state = "" if prof.enabled else "  [deshabilitado]"
            print(f"✓ {p.name}: {prof.name} — {len(prof.commands)} comandos, {len(prof.features)} controles{state}")
    return 1 if bad else 0


def cmd_send(args: argparse.Namespace) -> int:
    from .device import BackendUnavailable, DeviceError, MchoseDevice, discover
    from .protocol import ProtocolError
    values = {}
    for item in args.params:
        key, sep, raw = item.partition("=")
        if not sep:
            print(f"parámetro '{item}': usa nombre=valor", file=sys.stderr)
            return 2
        try:
            values[key] = json.loads(raw)       # 800 → int, true → bool, "#ff0000" → str
        except ValueError:
            values[key] = raw
    try:
        backend = _backend(args.demo)
    except BackendUnavailable as exc:
        print(exc, file=sys.stderr)
        return 2
    profiles, _ = load_profiles(include_disabled=args.demo)
    profile = next((p for p in profiles if p.id == args.profile), None)
    if profile is None:
        print(f"No hay un perfil habilitado con id '{args.profile}'", file=sys.stderr)
        return 1
    devices = discover([profile], backend)
    if not devices:
        print("No hay ningún dispositivo conectado que case con ese perfil", file=sys.stderr)
        return 1
    try:
        with MchoseDevice(devices[0], backend) as dev:
            result = dev.execute(args.command, **values)
    except (DeviceError, ProtocolError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print("OK (el comando no devuelve datos)" if result is None
          else json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_udev(args: argparse.Namespace) -> int:
    from .udevgen import (RULES_PATH, collect_vids, detect_mchose_vids, existing_vids, reload_udev,
                          render_rules, write_rules)
    profiles, _ = load_profiles()
    try:
        extra = [int(v, 0) for v in args.vid]
    except ValueError:
        print("--vid debe ser un número, p. ej. 0x320F", file=sys.stderr)
        return 2
    detected = {}
    if not args.no_detect:                       # lee el hardware conectado ahora (puede fallar sin hidapi: no pasa nada)
        try:
            detected = detect_mchose_vids(_backend(False).enumerate())
        except Exception:
            pass
    previous = {} if args.reset else existing_vids(RULES_PATH)
    vids = collect_vids(profiles, extra, include_known=not args.no_known, detected=detected, previous=previous)
    content = render_rules(vids, args.group)
    if not (args.install or args.output):
        print(content)
        return 0
    path = args.output or RULES_PATH
    try:
        write_rules(content, path)
    except PermissionError:
        print(f"Sin permiso para escribir {path}. Ejecuta: sudo mchose-hub udev --install", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"No se pudo escribir {path}: {exc}", file=sys.stderr)
        return 1
    print(f"Reglas escritas en {path}")
    if args.install:
        for note in reload_udev():
            print(f"aviso: {note}", file=sys.stderr)
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    from .device import BackendUnavailable, discover_unprofiled
    from .hidinfo import device_report
    try:
        backend = _backend(args.demo)
    except BackendUnavailable as exc:
        print(exc, file=sys.stderr)
        return 2
    profiles, _ = load_profiles(include_disabled=args.demo)
    devices = discover_unprofiled(profiles, backend, claimed=None, show_all=args.all)
    if not devices:
        print("No se detecta ningún dispositivo MCHOSE (prueba con --all).")
        return 1
    print("\n\n".join(device_report(d, backend) for d in devices))
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    try:
        from .gui import run
    except (ImportError, ValueError) as exc:    # gi.require_version lanza ValueError si falta el typelib
        print(f"No se pudo cargar GTK4/libadwaita: {exc}\nEjecuta install.sh para instalar las dependencias.",
              file=sys.stderr)
        return 2
    return run(demo=args.demo)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="mchose-hub", description="MCHOSE Linux Hub")
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="registro detallado (DEBUG)")
    parser.add_argument("--demo", action="store_true", help="usa un dispositivo simulado (sin hardware)")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("gui", help="abre la interfaz gráfica (por defecto)")
    p = sub.add_parser("list", help="lista las interfaces HID y los perfiles que las reclaman")
    p.add_argument("--all", action="store_true", help="muestra todas, no sólo las MCHOSE/conocidas")
    p = sub.add_parser("validate", help="valida archivos de perfil")
    p.add_argument("files", nargs="*", help="si se omite, valida todos los perfiles instalados")
    p = sub.add_parser("send", help="envía un comando de un perfil")
    p.add_argument("profile", help="device.id del perfil")
    p.add_argument("command", help="nombre del comando")
    p.add_argument("params", nargs="*", help="nombre=valor (p. ej. dpi=1600)")
    p = sub.add_parser("udev", help="genera o instala las reglas udev")
    p.add_argument("--install", action="store_true", help="escribe en el sistema y recarga udev (root)")
    p.add_argument("--output", help="escribe en este archivo en lugar de la salida estándar")
    p.add_argument("--group", help="usa un grupo en vez de uaccess (sistemas sin systemd-logind)")
    p.add_argument("--vid", action="append", default=[], help="VID extra (repetible)")
    p.add_argument("--no-known", action="store_true", help="no incluir los VID candidatos")
    p.add_argument("--no-detect", action="store_true", help="no añadir los VID de los MCHOSE conectados ahora")
    p.add_argument("--reset", action="store_true", help="no conservar los VID de las reglas ya instaladas")
    p = sub.add_parser("inspect", help="informe de interfaces y reports HID de los dispositivos MCHOSE")
    p.add_argument("--all", action="store_true", help="todos los dispositivos HID, no sólo los MCHOSE")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    handler = {"list": cmd_list, "validate": cmd_validate, "send": cmd_send,
               "udev": cmd_udev, "inspect": cmd_inspect}.get(args.cmd, cmd_gui)
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
