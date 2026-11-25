"""Métodos de backend CLI para GUI."""

from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger(__name__)


class CliBackendMethodsMixin:
    """Agrega métodos de backend CLI a cualquier clase de backend."""

    @staticmethod
    def get_current_profile_path() -> str | None:
        """Devuelve la ruta del perfil activo actual."""
        # Intentar leer desde el directorio de perfiles
        import os
        from pathlib import Path
        profiles_dir = Path("/home/damian/.config/mchose-hub/profiles")
        if profiles_dir.exists():
            for p in sorted(profiles_dir.glob("*.json")):
                # Buscar el que tiene "active": true o el más reciente
                try:
                    import json
                    with open(p, encoding="utf-8") as f:
                        profile = json.load(f)
                    if profile.get("active", False):
                        return str(p)
                except Exception:
                    pass
        # Devolver el más reciente si no hay active
        for p in sorted(profiles_dir.glob("*.json")):
            return str(p)
        return None

    @staticmethod
    def execute_profile_command(profile_path: str, command: str, *args: Any, **kwargs: Any) -> bool:
        """Ejecuta un comando de perfil (dpi, rate, etc.) usando el backend CLI."""
        try:
            log.info(f"Ejecutando {command}={args[0] if args else 'default'} para perfil {profile_path}")
            
            # Construir el comando adecuado según el tipo de parámetro
            cmd_parts = ["mchose", command]
            for arg in args:
                cmd_parts.append(str(arg))
            
            result = subprocess.run(
                cmd_parts,
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0:
                log.info(f"Comando exitoso: {result.stdout.strip()}")
                return True
            else:
                error_msg = result.stderr.strip()
                log.error(f"Comando falló: {error_msg}")
                return False
                
        except subprocess.TimeoutExpired as e:
            log.error(f"Timeout ejecutando comando: {e}")
            raise TimeoutError("El comando tomó demasiado tiempo")
        except Exception as e:
            log.exception("Error ejecutando comando CLI")
            raise RuntimeError(f"Error al ejecutar comando: {e}")

    @staticmethod
    def execute_effect(effect_name: str, effect_args: dict) -> bool:
        """Ejecuta un efecto RGB usando el backend CLI."""
        try:
            log.info(f"Ejecutando efecto RGB: {effect_name} con args={effect_args}")
            
            cmd = ["mchose", "rgb", "set", effect_name]
            for key, value in effect_args.items():
                cmd.extend(["--" + key.replace("_", "-"), str(value)])
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0:
                log.info(f"Efecto exitoso: {result.stdout.strip()}")
                return True
            else:
                error_msg = result.stderr.strip()
                log.error(f"Efecto falló: {error_msg}")
                return False
                
        except Exception as e:
            log.exception("Error ejecutando efecto RGB")
            return False
