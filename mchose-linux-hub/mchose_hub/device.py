"""
device.py — Metadatos de descubrimiento y definición de perfiles.

NOTA: La implementación del "motor HID personalizado" ha sido eliminada porque el dongle 2.4G
bloquea el canal largo (0x12) para feature reports, haciendo que los comandos DPI/polling fallen.

Ahora toda la E/S se hace mediante cli_backend.py usando el binario mchose verificado en hardware.

Este módulo solo contiene:
- Definiciones de perfiles predefinidos con sus características y parámetros.
- Funciones auxiliares para descubrimiento/metadatos del dispositivo (no más código HID roto).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


# ============================================================================
# Metadatos de perfiles predefinidos (no motor HID)
# ============================================================================

@dataclass
class FeatureSpec:
    """Definición estática de una característica del perfil."""
    id: str                    # identificador único para claves en _state_for()/_pending/...
    label: str                 # nombre mostrado al usuario ("DPI", "Polling rate")
    description: Optional[str]  # subtítulo opcional
    command: str               # comando CLI ('dpi', 'rate', etc.) o ruta de archivo para RGB, mapping
    param: str                 # parámetro en el estado (p. ej., 'pixels_per_inch' para DPI)
    unit: Optional[str] = None  # unidad mostrada ("DPI", "Hz")

    fixed: List[Dict[str, Any]] | Dict[str, Any] = field(default_factory=list)   # valores fijos por slot (mapping/RGB)
    
    choices: List[AnyParamChoice] = field(default_factory=list)                  # opciones para desplegable o slider
    slots: List[AnyMappingSlot] = field(default_factory=list)                    # slots de mapping
    
    group: str = "General"  # grupo en la vista ("DPI", "Polling", "RGB")

    
@dataclass  
class AnyParamChoice:
    """Opción para un parámetro (desplegable/slider)."""
    label: str          # texto mostrado al usuario
    value: int | float  # valor numérico que se envía a mchose


# ============================================================================
# Definición de perfiles predefinidos
# ============================================================================

@dataclass  
class ProfileSpec:
    """Especificación estática de un perfil (nombre, características, etc.)."""
    name: str                    # nombre del archivo JSON en profiles/
    kind: str = "mouse"          # tipo principal ("mouse", "keyboard") o mixto
    features: List[FeatureSpec]  # lista de FeatureSpec que define la vista
    
    @property  
    def choices(self) -> Dict[str, AnyParamChoice]:
        """Devuelve un dict {feat_id → [choices]} para construir widgets."""
        return {f.id: f.choices for f in self.features if hasattr(f, 'choices')}


# --- perfiles predefinidos ---

PROFILE_SPECIFICATIONS = {  
    # Perfiles de mouse con DPI y Polling (los más comunes)
    "L7 Pro": ProfileSpec(
        name="l7_pro.json",
        features=[
            FeatureSpec(id="dpi", label="DPI", description="Sensibilidad del cursor", command="dpi", param="pixels_per_inch", unit="DPI", group="General"),
            
            # Polling rate (125Hz-1000Hz) — slider o desplegable según prefieras. Usamos choices para discreto: 125/250/500/1000
            FeatureSpec(id="poll_rate", label="Polling rate", description="Frecuencia de lectura (menor = más batería)", command="rate", param="poll_hz", unit="Hz", group="General"),
            
            # RGB — desplegable para efectos, fijo por slot. El valor se guarda en _state_for('rgb_led', 'effect') y el color en _state_for('rgb_led', 'color').
            FeatureSpec(id="led_effect", label="Efecto LED", description="Modo de iluminación", command="rgb_led", param="effect_id", group="RGB"),
            
            # Color RGB (se guarda por separado) — no necesita choices, solo un color hex. El widget _build_color() lo maneja.
        ],
    ),  
}
