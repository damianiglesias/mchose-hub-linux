"""
cli_device.py — Dispositivo que envía comandos vía el CLI de alexfrih/mchose-linux.

Este módulo expone la misma interfaz que MchoseDevice (open, close, execute),
pero delega en las llamadas al binario 'mchose' en lugar del motor HID propio.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .cli_backend import MchoseCliBackend, MchoseCliError, MouseState

log = logging.getLogger(__name__)


class MchoseCliDevice:
    """Dispositivo MCHOSE que usa el CLI de alexfrih como backend."""

    def __init__(self, profile_data: Optional[Dict[str, Any]] = None) -> None:
        self._profile = profile_data or {}
        self._backend = MchoseCliBackend()

    @property
    def info(self) -> Dict[str, str]:
        """Información del dispositivo desde el CLI."""
        state = self._backend.get_state()
        return {
            "display_name": f"MCHOSE {state.profile}",
            "vid_pid": "5253:1020",  # MCHOSE L7 Pro
            "transport": "wireless_2.4g" if state.link else "wired",
        }

    @property
    def profile(self) -> Dict[str, Any]:
        """Perfil asociado (leído del JSON o datos por defecto)."""
        return self._profile

    @property
    def state(self) -> Optional[MouseState]:
        """Estado actual del ratón."""
        return self._backend.get_state()

    def open(self) -> None:
        """El CLI no requiere abrir explícitamente; siempre está listo."""
        pass

    def close(self) -> None:
        """Cierre simple; el backend persiste las settings."""
        pass

    def execute(self, command: str, **payload: Any) -> Optional[Dict[str, Any]]:
        """
        Ejecuta un comando delegando en el CLI.

        Mapeo de comandos:
          - dpi: set_dpi_stage (stage_index + dpi)
          - stage: set_active_stage (stage_index)
          - rate: set_rate (hz)
          - lod: set_lod (mm)
          - debounce: set_debounce (ms)
          - sleep: set_sleep (minutes)
          - motion-sync: set_motion_sync (on/off)
          - ripple: set_ripple (on/off)
          - angle-snap: set_angle_snap (on/off)
        """
        try:
            # Mapear el nombre del comando a un método del backend
            method_map = {
                "dpi": self._execute_dpi,
                "stage": self._execute_stage,
                "rate": self._execute_rate,
                "lod": self._execute_lod,
                "debounce": self._execute_debounce,
                "sleep": self._execute_sleep,
                "motion-sync": self._execute_motion_sync,
                "ripple": self._execute_ripple,
                "angle-snap": self._execute_angle_snap,
                "get_config": lambda _: None,      # read-only, no output
                "get_battery": lambda _: None,     # read-only, battery en show/info
                "get_rgb": lambda _: None,         # no implementación del CLI
            }

            if command not in method_map:
                log.warning(f"Comando desconocido para CLI backend: {command}")
                return None

            method = method_map[command]
            result = method(command, **payload)
            return result if result is not None else None

        except MchoseCliError as e:
            log.error(f"Error del CLI en '{command}': {e}")
            raise

    # -- métodos específicos por comando -----------------------------------------
    def _execute_dpi(self, command: str, stage_index: int, dpi: int) -> None:
        """Cambia el DPI de un perfil específico (stage_index 0-5)."""
        self._backend.set_dpi_stage(stage_index, dpi)

    def _execute_stage(self, command: str, stage_index: int) -> None:
        """Activa un perfil de DPI (stage_index 0-5)."""
        self._backend.set_active_stage(stage_index)

    def _execute_rate(self, command: str, polling: bool, rate_hz: int) -> None:
        """Configura la tasa de muestreo (Hz)."""
        self._backend.set_rate(rate_hz)

    def _execute_lod(self, command: str, lod_mm: int) -> None:
        """Cambia el LOD (longitud del disparo, en mm)."""
        self._backend.set_lod(lod_mm)

    def _execute_debounce(self, command: str, debounce_ms: int) -> None:
        """Ajusta el debounce (ms)."""
        self._backend.set_debounce(debounce_ms)

    def _execute_sleep(self, command: str, sleep_minutes: int) -> None:
        """Configura el modo de sueño (minutos; 0 = off)."""
        self._backend.set_sleep(sleep_minutes)

    def _execute_motion_sync(self, command: str, on: bool) -> None:
        """Activa/desactiva motion sync."""
        self._backend.set_motion_sync(on)

    def _execute_ripple(self, command: str, on: bool) -> None:
        """Activa/desactiva ripple (puede no existir en el CLI)."""
        try:
            self._backend.set_ripple(on)
        except MchoseCliError:
            pass  # Comando soportado por la GUI pero no por este hardware

    def _execute_angle_snap(self, command: str, on: bool) -> None:
        """Activa/desactiva angle snap."""
        self._backend.set_angle_snap(on)
